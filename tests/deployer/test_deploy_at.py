"""deploy.py --at <ref>: a commit of the checkout deployed from its exported tree, with the checkout's HEAD unmoved."""

from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from harness import REPOSITORY_ROOT, SOURCE_ID, DeployerTestCase, Result, git

from deployer import fsops, platform_support, source_ref, tools
from deployer.errors import DeployError


class RefValidationTests(unittest.TestCase):
    def test_a_ref_is_a_git_revision_that_cannot_be_read_as_an_option(self) -> None:
        for ref in ("v0.5.0", "main", "origin/main", "HEAD~2", "v1.0.0^{commit}", "@{u}", "a1b2c3d", "release_1+x"):
            with self.subTest(ref=ref):
                source_ref.check_ref(ref)
        refused = ("", "-x", "--upload-pack=evil", "-", "a b", "a\nb", "HEAD:path", "a;b", "$(x)", "x" * 256)
        for ref in refused:
            with self.subTest(ref=ref), self.assertRaises(DeployError) as raised:
                source_ref.check_ref(ref)
            self.assertEqual(2, raised.exception.exit_code)
            self.assertEqual(
                (
                    f"ERROR: --at must name a branch, tag, or commit of this checkout (got: {ref!r})",
                    "A ref may not start with '-' and may hold only letters, digits, and . _ / @ { } ~ ^ + -.",
                    "Run 'python deploy.py --help' for usage.",
                ),
                raised.exception.lines,
            )

    def test_git_is_asked_for_the_commit_with_the_options_ended_before_the_ref(self) -> None:
        commit = "0123456789abcdef0123456789abcdef01234567"
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / ".git").mkdir()
            result = platform_support.ToolResult(0, f"{commit}\n")
            with (
                mock.patch.object(platform_support, "find_executable", return_value="git"),
                mock.patch.object(platform_support, "run_tool", return_value=result) as run_tool,
            ):
                self.assertEqual(source_ref.Pin("v1.0.0", commit), source_ref.resolve(checkout, "v1.0.0"))
        run_tool.assert_called_once_with(
            ["git", "-C", str(checkout), "rev-parse", "--verify", "--quiet", "--end-of-options", "v1.0.0^{commit}"]
        )

    def test_only_a_full_commit_name_is_accepted_from_git(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / ".git").mkdir()
            for output in ("abc1234\n", "0123456789abcdef0123456789abcdef01234567\nsecond line\n", ""):
                with (
                    self.subTest(output=output),
                    mock.patch.object(platform_support, "find_executable", return_value="git"),
                    mock.patch.object(
                        platform_support, "run_tool", return_value=platform_support.ToolResult(0, output)
                    ),
                    self.assertRaisesRegex(DeployError, "names no commit"),
                ):
                    source_ref.resolve(checkout, "main")


class DeployAtTests(DeployerTestCase):
    """A fixture repository with a tagged release and a later commit on main that changes alpha and adds beta."""

    def setUp(self) -> None:
        super().setUp()
        self.make_config()
        self.make_source_json()
        self.make_skill("alpha", "Alpha at the release")
        # Bytes as written, whatever the developer's core.autocrlf says.
        self.write(self.source / ".gitattributes", "* -text\n")
        git(self.source, "init", "-q", "-b", "main")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "Release")
        git(self.source, "tag", "v1.0.0")
        self.release = git(self.source, "rev-parse", "HEAD")
        self.make_skill("alpha", "Alpha on main")
        self.make_skill("beta", "Beta on main")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "Later work")
        self.head = git(self.source, "rev-parse", "HEAD")
        self.exports: list[Path] = []

    def deploy(self, *arguments: str, stdin: str = "", **options: Any) -> Result:
        """Deploy as the harness does, recording each directory an --at run exports into."""
        real = source_ref.export

        def export(source_dir: Path, commit: str) -> Path:
            made = real(source_dir, commit)
            self.exports.append(made)
            return made

        with mock.patch.object(source_ref, "export", export):
            return super().deploy(*arguments, stdin=stdin, **options)

    def check(self) -> Result:
        return self.run_cli("check")

    def assert_head_unmoved(self) -> None:
        self.assertEqual(self.head, git(self.source, "rev-parse", "HEAD"))
        self.assertEqual("main", git(self.source, "symbolic-ref", "--short", "HEAD"))
        self.assertEqual("", git(self.source, "status", "--porcelain"))

    def assert_exports_removed(self, count: int = 1) -> None:
        self.assertEqual(count, len(self.exports))
        for export in self.exports:
            self.assertFalse(export.exists(), export)

    def test_a_release_tag_deploys_its_skills_and_check_reports_it_with_the_head_unmoved(self) -> None:
        output = self.deploy_ok("--all", "--at", "v1.0.0").output
        self.assertIn(f"At: v1.0.0 ({self.release})", output)
        self.assertIn("Alpha at the release", self.skill_text("alpha"))
        self.assertFalse((self.skills_dir / "beta").exists())
        entry = self.manifest()["sources"][SOURCE_ID]
        self.assertEqual(
            (str(self.source).replace("\\", "/"), self.release, "v1.0.0"),
            (entry["source_dir"], entry["source_commit"], entry["source_ref"]),
        )
        self.assert_head_unmoved()
        self.assert_exports_removed()
        self.assertIn(f"Deployed commit: v1.0.0 ({self.release}), pinned with --at v1.0.0\n", self.check().output)

    def test_a_plain_deployment_replaces_the_pin_and_says_so(self) -> None:
        self.deploy_ok("--all", "--at", "v1.0.0")
        replaced = f"the deployment pinned with --at v1.0.0 to v1.0.0 ({self.release})."
        self.assertIn(f"\nWould replace {replaced}\n", self.deploy_ok("--all", "--dry-run").output)
        self.assertIn(f"\nReplaced {replaced}\n", self.deploy_ok("--all").output)
        entry = self.manifest()["sources"][SOURCE_ID]
        self.assertEqual(self.head, entry["source_commit"])
        self.assertNotIn("source_ref", entry)
        self.assertIn("Alpha on main", self.skill_text("alpha"))
        self.assertTrue((self.skills_dir / "beta" / "SKILL.md").is_file())
        self.assertNotIn("Replaced the deployment pinned", self.deploy_ok("--all").output)

    def test_the_update_skill_reports_the_line_the_deployer_prints_over_a_pin(self) -> None:
        # update-coding-agent-skills deploys main's tip and reports a replaced pin by quoting this line.
        skill = REPOSITORY_ROOT / "skills" / "update-coding-agent-skills" / "SKILL.md"
        quoted = "If the deployer printed `Replaced the deployment pinned with --at <ref> ...`"
        self.assertIn(quoted, skill.read_text(encoding="utf-8"))
        self.deploy_ok("--all", "--at", "v1.0.0")
        self.assertIn("\nReplaced the deployment pinned with --at v1.0.0 to ", self.deploy_ok("--all").output)

    def test_the_update_skill_reads_the_pin_from_the_line_check_prints(self) -> None:
        # update-coding-agent-skills stops with PINNED <ref> <commit>, read from this line of check's output.
        script = REPOSITORY_ROOT / "skills" / "update-coding-agent-skills" / "scripts" / "update.sh"
        text = script.read_text(encoding="utf-8")
        self.assertIn('PIN_MARK=", pinned with --at "', text)
        self.assertIn('grep -m 1 -e "^Deployed commit: .*$PIN_MARK"', text)
        self.deploy_ok("--all", "--at", "v1.0.0")
        self.assertIn(f"\nDeployed commit: v1.0.0 ({self.release}), pinned with --at v1.0.0\n", self.check().output)

    def test_deploying_the_pinned_commit_again_keeps_the_pin_without_a_notice(self) -> None:
        self.deploy_ok("--all", "--at", "v1.0.0")
        output = self.deploy_ok("--all", "--at", self.release).output
        self.assertNotIn("Replaced", output)
        self.assertEqual(self.release, self.manifest()["sources"][SOURCE_ID]["source_ref"])

    def test_a_dry_run_at_a_ref_previews_that_tree_and_changes_nothing(self) -> None:
        before = self.deploy_ok("--all", "--at", "v1.0.0", "--dry-run").output
        self.assertIn(f"At: v1.0.0 ({self.release})", before)
        self.assertIn("Rendered and validated in memory (dry run).", before)
        self.assertNotIn("beta", before)
        self.assertFalse(self.manifest_file.exists())
        self.assertFalse((self.skills_dir / "alpha").exists())
        self.assert_head_unmoved()
        self.assert_exports_removed()

    def test_a_canary_home_deploys_a_ref(self) -> None:
        canary = self.root / "canary"
        canary.mkdir()
        self.deploy_ok("--all", "--at", "v1.0.0", "--canary-home", str(canary))
        self.assertIn("Alpha at the release", (canary / ".claude" / "skills" / "alpha" / "SKILL.md").read_text())
        self.assert_exports_removed()

    def test_a_ref_that_reads_as_an_option_is_refused_before_git_runs(self) -> None:
        with mock.patch.object(platform_support, "run_tool") as run_tool:
            for ref in ("-x", "--upload-pack=evil"):
                result = self.deploy_fails("--all", f"--at={ref}", pattern=r"--at must name a branch, tag, or commit")
                self.assertEqual(2, result.code)
        run_tool.assert_not_called()
        self.assert_exports_removed(0)

    def test_a_ref_the_clone_does_not_hold_is_refused_with_the_fetch_remedy(self) -> None:
        output = self.deploy_fails("--all", "--at", "v9.9.9", pattern=r"ERROR: --at v9\.9\.9 names no commit").output
        self.assertIn("The deployer does not fetch: run 'git fetch --tags origin' in the checkout", output)
        self.assertIn('See "Deploying an earlier commit" in docs/recovery.md.', output)
        self.assert_exports_removed(0)

    def test_a_ref_whose_source_json_names_another_source_is_refused(self) -> None:
        git(self.source, "switch", "-q", "-c", "other")
        self.make_source_json("other/skills")
        git(self.source, "commit", "-q", "-am", "Another source")
        git(self.source, "switch", "-q", "main")
        output = self.deploy_fails("--all", "--at", "other", pattern=r"has source ID 'other/skills'").output
        self.assertIn(f"not this checkout's '{SOURCE_ID}'", output)
        self.assert_head_unmoved()
        self.assert_exports_removed()

    def test_a_folder_that_is_not_a_git_checkout_is_refused(self) -> None:
        plain = self.snapshot_source("plain")
        fsops.remove(plain / ".git")
        self.deploy_fails("--all", "--at", "main", source=plain, pattern=r"--at needs a Git checkout")

    def test_an_export_git_refuses_leaves_no_directory_behind(self) -> None:
        patch, made = self.record_temporary_directories()
        with patch, self.assertRaisesRegex(DeployError, "git archive could not export 0{40}"):
            source_ref.export(self.source, "0" * 40)
        self.assertEqual(1, len(made))
        self.assertFalse(made[0].exists())

    def commit_link(self, branch: str, name: str, target: str) -> None:
        """Commit a symbolic link to target on a new branch through the index alone, as a checkout on Windows may not
        be able to make one, then return to main."""
        blob_file = self.root / "link-target"
        blob_file.write_bytes(target.encode("utf-8"))
        blob = git(self.source, "hash-object", "-w", str(blob_file))
        git(self.source, "switch", "-q", "-c", branch)
        git(self.source, "update-index", "--add", "--cacheinfo", f"120000,{blob},{name}")
        git(self.source, "commit", "-q", "-m", "A link")
        git(self.source, "switch", "-q", "--discard-changes", "main")

    def record_temporary_directories(self) -> tuple[Any, list[Path]]:
        """A patch of the export's mkdtemp that records each directory it makes, and the list it records them in."""
        made: list[Path] = []
        real = tempfile.mkdtemp

        def mkdtemp(*positional: Any, **keywords: Any) -> str:
            made.append(Path(real(*positional, **keywords)))
            return str(made[-1])

        return mock.patch.object(source_ref.tempfile, "mkdtemp", mkdtemp), made

    def test_a_link_in_the_tree_is_refused_by_name_before_anything_is_extracted_or_deployed(self) -> None:
        patch, made = self.record_temporary_directories()
        for index, target in enumerate(("../../outside", "/etc/passwd", "C:/Windows")):
            with self.subTest(target=target):
                branch = f"link-{index}"
                self.commit_link(branch, "skills/alpha/escape", target)
                with patch:
                    result = self.deploy_fails(
                        "--all", "--at", branch, pattern=r"ERROR: The tree of [0-9a-f]{40} holds 'skills/alpha/escape'"
                    )
                self.assertEqual(1, result.code)
                self.assertIn(
                    "holds 'skills/alpha/escape', a symbolic link, which the deployer does not extract.\n"
                    "A deployable tree holds only files and folders; deploy a commit without links or special files.",
                    result.output,
                )
                self.assertNotIn("Traceback", result.output)
                self.assertFalse(self.manifest_file.exists())
                self.assertFalse((self.skills_dir / "alpha").exists())
                self.assert_head_unmoved()
        self.assertEqual(3, len(made))
        for directory in made:
            self.assertFalse(directory.exists(), directory)

    def export_archive(self, members: list[tuple[tarfile.TarInfo, bytes]]) -> tuple[DeployError, list[Path]]:
        """Export a commit whose git archive holds exactly these members, returning the refusal and each directory the
        export made."""
        patch, made = self.record_temporary_directories()

        def archive(source_dir: Path, *arguments: str) -> platform_support.ToolResult:
            output = next(argument for argument in arguments if argument.startswith("--output="))
            with tarfile.open(output.removeprefix("--output="), "w") as handle:
                for info, content in members:
                    info.size = len(content)
                    handle.addfile(info, io.BytesIO(content) if info.isfile() else None)
            return platform_support.ToolResult(0, "")

        with (
            patch,
            mock.patch.object(source_ref, "_git", archive),
            self.assertRaises(DeployError) as raised,
        ):
            source_ref.export(self.source, self.release)
        return raised.exception, made

    @staticmethod
    def member(name: str, kind: bytes = tarfile.REGTYPE, linkname: str = "") -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = linkname
        return info

    def test_a_member_tarfile_data_filter_refuses_is_named_with_the_filters_reason(self) -> None:
        # git archive writes no such member, so the filter stands behind the deployer's own check of each member.
        for name in ("../escape.txt", "skills/../../escape.txt"):
            with self.subTest(name=name):
                refusal, made = self.export_archive([(self.member("README.md"), b"kept\n"), (self.member(name), b"x")])
                self.assertEqual(1, refusal.exit_code)
                self.assertEqual(1, len(refusal.lines), refusal.lines)
                prefix = (
                    f"ERROR: The deployer refused to extract the tree of {self.release}: {name!r} would be extracted"
                )
                self.assertTrue(refusal.lines[0].startswith(prefix), refusal.lines)
                self.assertTrue(refusal.lines[0].endswith(", which is outside the destination."), refusal.lines)
                self.assertEqual(1, len(made))
                self.assertFalse(made[0].exists())

    def test_a_hard_link_or_special_file_is_refused_by_name(self) -> None:
        for info, kind in (
            (self.member("skills/alpha/copy", tarfile.LNKTYPE, "skills/alpha/SKILL.md"), "a hard link"),
            (self.member("skills/alpha/pipe", tarfile.FIFOTYPE), "a special file"),
            (self.member("skills/alpha/tty", tarfile.CHRTYPE), "a special file"),
        ):
            with self.subTest(name=info.name):
                refusal, made = self.export_archive([(self.member("skills/alpha/SKILL.md"), b"x\n"), (info, b"")])
                self.assertEqual(
                    (
                        f"ERROR: The tree of {self.release} holds {info.name!r}, {kind}, which the deployer does not "
                        "extract.",
                        "A deployable tree holds only files and folders; deploy a commit without links or special "
                        "files.",
                    ),
                    refusal.lines,
                )
                self.assertFalse(made[0].exists())

    def test_a_python_without_tarfile_extraction_filters_is_refused_before_git_or_anything_runs(self) -> None:
        # 3.11.4 is the first 3.11 release whose tarfile has extraction filters.
        with (
            mock.patch.object(tools, "python_version", return_value=(3, 11, 3)),
            mock.patch.object(platform_support, "run_tool") as run_tool,
            mock.patch.object(source_ref.tempfile, "mkdtemp") as mkdtemp,
        ):
            result = self.deploy_fails("--all", "--at", "v1.0.0", pattern=r"--at needs Python 3\.11\.4 or newer")
        self.assertEqual(1, result.code)
        self.assertIn(
            "ERROR: --at needs Python 3.11.4 or newer, whose tarfile extracts with filters; this is Python 3.11.3.\n"
            'See "Installing the tools" in docs/installation.md.',
            result.output,
        )
        run_tool.assert_not_called()
        mkdtemp.assert_not_called()
        self.assertFalse(self.manifest_file.exists())

    def test_a_recorded_ref_is_validated_when_the_manifest_is_read(self) -> None:
        self.deploy_ok("--all", "--at", "v1.0.0")
        data = self.manifest()
        for entry in ({"source_ref": "-x"}, {"source_ref": 1}, {"source_ref": "v1.0.0", "source_commit": None}):
            with self.subTest(entry=entry):
                broken = {**data, "sources": {SOURCE_ID: {**data["sources"][SOURCE_ID], **entry}}}
                if entry.get("source_commit", "") is None:
                    del broken["sources"][SOURCE_ID]["source_commit"]
                self.write_manifest(broken)
                self.deploy_fails("--all", "--dry-run", pattern=f"Manifest source '{SOURCE_ID}' source_.* is malformed")

    def test_at_cannot_be_combined_with_migrate_from(self) -> None:
        result = self.deploy_fails(
            "--at", "v1.0.0", "--migrate-from", "old/skills", pattern=r"--at cannot be combined with --migrate-from"
        )
        self.assertEqual(2, result.code)


if __name__ == "__main__":
    unittest.main()

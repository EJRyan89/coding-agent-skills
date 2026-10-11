"""deploy.py --at <ref>: a commit of the checkout deployed from its exported tree, with the checkout's HEAD unmoved."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from harness import REPOSITORY_ROOT, SOURCE_ID, DeployerTestCase, Result, git

from deployer import fsops, platform_support, source_ref
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
        made: list[Path] = []
        real = tempfile.mkdtemp

        def mkdtemp(*positional: Any, **keywords: Any) -> str:
            made.append(Path(real(*positional, **keywords)))
            return str(made[-1])

        with (
            mock.patch.object(source_ref.tempfile, "mkdtemp", mkdtemp),
            self.assertRaisesRegex(DeployError, "git archive could not export 0{40}"),
        ):
            source_ref.export(self.source, "0" * 40)
        self.assertEqual(1, len(made))
        self.assertFalse(made[0].exists())

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

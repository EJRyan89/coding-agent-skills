"""deploy.py --at <ref>: a commit of the checkout deployed from its exported tree, with the checkout's HEAD unmoved."""

from __future__ import annotations

import io
import itertools
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from harness import (
    DEPLOY_COMMAND,
    REPOSITORY_ROOT,
    SOURCE_ID,
    DeployerTestCase,
    Result,
    git,
    recorded_temporary_directories,
)

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
                    f"Run '{DEPLOY_COMMAND} --help' for usage.",
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
            ["git", "-C", str(checkout), "rev-parse", "--verify", "--quiet", "--end-of-options", "v1.0.0^{commit}"],
            timeout=None,
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

    def refusal(self) -> str:
        """The lines a deployment without --at prints over the v1.0.0 pin, which only --unpin lets it replace."""
        return (
            f"ERROR: Refusing to replace the deployment pinned with --at v1.0.0 to v1.0.0 ({self.release}) without "
            "--unpin.\n"
            f"To move it to this checkout's HEAD, rerun with --unpin, as in '{DEPLOY_COMMAND} --all --unpin'; to "
            "keep the pin, change nothing.\n"
            'See "Deploying an earlier commit" in docs/recovery.md.\n'
        )

    def assert_pinned_at_the_release(self) -> None:
        entry = self.manifest()["sources"][SOURCE_ID]
        self.assertEqual((self.release, "v1.0.0"), (entry["source_commit"], entry["source_ref"]))
        self.assertIn("Alpha at the release", self.skill_text("alpha"))
        self.assertFalse((self.skills_dir / "beta").exists())

    def test_a_deployment_without_at_over_a_pin_is_refused_before_it_changes_anything(self) -> None:
        # An update skill from a commit before its own PINNED stop runs exactly `deploy.py --all </dev/null` from the
        # clone, so this refusal is what keeps a pin to such a commit; the harness holds the home unchanged.
        self.deploy_ok("--all", "--at", "v1.0.0")
        for arguments in (
            ("--all",),
            ("--all", "--dry-run"),
            ("--all", "--force"),
            ("--all", "--force-item", "alpha"),
        ):
            with self.subTest(arguments=arguments):
                result = self.deploy_fails(*arguments, pattern=r"ERROR: Refusing to replace the deployment pinned")
                self.assertEqual(1, result.code)
                self.assertIn(self.refusal(), result.output)
                self.assertNotIn("Rendered and validated", result.output)
                self.assertNotIn("Traceback", result.output)
                self.assert_pinned_at_the_release()
        selected = self.deploy_fails(
            stdin=f"{self.selection_number('alpha')}\n", pattern=r"ERROR: Refusing to replace the deployment pinned"
        )
        self.assertIn(self.refusal(), selected.output)
        self.assert_pinned_at_the_release()

    def test_unpin_replaces_the_pin_and_says_so(self) -> None:
        self.deploy_ok("--all", "--at", "v1.0.0")
        replaced = f"the deployment pinned with --at v1.0.0 to v1.0.0 ({self.release})."
        self.assertIn(f"\nWould replace {replaced}\n", self.deploy_ok("--all", "--dry-run", "--unpin").output)
        self.assert_pinned_at_the_release()
        self.assertIn(f"\nReplaced {replaced}\n", self.deploy_ok("--all", "--unpin").output)
        entry = self.manifest()["sources"][SOURCE_ID]
        self.assertEqual(self.head, entry["source_commit"])
        self.assertNotIn("source_ref", entry)
        self.assertIn("Alpha on main", self.skill_text("alpha"))
        self.assertTrue((self.skills_dir / "beta" / "SKILL.md").is_file())
        # Over a deployment that is not pinned, --unpin changes nothing about the run, so the update skill passes it on.
        for arguments in (("--all",), ("--all", "--unpin")):
            with self.subTest(arguments=arguments):
                self.assertNotIn("Replaced the deployment pinned", self.deploy_ok(*arguments).output)

    def test_another_ref_over_a_users_pin_is_refused_until_unpin(self) -> None:
        # update-coding-agent-skills' release channel deploys with --at, so a copy of it that could not read a pin, or
        # one from a commit before it stopped at a pin on that channel, is held here: the refusal ends that update with
        # DEPLOY_PINNED. A pin with a tag's short name, a commit, or a tag that is not a release is the user's.
        git(self.source, "tag", "latest", self.release)
        described = f"{git(self.source, 'describe', '--tags', '--always', self.release)} ({self.release})"
        for pin in ("v1.0.0", "refs/tags/latest", self.release):
            with self.subTest(pin=pin):
                self.deploy_ok("--all", "--at", pin, "--unpin")
                for arguments in (("--all", "--at", "main"), ("--all", "--at", "main", "--dry-run")):
                    result = self.deploy_fails(*arguments, pattern=r"ERROR: Refusing to replace the deployment pinned")
                    self.assertEqual(1, result.code)
                    self.assertIn(
                        f"ERROR: Refusing to replace the deployment pinned with --at {pin} to {described} without "
                        "--unpin.\n"
                        f"To move it to main, rerun with --unpin, as in '{DEPLOY_COMMAND} --all --at main --unpin'; to "
                        "keep the pin, change nothing.\n"
                        'See "Deploying an earlier commit" in docs/recovery.md.\n',
                        result.output,
                    )
                    entry = self.manifest()["sources"][SOURCE_ID]
                    self.assertEqual((self.release, pin), (entry["source_commit"], entry["source_ref"]))
                output = self.deploy_ok("--all", "--at", "main", "--unpin").output
                self.assertIn(f"\nReplaced the deployment pinned with --at {pin} to {described}.\n", output)
                entry = self.manifest()["sources"][SOURCE_ID]
                self.assertEqual((self.head, "main"), (entry["source_commit"], entry["source_ref"]))

    def test_another_ref_over_a_release_pin_a_later_release_on_its_commit_overtook_is_refused(self) -> None:
        # A later release tagged on the pinned commit makes refs/tags/v1.0.0 no longer the last release that commit
        # holds, so the pin reads as the user's, as update-coding-agent-skills reads it.
        self.deploy_ok("--all", "--at", "refs/tags/v1.0.0")
        git(self.source, "tag", "v1.0.1", self.release)
        self.deploy_fails("--all", "--at", "main", pattern=r"ERROR: Refusing to replace the deployment pinned")
        self.assertEqual("refs/tags/v1.0.0", self.manifest()["sources"][SOURCE_ID]["source_ref"])

    def test_a_release_pin_is_the_full_name_of_the_last_release_its_commit_holds(self) -> None:
        git(self.source, "tag", "v1.1.0")
        self.assertTrue(source_ref.is_release_pin(self.source, "refs/tags/v1.0.0", self.release))
        self.assertTrue(source_ref.is_release_pin(self.source, "refs/tags/v1.1.0", self.head))
        for ref, commit in (("v1.0.0", self.release), ("refs/tags/v1.0.0", self.head), ("refs/heads/main", self.head)):
            with self.subTest(ref=ref, commit=commit):
                self.assertFalse(source_ref.is_release_pin(self.source, ref, commit))
        # A pin whose tags Git cannot read is neither owner's: the caller hears Git's reason.
        with self.tags_unreadable(), self.assertRaises(source_ref.TagsUnreadable) as raised:
            source_ref.is_release_pin(self.source, "refs/tags/v1.0.0", self.release)
        self.assertEqual("fatal: refused by the test", str(raised.exception))

    def test_a_release_pin_asks_release_tags_for_the_last_release(self) -> None:
        # The one definition of the last release a commit holds answers, through the deployer's own git runner.
        with mock.patch.object(source_ref, "last_release", return_value="v9.0.0") as asked:
            self.assertTrue(source_ref.is_release_pin(self.source, "refs/tags/v9.0.0", self.release))
            self.assertFalse(source_ref.is_release_pin(self.source, "refs/tags/v1.0.0", self.release))
        client, repository, commit = asked.call_args.args
        self.assertEqual((self.source, self.release), (repository, commit))
        self.assertEqual(platform_support.TOOL_DEADLINE_SECONDS, client.timeout)
        result = client.run(["rev-parse", "--verify", "--quiet", "nothing-by-this-name"], directory=self.source)
        self.assertEqual((1, "", ""), (result.returncode, result.stdout, result.stderr))

    def tags_unreadable(self) -> Any:
        """Git fails, with its reason, every listing of the tags reaching a commit; every other git call runs."""
        real = source_ref._run_git

        def run_git(arguments: Any, timeout: float | None = None) -> platform_support.ToolResult:
            if "for-each-ref" in arguments:
                return platform_support.ToolResult(128, "fatal: refused by the test\n")
            return real(arguments, timeout)

        return mock.patch.object(source_ref, "_run_git", run_git)

    def test_a_pin_whose_tags_git_cannot_read_is_refused_with_gits_reason(self) -> None:
        # The update's own pin, whose release Git cannot read, is refused naming Git's fault, never with the user-pin
        # advice to pass --unpin, which update-coding-agent-skills would report as DEPLOY_PINNED over a pin nobody made.
        self.deploy_ok("--all", "--at", "refs/tags/v1.0.0")
        git(self.source, "tag", "v1.1.0")
        with self.tags_unreadable():
            result = self.deploy_fails("--all", "--at", "refs/tags/v1.1.0", pattern=r"ERROR: Refusing to replace")
        self.assertEqual(1, result.code)
        self.assertIn(
            f"\nERROR: Refusing to replace the deployment pinned with --at refs/tags/v1.0.0 to v1.0.0 ({self.release})"
            ": Git could not read the release tags reaching it, so whether it is update-coding-agent-skills' own pin "
            "is unknown: fatal: refused by the test\n"
            "Put right what Git reports in the checkout, then retry; nothing was replaced.\n"
            'See "Deploying an earlier commit" in docs/recovery.md.\n',
            result.output,
        )
        self.assertNotIn("without --unpin", result.output)
        entry = self.manifest()["sources"][SOURCE_ID]
        self.assertEqual((self.release, "refs/tags/v1.0.0"), (entry["source_commit"], entry["source_ref"]))
        self.assertFalse((self.skills_dir / "beta").exists())

    def test_a_pin_at_its_commit_whose_tags_git_cannot_read_keeps_its_recorded_ref(self) -> None:
        # Recording runs after the deployment, so a tag listing Git cannot give there keeps the ref as written, where a
        # readable one would record the short name given over the update's own pin.
        self.deploy_ok("--all", "--at", "refs/tags/v1.0.0")
        with self.tags_unreadable():
            self.assertNotIn("Replaced", self.deploy_ok("--all", "--at", "v1.0.0").output)
        entry = self.manifest()["sources"][SOURCE_ID]
        self.assertEqual((self.release, "refs/tags/v1.0.0"), (entry["source_commit"], entry["source_ref"]))
        pin = source_ref.Pin("v1.0.0", self.release)
        with self.tags_unreadable():
            recorded = source_ref.recorded_ref(self.source, pin, "refs/tags/v1.0.0", self.release, unpin=False)
        self.assertEqual("refs/tags/v1.0.0", recorded)

    def test_the_uninstall_removes_a_pinned_deployment_without_unpin(self) -> None:
        # Answering none is the explicit removal, so it needs no flag; the pin goes with the items it pinned.
        self.deploy_ok("--all", "--at", "v1.0.0")
        self.deploy_ok(stdin="none\n")
        self.assertFalse((self.skills_dir / "alpha").exists())
        self.assertNotIn("source_ref", self.manifest()["sources"][SOURCE_ID])
        self.assertNotIn("pinned with --at", self.check().output)

    def test_the_update_skill_reports_the_line_the_deployer_prints_over_a_pin(self) -> None:
        # update-coding-agent-skills started with --unpin deploys main's tip with --unpin and quotes this line.
        skill = REPOSITORY_ROOT / "skills" / "update-coding-agent-skills" / "SKILL.md"
        quoted = "If the deployer printed `Replaced the deployment pinned with --at <ref> ...`"
        self.assertIn(quoted, skill.read_text(encoding="utf-8"))
        self.deploy_ok("--all", "--at", "v1.0.0")
        self.assertIn(
            "\nReplaced the deployment pinned with --at v1.0.0 to ", self.deploy_ok("--all", "--unpin").output
        )

    def test_the_update_skill_reads_the_pin_from_the_line_check_prints(self) -> None:
        # update-coding-agent-skills stops with PINNED <ref> <commit>, and reads the deployed commit its major-version
        # stop compares, from this line of check's output.
        script = REPOSITORY_ROOT / "skills" / "update-coding-agent-skills" / "scripts" / "update.sh"
        text = script.read_text(encoding="utf-8")
        self.assertIn('PIN_MARK=", pinned with --at "', text)
        self.assertIn('grep -m 1 -e "^Deployed commit: " <<<"$CHECK_OUTPUT")', text)
        self.assertIn('*"$PIN_MARK"*) PIN_REF=', text)
        self.deploy_ok("--all", "--at", "v1.0.0")
        self.assertIn(f"\nDeployed commit: v1.0.0 ({self.release}), pinned with --at v1.0.0\n", self.check().output)

    def test_the_update_skill_pins_a_release_by_its_tags_full_name_and_moves_it_without_unpin(self) -> None:
        # update-coding-agent-skills on the release channel deploys with --at refs/tags/<tag>, which the manifest and
        # check keep as given, so the update tells its own pin from one made with the tag's short name; the next
        # release's --at moves it without --unpin, and the main channel replaces it with --unpin.
        script = REPOSITORY_ROOT / "skills" / "update-coding-agent-skills" / "scripts" / "update.sh"
        text = script.read_text(encoding="utf-8")
        self.assertIn('RELEASE_REF="refs/tags/$TARGET"', text)
        self.assertIn('DEPLOY_OPTIONS+=(--at "$RELEASE_REF")', text)
        self.assertIn('[ "$PIN_REF" = "refs/tags/$PINNED_RELEASE" ]', text)
        self.deploy_ok("--all", "--at", "refs/tags/v1.0.0")
        self.assertEqual("refs/tags/v1.0.0", self.manifest()["sources"][SOURCE_ID]["source_ref"])
        self.assertIn(
            f"\nDeployed commit: v1.0.0 ({self.release}), pinned with --at refs/tags/v1.0.0\n", self.check().output
        )
        git(self.source, "tag", "v1.1.0")
        self.assertIn(
            f"\nReplaced the deployment pinned with --at refs/tags/v1.0.0 to v1.0.0 ({self.release}).\n",
            self.deploy_ok("--all", "--at", "refs/tags/v1.1.0").output,
        )
        self.assertEqual("refs/tags/v1.1.0", self.manifest()["sources"][SOURCE_ID]["source_ref"])
        self.assertIn(
            "\nReplaced the deployment pinned with --at refs/tags/v1.1.0 to ", self.deploy_ok("--all", "--unpin").output
        )
        self.assertNotIn("source_ref", self.manifest()["sources"][SOURCE_ID])

    def test_the_update_skill_reads_the_pin_from_the_deployers_refusal(self) -> None:
        # When check names no pin, update-coding-agent-skills reads the ref and the commit from this refusal, between
        # these two fixed parts, and ends with DEPLOY_PINNED <ref> <commit>.
        script = REPOSITORY_ROOT / "skills" / "update-coding-agent-skills" / "scripts" / "update.sh"
        text = script.read_text(encoding="utf-8")
        self.assertIn('PIN_REFUSAL="ERROR: Refusing to replace the deployment pinned with --at "', text)
        self.assertIn('PIN_REFUSAL_END=" without --unpin."', text)
        self.deploy_ok("--all", "--at", "v1.0.0")
        result = self.deploy_fails("--all", pattern=r"ERROR: Refusing to replace the deployment pinned")
        self.assertEqual(1, result.code)
        self.assertIn(
            f"\nERROR: Refusing to replace the deployment pinned with --at v1.0.0 to v1.0.0 ({self.release}) without "
            "--unpin.\n",
            result.output,
        )

    def test_deploying_the_pinned_commit_again_keeps_the_pin_as_written_without_a_notice(self) -> None:
        # The ref's form tells the update skill's own pin from the user's, so another name for the pinned commit,
        # update-coding-agent-skills' refs/tags/<tag> among them, leaves the recorded ref as the user wrote it.
        self.deploy_ok("--all", "--at", "v1.0.0")
        for ref in (self.release, "refs/tags/v1.0.0", "v1.0.0"):
            with self.subTest(ref=ref):
                self.assertNotIn("Replaced", self.deploy_ok("--all", "--at", ref).output)
                self.assert_pinned_at_the_release()
        # --unpin hands the pin over, so the ref it names is recorded.
        self.assertNotIn("Replaced", self.deploy_ok("--all", "--at", "refs/tags/v1.0.0", "--unpin").output)
        self.assertEqual("refs/tags/v1.0.0", self.manifest()["sources"][SOURCE_ID]["source_ref"])

    def test_a_users_pin_the_update_deployed_its_release_over_is_refused_at_the_next_release_until_unpin(self) -> None:
        # update-coding-agent-skills does not stop at a user's pin at the release's own commit and deploys it with
        # --at refs/tags/<tag>; the pin stays the user's, so the next release's --at is refused, which the update
        # reports as DEPLOY_PINNED, until the user passes --unpin.
        self.deploy_ok("--all", "--at", "v1.0.0")
        self.deploy_ok("--all", "--at", "refs/tags/v1.0.0")
        git(self.source, "tag", "v1.1.0")
        result = self.deploy_fails("--all", "--at", "refs/tags/v1.1.0", pattern=r"ERROR: Refusing to replace")
        self.assertIn(
            f"\nERROR: Refusing to replace the deployment pinned with --at v1.0.0 to v1.0.0 ({self.release}) without "
            "--unpin.\n",
            result.output,
        )
        self.assert_pinned_at_the_release()
        output = self.deploy_ok("--all", "--at", "refs/tags/v1.1.0", "--unpin").output
        self.assertIn(f"\nReplaced the deployment pinned with --at v1.0.0 to v1.0.0 ({self.release}).\n", output)
        entry = self.manifest()["sources"][SOURCE_ID]
        self.assertEqual((self.head, "refs/tags/v1.1.0"), (entry["source_commit"], entry["source_ref"]))

    def assert_a_users_pin_over_the_updates_is_refused_at_the_next_release_until_unpin(self, user_ref: str) -> None:
        # The reverse order: the update's own release pin is recorded first, then the user pins the same commit by
        # hand. The user's ref is recorded, so the pin is the user's and the next release's --at is refused.
        self.deploy_ok("--all", "--at", "refs/tags/v1.0.0")
        self.assertNotIn("Replaced", self.deploy_ok("--all", "--at", user_ref).output)
        entry = self.manifest()["sources"][SOURCE_ID]
        self.assertEqual((self.release, user_ref), (entry["source_commit"], entry["source_ref"]))
        git(self.source, "tag", "v1.1.0")
        result = self.deploy_fails("--all", "--at", "refs/tags/v1.1.0", pattern=r"ERROR: Refusing to replace")
        self.assertIn(
            f"\nERROR: Refusing to replace the deployment pinned with --at {user_ref} to v1.0.0 ({self.release}) "
            "without --unpin.\n",
            result.output,
        )
        entry = self.manifest()["sources"][SOURCE_ID]
        self.assertEqual((self.release, user_ref), (entry["source_commit"], entry["source_ref"]))
        self.assertFalse((self.skills_dir / "beta").exists())
        output = self.deploy_ok("--all", "--at", "refs/tags/v1.1.0", "--unpin").output
        self.assertIn(f"\nReplaced the deployment pinned with --at {user_ref} to v1.0.0 ({self.release}).\n", output)
        entry = self.manifest()["sources"][SOURCE_ID]
        self.assertEqual((self.head, "refs/tags/v1.1.0"), (entry["source_commit"], entry["source_ref"]))

    def test_a_users_short_name_pin_over_the_updates_own_pin_is_refused_at_the_next_release_until_unpin(self) -> None:
        self.assert_a_users_pin_over_the_updates_is_refused_at_the_next_release_until_unpin("v1.0.0")

    def test_a_users_commit_pin_over_the_updates_own_pin_is_refused_at_the_next_release_until_unpin(self) -> None:
        self.assert_a_users_pin_over_the_updates_is_refused_at_the_next_release_until_unpin(self.release)

    def assert_the_pipeline_refuses(self, recorded: str | None, ref: str) -> None:
        """A user's pin at the release, recorded by a deployment, then --at ref at another commit without --unpin: the
        run is refused before it records anything."""
        assert recorded is not None
        self.deploy_ok("--all", "--at", recorded, "--unpin")
        self.deploy_fails(
            "--all", "--at", ref, pattern=r"ERROR: Refusing to replace the deployment pinned .* --unpin\."
        )
        entry = self.manifest()["sources"][SOURCE_ID]
        self.assertEqual((self.release, recorded), (entry["source_commit"], entry["source_ref"]))

    def test_the_recorded_ref_and_its_owner_for_every_pin_transition(self) -> None:
        # Every combination of the recorded pin (none, the update's own, or a user's by short name or by commit), the
        # form of --at's ref, whether it names the recorded commit, and --unpin. A user's pin keeps its ref only at its
        # own commit without --unpin, and is refused at another commit without it (_refuse_unpinning), so the pipeline
        # never records those runs; every other run records --at's ref, whose form then names the owner.
        git(self.source, "tag", "v1.1.0")
        recorded_refs = {
            "none": None,
            "update": "refs/tags/v1.0.0",
            "user short": "v1.0.0",
            "user commit": self.release,
        }
        new_refs = {
            ("short", True): "v1.0.0",
            ("full", True): "refs/tags/v1.0.0",
            ("commit", True): self.release,
            ("short", False): "v1.1.0",
            ("full", False): "refs/tags/v1.1.0",
            ("commit", False): self.head,
        }
        # (recorded, --at's form, same commit, --unpin) -> (the ref recorded, its owner), or ("refused", None) for a run
        # the pipeline refuses before it records anything
        expected = {
            ("none", "short", True, False): ("new", "user"),
            ("none", "short", True, True): ("new", "user"),
            ("none", "short", False, False): ("new", "user"),
            ("none", "short", False, True): ("new", "user"),
            ("none", "full", True, False): ("new", "update"),
            ("none", "full", True, True): ("new", "update"),
            ("none", "full", False, False): ("new", "update"),
            ("none", "full", False, True): ("new", "update"),
            ("none", "commit", True, False): ("new", "user"),
            ("none", "commit", True, True): ("new", "user"),
            ("none", "commit", False, False): ("new", "user"),
            ("none", "commit", False, True): ("new", "user"),
            ("update", "short", True, False): ("new", "user"),
            ("update", "short", True, True): ("new", "user"),
            ("update", "short", False, False): ("new", "user"),
            ("update", "short", False, True): ("new", "user"),
            ("update", "full", True, False): ("new", "update"),
            ("update", "full", True, True): ("new", "update"),
            ("update", "full", False, False): ("new", "update"),
            ("update", "full", False, True): ("new", "update"),
            ("update", "commit", True, False): ("new", "user"),
            ("update", "commit", True, True): ("new", "user"),
            ("update", "commit", False, False): ("new", "user"),
            ("update", "commit", False, True): ("new", "user"),
            ("user short", "short", True, False): ("recorded", "user"),
            ("user short", "short", True, True): ("new", "user"),
            ("user short", "short", False, False): ("refused", None),
            ("user short", "short", False, True): ("new", "user"),
            ("user short", "full", True, False): ("recorded", "user"),
            ("user short", "full", True, True): ("new", "update"),
            ("user short", "full", False, False): ("refused", None),
            ("user short", "full", False, True): ("new", "update"),
            ("user short", "commit", True, False): ("recorded", "user"),
            ("user short", "commit", True, True): ("new", "user"),
            ("user short", "commit", False, False): ("refused", None),
            ("user short", "commit", False, True): ("new", "user"),
            ("user commit", "short", True, False): ("recorded", "user"),
            ("user commit", "short", True, True): ("new", "user"),
            ("user commit", "short", False, False): ("refused", None),
            ("user commit", "short", False, True): ("new", "user"),
            ("user commit", "full", True, False): ("recorded", "user"),
            ("user commit", "full", True, True): ("new", "update"),
            ("user commit", "full", False, False): ("refused", None),
            ("user commit", "full", False, True): ("new", "update"),
            ("user commit", "commit", True, False): ("recorded", "user"),
            ("user commit", "commit", True, True): ("new", "user"),
            ("user commit", "commit", False, False): ("refused", None),
            ("user commit", "commit", False, True): ("new", "user"),
        }
        combinations = itertools.product(recorded_refs, ("short", "full", "commit"), (True, False), (True, False))
        self.assertEqual(set(combinations), set(expected))
        self.assertTrue(source_ref.is_release_pin(self.source, recorded_refs["update"], self.release))
        for (recorded, form, same, unpin), (kept, owner) in expected.items():
            with self.subTest(recorded=recorded, form=form, same=same, unpin=unpin):
                pin = source_ref.Pin(new_refs[form, same], self.release if same else self.head)
                if kept == "refused":
                    self.assert_the_pipeline_refuses(recorded_refs[recorded], pin.ref)
                    continue
                result = source_ref.recorded_ref(self.source, pin, recorded_refs[recorded], self.release, unpin=unpin)
                self.assertEqual(recorded_refs[recorded] if kept == "recorded" else pin.ref, result)
                self.assertEqual(owner == "update", source_ref.is_release_pin(self.source, result, pin.commit))
                # A run without --at records no pin, whatever was recorded.
                self.assertIsNone(
                    source_ref.recorded_ref(self.source, None, recorded_refs[recorded], self.release, unpin=unpin)
                )

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
                self.deploy_fails("--all", f"--at={ref}", code=2, pattern=r"--at must name a branch, tag, or commit")
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

    def test_a_machine_without_git_is_refused_with_the_install_remedy_before_anything_runs(self) -> None:
        real = platform_support.find_executable

        def find_executable(name: str) -> str | None:
            return None if name == "git" else real(name)

        with (
            mock.patch.object(platform_support, "find_executable", side_effect=find_executable),
            mock.patch.object(platform_support, "run_tool") as run_tool,
        ):
            result = self.deploy_fails("--all", "--at", "v1.0.0", pattern=r"--at needs Git")
        self.assert_lines(
            result.output,
            "ERROR: --at needs Git, which was not found.",
            'Install it as "Installing the tools" in docs/installation.md says, then retry.',
        )
        run_tool.assert_not_called()
        self.assert_exports_removed(0)

    def test_an_export_git_refuses_leaves_no_directory_behind(self) -> None:
        patch, made = recorded_temporary_directories()
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

    def test_a_link_in_the_tree_is_refused_by_name_before_anything_is_extracted_or_deployed(self) -> None:
        patch, made = recorded_temporary_directories()
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
        patch, made = recorded_temporary_directories()

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
            mock.patch.object(tempfile, "mkdtemp") as mkdtemp,
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
        self.deploy_fails(
            "--at",
            "v1.0.0",
            "--migrate-from",
            "old/skills",
            code=2,
            pattern=r"--at cannot be combined with --migrate-from",
        )

    def test_unpin_cannot_be_combined_with_migrate_from(self) -> None:
        self.deploy_fails(
            "--unpin", "--migrate-from", "old/skills", code=2, pattern="--unpin cannot be combined with --migrate-from"
        )

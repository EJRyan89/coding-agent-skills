"""The deployed source commit: recorded in the manifest's source entry, validated on load, and printed by check."""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any
from unittest import mock

from harness import SOURCE_ID, DeployerTestCase, Result, git

from deployer import fsops, manifest, platform_support, source, source_commit
from deployer.context import Context, Options
from deployer.errors import DeployError

FULL_SHA1 = re.compile(r"^[0-9a-f]{40}$")


class SourceCommitTests(DeployerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()

    def make_repository(self) -> str:
        git(self.source, "init", "-q", "-b", "main")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "Fixture source")
        return git(self.source, "rev-parse", "HEAD")

    def recorded(self) -> Any:
        return self.manifest()["sources"][SOURCE_ID].get("source_commit")

    def check(self) -> Result:
        return self.run_cli("check")

    def test_the_head_commit_is_read_and_described_from_a_checkout_and_not_from_another_folder(self) -> None:
        self.assertIsNone(source_commit.head_commit(self.source))
        self.assertEqual("unknown", source_commit.describe(self.source, None))
        head = self.make_repository()
        self.assertEqual(head, source_commit.head_commit(self.source))
        self.assertEqual(f"{head[:7]} ({head})", source_commit.describe(self.source, head))
        git(self.source, "tag", "v9.9.9")
        self.assertEqual(f"v9.9.9 ({head})", source_commit.describe(self.source, head))
        # Outside a checkout the recorded name is all there is, and git is never asked.
        with mock.patch.object(platform_support, "run_tool") as run_tool:
            self.assertEqual(head, source_commit.describe(self.root, head))
        run_tool.assert_not_called()

    def test_a_git_that_cannot_start_while_describing_is_refused_with_the_reason(self) -> None:
        head = self.make_repository()
        with (
            mock.patch.object(platform_support, "run_tool", side_effect=OSError(13, "Access is denied")),
            self.assertRaises(DeployError) as raised,
        ):
            source_commit.describe(self.source, head)
        shown = platform_support.normalize(self.source)
        self.assertEqual(
            (
                f"ERROR: Cannot describe the deployed commit {head} in {shown}: Access is denied",
                "Check that Git runs, then retry.",
            ),
            raised.exception.lines,
        )

    def test_a_deployment_from_a_git_checkout_records_its_full_head_commit(self) -> None:
        head = self.make_repository()
        self.deploy_ok("--all")
        self.assertRegex(self.recorded(), FULL_SHA1)
        self.assertEqual(head, self.recorded())

    def test_the_head_commit_is_read_before_the_deployment_moves_anything(self) -> None:
        head = self.make_repository()
        events: list[str] = []
        real_head, real_move = source_commit.head_commit, fsops.move

        def read_head(source_dir: Path) -> str | None:
            events.append("read the commit")
            return real_head(source_dir)

        def move(origin: Path, destination: Path) -> None:
            events.append("move")
            real_move(origin, destination)

        with (
            mock.patch("deployer.source_commit.head_commit", side_effect=read_head),
            mock.patch("deployer.fsops.move", side_effect=move),
        ):
            self.deploy_ok("--all")
        self.assertEqual("read the commit", events[0], events)
        self.assertEqual(1, events.count("read the commit"), events)
        self.assertIn("move", events)
        self.assertEqual(head, self.recorded())

    def test_the_head_commit_is_the_sources_whatever_repository_git_dir_or_git_index_file_names(self) -> None:
        head = self.make_repository()
        other = self.root / "another repository"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        git(other, "commit", "-q", "--allow-empty", "-m", "Another history")
        variables = {"GIT_DIR": str(other / ".git"), "GIT_INDEX_FILE": str(other / ".git" / "index")}
        with mock.patch.dict("os.environ", variables):
            self.assertEqual(head, source_commit.head_commit(self.source))
            self.deploy_ok("--all")
        self.assertEqual(head, self.recorded())

    def test_a_later_deployment_records_the_new_head_commit(self) -> None:
        first = self.make_repository()
        self.deploy_ok("--all")
        self.append(self.source / "skills" / "alpha" / "SKILL.md", "\nMore.\n")
        git(self.source, "commit", "-q", "-am", "Change alpha")
        second = git(self.source, "rev-parse", "HEAD")
        self.deploy_ok("--all")
        self.assertNotEqual(first, second)
        self.assertEqual(second, self.recorded())

    def test_a_source_that_is_not_a_git_checkout_records_no_commit_and_never_runs_git(self) -> None:
        with mock.patch(
            "deployer.platform_support.find_executable", wraps=platform_support.find_executable
        ) as find_executable:
            self.deploy_ok("--all")
        self.assertNotIn(mock.call("git"), find_executable.call_args_list)
        self.assertNotIn("source_commit", self.manifest()["sources"][SOURCE_ID])

    def cannot(self, reason: str) -> str:
        """The first line of the refusal of a checkout whose commit git cannot name, for this reason."""
        return f"ERROR: Cannot read the commit checked out in {platform_support.normalize(self.source)}: {reason}"

    def refuses_keeping_the_recorded_commit(self, run_tool: Any = None, find_executable: Any = None) -> str:
        """Deploy once, then redeploy and dry-run with git patched; each refuses, the home kept byte for byte.

        The harness compares the home before and after each refusal, so the manifest still records the first commit.
        Returns the refusal's output.
        """
        head = self.make_repository()
        self.deploy_ok("--all")
        patches = []
        if run_tool is not None:
            patches.append(mock.patch("deployer.platform_support.run_tool", side_effect=run_tool))
        if find_executable is not None:
            patches.append(mock.patch("deployer.platform_support.find_executable", side_effect=find_executable))
        cannot = re.escape(f"ERROR: Cannot read the commit checked out in {platform_support.normalize(self.source)}: ")
        outputs = []
        for arguments in (("--all",), ("--all", "--dry-run")):
            with self.subTest(arguments=arguments):
                for patch in patches:
                    patch.start()
                try:
                    outputs.append(self.deploy_fails(*arguments, pattern=cannot).output)
                finally:
                    for patch in patches:
                        patch.stop()
        self.assertEqual(head, self.recorded())
        self.assertIn(f"\nDeployed commit: {head[:7]} ({head})\n", self.check().output)
        self.assertNotIn("INSTALLED", outputs[0])
        self.assertNotIn("FRESH INSTALL", outputs[1])
        return outputs[0]

    def test_a_redeployment_without_git_on_path_refuses_and_keeps_the_recorded_commit(self) -> None:
        real = platform_support.find_executable
        output = self.refuses_keeping_the_recorded_commit(
            find_executable=lambda name: None if name == "git" else real(name)
        )
        self.assert_lines(
            output,
            self.cannot("Git was not found."),
            'Install it as "Installing the tools" in docs/installation.md says, then retry.',
        )

    def test_a_failing_git_refuses_the_redeployment_with_its_reason_and_keeps_the_recorded_commit(self) -> None:
        real = platform_support.run_tool

        def run_tool(
            arguments: list[str], environment: dict[str, str] | None = None, **options: Any
        ) -> platform_support.ToolResult:
            if "rev-parse" in arguments:
                return platform_support.ToolResult(128, "fatal: detected dubious ownership\n  in the checkout\n")
            return real(arguments, environment, **options)

        output = self.refuses_keeping_the_recorded_commit(run_tool=run_tool)
        self.assert_lines(
            output,
            self.cannot("fatal: detected dubious ownership in the checkout"),
            "Put right what Git reports in the checkout, then retry.",
        )

    def test_a_git_that_cannot_start_refuses_the_redeployment_and_keeps_the_recorded_commit(self) -> None:
        real = platform_support.run_tool

        def run_tool(
            arguments: list[str], environment: dict[str, str] | None = None, **options: Any
        ) -> platform_support.ToolResult:
            if "rev-parse" in arguments:
                raise PermissionError(13, "Access is denied")
            return real(arguments, environment, **options)

        output = self.refuses_keeping_the_recorded_commit(run_tool=run_tool)
        self.assert_lines(output, self.cannot("Access is denied"), "Check that Git runs, then retry.")

    def test_a_git_that_names_no_commit_or_exits_with_nothing_said_refuses(self) -> None:
        for result, reason in (
            (platform_support.ToolResult(0, "HEAD\n"), ": HEAD\n"),
            (platform_support.ToolResult(0, "a" * 40 + "\n" + "b" * 40 + "\n"), ""),
            (platform_support.ToolResult(129, ""), ": git exited with 129\n"),
        ):
            with (
                self.subTest(output=result.output),
                mock.patch("deployer.platform_support.run_tool", return_value=result),
            ):
                (self.source / ".git").mkdir(exist_ok=True)
                with self.assertRaises(DeployError) as raised:
                    source_commit.head_commit(self.source)
                self.assertIn(reason, raised.exception.lines[0] + "\n")

    def test_a_runs_context_holds_no_commit_until_the_deployment_reads_one(self) -> None:
        data = manifest.load(self.manifest_file)
        src = source.discover(self.paths, SOURCE_ID)
        context = Context(self.paths, Options(), src, {}, data, data.ownership(SOURCE_ID), io.StringIO())
        self.assertIsNone(context.commit)

    def test_the_head_commit_is_read_past_a_warning_git_prints_beside_it(self) -> None:
        head = self.make_repository()
        warned = platform_support.ToolResult(0, f"warning: unable to access a configuration file\n{head}\n")
        with mock.patch("deployer.platform_support.run_tool", return_value=warned):
            self.assertEqual(head, source_commit.head_commit(self.source))

    def test_a_manifest_entry_without_source_commit_loads_and_deploys(self) -> None:
        self.deploy_ok("--all")
        data = self.manifest()
        self.assertNotIn("source_commit", data["sources"][SOURCE_ID])
        loaded = manifest.load(self.manifest_file)
        self.assertIsNone(loaded.source_commit(SOURCE_ID))
        self.deploy_ok("--all")

    def test_a_sha256_repository_commit_is_accepted(self) -> None:
        self.deploy_ok("--all")
        data = self.manifest()
        data["sources"][SOURCE_ID]["source_commit"] = "a" * 64
        self.write_manifest(data)
        self.assertEqual("a" * 64, manifest.load(self.manifest_file).source_commit(SOURCE_ID))

    def test_a_malformed_source_commit_is_refused_on_load(self) -> None:
        self.deploy_ok("--all")
        for value in ("--output=x", "abc1234", "A" * 40, "a" * 41, "a" * 39 + "g", "", 123, None):
            with self.subTest(value=value):
                data = self.manifest()
                data["sources"][SOURCE_ID]["source_commit"] = value
                self.write_manifest(data)
                self.deploy_fails(
                    "--all", pattern=re.escape(f"ERROR: Manifest source '{SOURCE_ID}' source_commit is malformed")
                )

    def test_check_describes_the_deployed_commit_from_the_source_checkout(self) -> None:
        head = self.make_repository()
        git(self.source, "tag", "v9.9.9")
        self.deploy_ok("--all")
        self.assertIn(f"\nDeployed commit: v9.9.9 ({head})\n", self.check().output)

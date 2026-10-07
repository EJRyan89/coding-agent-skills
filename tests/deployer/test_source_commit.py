"""The deployed source commit: recorded in the manifest's source entry, validated on load, and printed by check."""

from __future__ import annotations

import contextlib
import io
import re
import subprocess
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from harness import SOURCE_ID, DeployerTestCase, Result

from deployer import cli, manifest, platform_support

FULL_SHA1 = re.compile(r"^[0-9a-f]{40}$")


def git(directory: Path, *arguments: str) -> str:
    """Run git in a fixture repository with no user hooks, signing, or identity from the developer's configuration."""
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            "-c",
            f"core.hooksPath={directory / '.no-hooks'}",
            "-C",
            str(directory),
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


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
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            code = cli.main(["check"], self.paths)
        return Result(code, captured.getvalue())

    def test_a_deployment_from_a_git_checkout_records_its_full_head_commit(self) -> None:
        head = self.make_repository()
        self.deploy_ok("--all")
        self.assertRegex(self.recorded(), FULL_SHA1)
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

    def test_a_deployment_without_git_on_path_records_no_commit(self) -> None:
        self.make_repository()
        real = platform_support.find_executable
        with mock.patch(
            "deployer.platform_support.find_executable",
            side_effect=lambda name: None if name == "git" else real(name),
        ):
            self.deploy_ok("--all")
        self.assertNotIn("source_commit", self.manifest()["sources"][SOURCE_ID])

    def test_a_failing_git_records_no_commit(self) -> None:
        self.make_repository()
        real = platform_support.run_tool

        def run_tool(arguments: list[str], environment: dict[str, str] | None = None) -> platform_support.ToolResult:
            if "rev-parse" in arguments:
                return platform_support.ToolResult(128, "fatal: detected dubious ownership\n")
            return real(arguments, environment)

        with mock.patch("deployer.platform_support.run_tool", side_effect=run_tool):
            self.deploy_ok("--all")
        self.assertNotIn("source_commit", self.manifest()["sources"][SOURCE_ID])

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


if __name__ == "__main__":
    unittest.main()

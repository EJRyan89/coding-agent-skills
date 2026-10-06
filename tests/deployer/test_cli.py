from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from harness import REPOSITORY_ROOT, DeployerTestCase, Result, forward

from deployer import cli, platform_support


def interrupting() -> io.StringIO:
    """Standard input whose next read behaves as if the user pressed Ctrl+C."""
    # A mock rather than a subclass: typeshed's StringIO.readline cannot be overridden without breaking the override
    # rule against io's binary base class.
    return mock.Mock(io.StringIO, readline=mock.Mock(side_effect=KeyboardInterrupt))


class SingleEntryPointTests(DeployerTestCase):
    def run_cli(self, *arguments: str, stdin: str | io.StringIO = "") -> Result:
        captured = io.StringIO()
        reader = stdin if isinstance(stdin, io.StringIO) else io.StringIO(stdin)
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            code = cli.main(list(arguments), self.paths, reader)
        return Result(code, captured.getvalue())

    def test_configure_command_writes_the_config(self) -> None:
        self.make_source_json()
        self.repos.mkdir()
        result = self.run_cli("configure", stdin=f"{forward(self.repos)}\n")
        self.assertEqual(0, result.code, result.output)
        self.assertTrue(result.output.startswith("\nSource: Test Skills (test/skills)\nConfig: "), result.output)
        self.assertTrue(
            result.output.endswith("Next, preview the deployment:\n  python deploy.py --all --dry-run\n\n"),
            result.output,
        )
        self.assertEqual(
            f"_source_id=test/skills\nREPOS_ROOT={forward(self.repos)}\n".encode(),
            self.config_file().read_bytes(),
        )

    def test_without_a_command_it_deploys(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        result = self.run_cli("--all")
        self.assertEqual(0, result.code, result.output)
        self.assertIn("=== DEPLOYED ===", result.output)
        self.assertTrue((self.skills_dir / "alpha" / "SKILL.md").is_file())

    def test_help_describes_both_commands_and_exits_successfully(self) -> None:
        self.make_source_json()
        usage = (
            "usage: python deploy.py [--all [--include NAME]] [--dry-run]\n"
            "                        [--force] [--force-item NAME]\n"
            "       python deploy.py --migrate-from ID\n"
            "       python deploy.py --canary-home DIR [--all [--include NAME]]\n"
            "                        [--force] [--force-item NAME]\n"
            "       python deploy.py configure [--reset]\n"
            "       python deploy.py check\n"
            "       python deploy.py verify\n"
        )
        expected = {
            ("--help",): (
                f"\n{usage}\n"
                "Render, validate, and deploy this repository's skills.\n"
                "\n"
                "options:\n"
                "  -h, --help         show this help message and exit\n"
                "  --all              deploy everything except uninstalled opt-in items\n"
                "  --include NAME     with --all, also deploy this opt-in item; repeatable\n"
                "  --dry-run          show what would change; change nothing\n"
                "  --force            replace modified or unmanaged items (backed up)\n"
                "  --force-item NAME  replace one item (backed up); repeatable\n"
                "  --migrate-from ID  take over items from another source ID\n"
                "  --canary-home DIR  deploy into a throwaway home under the temp directory\n"
                "\n"
                "commands:\n"
                "  configure          set the values skills need; see its --help\n"
                "  check              list the tools needed and which are missing\n"
                "  verify             check that Codex and Copilot CLI find the adapters\n"
                "\n"
            ),
            ("configure", "--help"): (
                f"\n{usage}\n"
                "Set or change the configuration values skills need.\n"
                "At each prompt, Enter keeps the current value and Ctrl+C cancels.\n"
                "\n"
                "options:\n"
                "  -h, --help  show this help message and exit\n"
                "  --reset     start from an empty configuration\n"
                "\n"
            ),
            ("verify", "--help"): (
                f"\n{usage}\n"
                "Check that Codex CLI and Copilot CLI, whichever are installed, find every\n"
                "deployed runtime adapter under ~/.agents/skills, enabled and not shadowed\n"
                "by another skill of the same name. No model is started; nothing is changed.\n"
                "\n"
                "options:\n"
                "  -h, --help  show this help message and exit\n"
                "\n"
            ),
        }
        for arguments, text in expected.items():
            with self.subTest(arguments=arguments):
                with mock.patch.dict(os.environ, {"COLUMNS": "40"}):
                    result = self.run_cli(*arguments)
                self.assertEqual(0, result.code, result.output)
                self.assertEqual(text, result.output)
                self.assertLessEqual(max(len(line) for line in result.output.splitlines()), 80)
        self.assertFalse(self.config_file().exists())

    def test_unknown_arguments_point_to_the_matching_help(self) -> None:
        self.make_source_json()
        self.make_config()
        for arguments, hint in (
            (("--bogus",), "Run 'python deploy.py --help' for usage."),
            (("configure", "--bogus"), "Run 'python deploy.py configure --help' for usage."),
            (("verify", "--bogus"), "Run 'python deploy.py verify --help' for usage."),
            (("--dry",), "Run 'python deploy.py --help' for usage."),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(1, result.code)
                self.assertIn(f"ERROR: unrecognized arguments: {arguments[-1]}\n{hint}", result.output)

    def test_missing_configuration_points_to_the_configure_command(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        result = self.run_cli("--all", "--dry-run")
        self.assertEqual(1, result.code)
        self.assertIn("Run 'python deploy.py configure' first.", result.output)

    def test_missing_required_variable_points_to_the_configure_command(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Root {{REPOS_ROOT}}", ["REPOS_ROOT"])
        self.config_file().write_bytes(b"_source_id=test/skills\n")
        result = self.run_cli("--all", "--dry-run")
        self.assertEqual(1, result.code)
        self.assertIn("Run 'python deploy.py configure' to set them.", result.output)

    def test_ctrl_c_at_the_configure_prompt_keeps_the_existing_config(self) -> None:
        self.make_source_json()
        self.make_config()
        before = self.config_file().read_bytes()
        result = self.run_cli("configure", stdin=interrupting())
        self.assertEqual(1, result.code, result.output)
        self.assertIn("Ctrl+C cancels", result.output)
        self.assertIn("Configuration cancelled; existing config was not changed.", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertEqual(before, self.config_file().read_bytes())

    def test_ctrl_c_at_the_selection_prompt_cancels_without_changes(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        for arguments in ((), ("--dry-run",)):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments, stdin=interrupting())
                self.assertEqual(130, result.code, result.output)
                self.assertIn("Cancelled; nothing was changed.", result.output)
                self.assertNotIn("Deployment failed", result.output)
                self.assertNotIn("Traceback", result.output)
                self.assertFalse((self.skills_dir / "alpha").exists())
                self.assertFalse(self.manifest_file.exists())
                self.assertFalse((self.home / ".claude" / "deployer" / ".deploy.lock.d").exists())
        self.deploy_ok("--all")

    def test_every_ending_is_framed_by_blank_lines(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        cases = (
            (("--help",), ""),
            (("configure", "--help"), ""),
            (("--dry",), ""),
            (("configure", "--bogus"), ""),
            (("--all", "--dry-run"), ""),
            (("configure",), interrupting()),
            ((), interrupting()),
            ((), "\n"),
        )
        for arguments, stdin in cases:
            with self.subTest(arguments=arguments, interrupted=not isinstance(stdin, str)):
                if arguments == ("--all", "--dry-run"):
                    self.config_file().unlink(missing_ok=True)
                else:
                    self.make_config()
                result = self.run_cli(*arguments, stdin=stdin)
                self.assertTrue(result.output.startswith("\n"), repr(result.output))
                self.assertTrue(result.output.endswith("\n\n"), repr(result.output))
                self.assertFalse(result.output.endswith("\n\n\n"), repr(result.output))

    def test_repository_entry_point_runs_the_configure_command(self) -> None:
        self.make_source_json()
        result = platform_support.run_tool(
            [sys.executable, "-B", str(REPOSITORY_ROOT / "deploy.py"), "configure", "--help"],
            {"HOME": str(self.home), "USERPROFILE": str(self.home)},
        )
        self.assertEqual(0, result.returncode, result.output)
        self.assertIn("--reset     start from an empty configuration", result.output)
        self.assertFalse((REPOSITORY_ROOT / "configure.py").exists())


def unwrapped(output: str) -> str:
    return " ".join(output.split())


def shell_path(path: Path) -> str:
    """The Git Bash form of a drive-letter path, such as /c/Tools for C:/Tools."""
    text = forward(path)
    return f"/{text[0].lower()}{text[2:]}"


class HomeTests(DeployerTestCase):
    """Every run names its home, and warns when HOME names another directory than the profile folder."""

    def setUp(self) -> None:
        super().setUp()
        self.other = self.root / "other home"
        self.warning = (
            f"WARNING: HOME is set to {forward(self.other)}, but Claude Code, Codex, and Copilot CLI read skills "
            f"from the Windows profile folder, so this run uses {forward(self.home)}. To deploy into a throwaway "
            "home instead, use --canary-home."
        )
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()

    def environment(self, home: str) -> Any:
        return mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": str(self.home)})

    def check(self) -> Result:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            code = cli.main(["check"], self.paths)
        return Result(code, captured.getvalue())

    def test_deploying_and_checking_name_the_home(self) -> None:
        for result in (self.deploy_ok("--all", "--dry-run"), self.deploy_ok("--all"), self.check()):
            with self.subTest(output=result.output[:40]):
                self.assertIn(f"\nHome: {forward(self.home)}\n", result.output)
                self.assertNotIn("WARNING", result.output)

    def test_home_naming_another_directory_is_warned_about_before_anything_changes(self) -> None:
        with self.environment(str(self.other)):
            for result in (self.deploy_ok("--all", "--dry-run"), self.check(), self.deploy_ok("--all")):
                with self.subTest(output=result.output[:40]):
                    self.assertIn(f"\nHome: {forward(self.home)}\n", result.output)
                    self.assertIn(self.warning, unwrapped(result.output))
            # The warning comes before the run reports anything it changed.
            output = self.deploy_ok("--all").output
            self.assertLess(output.index("WARNING: HOME"), output.index("=== DEPLOYED ==="))
        self.assertTrue((self.skills_dir / "alpha" / "SKILL.md").is_file())
        self.assertFalse(self.other.exists())

    def test_home_equal_to_the_profile_folder_is_not_warned_about(self) -> None:
        for home in (str(self.home), forward(self.home).upper(), shell_path(self.home)):
            with self.subTest(home=home), self.environment(home):
                self.assertNotIn("WARNING", self.deploy_ok("--all", "--dry-run").output)

    def test_a_canary_run_names_its_home_and_ignores_home(self) -> None:
        canary = self.root / "canary"
        canary.mkdir()
        with self.environment(str(self.other)):
            output = self.deploy_ok("--canary-home", str(canary), "--all").output
        self.assertIn(f"\nHome: {forward(canary)}\n", output)
        self.assertNotIn("WARNING", output)

    def test_the_entry_point_deploys_into_the_profile_folder_whatever_home_says(self) -> None:
        source_id = json.loads((REPOSITORY_ROOT / "source.json").read_text(encoding="utf-8"))["id"]
        self.make_config(source_id)
        result = platform_support.run_tool(
            [sys.executable, "-B", str(REPOSITORY_ROOT / "deploy.py"), "--all", "--dry-run"],
            {"HOME": shell_path(self.other), "USERPROFILE": str(self.home)},
        )
        self.assertEqual(0, result.returncode, result.output)
        self.assertIn(f"\nHome: {forward(self.home)}\n", result.output.replace("\r\n", "\n"))
        self.assertIn(self.warning, unwrapped(result.output))
        self.assertFalse(self.other.exists())


if __name__ == "__main__":
    unittest.main()

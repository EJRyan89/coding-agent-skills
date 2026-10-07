"""deploy.py's one argparse parser: subcommands, generated usage and help, and how a usage error ends (exit 2)."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from harness import DeployerTestCase, Result

from deployer import cli

# Every option and command the hand-written usage named before the subparsers replaced it, per help page.
OLD_USAGE_NAMES = {
    (): (
        "--all",
        "--include NAME",
        "--dry-run",
        "--force",
        "--force-item NAME",
        "--migrate-from ID",
        "--take-over-source",
        "--canary-home DIR",
        "--debug",
        "configure",
        "check",
        "verify",
    ),
    ("configure",): ("--reset", "--debug"),
    ("check",): ("--debug",),
    ("verify",): ("--debug",),
}


class RoutingTests(DeployerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()

    def run_cli(self, *arguments: str) -> Result:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            code = cli.main(list(arguments), self.paths, io.StringIO(""))
        return Result(code, captured.getvalue())

    def test_each_help_names_every_option_the_old_usage_named(self) -> None:
        for command, names in OLD_USAGE_NAMES.items():
            with self.subTest(command=command):
                result = self.run_cli(*command, "--help")
                self.assertEqual(0, result.code, result.output)
                prog = " ".join(("python deploy.py", *command))
                self.assertIn(f"usage: {prog} ", result.output)
                for name in names:
                    self.assertIn(name, result.output)

    def test_an_unknown_command_is_a_usage_error(self) -> None:
        result = self.run_cli("deploy")
        self.assertEqual(2, result.code, result.output)
        self.assertIn("ERROR: argument COMMAND: invalid choice: 'deploy'", result.output)
        self.assertIn("Run 'python deploy.py --help' for usage.", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_deploy_options_cannot_go_with_a_command(self) -> None:
        for arguments in (("--all", "configure"), ("--dry-run", "check"), ("--force", "verify")):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(2, result.code, result.output)
                self.assertIn(
                    f"ERROR: {arguments[0]} cannot be combined with the {arguments[1]} command\n"
                    "Run 'python deploy.py --help' for usage.",
                    result.output,
                )

    def test_debug_goes_before_or_after_the_command(self) -> None:
        for arguments in (("--debug", "check"), ("check", "--debug")):
            with (
                self.subTest(arguments=arguments),
                mock.patch("deployer.tools.probe", side_effect=PermissionError(13, "Permission denied", "C:/x")),
            ):
                result = self.run_cli(*arguments)
                self.assertEqual(1, result.code, result.output)
                self.assertIn("Traceback (most recent call last):", result.output)


if __name__ == "__main__":
    unittest.main()

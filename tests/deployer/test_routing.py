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


class DeployUsageErrorTests(DeployerTestCase):
    """A deploy command line that cannot run is refused before anything changes, as every usage error is: exit 2."""

    HELP = "Run 'python deploy.py --help' for usage."

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

    def assert_usage_error(self, arguments: tuple[str, ...], *lines: str) -> None:
        result = self.run_cli(*arguments)
        self.assertEqual(2, result.code, result.output)
        self.assertIn("\n".join(("", *lines, self.HELP, "")), result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertFalse(self.manifest_file.exists())
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_options_that_cannot_run_together_are_usage_errors(self) -> None:
        canary = str(self.root)
        for arguments, message in (
            (("--include", "alpha"), "ERROR: --include can only be used with --all"),
            (("--take-over-source", "--dry-run"), "ERROR: --take-over-source cannot be combined with --dry-run"),
            (("--canary-home", canary, "--dry-run"), "ERROR: --canary-home cannot be combined with --dry-run"),
            (
                ("--canary-home", canary, "--migrate-from", "old/source"),
                "ERROR: --canary-home cannot be combined with --migrate-from",
            ),
            (
                ("--canary-home", canary, "--take-over-source"),
                "ERROR: --canary-home cannot be combined with --take-over-source",
            ),
        ):
            with self.subTest(arguments=arguments):
                self.assert_usage_error(arguments, message)

    def test_migration_refuses_every_option_it_would_ignore(self) -> None:
        for extra, message in (
            (("--dry-run",), "ERROR: --migrate-from cannot be combined with --dry-run"),
            (("--all",), "ERROR: --migrate-from cannot be combined with --all"),
            (("--all", "--include", "alpha"), "ERROR: --migrate-from cannot be combined with --all"),
            (("--force",), "ERROR: --migrate-from cannot be combined with --force"),
            (("--force-item", "alpha"), "ERROR: --migrate-from cannot be combined with --force-item"),
            (("--force-item", "unknown"), "ERROR: --migrate-from cannot be combined with --force-item"),
        ):
            with self.subTest(extra=extra):
                self.assert_usage_error(("--migrate-from", "old/source", *extra), message)

    def test_migration_still_takes_over_the_source(self) -> None:
        result = self.run_cli("--migrate-from", "old/source", "--take-over-source")
        self.assertEqual(1, result.code, result.output)
        self.assertIn("ERROR: Cannot migrate without an existing manifest.", result.output)
        self.assertNotIn(self.HELP, result.output)

    def test_a_migration_source_id_this_source_cannot_take_over_from_is_a_usage_error(self) -> None:
        self.assert_usage_error(("--migrate-from", "Not Valid"), "ERROR: Invalid migration source ID: Not Valid")
        self.assert_usage_error(("--migrate-from", "test/skills"), "ERROR: --migrate-from must name a different source")

    def test_a_force_item_the_run_does_not_install_is_a_usage_error(self) -> None:
        for arguments in (("--dry-run",), ()):
            with self.subTest(arguments=arguments):
                self.assert_usage_error(
                    ("--all", *arguments, "--force-item", "gamma"),
                    "ERROR: --force-item names 'gamma', which this run does not install.",
                    "Name one of: alpha.",
                )


if __name__ == "__main__":
    unittest.main()

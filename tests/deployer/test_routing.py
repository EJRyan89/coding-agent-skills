"""deploy.py's one argparse parser: subcommands, generated usage and help, and how a usage error ends (exit 2)."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from harness import DeployerTestCase, Result, tree_state

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
        for arguments in (("--all", "--dry-run"), ("--all",), ("--dry-run",), ()):
            with self.subTest(arguments=arguments):
                before = tree_state(self.home)
                result = self.run_cli(*arguments, "--force-item", "gamma")
                self.assertEqual(2, result.code, result.output)
                self.assertEqual(
                    "\nERROR: --force-item names 'gamma', which this run does not install.\n"
                    f"Name one of: alpha.\n{self.HELP}\n\n",
                    result.output,
                )
                self.assertEqual(before, tree_state(self.home), result.output)

    def test_unknown_forced_items_do_not_claim_a_canary_home(self) -> None:
        canary = self.root / "canary"
        canary.mkdir()
        result = self.deploy_fails(
            "--all", "--canary-home", str(canary), "--force-item", "gamma", home=canary, pattern="--force-item names"
        )
        self.assertEqual(2, result.code, result.output)
        self.assertEqual(
            "\nERROR: --force-item names 'gamma', which this run does not install.\n"
            f"Name one of: alpha.\n{self.HELP}\n\n",
            result.output,
        )
        self.assertEqual([], list(canary.iterdir()), result.output)

    def test_unknown_includes_are_usage_errors_before_any_change(self) -> None:
        for arguments in (("--all",), ("--all", "--dry-run")):
            with self.subTest(arguments=arguments):
                result = self.deploy_fails(
                    *arguments,
                    "--include",
                    "alpha",
                    "--include",
                    "missing",
                    "--include",
                    "other",
                    pattern="--include names no bundle or skill",
                )
                self.assertEqual(2, result.code, result.output)
                self.assertEqual(
                    f"\nERROR: --include names no bundle or skill in this source: missing, other\n{self.HELP}\n\n",
                    result.output,
                )

    def test_unknown_includes_do_not_claim_a_canary_home(self) -> None:
        canary = self.root / "canary"
        canary.mkdir()
        before = tree_state(self.home)
        result = self.deploy_fails(
            "--all",
            "--canary-home",
            str(canary),
            "--include",
            "missing",
            home=canary,
            pattern="--include names no bundle or skill",
        )
        self.assertEqual(2, result.code, result.output)
        self.assertEqual(
            f"\nERROR: --include names no bundle or skill in this source: missing\n{self.HELP}\n\n",
            result.output,
        )
        self.assertEqual([], list(canary.iterdir()), result.output)
        self.assertEqual(before, tree_state(self.home), result.output)

    def test_known_forced_items_still_require_the_selected_run_to_install_them(self) -> None:
        self.make_source_json(
            shared_assets={"shared.md": "owner"}, bundles={"optional": {"members": ["beta"], "opt_in": True}}
        )
        self.make_agent("reviewer")
        self.make_shared_asset("shared.md")
        self.make_skill("support", selectable=False)
        self.make_skill("beta", skill_deps=["support"], shared_deps=["shared.md"], agent_deps=["reviewer"])
        for name in ("beta", "support", "shared.md", "reviewer", "reviewer.md"):
            for arguments in (("--dry-run",), ()):
                with self.subTest(name=name, arguments=arguments):
                    result = self.deploy_fails("--all", *arguments, "--force-item", name, pattern="--force-item names")
                    self.assertEqual(2, result.code, result.output)
                    self.assertIn(
                        f"\nERROR: --force-item names '{name}', which this run does not install.\n", result.output
                    )
                    self.assertIn(f"Name one of: alpha.\n{self.HELP}\n", result.output)
                    self.assertNotIn("Deployment failed", result.output)
                    self.assertNotIn("reconciling", result.output)
            selected = self.deploy_ok("--all", "--include", "optional", "--dry-run", "--force-item", name)
            self.assertIn("Rendered and validated in memory (dry run).", selected.output)

    def test_source_preflight_does_not_accept_items_no_selection_installs(self) -> None:
        self.make_skill("orphan", selectable=False)
        self.make_agent("unused")
        self.make_source_json(shared_assets={"unused.md": "owner", "external.md": "dependency"})
        self.make_shared_asset("unused.md")
        for name in ("orphan", "unused", "unused.md", "external.md"):
            with self.subTest(name=name):
                result = self.deploy_fails("--force-item", name, pattern="--force-item names")
                self.assertEqual(2, result.code, result.output)
                self.assertEqual(
                    f"\nERROR: --force-item names '{name}', which this run does not install.\n"
                    f"Name one of: alpha.\n{self.HELP}\n\n",
                    result.output,
                )


if __name__ == "__main__":
    unittest.main()

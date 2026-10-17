from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from functools import partial
from typing import Any
from unittest import mock

from harness import DEPLOY_COMMAND, REPOSITORY_ROOT, DeployerTestCase, Result, forward, interrupting

from deployer import check, configure, platform_support, verify
from deployer.arguments import USAGE_ERROR, debug_given, parse_command


class SingleEntryPointTests(DeployerTestCase):
    def test_each_command_module_parses_its_own_command_line_as_cli_routes_it(self) -> None:
        namespace = parse_command(["check"])
        self.assertEqual("check", getattr(namespace, "command", None))
        for module, command in ((check, "check"), (configure, "configure"), (verify, "verify")):
            with self.subTest(command=command):
                result = self.run_command(partial(module.run, ["--bogus"], self.paths))
                self.assertEqual(USAGE_ERROR, result.code)
                self.assertIn(
                    f"ERROR: unrecognized arguments: --bogus\nRun '{DEPLOY_COMMAND} {command} --help' for usage.",
                    result.output,
                )
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            self.assertEqual(USAGE_ERROR, parse_command(["--all", "check"]))
        self.assertIn("ERROR: --all cannot be combined with the check command", captured.getvalue())

    def test_configure_command_writes_the_config(self) -> None:
        self.make_source_json()
        self.repos.mkdir()
        result = self.run_cli("configure", stdin=f"{forward(self.repos)}\n")
        self.assertEqual(0, result.code, result.output)
        self.assertTrue(result.output.startswith("\nSource: Test Skills (test/skills)\nConfig: "), result.output)
        self.assertTrue(
            result.output.endswith(f"Next, preview the deployment:\n  {DEPLOY_COMMAND} --all --dry-run\n\n"),
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
        # Usage continues under the first option, past the command line the platform runs deploy.py by.
        pad = " " * len(f"usage: {DEPLOY_COMMAND} ")
        usage = (
            f"usage: {DEPLOY_COMMAND} [-h] [--all] [--include NAME] [--uninstall] [--dry-run]\n"
            f"{pad}[--force] [--force-item NAME] [--migrate-from ID]\n"
            f"{pad}[--take-over-source] [--canary-home DIR] [--at REF]\n"
            f"{pad}[--unpin] [--debug]\n"
            f"{pad}COMMAND ...\n"
        )
        options = (
            "\n"
            "Render, validate, and deploy this repository's skills.\n"
            "\n"
            "options:\n"
            "  -h, --help          show this help message and exit\n"
            "  --all               deploy everything except opt-in items not already chosen\n"
            "  --include NAME      with --all, also deploy this opt-in item; repeatable\n"
            "  --uninstall         select nothing, removing what this source deployed\n"
            "  --dry-run           show what would change; change nothing\n"
            "  --force             replace modified or unmanaged items (backed up)\n"
            "  --force-item NAME   replace one item (backed up); repeatable\n"
            "  --migrate-from ID   take over items from another source ID\n"
            "  --take-over-source  deploy from this checkout in place of the recorded one\n"
            "  --canary-home DIR   deploy into a throwaway home under the temp directory\n"
            "  --at REF            deploy this commit, tag, or branch instead of the checkout\n"
            "  --unpin             let a deployment without --at replace a pinned one\n"
            "  --debug             print the traceback when it fails; or set DEPLOYER_DEBUG=1\n"
            "\n"
            "commands:\n"
            "  COMMAND             run one of these instead of deploying\n"
            "    configure         set the values skills need; see its --help\n"
            "    check             list the tools needed and which are missing\n"
            "    verify            check that Codex and Copilot CLI find the adapters\n"
            "\n"
        )
        expected = {
            ("--help",): "\n" + usage + options,
            ("configure", "--help"): (
                "\n"
                f"usage: {DEPLOY_COMMAND} configure [-h] [--reset] [--debug]\n"
                "\n"
                "Set or change the configuration values skills need.\n"
                "At each prompt, Enter keeps the current value and Ctrl+C cancels.\n"
                "\n"
                "options:\n"
                "  -h, --help  show this help message and exit\n"
                "  --reset     start from an empty configuration\n"
                "  --debug     print the traceback when it fails; or set DEPLOYER_DEBUG=1\n"
                "\n"
            ),
            ("verify", "--help"): (
                "\n"
                f"usage: {DEPLOY_COMMAND} verify [-h] [--debug]\n"
                "\n"
                "Check that Codex CLI and Copilot CLI, whichever are installed, find every\n"
                "deployed runtime adapter under ~/.agents/skills, enabled and not shadowed\n"
                "by another skill of the same name. No model is started; nothing is changed.\n"
                "\n"
                "options:\n"
                "  -h, --help  show this help message and exit\n"
                "  --debug     print the traceback when it fails; or set DEPLOYER_DEBUG=1\n"
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
            (("--bogus",), f"Run '{DEPLOY_COMMAND} --help' for usage."),
            (("configure", "--bogus"), f"Run '{DEPLOY_COMMAND} configure --help' for usage."),
            (("verify", "--bogus"), f"Run '{DEPLOY_COMMAND} verify --help' for usage."),
            (("--dry",), f"Run '{DEPLOY_COMMAND} --help' for usage."),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(2, result.code)
                self.assertIn(f"ERROR: unrecognized arguments: {arguments[-1]}\n{hint}", result.output)

    def test_missing_configuration_points_to_the_configure_command(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        result = self.run_cli("--all", "--dry-run")
        self.assertEqual(1, result.code)
        self.assertIn(f"Run '{DEPLOY_COMMAND} configure' first.", result.output)

    def test_missing_required_variable_points_to_the_configure_command(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Root {{REPOS_ROOT}}", ["REPOS_ROOT"])
        self.write(self.config_file(), "_source_id=test/skills\n")
        result = self.run_cli("--all", "--dry-run")
        self.assertEqual(1, result.code)
        self.assertIn(f"Run '{DEPLOY_COMMAND} configure' to set them.", result.output)

    def test_ctrl_c_at_the_configure_prompt_keeps_the_existing_config(self) -> None:
        self.make_source_json()
        self.make_config()
        before = self.config_file().read_bytes()
        result = self.run_cli("configure", stdin=interrupting())
        self.assertEqual(130, result.code, result.output)
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
        result = self.run_entry_point(
            "configure", "--help", environment={"HOME": str(self.home), "USERPROFILE": str(self.home)}
        )
        self.assertEqual(0, result.code, result.output)
        self.assertIn("--reset     start from an empty configuration", result.output)
        self.assertFalse((REPOSITORY_ROOT / "configure.py").exists())


def unwrapped(output: str) -> str:
    return " ".join(output.split())


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
        return self.run_cli("check")

    def test_deploying_and_checking_name_the_home(self) -> None:
        for result in (self.deploy_ok("--all", "--dry-run"), self.deploy_ok("--all"), self.check()):
            with self.subTest(output=result.output[:40]):
                self.assertIn(f"\nHome: {forward(self.home)}\n", result.output)
                self.assertNotIn("WARNING", result.output)

    def test_a_canary_run_names_its_home_and_ignores_home(self) -> None:
        canary = self.root / "canary"
        canary.mkdir()
        with self.environment(str(self.other)):
            output = self.deploy_ok("--canary-home", str(canary), "--all").output
        self.assertIn(f"\nHome: {forward(canary)}\n", output)
        self.assertNotIn("WARNING", output)

    if platform_support.WINDOWS:

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
            for home in (str(self.home), forward(self.home).upper(), platform_support.to_shell_path(str(self.home))):
                with self.subTest(home=home), self.environment(home):
                    self.assertNotIn("WARNING", self.deploy_ok("--all", "--dry-run").output)

        def test_the_entry_point_deploys_into_the_profile_folder_whatever_home_says(self) -> None:
            source_id = json.loads((REPOSITORY_ROOT / "source.json").read_text(encoding="utf-8"))["id"]
            self.make_config(source_id)
            result = self.run_entry_point(
                "--all",
                "--dry-run",
                environment={"HOME": platform_support.to_shell_path(str(self.other)), "USERPROFILE": str(self.home)},
            )
            self.assertEqual(0, result.code, result.output)
            self.assertIn(f"\nHome: {forward(self.home)}\n", result.output.replace("\r\n", "\n"))
            self.assertIn(self.warning, unwrapped(result.output))
            self.assertFalse(self.other.exists())

    else:

        def test_home_is_the_home_so_no_other_value_is_warned_about(self) -> None:
            for home in (str(self.other), str(self.home)):
                with self.subTest(home=home), self.environment(home):
                    self.assertNotIn("WARNING", self.deploy_ok("--all", "--dry-run").output)

        def test_the_entry_point_deploys_into_home_whatever_a_profile_variable_says(self) -> None:
            source_id = json.loads((REPOSITORY_ROOT / "source.json").read_text(encoding="utf-8"))["id"]
            self.make_config(source_id)
            result = self.run_entry_point(
                "--all", "--dry-run", environment={"HOME": str(self.home), "USERPROFILE": str(self.other)}
            )
            self.assertEqual(0, result.code, result.output)
            self.assertIn(f"\nHome: {forward(self.home)}\n", result.output)
            self.assertNotIn("WARNING", result.output)
            self.assertFalse(self.other.exists())


# Run deploy.py as written in a child Python whose deploy and check commands raise past everything they handle, so the
# failure reaches the entry point's runner: argv[1] is the checkout copy, argv[2] the failure, and the rest deploy.py's.
ESCAPING_FAILURE = r"""
import runpy, sys
root = sys.argv.pop(1)
sys.path.insert(0, root)
from deployer import check, pipeline
failure = KeyboardInterrupt() if sys.argv.pop(1) == "interrupt" else ValueError("injected failure")
def failing(*arguments, **keywords):
    raise failure
pipeline.execute = check.execute = failing
sys.argv[0] = root + "/deploy.py"
runpy.run_path(sys.argv[0], run_name="__main__")
"""
TRACEBACK_HEADER = "Traceback (most recent call last):\n"


class EscapingFailureTests(DeployerTestCase):
    """--debug and DEPLOYER_DEBUG show the traceback of a failure no command handled, after its FAILED line."""

    def run_failing(self, failure: str, *arguments: str, debug_variable: str | None = None) -> tuple[int, str, str]:
        environment = {key: value for key, value in os.environ.items() if key != "DEPLOYER_DEBUG"}
        environment.update({"HOME": str(self.home), "USERPROFILE": str(self.home)})
        if debug_variable is not None:
            environment["DEPLOYER_DEBUG"] = debug_variable
        result = self.run_in_checkout(ESCAPING_FAILURE, failure, *arguments, environment=environment)
        return result.code, result.stdout, result.stderr

    def test_debug_shows_the_traceback_after_the_failed_line_and_keeps_the_exit_code(self) -> None:
        for arguments, variable in (
            (("--debug",), None),
            (("--dry-run", "--debug"), None),
            (("check", "--debug"), None),
            (("--debug", "check"), None),
            (("--dry-run",), "1"),
            (("check",), "yes"),
        ):
            with self.subTest(arguments=arguments, variable=variable):
                code, output, errors = self.run_failing("error", *arguments, debug_variable=variable)
                self.assertEqual((1, "FAILED ValueError: injected failure\n"), (code, output), errors)
                self.assertTrue(errors.startswith(TRACEBACK_HEADER), errors)
                self.assertIn("in failing", errors)
                self.assertTrue(errors.endswith("ValueError: injected failure\n"), errors)

    def test_ctrl_c_shows_its_traceback_under_debug_and_keeps_exit_130(self) -> None:
        code, output, errors = self.run_failing("interrupt", "--debug")
        self.assertEqual((130, "FAILED interrupted\n"), (code, output), errors)
        self.assertTrue(errors.startswith(TRACEBACK_HEADER), errors)
        self.assertTrue(errors.endswith("KeyboardInterrupt\n"), errors)

    def test_without_debug_the_failed_line_is_the_whole_report(self) -> None:
        for failure, arguments, variable, ending in (
            ("error", (), None, (1, "FAILED ValueError: injected failure\n")),
            ("error", ("check",), None, (1, "FAILED ValueError: injected failure\n")),
            ("error", ("--dry-run",), "0", (1, "FAILED ValueError: injected failure\n")),
            ("error", ("--dry-run",), "", (1, "FAILED ValueError: injected failure\n")),
            ("interrupt", (), None, (130, "FAILED interrupted\n")),
        ):
            with self.subTest(failure=failure, arguments=arguments, variable=variable):
                code, output, errors = self.run_failing(failure, *arguments, debug_variable=variable)
                self.assertEqual((ending, ""), ((code, output), errors))

    def test_reading_debug_again_prints_nothing_and_a_refused_command_line_gives_none(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertTrue(debug_given(["--debug", "--dry-run"]))
            self.assertTrue(debug_given(["verify", "--debug"]))
            self.assertFalse(debug_given(["--dry-run"]))
            self.assertFalse(debug_given(["--debug", "--bogus"]))
            self.assertFalse(debug_given(["--debug", "--at"]))
        self.assertEqual(("", ""), (output.getvalue(), errors.getvalue()))


if __name__ == "__main__":
    unittest.main()

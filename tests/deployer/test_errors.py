"""How deploy.py ends on an OSError: the error's lines without a traceback, and the traceback after them on --debug."""

from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from harness import DeployerTestCase

from deployer import errors

PATH = "C:/blocked/item"
RETRY = "Check that the path exists and that you can read it, then retry."
DEBUG_HINT = "Rerun with --debug to see the traceback."
TRACEBACK = "Traceback (most recent call last):"


def denied(*_arguments: Any, **_keywords: Any) -> Any:
    raise PermissionError(13, "Permission denied", PATH)


class OSErrorTests(DeployerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()

    def canary_listing_denied(self) -> tuple[str, Callable[..., Any]]:
        home = Path(tempfile.mkdtemp(prefix="deploy-test-canary.")).resolve()
        self.addCleanup(home.rmdir)
        real = Path.iterdir

        def iterdir(path: Path) -> Any:
            if path == home:
                raise PermissionError(13, "Permission denied", str(path).replace("\\", "/"))
            return real(path)

        return str(home), iterdir

    def cases(self) -> list[tuple[str, tuple[str, ...], Any, list[str]]]:
        """Each named point: its command line, the patch that fails there, and the lines it ends with."""
        self.deploy_ok("--all")  # verify needs deployed adapters
        home, iterdir = self.canary_listing_denied()
        listed = home.replace("\\", "/")
        return [
            (
                "source discovery",
                ("--all", "--dry-run"),
                mock.patch("deployer.source._discover_directories", side_effect=denied),
                [
                    f"ERROR: Could not read the skill source: {PATH}: Permission denied",
                    "Check that this checkout is complete and that you can read it, then retry.",
                    DEBUG_HINT,
                ],
            ),
            (
                "canary-home listing",
                ("--canary-home", home, "--all"),
                mock.patch.object(Path, "iterdir", iterdir),
                [
                    f"ERROR: Could not list --canary-home: {listed}: Permission denied",
                    "Choose an empty directory you can read under the temporary directory, then retry.",
                    DEBUG_HINT,
                ],
            ),
            (
                "dry run",
                ("--all", "--dry-run"),
                mock.patch("deployer.plan.build", side_effect=denied),
                [f"ERROR: Could not finish the dry run: {PATH}: Permission denied", RETRY, DEBUG_HINT],
            ),
            (
                "check",
                ("check",),
                mock.patch("deployer.tools.probe", side_effect=denied),
                [f"ERROR: Could not finish the check: {PATH}: Permission denied", RETRY, DEBUG_HINT],
            ),
            (
                "verify",
                ("verify",),
                mock.patch("deployer.verify.tempfile.mkdtemp", side_effect=denied),
                [f"ERROR: Could not finish verifying the adapters: {PATH}: Permission denied", RETRY, DEBUG_HINT],
            ),
        ]

    def test_an_os_error_ends_with_its_lines_and_no_traceback(self) -> None:
        for name, arguments, patch, lines in self.cases():
            with self.subTest(point=name), patch, mock.patch.dict(os.environ, {"DEPLOYER_DEBUG": ""}):
                result = self.run_cli(
                    *arguments, refusal_home=Path(arguments[1]) if name == "canary-home listing" else None
                )
                self.assertEqual(1, result.code, result.output)
                self.assertTrue(result.output.endswith("\n" + "\n".join(lines) + "\n\n"), result.output)
                self.assertNotIn(TRACEBACK, result.output)

    def test_debug_prints_the_traceback_after_the_lines(self) -> None:
        for name, arguments, patch, lines in self.cases():
            for how, extra, environment in (
                ("flag", ("--debug",), {"DEPLOYER_DEBUG": ""}),
                ("variable", (), {"DEPLOYER_DEBUG": "1"}),
            ):
                with self.subTest(point=name, debug=how), patch, mock.patch.dict(os.environ, environment):
                    result = self.run_cli(
                        *arguments, *extra, refusal_home=Path(arguments[1]) if name == "canary-home listing" else None
                    )
                    self.assertEqual(1, result.code, result.output)
                    message = "\n".join(lines) + "\n\n"
                    self.assertIn(message, result.output)
                    self.assertIn(TRACEBACK, result.output)
                    self.assertLess(result.output.index(message), result.output.index(TRACEBACK))
                    self.assertIn("PermissionError: [Errno 13] Permission denied", result.output)
                    self.assertTrue(result.output.endswith("\n\n"), result.output)

    def test_debug_variable_zero_or_empty_is_off(self) -> None:
        for value in ("0", ""):
            with (
                self.subTest(value=value),
                mock.patch("deployer.plan.build", side_effect=denied),
                mock.patch.dict(os.environ, {"DEPLOYER_DEBUG": value}),
            ):
                result = self.run_cli("--all", "--dry-run")
                self.assertEqual(1, result.code, result.output)
                self.assertNotIn(TRACEBACK, result.output)


class ConvertedTests(unittest.TestCase):
    """An OSError or Ctrl+C as the lines a command ends with, and the line naming any other exception."""

    def test_an_os_error_names_the_action_path_and_reason_with_the_remedy(self) -> None:
        failure = PermissionError(13, "Permission denied", PATH)
        converted = errors.converted(failure, "finish the deployment")
        self.assertEqual(
            (f"ERROR: Could not finish the deployment: {PATH}: Permission denied", RETRY, DEBUG_HINT),
            converted.lines,
        )
        self.assertEqual(1, converted.exit_code)
        self.assertIs(failure, converted.__cause__)
        self.assertEqual(
            f"ERROR: Could not finish recovery: {PATH}: Permission denied", errors.named(failure, "finish recovery")
        )

    def test_ctrl_c_is_a_cancellation_in_the_words_its_moment_gives(self) -> None:
        for given, lines in (
            ((), (errors.CANCELLED,)),
            (
                (errors.DEPLOYMENT_CANCELLED,),
                ("Cancelled after the deployment began; its changes are reconciled before exit.",),
            ),
        ):
            with self.subTest(lines=lines):
                interrupt = KeyboardInterrupt()
                converted = errors.converted(interrupt, "finish the deployment", *given)
                self.assertEqual(lines, converted.lines)
                self.assertEqual(130, converted.exit_code)
                self.assertIs(interrupt, converted.__cause__)

    def test_any_other_exception_is_named_as_unexpected(self) -> None:
        self.assertEqual("ERROR: Unexpected RuntimeError: boom", errors.named(RuntimeError("boom"), "finish recovery"))

"""Every deploy.py command exits 130 when cancelled by Ctrl+C or by the end of input at a prompt, changing nothing."""

from __future__ import annotations

import contextlib
import io
import unittest
from typing import Any
from unittest import mock

from harness import DeployerTestCase, Result

from deployer import cli

CANCELLED = "Cancelled; nothing was changed."
CONFIGURE_CANCELLED = "Configuration cancelled; existing config was not changed."


def interrupt(*_arguments: Any, **_keywords: Any) -> Any:
    raise KeyboardInterrupt


def interrupting() -> io.StringIO:
    """Standard input whose next read behaves as if the user pressed Ctrl+C."""
    return mock.Mock(io.StringIO, readline=mock.Mock(side_effect=KeyboardInterrupt))


class CancellationTests(DeployerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()

    def run_cli(self, *arguments: str, stdin: io.StringIO | None = None) -> Result:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            code = cli.main(list(arguments), self.paths, stdin if stdin is not None else io.StringIO(""))
        return Result(code, captured.getvalue())

    def assert_cancelled(self, result: Result, message: str) -> None:
        self.assertEqual(130, result.code, result.output)
        self.assertTrue(result.output.endswith(f"\n{message}\n\n"), result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertNotIn("Deployment failed", result.output)

    def test_configure_cancels_at_ctrl_c_and_at_the_end_of_input(self) -> None:
        before = self.config_file().read_bytes()
        for name, stdin in (("ctrl+c", interrupting()), ("end of input", io.StringIO(""))):
            with self.subTest(cancelled_by=name):
                self.assert_cancelled(self.run_cli("configure", stdin=stdin), CONFIGURE_CANCELLED)
                self.assertEqual(before, self.config_file().read_bytes())

    def test_the_selection_prompt_cancels_at_ctrl_c_and_at_the_end_of_input(self) -> None:
        for arguments in ((), ("--dry-run",)):
            for name, stdin in (("ctrl+c", interrupting()), ("end of input", io.StringIO(""))):
                with self.subTest(arguments=arguments, cancelled_by=name):
                    self.assert_cancelled(self.run_cli(*arguments, stdin=stdin), CANCELLED)
                    self.assertFalse((self.skills_dir / "alpha").exists())
                    self.assertFalse(self.manifest_file.exists())
                    self.assertFalse((self.home / ".claude" / "deployer" / ".deploy.lock.d").exists())

    def test_ctrl_c_before_the_lock_cancels_the_deployment(self) -> None:
        for arguments in (("--all",), ("--all", "--dry-run")):
            with self.subTest(arguments=arguments), mock.patch("deployer.source._discover", side_effect=interrupt):
                self.assert_cancelled(self.run_cli(*arguments), CANCELLED)
                self.assertFalse(self.manifest_file.exists())

    def test_ctrl_c_in_a_dry_run_cancels_it(self) -> None:
        with mock.patch("deployer.plan.build", side_effect=interrupt):
            self.assert_cancelled(self.run_cli("--all", "--dry-run"), CANCELLED)

    def test_ctrl_c_cancels_check(self) -> None:
        with mock.patch("deployer.tools.probe", side_effect=interrupt):
            self.assert_cancelled(self.run_cli("check"), CANCELLED)

    def test_ctrl_c_cancels_verify(self) -> None:
        self.deploy_ok("--all")
        with mock.patch("deployer.verify.tempfile.mkdtemp", side_effect=interrupt):
            self.assert_cancelled(self.run_cli("verify"), CANCELLED)


if __name__ == "__main__":
    unittest.main()

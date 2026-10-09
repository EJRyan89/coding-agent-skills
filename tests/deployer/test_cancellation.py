"""Every deploy.py command exits 130 when cancelled by Ctrl+C or by the end of input at a prompt, changing nothing."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from harness import DeployerTestCase, Result

from deployer import cli, fsops, hashing, pipeline, platform_support

CANCELLED = "Cancelled; nothing was changed."
RECOVERY_CANCELLED = "Cancelled during recovery; the next deployment finishes recovering before it deploys."
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

    @property
    def lock_dir(self) -> Path:
        return self.home / ".claude" / "deployer" / ".deploy.lock.d"

    def interrupted_run(self) -> Path:
        """Deploy alpha, then leave the journal of an uncommitted run killed after it replaced alpha."""
        self.assertEqual(0, self.run_cli("--all").code)
        alpha = self.skills_dir / "alpha"
        original = hashing.hash_path(alpha)
        alpha.rename(self.skills_dir / "alpha.deploying-bak")
        self.write(alpha / "SKILL.md", "installed by the interrupted run\n")
        backup: dict[str, object] = {"op": "backup", "item": "alpha", "from": "skills/alpha", "retain": False}
        backup.update({"to": "skills/alpha.deploying-bak", "backup_hash": original})
        install = {"op": "install", "item": "alpha", "from": "staging/alpha", "to": "skills/alpha"}
        install["staged_hash"] = hashing.hash_path(alpha)
        run = self.home / ".claude" / "deployer" / "staging" / "20260101-000000-kill"
        self.write(run / "journal.jsonl", "".join(json.dumps(entry) + "\n" for entry in (backup, install)))
        return run

    def test_ctrl_c_during_startup_recovery_cancels_and_the_next_deployment_finishes_it(self) -> None:
        run = self.interrupted_run()
        real_move = fsops.move

        def interrupted_restore(source: Path, destination: Path) -> None:
            if source.name == "alpha.deploying-bak":
                raise KeyboardInterrupt
            real_move(source, destination)

        with mock.patch("deployer.fsops.move", side_effect=interrupted_restore):
            self.assert_cancelled(self.run_cli("--all"), RECOVERY_CANCELLED)
        self.assertFalse(self.lock_dir.exists())
        self.assertTrue((run / "journal.jsonl").is_file())
        result = self.run_cli("--all")
        self.assertEqual(0, result.code, result.output)
        self.assertIn("Recovering uncommitted run 20260101-000000-kill (rolling back)...", result.output)
        self.assertIn("Alpha", self.skill_text("alpha"))
        self.assertFalse(run.exists())
        self.assertFalse((self.skills_dir / "alpha.deploying-bak").exists())
        self.assertFalse(self.lock_dir.exists())

    def test_ctrl_c_while_taking_the_lock_cancels_and_leaves_no_lock(self) -> None:
        """Ctrl+C between creating the lock and writing its metadata removes it, so the next run is not refused."""
        real_write = fsops.write_atomic

        def interrupted_metadata(path: Path, content: bytes) -> None:
            if path.name == "info.json":
                raise KeyboardInterrupt
            real_write(path, content)

        stale_info = {"pid": 4242, "token": "stale-token", "start_time": 1}
        for name, stale in (("fresh lock", None), ("reclaimed stale lock", stale_info)):
            with self.subTest(taking=name):
                if stale is not None:
                    self.write(self.lock_dir / "token", "stale-token")
                    self.write(self.lock_dir / "info.json", json.dumps(stale))
                with mock.patch("deployer.fsops.write_atomic", side_effect=interrupted_metadata):
                    self.assert_cancelled(self.deploy_with_dead_holder("--all"), CANCELLED)
                self.assertEqual([], sorted(path.name for path in self.lock_dir.parent.glob(".deploy.lock*")))
                self.assertFalse(self.manifest_file.exists())
                result = self.deploy_with_dead_holder("--all")
                self.assertEqual(0, result.code, result.output)
                self.assertNotIn("Stale lock", result.output)
                self.assertTrue((self.skills_dir / "alpha" / "SKILL.md").is_file())
                self.assertFalse(self.lock_dir.exists())
                shutil.rmtree(self.skills_dir / "alpha")
                self.manifest_file.unlink()

    def deploy_with_dead_holder(self, *arguments: str) -> Result:
        """Deploy as `python deploy.py` does, judging any lock it finds as held by a process that has exited."""
        captured = io.StringIO()
        probe = mock.Mock(return_value=platform_support.ProcessStatus(False, None))
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            code = pipeline.run(list(arguments), self.paths, probe=probe, stdin=io.StringIO(""))
        return Result(code, captured.getvalue())

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

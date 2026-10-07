"""Regression tests for the bounded, non-interactive layer under the git and gh clients."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bounded_process
from bounded_process import Finished, run_bounded

ENVIRONMENT_PROGRAM = (
    "import json, os, sys; "
    "print(json.dumps({'stdin': sys.stdin.read(), "
    "'variables': {name: os.environ.get(name) for name in ('GIT_TERMINAL_PROMPT', 'GCM_INTERACTIVE', "
    "'GH_PROMPT_DISABLED', 'KEPT')}}))"
)


class BoundedProcessTests(unittest.TestCase):
    def test_the_command_reads_no_stdin_and_sees_every_prompt_turned_off(self) -> None:
        with mock.patch.dict("os.environ", {"KEPT": "a value", "GIT_TERMINAL_PROMPT": "1", "GCM_INTERACTIVE": "auto"}):
            finished = run_bounded([sys.executable, "-c", ENVIRONMENT_PROGRAM], 60)
        self.assertEqual(0, finished.returncode)
        self.assertEqual(
            {
                "stdin": "",
                "variables": {
                    "GIT_TERMINAL_PROMPT": "0",
                    "GCM_INTERACTIVE": "never",
                    "GH_PROMPT_DISABLED": "1",
                    "KEPT": "a value",
                },
            },
            json.loads(finished.stdout),
        )

    def test_exit_status_and_both_streams_are_returned_as_bytes(self) -> None:
        program = "import sys; sys.stdout.buffer.write(b'out \\xff'); sys.stderr.buffer.write(b'err'); sys.exit(4)"
        self.assertEqual(Finished(4, b"out \xff", b"err"), run_bounded([sys.executable, "-c", program], 60))

    def test_stdout_may_go_to_a_file_of_the_callers(self) -> None:
        program = "import sys; sys.stdout.buffer.write(bytes(range(256)))"
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "out"
            with target.open("wb") as handle:
                finished = run_bounded([sys.executable, "-c", program], 60, stdout=handle)
            self.assertEqual(bytes(range(256)), target.read_bytes())
        self.assertEqual(Finished(0, b"", b""), finished)

    def test_a_command_that_runs_too_long_is_killed_and_raises(self) -> None:
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_bounded([sys.executable, "-c", "import time; time.sleep(60)"], 0.5)
        self.assertLess(time.monotonic() - started, 30)

    def test_a_process_left_holding_the_output_does_not_hold_the_timeout(self) -> None:
        # The command starts a child that inherits its output and outlives it, as git-remote-https can.
        child = "import time; time.sleep(20)"
        program = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)"
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_bounded([sys.executable, "-c", program], 1)
        self.assertLess(time.monotonic() - started, 15)

    def test_a_missing_command_raises_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            run_bounded(["coding-agent-skills-no-such-command"], 60)

    def test_the_prompt_switches(self) -> None:
        self.assertEqual(
            {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GH_PROMPT_DISABLED": "1"},
            bounded_process.NON_INTERACTIVE,
        )


if __name__ == "__main__":
    unittest.main()

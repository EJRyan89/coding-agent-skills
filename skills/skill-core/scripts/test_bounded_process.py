"""Regression tests for the bounded, non-interactive layer under the git and gh clients."""

from __future__ import annotations

import hashlib
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

# The streaming tests read a command's first output under STARTUP_SECONDS, which covers starting interpreters on a
# loaded machine, and only then lower the idle limit to IDLE_SECONDS, the short limit under test, so how long start-up
# takes never decides whether the limit holds. A read that must raise may do so up to IDLE_MARGIN_SECONDS past it,
# which covers a loaded machine scheduling the reader late, and still shows the read ended by the limit.
STARTUP_SECONDS = 60.0
IDLE_SECONDS = 0.5
IDLE_MARGIN_SECONDS = 10.0


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

    def test_given_input_is_the_whole_stdin_with_every_prompt_still_off(self) -> None:
        with mock.patch.dict("os.environ", {"KEPT": "kept", "GIT_TERMINAL_PROMPT": "1", "GCM_INTERACTIVE": "auto"}):
            finished = run_bounded([sys.executable, "-c", ENVIRONMENT_PROGRAM], 60, input_bytes=b"given\n")
        self.assertEqual(0, finished.returncode)
        self.assertEqual(
            {
                "stdin": "given\n",
                "variables": {
                    "GIT_TERMINAL_PROMPT": "0",
                    "GCM_INTERACTIVE": "never",
                    "GH_PROMPT_DISABLED": "1",
                    "KEPT": "kept",
                },
            },
            json.loads(finished.stdout),
        )

    def test_input_larger_than_a_pipe_arrives_byte_for_byte_and_then_ends(self) -> None:
        given = bytes(range(256)) * 8192 + b"\r\n\0 last"  # 2 MiB, far past any pipe's buffer
        program = (
            "import hashlib, sys; data = sys.stdin.buffer.read(); after = sys.stdin.buffer.read(); "
            "print(len(data), hashlib.sha256(data).hexdigest(), len(after))"
        )
        finished = run_bounded([sys.executable, "-c", program], 60, input_bytes=given)
        self.assertEqual((0, b""), (finished.returncode, finished.stderr))
        self.assertEqual(f"{len(given)} {hashlib.sha256(given).hexdigest()} 0", finished.stdout.decode().strip())

    def test_a_command_that_reads_none_of_its_input_still_finishes(self) -> None:
        started = time.monotonic()
        finished = run_bounded([sys.executable, "-c", "pass"], 60, input_bytes=b"x" * (4 * 1024 * 1024))
        self.assertEqual(Finished(0, b"", b""), finished)
        self.assertLess(time.monotonic() - started, 30)

    def test_the_time_limit_covers_a_command_given_input(self) -> None:
        program = "import sys, time; sys.stdin.buffer.read(); time.sleep(60)"
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_bounded([sys.executable, "-c", program], 0.5, input_bytes=b"request\n")
        self.assertLess(time.monotonic() - started, 30)

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

    def test_stderr_may_go_to_a_file_of_the_callers_and_share_it_with_stdout(self) -> None:
        program = (
            "import sys; sys.stdout.write('one '); sys.stdout.flush(); sys.stderr.write('two '); sys.stderr.flush(); "
            "sys.stdout.write('three')"
        )
        with tempfile.TemporaryDirectory() as temporary:
            alone, shared = Path(temporary) / "alone", Path(temporary) / "shared"
            with alone.open("wb") as handle:
                separate = run_bounded([sys.executable, "-c", program], 60, stderr=handle)
            with shared.open("wb") as handle:
                together = run_bounded([sys.executable, "-c", program], 60, stdout=handle, stderr=handle)
            self.assertEqual((b"two ", b"one two three"), (alone.read_bytes(), shared.read_bytes()))
        self.assertEqual((Finished(0, b"one three", b""), Finished(0, b"", b"")), (separate, together))

    def test_the_command_may_run_in_another_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "a folder"
            directory.mkdir()
            finished = run_bounded([sys.executable, "-c", "import os; print(os.getcwd())"], 60, cwd=directory)
            self.assertEqual(directory.resolve(), Path(finished.stdout.decode().strip()).resolve())

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


# Answers each request line on stdin with "<line> answered" and the environment's prompt switches, then exits.
ANSWERING_PROGRAM = (
    "import os, sys\n"
    "switches = f\"{os.environ.get('GIT_TERMINAL_PROMPT')} {os.environ.get('GCM_INTERACTIVE')}\"\n"
    "for line in sys.stdin.buffer:\n"
    "    sys.stdout.buffer.write(line.strip() + b' answered ' + switches.encode() + bytes([10]))\n"
    "    sys.stdout.flush()\n"
    "sys.stderr.write('done')\n"
    "sys.exit(3)\n"
)


class StreamingTests(unittest.TestCase):
    def test_requests_go_in_and_answers_come_out_with_prompts_off(self) -> None:
        with bounded_process.streaming([sys.executable, "-c", ANSWERING_PROGRAM], idle_timeout=60) as running:
            running.stdin.write(b"first\nsecond\n")
            running.stdin.flush()
            self.assertEqual(b"first answered 0 never\n", running.readline())
            self.assertEqual(b"second", running.read(6))
            self.assertEqual(b" answered 0 never\n", running.readline())
            running.stdin.close()
            self.assertEqual(b"", running.readline())
            self.assertEqual(b"", running.read(1))
            self.assertEqual(3, running.wait())
            self.assertEqual(b"done", running.errors())

    def test_a_line_read_with_a_limit_is_cut_and_its_rest_left_to_the_next_read(self) -> None:
        # Sixteen megabytes in one line, written in pieces, then two short lines.
        program = (
            "import sys\n"
            "for _ in range(256):\n"
            "    sys.stdout.buffer.write(b'z' * 65536); sys.stdout.flush()\n"
            "sys.stdout.buffer.write(bytes([10]) + b'short' + bytes([10]) + b'end')\n"
        )
        with bounded_process.streaming([sys.executable, "-c", program], idle_timeout=60) as running:
            self.assertEqual(b"z" * 100, running.readline(100))
            rest = 0
            while not (piece := running.readline(4096)).endswith(b"\n"):
                self.assertLessEqual(len(piece), 4096)
                rest += len(piece)
            self.assertEqual(256 * 65536 - 100, rest + len(piece) - 1)
            self.assertEqual(b"sho", running.readline(3))
            self.assertEqual(b"rt\n", running.readline(100))
            self.assertEqual(b"end", running.readline(100))
            self.assertEqual(b"", running.readline(100))
            self.assertEqual(0, running.wait())

    def test_output_that_keeps_arriving_never_times_out(self) -> None:
        # Two seconds of output in all, but never half a second without a byte.
        program = (
            "import sys, time\n"
            "for _ in range(40):\n"
            "    sys.stdout.buffer.write(b'x' * 4096); sys.stdout.flush(); time.sleep(0.05)\n"
        )
        with bounded_process.streaming([sys.executable, "-c", program], idle_timeout=STARTUP_SECONDS) as running:
            first = running.read(4096)
            running.idle_timeout = IDLE_SECONDS
            self.assertEqual(b"x" * 4096 * 40, first + running.read(4096 * 39))
            running.idle_timeout = STARTUP_SECONDS  # the wait bounds the interpreter's exit, which is not under test
            self.assertEqual(0, running.wait())

    def test_a_read_that_waits_too_long_raises_even_while_a_child_holds_the_pipe(self) -> None:
        # The command answers once, then stalls, and a child it started holds its stdout open, as the program Git for
        # Windows' git.exe launches does. Killing the command would not end the read; the idle bound does.
        child = "import time; time.sleep(30)"
        program = (
            f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {child!r}])\n"
            "print('ready'); sys.stdout.flush(); time.sleep(60)\n"
        )
        command = [sys.executable, "-c", program]
        with bounded_process.streaming(command, idle_timeout=STARTUP_SECONDS, exit_wait=0.5) as running:
            self.assertEqual(b"ready\n", running.readline().replace(b"\r", b""))
            running.idle_timeout = IDLE_SECONDS
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                running.readline()
            self.assertLess(time.monotonic() - started, IDLE_SECONDS + IDLE_MARGIN_SECONDS)
        self.assertLess(time.monotonic() - started, 25)

    def test_leaving_early_lets_a_command_still_writing_exit_by_itself(self) -> None:
        program = "import sys\nfor _ in range(64):\n    sys.stdout.buffer.write(b'y' * (1024 * 1024))\n"
        started = time.monotonic()
        with bounded_process.streaming([sys.executable, "-c", program], idle_timeout=60, exit_wait=30) as running:
            self.assertEqual(b"y" * 1024, running.read(1024))
        self.assertLess(time.monotonic() - started, 20)  # well inside exit_wait: it ended without being killed

    def test_a_missing_command_raises_file_not_found(self) -> None:
        with (
            self.assertRaises(FileNotFoundError),
            bounded_process.streaming(["coding-agent-skills-no-such"], idle_timeout=1),
        ):
            pass


if __name__ == "__main__":
    unittest.main()

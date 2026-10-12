"""Regression tests for the Windows job object that ends every process a command starts."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import job_tree
from bounded_process import run_bounded
from job_tree import JobRefused, JobTree

# Seconds a started program gets to come up on a loaded machine, and a held file to be let go once its holder ends.
STARTUP_SECONDS = 60.0
RELEASE_SECONDS = 10.0
# Starts a program that holds the file named by its first argument open and then creates "<file>.ready", and waits
# for it to be ready; with a second argument, it then runs on, as a command that hangs does, and otherwise it exits.
LAUNCHER_PROGRAM = (
    "import os, subprocess, sys, time\n"
    "held = sys.argv[1]\n"
    'holder = \'import sys, time\\nhandle = open(sys.argv[1], "w")\\nopen(sys.argv[1] + ".ready", "w").close()\\n'
    "time.sleep(120)\\n'\n"
    "subprocess.Popen([sys.executable, '-c', holder, held])\n"
    "for _ in range(1200):\n"
    "    if os.path.exists(held + '.ready'): break\n"
    "    time.sleep(0.05)\n"
    "if len(sys.argv) > 2: time.sleep(120)\n"
)


class ReadyTree(JobTree):
    """Holds the command as JobTree does, and lets its time limit start only once the program it started is ready, so
    how long start-up takes never decides whether that program ran."""

    def __init__(self, ready: Path) -> None:
        self.ready = ready

    @contextmanager
    def hold(self, process: subprocess.Popen[Any]) -> Iterator[None]:
        with super().hold(process):
            deadline = time.monotonic() + STARTUP_SECONDS
            while not self.ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            yield


class JobTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="job-tree-test.")
        self.addCleanup(temporary.cleanup)
        self.held = Path(temporary.name) / "held.txt"
        self.ready = Path(f"{self.held}.ready")

    def assert_released(self) -> None:
        """The program that held the file has ended: Windows refuses to remove a file another process holds open."""
        self.assertTrue(self.ready.exists(), "the started program never came up")
        deadline = time.monotonic() + RELEASE_SECONDS
        while True:
            try:
                self.held.unlink()
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    self.fail("the program the command started still holds its file")
                time.sleep(0.05)

    def test_a_program_left_running_by_a_finished_command_is_ended_with_the_tree(self) -> None:
        finished = run_bounded(
            [sys.executable, "-c", LAUNCHER_PROGRAM, str(self.held)], STARTUP_SECONDS, tree=ReadyTree(self.ready)
        )
        self.assertEqual(0, finished.returncode)
        self.assert_released()

    def test_a_command_killed_at_its_time_limit_ends_every_program_it_started(self) -> None:
        command = [sys.executable, "-c", LAUNCHER_PROGRAM, str(self.held), "hang"]
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_bounded(command, 0.5, tree=ReadyTree(self.ready))
        self.assert_released()
        self.assertLess(time.monotonic() - started, STARTUP_SECONDS + RELEASE_SECONDS)

    def test_a_running_command_that_cannot_join_is_killed_and_refused(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        with (
            mock.patch.object(job_tree, "job_holding", side_effect=OSError("AssignProcessToJobObject failed")),
            self.assertRaisesRegex(JobRefused, r"^it could not join a Windows job object: AssignProcessToJobObject"),
            JobTree().hold(process),
        ):
            self.fail("a command that cannot join is never held")
        self.assertIsNotNone(process.poll(), "the refused command was killed")

    def test_a_command_already_gone_when_it_would_join_is_held_without_a_job(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait(timeout=STARTUP_SECONDS)
        entered = False
        with (
            mock.patch.object(job_tree, "job_holding", side_effect=OSError("OpenProcess failed")),
            JobTree().hold(process),
        ):
            entered = True
        self.assertTrue(entered)

    def test_a_job_that_cannot_be_made_names_the_call_and_the_windows_error(self) -> None:
        kernel32 = mock.Mock()
        kernel32.CreateJobObjectW.return_value = None
        with (
            mock.patch.object(job_tree, "_kernel32", return_value=kernel32),
            mock.patch.object(job_tree.ctypes, "get_last_error", return_value=5, create=True),
            mock.patch.object(job_tree.ctypes, "FormatError", return_value="Access is denied. ", create=True),
            self.assertRaisesRegex(OSError, r"^CreateJobObjectW failed: Access is denied\. \(Windows error 5\)$"),
        ):
            job_tree.job_holding(1)

    def test_a_job_that_cannot_take_the_process_is_closed(self) -> None:
        kernel32 = mock.Mock()
        kernel32.CreateJobObjectW.return_value = 11
        kernel32.OpenProcess.return_value = 22
        kernel32.AssignProcessToJobObject.return_value = 0
        with (
            mock.patch.object(job_tree, "_kernel32", return_value=kernel32),
            self.assertRaisesRegex(OSError, "^AssignProcessToJobObject failed: "),
        ):
            job_tree.job_holding(1)
        self.assertEqual([mock.call(11), mock.call(22)], kernel32.CloseHandle.call_args_list)

    def test_the_windows_job_values(self) -> None:
        # Pinned against the values winnt.h and the job object documentation give, not the module's own names.
        self.assertEqual(
            (0x0001, 0x0100, 9, 0x00002000),
            (
                job_tree.PROCESS_TERMINATE,
                job_tree.PROCESS_SET_QUOTA,
                job_tree.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                job_tree.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            ),
        )


if __name__ == "__main__":
    unittest.main()

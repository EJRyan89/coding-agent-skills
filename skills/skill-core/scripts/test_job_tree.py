"""Regression tests for the Windows job object that ends every process a command starts."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Iterator
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
# A command that never exits is refused at SHORT_LIMIT_SECONDS, a short limit, because SignalledTree starts the clock
# only once the command has signalled that it reached its wait, which it gets up to STARTUP_SECONDS to do. The refusal
# may come up to REFUSAL_MARGIN_SECONDS past the limit, which covers killing the command and ending what it started.
SHORT_LIMIT_SECONDS = 1
REFUSAL_MARGIN_SECONDS = 30
ACCESS_DENIED = 5
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


class SignalledTree(JobTree):
    """Holds the command as JobTree does, and lets its time limit start only once the command has created `signal`, so
    how long start-up takes never decides whether the command ran or whether the limit is what refused it. `released`
    is when the clock started. Every suite that times a command under a JobTree uses it."""

    def __init__(self, signal: Path, *, hidden_window: bool = False) -> None:
        super().__init__(hidden_window=hidden_window)
        self.signal = signal
        self.released: float | None = None

    @contextmanager
    def hold(self, process: subprocess.Popen[Any]) -> Iterator[None]:
        with super().hold(process):
            deadline = time.monotonic() + STARTUP_SECONDS
            while not self.signal.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.released = time.monotonic()
            yield

    def assert_refused_at_the_limit(self, test: unittest.TestCase, refused: float) -> None:
        """That `refused`, a monotonic time, came no sooner than SHORT_LIMIT_SECONDS after the signal, and within
        REFUSAL_MARGIN_SECONDS of it."""
        if self.released is None:
            test.fail("the command ran outside its process tree")
        test.assertGreaterEqual(refused - self.released, SHORT_LIMIT_SECONDS, "refused no sooner than its limit")
        test.assertLess(refused - self.released, SHORT_LIMIT_SECONDS + REFUSAL_MARGIN_SECONDS, "refused at its limit")


class FailingKernel:
    """kernel32 as job_tree calls it, with each call named in `failing` failing as a kernel call does: it returns no
    result and leaves ACCESS_DENIED as the thread's last error. The arguments of each call's last failure are kept."""

    def __init__(self, *failing: str) -> None:
        self.real = job_tree._kernel32()
        self.failing = set(failing)
        self.failed: dict[str, tuple[Any, ...]] = {}

    def __getattr__(self, name: str) -> Callable[..., Any]:
        call: Callable[..., Any] = getattr(self.real, name)
        if name not in self.failing:
            return call

        def fail(*arguments: Any) -> int:
            self.failed[name] = arguments
            job_tree.ctypes.set_last_error(ACCESS_DENIED)  # type: ignore[attr-defined]  # Windows only
            return 0

        return fail


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
            [sys.executable, "-c", LAUNCHER_PROGRAM, str(self.held)], STARTUP_SECONDS, tree=SignalledTree(self.ready)
        )
        self.assertEqual(0, finished.returncode)
        self.assert_released()

    def test_a_command_killed_at_its_time_limit_ends_every_program_it_started(self) -> None:
        command = [sys.executable, "-c", LAUNCHER_PROGRAM, str(self.held), "hang"]
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_bounded(command, 0.5, tree=SignalledTree(self.ready))
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

    def test_a_command_no_job_can_hold_is_killed_before_it_starts_a_program(self) -> None:
        command = [sys.executable, "-c", LAUNCHER_PROGRAM, str(self.held), "hang"]
        for call in ("CreateJobObjectW", "SetInformationJobObject", "OpenProcess", "AssignProcessToJobObject"):
            with (
                self.subTest(failing=call),
                mock.patch.object(job_tree, "_kernel32", return_value=FailingKernel(call)),
                self.assertRaises(JobRefused) as raised,
            ):
                run_bounded(command, STARTUP_SECONDS, tree=JobTree())
            self.assertRegex(
                str(raised.exception),
                rf"^it could not join a Windows job object: {call} failed: .+ \(Windows error {ACCESS_DENIED}\)$",
            )
            time.sleep(1)
            self.assertFalse(self.ready.exists(), "the command was killed before it started its program")

    def test_a_job_that_cannot_end_its_processes_ends_them_as_it_closes(self) -> None:
        command = [sys.executable, "-c", LAUNCHER_PROGRAM, str(self.held)]
        for failing in ("TerminateJobObject", "CloseHandle"):
            with self.subTest(failing=failing):
                kernel = FailingKernel(failing)
                with mock.patch.object(job_tree, "_kernel32", return_value=kernel):
                    finished = run_bounded(command, STARTUP_SECONDS, tree=SignalledTree(self.ready))
                self.assertEqual(0, finished.returncode)
                self.assertIn(failing, kernel.failed)
                self.assert_released()
                self.ready.unlink()

    def test_a_job_that_can_neither_end_its_processes_nor_close_says_they_may_still_run(self) -> None:
        command = [sys.executable, "-c", LAUNCHER_PROGRAM, str(self.held)]
        kernel = FailingKernel("TerminateJobObject", "CloseHandle")
        with (
            mock.patch.object(job_tree, "_kernel32", return_value=kernel),
            self.assertRaisesRegex(
                OSError,
                r"^The job's processes may still run: TerminateJobObject failed: .+; CloseHandle failed: ",
            ),
        ):
            run_bounded(command, STARTUP_SECONDS, tree=SignalledTree(self.ready))
        with self.assertRaises(PermissionError, msg="the error is true: the program still holds its file"):
            self.held.unlink()
        self.assertTrue(job_tree._kernel32().CloseHandle(kernel.failed["CloseHandle"][0]))
        self.assert_released()

    def test_the_hidden_window_is_a_start_option(self) -> None:
        self.assertEqual({}, JobTree().start_options())
        self.assertEqual({"creationflags": 0x08000000}, JobTree(hidden_window=True).start_options())

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
            (0x0001, 0x0100, 9, 0x00002000, 0x08000000),
            (
                job_tree.PROCESS_TERMINATE,
                job_tree.PROCESS_SET_QUOTA,
                job_tree.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                job_tree.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
                job_tree.CREATE_NO_WINDOW,
            ),
        )


if __name__ == "__main__":
    unittest.main()

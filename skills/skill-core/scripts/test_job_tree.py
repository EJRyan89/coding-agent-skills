"""Regression tests for the process trees that end every process a command starts: a Windows job object, and a
process group elsewhere."""

from __future__ import annotations

import contextlib
import ctypes
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
from job_tree import GroupTree, JobRefused, JobTree
from process_identity import process_status

# Seconds a started program gets to come up on a loaded machine, and a held file to be let go once its holder ends.
STARTUP_SECONDS = 60.0
RELEASE_SECONDS = 10.0
# A command that never exits is refused at SHORT_LIMIT_SECONDS, a short limit, because SignalledTree starts the clock
# only once the command has signalled that it reached its wait, which it gets up to STARTUP_SECONDS to do. The refusal
# may come up to REFUSAL_MARGIN_SECONDS past the limit, which covers killing the command and ending what it started.
SHORT_LIMIT_SECONDS = 1
REFUSAL_MARGIN_SECONDS = 30
ACCESS_DENIED = 5
# What a kernel call that does not report failure by returning no result returns when it fails: (DWORD)-1.
FAILED_RESULTS = {"ResumeThread": 0xFFFFFFFF}
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


# Starts a program that writes its PID to the file named by the first argument, and waits for it; with a second
# argument, it then runs on, as a command that hangs does, and otherwise it exits, leaving that program running.
GROUP_LAUNCHER = (
    "import os, subprocess, sys, time\n"
    "ready = sys.argv[1]\n"
    'holder = \'import os, sys, time\\npartial = sys.argv[1] + ".partial"\\n'
    'open(partial, "w").write(str(os.getpid()))\\nos.replace(partial, sys.argv[1])\\ntime.sleep(120)\\n\'\n'
    "subprocess.Popen([sys.executable, '-c', holder, ready])\n"
    "for _ in range(1200):\n"
    "    if os.path.exists(ready): break\n"
    "    time.sleep(0.05)\n"
    "if len(sys.argv) > 2: time.sleep(120)\n"
)
SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]
# Starts a program that runs on, writes its PID to the file named by its first argument, and exits at once, leaving
# that program running.
QUICK_LAUNCHER = (
    "import subprocess, sys\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
)
# The longest PausedTree waits before it holds the command: long enough for a command that runs meanwhile to start its
# program and exit.
PAUSE_SECONDS = 2.0


def group_stat(pid: int, command: str, state: str, group: int) -> bytes:
    """A /proc/<pid>/stat line as Linux writes it, up to the fields a group's listing reads."""
    return f"{pid} ({command}) {state} 1 {group} {group} 0 -1 0\n".encode()


def ended(pid: int, seconds: float = 20.0) -> bool:
    """Whether the process `pid` has ended within `seconds`. On Windows that is counted only once Windows has signaled
    its process object, which it does after closing every handle the process held, its working directory's among them,
    so the folder it ran in can then be removed; a probe of its exit code reports it gone before that. Elsewhere a
    process has ended once it is gone or waits only to be reaped."""
    if sys.platform != "win32":
        deadline = time.monotonic() + seconds
        while process_status(pid).alive:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True
    from ctypes import wintypes

    synchronize, wait_object_0, invalid_parameter = 0x00100000, 0, 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]  # Windows only
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        error = ctypes.get_last_error()  # type: ignore[attr-defined]  # Windows only
        if error == invalid_parameter:
            return True  # no process has this PID any more
        raise ctypes.WinError(error)  # type: ignore[attr-defined]  # Windows only
    try:
        return bool(kernel32.WaitForSingleObject(handle, int(seconds * 1000)) == wait_object_0)
    finally:
        kernel32.CloseHandle(handle)


# This platform's tree, which SignalledTree holds a command in.
if sys.platform == "win32":
    NativeTree = JobTree
else:
    NativeTree = GroupTree


class SignalledTree(NativeTree):
    """Holds the command as this platform's tree does, and lets its time limit start only once the command has
    created `signal`, so how long start-up takes never decides whether the command ran or whether the limit is what
    refused it. `released` is when the clock started. Every suite that times a command under a process tree uses it."""

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


class PausedTree(NativeTree):
    """Holds the command as this platform's tree does, but only once the command has created `started`, or
    PAUSE_SECONDS on, whichever is sooner: a pause between its start and its joining the tree, in which a command
    already running outside the tree would start a program the tree never holds. `ran_before_held` says whether the
    command created `started` during the pause."""

    def __init__(self, started: Path) -> None:
        super().__init__()
        self.started = started
        self.ran_before_held: bool | None = None

    @contextmanager
    def hold(self, process: subprocess.Popen[Any]) -> Iterator[None]:
        deadline = time.monotonic() + PAUSE_SECONDS
        while not self.started.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.ran_before_held = self.started.exists()
        with super().hold(process):
            yield


class FailingKernel:
    """kernel32 as job_tree calls it, with each call named in `failing` failing as a kernel call does: it returns no
    result, or the value FAILED_RESULTS gives for a call that reports failure otherwise, and leaves ACCESS_DENIED as the
    thread's last error. The arguments of each failed call are kept in order."""

    def __init__(self, *failing: str) -> None:
        self.real = job_tree._kernel32()
        self.failing = set(failing)
        self.failed: dict[str, list[tuple[Any, ...]]] = {}

    def __getattr__(self, name: str) -> Callable[..., Any]:
        call: Callable[..., Any] = getattr(self.real, name)
        if name not in self.failing:
            return call

        def fail(*arguments: Any) -> int:
            self.failed.setdefault(name, []).append(arguments)
            job_tree.ctypes.set_last_error(ACCESS_DENIED)  # type: ignore[attr-defined]  # Windows only
            return FAILED_RESULTS.get(name, 0)

        return fail


if sys.platform == "win32":

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
                [sys.executable, "-c", LAUNCHER_PROGRAM, str(self.held)],
                STARTUP_SECONDS,
                tree=SignalledTree(self.ready),
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
                self.assertRaisesRegex(
                    JobRefused, r"^it could not join a Windows job object: AssignProcessToJobObject"
                ),
                JobTree().hold(process),
            ):
                self.fail("a command that cannot join is never held")
            self.assertIsNotNone(process.poll(), "the refused command was killed")

        def test_a_command_no_job_can_hold_is_killed_before_it_starts_a_program(self) -> None:
            command = [sys.executable, "-c", LAUNCHER_PROGRAM, str(self.held), "hang"]
            joining = "it could not join a Windows job object"
            resuming = "it could not be resumed in its Windows job object"
            for call, refusal in (
                ("CreateJobObjectW", joining),
                ("SetInformationJobObject", joining),
                ("OpenProcess", joining),
                ("AssignProcessToJobObject", joining),
                ("CreateToolhelp32Snapshot", resuming),
                ("Thread32First", resuming),
                ("Thread32Next", resuming),
                ("OpenThread", resuming),
                ("ResumeThread", resuming),
            ):
                with (
                    self.subTest(failing=call),
                    mock.patch.object(job_tree, "_kernel32", return_value=FailingKernel(call)),
                    self.assertRaises(JobRefused) as raised,
                ):
                    run_bounded(command, STARTUP_SECONDS, tree=JobTree())
                self.assertRegex(
                    str(raised.exception), rf"^{refusal}: {call} failed: .+ \(Windows error {ACCESS_DENIED}\)$"
                )
                time.sleep(1)
                self.assertFalse(self.ready.exists(), "the command was killed before it started its program")

        def test_a_command_that_cannot_be_resumed_once_it_joined_is_killed_with_its_job(self) -> None:
            process = subprocess.Popen(SLEEPER, **JobTree().start_options())
            self.addCleanup(process.wait)
            self.addCleanup(process.kill)
            with (
                mock.patch.object(job_tree, "resume", side_effect=OSError("no thread of process 1 was listed")),
                mock.patch.object(job_tree, "_end_job", wraps=job_tree._end_job) as end_job,
                self.assertRaisesRegex(
                    JobRefused, "^it could not be resumed in its Windows job object: no thread of process 1 was listed$"
                ),
                JobTree().hold(process),
            ):
                self.fail("a command that cannot be resumed is never held")
            self.assertIsNotNone(process.poll(), "the refused command was killed")
            end_job.assert_called_once()

        def test_a_command_already_gone_when_it_would_be_resumed_is_held_in_its_job(self) -> None:
            process = subprocess.Popen([sys.executable, "-c", "pass"])
            process.wait(timeout=STARTUP_SECONDS)
            with (
                mock.patch.object(job_tree, "job_holding", return_value=7),
                mock.patch.object(job_tree, "resume", side_effect=OSError("no thread of process 1 was listed")),
                mock.patch.object(job_tree, "_end_job") as end_job,
                JobTree().hold(process),
            ):
                end_job.assert_not_called()
            end_job.assert_called_once_with(7)

        def test_a_command_started_by_its_tree_runs_only_once_it_has_joined(self) -> None:
            started = Path(f"{self.held}.started")
            process = subprocess.Popen(
                [sys.executable, "-c", "import sys; open(sys.argv[1], 'w').close()", str(started)],
                **JobTree().start_options(),
            )
            self.addCleanup(process.wait)
            self.addCleanup(process.kill)
            time.sleep(PAUSE_SECONDS)
            self.assertIsNone(process.poll(), "the command waits, suspended, to join its job")
            self.assertFalse(started.exists(), "the command has not run before it joined")
            with JobTree().hold(process):
                self.assertEqual(0, process.wait(timeout=STARTUP_SECONDS))
            self.assertTrue(started.exists(), "the command ran once it joined")

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
            self.assertTrue(job_tree._kernel32().CloseHandle(kernel.failed["TerminateJobObject"][0][0]))
            self.assert_released()

        def test_leaving_the_tree_returns_only_once_every_program_has_let_go_of_its_files(self) -> None:
            # Ending a job is asynchronous: without the wait, a folder the programs ran in can still be in use here.
            for how, extra, room in (
                ("finished", [], 64),
                ("killed at its limit", ["hang"], 64),
                ("listed in parts", [], 1),
            ):
                with self.subTest(how=how), mock.patch.object(job_tree, "LISTED_PROCESSES", room):
                    command = [sys.executable, "-c", LAUNCHER_PROGRAM, str(self.held), *extra]
                    with contextlib.suppress(subprocess.TimeoutExpired):  # the hanging command is killed at its limit
                        run_bounded(command, 1 if extra else STARTUP_SECONDS, tree=SignalledTree(self.ready))
                    self.assertTrue(self.ready.exists(), "the started program came up")
                    self.held.unlink()  # no retry: nothing holds it any more
                    self.ready.unlink()

        def test_a_job_it_cannot_list_is_ended_and_says_its_processes_may_still_run(self) -> None:
            command = [sys.executable, "-c", LAUNCHER_PROGRAM, str(self.held)]
            with (
                mock.patch.object(job_tree, "_kernel32", return_value=FailingKernel("QueryInformationJobObject")),
                self.assertRaisesRegex(
                    OSError,
                    r"^The job's processes may still run: QueryInformationJobObject failed: .+ \(Windows error 5\)$",
                ),
            ):
                run_bounded(command, STARTUP_SECONDS, tree=SignalledTree(self.ready))
            self.assert_released()

        def test_processes_that_have_not_ended_in_time_are_reported(self) -> None:
            timeout = 258  # WAIT_TIMEOUT
            kernel = FailingKernel()
            command = [sys.executable, "-c", LAUNCHER_PROGRAM, str(self.held)]
            with (
                mock.patch.object(job_tree, "_kernel32", return_value=kernel),
                mock.patch.object(kernel, "WaitForSingleObject", return_value=timeout, create=True),
                self.assertRaisesRegex(
                    OSError, r"^The job's processes may still run: process \d+ had not ended 10 seconds on$"
                ),
            ):
                run_bounded(command, STARTUP_SECONDS, tree=SignalledTree(self.ready))
            self.assert_released()

        def test_a_process_has_ended_only_once_windows_has_let_go_of_it(self) -> None:
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            self.addCleanup(process.wait)
            self.addCleanup(process.kill)
            self.assertFalse(ended(process.pid, 0.2), "a running process has not ended")
            process.kill()
            self.assertTrue(ended(process.pid), "a killed process has ended")
            process.wait(timeout=STARTUP_SECONDS)

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


class ProcessTreeTests(unittest.TestCase):
    """What each tree asks of the command's start, and which one this platform uses, on every platform."""

    def test_a_job_tree_starts_the_command_suspended_and_hides_its_window_when_asked(self) -> None:
        self.assertEqual({"creationflags": 0x00000004}, JobTree().start_options())
        self.assertEqual({"creationflags": 0x08000004}, JobTree(hidden_window=True).start_options())

    def test_a_group_tree_starts_the_command_as_the_leader_of_a_session_and_hides_nothing(self) -> None:
        for hidden in (False, True):
            with self.subTest(hidden_window=hidden):
                self.assertEqual({"start_new_session": True}, GroupTree(hidden_window=hidden).start_options())

    def test_windows_holds_a_command_in_a_job_and_every_other_platform_in_a_process_group(self) -> None:
        for platform, kind in (("win32", JobTree), ("linux", GroupTree), ("darwin", GroupTree)):
            with self.subTest(platform=platform), mock.patch.object(job_tree.sys, "platform", platform):
                tree = job_tree.process_tree(hidden_window=True)
                self.assertIs(kind, type(tree))
                self.assertTrue(tree.hidden_window)

    def test_a_group_lists_its_processes_still_running_as_proc_shows_them(self) -> None:
        with tempfile.TemporaryDirectory(prefix="group-tree-test.") as temporary:
            proc = Path(temporary)
            for pid, command, state, group in (
                (100, "python3", "S", 7),
                (101, "python3", "Z", 7),  # ended, waiting to be reaped
                (102, "bash", "R", 8),  # another group
                (103, "odd ) 8 (name", "R", 7),
                (104, "python3", "X", 7),
            ):
                (proc / str(pid)).mkdir()
                (proc / str(pid) / "stat").write_bytes(group_stat(pid, command, state, group))
            (proc / "105").mkdir()  # gone before its stat file could be read
            (proc / "self").mkdir()
            with mock.patch.object(job_tree, "PROC", temporary):
                self.assertEqual([100, 103], sorted(job_tree.group_members(7)))
                self.assertEqual([], job_tree.group_members(9))

    def test_the_windows_job_values(self) -> None:
        # Pinned against the values winnt.h and the job object documentation give, not the module's own names.
        self.assertEqual(
            (0x0001, 0x0100, 9, 0x00002000, 0x08000000, 0x00000004, 0x0002, 0x00000004, 18, 0xFFFFFFFF),
            (
                job_tree.PROCESS_TERMINATE,
                job_tree.PROCESS_SET_QUOTA,
                job_tree.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                job_tree.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
                job_tree.CREATE_NO_WINDOW,
                job_tree.CREATE_SUSPENDED,
                job_tree.THREAD_SUSPEND_RESUME,
                job_tree.TH32CS_SNAPTHREAD,
                job_tree.ERROR_NO_MORE_FILES,
                job_tree.RESUME_FAILED,
            ),
        )

    def test_a_thread_entry_is_laid_out_as_tlhelp32_declares_it(self) -> None:
        self.assertEqual(28, ctypes.sizeof(job_tree._ThreadEntry))


class HeldFromTheStartTests(unittest.TestCase):
    """A command is in its tree before it runs, on every platform, so a program it starts and leaves at once is ended
    with the tree however long the command takes to join it."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="held-from-the-start-test.")
        self.addCleanup(temporary.cleanup)
        self.started = Path(temporary.name) / "child.pid"

    def test_a_program_started_by_a_command_that_exits_before_it_joins_is_ended_with_the_tree(self) -> None:
        tree = PausedTree(self.started)
        finished = run_bounded([sys.executable, "-c", QUICK_LAUNCHER, str(self.started)], STARTUP_SECONDS, tree=tree)
        self.assertEqual(0, finished.returncode)
        if sys.platform == "win32":
            self.assertFalse(tree.ran_before_held, "the command waited, suspended, to join its job")
        child = int(self.started.read_text(encoding="utf-8"))
        self.assertTrue(ended(child, 0), "the program the command started has ended with the tree")


if sys.platform != "win32":

    class GroupTreeTests(unittest.TestCase):
        def setUp(self) -> None:
            temporary = tempfile.TemporaryDirectory(prefix="group-tree-test.")
            self.addCleanup(temporary.cleanup)
            self.ready = Path(temporary.name) / "holder.pid"

        def holder(self) -> int:
            self.assertTrue(self.ready.exists(), "the started program never came up")
            return int(self.ready.read_text(encoding="utf-8"))

        def test_a_program_left_running_by_a_finished_command_is_ended_with_the_tree(self) -> None:
            command = [sys.executable, "-c", GROUP_LAUNCHER, str(self.ready)]
            finished = run_bounded(command, STARTUP_SECONDS, tree=SignalledTree(self.ready))
            self.assertEqual(0, finished.returncode)
            # Leaving the tree returns only once nothing of the group runs, so the program has ended already.
            self.assertTrue(ended(self.holder(), 0), "the program the command started has ended")

        def test_a_command_killed_at_its_time_limit_ends_every_program_it_started(self) -> None:
            command = [sys.executable, "-c", GROUP_LAUNCHER, str(self.ready), "hang"]
            with self.assertRaises(subprocess.TimeoutExpired):
                run_bounded(command, 0.5, tree=SignalledTree(self.ready))
            self.assertTrue(ended(self.holder(), 0), "the program the command started has ended")

        def test_a_command_that_does_not_lead_a_group_of_its_own_is_killed_and_refused(self) -> None:
            process = subprocess.Popen(SLEEPER)  # started without the tree's options, so in this process's group
            self.addCleanup(process.wait)
            self.addCleanup(process.kill)
            with (
                self.assertRaisesRegex(JobRefused, "^it was not started as the leader of a process group of its own$"),
                GroupTree().hold(process),
            ):
                self.fail("a command outside a group of its own is never held")
            self.assertIsNotNone(process.poll(), "the refused command was killed")

        def test_a_group_it_cannot_signal_says_its_processes_may_still_run(self) -> None:
            with (
                mock.patch.object(job_tree.os, "killpg", side_effect=PermissionError(1, "Operation not permitted")),
                self.assertRaisesRegex(OSError, r"^The group's processes may still run: killpg failed: "),
            ):
                job_tree.end_group(4242)

        def test_a_group_with_no_process_left_has_ended(self) -> None:
            with (
                mock.patch.object(job_tree.os, "killpg", side_effect=ProcessLookupError(3, "No such process")),
                mock.patch.object(job_tree, "group_members") as members,
            ):
                job_tree.end_group(4242)
            members.assert_not_called()

        def test_processes_that_have_not_ended_in_time_are_reported(self) -> None:
            with (
                mock.patch.object(job_tree.os, "killpg"),
                mock.patch.object(job_tree, "group_members", return_value=[4243]),
                mock.patch.object(job_tree, "KILL_WAIT_SECONDS", 0.2),
                self.assertRaisesRegex(
                    OSError, r"^The group's processes may still run: process 4243 had not ended 0.2 seconds on$"
                ),
            ):
                job_tree.end_group(4242)

        def test_without_proc_the_group_itself_is_asked(self) -> None:
            for raised, expected in ((None, [4242]), (ProcessLookupError(3, "No such process"), [])):
                with (
                    self.subTest(raised=raised),
                    mock.patch.object(job_tree, "PROC", str(Path(self.ready.parent) / "not mounted")),
                    mock.patch.object(job_tree.os, "killpg", side_effect=raised) as killpg,
                ):
                    self.assertEqual(expected, job_tree.group_members(4242))
                killpg.assert_called_once_with(4242, 0)


if __name__ == "__main__":
    unittest.main()

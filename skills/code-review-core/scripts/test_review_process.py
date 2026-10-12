from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_process
from review_process import (
    JobTree,
    ProcessStatus,
    path_programs,
    process_status,
    same_process,
    start_detached,
    winget_copilot,
)

# Prints a line, then sleeps until it is terminated, so its liveness can be probed from outside.
SLEEPER = (
    "import sys, time; print('host started', flush=True); "
    "print('to stderr', file=sys.stderr, flush=True); time.sleep(120)"
)

# Starts a program that writes its PID to the file its first argument names and sleeps, then sleeps itself unless its
# second argument is "exit", as a launcher that leaves its program running does.
LAUNCHER = (
    "import subprocess, sys, time\n"
    'child = \'import os, sys, time; from pathlib import Path; part = Path(sys.argv[1] + ".part"); '
    "part.write_text(str(os.getpid())); os.replace(part, sys.argv[1]); time.sleep(120)'\n"
    "subprocess.Popen([sys.executable, '-c', child, sys.argv[1]], stdin=subprocess.DEVNULL)\n"
    "if sys.argv[2] != 'exit':\n"
    "    time.sleep(120)\n"
)


def terminate(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)


def kernel32() -> ctypes.WinDLL:
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.CreateFileW.restype = wintypes.HANDLE
    library.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    library.OpenProcess.restype = wintypes.HANDLE
    library.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    return library


def opens_exclusively(path: Path) -> bool:
    """Whether the file opens for writing with no sharing, which Windows refuses while any handle to it is open."""
    generic_write, open_existing, normal, sharing_violation = 0x40000000, 3, 0x80, 32
    library = kernel32()
    handle = library.CreateFileW(str(path), generic_write, 0, None, open_existing, normal, None)
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        if error == sharing_violation:
            return False
        raise ctypes.WinError(error)
    library.CloseHandle(handle)
    return True


def terminate_and_wait(pid: int) -> bool:
    """End this one process, not its tree, and report whether its process object was signaled within 20 seconds.

    Windows closes every handle a process holds before it signals the process object, but sets the exit code that
    process_status reads before either, so only this wait proves the process's own handles are gone.
    """
    terminate_access, synchronize, wait_object_0 = 0x0001, 0x00100000, 0
    library = kernel32()
    handle = library.OpenProcess(terminate_access | synchronize, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not library.TerminateProcess(handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        return library.WaitForSingleObject(handle, 20_000) == wait_object_0
    finally:
        library.CloseHandle(handle)


def ended(pid: int, seconds: float = 20.0) -> bool:
    """Whether the process `pid` has ended within `seconds`, counted only once Windows has signaled its process
    object, which it does after closing every handle the process held, its working directory's among them, so the
    folder it ran in can then be removed. process_status reports it gone as soon as its exit code is set, before that.
    """
    synchronize, wait_object_0, invalid_parameter = 0x00100000, 0, 87
    library = kernel32()
    handle = library.OpenProcess(synchronize, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == invalid_parameter:
            return True  # no process has this PID any more
        raise ctypes.WinError(error)
    try:
        return library.WaitForSingleObject(handle, int(seconds * 1000)) == wait_object_0
    finally:
        library.CloseHandle(handle)


def wait_until(condition, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


# A command that never exits is refused at SHORT_LIMIT_SECONDS, a short limit, because SignalledTree starts the clock
# only once the command has signalled that it reached its wait, which it gets up to STARTUP_SECONDS to do on a loaded
# machine. The refusal may come up to REFUSAL_MARGIN_SECONDS past the limit, which covers killing the command and
# ending what it started.
SHORT_LIMIT_SECONDS = 1
STARTUP_SECONDS = 60
REFUSAL_MARGIN_SECONDS = 30


class SignalledTree(JobTree):
    """A command's process tree, held as JobTree holds it, that starts the command's time limit only once the command
    has created `signal`, so how long start-up takes never decides whether the limit is what refuses it. `released`
    is when the clock started. The condition and Copilot CLI host suites both time their never-exiting commands here.
    """

    def __init__(self, signal: Path, *, hidden_window: bool = False) -> None:
        super().__init__(hidden_window=hidden_window)
        self.signal = signal
        self.released: float | None = None

    @contextmanager
    def hold(self, process: subprocess.Popen[bytes]) -> Iterator[None]:
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


ACCESS_DENIED = 5


class FailingKernel:
    """kernel32 as review_process calls it, with each call named in `failing` failing as a kernel call does: it
    returns no result and leaves ACCESS_DENIED as the thread's last error. Each failed call's arguments are kept."""

    def __init__(self, *failing: str) -> None:
        self.real = review_process._kernel32()
        self.failing = set(failing)
        self.failed: dict[str, tuple[Any, ...]] = {}

    def __getattr__(self, name: str) -> Callable[..., Any]:
        call: Callable[..., Any] = getattr(self.real, name)
        if name not in self.failing:
            return call

        def fail(*arguments: Any) -> int:
            self.failed[name] = arguments
            ctypes.set_last_error(ACCESS_DENIED)
            return 0

        return fail


class SameProcessTests(unittest.TestCase):
    """The deployer lock's rule: a live PID is the recorded process only when its start time matches."""

    def test_identity_follows_pid_and_start_time(self) -> None:
        self.assertIs(False, same_process(ProcessStatus(False, None), 100))
        self.assertIs(True, same_process(ProcessStatus(True, 100), 100))
        self.assertIs(False, same_process(ProcessStatus(True, 200), 100), "a reused PID is another process")
        self.assertIsNone(same_process(ProcessStatus(True, None), 100), "an unreadable start time proves nothing")
        self.assertIsNone(same_process(ProcessStatus(True, 100), None), "nothing was recorded to compare")


class ProcessStatusTests(unittest.TestCase):
    def test_this_process_is_alive_with_a_start_time(self) -> None:
        status = process_status(os.getpid())
        self.assertTrue(status.alive)
        self.assertIsInstance(status.start_time, int)
        self.assertEqual(status, process_status(os.getpid()), "the start time is stable")

    def test_an_exited_process_is_not_alive(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait()
        # The Popen handle keeps the exited process queryable, so this reads its exit code, not a missing PID.
        self.assertFalse(process_status(process.pid).alive)


class WingetCopilotTests(unittest.TestCase):
    def test_copilot_is_found_only_where_winget_links_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"LOCALAPPDATA": temporary}):
            self.assertIsNone(winget_copilot())
            link = Path(temporary) / "Microsoft" / "WinGet" / "Links" / "copilot.exe"
            link.parent.mkdir(parents=True)
            self.assertIsNone(winget_copilot(), "the Links folder alone is not an installation")
            link.write_bytes(b"fixture")
            self.assertEqual(link, winget_copilot())


class PathProgramsTests(unittest.TestCase):
    def test_every_match_on_path_in_order_by_the_extension_rules_of_shutil_which(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first, second, third = (Path(temporary) / f"folder {index}" for index in range(3))
            for folder in (first, second, third):
                folder.mkdir()
            (first / "copilot").write_bytes(b"no extension, so not a Windows command")
            (first / "copilot.cmd").write_bytes(b"fixture")
            (first / "copilot.exe").write_bytes(b"fixture")
            (second / "copilot.exe").mkdir()
            (third / "copilot.bat").write_bytes(b"fixture")
            path = os.pathsep.join(("", str(first), str(second), str(first), str(third)))
            with mock.patch.dict(os.environ, {"PATH": path, "PATHEXT": ".EXE;.BAT;.CMD"}):
                self.assertEqual(
                    [str(first / "copilot.EXE"), str(first / "copilot.CMD"), str(third / "copilot.BAT")],
                    path_programs("copilot"),
                    "PATHEXT's order within a folder, PATH's across them; a folder once; no directory",
                )
                self.assertEqual(shutil.which("copilot", path=str(first)), path_programs("copilot")[0])
                self.assertEqual([str(third / "copilot.bat")], path_programs("copilot.bat"), "a name with one")
            with mock.patch.dict(os.environ, {"PATH": "", "PATHEXT": ".EXE"}):
                self.assertEqual([], path_programs("copilot"))


class StartDetachedTests(unittest.TestCase):
    def test_a_detached_host_outlives_its_start_and_logs_its_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run with spaces"
            root.mkdir()
            log = root / "host.log"
            pid = start_detached([sys.executable, "-c", SLEEPER], root, log)
            self.addCleanup(terminate, pid)
            status = process_status(pid)
            self.assertTrue(status.alive)
            if not isinstance(status.start_time, int):
                self.fail("a live process reports its start time")
            self.assertTrue(wait_until(lambda: "to stderr" in log.read_text(encoding="utf-8", errors="replace")))
            text = log.read_text(encoding="utf-8", errors="replace")
            self.assertIn("host started", text)
            self.assertTrue(same_process(process_status(pid), status.start_time))
            self.assertFalse(same_process(process_status(pid), status.start_time + 1), "PID reuse reads as gone")
            terminate(pid)
            self.assertTrue(wait_until(lambda: not process_status(pid).alive))
            # process_status reads the exit code, which Windows sets moments before it closes the host's handles,
            # so the log is released shortly after, not at once; the directory's removal needs it released.
            self.assertTrue(wait_until(lambda: opens_exclusively(log)), "the log is released within 20 seconds")

    def test_once_the_host_has_exited_nothing_else_holds_its_log(self) -> None:
        # A handle to the log inherited by another process, or left open in this one, would outlive the host.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run with spaces"
            root.mkdir()
            log = root / "host.log"
            pid = start_detached([sys.executable, "-c", SLEEPER], root, log)
            self.addCleanup(terminate, pid)
            self.assertTrue(wait_until(lambda: "to stderr" in log.read_text(encoding="utf-8", errors="replace")))
            self.assertFalse(opens_exclusively(log), "the running host holds its log")
            self.assertTrue(terminate_and_wait(pid), "the host exits")
            self.assertFalse(process_status(pid).alive)
            self.assertTrue(opens_exclusively(log), "nothing holds the log once the host has exited")


class JobTreeTests(unittest.TestCase):
    """Leaving a held tree ends the command and every process it started, however it ended."""

    def launch(self, folder: Path, how: str) -> tuple[subprocess.Popen[bytes], Path]:
        pid_file = folder / "child.pid"
        command = [sys.executable, "-c", LAUNCHER, str(pid_file), how]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        self.addCleanup(terminate, process.pid)
        return process, pid_file

    def started_child(self, pid_file: Path) -> int:
        self.assertTrue(wait_until(pid_file.exists, 60), "the launcher started its program")
        child = int(pid_file.read_text(encoding="utf-8"))
        self.addCleanup(terminate, child)
        self.assertTrue(process_status(child).alive)
        return child

    def test_leaving_ends_a_command_still_running_and_the_program_it_started(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            process, pid_file = self.launch(Path(temporary), "stay")
            with JobTree().hold(process):
                child = self.started_child(pid_file)
                self.assertIsNone(process.poll())
            self.assertNotEqual(0, process.wait(timeout=20), "the command was ended, not left to finish")
            self.assertTrue(wait_until(lambda: not process_status(child).alive), "the program it started is gone")

    def test_leaving_ends_a_program_left_running_by_a_command_that_exited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            process, pid_file = self.launch(Path(temporary), "exit")
            with JobTree().hold(process):
                child = self.started_child(pid_file)
                self.assertEqual(0, process.wait(timeout=60))
                self.assertTrue(process_status(child).alive, "the program outlives the command until the tree ends")
            self.assertTrue(wait_until(lambda: not process_status(child).alive), "the program it started is gone")

    def test_a_command_no_job_holds_is_ended_and_refused_before_it_runs(self) -> None:
        for call in ("CreateJobObjectW", "SetInformationJobObject", "OpenProcess", "AssignProcessToJobObject"):
            with (
                self.subTest(failing=call),
                tempfile.TemporaryDirectory() as temporary,
                mock.patch.object(review_process, "_kernel32", return_value=FailingKernel(call)),
            ):
                process, pid_file = self.launch(Path(temporary), "stay")
                with self.assertRaises(OSError) as raised, JobTree().hold(process):
                    self.fail("the command ran without a job object")
                self.assertRegex(
                    str(raised.exception),
                    r"^Cannot hold the command in a Windows job object, so it was ended: "
                    rf"\[WinError 5\] {call} failed: ",
                )
                cause = raised.exception.__cause__
                self.assertIsInstance(cause, OSError)
                self.assertEqual(ACCESS_DENIED, getattr(cause, "winerror", None))
                self.assertIsNotNone(process.returncode, "the command was ended and reaped before the refusal")
                self.assertNotEqual(0, process.returncode)
                self.assertFalse(wait_until(pid_file.exists, 1), "nothing the command could start is running")

    def test_a_command_already_gone_is_refused_rather_than_held_without_a_job(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL)
        self.assertEqual(0, process.wait(timeout=60))
        with self.assertRaises(OSError) as raised, JobTree().hold(process):
            self.fail("the command was held without a job object")
        self.assertRegex(
            str(raised.exception),
            r"^Cannot hold the command in a Windows job object, so it was ended: \[WinError \d+\] "
            r"(OpenProcess|AssignProcessToJobObject) failed: ",
        )

    def test_a_job_that_cannot_end_its_processes_ends_them_when_it_closes(self) -> None:
        for failing in ("TerminateJobObject", "CloseHandle"):
            with self.subTest(failing=failing), tempfile.TemporaryDirectory() as temporary:
                process, pid_file = self.launch(Path(temporary), "stay")
                kernel = FailingKernel(failing)
                ending = mock.patch.object(review_process, "_kernel32", return_value=kernel)
                self.addCleanup(ending.stop)
                with JobTree().hold(process):
                    child = self.started_child(pid_file)
                    ending.start()
                ending.stop()
                self.assertIn(failing, kernel.failed)
                process.wait(timeout=20)  # it sleeps far longer, so returning at all means it was ended
                gone = wait_until(lambda pid=child: not process_status(pid).alive)
                self.assertTrue(gone, "the program it started is gone")

    def test_a_job_that_can_neither_end_its_processes_nor_close_says_they_may_still_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            process, pid_file = self.launch(Path(temporary), "stay")
            kernel = FailingKernel("TerminateJobObject", "CloseHandle")
            ending = mock.patch.object(review_process, "_kernel32", return_value=kernel)
            self.addCleanup(ending.stop)
            with self.assertRaises(OSError) as raised, JobTree().hold(process):
                child = self.started_child(pid_file)
                ending.start()
            ending.stop()
            self.assertRegex(
                str(raised.exception),
                r"^The job's processes may still run: \[WinError 5\] TerminateJobObject failed: .*; "
                r"\[WinError 5\] CloseHandle failed: ",
            )
            self.assertIsNone(process.poll(), "the error is true: the command still runs")
            self.assertTrue(review_process._kernel32().CloseHandle(kernel.failed["CloseHandle"][0]))
            process.wait(timeout=20)  # it sleeps far longer, so returning at all means closing the job ended it
            self.assertTrue(wait_until(lambda: not process_status(child).alive), "and the program it started")

    def test_the_hidden_window_is_a_start_option(self) -> None:
        self.assertEqual({}, JobTree().start_options())
        self.assertEqual({"creationflags": 0x08000000}, JobTree(hidden_window=True).start_options())


if __name__ == "__main__":
    unittest.main()

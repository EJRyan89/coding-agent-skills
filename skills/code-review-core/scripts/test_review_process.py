from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
import unittest
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_process import ProcessStatus, process_status, same_process, start_detached

# Prints a line, then sleeps until it is terminated, so its liveness can be probed from outside.
SLEEPER = (
    "import sys, time; print('host started', flush=True); "
    "print('to stderr', file=sys.stderr, flush=True); time.sleep(120)"
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


def wait_until(condition, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


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


if __name__ == "__main__":
    unittest.main()

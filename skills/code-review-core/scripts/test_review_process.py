from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

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

    def test_a_command_that_cannot_join_a_job_is_held_without_one(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL)
        process.wait(timeout=60)
        with JobTree().hold(process):
            pass  # its handle is closed and its PID free, so no job takes it; the caller still kills it on timeout

    def test_the_hidden_window_is_a_start_option(self) -> None:
        self.assertEqual({}, JobTree().start_options())
        self.assertEqual({"creationflags": 0x08000000}, JobTree(hidden_window=True).start_options())


if __name__ == "__main__":
    unittest.main()

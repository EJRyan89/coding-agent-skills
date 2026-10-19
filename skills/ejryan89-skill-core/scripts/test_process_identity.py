"""Regression tests for the one probe of whether a process runs and when it started."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import job_tree
import process_identity
from process_identity import ProcessStatus, proc_status, process_status, stat_fields

# Seconds a started child gets to come up, and an ended one to be reported as ended, on a loaded machine.
SETTLE_SECONDS = 20.0
SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]


def stat_line(pid: int, command: str, state: str, start_time: int) -> bytes:
    """A /proc/<pid>/stat line as Linux writes it, with every field this probe does not read as zero."""
    before_start = " ".join(["0"] * 18)  # the parent's ID through the item before the start time, fields 4 to 21
    return f"{pid} ({command}) {state} {before_start} {start_time} 0 0\n".encode()


class ProcessStatusTests(unittest.TestCase):
    """The probe on the platform the suite runs on, against real processes."""

    def test_this_process_is_alive_with_a_stable_start_time(self) -> None:
        status = process_status(os.getpid())
        self.assertTrue(status.alive)
        self.assertIsInstance(status.start_time, int)
        self.assertEqual(status, process_status(os.getpid()), "the start time is stable")

    def test_a_running_child_is_alive_and_started_after_this_process(self) -> None:
        child = subprocess.Popen(SLEEPER)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        status = process_status(child.pid)
        own = process_status(os.getpid())
        if status.start_time is None or own.start_time is None:
            self.fail("a running process reports its start time")
        self.assertTrue(status.alive)
        self.assertGreaterEqual(status.start_time, own.start_time)

    def test_an_exited_child_is_not_alive(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=SETTLE_SECONDS)
        self.assertEqual(ProcessStatus(False, None), process_status(child.pid))

    def test_no_process_has_a_pid_of_zero_or_below(self) -> None:
        for pid in (0, -1):
            with self.subTest(pid=pid):
                self.assertFalse(process_status(pid).alive)

    if sys.platform != "win32":

        def test_a_child_that_has_ended_but_is_not_yet_reaped_is_not_alive(self) -> None:
            child = subprocess.Popen([sys.executable, "-c", "pass"])
            self.addCleanup(child.wait)
            stat = Path(f"/proc/{child.pid}/stat")
            deadline = time.monotonic() + SETTLE_SECONDS
            while b") Z " not in stat.read_bytes():
                if time.monotonic() >= deadline:
                    self.fail("the child never became a zombie")
                time.sleep(0.05)
            self.assertEqual(ProcessStatus(False, None), process_status(child.pid))


class ProcStatusTests(unittest.TestCase):
    """How /proc is read, from stat files written as Linux writes them, on every platform."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="process-identity-test.")
        self.addCleanup(temporary.cleanup)
        self.proc = Path(temporary.name)
        patcher = mock.patch.object(process_identity, "PROC", self.proc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_stat(self, pid: int, content: bytes) -> None:
        (self.proc / str(pid)).mkdir()
        (self.proc / str(pid) / "stat").write_bytes(content)

    def test_the_start_time_is_the_twenty_second_field(self) -> None:
        self.write_stat(4242, stat_line(4242, "python3", "S", 987654))
        self.assertEqual(ProcessStatus(True, 987654), proc_status(4242))

    def test_a_command_name_with_spaces_and_parentheses_is_passed_over(self) -> None:
        self.write_stat(4242, stat_line(4242, "deploy (x) S 1 2", "R", 31337))
        self.assertEqual(ProcessStatus(True, 31337), proc_status(4242))

    def test_a_zombie_or_a_dead_process_has_ended(self) -> None:
        for pid, state in ((4243, "Z"), (4244, "X"), (4245, "x")):
            with self.subTest(state=state):
                self.write_stat(pid, stat_line(pid, "python3", state, 5))
                self.assertEqual(ProcessStatus(False, None), proc_status(pid))

    def test_a_stat_file_it_cannot_read_reports_the_process_alive_with_no_start_time(self) -> None:
        # A directory where the stat file should be fails to read as an unreadable file does.
        (self.proc / "4246" / "stat").mkdir(parents=True)
        self.assertEqual(ProcessStatus(True, None), proc_status(4246))

    def test_a_stat_file_it_cannot_parse_reports_the_process_alive_with_no_start_time(self) -> None:
        for pid, content in (
            (4247, b""),
            (4248, b"4248 no parentheses S 0 0\n"),
            (4249, b"4249 (python3) S 1 2 3\n"),
            (4250, stat_line(4250, "python3", "S", 0).replace(b" 0 0 0\n", b" soon 0 0\n")),
        ):
            with self.subTest(content=content):
                self.write_stat(pid, content)
                self.assertEqual(ProcessStatus(True, None), proc_status(pid))

    def test_a_pid_proc_does_not_list_is_asked_of_the_kernel(self) -> None:
        for raised, expected in (
            (ProcessLookupError(3, "No such process"), ProcessStatus(False, None)),
            (PermissionError(1, "Operation not permitted"), ProcessStatus(True, None)),
            (None, ProcessStatus(True, None)),
        ):
            with (
                self.subTest(raised=raised),
                mock.patch.object(process_identity.os, "kill", side_effect=raised) as kill,
            ):
                self.assertEqual(expected, proc_status(4251))
            kill.assert_called_once_with(4251, 0)

    if sys.platform != "win32":
        # Windows' os.kill ends the process whatever the signal, so the kernel is asked only where a signal of none
        # asks without sending anything.

        def test_without_proc_a_running_process_is_never_reported_gone(self) -> None:
            with mock.patch.object(process_identity, "PROC", self.proc / "not mounted"):
                self.assertEqual(ProcessStatus(True, None), proc_status(os.getpid()))

    def test_no_signal_is_sent_for_a_pid_of_zero_or_below(self) -> None:
        with mock.patch.object(process_identity.os, "kill") as kill:
            self.assertEqual(ProcessStatus(False, None), proc_status(0))
        kill.assert_not_called()


class OpenFailureTests(unittest.TestCase):
    """A process Windows will not open a handle to is reported gone only when its PID names no process."""

    # Windows' error codes, from winerror.h.
    NAMES_NO_PROCESS = 87  # ERROR_INVALID_PARAMETER
    UNREADABLE = (
        5,  # ERROR_ACCESS_DENIED
        8,  # ERROR_NOT_ENOUGH_MEMORY
        1450,  # ERROR_NO_SYSTEM_RESOURCES
        1816,  # ERROR_NOT_ENOUGH_QUOTA
        0,  # no error set
    )

    def test_only_an_invalid_parameter_reports_the_process_gone(self) -> None:
        self.assertEqual(ProcessStatus(False, None), process_identity.open_failure_status(self.NAMES_NO_PROCESS))
        for error in self.UNREADABLE:
            with self.subTest(error=error):
                self.assertEqual(ProcessStatus(True, None), process_identity.open_failure_status(error))

    if sys.platform == "win32":

        def test_a_failed_open_process_is_judged_by_its_error(self) -> None:
            for error, expected in (
                (self.NAMES_NO_PROCESS, ProcessStatus(False, None)),
                *((error, ProcessStatus(True, None)) for error in self.UNREADABLE),
            ):
                kernel32 = mock.MagicMock()
                kernel32.OpenProcess.return_value = None
                with (
                    self.subTest(error=error),
                    mock.patch.object(process_identity, "_kernel32", return_value=kernel32),
                    mock.patch("ctypes.get_last_error", return_value=error),
                ):
                    self.assertEqual(expected, process_status(4252))
                kernel32.OpenProcess.assert_called_once_with(0x1000, False, 4252)
                kernel32.GetExitCodeProcess.assert_not_called()
                kernel32.CloseHandle.assert_not_called()


class Kernel32Tests(unittest.TestCase):
    """Every kernel32 function the probe calls is declared, so a pointer-sized handle passes whole."""

    if sys.platform == "win32":

        def test_every_function_the_probe_calls_is_declared(self) -> None:
            import ctypes
            from ctypes import wintypes

            handle = ctypes.c_void_p
            dword = ctypes.c_ulong
            boolean = ctypes.c_long
            filetime = ctypes.POINTER(wintypes.FILETIME)
            kernel32 = process_identity._kernel32()
            for name, argtypes, restype in (
                ("OpenProcess", [dword, boolean, dword], handle),
                ("GetExitCodeProcess", [handle, ctypes.POINTER(dword)], boolean),
                ("GetProcessTimes", [handle, filetime, filetime, filetime, filetime], boolean),
                ("CloseHandle", [handle], boolean),
            ):
                with self.subTest(function=name):
                    self.assertEqual(argtypes, getattr(kernel32, name).argtypes)
                    self.assertIs(restype, getattr(kernel32, name).restype)

        def test_a_handle_above_the_range_of_a_c_int_passes_as_a_handle(self) -> None:
            import ctypes
            from ctypes import wintypes

            exit_code = wintypes.DWORD()
            # No such handle exists, so the call fails as Windows fails it rather than in ctypes' conversion.
            self.assertFalse(process_identity._kernel32().GetExitCodeProcess(2**40, ctypes.byref(exit_code)))

        def test_kernel32_is_loaded_once_for_every_probe(self) -> None:
            import ctypes

            process_identity._kernel32.cache_clear()
            self.addCleanup(process_identity._kernel32.cache_clear)
            with mock.patch("ctypes.WinDLL", wraps=ctypes.WinDLL) as load:
                process_status(os.getpid())
                process_status(os.getpid())
            load.assert_called_once_with("kernel32", use_last_error=True)


class StatFieldsTests(unittest.TestCase):
    """The one parse of a stat line, which job_tree reads a process group's members with too."""

    def test_the_fields_start_after_the_last_closing_parenthesis(self) -> None:
        self.assertEqual([b"R", b"1", b"7"], stat_fields(b"4242 (deploy (x) S 1 2) R 1 7\n"))

    def test_a_line_with_no_closing_parenthesis_has_no_fields(self) -> None:
        for text in (b"", b"4242 no parentheses S 0 0\n"):
            with self.subTest(text=text):
                self.assertEqual([], stat_fields(text))

    def test_job_tree_reads_proc_with_this_module_s_definitions(self) -> None:
        self.assertIs(process_identity.PROC, job_tree.PROC)
        self.assertIs(process_identity.ENDED_STATES, job_tree.ENDED_STATES)
        self.assertIs(stat_fields, job_tree.stat_fields)
        self.assertEqual(Path("/proc"), process_identity.PROC)
        self.assertEqual(frozenset({b"Z", b"X", b"x"}), process_identity.ENDED_STATES)

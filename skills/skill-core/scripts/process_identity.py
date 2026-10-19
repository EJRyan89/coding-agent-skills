"""Whether a process is running and when it started: the one probe of a process's identity.

A process is identified by its PID and its start time. The deployment lock records both for the deployment that holds
it, and the code-review pipeline for the host it starts, so a PID that names a running process whose start time differs
from the one recorded is another program that reuses it. The deployer reaches the probe through
`platform_support.process_status`.

A probe that cannot read a process's state never reports it gone: a process whose state is unreadable is reported
alive with no start time, which its caller treats as a holder it cannot confirm, never as one it may reclaim.
"""

from __future__ import annotations

import functools
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Where Linux reports each process: /proc/<pid>/stat, whose fields after the command name, in parentheses, start with
# the state and hold the start time, in clock ticks since boot, as the twentieth (field 22 of the whole line). It is
# skill-core's one reader of /proc: job_tree imports PROC, ENDED_STATES, and stat_fields from here.
PROC = Path("/proc")
STAT_START_TIME = 19
# The states of a process that has ended: a zombie waits only to be reaped by its parent, and a dead one is going.
ENDED_STATES = frozenset({b"Z", b"X", b"x"})
# The error OpenProcess sets for a PID that names no process.
ERROR_INVALID_PARAMETER = 87


@dataclass(frozen=True)
class ProcessStatus:
    alive: bool
    start_time: int | None


def process_status(pid: int) -> ProcessStatus:
    """Report whether a process is running and, when readable, its start time: its creation time on Windows, and
    elsewhere its start in clock ticks since boot, read from /proc."""
    if sys.platform == "win32":
        return _windows_status(pid)
    return proc_status(pid)


def proc_status(pid: int) -> ProcessStatus:
    """The process's state as /proc reports it. A PID /proc does not list is asked of the kernel with a signal of none,
    which tells a process that has gone from one /proc cannot show, such as another user's under `hidepid`."""
    if pid <= 0:
        return ProcessStatus(False, None)
    try:
        text = (PROC / str(pid) / "stat").read_bytes()
    except FileNotFoundError:
        return _signalled_status(pid)
    except OSError:
        return ProcessStatus(True, None)
    fields = stat_fields(text)
    if len(fields) <= STAT_START_TIME:
        return ProcessStatus(True, None)
    if fields[0] in ENDED_STATES:
        return ProcessStatus(False, None)
    try:
        return ProcessStatus(True, int(fields[STAT_START_TIME]))
    except ValueError:
        return ProcessStatus(True, None)


def stat_fields(text: bytes) -> list[bytes]:
    """The fields of a /proc/<pid>/stat line after the command name, which sits in parentheses and may itself hold
    spaces and parentheses: the state, the parent's ID, the process group's ID, and on. A line with no closing
    parenthesis has none."""
    close = text.rfind(b")")
    return text[close + 1 :].split() if close >= 0 else []


def _signalled_status(pid: int) -> ProcessStatus:
    """A process /proc does not show: gone only when the kernel says no process has its PID."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ProcessStatus(False, None)
    except OSError:
        return ProcessStatus(True, None)
    return ProcessStatus(True, None)


def open_failure_status(error: int) -> ProcessStatus:
    """The state of a process Windows would not open a handle to, from the error OpenProcess set. Only an invalid
    parameter says no process has the PID; access denied names one this user may not query, and any other error, such
    as a quota or resource failure, leaves the process unread, so both report it alive with no start time."""
    return ProcessStatus(error != ERROR_INVALID_PARAMETER, None)


@functools.cache
def _kernel32() -> Any:
    """kernel32, loaded once, with every function the probe calls declared: a handle is pointer-sized, so it passes as
    a HANDLE, never as the C int ctypes would convert an undeclared argument to."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]  # Windows only
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def _windows_status(pid: int) -> ProcessStatus:
    """The process's state as Windows reports it, by a handle that may only query it."""
    import ctypes
    from ctypes import wintypes

    kernel32 = _kernel32()
    query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return open_failure_status(ctypes.get_last_error())  # type: ignore[attr-defined]  # Windows only
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return ProcessStatus(True, None)
        if exit_code.value != still_active:
            return ProcessStatus(False, None)
        creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return ProcessStatus(True, None)
        return ProcessStatus(True, (creation.dwHighDateTime << 32) | creation.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)

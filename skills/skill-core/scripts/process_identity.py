"""Whether a process is running and when it started: the one probe of a process's identity.

A process is identified by its PID and its start time. The deployment lock records both for the deployment that holds
it, and the code-review pipeline for the host it starts, so a PID that names a running process whose start time differs
from the one recorded is another program that reuses it. The deployer reaches the probe through
`platform_support.process_status`.

A probe that cannot read a process's state never reports it gone: a process whose state is unreadable is reported
alive with no start time, which its caller treats as a holder it cannot confirm, never as one it may reclaim.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Where Linux reports each process: /proc/<pid>/stat, whose fields after the command name, in parentheses, start with
# the state and hold the start time, in clock ticks since boot, as the twentieth (field 22 of the whole line).
PROC = Path("/proc")
STAT_START_TIME = 19
# The states of a process that has ended: a zombie waits only to be reaped by its parent, and a dead one is going.
ENDED_STATES = frozenset({b"Z", b"X", b"x"})


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
    close = text.rfind(b")")
    fields = text[close + 1 :].split() if close >= 0 else []
    if len(fields) <= STAT_START_TIME:
        return ProcessStatus(True, None)
    if fields[0] in ENDED_STATES:
        return ProcessStatus(False, None)
    try:
        return ProcessStatus(True, int(fields[STAT_START_TIME]))
    except ValueError:
        return ProcessStatus(True, None)


def _signalled_status(pid: int) -> ProcessStatus:
    """A process /proc does not show: gone only when the kernel says no process has its PID."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ProcessStatus(False, None)
    except OSError:
        return ProcessStatus(True, None)
    return ProcessStatus(True, None)


def _windows_status(pid: int) -> ProcessStatus:
    """The process's state as Windows reports it, by a handle that may only query it."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    query_limited_information = 0x1000
    access_denied = 5
    still_active = 259
    handle = kernel32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return ProcessStatus(ctypes.get_last_error() == access_denied, None)
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

"""Whether a process is running and when it started: the one probe of a process's identity.

A process is identified by its PID and its start time. The deployment lock records both for the deployment that holds
it, and the code-review pipeline for the host it starts, so a PID that names a running process whose start time differs
from the one recorded is another program that reuses it. The deployer reaches the probe through
`platform_support.process_status`.

A probe that cannot read a process's state never reports it gone: a process whose state is unreadable is reported
alive with no start time, which its caller treats as a holder it cannot confirm, never as one it may reclaim.

A PID read from a file is read through `read_pid`, which refuses one outside the platform's range, so no value is
truncated to another process's PID before it reaches the probe: Windows passes a PID as a 32-bit `DWORD`, Linux
allots none past `PID_MAX_LIMIT`, the most `/proc/sys/kernel/pid_max` may be set to, and macOS none past its
`PID_MAX`. The probe itself reports a PID outside that range as no process.
"""

from __future__ import annotations

import functools
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bounded_process import run_bounded
from typed_fields import FieldReader

# Where Linux reports each process: /proc/<pid>/stat, whose fields after the command name, in parentheses, start with
# the state and hold the start time, in clock ticks since boot, as the twentieth (field 22 of the whole line). It is
# ejryan89-skill-core's one reader of /proc: job_tree imports PROC, ENDED_STATES, and stat_fields from here.
PROC = Path("/proc")
STAT_START_TIME = 19
# The states of a process that has ended: a zombie waits only to be reaped by its parent, and a dead one is going.
ENDED_STATES = frozenset({b"Z", b"X", b"x"})
# The error OpenProcess sets for a PID that names no process.
ERROR_INVALID_PARAMETER = 87
# The largest PID each platform can give a process: OpenProcess takes a DWORD, Linux's PID_MAX_LIMIT on a 64-bit
# kernel, 4 * 1024 * 1024, bounds pid_max, every PID being below it, and macOS's kernel gives none at or past its
# PID_MAX, 99999.
WINDOWS_PID_MAXIMUM = 2**32 - 1
LINUX_PID_MAXIMUM = 4 * 1024 * 1024 - 1
MACOS_PID_MAXIMUM = 99_999
PID_MAXIMUM = {"win32": WINDOWS_PID_MAXIMUM, "darwin": MACOS_PID_MAXIMUM}.get(sys.platform, LINUX_PID_MAXIMUM)
# macOS has no /proc, so the probe asks ps for a process's state and start: by its own path, /bin/ps, so no program
# PATH names answers instead, with this environment, in the C locale, so the start is in English, and in UTC, so a
# process's start reads the same whatever time zone the user changes to. A start printed in local time would read as
# another process's after a change of zone, and the lock would take a running deployment's lock from it. job_tree
# lists a process group's members with the same ps, environment, and time limit.
PS_ENVIRONMENT = {"LC_ALL": "C", "TZ": "UTC0", "PATH": "/usr/bin:/bin"}
PS_SECONDS = 10.0
# The command ps is given: each field's header left empty, so it prints the state, then the start as `Tue Nov 10
# 20:00:01 2026`.
PS_FIELDS = ("-o", "stat=", "-o", "lstart=")
MONTHS = {
    name: number
    for number, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1
    )
}


@dataclass(frozen=True)
class ProcessStatus:
    alive: bool
    start_time: int | None


def read_pid(value: object, path: str, reader: FieldReader | None = None) -> int:
    """A PID read from a file: an integer from 1 to the platform's `PID_MAXIMUM`, or `reader`'s `FieldError`, naming
    `path`, for anything else, so a value past the range is refused rather than truncated to another process's PID."""
    return (reader or FieldReader()).integer(value, path, minimum=1, maximum=PID_MAXIMUM)


def process_status(pid: int) -> ProcessStatus:
    """Report whether a process is running and, when readable, its start time: its creation time on Windows, its start
    in seconds since the epoch on macOS, read from ps, and elsewhere its start in clock ticks since boot, read from
    /proc. A PID outside the platform's range names no process."""
    if not 0 < pid <= PID_MAXIMUM:
        return ProcessStatus(False, None)
    if sys.platform == "win32":
        return _windows_status(pid)
    if sys.platform == "darwin":
        return ps_status(pid)
    return proc_status(pid)


def ps_status(pid: int) -> ProcessStatus:
    """The process's state as ps reports it. A PID ps does not list is asked of the kernel with a signal of none, so
    only the kernel's word that no process has it reports one gone; a ps that cannot be run, does not finish, or prints
    what this cannot read reports the process running with no start time. A PID past macOS's range names no process."""
    if not 0 < pid <= MACOS_PID_MAXIMUM:
        return ProcessStatus(False, None)
    try:
        finished = run_bounded(
            ["/bin/ps", *PS_FIELDS, "-p", str(pid)], PS_SECONDS, env=PS_ENVIRONMENT, output_limit=4096
        )
    except (OSError, subprocess.TimeoutExpired):
        return ProcessStatus(True, None)
    text = finished.stdout.decode("ascii", "replace").strip()
    if not text:
        return _signalled_status(pid)
    return ps_line_status(text)


def ps_line_status(line: str) -> ProcessStatus:
    """A process's state from the line ps prints for it: the state, then the start. A process that has ended, which
    waits only to be reaped, is gone; a start this cannot read reports it running with no start time."""
    state, _, start = line.partition(" ")
    if state.startswith("Z"):
        return ProcessStatus(False, None)
    return ProcessStatus(True, ps_start_seconds(start))


def ps_start_seconds(text: str) -> int | None:
    """The seconds since the epoch of a start ps prints in UTC as `Tue Nov 10 20:00:01 2026`, or None for text of
    another shape."""
    fields = text.split()
    if len(fields) != 5 or fields[1] not in MONTHS:
        return None
    _, month, day, clock, year = fields
    parts = clock.split(":")
    if len(parts) != 3 or not all(number.isascii() and number.isdigit() for number in (day, year, *parts)):
        return None
    hour, minute, second = (int(part) for part in parts)
    try:
        moment = datetime(int(year), MONTHS[month], int(day), hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(moment.timestamp())


def proc_status(pid: int) -> ProcessStatus:
    """The process's state as /proc reports it. A PID /proc does not list is asked of the kernel with a signal of none,
    which tells a process that has gone from one /proc cannot show, such as another user's under `hidepid`. A PID past
    Linux's range names no process, and never reaches the signal, which refuses one past a C int."""
    if not 0 < pid <= LINUX_PID_MAXIMUM:
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
    if sys.platform != "win32":
        raise OSError("kernel32 is loaded only on Windows")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
    if sys.platform != "win32":
        raise OSError("a process is queried through kernel32 only on Windows")
    import ctypes
    from ctypes import wintypes

    kernel32 = _kernel32()
    query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return open_failure_status(ctypes.get_last_error())
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

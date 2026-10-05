"""Process identity and detached starts for the Copilot CLI host. Only Windows is implemented.

A process is identified by its PID and its start time, as the deployer's lock does in
deployer/platform_support.py; a deployed skill cannot import the deployer, so this is its own copy. Every
operating-system-specific step of the host's lifecycle lives here.
"""

from __future__ import annotations

import subprocess
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class ProcessStatus:
    alive: bool
    start_time: int | None


def process_status(pid: int) -> ProcessStatus:
    """Report whether a process is running and, when readable, its creation time."""
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


def same_process(status: ProcessStatus, recorded_start: int | None) -> bool | None:
    """Whether a probed PID is still the recorded process: None when its start time cannot settle it."""
    if not status.alive:
        return False
    if status.start_time is None or recorded_start is None:
        return None
    return status.start_time == recorded_start


def hidden_window() -> dict[str, int]:
    """Keyword arguments for subprocess that start a console program without a window of its own."""
    return {"creationflags": CREATE_NO_WINDOW}


def start_detached(arguments: Sequence[str], cwd: Path, log_path: Path) -> int:
    """Start a process that outlives the command that started it, with its output in a log file; return its PID.

    It gets a hidden console of its own, so neither a console's Ctrl+C nor the end of the shell call that ran
    dispatch reaches it, and the console programs it starts inherit that console instead of opening a window. It
    leaves the caller's job object when the job allows that, since a runtime may end a command's whole job.
    """
    with open(log_path, "ab") as log:
        def start(flags: int) -> subprocess.Popen[bytes]:
            return subprocess.Popen(
                list(arguments), cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                close_fds=True, creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | flags,
            )

        try:
            process = start(CREATE_BREAKAWAY_FROM_JOB)
        except PermissionError:
            # The caller's job forbids breakaway, so the process stays in it.
            process = start(0)
    pid = process.pid
    with warnings.catch_warnings():
        # Dropping the handle of a process still running warns; outliving this handle is the point.
        warnings.simplefilter("ignore", ResourceWarning)
        del process
    return pid

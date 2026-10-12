"""Process identity, detached starts, and process trees for the review's processes. Only Windows is implemented.

A process is identified by its PID and its start time, as the deployer's lock does in
deployer/platform_support.py; a deployed skill cannot import the deployer, so this is its own copy. Every
operating-system-specific step of the host's lifecycle lives here, and so does where WinGet installs Copilot CLI and
the job object that ends a condition script's or Copilot CLI's descendants with it.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import warnings
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from bounded_process import KILL_WAIT_SECONDS

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class ProcessStatus:
    alive: bool
    start_time: int | None


# Definitions that tests/run_validation.py allows to be copied in another file, with the reason.
DUPLICATION_ALLOWED = {
    "process_status": "a permanent copy of deployer/platform_support.py's: a deployed skill cannot import the "
    "deployer, and the deployer cannot import a skill; this policy keeps the two identical",
}


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


PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JobTree:
    """A command and every process it starts, held in one Windows job object: a `ProcessTree` for skill-core's
    `run_bounded`. Leaving `hold` ends every process of the job still running, and the job ends them too if this
    process dies first, since it kills its processes when its last handle closes.

    A process joins the job right after it starts, before it has loaded enough to start a program of its own, and
    every process it then starts joins with it. One that cannot join, as when the job cannot be made, it already sits
    in a job that forbids another, or it is already gone, is never run without one: `hold` ends it and raises the
    OSError naming the call that failed, as `run_bounded` raises one for a command that cannot start. When the job can
    neither end its processes nor be closed, leaving `hold` raises an OSError saying they may still run.
    """

    def __init__(self, *, hidden_window: bool = False) -> None:
        self.hidden_window = hidden_window

    def start_options(self) -> dict[str, Any]:
        return {"creationflags": CREATE_NO_WINDOW} if self.hidden_window else {}

    @contextmanager
    def hold(self, process: subprocess.Popen[bytes]) -> Iterator[None]:
        try:
            job = _job_holding(process.pid)
        except OSError as exc:
            process.kill()
            process.wait(timeout=KILL_WAIT_SECONDS)
            raise OSError(f"Cannot hold the command in a Windows job object, so it was ended: {exc}") from exc
        try:
            yield
        finally:
            _end_job(job)


def _kernel32() -> Any:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]  # Windows only
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def _failed(call: str) -> OSError:
    """The error of the kernel call `call` that just failed on this thread, naming it."""
    code = ctypes.get_last_error()
    return ctypes.WinError(code, f"{call} failed: {ctypes.FormatError(code)}")


def _job_holding(pid: int) -> int:
    """A new job that kills its processes when closed, holding the process `pid`. Raises the OSError of the first
    call that failed, with nothing left open; a process handle it could not close holds no process in the job.

    The caller's Popen still holds the process's handle, so its PID names no other process here.
    """
    kernel32 = _kernel32()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise _failed("CreateJobObjectW")
    try:
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            raise _failed("SetInformationJobObject")
        process = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            raise _failed("OpenProcess")
        try:
            if not kernel32.AssignProcessToJobObject(job, process):
                raise _failed("AssignProcessToJobObject")
        finally:
            kernel32.CloseHandle(process)
    except OSError:
        kernel32.CloseHandle(job)  # it holds no process, so closing it ends nothing and a failure leaks only it
        raise
    return int(job)


def _end_job(job: int) -> None:
    """End every process of the job still running, and close it. Either ends them, since the job kills its processes
    when its last handle closes, so this raises only when both fail and they may still run."""
    kernel32 = _kernel32()
    if kernel32.TerminateJobObject(job, 1):
        kernel32.CloseHandle(job)  # its processes are ended, so a failure leaks only the handle
        return
    error = _failed("TerminateJobObject")
    if not kernel32.CloseHandle(job):
        raise OSError(f"The job's processes may still run: {error}; {_failed('CloseHandle')}") from error


# The functions that start a command whose program tests/run_validation.py cannot read here, with the reason.
COMMANDS_ALLOWED = {
    "start_detached": "starts the command review_pipeline.py passes it as its launch service, this skill's own "
    "host script under sys.executable, which the policy reads there",
}


def start_detached(arguments: Sequence[str], cwd: Path, log_path: Path) -> int:
    """Start a process that outlives the command that started it, with its output in a log file; return its PID.

    It gets a hidden console of its own, so neither a console's Ctrl+C nor the end of the shell call that ran
    dispatch reaches it, and the console programs it starts inherit that console instead of opening a window. It
    leaves the caller's job object when the job allows that, since a runtime may end a command's whole job.
    """
    with log_path.open("ab") as log:

        def start(flags: int) -> subprocess.Popen[bytes]:
            return subprocess.Popen(
                list(arguments),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | flags,
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


# What Windows starts through a shell rather than as a program: cmd.exe runs a .bat or .cmd file and parses its whole
# command line by its own rules, which Python's argument quoting does not hold against, and PowerShell runs a .ps1.
SHELL_SCRIPT_SUFFIXES = frozenset({".bat", ".cmd", ".ps1"})


def shell_script(path: str) -> bool:
    """Whether Windows would start `path` through a shell, as it does npm's `copilot.cmd`, rather than as a program."""
    return Path(path).suffix.casefold() in SHELL_SCRIPT_SUFFIXES


def path_programs(name: str) -> list[str]:
    """Every file PATH holds for the command `name`, in the order `shutil.which` would look: PATH's folders in turn,
    each folder once, and within one, `name` itself when it already ends in a PATHEXT extension, then `name` with each
    extension in PATHEXT's order. `shutil.which` stops at the first, so a lookup that passes over one, as a shell
    script is passed over, takes the next from here. The current directory, which `shutil.which` searches first on
    Windows, is never searched, because PATH does not name it."""
    extensions = [extension for extension in os.environ.get("PATHEXT", "").split(os.pathsep) if extension]
    names = [name + extension for extension in extensions] or [name]
    if any(name.casefold().endswith(extension.casefold()) for extension in extensions):
        names.insert(0, name)
    found: list[str] = []
    seen: set[str] = set()
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if not folder or os.path.normcase(folder) in seen:
            continue
        seen.add(os.path.normcase(folder))
        for candidate in (Path(folder) / file for file in names):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                found.append(str(candidate))
    return found


def winget_copilot() -> Path | None:
    """Copilot CLI in the folder WinGet links the commands it installs into, which PATH may not include; None when it
    is not there."""
    candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "copilot.exe"
    return candidate if candidate.is_file() else None

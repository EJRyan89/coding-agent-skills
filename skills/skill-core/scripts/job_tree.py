"""A command and every process it starts, held in one Windows job object: a `ProcessTree` for `run_bounded`.

Leaving `JobTree.hold` ends every process of the job still running, and the job ends them too if this process dies
first, since it kills its processes when its last handle closes. A process joins the job right after it starts, before
it has loaded enough to start a program of its own, and every process it then starts joins with it. Only Windows is
implemented.

A command still running that cannot join is killed and refused with `JobRefused`, never run without the job: its
caller relies on ending everything it starts. One already gone when it would join has nothing left to hold.

Leaving `hold` returns only once every process of the job has ended and Windows has let go of what it held, its
working directory and open files among them, so a caller may remove the folder the command ran in at once. Ending a
job is asynchronous, so `hold` waits for each process, up to KILL_WAIT_SECONDS; a job whose processes it cannot list,
end, or see end in that time leaves `hold` with an OSError saying they may still run.
"""

from __future__ import annotations

import ctypes
import functools
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
ERROR_MORE_DATA = 234
CREATE_NO_WINDOW = 0x08000000
# Seconds to wait for a refused command, once killed, to be reported gone, and for an ended job's processes to end.
KILL_WAIT_SECONDS = 10.0
# Processes a job's first listing has room for; a longer listing is asked for again with room for all.
LISTED_PROCESSES = 64


class JobRefused(OSError):
    """A running command could not join its job, so it was killed; the message says why it could not."""


class _BasicLimitInformation(ctypes.Structure):
    """JOBOBJECT_BASIC_LIMIT_INFORMATION."""

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


class _ExtendedLimitInformation(ctypes.Structure):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION."""

    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JobTree:
    """The process tree of one command: see the module's description. With `hidden_window`, a console command starts
    without a window of its own."""

    hidden_window = False

    def __init__(self, *, hidden_window: bool = False) -> None:
        self.hidden_window = hidden_window

    def start_options(self) -> dict[str, Any]:
        return {"creationflags": CREATE_NO_WINDOW} if self.hidden_window else {}

    @contextmanager
    def hold(self, process: subprocess.Popen[Any]) -> Iterator[None]:
        try:
            job: int | None = job_holding(process.pid)
        except OSError as exc:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=KILL_WAIT_SECONDS)
                raise JobRefused(f"it could not join a Windows job object: {exc}") from exc
            job = None
        try:
            yield
        finally:
            if job is not None:
                _end_job(job)


@functools.cache
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
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
    ]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def _failed(call: str) -> OSError:
    error = ctypes.get_last_error()  # type: ignore[attr-defined]  # Windows only
    reason = ctypes.FormatError(error).strip()  # type: ignore[attr-defined]  # Windows only
    return OSError(f"{call} failed: {reason} (Windows error {error})")


def _process_ids(job: int) -> list[int]:
    """The IDs of the processes the job holds now."""
    kernel32 = _kernel32()
    room = LISTED_PROCESSES
    while True:

        class Listing(ctypes.Structure):
            """JOBOBJECT_BASIC_PROCESS_ID_LIST with room for `room` IDs."""

            _fields_ = [
                ("NumberOfAssignedProcesses", ctypes.c_uint32),
                ("NumberOfProcessIdsInList", ctypes.c_uint32),
                ("ProcessIdList", ctypes.c_size_t * room),
            ]

        listing = Listing()
        if kernel32.QueryInformationJobObject(
            job, JOB_OBJECT_BASIC_PROCESS_ID_LIST, ctypes.byref(listing), ctypes.sizeof(listing), None
        ):
            return [int(pid) for pid in listing.ProcessIdList[: listing.NumberOfProcessIdsInList]]
        if ctypes.get_last_error() != ERROR_MORE_DATA:  # type: ignore[attr-defined]  # Windows only
            raise _failed("QueryInformationJobObject")
        room = max(room * 2, listing.NumberOfAssignedProcesses)


def _watch(job: int, watched: dict[int, int]) -> OSError | None:
    """Open a handle to wait on each process the job lists that is not yet in `watched`, or the listing's error; a
    process already gone needs none."""
    kernel32 = _kernel32()
    try:
        pids = _process_ids(job)
    except OSError as error:
        return error
    for pid in pids:
        if pid not in watched:
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle:
                watched[pid] = handle
    return None


def _until_ended(watched: dict[int, int]) -> None:
    """Wait, up to KILL_WAIT_SECONDS in all, for each watched process to end."""
    kernel32 = _kernel32()
    deadline = time.monotonic() + KILL_WAIT_SECONDS
    for pid, handle in watched.items():
        remaining = max(0, int((deadline - time.monotonic()) * 1000))
        if kernel32.WaitForSingleObject(handle, remaining) != WAIT_OBJECT_0:
            raise OSError(
                f"The job's processes may still run: process {pid} had not ended {KILL_WAIT_SECONDS:g} seconds on"
            )


def _end_job(job: int) -> None:
    """End every process of the job, close it, and wait until each has ended. Ending them through the job or closing
    it either ends them, since the job kills its processes when its last handle closes, so this raises only when
    both fail, or when the job cannot be listed or its processes have not ended within KILL_WAIT_SECONDS.

    Each process is watched from before the job ends it, while its ID still names it, and so is any the job still
    lists once ended, which one started meanwhile would be. Windows signals a process once it has let go of everything.
    """
    kernel32 = _kernel32()
    watched: dict[int, int] = {}
    try:
        problem = _watch(job, watched)
        if kernel32.TerminateJobObject(job, 1):
            problem = problem or _watch(job, watched)
            kernel32.CloseHandle(job)  # its processes are ended, so a failure leaks only the handle
        else:
            error = _failed("TerminateJobObject")
            if not kernel32.CloseHandle(job):
                raise OSError(f"The job's processes may still run: {error}; {_failed('CloseHandle')}") from error
        if problem is not None:
            raise OSError(f"The job's processes may still run: {problem}") from problem
        _until_ended(watched)
    finally:
        for handle in watched.values():
            kernel32.CloseHandle(handle)


def job_holding(pid: int) -> int:
    """A new job that kills its processes when closed, holding the process `pid`; raises OSError when it cannot.

    The caller's Popen still holds the process's handle, so its PID names no other process here.
    """
    kernel32 = _kernel32()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise _failed("CreateJobObjectW")
    process = None
    try:
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            raise _failed("SetInformationJobObject")
        process = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            raise _failed("OpenProcess")
        if not kernel32.AssignProcessToJobObject(job, process):
            raise _failed("AssignProcessToJobObject")
    except OSError:
        kernel32.CloseHandle(job)
        raise
    finally:
        if process:
            kernel32.CloseHandle(process)
    return int(job)

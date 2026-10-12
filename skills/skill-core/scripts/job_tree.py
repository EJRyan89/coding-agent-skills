"""A command and every process it starts, held in one Windows job object: a `ProcessTree` for `run_bounded`.

Leaving `JobTree.hold` ends every process of the job still running, and the job ends them too if this process dies
first, since it kills its processes when its last handle closes. A process joins the job right after it starts, before
it has loaded enough to start a program of its own, and every process it then starts joins with it. Only Windows is
implemented.

A command still running that cannot join is killed and refused with `JobRefused`, never run without the job: its
caller relies on ending everything it starts. One already gone when it would join has nothing left to hold.
"""

from __future__ import annotations

import ctypes
import functools
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
# Seconds to wait for a refused command, once killed, to be reported gone.
KILL_WAIT_SECONDS = 10.0


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
    """The process tree of one command: see the module's description."""

    def start_options(self) -> dict[str, Any]:
        return {}

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
                # Ends every process of the job still running, then closes it.
                _kernel32().TerminateJobObject(job, 1)
                _kernel32().CloseHandle(job)


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
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def _failed(call: str) -> OSError:
    error = ctypes.get_last_error()  # type: ignore[attr-defined]  # Windows only
    return OSError(f"{call} failed: {ctypes.FormatError(error).strip()} (Windows error {error})")  # type: ignore[attr-defined]  # Windows only


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

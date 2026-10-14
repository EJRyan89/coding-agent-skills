"""A command and every process it starts, held together so that ending the tree ends them all: a `ProcessTree` for
`run_bounded`. `process_tree` gives this platform's: a Windows job object, `JobTree`, or elsewhere a process group of a
session of its own, `GroupTree`.

Leaving `JobTree.hold` ends every process of the job still running, and the job ends them too if this process dies
first, since it kills its processes when its last handle closes. A command starts suspended, joins the job, and only
then runs, so every process it starts joins with it from the first: none can start outside the job before it joins.

A command still running that cannot join, or that cannot be let run once it has, is killed and refused with
`JobRefused`, never run without the job: its caller relies on ending everything it starts. One already gone when it
would join has nothing left to hold.

Leaving `hold` returns only once every process of the job has ended and Windows has let go of what it held, its
working directory and open files among them, so a caller may remove the folder the command ran in at once. Ending a
job is asynchronous, so `hold` waits for each process, up to KILL_WAIT_SECONDS; a job whose processes it cannot list,
end, or see end in that time leaves `hold` with an OSError saying they may still run.

A `GroupTree` starts the command as the leader of a new session, so the command and every process it starts that does
not leave the group share one process group, which the command cannot fail to join: it leads the group before it
runs. Leaving `hold` kills the group and returns once no process of it is running but the ones that have ended and
wait only to be reaped, up to KILL_WAIT_SECONDS, or with an OSError saying they may still run. Unlike a job, the group
does not outlive this process: a process killed outright leaves its group running.
"""

from __future__ import annotations

import ctypes
import functools
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
ERROR_MORE_DATA = 234
ERROR_NO_MORE_FILES = 18
CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002
TH32CS_SNAPTHREAD = 0x00000004
# What ResumeThread returns when it fails, (DWORD)-1, in place of the thread's previous suspend count.
RESUME_FAILED = 0xFFFFFFFF
# What CreateToolhelp32Snapshot returns when it fails, as ctypes reads a handle.
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
# Seconds to wait for a refused command, once killed, to be reported gone, and for an ended job's processes to end.
KILL_WAIT_SECONDS = 10.0
# Processes a job's first listing has room for; a longer listing is asked for again with room for all.
LISTED_PROCESSES = 64
# Seconds between two looks at whether a killed process group still has a process running.
GROUP_POLL_SECONDS = 0.05
# Where Linux reports each process, and the states, in its stat file, of one that has ended and waits to be reaped.
PROC = "/proc"
ENDED_STATES = frozenset({b"Z", b"X", b"x"})


class JobRefused(OSError):
    """A command could not be held in its job: it could not join it, or, on Windows, it joined and could not be
    resumed in it, so it was killed; the message says which."""


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


class _ThreadEntry(ctypes.Structure):
    """THREADENTRY32."""

    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_int32),
        ("tpDeltaPri", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
    ]


class JobTree:
    """The process tree of one command: see the module's description. With `hidden_window`, a console command starts
    without a window of its own."""

    hidden_window = False

    def __init__(self, *, hidden_window: bool = False) -> None:
        self.hidden_window = hidden_window

    def start_options(self) -> dict[str, Any]:
        return {"creationflags": CREATE_SUSPENDED | (CREATE_NO_WINDOW if self.hidden_window else 0)}

    @contextmanager
    def hold(self, process: subprocess.Popen[Any]) -> Iterator[None]:
        try:
            job: int | None = job_holding(process.pid)
        except OSError as exc:
            if process.poll() is None:
                _kill(process)
                raise JobRefused(f"it could not join a Windows job object: {exc}") from exc
            job = None
        if job is not None:
            try:
                resume(process.pid)
            except OSError as exc:
                if process.poll() is None:
                    _kill(process)
                    _end_job(job)
                    raise JobRefused(f"it could not be resumed in its Windows job object: {exc}") from exc
        try:
            yield
        finally:
            if job is not None:
                _end_job(job)


def _kill(process: subprocess.Popen[Any]) -> None:
    """Kill a command that may not run, and wait for it to be reported gone."""
    process.kill()
    process.wait(timeout=KILL_WAIT_SECONDS)


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
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
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


def resume(pid: int) -> None:
    """Let the process `pid`, started suspended, run: resume each of its threads, of which a process started suspended
    has only its first. Raises OSError when a thread cannot be resumed or the process has none."""
    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        raise _failed("CreateToolhelp32Snapshot")
    resumed = 0
    try:
        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        call = "Thread32First"
        listed = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while listed:
            if entry.th32OwnerProcessID == pid:
                _resume_thread(entry.th32ThreadID)
                resumed += 1
            call = "Thread32Next"
            listed = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != ERROR_NO_MORE_FILES:  # type: ignore[attr-defined]  # Windows only
            raise _failed(call)
    finally:
        kernel32.CloseHandle(snapshot)
    if not resumed:
        raise OSError(f"no thread of process {pid} was listed to resume")


def _resume_thread(thread_id: int) -> None:
    kernel32 = _kernel32()
    thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, thread_id)
    if not thread:
        raise _failed("OpenThread")
    try:
        if kernel32.ResumeThread(thread) == RESUME_FAILED:
            raise _failed("ResumeThread")
    finally:
        kernel32.CloseHandle(thread)


class GroupTree:
    """The process tree of one command where there are no job objects: see the module's description. `hidden_window`
    is taken for the surface `JobTree` has and changes nothing, since a program here opens no window by starting."""

    hidden_window = False

    def __init__(self, *, hidden_window: bool = False) -> None:
        self.hidden_window = hidden_window

    def start_options(self) -> dict[str, Any]:
        return {"start_new_session": True}

    @contextmanager
    def hold(self, process: subprocess.Popen[Any]) -> Iterator[None]:
        # The command leads its own session, so its process group's ID is its PID, which names no other process while
        # its caller's Popen has not reaped it, nor any other group while one of the group's processes runs.
        try:
            leads = os.getpgid(process.pid) == process.pid  # type: ignore[attr-defined]  # Unix only
        except ProcessLookupError:
            leads = True  # already reaped, so nothing of it is left to hold but what end_group finds
        if not leads and process.poll() is None:
            process.kill()
            process.wait(timeout=KILL_WAIT_SECONDS)
            raise JobRefused("it was not started as the leader of a process group of its own")
        try:
            yield
        finally:
            end_group(process.pid)


def end_group(group: int) -> None:
    """Kill every process of the process group `group`, and return once none is running; raises OSError when they
    cannot be killed or have not ended within KILL_WAIT_SECONDS."""
    try:
        os.killpg(group, signal.SIGKILL)  # type: ignore[attr-defined]  # Unix only
    except ProcessLookupError:
        return  # no process of the group is left
    except OSError as error:
        raise OSError(f"The group's processes may still run: killpg failed: {error}") from error
    deadline = time.monotonic() + KILL_WAIT_SECONDS
    while running := group_members(group):
        if time.monotonic() >= deadline:
            raise OSError(
                f"The group's processes may still run: process {running[0]} had not ended {KILL_WAIT_SECONDS:g} "
                "seconds on"
            )
        time.sleep(GROUP_POLL_SECONDS)


def group_members(group: int) -> list[int]:
    """The IDs of the processes of `group` still running, as /proc lists them: a process that has ended and waits only
    to be reaped holds nothing and runs nothing, so it is not one. Without /proc, the group itself is asked with a
    signal of none, which counts such a process as running."""
    try:
        entries = [entry.name for entry in Path(PROC).iterdir()]
    except OSError:
        try:
            os.killpg(group, 0)  # type: ignore[attr-defined]  # Unix only
        except ProcessLookupError:
            return []
        return [group]
    running: list[int] = []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            text = (Path(PROC) / entry / "stat").read_bytes()
        except OSError:
            continue  # it ended meanwhile
        # After the command name, in parentheses, come the state, the parent's ID, and the process group's ID.
        fields = text[text.rfind(b")") + 1 :].split()
        if len(fields) > 2 and fields[0] not in ENDED_STATES and fields[2] == str(group).encode():
            running.append(int(entry))
    return running


def process_tree(*, hidden_window: bool = False) -> JobTree | GroupTree:
    """A tree for one command and every process it starts, as this platform holds one: a job object on Windows, and a
    process group elsewhere."""
    if sys.platform == "win32":
        return JobTree(hidden_window=hidden_window)
    return GroupTree(hidden_window=hidden_window)

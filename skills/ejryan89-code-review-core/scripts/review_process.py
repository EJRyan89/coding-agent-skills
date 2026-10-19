"""Process identity and detached starts for the review's processes, on Windows and elsewhere.

A process is identified by its PID and its start time, which ejryan89-skill-core's `process_identity` reads, for the
review's processes and the deployer's lock alike. Every other operating-system-specific step of the host's lifecycle
lives here, except where Copilot CLI is found, which is ejryan89-skill-core's `executable_locator`, and the process tree
that ends a condition script's or Copilot CLI's descendants with it, which is ejryan89-skill-core's
`job_tree.process_tree`: a Windows job object, or elsewhere a process group.
"""

from __future__ import annotations

import os
import subprocess
import sys
import warnings
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from bounded_process import without_repository_variables
from process_identity import ProcessStatus

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000


def same_process(status: ProcessStatus, recorded_start: int | None) -> bool | None:
    """Whether a probed PID is still the recorded process: None when its start time cannot settle it."""
    if not status.alive:
        return False
    if status.start_time is None or recorded_start is None:
        return None
    return status.start_time == recorded_start


# The functions that start a command whose program tests/run_validation.py cannot read here, with the reason.
COMMANDS_ALLOWED = {
    "start_detached": "starts the command review_pipeline.py passes it as its launch service, this skill's own "
    "host script under sys.executable, which the policy reads there",
}


def start_detached(arguments: Sequence[str], cwd: Path, log_path: Path) -> int:
    """Start a process that outlives the command that started it, with its output in a log file; return its PID.

    On Windows it gets a hidden console of its own, so neither a console's Ctrl+C nor the end of the shell call that
    ran dispatch reaches it, and the console programs it starts inherit that console instead of opening a window. It
    leaves the caller's job object when the job allows that, since a runtime may end a command's whole job. Elsewhere
    it leads a session of its own, so neither the terminal's signals nor the end of the caller's process group reach
    it. Either way its environment is this process's without git's repository-selecting variables, so neither it nor
    a program it starts reads the repository a shell's `GIT_DIR` names.
    """
    if sys.platform != "win32":
        return _start_session(arguments, cwd, log_path)
    with log_path.open("ab") as log:

        def start(flags: int) -> subprocess.Popen[bytes]:
            return subprocess.Popen(
                list(arguments),
                cwd=cwd,
                env=without_repository_variables(os.environ),
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


def _start_session(arguments: Sequence[str], cwd: Path, log_path: Path) -> int:
    """start_detached off Windows: the process leads a new session, with no terminal, and its output in the log."""
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            list(arguments),
            cwd=cwd,
            env=without_repository_variables(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )
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
    """Whether Windows would start `path` through a shell, as it does npm's `copilot.cmd`, rather than as a program.
    Elsewhere none is: the kernel starts a script by its `#!` line and hands it the arguments as they are, so no shell
    parses its command line."""
    return sys.platform == "win32" and Path(path).suffix.casefold() in SHELL_SCRIPT_SUFFIXES

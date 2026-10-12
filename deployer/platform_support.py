"""Operating-system specific behavior. Only Windows is implemented."""

from __future__ import annotations

import errno
import os
import re
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import executable_locator
from bounded_process import run_bounded
from job_tree import JobRefused, JobTree

from .errors import DeployError, see_recovery

WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:/")
WINDOWS_ROOT = re.compile(r"^[A-Za-z]:/$")
GIT_BASH_DRIVE = re.compile(r"^/([A-Za-z])(?:/(.*))?$", re.DOTALL)
DRIVE_PATH = re.compile(r"^([A-Za-z]):(.*)$", re.DOTALL)
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
CREATE_NO_WINDOW = 0x08000000
# The longest command line CreateProcess accepts, in characters. A tool given one path per file batches below it.
COMMAND_LINE_LIMIT = 32_767
# Seconds a tool the deployer runs, such as Git Bash, ShellCheck, PowerShell, or git, may take before it is ended with
# every process it started. Minutes, not seconds: ShellCheck and PowerShell over a large selection on a loaded machine
# take far longer than on an idle one and must still finish. The limit exists because a deployment holds its lock
# while it validates, so a tool that hangs would otherwise hold the lock, and stall every later run, until killed.
TOOL_DEADLINE_SECONDS = 600
# The recovery guide's section a refusal for a tool that ran out of time points at.
TOOL_DEADLINE_SECTION = "A tool that does not finish"
INSTALL_HINTS = {
    "Git Bash": "winget install --id Git.Git",
    "ShellCheck": "winget install --id koalaman.shellcheck",
    "PowerShell": "winget install --id Microsoft.PowerShell",
    # A validation-only dependency, installed into the interpreter that runs validation, which is python here.
    "ruff": "python -m pip install -r requirements-dev.txt",
    "mypy": "python -m pip install -r requirements-dev.txt",
    # Validation-only too. -Force upgrades an older install and answers the untrusted-PSGallery prompt.
    "PSScriptAnalyzer": "pwsh -Command 'Install-Module PSScriptAnalyzer -Scope CurrentUser -Force'",
}
INSTALL_HELP = (
    'For Chocolatey, Scoop, or direct downloads, see "Installing the tools" in',
    "docs/installation.md. To use Git Bash from another location, set GIT_BASH",
    "to its bash.exe.",
    "After installing, open a new terminal. Applications that were already running,",
    "including ones minimized to the system tray, keep the old PATH until restarted.",
)
# Commands this platform provides beyond the portable ones in tools.STANDARD_COMMANDS: Python as python, because
# python3 is often a Microsoft Store placeholder; Windows PowerShell; and cygpath, which Git for Windows bundles.
STANDARD_COMMANDS = frozenset({"python", "powershell", "cygpath"})


def ensure_supported() -> None:
    if sys.platform != "win32":
        raise DeployError(f"ERROR: The deployer currently supports Windows only (detected platform: {sys.platform}).")


def normalize(path: str | os.PathLike[str]) -> str:
    """Render a path with forward slashes, the form used in config values and output."""
    return os.fspath(path).replace("\\", "/")


def normalize_path_input(value: str) -> str:
    """Accept a path as typed or pasted on Windows: backslashes, surrounding quotes, trailing separators."""
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
        stripped = stripped[1:-1]
    normalized = normalize(stripped)
    # Keep a drive root such as C:/ intact so it is still rejected as a filesystem root.
    while len(normalized) > 3 and normalized.endswith("/"):
        normalized = normalized[:-1]
    return normalized


def from_shell_path(value: str) -> str:
    """Translate a Git Bash drive path such as /c/Tools into C:/Tools; leave every other path unchanged."""
    match = GIT_BASH_DRIVE.match(value)
    return f"{match.group(1).upper()}:/{match.group(2) or ''}" if match else value


def to_shell_path(value: str) -> str:
    """Translate a drive-letter path such as C:/Tools into Git Bash's /c/Tools; leave every other path unchanged."""
    normalized = normalize(value)
    match = DRIVE_PATH.match(normalized)
    return f"/{match.group(1).lower()}{match.group(2)}" if match else normalized


# Where the runtimes read personal skills from, as named in the warning about an ignored HOME.
RUNTIME_HOME = "the Windows profile folder"


def home_directory(environment: Mapping[str, str] | None = None) -> Path:
    """The home the runtimes read skills from: on Windows the profile folder, whatever HOME says.

    Claude Code, Codex, and Copilot CLI all read the profile folder on Windows, while Git Bash may set HOME to
    another directory and MSYS2 does by default. Without USERPROFILE, Path.home() follows HOME on other systems.
    """
    environment = os.environ if environment is None else environment
    profile = environment.get("USERPROFILE")
    return Path(profile) if profile else Path.home()


def ignored_home_variable(home: Path, environment: Mapping[str, str] | None = None) -> str | None:
    """HOME as a drive-letter path, when this run's home is the profile folder and HOME names another directory.

    A home chosen explicitly, such as a --canary-home directory, is never compared with HOME.
    """
    environment = os.environ if environment is None else environment
    value = environment.get("HOME")
    if not value or not same_directory(home, home_directory(environment)):
        return None
    named = from_shell_path(normalize(value))
    return None if same_directory(Path(named), home) else normalize(os.path.normpath(named))


def same_directory(first: Path, second: Path) -> bool:
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def name_key(name: str) -> str:
    """The form in which the file system compares a name: two item names with one key are one file or directory.

    NTFS compares names without regard to case, so Windows folds case. A case-sensitive file system returns the name
    unchanged.
    """
    return name.casefold()


# The extension Windows finds a program by when its caller leaves it off, so git and git.exe start one program.
PROGRAM_EXTENSION = ".exe"


def program_name(name: str) -> str:
    """The name a program is called by, without the extension Windows lets its caller add, in any case: git for
    git.exe and for git.EXE. Any other name is returned unchanged."""
    if name.casefold().endswith(PROGRAM_EXTENSION):
        return name[: -len(PROGRAM_EXTENSION)]
    return name


def is_absolute(value: str) -> bool:
    return bool(WINDOWS_ABSOLUTE.match(value))


def is_filesystem_root(value: str) -> bool:
    return bool(WINDOWS_ROOT.fullmatch(value))


def canonical_directory(path: str | os.PathLike[str]) -> str:
    return normalize(os.path.realpath(path))


# Windows' rename refuses an existing destination; a POSIX rename replaces a file or an empty directory.
RENAME_REPLACES = sys.platform != "win32"


def refuse_existing_destination(destination: Path) -> None:
    """Raise FileExistsError for an existing destination where a rename onto it would replace it.

    Windows' rename refuses an existing destination itself, atomically, so nothing is checked there. Elsewhere any
    existing destination, a broken link included, is refused before the rename, which holds against everything but a
    destination created between the check and the rename.
    """
    if RENAME_REPLACES and os.path.lexists(destination):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), os.fspath(destination))


def is_link(path: Path) -> bool:
    return path.is_symlink()


def is_reparse_point(path: Path) -> bool:
    try:
        attributes = os.lstat(path).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def find_executable(name: str) -> str | None:
    """The program `name` runs, by skill-core's locator, so a tool `check` finds is the one a skill's script finds."""
    return executable_locator.find_executable(name)


def find_executables(name: str) -> list[str]:
    """Every program `name` may run, in skill-core's locator's search order, for a tool whose probe passes over one
    it does not support, as the skills' own lookup does."""
    return executable_locator.find_executables(name)


def find_bash() -> str | None:
    """Find Git for Windows' Bash; never another bash on PATH, such as WSL's."""
    configured = os.environ.get("GIT_BASH")
    if configured and Path(configured).is_file():
        return configured
    candidate = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
    if candidate.is_file():
        return str(candidate)
    return _bash_beside_git()


def _bash_beside_git() -> str | None:
    """Locate the Bash shipped with the git on PATH, as Scoop and per-user Git installs place it."""
    git = shutil.which("git")
    if git is None:
        return None
    result = run_tool([git, "--exec-path"])
    if result.returncode != 0:
        return None
    # Git for Windows reports <root>/mingw64/libexec/git-core; its Bash is <root>/bin/bash.exe.
    parents = Path(result.output.strip()).parents
    if len(parents) < 3:
        return None
    candidate = parents[2] / "bin" / "bash.exe"
    return str(candidate) if candidate.is_file() else None


def find_powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def find_pwsh(search_path: str) -> str | None:
    """Find PowerShell 7 on exactly this PATH value; never Windows PowerShell, and never the caller's own PATH."""
    return shutil.which("pwsh", path=search_path) if search_path else None


class HiddenWindow(TypedDict):
    """The subprocess.run keyword hidden_window gives, typed so that `**hidden_window()` checks as that keyword."""

    creationflags: int


def hidden_window() -> HiddenWindow:
    """Keyword arguments for subprocess.run that start a console program without a window of its own.

    A hook runs without a console, so a console program it starts would otherwise flash a window.
    """
    return {"creationflags": CREATE_NO_WINDOW}


def install_hint(tool: str) -> str:
    return INSTALL_HINTS[tool]


# Windows reports a process that an exception or another process ended, rather than the program itself, by an NTSTATUS
# whose severity bits put it at 0x80000000 or above, read unsigned as Python reads every exit code here. A program's
# own exit code stays below it, as Bash's, which is at most 255, does. Git Bash ended with 0x80000004 on the hosted
# runners before it printed anything.
TERMINATION_STATUS_FLOOR = 0x80000000


def termination_status(returncode: int) -> str | None:
    """The status, in hexadecimal, of a process ended by an exception or by another process rather than by the
    program, or None for a program's own exit code."""
    return f"0x{returncode:08X}" if returncode >= TERMINATION_STATUS_FLOOR else None


@dataclass(frozen=True)
class ToolResult:
    returncode: int
    output: str


def command_line_length(arguments: list[str]) -> int:
    """The characters these arguments take on a command line, quoting included, as run_tool passes them."""
    import subprocess

    return len(subprocess.list2cmdline(arguments))


def process_tree() -> JobTree:
    """A tree for one command and every process it starts, which ends them all when it is left: a job object."""
    return JobTree()


class ToolTimedOut(DeployError):
    """A tool did not finish within its deadline, so it was ended with every process it started."""

    def __init__(self, program: str, seconds: float) -> None:
        name = Path(program).name
        super().__init__(
            f"ERROR: {name} did not finish within {seconds:g} seconds, the deadline TOOL_DEADLINE_SECONDS in "
            "deployer/platform_support.py sets, so it was ended with every process it started.",
            f"The tool was {normalize(program)}. Run it by itself to see whether it finishes, then retry.",
            see_recovery(TOOL_DEADLINE_SECTION),
        )
        self.program = program
        self.seconds = seconds


def run_tool(
    arguments: list[str], environment: dict[str, str] | None = None, *, timeout: float | None = None
) -> ToolResult:
    """Run a tool with stdout and stderr captured through a file, never inherited pipes, within a deadline.

    The tool and every process it starts are held in one process tree, which ends them all once it has finished.
    Raises ToolTimedOut when it has not finished within `timeout` seconds, TOOL_DEADLINE_SECONDS unless given, and
    DeployError when it cannot be held with the processes it starts.
    """
    import subprocess
    import tempfile

    seconds = TOOL_DEADLINE_SECONDS if timeout is None else timeout
    merged = dict(os.environ)
    if environment:
        merged.update(environment)
    # MSYS tools can reject inherited anonymous pipes with a spurious text/binary mode error.
    with tempfile.TemporaryFile() as captured:
        try:
            finished = run_bounded(
                arguments, seconds, stdout=captured, stderr=captured, env=merged, tree=process_tree()
            )
        except subprocess.TimeoutExpired:
            raise ToolTimedOut(arguments[0], seconds) from None
        except JobRefused as exc:
            raise DeployError(
                f"ERROR: {Path(arguments[0]).name} could not be run with an end to every process it starts: {exc}."
            ) from exc
        captured.seek(0)
        return ToolResult(finished.returncode, captured.read().decode("utf-8", "replace"))


@dataclass(frozen=True)
class ProcessStatus:
    alive: bool
    start_time: int | None


def current_process_id() -> int:
    return os.getpid()


# Filesystem writes that tests/run_validation.py allows outside deployer/fsops.py, with the reason.
FSOPS_ALLOWED = {
    "tempfile.TemporaryFile": "run_tool captures a tool's output in an unnamed file under the system temporary "
    "directory, outside every managed root, which closing it deletes; it changes no deployed state",
}
# Definitions that tests/run_validation.py allows to be copied in another file, with the reason.
DUPLICATION_ALLOWED = {
    "process_status": "skills/code-review-core/scripts/review_process.py carries a permanent copy: a deployed skill "
    "cannot import the deployer, and the deployer cannot import a skill; this policy keeps the two identical",
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

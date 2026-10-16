"""Operating-system specific behavior: Windows, and Linux in the deployer core."""

from __future__ import annotations

import errno
import os
import re
import signal
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import executable_locator
import job_tree
import process_identity
from bounded_process import ProcessTree, run_bounded, without_repository_variables
from job_tree import JobRefused

from .errors import DeployError, see_recovery

# The platforms the deployer runs on, by sys.platform. macOS is not yet one of them.
SUPPORTED_PLATFORMS = {"win32": "Windows", "linux": "Linux"}
WINDOWS = sys.platform == "win32"
# Whether a process's environment matches a variable's name in any case, as Windows does, so git there takes git_dir
# as GIT_DIR; elsewhere each spelling is a variable of its own and git reads only the upper-case one.
ENVIRONMENT_NAMES_FOLD_CASE = WINDOWS
# This platform's name as a person knows it, which a report names it by.
PLATFORM_NAME = SUPPORTED_PLATFORMS.get(sys.platform, sys.platform)
WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:/")
WINDOWS_ROOT = re.compile(r"^[A-Za-z]:/$")
# A POSIX absolute path: one leading slash, since POSIX leaves the meaning of a path that starts with two to the system.
POSIX_ABSOLUTE = re.compile(r"^/(?!/)")
POSIX_ROOT = re.compile(r"^/$")
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
# What the Bash that checks rendered scripts is called here: Git for Windows' on Windows, and the system's elsewhere.
BASH_NAME = "Git Bash" if WINDOWS else "Bash"
# Why no Bash was found, as the validation runner says it.
BASH_MISSING = (
    "Git Bash was not found. Install Git for Windows or set GIT_BASH;" if WINDOWS else "No bash was found on PATH."
)
WINDOWS_INSTALL_HINTS = {
    "Git Bash": "winget install --id Git.Git",
    "ShellCheck": "winget install --id koalaman.shellcheck",
    "PowerShell": "winget install --id Microsoft.PowerShell",
    # A validation-only dependency, installed into the interpreter that runs validation, which is python here.
    "ruff": "python -m pip install -r requirements-dev.txt",
    "mypy": "python -m pip install -r requirements-dev.txt",
    # Validation-only too. -Force upgrades an older install and answers the untrusted-PSGallery prompt.
    "PSScriptAnalyzer": "pwsh -Command 'Install-Module PSScriptAnalyzer -Scope CurrentUser -Force'",
}
# Debian and Ubuntu's commands; PowerShell is not in their own archives, so its hint is the snap Microsoft publishes.
LINUX_INSTALL_HINTS = {
    "Bash": "sudo apt install bash",
    "ShellCheck": "sudo apt install shellcheck",
    "PowerShell": "sudo snap install powershell --classic",
    "ruff": "python3 -m pip install -r requirements-dev.txt",
    "mypy": "python3 -m pip install -r requirements-dev.txt",
    "PSScriptAnalyzer": "pwsh -Command 'Install-Module PSScriptAnalyzer -Scope CurrentUser -Force'",
}
INSTALL_HINTS = WINDOWS_INSTALL_HINTS if WINDOWS else LINUX_INSTALL_HINTS
WINDOWS_INSTALL_HELP = (
    'For Chocolatey, Scoop, or direct downloads, see "Installing the tools" in',
    "docs/installation.md. To use Git Bash from another location, set GIT_BASH",
    "to its bash.exe.",
    "After installing, open a new terminal. Applications that were already running,",
    "including ones minimized to the system tray, keep the old PATH until restarted.",
)
LINUX_INSTALL_HELP = (
    'For other distributions, Microsoft\'s package repository, or direct downloads, see "Installing the tools" in',
    "docs/installation.md.",
)
INSTALL_HELP = WINDOWS_INSTALL_HELP if WINDOWS else LINUX_INSTALL_HELP
# The command that runs Python here, which every shipped skill's fences and grants render from {{PYTHON}}: skill-core's,
# which the skills' scripts write into the commands they hand an agent, so the two always agree.
PYTHON = executable_locator.PYTHON
# Commands this platform provides beyond the portable ones in tools.STANDARD_COMMANDS: Python by the name above, and on
# Windows Windows PowerShell and cygpath, which Git for Windows bundles.
STANDARD_COMMANDS = frozenset({"python", "powershell", "cygpath"}) if WINDOWS else frozenset({PYTHON})


def ensure_supported() -> None:
    if sys.platform not in SUPPORTED_PLATFORMS:
        raise DeployError(
            f"ERROR: The deployer runs on {' and '.join(SUPPORTED_PLATFORMS.values())} only "
            f"(detected platform: {sys.platform})."
        )


def normalize(path: str | os.PathLike[str]) -> str:
    """Render a path with forward slashes, the form used in config values and output. Only Windows separates with a
    backslash; elsewhere it is a character of the name, and is kept."""
    text = os.fspath(path)
    return text.replace("\\", "/") if WINDOWS else text


def normalize_path_input(value: str) -> str:
    """Accept a path as typed or pasted on Windows: backslashes, surrounding quotes, trailing separators."""
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
        stripped = stripped[1:-1]
    normalized = normalize(stripped)
    # Keep a root, C:/ or /, intact so it is still rejected as a filesystem root.
    root_length = 3 if WINDOWS else 1
    while len(normalized) > root_length and normalized.endswith("/"):
        normalized = normalized[:-1]
    return normalized


def from_shell_path(value: str) -> str:
    """Translate a Git Bash drive path such as /c/Tools into C:/Tools; leave every other path unchanged, and every
    path off Windows, where /c/Tools is a directory of its own."""
    match = GIT_BASH_DRIVE.match(value) if WINDOWS else None
    return f"{match.group(1).upper()}:/{match.group(2) or ''}" if match else value


def to_shell_path(value: str) -> str:
    """Translate a drive-letter path such as C:/Tools into Git Bash's /c/Tools; leave every other path unchanged, and
    every path off Windows, where the shell reads paths as they are."""
    normalized = normalize(value)
    match = DRIVE_PATH.match(normalized) if WINDOWS else None
    return f"/{match.group(1).lower()}{match.group(2)}" if match else normalized


# Where the runtimes read personal skills from, as named in the warning about an ignored HOME.
RUNTIME_HOME = "the Windows profile folder"


def home_directory(environment: Mapping[str, str] | None = None) -> Path:
    """The home the runtimes read skills from: on Windows the profile folder, whatever HOME says, and elsewhere HOME.

    Claude Code, Codex, and Copilot CLI all read the profile folder on Windows, while Git Bash may set HOME to
    another directory and MSYS2 does by default. Elsewhere every runtime reads HOME. Without the variable, the home is
    Python's, which on Unix is the user's entry in the password database.
    """
    environment = os.environ if environment is None else environment
    value = environment.get("USERPROFILE" if WINDOWS else "HOME")
    return Path(value) if value else Path.home()


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


def name_key(name: str, directory: str | os.PathLike[str] | None = None) -> str:
    """The form in which the file system compares a name in `directory`: two names with one key are one file there.

    Case sensitivity belongs to the file system, not the platform: NTFS folds case, while Linux's file systems, and a
    Windows directory marked case-sensitive, do not. So `directory` is asked, through folds_case, and a name in one that
    does not fold is its own key. Without a directory, case is folded, the answer that refuses more names as one.
    """
    if directory is not None and not folds_case(directory):
        return name
    return name.casefold()


# Each directory's answer to folds_case once the file system has given one, by its absolute path.
_FOLDS_CASE: dict[str, bool] = {}


def folds_case(directory: str | os.PathLike[str]) -> bool:
    """Whether the file system compares the names in `directory` without regard to case.

    It is asked of an entry whose name has an ASCII letter: the entry is looked up again by the name with each such
    letter's case swapped, and the directory folds when that finds the same file. The answer is kept for the rest of
    the run. A directory that cannot be listed, or holds no such entry yet, is taken to fold, the answer that refuses
    more, and is asked again next time.
    """
    key = os.path.abspath(directory)  # noqa: PTH100 - a lexical key, as derived paths are
    if key in _FOLDS_CASE:
        return _FOLDS_CASE[key]
    answer = _asked_folds_case(Path(key))
    if answer is None:
        return True
    _FOLDS_CASE[key] = answer
    return answer


def _asked_folds_case(directory: Path) -> bool | None:
    """The file system's answer for `directory`, or None when no entry of it can give one."""
    try:
        names = sorted(entry.name for entry in os.scandir(directory))
    except OSError:
        return None
    for name in names:
        swapped = "".join(character.swapcase() if character.isascii() else character for character in name)
        if swapped == name:
            continue
        try:
            found = os.lstat(directory / name)
        except OSError:
            continue  # removed meanwhile
        try:
            other = os.lstat(directory / swapped)
        except FileNotFoundError:
            return False
        except OSError:
            continue
        return (other.st_dev, other.st_ino) == (found.st_dev, found.st_ino)
    return None


# The extension Windows finds a program by when its caller leaves it off, so git and git.exe start one program.
PROGRAM_EXTENSION = ".exe"


def program_name(name: str) -> str:
    """The name a program is called by, without the extension Windows lets its caller add, in any case: git for
    git.exe and for git.EXE. Any other name is returned unchanged."""
    if name.casefold().endswith(PROGRAM_EXTENSION):
        return name[: -len(PROGRAM_EXTENSION)]
    return name


# A configured directory is an absolute path of this platform's form, which errors name: a drive-letter path on
# Windows and a POSIX path elsewhere. The character allowlist that applies to it is the configuration's own.
ABSOLUTE = WINDOWS_ABSOLUTE if WINDOWS else POSIX_ABSOLUTE
ROOT = WINDOWS_ROOT if WINDOWS else POSIX_ROOT
ABSOLUTE_PATH = "an absolute Windows path" if WINDOWS else "an absolute path"
# The directory a prompt for one gives as its example.
EXAMPLE_DIRECTORY = "C:/GitHub" if WINDOWS else "/home/you/GitHub"


def is_absolute(value: str) -> bool:
    return bool(ABSOLUTE.match(value))


def is_filesystem_root(value: str) -> bool:
    return bool(ROOT.fullmatch(value))


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
        attributes = os.lstat(path).st_file_attributes  # type: ignore[attr-defined]  # Windows only, and AttributeError elsewhere
    except (AttributeError, OSError):
        return False
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


# The machine settings a runtime needs here before it can run a skill's scripts, which `deploy.py check` reports: on
# Windows, Codex CLI uses a read-only sandbox until ~/.codex/config.toml sets [windows] sandbox, and no sandbox of
# Codex CLI can start a program that is an app execution alias. Linux has neither.
CODEX_NEEDS_SANDBOX_SETTING = WINDOWS
HAS_APP_EXECUTION_ALIASES = WINDOWS
APP_EXECUTION_ALIAS_REMEDY = (
    "turn off the python.exe and python3.exe aliases under Settings > Apps > Advanced app settings > "
    "App execution aliases"
)


def is_app_execution_alias(program: str | os.PathLike[str]) -> bool:
    """Whether a program is an app execution alias: a reparse point in the local application data's WindowsApps
    folder, as the python.exe stub every Windows install registers is. Never on another platform, and never when that
    folder is unset or relative, so nothing is judged relative to the working directory."""
    if not HAS_APP_EXECUTION_ALIASES:
        return False
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    if not local.is_absolute():
        return False
    path = Path(program)
    aliases = local / "Microsoft" / "WindowsApps"
    return os.path.normcase(path.parent) == os.path.normcase(aliases) and is_reparse_point(path)


def find_executable(name: str) -> str | None:
    """The program `name` runs, by skill-core's locator, so a tool `check` finds is the one a skill's script finds."""
    return executable_locator.find_executable(name)


def find_executables(name: str) -> list[str]:
    """Every program `name` may run, in skill-core's locator's search order, for a tool whose probe passes over one
    it does not support, as the skills' own lookup does."""
    return executable_locator.find_executables(name)


def find_bash() -> str | None:
    """Find the Bash that checks rendered scripts: on Windows Git for Windows' Bash, never another bash on PATH, such
    as WSL's, and elsewhere the system's, the first bash on PATH."""
    if not WINDOWS:
        return find_executable("bash")
    configured = os.environ.get("GIT_BASH")
    if configured and Path(configured).is_file():
        return configured
    candidate = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
    if candidate.is_file():
        return str(candidate)
    return _bash_beside_git()


def _bash_beside_git() -> str | None:
    """Locate the Bash shipped with the git the locator finds, as Scoop and per-user Git installs place it."""
    git = find_executable("git")
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
    """PowerShell 7, or on Windows Windows PowerShell when it is not installed, found by the locator."""
    if not WINDOWS:
        return find_executable("pwsh")
    return find_executable("pwsh") or find_executable("powershell")


def find_on_path(name: str, search_path: str) -> str | None:
    """The first program `name` runs on exactly this PATH value, by skill-core's walk of PATH: never the caller's own
    PATH, no link folder, and never the working directory, which `shutil.which` searches first on Windows."""
    return next(iter(executable_locator.path_programs(name, search_path)), None) if search_path else None


def find_pwsh(search_path: str) -> str | None:
    """Find PowerShell 7 on exactly this PATH value; never Windows PowerShell, and never the caller's own PATH."""
    return find_on_path("pwsh", search_path)


class HiddenWindow(TypedDict, total=False):
    """The subprocess.run keyword hidden_window gives, typed so that `**hidden_window()` checks as that keyword."""

    creationflags: int


def hidden_window() -> HiddenWindow:
    """Keyword arguments for subprocess.run that start a console program without a window of its own.

    A hook runs without a console on Windows, so a console program it starts would otherwise flash a window. Elsewhere
    a program opens no window by starting, so there are none.
    """
    return {"creationflags": CREATE_NO_WINDOW} if WINDOWS else {}


def install_hint(tool: str) -> str:
    return INSTALL_HINTS[tool]


# Windows reports a process that an exception or another process ended, rather than the program itself, by an NTSTATUS
# whose severity bits put it at 0x80000000 or above, read unsigned as Python reads every exit code here. A program's
# own exit code stays below it, as Bash's, which is at most 255, does. Git Bash ended with 0x80000004 on the hosted
# runners before it printed anything.
TERMINATION_STATUS_FLOOR = 0x80000000


def termination_status(returncode: int) -> str | None:
    """How a process was ended by an exception or by another process rather than by the program, or None for a
    program's own exit code: on Windows the status in hexadecimal, and elsewhere the signal, which Python reports as
    the negated signal number, by its name."""
    if not WINDOWS:
        if returncode >= 0:
            return None
        try:
            return signal.Signals(-returncode).name
        except ValueError:
            return f"signal {-returncode}"
    return f"0x{returncode:08X}" if returncode >= TERMINATION_STATUS_FLOOR else None


@dataclass(frozen=True)
class ToolResult:
    returncode: int
    output: str


def command_line_length(arguments: list[str]) -> int:
    """The characters these arguments take on a command line, quoting included, as run_tool passes them."""
    import subprocess

    return len(subprocess.list2cmdline(arguments))


def process_tree() -> ProcessTree:
    """A tree for one command and every process it starts, which ends them all when it is left: skill-core's, a job
    object on Windows and a process group elsewhere. A caller that starts the command itself starts it with the tree's
    start_options."""
    return job_tree.process_tree()


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

    The tool and every process it starts are held in one process tree, which ends them all once it has finished: a
    Windows job object allows no breakaway, and a process group holds every process that does not start a session or
    group of its own. Its environment is this process's without git's repository-selecting variables, so a git it runs,
    such as the source commit's, reads the checkout it names, with `environment` added.
    Raises ToolTimedOut when it has not finished within `timeout` seconds, TOOL_DEADLINE_SECONDS unless given, and
    DeployError when it cannot be held with the processes it starts.
    """
    import subprocess
    import tempfile

    seconds = TOOL_DEADLINE_SECONDS if timeout is None else timeout
    merged = without_repository_variables(os.environ)
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


# Whether a process is running and when it started, which the lock judges a holder by: skill-core's one probe, which
# the code-review pipeline also uses, and which reports a process it cannot read as running.
ProcessStatus = process_identity.ProcessStatus


def current_process_id() -> int:
    return os.getpid()


# Filesystem writes that tests/run_validation.py allows outside deployer/fsops.py, with the reason.
FSOPS_ALLOWED = {
    "tempfile.TemporaryFile": "run_tool captures a tool's output in an unnamed file under the system temporary "
    "directory, outside every managed root, which closing it deletes; it changes no deployed state",
}


def process_status(pid: int) -> ProcessStatus:
    """Report whether a process is running and, when readable, its start time."""
    return process_identity.process_status(pid)

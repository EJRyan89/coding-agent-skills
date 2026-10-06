"""Operating-system specific behavior. Only Windows is implemented."""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import DeployError

WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:/")
WINDOWS_ROOT = re.compile(r"^[A-Za-z]:/$")
GIT_BASH_DRIVE = re.compile(r"^/([A-Za-z])(?:/(.*))?$", re.DOTALL)
DRIVE_PATH = re.compile(r"^([A-Za-z]):(.*)$", re.DOTALL)
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
CREATE_NO_WINDOW = 0x08000000
INSTALL_HINTS = {
    "Git Bash": "winget install --id Git.Git",
    "ShellCheck": "winget install --id koalaman.shellcheck",
    "PowerShell": "winget install --id Microsoft.PowerShell",
    # A validation-only dependency, installed into the interpreter that runs validation, which is python here.
    "ruff": "python -m pip install -r requirements-dev.txt",
    "mypy": "python -m pip install -r requirements-dev.txt",
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


def use_utf8_output() -> None:
    """Write UTF-8 even when output is redirected, where Windows would otherwise use the ANSI code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


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


def is_absolute(value: str) -> bool:
    return bool(WINDOWS_ABSOLUTE.match(value))


def is_filesystem_root(value: str) -> bool:
    return bool(WINDOWS_ROOT.fullmatch(value))


def canonical_directory(path: str | os.PathLike[str]) -> str:
    return normalize(os.path.realpath(path))


def is_link(path: Path) -> bool:
    return path.is_symlink()


def is_reparse_point(path: Path) -> bool:
    try:
        attributes = os.lstat(path).st_file_attributes  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def find_executable(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / f"{name}.exe"
    return str(winget) if winget.is_file() else None


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


def hidden_window() -> dict[str, int]:
    """Keyword arguments for subprocess.run that start a console program without a window of its own.

    A hook runs without a console, so a console program it starts would otherwise flash a window.
    """
    return {"creationflags": CREATE_NO_WINDOW}


def install_hint(tool: str) -> str:
    return INSTALL_HINTS[tool]


@dataclass(frozen=True)
class ToolResult:
    returncode: int
    output: str


def run_tool(arguments: list[str], environment: dict[str, str] | None = None) -> ToolResult:
    """Run a tool with stdout and stderr captured through files, never inherited pipes."""
    import subprocess
    import tempfile

    merged = dict(os.environ)
    if environment:
        merged.update(environment)
    # MSYS tools can reject inherited anonymous pipes with a spurious text/binary mode error.
    with tempfile.TemporaryFile() as captured:
        completed = subprocess.run(
            arguments,
            env=merged,
            stdin=subprocess.DEVNULL,
            stdout=captured,
            stderr=subprocess.STDOUT,
            check=False,
        )
        captured.seek(0)
        return ToolResult(completed.returncode, captured.read().decode("utf-8", "replace"))


@dataclass(frozen=True)
class ProcessStatus:
    alive: bool
    start_time: int | None


def current_process_id() -> int:
    return os.getpid()


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

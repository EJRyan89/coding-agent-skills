"""The one lookup of a program by its command name, shared by `deploy.py check` and the skills that run the program.

A tool `check` reports as installed is then the one a skill finds and runs under the same environment: the first
PATH names, else one in a folder the platform's package manager links the commands it installs into, which PATH may
not include. A skill runs the path found here, never the bare name: process creation would look the name up on PATH
alone elsewhere, and on Windows in the working directory before PATH, and neither searches the link folders. Nothing
here depends on the working directory. The deployer reaches it through `platform_support.find_executable` and
`find_program`; it holds the platform's link folders itself because a skill's script runs without the deployer.

Two lookups answer differently on Windows only when a shell script, a `.bat`, `.cmd`, or `.ps1` file, comes before the
program: `find_program` passes over it, and `find_executable` does not. git, gh, dotnet, and dotnet-format, which a
skill starts with arguments through `located_command`, are found by `find_program` in `check`, the skills, and the
deployer alike, so `check` reports such a host as missing the tool when it has only a shell script. The runtime CLIs,
which npm installs as `.cmd` scripts and which are started with arguments the repository writes, are found by
`find_executable`.
"""

from __future__ import annotations

import errno
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from path_keys import path_key

# The command name Python answers to here, which the deployer renders into the skills as their interpreter token and
# the skills' own scripts write into the commands they hand an agent: python on Windows, where python3 is often a
# Microsoft Store placeholder, and python3 elsewhere, where a distribution may install no python command.
PYTHON = "python" if sys.platform == "win32" else "python3"

# The extension Windows finds a program by when its caller leaves it off, so git and git.exe start one program.
PROGRAM_EXTENSION = ".exe"


def program_name(name: str) -> str:
    """The name a program is called by, without the extension Windows lets its caller add, in any case: git for
    git.exe and for git.EXE. Any other name is returned unchanged. The deployer reads it as
    `platform_support.program_name`."""
    if name.casefold().endswith(PROGRAM_EXTENSION):
        return name[: -len(PROGRAM_EXTENSION)]
    return name


# The environment variables a lookup reads, so a program kept from one lookup is looked up again once they change.
LOOKUP_VARIABLES = ("PATH", "PATHEXT", "LOCALAPPDATA")

# Programs `located_command` found, by the command name and the values of LOOKUP_VARIABLES it found each under.
Found = dict[tuple[str | None, ...], str]


# The presence reads and removals here that read a path they cannot stat, list, or remove as nothing there,
# each with why that is the answer it should give.
PRESENCE_ALLOWED = {
    "path_programs": "searches as shutil.which does, passing over a candidate it cannot stat for the next one; a "
    "command with no candidate it can run is reported missing, never run",
    "find_executables": "searches as shutil.which does, passing over a candidate it cannot stat for the next one; a "
    "command with no candidate it can run is reported missing, never run",
}


class RelativeEnvironmentPath(ValueError):
    """An environment variable that is set names a path that is not absolute, which only the working directory could
    complete."""

    def __init__(self, variable: str, value: str) -> None:
        super().__init__(f"{variable} is {value!r}, which is not an absolute path")
        self.variable = variable
        self.value = value


def required_environment_path(variable: str, environment: Mapping[str, str] | None = None) -> Path | None:
    """The absolute path the environment variable `variable` names in `environment`, this process's when it is None,
    or None when it is unset or empty; raises RelativeEnvironmentPath for any other value that is not an absolute
    path, a drive-relative `C:bin` or a rooted `\\bin` on Windows included. It is for a caller that must refuse such a
    value rather than take the variable for unset, as a home or a runtime's configuration directory must."""
    value = (os.environ if environment is None else environment).get(variable, "")
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise RelativeEnvironmentPath(variable, value)
    return path


# The variable Python's home lookup reads first: the profile folder on Windows, and HOME elsewhere.
HOME_VARIABLE = "USERPROFILE" if sys.platform == "win32" else "HOME"


def absolute_home() -> Path:
    """Python's home folder, `Path.home()`, which reads HOME_VARIABLE as given; raises RelativeEnvironmentPath naming
    that variable when the home is not an absolute path, or cannot be found at all, so no folder under the home is read
    relative to the working directory."""
    try:
        home = Path.home()
    except RuntimeError:
        raise RelativeEnvironmentPath(HOME_VARIABLE, "") from None
    if not home.is_absolute():
        raise RelativeEnvironmentPath(HOME_VARIABLE, str(home))
    return home


def environment_path(variable: str, environment: Mapping[str, str] | None = None) -> Path | None:
    """The absolute path the environment variable `variable` names in `environment`, this process's when it is None,
    or None when it is unset, empty, or not an absolute path, a drive-relative `C:bin` or a rooted `\\bin` on Windows
    included, so a caller that looks a program or a settings file up from it never resolves one against the working
    directory."""
    try:
        return required_environment_path(variable, environment)
    except RelativeEnvironmentPath:
        return None


def link_folders() -> list[Path]:
    """The folders a package manager links the commands it installs into: WinGet's Links folder on Windows, and none
    elsewhere, where a distribution's package manager installs into a folder PATH already names. A local application
    data folder that is unset or relative gives none, so nothing is looked up relative to the working directory."""
    if sys.platform != "win32":
        return []
    local = environment_path("LOCALAPPDATA")
    return [] if local is None else [local / "Microsoft" / "WinGet" / "Links"]


def path_programs(name: str, search_path: str | None = None) -> list[str]:
    """Every file PATH holds for the command `name`, in the order `shutil.which` would look: PATH's folders in turn,
    each folder once, and within one, `name` itself when it already ends in a PATHEXT extension, then `name` with each
    extension in PATHEXT's order. `search_path` is the PATH value searched, this process's own when it is None. The
    current directory, which `shutil.which` searches first on Windows, is never searched: a PATH entry that is not an
    absolute path, such as `.`, `bin`, an empty one, or on Windows a drive-relative `C:bin` or a rooted `\\bin` on the
    current drive, is skipped rather than resolved against the working directory."""
    extensions = [extension for extension in os.environ.get("PATHEXT", "").split(os.pathsep) if extension]
    names = [name + extension for extension in extensions] or [name]
    if any(name.casefold().endswith(extension.casefold()) for extension in extensions):
        names.insert(0, name)
    found: list[str] = []
    seen: set[str] = set()
    for folder in (os.environ.get("PATH", "") if search_path is None else search_path).split(os.pathsep):
        if not Path(folder).is_absolute() or path_key(folder) in seen:
            continue
        seen.add(path_key(folder))
        found.extend(
            str(candidate)
            for candidate in (Path(folder) / file for file in names)
            if candidate.is_file() and os.access(candidate, os.X_OK)
        )
    return found


def find_executables(name: str) -> list[str]:
    """Every program `name` may run, in the order they are searched: PATH's, then each link folder's, each file once.
    A caller that passes over one, as an unsupported parser or a shell script is passed over, takes the next."""
    found: list[str] = []
    seen: set[str] = set()
    # WinGet links each command it installs as `<name>.exe`.
    linked = [folder / f"{name}.exe" for folder in link_folders()]
    for candidate in [*path_programs(name), *(str(program) for program in linked if program.is_file())]:
        key = path_key(candidate)
        if key not in seen:
            seen.add(key)
            found.append(candidate)
    return found


def find_executable(name: str) -> str | None:
    """The program `name` runs: the first PATH names, else the first link folder's, or None when neither has it. A
    shell script is found here like any program, since npm installs Claude Code, Codex, and Copilot CLI on Windows as
    `.cmd` scripts, which `deploy.py check` and `verify` start with arguments the repository writes; a program started
    with arguments its caller does not control is found with `find_program` instead."""
    return next(iter(find_executables(name)), None)


# What Windows starts through a shell rather than as a program: cmd.exe runs a .bat or .cmd file and parses its whole
# command line by its own rules, which Python's argument quoting does not hold against, and PowerShell runs a .ps1.
SHELL_SCRIPT_SUFFIXES = frozenset({".bat", ".cmd", ".ps1"})


def shell_script(path: str) -> bool:
    """Whether Windows would start `path` through a shell, as it does npm's `copilot.cmd`, rather than as a program.
    Elsewhere none is: the kernel starts a script by its `#!` line and hands it the arguments as they are, so no shell
    parses its command line."""
    return sys.platform == "win32" and Path(path).suffix.casefold() in SHELL_SCRIPT_SUFFIXES


def find_program(name: str) -> str | None:
    """The first program `find_executables` finds for `name` that is no shell script, wherever a shell script stands
    before it, or None when it finds none. cmd.exe would read `&`, `|`, `^`, `%`, `<`, `>`, and `"` in an argument as
    its own syntax, so an argument such as a search pattern a reviewer wrote could run a second command; a program
    found here receives its arguments as they are."""
    return next((path for path in find_executables(name) if not shell_script(path)), None)


def located_command(command: Sequence[str], *, kept: Found | None = None) -> list[str]:
    """`command` with its program named by the path `find_program` finds for it, so the program run is the one `check`
    reports and never a shell script, since the caller's arguments, such as a search pattern for git or gh, may hold
    text no caller controls; a program already given as a path is left as it is. Raises FileNotFoundError, as starting
    a missing program does, when the locator finds none, or finds only shell scripts.

    With `kept`, a program found is kept there and reused until one of LOOKUP_VARIABLES changes, so a caller that
    starts the same program many times walks PATH once rather than for each command."""
    program, *arguments = command
    if Path(program).name != program:
        return [program, *arguments]
    key = (program, *(os.environ.get(variable) for variable in LOOKUP_VARIABLES))
    found = None if kept is None else kept.get(key)
    if found is None:
        found = find_program(program)
        if found is None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), program)
        if kept is not None:
            kept[key] = found
    return [found, *arguments]

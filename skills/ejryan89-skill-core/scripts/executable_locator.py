"""The one lookup of a program by its command name, shared by `deploy.py check` and the skills that run the program.

A tool `check` reports as installed is then the one a skill finds and runs under the same environment: the first
PATH names, else one in a folder the platform's package manager links the commands it installs into, which PATH may
not include. A skill runs the path found here, never the bare name: process creation would look the name up on PATH
alone elsewhere, and on Windows in the working directory before PATH, and neither searches the link folders. Nothing
here depends on the working directory. The deployer reaches it through `platform_support.find_executable`; it holds
the platform's link folders itself because a skill's script runs without the deployer.
"""

from __future__ import annotations

import errno
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from path_keys import path_key

# The command name Python answers to here, which the deployer renders into the skills as their interpreter token and
# the skills' own scripts write into the commands they hand an agent: python on Windows, where python3 is often a
# Microsoft Store placeholder, and python3 elsewhere, where a distribution may install no python command.
PYTHON = "python" if sys.platform == "win32" else "python3"

# The environment variables a lookup reads, so a program kept from one lookup is looked up again once they change.
LOOKUP_VARIABLES = ("PATH", "PATHEXT", "LOCALAPPDATA")

# Programs `located_command` found, by the command name and the values of LOOKUP_VARIABLES it found each under.
Found = dict[tuple[str | None, ...], str]


def environment_path(variable: str) -> Path | None:
    """The absolute path the environment variable `variable` names, or None when it is unset, empty, or not an
    absolute path, a drive-relative `C:bin` or a rooted `\\bin` on Windows included, so a caller that looks a program
    or a settings file up from it never resolves one against the working directory."""
    path = Path(os.environ.get(variable, ""))
    return path if path.is_absolute() else None


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
        if not Path(folder).is_absolute() or os.path.normcase(folder) in seen:
            continue
        seen.add(os.path.normcase(folder))
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
    """The program `name` runs: the first PATH names, else the first link folder's, or None when neither has it."""
    return next(iter(find_executables(name)), None)


def located_command(command: Sequence[str], *, kept: Found | None = None) -> list[str]:
    """`command` with its program named by the path the locator finds for it, so the program run is the one `check`
    reports; a program already given as a path is left as it is. Raises FileNotFoundError, as starting a missing
    program does, when the locator finds none.

    With `kept`, a program found is kept there and reused until one of LOOKUP_VARIABLES changes, so a caller that
    starts the same program many times walks PATH once rather than for each command."""
    program, *arguments = command
    if Path(program).name != program:
        return [program, *arguments]
    key = (program, *(os.environ.get(variable) for variable in LOOKUP_VARIABLES))
    found = None if kept is None else kept.get(key)
    if found is None:
        found = find_executable(program)
        if found is None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), program)
        if kept is not None:
            kept[key] = found
    return [found, *arguments]

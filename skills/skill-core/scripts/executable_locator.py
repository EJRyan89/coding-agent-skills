"""The one lookup of a program by its command name, shared by `deploy.py check` and the skills that run the program.

A tool `check` reports as installed is then the one a skill finds and runs under the same environment: the first
PATH names, else one in a folder the platform's package manager links the commands it installs into, which PATH may
not include. A skill runs the path found here, never the bare name, which process creation would look up on PATH
alone. The deployer reaches it through `platform_support.find_executable`; it holds the platform's link folders
itself because a skill's script runs without the deployer.
"""

from __future__ import annotations

import errno
import os
import sys
from collections.abc import Sequence
from pathlib import Path


def link_folders() -> list[Path]:
    """The folders a package manager links the commands it installs into: WinGet's Links folder on Windows, and none
    elsewhere yet. A local application data folder that is unset or relative gives none, so nothing is looked up
    relative to the working directory."""
    if sys.platform != "win32":
        return []
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    return [local / "Microsoft" / "WinGet" / "Links"] if local.is_absolute() else []


def path_programs(name: str) -> list[str]:
    """Every file PATH holds for the command `name`, in the order `shutil.which` would look: PATH's folders in turn,
    each folder once, and within one, `name` itself when it already ends in a PATHEXT extension, then `name` with each
    extension in PATHEXT's order. The current directory, which `shutil.which` searches first on Windows, is never
    searched, because PATH does not name it."""
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


def find_executables(name: str) -> list[str]:
    """Every program `name` may run, in the order they are searched: PATH's, then each link folder's, each file once.
    A caller that passes over one, as an unsupported parser or a shell script is passed over, takes the next."""
    found: list[str] = []
    seen: set[str] = set()
    # WinGet links each command it installs as `<name>.exe`.
    linked = [folder / f"{name}.exe" for folder in link_folders()]
    for candidate in [*path_programs(name), *(str(program) for program in linked if program.is_file())]:
        key = os.path.normcase(Path(candidate).absolute())
        if key not in seen:
            seen.add(key)
            found.append(candidate)
    return found


def find_executable(name: str) -> str | None:
    """The program `name` runs: the first PATH names, else the first link folder's, or None when neither has it."""
    return next(iter(find_executables(name)), None)


def located_command(command: Sequence[str]) -> list[str]:
    """`command` with its program named by the path the locator finds for it, so the program run is the one `check`
    reports; a program already given as a path is left as it is. Raises FileNotFoundError, as starting a missing
    program does, when the locator finds none."""
    program, *arguments = command
    if Path(program).name != program:
        return [program, *arguments]
    found = find_executable(program)
    if found is None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), program)
    return [found, *arguments]

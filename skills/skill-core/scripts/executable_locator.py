"""The one lookup of a program by its command name, shared by `deploy.py check` and the skills that run the program.

A tool `check` reports as installed is then the one a skill finds under the same environment: the first PATH names,
else one in a folder the platform's package manager links the commands it installs into, which PATH may not include.
The deployer reaches it through `platform_support.find_executable`; it holds the platform's link folders itself
because a skill's script runs without the deployer.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def link_folders() -> list[Path]:
    """The folders a package manager links the commands it installs into: WinGet's Links folder on Windows, and none
    elsewhere yet. A local application data folder that is unset or relative gives none, so nothing is looked up
    relative to the working directory."""
    if sys.platform != "win32":
        return []
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    return [local / "Microsoft" / "WinGet" / "Links"] if local.is_absolute() else []


def find_executable(name: str) -> str | None:
    """The program `name` runs: the first PATH names, else the first link folder's, or None when neither has it."""
    found = shutil.which(name)
    if found:
        return found
    for folder in link_folders():
        # WinGet links each command it installs as `<name>.exe`.
        candidate = folder / f"{name}.exe"
        if candidate.is_file():
            return str(candidate)
    return None

"""How the deployer reads the files it keeps and the source's JSON: a file, nothing, or a named refusal.

A state file (the manifest, the configuration, a run's journal, the lock's metadata) is read by the deployer on every
later run, so a reader that lets an unexpected exception escape fails every run after it, and one that takes a
directory in the file's place for no file at all plans a deployment the commit then cannot make.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import platform_support
from .errors import DeployError


def parse_json(text: str) -> object | None:
    """The JSON text's value, or None when it is not JSON the deployer can read.

    ValueError covers a decoding error and an integer past Python's digit limit; RecursionError, nesting too deep.
    """
    value: object
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return value


def is_present(path: Path, what: str, *remedy: str) -> bool:
    """Whether a file is at path: False when nothing is, and a refusal naming it when something else is.

    A directory, a link to nothing, or any other thing that is not a regular file is refused here, before anything is
    planned, so it is never read as absent.
    """
    if path.is_file():
        return True
    if not os.path.lexists(path):
        return False
    raise DeployError(f"ERROR: {what} at {platform_support.normalize(path)} is not a file.", *remedy)

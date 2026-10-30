"""How the deployer reads the files it keeps and the source's JSON: a file, nothing, or a named refusal.

A state file (the manifest, the configuration, a run's journal, the lock's metadata) is read by the deployer on every
later run, so a reader that lets an unexpected exception escape fails every run after it, and one that takes a
directory in the file's place for no file at all plans a deployment the commit then cannot make.
"""

from __future__ import annotations

from pathlib import Path

import file_presence
import json_reader

from . import platform_support
from .errors import DeployError


def parse_json(text: str) -> object | None:
    """The JSON text's value, or None when it is not JSON the deployer can read.

    ValueError covers a decoding error, an integer past Python's digit limit, and the repeated key or undefined
    constant `json_reader` refuses; RecursionError, nesting too deep.
    """
    value: object
    try:
        value = json_reader.loads(text)
    except (ValueError, RecursionError):
        return None
    return value


def holds_lone_surrogate(text: str) -> bool:
    """Whether `text` is JSON that `parse_json` refuses only for a lone surrogate, a character no UTF-8 file holds.

    The deployer up to v0.11.1 wrote its state through `json.dumps`, which escapes one, so a file it left can hold one;
    its writers now refuse one, so a reader that names this case can tell the user the file is an earlier release's,
    not damaged, and the same remedy applies.
    """
    try:
        json_reader.loads(text)
    except (ValueError, RecursionError):
        pass
    else:
        return False
    try:
        json_reader.loads(text, surrogates=True)
    except (ValueError, RecursionError):
        return False
    return True


def is_present(path: Path, what: str, *remedy: str) -> bool:
    """Whether a file is at path: False when nothing is, and a refusal naming it when something else is.

    A directory, a link to nothing, or any other thing that is not a regular file is refused here, before anything is
    planned, so it is never read as absent.
    """
    try:
        return file_presence.is_present(path)
    except file_presence.NotAFile:
        raise DeployError(f"ERROR: {what} at {platform_support.normalize(path)} is not a file.", *remedy) from None

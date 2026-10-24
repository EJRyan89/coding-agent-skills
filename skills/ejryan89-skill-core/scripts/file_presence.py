"""Whether a file a reader may find absent is there: a file, nothing, or a refusal naming what stands in its place.

A reader that checks `is_file()` before it reads takes a directory, a link to nothing, or any other thing that is not
a regular file for no file at all, and reports the repository as having no such configuration, a pass by absence.
`is_present` tells the three apart, and raises `NotAFile`, an `OSError`, for the third, so a reader that already turns
an `OSError` from its read into its own refusal refuses this one alike.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path


class NotAFile(OSError):
    """Something other than a regular file stands where a file is read."""

    def __init__(self, path: Path) -> None:
        super().__init__(errno.EISDIR if path.is_dir() else errno.EINVAL, "not a file", str(path))


def is_present(path: Path) -> bool:
    """Whether a regular file is at `path`, following links: False when nothing is, not even a link to nothing, and
    `NotAFile` when something else is."""
    if path.is_file():
        return True
    if not os.path.lexists(path):
        return False
    raise NotAFile(path)

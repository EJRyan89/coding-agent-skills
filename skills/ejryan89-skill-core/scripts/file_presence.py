"""Whether a file a reader may find absent is there: a file, nothing, or a refusal naming what stands in its place.

A reader that checks `is_file()` before it reads takes a directory, a link to nothing, or any other thing that is not
a regular file for no file at all, and reports the repository as having no such configuration, a pass by absence.
`is_present` tells the three apart, and raises `NotAFile`, an `OSError`, for the third, so a reader that already turns
an `OSError` from its read into its own refusal refuses this one alike.

A stat it cannot make is a fourth answer, that it could not tell, and reaches the caller as the `OSError` the stat
raised: `Path.is_file`, `os.path.exists`, and `os.path.lexists` return False on every `OSError` from Python 3.14 on,
and `os.path.lexists` on a `ValueError` too, so none of them is called here. Only `FileNotFoundError` and
`NotADirectoryError` mean nothing is there. A path holding a NUL character, which no file name holds, is refused as an
`OSError` too, rather than the `ValueError` the stat raises, so the same handler refuses it.
"""

from __future__ import annotations

import errno
import stat
from pathlib import Path

# The errors a stat raises when nothing is at the path: the path or a folder above it is missing, or a folder above
# it is a file. Every other error is one the reader cannot read as absence.
ABSENT = (FileNotFoundError, NotADirectoryError)


class NotAFile(OSError):
    """Something other than a regular file stands where a file is read."""

    def __init__(self, path: Path, *, directory: bool = False) -> None:
        super().__init__(errno.EISDIR if directory else errno.EINVAL, "not a file", str(path))


def is_present(path: Path) -> bool:
    """Whether a regular file is at `path`, following links: False when nothing is, not even a link to nothing,
    `NotAFile` when something else is, and the stat's `OSError` when it cannot tell."""
    if "\0" in str(path):
        raise OSError(errno.EINVAL, "the path holds a NUL character, which no file name holds", str(path))
    try:
        mode = path.stat().st_mode
    except ABSENT:
        try:
            path.stat(follow_symlinks=False)
        except ABSENT:
            return False
        raise NotAFile(path) from None
    if stat.S_ISREG(mode):
        return True
    raise NotAFile(path, directory=stat.S_ISDIR(mode))

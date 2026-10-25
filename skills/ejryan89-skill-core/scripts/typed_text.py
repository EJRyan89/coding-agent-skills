"""A text file as its author typed it: its UTF-8 text without the byte-order mark a Windows editor may save first.

A file read as `utf-8` keeps that mark as U+FEFF at the head of its first line, so a check that reads the first line,
such as whether a pull request title opens with a type or a body opens with a heading, refuses the text as typed.
`read_typed` is the one read of such a file, and validation refuses a title or body file read any other way.
"""

from __future__ import annotations

from pathlib import Path

# The codec that drops one leading byte-order mark and reads the rest as UTF-8.
TYPED_ENCODING = "utf-8-sig"
# What `read_typed` raises on bad content: text that is not UTF-8.
CONTENT_ERRORS: tuple[type[Exception], ...] = (UnicodeDecodeError,)


def read_typed(path: Path) -> str:
    """The UTF-8 text of the file at `path` without a leading byte-order mark: an `OSError` when it cannot be read, and
    a `UnicodeDecodeError` when it is not UTF-8."""
    return path.read_text(encoding=TYPED_ENCODING)

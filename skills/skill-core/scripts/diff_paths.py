"""The repository paths a git diff's file headers name.

Git writes a path in a `--- a/<path>` or `+++ b/<path>` line, and in a `rename from` or `rename to` line, as is,
unless it holds a byte outside printable ASCII, a double quote, or a backslash: then it writes the whole name, its
prefix included, between double quotes, each such byte escaped as `\\a`, `\\b`, `\\t`, `\\n`, `\\v`, `\\f`, `\\r`,
`\\"`, `\\\\`, or three octal digits. An unquoted `---` or `+++` path that holds a space ends with a tab, so the
name's end is plain however it ends. A script that reads those lines decodes the path here, so every reader drops the
same tab and undoes the same quoting.

A decoded path is the pull request author's to choose, and pathlib on Windows reads a backslash in it as a separator,
so a script that reads the file a diff names finds it through `snapshot_path`, which holds it inside the snapshot.
"""

from __future__ import annotations

import re
from pathlib import Path

# The bytes git escapes by a letter or by themselves rather than in octal.
ESCAPED_BYTES = {"a": 0x07, "b": 0x08, "t": 0x09, "n": 0x0A, "v": 0x0B, "f": 0x0C, "r": 0x0D, '"': 0x22, "\\": 0x5C}
OCTAL_DIGITS = frozenset("01234567")
# What no path a script reads by may hold: a backslash, which Windows reads as a separator, or a control character.
UNSAFE_CHARACTERS = re.compile(r"[\\\x00-\x1f\x7f]")
DRIVE = re.compile(r"^[A-Za-z]:")


class DiffPathError(ValueError):
    """A quoted path git does not write."""


def unquote(value: str) -> str:
    """`value` with git's quoting undone, or as given when it is not quoted. The escaped bytes are decoded as UTF-8,
    with one U+FFFD for each byte that is not, as a diff read with `errors="replace"` holds an unquoted name.

    Raises DiffPathError for a quoted value with no closing quote, a bare quote inside, or an escape git does not
    write."""
    if not value.startswith('"'):
        return value
    if len(value) < 2 or not value.endswith('"'):
        raise DiffPathError(f"Malformed quoted diff path: {value!r}")
    body, raw, index = value[1:-1], bytearray(), 0
    while index < len(body):
        character = body[index]
        if character == '"':
            raise DiffPathError(f"Malformed quoted diff path: {value!r}")
        if character != "\\":
            raw += character.encode("utf-8")
            index += 1
            continue
        escape = body[index + 1 : index + 2]
        if escape in ESCAPED_BYTES:
            raw.append(ESCAPED_BYTES[escape])
            index += 2
        elif len(octal := body[index + 1 : index + 4]) == 3 and set(octal) <= OCTAL_DIGITS and octal < "400":
            raw.append(int(octal, 8))
            index += 4
        else:
            raise DiffPathError(f"Malformed quoted diff path: {value!r}")
    return raw.decode("utf-8", "replace")


def header_path(value: str) -> str | None:
    """The repository path a `--- ` or `+++ ` line names, given the line after those four characters: its line end
    and the tab git ends an unquoted name holding a space with left off, git's quoting undone, and its `a/` or `b/`
    prefix removed; None for `/dev/null`, the side an added or a deleted file has no file on. A name that ends in a
    space keeps it."""
    path = unquote(value.removesuffix("\n").removesuffix("\r").removesuffix("\t"))
    if path == "/dev/null":
        return None
    return path[2:] if path.startswith(("a/", "b/")) else path


def snapshot_path(root: Path, path: str) -> Path | None:
    """Where a decoded diff path lands in the snapshot at `root`, resolved, or None when it could land outside it: a
    path holding a backslash or a control character, one with an empty, `.`, or `..` segment, which an absolute path
    has first, or a segment that starts with a drive, which Windows joins as a new root, and one that resolves outside
    the resolved root, as through a link, or cannot be resolved. The rule is the same on every platform, so a path
    refused on one is refused on all."""
    if UNSAFE_CHARACTERS.search(path):
        return None
    if any(segment in {"", ".", ".."} or DRIVE.match(segment) for segment in path.split("/")):
        return None
    try:
        base = root.resolve()
        target = base.joinpath(*path.split("/")).resolve()
    except OSError:
        return None
    return target if target.is_relative_to(base) else None

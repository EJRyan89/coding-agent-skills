"""Whether a file or folder a reader may find absent is there: it, nothing, or a refusal naming what stands in its
place.

A reader that checks `is_file()` before it reads takes a directory, a link to nothing, or any other thing that is not
a regular file for no file at all, and reports the repository as having no such configuration, a pass by absence.
`is_present` tells the three apart, and raises `NotAFile`, an `OSError`, for the third, so a reader that already turns
an `OSError` from its read into its own refusal refuses this one alike. `is_directory` answers the same for a folder,
raising `NotADirectory`, and `is_occupied` whether anything at all, a link to nothing included, is at a path.
`refusal_reason` words why a file or folder a caller needs is not there, missing, of another kind, or not
examinable, and `require` raises the caller's own error with it, so a refusal never names the last two as missing.
`matching` lists what a glob pattern matches below a folder, as `Path.glob` does, but raises for a folder it cannot
list, where `Path.glob` matches nothing in it from Python 3.15 on, so a reader takes no unreadable folder for an empty
one.

A stat it cannot make is a fourth answer, that it could not tell, and reaches the caller as the `OSError` the stat
raised: `Path.is_file`, `os.path.exists`, and `os.path.lexists` return False on every `OSError` from Python 3.14 on,
and `os.path.lexists` on a `ValueError` too, so none of them is called here. Only `FileNotFoundError` and
`NotADirectoryError` mean nothing is there. A path holding a NUL character, which no file name holds, is refused as an
`OSError` too, rather than the `ValueError` the stat raises, so the same handler refuses it.
"""

from __future__ import annotations

import errno
import fnmatch
import os
import stat
from collections.abc import Callable
from pathlib import Path

# The errors a stat raises when nothing is at the path: the path or a folder above it is missing, or a folder above
# it is a file. Every other error is one the reader cannot read as absence.
ABSENT = (FileNotFoundError, NotADirectoryError)


class NotAFile(OSError):
    """Something other than a regular file stands where a file is read."""

    def __init__(self, path: Path, *, directory: bool = False) -> None:
        super().__init__(errno.EISDIR if directory else errno.EINVAL, "not a file", str(path))


class NotADirectory(OSError):
    """Something other than a directory stands where a folder is read. It is not a NotADirectoryError, which `ABSENT`
    reads as nothing being there."""

    def __init__(self, path: Path) -> None:
        super().__init__(errno.ENOTDIR, "not a directory", str(path))


def _refuse_nul(path: Path) -> None:
    if "\0" in str(path):
        raise OSError(errno.EINVAL, "the path holds a NUL character, which no file name holds", str(path))


def _mode(path: Path) -> int | None:
    """The mode of what is at `path` through links, the link's own mode for a link to nothing, or None when nothing
    is there; any other stat error reaches the caller."""
    _refuse_nul(path)
    try:
        return path.stat().st_mode
    except ABSENT:
        try:
            return path.stat(follow_symlinks=False).st_mode
        except ABSENT:
            return None


def is_present(path: Path, *, other_is_absent: bool = False) -> bool:
    """Whether a regular file is at `path`, following links: False when nothing is, not even a link to nothing,
    `NotAFile` when something else is, or False then too for a caller that passes `other_is_absent`, such as one
    choosing the files among a listing's entries, and the stat's `OSError` when it cannot tell."""
    mode = _mode(path)
    if mode is None:
        return False
    if stat.S_ISREG(mode):
        return True
    if other_is_absent:
        return False
    raise NotAFile(path, directory=stat.S_ISDIR(mode))


def is_directory(path: Path, *, other_is_absent: bool = False) -> bool:
    """Whether a folder is at `path`, following links: False when nothing is, not even a link to nothing,
    `NotADirectory` when something else is, or False then too for a caller that passes `other_is_absent`, such as one
    choosing the folders among a listing's entries, and the stat's `OSError` when it cannot tell."""
    mode = _mode(path)
    if mode is None:
        return False
    if stat.S_ISDIR(mode):
        return True
    if other_is_absent:
        return False
    raise NotADirectory(path)


# What `refusal_reason` answers for a path where nothing is, so a caller that words a missing path its own way can
# tell that answer from the others.
MISSING = "is missing"


def refusal_reason(path: Path, *, folder: bool = False) -> str | None:
    """None when a regular file, or with `folder` a folder, is at `path`, following links; otherwise why not, worded to
    follow the path in a refusal: `MISSING` when nothing is there, "is not a file" or "is not a folder" when something
    else is, and "cannot be examined: <reason>" with the operating system's reason when a stat cannot tell. A caller
    that refuses a path it needs raises on any answer but None, so it never names a path it could not examine, or one
    of the wrong kind, as missing."""
    try:
        present = is_directory(path) if folder else is_present(path)
    except NotADirectory:
        return "is not a folder"
    except NotAFile:
        return "is not a file"
    except OSError as exc:
        return f"cannot be examined: {exc.strerror or exc}"
    return None if present else MISSING


def require(path: Path, error: Callable[[str], Exception], missing: str, subject: str, *, folder: bool = False) -> None:
    """Return when a regular file, or with `folder` a folder, is at `path`. Otherwise raise the caller's `error`:
    with `missing` when nothing is there, and with `subject` followed by `refusal_reason`'s reason when something
    else is there or a stat cannot examine it, so neither is ever reported as missing."""
    reason = refusal_reason(path, folder=folder)
    if reason == MISSING:
        raise error(missing)
    if reason is not None:
        raise error(f"{subject} {reason}")


def is_occupied(path: Path) -> bool:
    """Whether anything is at `path` itself, a link to nothing included, without following a link: False only when
    nothing is, and the stat's `OSError` when it cannot tell."""
    _refuse_nul(path)
    try:
        path.stat(follow_symlinks=False)
    except ABSENT:
        return False
    return True


def _never(path: Path) -> bool:
    return False


def matching(folder: Path, pattern: str, *, skip: Callable[[Path], bool] = _never) -> list[Path]:
    """The paths below `folder` a relative glob pattern matches, in path order, as `Path.glob` matches them: `*`, `?`,
    and `[...]` within one name, in the case the platform reads names in, and a `**` part before another any depth of
    folders, the folder itself included, without entering a symbolic link to a folder. Nothing when nothing is at
    `folder`, `NotADirectory` when something else is, and the `OSError` of a folder it cannot list, where `Path.glob`
    matches nothing in it from Python 3.15 on. A pattern that is empty, absolute, ends in `**`, whose matches differ
    between Python versions, or holds an empty, `.`, or `..` part raises ValueError.

    `skip` names the folders below `folder` a caller leaves out, such as `node_modules` or a folder of fixtures: one it
    answers True for is never listed or entered, so a folder it cannot list inside a skipped one is never met, while
    one elsewhere still raises. A skipped folder is still a match of a final part, as an entry of the folder above
    it.

    Each path is kept once by its spelling as the listing gave it, never by `Path` equality, which on Windows lowers
    each name with `str.lower` and so takes two names the file system keeps apart, such as the Kelvin sign and K, for
    one."""
    parts = pattern.split("/")
    if parts[-1] == "**" or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"{pattern!r} is no relative glob pattern this reads")
    if not is_directory(folder):
        return []
    found = [folder]
    for index, part in enumerate(parts):
        if part == "**":
            found = [below for base in found for below in _folders_below(base, skip)]
            continue
        last = index == len(parts) - 1
        found = [
            Path(entry.path)
            for base in found
            for entry in _entries(base)
            if fnmatch.fnmatch(entry.name, part) and (last or (entry.is_dir() and not skip(Path(entry.path))))
        ]
    return sorted({os.fspath(path): path for path in found}.values())


def _entries(folder: Path) -> list[os.DirEntry[str]]:
    """What a folder holds; a folder it cannot list raises, and a DirEntry's kind test raises every error but "not
    found"."""
    with os.scandir(folder) as listing:
        return list(listing)


def _folders_below(folder: Path, skip: Callable[[Path], bool]) -> list[Path]:
    """A folder and every folder below it, entering no symbolic link to a folder and no folder `skip` names. It walks
    with a stack of its own, as `folder_walk` does, so a tree nested deeper than the interpreter recurses is listed
    too, never ended in a RecursionError; entering no link, it meets no folder twice and so needs no depth bound."""
    found: list[Path] = []
    pending = [folder]
    while pending:
        current = pending.pop()
        found.append(current)
        below = [Path(entry.path) for entry in _entries(current) if entry.is_dir(follow_symlinks=False)]
        pending += reversed([path for path in below if not skip(path)])
    return found

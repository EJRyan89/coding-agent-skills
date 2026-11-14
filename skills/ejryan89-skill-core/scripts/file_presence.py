"""Whether a file or folder a reader may find absent is there: it, nothing, or a refusal naming what stands in its
place.

A reader that checks `is_file()` before it reads takes a directory or any other thing that is not a regular file for
no file at all, and reports the repository as having no such configuration, a pass by absence. `is_present` tells a
file, nothing, and something else apart, and raises `NotAFile`, an `OSError`, for the third, so a reader that already
turns an `OSError` from its read into its own refusal refuses this one alike. `is_directory` answers the same for a
folder, raising `NotADirectory`, and `is_there` whether anything a reader meets is there at all. `refusal_reason`
words why a file or folder a caller needs is not there, missing, of another kind, or not examinable, and `require`
raises the caller's own error with it, so a refusal never names the last two as missing.

A symbolic link whose target does not exist, a link to nothing, is read two ways on purpose. A reader asks what it can
read there, and nothing can be read through such a link, so `is_present`, `is_directory`, `is_there`, and
`refusal_reason` answer for it as for a missing path: no file, no folder, nothing there, `MISSING`. A writer asks
whether its write would land on something, and writing to such a link creates its target wherever it points, so
`is_occupied`, which a writer asks before it creates or moves anything into a place, counts the link as occupying it,
and a reader never asks it. A caller that reads a path as missing and then
writes there asks `is_occupied` before the write, or writes through a call that replaces the link itself, such as a
rename, never one that follows it.
`matching` lists what a glob pattern matches below a folder, as `Path.glob` does, but raises for a folder it cannot
list, where `Path.glob` matches nothing in it from Python 3.15 on, so a reader takes no unreadable folder for an empty
one, and for a pattern it cannot read relative to its folder on every platform, such as one naming a drive, so a
reader takes no pattern it never looked for as matching nothing.

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
from pathlib import Path, PurePosixPath, PureWindowsPath

PLATFORM_ALLOWED = {
    "pathlib.PureWindowsPath": "refuses a glob pattern Windows reads as naming a drive, a root, or a network share, on "
    "every platform, so a pattern means the same names below its folder on each"
}

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
    """The mode of what is at `path` through links, or None when nothing is there, a link to nothing included; any
    other stat error reaches the caller. The place itself is examined after the stat through it answered "not found",
    as `is_occupied` examines it, so a stat of a link that fails is reported, never read as nothing there."""
    _refuse_nul(path)
    try:
        return path.stat().st_mode
    except ABSENT:
        is_occupied(path)
        return None


def is_present(path: Path, *, other_is_absent: bool = False) -> bool:
    """Whether a regular file is at `path`, following links: False when nothing is, a link to nothing included,
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
    """Whether a folder is at `path`, following links: False when nothing is, a link to nothing included,
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
    follow the path in a refusal: `MISSING` when nothing is there, a link to nothing included, "is not a file" or
    "is not a folder" when something else is, and "cannot be examined: <reason>" with the operating system's reason
    when a stat cannot tell. A caller that refuses a path it needs raises on any answer but None, so it never names a
    path it could not examine, or one of the wrong kind, as missing."""
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


def is_there(path: Path) -> bool:
    """Whether anything a reader meets is at `path`, following links: a file, a folder, or anything else, but False
    when nothing is, a link to nothing included, and the stat's `OSError` when it cannot tell. It is the reader's
    question `is_occupied` answers for a writer, for a reader that names something of another kind apart from nothing
    there."""
    return _mode(path) is not None


def is_occupied(path: Path) -> bool:
    """Whether anything is at `path` itself, a link to nothing included, without following a link: False only when
    nothing is, and the stat's `OSError` when it cannot tell. The readers above answer a link to nothing as missing;
    this counts it as occupying its place, so a writer that asks it before creating there never writes through the
    link to create its target."""
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
    `folder`, a link to nothing included, `NotADirectory` when something else is, and the `OSError` of a folder it
    cannot list, where `Path.glob` matches nothing in it from Python 3.15 on. A pattern not of the one form
    `_pattern_parts` accepts raises ValueError, so a pattern it never looked for is never read as matching
    nothing.

    `skip` names the folders below `folder` a caller leaves out, such as `node_modules` or a folder of fixtures: one it
    answers True for is never listed or entered, so a folder it cannot list inside a skipped one is never met, while
    one elsewhere still raises. A skipped folder is still a match of a final part, as an entry of the folder above
    it.

    Each path is kept once by its spelling as the listing gave it, never by `Path` equality, which on Windows lowers
    each name with `str.lower` and so takes two names the file system keeps apart, such as the Kelvin sign and K, for
    one."""
    parts = _pattern_parts(pattern)
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


def _pattern_parts(pattern: str) -> tuple[str, ...]:
    """The parts of a glob pattern `matching` reads, split on `/`, or ValueError for any other pattern. It accepts one
    form, rather than refusing the spellings found so far: a pattern in which Windows path rules read no anchor, so
    no drive, root, or network share, and whose `/`-separated parts are exactly the parts both POSIX and Windows path
    rules read in it, so no part is empty or `.` or holds a backslash, and the pattern means the same names below its
    folder on every platform. Of those parts, none is `..`, which climbs out of the folder, and the last is not `**`,
    whose matches differ between Python versions."""
    parts = tuple(pattern.split("/"))
    windows = PureWindowsPath(pattern)
    if (
        windows.anchor
        or windows.parts != parts
        or PurePosixPath(pattern).parts != parts
        or ".." in parts
        or parts[-1] == "**"
    ):
        raise ValueError(f"{pattern!r} is no relative glob pattern this reads")
    return parts


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

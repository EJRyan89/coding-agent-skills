"""The one place a script keeps state of the user's own across runs: a folder in the temporary directory that only
the user owns and can write, checked before every use.

The temporary directory is the user's own on Windows and macOS, but on Linux, with `TMPDIR` unset, it is the shared
`/tmp`, where any local account may create a folder of a fixed name first. The sticky bit on `/tmp` keeps another
account from deleting or renaming an entry the user made there, but not an entry inside a folder that account made, so
a folder of state at a fixed name there would be the first creator's to empty or to fill.

`location` names the folder, carrying the user's id where the platform has one, so another account's folder never
stands in its place by name. `ensure` makes it, owner only, when it is missing, and refuses it with
`UnsafeFolderError` unless it is a directory, no link or junction, and, where the platform has user ids, one this user
owns that no group or other user can write. A folder that passes stays the user's: no other account can remove or
replace an entry the user made in a sticky directory, nor write inside a folder it cannot write. On Windows the
temporary directory is under the user's profile, which other accounts cannot write, and a folder's mode bits say
nothing about who may write it, so only the kind of the folder is checked there.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path

from link_probe import is_link_or_junction

# The mode a missing folder is made with: read, write, and search for its owner alone.
OWNER_ONLY = 0o700
# The mode bits that let a user other than the owner write a folder: its group's and everyone's.
OTHERS_WRITE = stat.S_IWGRP | stat.S_IWOTH


class UnsafeFolderError(Exception):
    """The folder is not one only this user owns and can write, so state kept there could be another's."""


def user_id() -> int | None:
    """The effective user id of this process, which owns what it creates, or None where the platform has none."""
    geteuid = getattr(os, "geteuid", None)
    return None if geteuid is None else int(geteuid())


def location(name: str, base: Path | None = None) -> Path:
    """Where the user's folder `name` lies: in `base`, by default the temporary directory, named `<name>-<user id>`
    where the platform has user ids and `name` alone where it has none."""
    user = user_id()
    return (Path(tempfile.gettempdir()) if base is None else base) / (name if user is None else f"{name}-{user}")


def refusal(folder: Path, metadata: os.stat_result, user: int | None) -> str | None:
    """Why `folder`, whose own `lstat` result is `metadata`, is not a folder only `user` owns and can write, or None
    when it is. With no `user`, as on Windows, only its kind is judged."""
    if is_link_or_junction(folder, metadata):
        return f"{folder} is a link or junction, not a folder of this user's own"
    if not stat.S_ISDIR(metadata.st_mode):
        return f"{folder} is not a directory"
    if user is None:
        return None
    if metadata.st_uid != user:
        return f"{folder} is owned by user id {metadata.st_uid}, not by this user (id {user})"
    if metadata.st_mode & OTHERS_WRITE:
        return f"{folder} can be written by users other than its owner (mode {stat.S_IMODE(metadata.st_mode):o})"
    return None


def ensure(folder: Path) -> Path:
    """`folder`, made owner only when it is missing, whose parent must exist; raises UnsafeFolderError naming why
    when it is not a folder only this user owns and can write, and lets any other OSError through."""
    with contextlib.suppress(FileExistsError):
        folder.mkdir(mode=OWNER_ONLY)
    reason = refusal(folder, os.lstat(folder), user_id())
    if reason is not None:
        raise UnsafeFolderError(reason)
    return folder

"""The one place a script keeps state of the user's own across runs: a folder in the user's runtime directory, or else
the temporary directory, that only the user owns and can write, checked before every use.

The temporary directory is the user's own on Windows and macOS, but on Linux, with `TMPDIR` unset, it is the shared
`/tmp`, where any local account may create a folder of a fixed name first. The sticky bit on `/tmp` keeps another
account from deleting or renaming an entry the user made there, but not an entry inside a folder that account made, so
a folder of state at a fixed name there would be the first creator's to empty or to fill.

`location` names the folder, carrying the user's id where the platform has one, so another account's folder never
stands in its place by name. Where the platform has user ids, it lies in the runtime directory `XDG_RUNTIME_DIR` names,
as systemd-logind sets it on Linux, so no other account can take the name first, but only when that is an absolute
path to a directory, no link, that the user owns with mode 0700, in a parent root or the user owns that no other
user can write unless it is sticky, as the XDG Base Directory specification requires of it: a runtime directory set by
hand, such as one under /tmp, that is a link another account planted, or that another account could rename away, is
never followed. Otherwise, and always on Windows, which has no user ids and where `ensure` judges only a folder's
kind, it lies in the temporary directory, where another account that makes the name first can only deny the user their
state, since `ensure` refuses its folder. A command that saves state and a later one that reads it must therefore
agree on the runtime directory: a session that sets it differently, or not at all, looks in another folder and finds
nothing saved. `ensure` makes the folder, owner only, when it is missing, and refuses it with
`UnsafeFolderError` unless it is a directory, no link or junction, and, where the platform has user ids, one this user
owns that no group or other user can write, in a parent that root or this user owns and that no other user can write
unless it is sticky. A folder that passes stays the user's: no other account can remove, rename, or replace an entry
the user made in such a parent, nor write inside a folder it cannot write, so nothing can swap the folder between the
check and a write. On Windows the temporary directory is under the user's profile, which other accounts cannot write,
and a folder's mode bits say nothing about who may write it, so only the kind of the folder is checked there.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path

from executable_locator import environment_path
from link_probe import is_link_or_junction

# The mode a missing folder is made with: read, write, and search for its owner alone.
OWNER_ONLY = 0o700
# The mode bits that let a user other than the owner write a folder: its group's and everyone's.
OTHERS_WRITE = stat.S_IWGRP | stat.S_IWOTH
# The variable naming the user's runtime directory, which systemd-logind sets on Linux to /run/user/<user id>, a folder
# the user owns with mode 0700, cleared when their last session ends.
RUNTIME_DIRECTORY = "XDG_RUNTIME_DIR"
# The one mode the XDG Base Directory specification allows a runtime directory: read, write, and search for its owner.
RUNTIME_MODE = 0o700

PRESENCE_ALLOWED = {
    "runtime_directory": "a runtime directory, or its parent, that it cannot stat is not one proven the user's own, so "
    "the temporary directory serves instead, where ensure judges the state folder before any use",
}
# The user id of root, who may remove any entry anywhere, so a parent root owns is as safe as one the user owns.
ROOT = 0


class UnsafeFolderError(Exception):
    """The folder is not one only this user owns and can write, so state kept there could be another's."""


def user_id() -> int | None:
    """The effective user id of this process, which owns what it creates, or None where the platform has none."""
    geteuid = getattr(os, "geteuid", None)
    return None if geteuid is None else int(geteuid())


def runtime_directory() -> Path | None:
    """The runtime directory `XDG_RUNTIME_DIR` names, or None where the platform has no user ids, as on Windows, or
    where it is unset, empty, or not an absolute path, so a relative one is never resolved against the working
    directory, or names anything but a directory, no link, that this user owns with mode RUNTIME_MODE, in a parent
    that root or this user owns and that no other user can write unless it is sticky."""
    user = user_id()
    runtime = None if user is None else environment_path(RUNTIME_DIRECTORY)
    if runtime is None:
        return None
    try:
        metadata = os.lstat(runtime)
        parent = runtime.parent.stat()
    except OSError:
        return None
    if (
        refusal(runtime, metadata, user) is not None
        or stat.S_IMODE(metadata.st_mode) != RUNTIME_MODE
        or parent_refusal(runtime.parent, parent, user) is not None
    ):
        return None
    return runtime


def base() -> Path:
    """The folder every state folder lies in: the runtime directory when `runtime_directory` finds one, and the
    temporary directory otherwise."""
    runtime = runtime_directory()
    return Path(tempfile.gettempdir()) if runtime is None else runtime


def location(name: str, parent: Path | None = None) -> Path:
    """Where the user's folder `name` lies: in `parent`, by default `base()`, named `<name>-<user id>` where the
    platform has user ids and `name` alone where it has none."""
    user = user_id()
    return (base() if parent is None else parent) / (name if user is None else f"{name}-{user}")


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


def parent_refusal(parent: Path, metadata: os.stat_result, user: int | None) -> str | None:
    """Why `parent`, whose `stat` result is `metadata`, would let another account remove or rename a folder this user
    made in it, or None when it would not: it must be owned by root or `user`, since a directory's owner may remove
    any entry, and no group or other user may write it unless it is sticky. With no `user`, as on Windows, nothing is
    judged."""
    if user is None:
        return None
    if metadata.st_uid not in (ROOT, user):
        return f"its parent {parent} is owned by user id {metadata.st_uid}, not by root or this user (id {user})"
    if metadata.st_mode & OTHERS_WRITE and not metadata.st_mode & stat.S_ISVTX:
        mode = stat.S_IMODE(metadata.st_mode)
        return f"its parent {parent} can be written by users other than its owner and is not sticky (mode {mode:o})"
    return None


def ensure(folder: Path) -> Path:
    """`folder`, made owner only when it is missing, whose parent must exist; raises UnsafeFolderError naming why
    when it is not a folder only this user owns and can write in a parent no other account can rename it out of, and
    lets any other OSError through."""
    user = user_id()
    reason = parent_refusal(folder.parent, folder.parent.stat(), user)
    if reason is not None:
        raise UnsafeFolderError(f"{folder}: {reason}")
    with contextlib.suppress(FileExistsError):
        folder.mkdir(mode=OWNER_ONLY)
    reason = refusal(folder, os.lstat(folder), user)
    if reason is not None:
        raise UnsafeFolderError(reason)
    return folder

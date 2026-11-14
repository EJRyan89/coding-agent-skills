"""The one place a script keeps state of the user's own across runs: a folder in the user's runtime directory, or else
the temporary directory, that only the user owns and can write, checked before every use.

The temporary directory is the user's own on Windows and macOS, but on Linux, with `TMPDIR` unset, it is the shared
`/tmp`, where any local account may create a folder of a fixed name first. The sticky bit on `/tmp` keeps another
account from deleting or renaming an entry the user made there, but not an entry inside a folder that account made, so
a folder of state at a fixed name there would be the first creator's to empty or to fill.

A folder is the user's only when no other account can rename or replace it or any folder above it: whoever may rename
an ancestor may put a tree of their own in its place, the user's folder and all, between a check and a write. So every
folder from its parent up to the root must be a directory, no link, that root or the user owns and that no other user
can write unless it is sticky, and each is judged on the path resolved through every link and `..` first, never on
the name as given, since `/shared/u/rt/inner/..` names `/shared/u/rt`, whose parent is `/shared/u`, not `.../inner`.
The resolved path is the one handed back and used, so no link along the name, which a folder another account can
write might hold, is followed again after the check.

`location` names the folder, carrying the user's id where the platform has one, so another account's folder never
stands in its place by name. Where the platform has user ids, it lies in the runtime directory `XDG_RUNTIME_DIR` names,
as systemd-logind sets it on Linux, so no other account can take the name first, but only when that is an absolute
path to a directory, no link, that the user owns with mode 0700, below folders no other account can rename, as above,
as the XDG Base Directory specification requires of it. A runtime directory set by hand that fails, such as one under
/tmp, a link another account planted, or one inside a shared folder anyone can write, is never followed:
`runtime_directory` falls back to the temporary directory instead, since a value in the environment is a preference,
and the temporary directory's own folder is then judged by `ensure` like any other. Otherwise, and always on Windows,
which has no user ids and where `ensure` judges only a folder's kind, it lies in the temporary directory, where another
account that makes the name first can only deny the user their state, since `ensure` refuses its folder. A command
that saves state and a later one that reads it must therefore agree on the runtime directory: a session that sets it
differently, or not at all, looks in another folder and finds nothing saved.

`ensure` makes the folder, owner only, when it is missing, and refuses it with `UnsafeFolderError`, never falling back,
since its caller named the folder and state kept anywhere else would not be found again: it refuses a folder that is
not a directory, no link or junction, and, where the platform has user ids, one this user owns that no group or other
user can write, with every folder above it, the temporary directory's included, one root or this user owns and that no
other user can write unless it is sticky. A folder that passes stays the user's: no other account can remove, rename,
or replace it or a folder above it, nor write inside a folder it cannot write, so nothing can swap the folder between
the check and a write. On Windows the temporary directory is under the user's profile, which other accounts cannot
write, and a folder's mode bits say nothing about who may write it, so only the kind of the folder is checked there,
and its name is used as given.
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
    "runtime_directory": "a runtime directory, or a folder above it, that it cannot stat is not one proven the user's "
    "own, so the temporary directory serves instead, where ensure judges the state folder before any use",
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
    """The runtime directory `XDG_RUNTIME_DIR` names, resolved through every link and `..`, or None where the platform
    has no user ids, as on Windows, or where it is unset, empty, or not an absolute path, so a relative one is never
    resolved against the working directory, or names a link, or resolves to anything but a directory that this user
    owns with mode RUNTIME_MODE, below folders `ancestry_refusal` accepts, or to one it cannot stat."""
    user = user_id()
    runtime = None if user is None else environment_path(RUNTIME_DIRECTORY)
    if user is None or runtime is None:
        return None
    try:
        named = os.lstat(runtime)
        real = Path(os.path.realpath(runtime))
        metadata = os.lstat(real)
        above = ancestry_refusal(real.parent, user)
    except OSError:
        return None
    if (
        is_link_or_junction(runtime, named)
        or refusal(real, metadata, user) is not None
        or stat.S_IMODE(metadata.st_mode) != RUNTIME_MODE
        or above is not None
    ):
        return None
    return real


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


def parent_refusal(parent: Path, metadata: os.stat_result, user: int | None, role: str = "parent") -> str | None:
    """Why `parent`, whose `stat` result is `metadata`, would let another account remove or rename a folder this user
    made in it, or None when it would not: it must be owned by root or `user`, since a directory's owner may remove
    any entry, and no group or other user may write it unless it is sticky. `role` names it in the reason, as the
    folder's parent or as an ancestor further up. With no `user`, as on Windows, nothing is judged."""
    if user is None:
        return None
    if metadata.st_uid not in (ROOT, user):
        return f"its {role} {parent} is owned by user id {metadata.st_uid}, not by root or this user (id {user})"
    if metadata.st_mode & OTHERS_WRITE and not metadata.st_mode & stat.S_ISVTX:
        mode = stat.S_IMODE(metadata.st_mode)
        return f"its {role} {parent} can be written by users other than its owner and is not sticky (mode {mode:o})"
    return None


def ancestry_refusal(parent: Path, user: int) -> str | None:
    """Why `parent`, a path already resolved through every link and `..`, or a folder above it up to the root, would
    let another account rename or replace a folder made in `parent`, or None when none would: each must be no link or
    junction, since one appearing on a resolved path was put there since, and must pass `parent_refusal`. The reason
    names `parent` as the folder's parent and each folder above it as its ancestor. An OSError from a stat is let
    through."""
    for ancestor in (parent, *parent.parents):
        role = "parent" if ancestor == parent else "ancestor"
        metadata = os.lstat(ancestor)
        if is_link_or_junction(ancestor, metadata):
            return f"its {role} {ancestor} is a link or junction"
        reason = parent_refusal(ancestor, metadata, user, role)
        if reason is not None:
            return reason
    return None


def resolved_place(folder: Path, user: int) -> Path:
    """`folder` with its parent resolved through every link and `..`, and its own name kept, so a link in its place is
    still judged as one; raises UnsafeFolderError naming why when its name is no folder's, as a trailing `..` is, or
    when `ancestry_refusal` refuses the resolved parent, and lets an OSError from a stat through."""
    if folder.name in ("", os.pardir):
        raise UnsafeFolderError(f"{folder} does not end in the name of a folder")
    parent = Path(os.path.realpath(folder.parent))
    reason = ancestry_refusal(parent, user)
    if reason is not None:
        raise UnsafeFolderError(f"{folder}: {reason}")
    return parent / folder.name


def ensure(folder: Path) -> Path:
    """`folder`, made owner only when it is missing, whose parent must exist, and returned, where the platform has
    user ids, with its parent resolved through every link and `..`, as every later use must name it; raises
    UnsafeFolderError naming why when it is not a folder only this user owns and can write below folders no other
    account can rename or replace, and lets any other OSError through."""
    user = user_id()
    if user is not None:
        folder = resolved_place(folder, user)
    with contextlib.suppress(FileExistsError):
        folder.mkdir(mode=OWNER_ONLY)
    reason = refusal(folder, os.lstat(folder), user)
    if reason is not None:
        raise UnsafeFolderError(reason)
    return folder

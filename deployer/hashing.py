"""Canonical, locale-independent content hashes for deployed files and directories."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Iterator
from pathlib import Path

import apple_double
import file_presence

from . import platform_support
from .errors import DeployError

HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
# The step each refusal of a tree the deployer cannot hash ends with: a folder it cannot list, a file whose name it
# cannot encode, and a link it will not follow.
UNLISTED_REMEDY = "Give your account read access to it, or move it out of the way, then rerun with --dry-run."
UNENCODABLE_REMEDY = "Rename or remove that file, then rerun with --dry-run."
LINKED_REMEDY = "Remove the link, or replace it with a copy of what it points to, then rerun with --dry-run."
# The step a refusal of a FIFO, socket, device, or other special file ends with: no deployment holds one.
SPECIAL_REMEDY = "Remove it, or move it out of the tree, then rerun with --dry-run."
# Each special kind a stat mode gives, as a refusal names it; any other that is no file, folder, or link is a special
# file.
SPECIAL_KINDS = (
    (stat.S_ISFIFO, "FIFO (named pipe)"),
    (stat.S_ISSOCK, "socket"),
    (stat.S_ISCHR, "character device"),
    (stat.S_ISBLK, "block device"),
)


PRESENCE_ALLOWED = {
    "hash_path": (
        "a path it cannot stat is read as a file, whose own metadata's read raises the error, so a failed stat is "
        "never hashed as anything"
    ),
}


def file_hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def tree_hash(files: dict[str, bytes]) -> str:
    """Hash a tree from its sorted POSIX relative paths and each file's content digest."""
    digest = hashlib.sha256()
    for relative in sorted(files, key=lambda path: path.encode("utf-8")):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(files[relative]).digest())
    return "sha256:" + digest.hexdigest()


def special_kind(metadata: os.stat_result) -> str | None:
    """What a path whose own metadata, never followed through a link, is `metadata` is, as a refusal names it, when it
    is a special file: a FIFO, a socket, a device, or any other kind that is no regular file, folder, or link. None
    for those three, which their own readers judge."""
    mode = metadata.st_mode
    if stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        return None
    return next((kind for test, kind in SPECIAL_KINDS if test(mode)), "special file")


def refuse_special(path: Path) -> None:
    """Refuse `path` when it is a special file by its own metadata, naming it and its kind, before anything opens it:
    opening a FIFO blocks until another process opens its other end, and a device or socket holds no content a
    deployment could hold. A path whose metadata cannot be read raises its OSError."""
    kind = special_kind(os.lstat(path))
    if kind is not None:
        raise DeployError(
            f"ERROR: Refusing to hash content containing a {kind}: {platform_support.normalize(path)}",
            SPECIAL_REMEDY,
        )


def _refuse_unlisted(error: OSError) -> None:
    """Refuse a folder os.walk cannot list, which it would otherwise skip: its files would be judged free of links
    and left out of the hash, so a tree whose contents were never read would compare as unmodified. os.walk passes the
    error os.scandir raised, which names the folder."""
    folder = platform_support.normalize(Path(error.filename))
    raise DeployError(
        f"ERROR: Cannot list {folder} to hash it or check it for links: {error.strerror or error}", UNLISTED_REMEDY
    )


def _hashable_name(root: Path, path: Path) -> str:
    """The POSIX relative name tree_hash hashes as UTF-8. A name UTF-8 cannot encode, such as a lone surrogate on
    Windows or a byte that is not UTF-8 on Linux, which Python reads as a surrogate escape, is refused naming the item
    and the path, never left to raise a UnicodeEncodeError that names neither."""
    relative = path.relative_to(root).as_posix()
    try:
        relative.encode("utf-8")
    except UnicodeEncodeError as error:
        # The path with the character UTF-8 cannot encode written as its escape, so every stream prints it alike.
        shown = platform_support.normalize(path).encode("utf-8", "backslashreplace").decode("utf-8")
        raise DeployError(
            f"ERROR: Cannot hash {root.name}: the name of {shown} is not valid UTF-8",
            UNENCODABLE_REMEDY,
        ) from error
    return relative


def _walk(root: Path) -> Iterator[tuple[str, list[str], list[str]]]:
    return os.walk(root, onerror=_refuse_unlisted, followlinks=False)


def find_link(path: Path) -> Path | None:
    """Return the first symlink or junction at or beneath a path, without following any. A path whose metadata cannot
    be read raises its OSError, never read as holding no link."""
    if platform_support.is_link_or_junction(path):
        return path
    try:
        if not file_presence.is_directory(path):
            return None
    except file_presence.NotADirectory:
        return None
    for directory, subdirectories, names in _walk(path):
        base = Path(directory)
        for name in [*subdirectories, *names]:
            if platform_support.is_link_or_junction(base / name):
                return base / name
    return None


def read_tree(root: Path) -> dict[str, bytes]:
    """Read every file beneath root; a symlink or junction anywhere, a folder that cannot be listed, a FIFO, socket,
    device, or other special file (`refuse_special`), or a file whose name is not valid UTF-8 is an error, never
    skipped. This is the one reader of names from disk that tree_hash hashes; the deployer writes every other tree it
    hashes, under names it chose.

    Python bytecode caches are ignored: running a deployed script creates them, and they
    must not make an untouched skill look locally modified.

    So are the AppleDouble `._<name>` sidecars macOS writes beside a file or folder on a FAT, exFAT, or SMB volume, as
    `apple_double.is_sidecar` recognizes one, by its sibling in the same folder and its header: one in a source
    checkout there is no file of the skill, and is not deployed, and one macOS writes beside a deployed file there
    must not make the skill look locally modified. The header is read only once `find_link` found no link in the tree,
    and only from an entry its own metadata shows a regular file, so it is never read through a link, a pipe, or a
    device; any other `._` file is read and hashed as the skill's own. The look at an entry's kind and its read are
    two steps, so a file replaced by a FIFO between them is opened: a stated limit, since a non-blocking open is a flag
    only POSIX defines, and only a local process can plant one, git committing none.
    """
    link = find_link(root)
    if link is not None:
        raise DeployError(
            f"ERROR: Refusing to hash content containing a symlink or junction: {platform_support.normalize(link)}",
            LINKED_REMEDY,
        )
    files: dict[str, bytes] = {}
    for directory, subdirectories, names in _walk(root):
        listing = frozenset((*subdirectories, *names))
        subdirectories[:] = [name for name in subdirectories if name != "__pycache__"]
        base = Path(directory)
        for name in names:
            path = base / name
            refuse_special(path)
            if name.endswith(".pyc"):
                continue
            if apple_double.is_sidecar(path, listing):
                continue
            files[_hashable_name(root, path)] = path.read_bytes()
    return files


def hash_path(path: Path) -> str:
    """The hash of the tree or file at `path`, a special file refused unopened as read_tree refuses one beneath it."""
    if path.is_dir() or platform_support.is_link_or_junction(path):
        return tree_hash(read_tree(path))
    refuse_special(path)
    return file_hash(path.read_bytes())

"""Canonical, locale-independent content hashes for deployed files and directories."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from . import platform_support
from .errors import DeployError

HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


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


def _is_link(path: Path) -> bool:
    return platform_support.is_link(path) or platform_support.is_reparse_point(path)


def find_link(path: Path) -> Path | None:
    """Return the first symlink or junction at or beneath a path, without following any."""
    if _is_link(path):
        return path
    if not path.is_dir():
        return None
    for directory, subdirectories, names in os.walk(path, followlinks=False):
        base = Path(directory)
        for name in [*subdirectories, *names]:
            if _is_link(base / name):
                return base / name
    return None


def read_tree(root: Path) -> dict[str, bytes]:
    """Read every file beneath root; a symlink or junction anywhere is an error, never skipped.

    Python bytecode caches are ignored: running a deployed script creates them, and they
    must not make an untouched skill look locally modified.
    """
    link = find_link(root)
    if link is not None:
        raise DeployError(f"ERROR: Refusing to hash content containing a symlink or junction: {platform_support.normalize(link)}")
    files: dict[str, bytes] = {}
    for directory, subdirectories, names in os.walk(root, followlinks=False):
        subdirectories[:] = [name for name in subdirectories if name != "__pycache__"]
        base = Path(directory)
        for name in names:
            if name.endswith(".pyc"):
                continue
            path = base / name
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def hash_path(path: Path) -> str:
    if path.is_dir() or _is_link(path):
        return tree_hash(read_tree(path))
    return file_hash(path.read_bytes())

"""Every mutating filesystem operation the deployer performs.

Tests replace these functions to inject failures at precise points.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import platform_support

PLATFORM_ALLOWED = {
    "chmod": "remove() adds owner write permission to retry deleting a file Windows marks read-only, such as a "
    "version-control pack file; changing a permission is a filesystem write, so it belongs here, not in "
    "platform_support",
}


def move(source: Path, destination: Path) -> None:
    """Rename without ever replacing an existing destination: a FileExistsError leaves both as they were.

    Journal finalization, recovery, and the lock reclaim rely on it, so platform_support refuses an existing destination
    where the platform's rename would replace it.
    """
    platform_support.refuse_existing_destination(destination)
    source.rename(destination)


def make_directory(path: Path) -> None:
    path.mkdir()


def make_directories(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _retry_writable(function: Callable[[str], Any], name: str, error: BaseException) -> None:
    """Retry a deletion that failed on a read-only file once, after making it writable; re-raise anything else."""
    if not isinstance(error, PermissionError):
        raise error
    try:
        Path(name).chmod(Path(name).stat().st_mode | stat.S_IWRITE)
    except OSError:
        raise error from None
    function(name)


def _remove_tree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry_writable)
    else:
        shutil.rmtree(path, onerror=lambda function, name, info: _retry_writable(function, name, info[1]))


def remove(path: Path) -> None:
    """Delete a path; a symlink or junction is unlinked itself, never followed into its target.

    A read-only file, which Windows will not delete, is made writable and deleted.
    """
    if platform_support.is_link(path) or platform_support.is_reparse_point(path):
        try:
            path.unlink()
        except OSError:
            path.rmdir()
    elif path.is_dir():
        _remove_tree(path)
    elif os.path.lexists(path):
        try:
            path.unlink()
        except OSError as error:
            _retry_writable(os.unlink, os.fspath(path), error)


def write_file(path: Path, content: bytes) -> None:
    """Write a file the deployer creates, such as a staged one; it is never read until the write has finished."""
    path.write_bytes(content)


def write_atomic(path: Path, content: bytes) -> None:
    """Replace a file through a synced copy beside it, removing the copy if the write or the replace fails.

    mkstemp creates the copy under a fresh name and refuses an existing one, so a file left at a temporary name, or
    planted there, is never written through. Its owner-only mode bits set no access control on Windows: the file
    inherits its folder's permissions, so a private file relies on its folder.
    """
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        if os.path.lexists(temporary):
            temporary.unlink()
        raise


def append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())

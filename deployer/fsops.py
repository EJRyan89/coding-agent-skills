"""Every mutating filesystem operation the deployer performs.

Tests replace these functions to inject failures at precise points.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from . import platform_support


def move(source: Path, destination: Path) -> None:
    """Rename without ever replacing an existing destination."""
    source.rename(destination)


def replace(source: Path, destination: Path) -> None:
    """Atomically replace a destination file the deployer itself owns."""
    source.replace(destination)


def make_directory(path: Path) -> None:
    path.mkdir()


def make_directories(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def remove(path: Path) -> None:
    """Delete a path; a symlink or junction is unlinked itself, never followed into its target."""
    if platform_support.is_link(path) or platform_support.is_reparse_point(path):
        try:
            path.unlink()
        except OSError:
            path.rmdir()
    elif path.is_dir():
        shutil.rmtree(path)
    elif os.path.lexists(path):
        path.unlink()


def write_file(path: Path, content: bytes) -> None:
    """Write a file the deployer creates, such as a staged one; it is never read until the write has finished."""
    path.write_bytes(content)


def write_private(path: Path, content: bytes) -> None:
    """Atomically replace a file only its owner may read, removing the temporary copy if anything fails."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        # mkstemp already creates the file readable and writable by its owner only, and the replace keeps that.
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())

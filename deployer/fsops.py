"""Every mutating filesystem operation the deployer performs.

Tests replace these functions to inject failures at precise points.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import platform_support


def move(source: Path, destination: Path) -> None:
    """Rename without ever replacing an existing destination."""
    os.rename(source, destination)


def replace(source: Path, destination: Path) -> None:
    """Atomically replace a destination file the deployer itself owns."""
    os.replace(source, destination)


def make_directory(path: Path) -> None:
    os.mkdir(path)


def make_directories(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def remove(path: Path) -> None:
    """Delete a path; a symlink or junction is unlinked itself, never followed into its target."""
    if platform_support.is_link(path) or platform_support.is_reparse_point(path):
        try:
            os.unlink(path)
        except OSError:
            os.rmdir(path)
    elif path.is_dir():
        shutil.rmtree(path)
    elif os.path.lexists(path):
        path.unlink()


def write_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with open(temporary, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def append_line(path: Path, line: str) -> None:
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())

"""The one walk up a folder's parents, shared by the skills that look for the nearest folder holding a file.

`folders_up(start, stop)` gives `start` and then each folder above it, nearest first, and ends after the folder that
is `stop`, compared as the file system compares paths (`dotnet_solution.same_path`: normalized, and case-folded where
the platform ignores case), or after the file system's root. A start that is not below `stop` therefore walks to the
root, so a caller that must stay inside a tree starts inside it. Without a `stop` the walk ends at the root, as
MSBuild's search for a `Directory.Build.props` does. The walk is lexical: it reads nothing, so a caller decides what
each folder holds, and a link on the way is a folder like any other.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from dotnet_solution import same_path


def folders_up(start: Path, stop: Path | None = None) -> Iterator[Path]:
    """`start` and each folder above it, nearest first, through `stop` or the file system's root."""
    end = None if stop is None else same_path(stop)
    folder = start
    while True:
        yield folder
        if same_path(folder) == end or folder.parent == folder:
            return
        folder = folder.parent

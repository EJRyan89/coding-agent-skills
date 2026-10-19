"""The one test of whether a path is a symbolic link or a Windows junction, for every skill that must never follow one.

A path is a link when its own metadata, never its target's, says so: a symbolic link, or, on Windows, any reparse
point, so a junction, a mount point, and a placeholder a sync client stands in for a file are each judged alike, since
each may lead somewhere the caller did not choose. Only Windows has the attribute, so elsewhere a link is a symbolic
link.

`is_link_or_junction` reads that metadata from the `lstat` result a listing already holds, or else with one `lstat`,
and lets an `OSError` through, for a caller that tells a missing path from an unreadable one. `is_plain_directory`
and `is_plain_file` fail closed: a path whose metadata cannot be read is neither.
"""

from __future__ import annotations

import os
import stat

StrPath = str | os.PathLike[str]


def is_link_or_junction(path: StrPath, metadata: os.stat_result | None = None) -> bool:
    """Whether `path` is a symbolic link, a junction, or another reparse point, judged from `metadata` when given,
    which must be `path`'s own `lstat` result, or else from one `lstat` of it, whose `OSError` is raised."""
    if metadata is None:
        metadata = os.lstat(path)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def is_plain_directory(path: StrPath, metadata: os.stat_result | None = None) -> bool:
    """Whether `path` is a directory that is no link or junction; False when its metadata cannot be read."""
    metadata = _own_metadata(path, metadata)
    return metadata is not None and stat.S_ISDIR(metadata.st_mode) and not is_link_or_junction(path, metadata)


def is_plain_file(path: StrPath, metadata: os.stat_result | None = None) -> bool:
    """Whether `path` is a regular file that is no link or reparse point; False when its metadata cannot be read."""
    metadata = _own_metadata(path, metadata)
    return metadata is not None and stat.S_ISREG(metadata.st_mode) and not is_link_or_junction(path, metadata)


def _own_metadata(path: StrPath, metadata: os.stat_result | None) -> os.stat_result | None:
    if metadata is not None:
        return metadata
    try:
        return os.lstat(path)
    except OSError:
        return None

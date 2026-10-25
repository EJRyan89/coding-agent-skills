"""The one form a path is compared in, so two spellings of one path compare equal as the platform reads them.

A path's key is the path made absolute against the working directory and normalized, so `a/../b` and `b` are one
path, then case-folded where the platform folds case, so `C:\\Work` and `c:\\work` are one path on Windows. By default
the key is lexical: nothing is opened, followed, or looked up, so it decides before anything touches the path; with
`resolve`, every link is followed first, so a path and a link to it are one. With `textual`, the lexical key is
made by text alone, the working directory joined and `.` and `..` folded, with none of the rewriting Windows' own
path parsing adds, such as dropping a name's trailing dots and spaces, so a check that must judge exactly the path it
was given, as the reviewer guard does, compares that. The deployer reaches it through
`platform_support`, its platform seam, and skill scripts and `tools/` import it here.
"""

from __future__ import annotations

import os
from pathlib import Path


def path_key(path: str | os.PathLike[str], *, resolve: bool = False, textual: bool = False) -> str:
    """`path` as the platform compares it: absolute and normalized, lexically or with every link followed when
    `resolve` is true, by text alone when `textual` is, and case-folded where the platform ignores case. A key cannot
    be both resolved and textual, so asking for both raises ValueError."""
    if resolve and textual:
        raise ValueError("a path key is resolved or textual, not both")
    if textual:
        given = Path(path)
        return os.path.normcase(os.path.normpath(given if given.is_absolute() else Path.cwd() / given))
    # Path.resolve would follow links where `resolve` is false, and Path.absolute keeps "..".
    absolute = os.path.realpath(path) if resolve else os.path.abspath(path)  # noqa: PTH100 - lexical, as above
    return os.path.normcase(absolute)

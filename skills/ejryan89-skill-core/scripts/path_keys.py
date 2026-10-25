"""The one form a path is compared in, so two spellings of one path compare equal as the platform reads them.

A path's key is the path made absolute against the working directory and normalized, so `a/../b` and `b` are one
path, then case-folded where the platform folds case, so `C:\\Work` and `c:\\work` are one path on Windows. By default
the key is lexical: nothing is opened, followed, or looked up, so it decides before anything touches the path; with
`resolve`, every link is followed first, so a path and a link to it are one. The deployer reaches it through
`platform_support`, its platform seam, and skill scripts and `tools/` import it here.
"""

from __future__ import annotations

import os


def path_key(path: str | os.PathLike[str], *, resolve: bool = False) -> str:
    """`path` as the platform compares it: absolute and normalized, lexically or with every link followed when
    `resolve` is true, and case-folded where the platform ignores case."""
    # Path.resolve would follow links where `resolve` is false, and Path.absolute keeps "..".
    absolute = os.path.realpath(path) if resolve else os.path.abspath(path)  # noqa: PTH100 - lexical, as above
    return os.path.normcase(absolute)

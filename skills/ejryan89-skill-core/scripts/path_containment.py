"""The one test of whether a path lies inside a tree, for every check that keeps a path from leaving one.

`within` compares two paths by text alone: each made absolute against the working directory and normalized without
reaching the file system, then compared as the platform compares names, case-folded where it ignores case, part by
part. So a `..` that climbs out of the tree, another drive, and a sibling whose name starts with the tree's are each
outside. A caller that must follow links resolves both paths first and compares the results; one that must never
follow them compares the paths as written. `posix_within` compares a `/`-separated name a listing gives, such as a
repository path git lists, by text, case and all.

`refusal` judges an entry taken from input, such as a repository path a record or a list names, inside the tree it
must stay in, before anything opens it. It runs three tests in turn and names the first that fails, or gives None:
the entry as written, which must be a relative path with no empty or `..` segment and no absolute, drive, rooted,
network-share, or device form; the entry as resolved, which must still lie in the resolved tree, so no link leads it
out; and ejryan89-skill-core's `link_probe`, which must find no symbolic link or junction on its way from the tree,
so the entry is never reached through a link even one that stays inside.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from link_probe import is_link_or_junction

StrPath = str | os.PathLike[str]


def _compared(path: StrPath) -> Path:
    """`path` as `within` compares it: absolute, normalized, and case-folded where the platform ignores case."""
    absolute = Path(path)
    if not absolute.is_absolute():
        absolute = Path.cwd() / absolute
    return Path(os.path.normcase(os.path.normpath(absolute)))


def within(path: StrPath, tree: StrPath, *, strict: bool = False) -> bool:
    """Whether `path` lies in `tree`, by text alone; with `strict`, below it, so the tree itself is not within."""
    candidate, root = _compared(path), _compared(tree)
    return candidate.is_relative_to(root) and not (strict and candidate == root)


def posix_within(name: str, folder: str, *, strict: bool = False) -> bool:
    """Whether `name`, a `/`-separated path such as a repository path git lists, is `folder` or lies in it, by text,
    case and all, as git names a repository's files; with `strict`, lies in it only. It is for names a listing gives,
    never for a path taken from input, which `refusal` judges."""
    return name.startswith(f"{folder}/") or (not strict and name == folder)


def written_refusal(entry: str) -> str | None:
    """Why `entry`, as written, is no relative path inside a tree, or None: an empty entry, a network-share, device,
    rooted, or drive form, or an empty or `..` segment. Both separators count, since Windows reads either."""
    if not entry:
        return "the entry is empty, so it names no path inside the tree"
    forward = entry.replace("\\", "/")
    if forward.startswith(("//?/", "//./")):
        return "the entry names a device, which is never opened"
    if forward.startswith("//"):
        return "the entry names a network share, which is never opened, so no host is contacted"
    if forward.startswith("/"):
        return "the entry is absolute, not a path relative to the tree"
    segments = forward.split("/")
    if any(re.match(r"^[A-Za-z]:", segment) for segment in segments):
        return "the entry names a drive, which Windows joins as a new root"
    if ".." in segments:
        return "the entry climbs out of its folder with a `..` segment"
    if "" in segments:
        return "the entry holds an empty segment"
    return None


def refusal(entry: str, tree: Path) -> str | None:
    """Why `entry`, a path relative to `tree` taken from input, is not read or written inside it, or None: refused as
    written by `written_refusal`, then as resolved when it leads outside the resolved tree or cannot be resolved, then
    when a symbolic link or junction lies on its path below the tree, or its link state cannot be read."""
    written = written_refusal(entry)
    if written is not None:
        return written
    parts = entry.replace("\\", "/").split("/")
    target = tree.joinpath(*parts)
    try:
        leaves = not within(target.resolve(), tree.resolve())
    except (OSError, RuntimeError) as error:
        return f"the entry cannot be resolved: {getattr(error, 'strerror', None) or error}"
    if leaves:
        return "a symbolic link or junction leads the entry outside the tree"
    current = tree
    for part in parts:
        current = current / part
        try:
            linked = is_link_or_junction(current)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as error:
            return f"the entry's link state cannot be read: {error.strerror or error}"
        if linked:
            return "a symbolic link or junction lies on the entry's path, which is never followed"
    return None

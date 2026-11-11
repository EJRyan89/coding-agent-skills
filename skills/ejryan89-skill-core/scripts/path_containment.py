"""The one test of whether a path lies inside a tree, for every check that keeps a path from leaving one.

`within` compares two paths by text alone: each made absolute against the working directory and normalized without
reaching the file system, then compared part by part as the file system holding the tree compares names, which
`path_keys.same_parts` asks of the tree's directories alone, never of the path judged, each without Windows'
extended-length or device-namespace prefix (`without_extended_prefix`), which `Path.resolve` may give one path and not
the other. So a `..` that climbs out of the tree, another drive, and a sibling whose name starts
with the tree's are each outside. A caller that must follow links resolves both paths first and compares the results;
one that must never follow them compares the paths as written. `posix_within` compares a `/`-separated name a listing
gives, such as a repository path git lists, by text, case and all.

`refusal` judges an entry taken from input, such as a repository path a record or a list names, inside the tree it
must stay in, before anything opens it. It runs three tests in turn and names the first that fails, or gives None:
the entry as written, which must be a relative path with no NUL character, no empty or `..` segment, no segment
Windows opens as a device or reads without its trailing dots and spaces, and no absolute, drive, rooted,
network-share, or device form; the entry as resolved, which must still lie in the resolved tree, so no link leads it
out; and ejryan89-skill-core's `link_probe`, which must find no symbolic link or junction on its way from the tree, so
the entry is never reached through a link even one that stays inside.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from device_names import is_reserved_device_name
from link_probe import is_link_or_junction
from path_keys import same_parts

StrPath = str | os.PathLike[str]

# The prefixes of Windows' extended-length and device-namespace spellings: `\\?\C:\x` and `\\.\C:\x` name `C:\x`, and
# `\\?\UNC\host\share\x` and `\\.\UNC\host\share\x` name `\\host\share\x`. `Path.resolve` keeps the first, and gives a
# volume's GUID path back with it, so a path and a tree are compared without any of them. A device-namespace path that
# names no drive or share, such as `\\.\pipe\x`, is kept. pathlib reads each as part of a path's drive, which a path has
# only on Windows, so elsewhere the same characters are an ordinary name and are kept.
EXTENDED_PREFIXES = ("\\\\?\\", "\\\\.\\")
UNC_PREFIX = "UNC\\"


def without_extended_prefix(path: Path) -> Path:
    """`path` without Windows' extended-length or device-namespace prefix, when one comes before a drive letter or a
    UNC share; any other path, such as a volume's GUID path or a pipe that no drive letter names, is kept as it is."""
    if not path.drive:
        return path
    text = str(path)
    prefix = next((prefix for prefix in EXTENDED_PREFIXES if text.startswith(prefix)), None)
    if prefix is None:
        return path
    rest = text[len(prefix) :]
    plain = Path("\\\\" + rest[len(UNC_PREFIX) :]) if rest[: len(UNC_PREFIX)].upper() == UNC_PREFIX else Path(rest)
    return plain if plain.drive else path


def within(path: StrPath, tree: StrPath, *, strict: bool = False) -> bool:
    """Whether `path` lies in `tree`, by text alone; with `strict`, below it, so the tree itself is not within. The
    path's leading parts are compared with the tree's as the tree's directories compare names, so the path judged is
    never asked, and a spelling the tree's file system takes as the tree's is inside it."""
    candidate, root = _plain(path).parts, _plain(tree).parts
    if len(candidate) < len(root) + strict:
        return False
    return same_parts(candidate[: len(root)], root)


def _plain(path: StrPath) -> Path:
    """`path` as `within` reads it before folding: without an extended-length or device-namespace prefix, made
    absolute against the working directory, and normalized by text alone."""
    return Path(os.path.normpath(Path.cwd() / without_extended_prefix(Path(path))))


def relative_within(path: StrPath, tree: StrPath, *, strict: bool = False) -> Path | None:
    """`path` relative to `tree`, or None when `within` finds it outside, or, with `strict`, the tree itself. Which
    parts lie below the tree is decided on the same forms `within` compares, the tree's part count in its compared
    form, so neither a prefix one side carries nor a case rule other than the tree's file system's can disagree with
    it; the parts themselves keep the path's own spelling. A caller that needs where a path lies in its tree takes it
    here, never from a `relative_to` of the paths as given."""
    if not within(path, tree, strict=strict):
        return None
    return Path(*_plain(path).parts[len(_plain(tree).parts) :])


def posix_within(name: str, folder: str, *, strict: bool = False) -> bool:
    """Whether `name`, a `/`-separated path such as a repository path git lists, is `folder` or lies in it, by text,
    case and all, as git names a repository's files; with `strict`, lies in it only. It is for names a listing gives,
    never for a path taken from input, which `refusal` judges."""
    return name.startswith(f"{folder}/") or (not strict and name == folder)


def written_refusal(entry: str) -> str | None:
    """Why `entry`, as written, is no relative path inside a tree, or None: an empty entry, a NUL character, a
    network-share, device, rooted, or drive form, an empty or `..` segment, a segment other than `.` that ends in a dot
    or a space, which Windows drops, so it names another file than written, as Git for Windows refuses such a name, or
    a segment `device_names` finds Windows opens as a device. Both separators count, since Windows reads either, and
    the trailing and device rules count on every platform, so an entry refused on one is refused on each."""
    if not entry:
        return "the entry is empty, so it names no path inside the tree"
    if "\0" in entry:
        return "the entry holds a NUL character, which no file name holds"
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
    if any(segment != "." and segment.endswith((".", " ")) for segment in segments):
        return "the entry has a segment ending in a dot or a space, which Windows drops, so it names another file"
    if any(is_reserved_device_name(segment) for segment in segments):
        return "the entry names a reserved device as a segment, which Windows opens instead of a file"
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

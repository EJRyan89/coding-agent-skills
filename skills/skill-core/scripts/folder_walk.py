"""The one walk of a folder tree, shared by the skills that search a repository for files by name.

`walk_files(top, wanted)` gives every file below `top` whose name `wanted` accepts, and every folder it did not enter
with the reason, so each caller decides what a folder it could not search costs it. Its contract:

- Order: each folder's files, in name order, before its subfolders, which it walks in name order, depth first.
- Skipped by name: a folder whose name, compared case-insensitively, is in `skipped`, by default `SKIPPED_FOLDERS`:
  version control (`.git`, `.hg`, `.svn`), dependencies and their caches (`node_modules`, `.venv`, `venv`, `.tox`,
  `.nox`, `__pycache__`), build output (`bin`, `obj`), and Visual Studio's own state (`.vs`). Such a folder is not
  reported: the set is the statement. A caller passes another set when its search needs one, as the .NET skills add
  NuGet's `packages` (`dotnet_solution.SEARCH_SKIPPED_FOLDERS`).
- Nested checkouts: a folder below `top` holding a `.git` folder or file, a clone, a submodule, or a worktree, is
  another repository's, so its files are not given; it is reported as `NESTED_CHECKOUT`. `top` itself is searched.
- Links: a folder that is a symbolic link or a Windows junction is not entered, since it may lead out of the tree or
  back into it, and is reported as `LINKED`, unless the caller's `follow` accepts its path. Even then a link that leads
  back into a folder the walk is inside is reported as `LOOP` and not entered, so the walk always ends. `top` is
  searched as given, link or not; a link to a file is a file, which the caller judges.
- Failures: a folder that cannot be listed is reported as `UNLISTABLE`, and an entry that cannot be examined as
  `UNEXAMINED`, each by path with the reason, and the walk goes on; an error never ends it.
- Bounds: none by depth or count. No caller bounds its search, and the walk enters no folder twice along one path.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

SKIPPED_FOLDERS = frozenset(
    {".git", ".hg", ".svn", "node_modules", ".venv", "venv", ".tox", ".nox", "__pycache__", "bin", "obj", ".vs"}
)
CHECKOUT_MARKER = ".git"

LINKED = "linked"
LOOP = "loop"
NESTED_CHECKOUT = "nested-checkout"
UNLISTABLE = "unlistable"
UNEXAMINED = "unexamined"

LINKED_REASON = "the folder is a junction or a symbolic link, which the search does not follow"
LOOP_REASON = "the folder is a link back into a folder the search is inside, which it does not enter again"
NESTED_CHECKOUT_REASON = "the folder holds another checkout, a clone, submodule, or worktree, which the search skips"


@dataclass(frozen=True)
class NotEntered:
    """A folder or entry the walk did not search: its path, one of the kinds above, and why, as a phrase."""

    path: Path
    kind: str
    reason: str


@dataclass
class Walk:
    """The files a walk found, in its order, and what it did not enter, in the order it met them."""

    files: list[Path] = field(default_factory=list)
    not_entered: list[NotEntered] = field(default_factory=list)


def is_folder_link(entry: os.DirEntry[str]) -> bool:
    """Whether a listed entry is a Windows junction or a symbolic link to a folder, judged from the entry's own
    metadata, so a junction whose target is gone counts too. Only Windows has the tag and the field, so elsewhere no
    entry is a junction."""
    tag = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
    junction = tag is not None and getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", None) == tag
    return junction or (entry.is_symlink() and entry.is_dir())


def folder_identity(path: Path) -> tuple[int, int]:
    """The device and file number of the folder `path` leads to, the same through any link to it."""
    status = path.stat()
    return status.st_dev, status.st_ino


def unlistable(path: Path, error: OSError) -> NotEntered:
    return NotEntered(path, UNLISTABLE, f"the folder cannot be listed: {error.strerror or error}")


Identities = frozenset[tuple[int, int]]


def walk_files(
    top: Path,
    wanted: Callable[[str], bool],
    *,
    skipped: frozenset[str] = SKIPPED_FOLDERS,
    follow: Callable[[Path], bool] | None = None,
) -> Walk:
    """Every file below `top` whose name `wanted` accepts, and every folder not entered, as the module states."""
    walk = Walk()
    folded = frozenset(name.casefold() for name in skipped)
    try:
        inside: Identities = frozenset({folder_identity(top)}) if follow is not None else frozenset()
    except OSError as error:
        walk.not_entered.append(unlistable(top, error))
        return walk
    pending: list[tuple[Path, Identities]] = [(top, inside)]
    while pending:
        folder, inside = pending.pop()
        entered = []
        for path, linked in search_folder(walk, folder, top, wanted, folded):
            identities = enter(walk, path, linked, inside, follow)
            if identities is not None:
                entered.append((path, identities))
        pending.extend(reversed(entered))
    return walk


def enter(
    walk: Walk, path: Path, linked: bool, inside: Identities, follow: Callable[[Path], bool] | None
) -> Identities | None:
    """The folders the walk is inside once it enters `path`, or None, reporting why, when it does not enter it. The
    identities are kept only when the walk may follow a link, the one way it could come back into a folder."""
    if linked and (follow is None or not follow(path)):
        walk.not_entered.append(NotEntered(path, LINKED, LINKED_REASON))
        return None
    if follow is None:
        return inside
    try:
        identity = folder_identity(path)
    except OSError as error:
        walk.not_entered.append(unlistable(path, error))
        return None
    if identity in inside:
        walk.not_entered.append(NotEntered(path, LOOP, LOOP_REASON))
        return None
    return inside | {identity}


def search_folder(
    walk: Walk, folder: Path, top: Path, wanted: Callable[[str], bool], skipped: frozenset[str]
) -> list[tuple[Path, bool]]:
    """Add the wanted files directly in `folder` to `walk`, and give its subfolders to search, in name order, each
    with whether it is a link; a folder that cannot be listed or is a nested checkout is reported and gives none."""
    try:
        with os.scandir(folder) as listing:
            entries = sorted(listing, key=lambda entry: entry.name)
    except OSError as error:
        walk.not_entered.append(unlistable(folder, error))
        return []
    if folder != top and any(entry.name.casefold() == CHECKOUT_MARKER for entry in entries):
        walk.not_entered.append(NotEntered(folder, NESTED_CHECKOUT, NESTED_CHECKOUT_REASON))
        return []
    subfolders: list[tuple[Path, bool]] = []
    for entry in entries:
        path = Path(entry.path)
        try:
            if is_folder_link(entry):
                if entry.name.casefold() not in skipped:
                    subfolders.append((path, True))
            elif entry.is_dir(follow_symlinks=False):
                if entry.name.casefold() not in skipped:
                    subfolders.append((path, False))
            elif wanted(entry.name):
                walk.files.append(path)
        except OSError as error:
            walk.not_entered.append(
                NotEntered(path, UNEXAMINED, f"the entry cannot be examined: {error.strerror or error}")
            )
    return subfolders

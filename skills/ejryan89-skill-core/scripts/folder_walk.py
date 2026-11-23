"""The one walk of a folder tree, shared by the skills that search a repository for files by name.

`walk_files(top, wanted)` gives every file below `top` whose name `wanted` accepts, and every folder it did not enter
with the reason, so each caller decides what a folder it could not search costs it. Its contract:

- Order: each folder's files, in name order, before its subfolders, which it walks in name order, depth first.
- Skipped by name: a folder whose name, compared case-insensitively, is in `skipped`, by default `SKIPPED_FOLDERS`:
  version control (`.git`, `.hg`, `.svn`), dependencies and their caches (`node_modules`, `.venv`, `venv`, `.tox`,
  `.nox`, `__pycache__`), build output (`bin`, `obj`), and Visual Studio's own state (`.vs`). Such a folder is not
  reported: the set is the statement. A caller passes another set when its search needs one, as the .NET skills add
  NuGet's `packages` (`dotnet_solution.SEARCH_SKIPPED_FOLDERS`).
- Pruned: a folder whose path the caller's `prune` accepts, such as a hidden folder, one whose name starts with a dot,
  which `is_hidden` accepts, is neither entered nor reported either, so the walk lists nothing inside it. `top` is
  never pruned.
- Nested checkouts: a folder below `top` holding a `.git` folder or file, a clone, a submodule, or a worktree, is
  another repository's, so its files are not given; it is reported as `NESTED_CHECKOUT`. `top` itself is searched.
  The `.git` entry is the one the folder gives for `.git`, as `path_keys.opens_as` asks it, so `.GIT` marks a checkout
  only where that folder folds case.
- Links: an entry that is a symbolic link, a Windows junction, or another reparse point, as `link_probe` judges one,
  and that leads to a folder or to nothing, is not entered, since it may lead out of the tree or back into it,
  and is reported as `LINKED`, unless the caller's `follow` accepts its path. Even then a link that leads
  back into a folder the walk is inside is reported as `LOOP` and not entered, so the walk always ends. `top` is
  searched as given, link or not; a link to a file or a device is an entry the caller judges. A caller that gives
  `tree` has no link entry followed out of it, not even to learn where it leads: an entry that `outside_link_refusal`
  finds leads outside `tree` is given to the caller when `wanted` accepts its name, for the caller to refuse, and is
  otherwise reported as `LINKED` with `LINKED_OUT_REASON`, folder or file, since telling them apart would follow it.
- AppleDouble sidecars: a wanted entry that `apple_double.is_sidecar` takes for the sidecar macOS writes beside a file
  or folder on a FAT, exFAT, or SMB volume, a `._<name>` file beside `<name>` in the same listing that opens with the
  AppleDouble header, is not given, since it is the system's, not the caller's. Only a regular file, by its own
  metadata and never a link, is opened to read the header, so no link is followed and no device or pipe is opened to
  decide; any other `._` entry, one whose header cannot be read included, is given as before, for the caller's own
  read to judge. Such a file is left out unreported: the rule is the statement. The look at its metadata and the open
  are two steps, as `followed_inside`'s look and a later read are, so a file replaced by a link or a pipe between them
  is opened through it: a stated limit, since a no-follow, non-blocking open is a flag only POSIX defines.
- Failures: a folder that cannot be listed is reported as `UNLISTABLE`, and an entry that cannot be examined as
  `UNEXAMINED`, each by path with the reason and the error, and the walk goes on; an error never ends it.
- Bounds: none by depth, and none by count unless the caller gives `limit`, the most folders the walk lists, `top`
  included. Past it the walk stops and reports the next folder it would have listed as `BOUNDED`, so a caller can say
  where its search stopped; that folder and every folder after it in the walk's order are not searched. Without a
  limit the walk still ends, since it enters no folder twice along one path.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

import apple_double
from link_probe import is_link_or_junction
from path_containment import within, without_extended_prefix
from path_keys import opens_as

SKIPPED_FOLDERS = frozenset(
    {".git", ".hg", ".svn", "node_modules", ".venv", "venv", ".tox", ".nox", "__pycache__", "bin", "obj", ".vs"}
)
CHECKOUT_MARKER = ".git"

# Why the walk did not enter a folder or an entry, as the contract above names each kind.
NotEnteredKind = Literal["linked", "loop", "nested-checkout", "unlistable", "unexamined", "bounded"]
LINKED: Final = "linked"
LOOP: Final = "loop"
NESTED_CHECKOUT: Final = "nested-checkout"
UNLISTABLE: Final = "unlistable"
UNEXAMINED: Final = "unexamined"
BOUNDED: Final = "bounded"

LINKED_REASON = "the folder is a junction or a symbolic link, which the search does not follow"
LINKED_OUT_REASON = (
    "the entry is a junction or a symbolic link that leads outside the examined tree, which the search does not follow"
)
# The most links `followed_inside` follows on one path, as Linux bounds one lookup, so a loop of links ends.
LINK_LIMIT = 40
NOT_FOLLOWED = "which the reader does not follow"
# A link a name came from, its path and its text, or None for a name the path itself holds.
Source = tuple[Path, Path] | None
LOOP_REASON = "the folder is a link back into a folder the search is inside, which it does not enter again"
NESTED_CHECKOUT_REASON = "the folder holds another checkout, a clone, submodule, or worktree, which the search skips"


PRESENCE_ALLOWED = {
    "search_folder": (
        "a DirEntry's is_dir, which answers from the listing and raises every error but not found, as "
        "file_presence's own listing does, so a failed stat is never read as no folder, and an entry whose test "
        "raises is recorded as not entered"
    ),
}


@dataclass(frozen=True)
class NotEntered:
    """A folder or entry the walk did not search: its path, one of the kinds above, and why, as a phrase. A folder it
    could not list and an entry it could not examine carry the error too, never compared, so a caller that must fail
    rather than pass one by raises it as it was."""

    path: Path
    kind: NotEnteredKind
    reason: str
    error: OSError | None = field(default=None, compare=False)


@dataclass
class Walk:
    """The files a walk found, in its order, and what it did not enter, in the order it met them."""

    files: list[Path] = field(default_factory=list)
    not_entered: list[NotEntered] = field(default_factory=list)


def is_folder_link(entry: os.DirEntry[str]) -> bool:
    """Whether a listed entry is a link the walk does not enter: a symbolic link, a junction, or another reparse point,
    as `link_probe` judges one from the entry's own metadata, that leads to a folder or to nothing. A link whose
    target is gone counts too, on every platform, since nothing shows it leads to a file; a link to a file or a device
    is given to the caller, which judges it."""
    if not is_link_or_junction(entry.path, entry.stat(follow_symlinks=False)):
        return False
    try:
        return stat.S_ISDIR(entry.stat().st_mode)
    except FileNotFoundError:
        return True


def followed_inside(path: Path, tree: Path) -> tuple[Path | None, str | None]:
    """Where `path` leads inside `tree`, as the system follows it, or why it is not followed: each name down from the
    tree is judged from its own metadata (`link_probe`) and a link from its text alone, a `..` stepping back from where
    the name before it leads, so nothing outside the tree is stat, listed, or opened to decide. The answer is the path
    it leads to and None, or None and the refusal: a link, or a `..` a link's text holds, that leads outside the tree,
    naming the link whose text it is; a name whose metadata cannot be read, with the operating system's reason; a link
    whose text cannot be read; an absolute target naming the tree through `.` or `..` or another link; and more than
    LINK_LIMIT links. A name that is not there ends the look, the rest joined as written, since nothing past it can be
    followed. A path whose own leading parts do not name the tree, read as written, or whose own `..` climbs out of it
    with no link before it, answers None and None: `within`'s question, not this one. The look and a later read are two
    steps, so a name replaced by a link between them is read through, and a hard link is the file itself, read as a file
    of the tree wherever its other names are."""
    root = tree.absolute()
    below = names_below(path.absolute(), root)
    if below is None:
        return None, None
    pending: list[tuple[str, Source]] = [(name, None) for name in below]
    current, depth, links = root, 0, 0
    while pending:
        name, source = pending.pop(0)
        if name == "..":
            if depth == 0:
                # A `..` the path itself holds leaves the tree by its own text, which is `within`'s question.
                return (None, None) if source is None else (None, leads_outside(source, root))
            current, depth = current.parent, depth - 1
            continue
        candidate = current / name
        try:
            linked = is_link_or_junction(candidate)
        except (FileNotFoundError, NotADirectoryError):
            return candidate.joinpath(*(rest for rest, _ in pending)), None
        except OSError as error:
            # Its stat fails as the reader's would, so the refusal gives the operating system's reason as the read did.
            return None, error.strerror or str(error)
        if not linked:
            current, depth = candidate, depth + 1
            continue
        links += 1
        if links > LINK_LIMIT:
            return None, f"more than {LINK_LIMIT} links lie on the way to it, {NOT_FOLLOWED}"
        try:
            written = candidate.readlink()
        except OSError as error:
            return None, f"{candidate.as_posix()} is a link whose target cannot be read: {error.strerror or error}"
        target = without_extended_prefix(written)
        followed = names_below(target, root) if target.anchor else list(target.parts)
        if followed is None:
            return None, leads_outside((candidate, written), root)
        if target.anchor:
            current, depth = root, 0
        pending = [*((followed_name, (candidate, written)) for followed_name in followed), *pending]
    return current, None


def outside_link_refusal(path: Path, tree: Path) -> str | None:
    """Why the file at `path`, inside `tree`, is not stat or read: `followed_inside`'s refusal, or None for a path
    whose links all stay inside the tree, one with a name that is not there, whose reader then reports it, and one
    whose leading parts do not name the tree, which `refusal` judges."""
    return followed_inside(path, tree)[1]


def names_below(path: Path, root: Path) -> list[str] | None:
    """The names an absolute `path` holds below `root`, as written, `..` included, or None when its leading parts do
    not name `root`: a relative path, one shorter than `root`, one whose leading parts hold a `.` or `..`, which would
    need the folders above `root` looked up to place, or one `within` finds elsewhere."""
    target = without_extended_prefix(path)
    head = target.parts[: len(root.parts)]
    if not target.is_absolute() or len(head) < len(root.parts) or {".", ".."} & set(head):
        return None
    return list(target.parts[len(root.parts) :]) if within(Path(*head), root) else None


def leads_outside(source: tuple[Path, Path], root: Path) -> str:
    """The refusal of a path whose way leads out of `root` through the link `source`, naming it and its text."""
    link, written = source
    outside = f"outside the examined tree {root.as_posix()}, {NOT_FOLLOWED}"
    return f"{link.as_posix()} is a link to {without_extended_prefix(written).as_posix()}, {outside}"


def leaves(entry: os.DirEntry[str], tree: Path) -> bool:
    """Whether a listed entry is a link, judged from its own metadata, that `outside_link_refusal` refuses inside
    `tree`, so the walk neither follows it nor stats what it leads to."""
    own = entry.stat(follow_symlinks=False)
    return is_link_or_junction(entry.path, own) and outside_link_refusal(Path(entry.path), tree) is not None


def is_hidden(path: Path) -> bool:
    """Whether `path` is hidden, its name starting with a dot, as a tool's state or cache folder is: the prune a
    caller passes to skip hidden folders, and the test of an entry's name the walk could not examine."""
    return path.name.startswith(".")


def folder_identity(path: Path) -> tuple[int, int]:
    """The device and file number of the folder `path` leads to, the same through any link to it."""
    status = path.stat()
    return status.st_dev, status.st_ino


def bounded_reason(limit: int) -> str:
    """Why a walk with `limit` did not enter the folder it reports as `BOUNDED`."""
    return f"the search stopped at its bound of {limit} folders, before this folder and the folders after it"


def unlistable(path: Path, error: OSError) -> NotEntered:
    return NotEntered(path, UNLISTABLE, f"the folder cannot be listed: {error.strerror or error}", error)


Identities = frozenset[tuple[int, int]]


def walk_files(
    top: Path,
    wanted: Callable[[str], bool],
    *,
    skipped: frozenset[str] = SKIPPED_FOLDERS,
    prune: Callable[[Path], bool] | None = None,
    follow: Callable[[Path], bool] | None = None,
    limit: int | None = None,
    tree: Path | None = None,
) -> Walk:
    """Every file below `top` whose name `wanted` accepts, and every folder not entered, as the module states."""
    walk = Walk()
    folded = frozenset(name.casefold() for name in skipped)

    def excluded(path: Path) -> bool:
        return path.name.casefold() in folded or (prune is not None and prune(path))

    try:
        inside: Identities = frozenset({folder_identity(top)}) if follow is not None else frozenset()
    except OSError as error:
        walk.not_entered.append(unlistable(top, error))
        return walk
    pending: list[tuple[Path, Identities]] = [(top, inside)]
    listed = 0
    while pending:
        folder, inside = pending.pop()
        if limit is not None and listed >= limit:
            walk.not_entered.append(NotEntered(folder, BOUNDED, bounded_reason(limit)))
            break
        listed += 1
        entered = []
        for path, linked in search_folder(walk, folder, top, wanted, excluded, tree):
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


def is_checkout_marker(name: str, folder: Path) -> bool:
    """Whether `name` in `folder` is the entry git finds there as `.git`, as `path_keys.opens_as` asks the folder. The
    folder is asked only about a name that is `.git` in some case or form, so the walk asks no folder holding none."""
    return opens_as(folder / name, CHECKOUT_MARKER)


def is_sidecar_entry(entry: os.DirEntry[str], names: Collection[str]) -> bool:
    """Whether a listed entry is an AppleDouble sidecar the walk does not give: a regular file by its own metadata, not
    a link or another reparse point as `link_probe` judges one, that `apple_double.is_sidecar` takes for one given its
    folder's `names`. Only such a file is opened, so a header is never read through a link, a device, or a pipe."""
    if not entry.name.startswith(apple_double.PREFIX):
        return False
    own = entry.stat(follow_symlinks=False)
    if not stat.S_ISREG(own.st_mode) or is_link_or_junction(entry.path, own):
        return False
    return apple_double.is_sidecar(Path(entry.path), names)


def search_folder(
    walk: Walk,
    folder: Path,
    top: Path,
    wanted: Callable[[str], bool],
    excluded: Callable[[Path], bool],
    tree: Path | None = None,
) -> list[tuple[Path, bool]]:
    """Add the wanted files directly in `folder` to `walk`, but AppleDouble sidecars (`is_sidecar_entry`), and give
    its subfolders to search, in name order, each with whether it is a link, but those `excluded` skips or prunes; a
    folder that cannot be listed or is a nested checkout is reported and gives none. With `tree`, a link entry leading
    outside it is never followed (`leaves`)."""
    try:
        with os.scandir(folder) as listing:
            entries = sorted(listing, key=lambda entry: entry.name)
    except OSError as error:
        walk.not_entered.append(unlistable(folder, error))
        return []
    if folder != top and any(is_checkout_marker(entry.name, folder) for entry in entries):
        walk.not_entered.append(NotEntered(folder, NESTED_CHECKOUT, NESTED_CHECKOUT_REASON))
        return []
    names = frozenset(entry.name for entry in entries)
    subfolders: list[tuple[Path, bool]] = []
    for entry in entries:
        path = Path(entry.path)
        try:
            if tree is not None and leaves(entry, tree):
                if wanted(entry.name):
                    walk.files.append(path)
                elif not excluded(path):
                    walk.not_entered.append(NotEntered(path, LINKED, LINKED_OUT_REASON))
            elif is_folder_link(entry):
                if not excluded(path):
                    subfolders.append((path, True))
            elif entry.is_dir(follow_symlinks=False):
                if not excluded(path):
                    subfolders.append((path, False))
            elif wanted(entry.name) and not is_sidecar_entry(entry, names):
                walk.files.append(path)
        except OSError as error:
            walk.not_entered.append(
                NotEntered(path, UNEXAMINED, f"the entry cannot be examined: {error.strerror or error}", error)
            )
    return subfolders

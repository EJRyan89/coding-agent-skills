"""The one reader of a tree a skill promises to read only inside, for the .NET skills.

dotnet-format promises to read only inside the repository, and keeps the promise through a `TreeReader` bound to the
repository root: every stat, open, read, listing, and walk of a path in its scripts goes through one, and validation
refuses a direct read there (the tree read policy in `tests/validation/fsops_platform.py`), so a caller that bypasses
the reader fails validation rather than waiting for a review to find it. dotnet-dependencies, which promises to read
only inside the tree it examines, is brought to the reader and the policy in a change of its own.

Each call judges its path before it touches it (`refusal`), from the path's text and then by its links:

- by its text, as `dotnet_solution.refusal` judges it: one outside the tree, or written as a network share or a device,
  is refused unopened. A segment Windows reserves as a device, such as `Con.cs`, is not refused here, since the
  reader also reads names a listing or git gives, which a tree on Linux or macOS may hold; a caller judging a path a
  file names, such as a solution's entry, asks `dotnet_solution.refusal` itself first, which refuses one;
- by its links, as `folder_walk.followed_inside` follows them: each name down from the tree is judged from its own
  metadata and each link from its text, so a link, or a `..` a link's text holds, that leads outside the tree is
  refused with nothing outside it stat, listed, or opened to decide, and so is a name whose metadata cannot be read.
  The path is judged both as written, each `..` taken after following the link before it, as Linux and macOS open it,
  and normalized as text, as Windows opens it, on every platform, so a path either reading leads out of the tree is
  refused wherever it runs, and so is one whose `..` climbs above the tree once the links before it are followed.

A refused path raises `TreeRefused`, an `OSError` naming the path and the reason as `<path> cannot be examined:
<reason>`, the words both skills use for a path they do not touch, so a caller that turns an `OSError` into its own
refusal refuses this one alike, and a caller that words its own refusal takes `reason`. A path the reader lets through
is then read as `file_presence` and `dotnet_solution` read one: presence lets a failed stat through, and a file's bytes
are read within `dotnet_solution.READ_LIMIT_BYTES` from a regular file only.

`occupied` and `is_link` ask about a place itself, never what a link there leads to, so they judge the folder holding it
and examine the name from its own metadata, as MSBuild's `Exists()` and a writer's check of a link ask. `key` is
`dotnet_solution.entry_key`: the path as the file system compares it (`same_path`) only inside the tree with no link
leading out, and its text otherwise, since the key's case probe lists each folder on the path.

Limits: deciding whether a path lies in the tree asks the tree's own folders, and the folders above it, how they compare
names (`path_keys`), which lists each and looks up one entry's own metadata without following it, so a sibling of the
tree, or of a folder above it, may be looked up; the look at a path's links and its read are two steps, so a name
replaced by a link between them is read through; a hard link is the file itself, read as a file of the tree wherever its
other names lie; and a link whose absolute target names a place inside the tree through another spelling of it, such as
macOS's /tmp for /private/tmp, is refused, though it leads inside.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import file_presence
from atomic_file import is_read_only
from dotnet_solution import decode_text, entry_key, lexical_absolute, read_bounded, refusal
from folder_walk import SKIPPED_FOLDERS, Walk, followed_inside, walk_files
from folders_up import folders_up
from link_probe import is_link_or_junction

# Why a path is refused when a `..` in it, taken after the links before it are followed, climbs above the tree's top.
CLIMBS_OUT = "a `..` in it climbs above the examined tree once the links before it are followed"


class TreeRefused(OSError):
    """A path the reader does not touch: `path` and `reason`, which the message joins as `<path> cannot be examined:
    <reason>`."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path} cannot be examined: {reason}")
        self.path = path
        self.reason = reason


class TreeReader:
    """A reader bound to `tree`, the tree a skill examines, made absolute without reaching the file system, which reads
    nothing outside it, as the module states."""

    def __init__(self, tree: Path) -> None:
        self.tree = lexical_absolute(tree)

    def refusal(self, path: Path) -> str | None:
        """Why the reader does not touch `path`, judged by its text and then by its links as the module states, or None
        for a path whose links all stay inside the tree, and one with a name that is not there, whose read then finds
        nothing."""
        absolute = lexical_absolute(path)
        if (refused := refusal(absolute, self.tree, devices=False)) is not None:
            return refused
        for named in dict.fromkeys((path.absolute(), absolute)):
            inside, linked = followed_inside(named, self.tree)
            if linked is not None:
                return linked
            if inside is None:
                return CLIMBS_OUT
        return None

    def judged(self, path: Path) -> Path:
        """`path`, or `TreeRefused` when `refusal` refuses it."""
        if (refused := self.refusal(path)) is not None:
            raise TreeRefused(path, refused)
        return path

    def present(self, path: Path, *, other_is_absent: bool = False) -> bool:
        """`file_presence.is_present` of a judged path: whether a regular file is there, following links."""
        return file_presence.is_present(self.judged(path), other_is_absent=other_is_absent)

    def folder(self, path: Path, *, other_is_absent: bool = False) -> bool:
        """`file_presence.is_directory` of a judged path: whether a folder is there, following links."""
        return file_presence.is_directory(self.judged(path), other_is_absent=other_is_absent)

    def there(self, path: Path) -> bool:
        """`file_presence.is_there` of a judged path: whether anything a reader meets is there."""
        return file_presence.is_there(self.judged(path))

    def occupied(self, path: Path) -> bool:
        """`file_presence.is_occupied`: whether anything takes the place `path` names, a link to nothing included. The
        folder holding it is judged, and the place is examined from its own metadata, never through a link there."""
        self.judged(path.parent)
        return file_presence.is_occupied(path)

    def is_link(self, path: Path) -> bool:
        """Whether `path` is a symbolic link, a junction, or another reparse point, judged from its own metadata
        (`link_probe`) after the folder holding it is judged, so the link is never followed."""
        self.judged(path.parent)
        return is_link_or_junction(path)

    def missing_reason(self, path: Path, *, folder: bool = False) -> str | None:
        """`file_presence.refusal_reason` of a judged path, or `cannot be examined: <reason>` for a refused one, in the
        words `refusal_reason` gives a path a stat cannot examine."""
        if (refused := self.refusal(path)) is not None:
            return f"cannot be examined: {refused}"
        return file_presence.refusal_reason(path, folder=folder)

    def require(
        self, path: Path, error: Callable[[str], Exception], missing: str, subject: str, *, folder: bool = False
    ) -> None:
        """`file_presence.require` of a judged path: return when a regular file, or with `folder` a folder, is there;
        otherwise raise the caller's `error`, with `missing` when nothing is there, and with `subject` followed by
        `missing_reason`'s reason when something else is, a stat cannot examine it, or the reader refuses it."""
        reason = self.missing_reason(path, folder=folder)
        if reason == file_presence.MISSING:
            raise error(missing)
        if reason is not None:
            raise error(f"{subject} {reason}")

    def status(self, path: Path) -> os.stat_result:
        """The status of what a judged path leads to, following links."""
        return self.judged(path).stat()

    def read_only(self, path: Path) -> bool:
        """`atomic_file.is_read_only` of a judged path: whether it, or the file a link there leads to, is read-only."""
        return is_read_only(self.judged(path))

    def contents(self, path: Path) -> bytes:
        """The bytes of the regular file a judged path leads to, within `dotnet_solution.read_bounded`'s bound."""
        return read_bounded(self.judged(path))

    def text(self, path: Path) -> str:
        """The text of an MSBuild, solution, or configuration file at a judged path, as `dotnet_solution.read_text`
        decodes one."""
        return decode_text(self.contents(path))

    def listing(self, folder: Path) -> list[Path]:
        """The entries of a judged folder, in path order; a folder it cannot list raises the listing's `OSError`. An
        entry is only named, never examined: a caller judges each one it reads through the reader again."""
        return sorted(self.judged(folder).iterdir())

    def search(
        self,
        top: Path,
        wanted: Callable[[str], bool],
        *,
        skipped: frozenset[str] = SKIPPED_FOLDERS,
        prune: Callable[[Path], bool] | None = None,
        limit: int | None = None,
    ) -> Walk:
        """`folder_walk.walk_files` below a judged `top`, given the tree, so it follows no link entry out of it, not
        even to learn what it is."""
        return walk_files(self.judged(top), wanted, skipped=skipped, prune=prune, limit=limit, tree=self.tree)

    def climb(self, start: Path) -> Iterator[Path]:
        """`start`, a judged path inside the tree, and each folder above it up to the tree's top, nearest first, keyed
        as `folders_up` keys them, so no folder above the tree is listed to compare it."""
        return folders_up(self.judged(start), self.tree)

    def key(self, path: Path) -> str:
        """What a collection compares `path` by, `dotnet_solution.entry_key` in the tree: `same_path` for a path inside
        it that no link leads out of, and its text, made absolute without reaching the file system, for any other,
        since the key's case probe lists the folders on the path."""
        return entry_key(path, self.tree)

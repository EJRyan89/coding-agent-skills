"""The one form a path or a name is compared in, so two spellings of one path compare equal as its file system reads
them.

A path's key is the path made absolute against the working directory and normalized, so `a/../b` and `b` are one
path, then each name in it folded as the directory holding that name compares names. Case sensitivity belongs to the
file system, not the platform: NTFS and macOS's default APFS fold case, while Linux's file systems, an APFS volume
formatted case-sensitive, and a Windows directory marked case-sensitive do not. So each directory on the path is asked
through `folds_case`, and a name in one that folds is case-folded, so `C:\\Work` and `c:\\work` are one path on NTFS
and `/Users/you/Work` and `/users/YOU/work` are one on APFS, while a case-sensitive directory keeps them apart. The
anchor, a drive letter or a network share's name, is folded on every platform, since no file system holds it.

A name is also composed to Unicode's NFC form on every file system. APFS and HFS+ ignore the difference between a
composed and a decomposed spelling, so `é` written as one code point or as `e` and a combining accent names one file
there; NTFS and Linux's file systems keep them apart, where the key merges two names that differ only in it. A
directory can rarely be asked, since it seldom holds a name the two forms spell differently, so the key composes
everywhere, the answer that keeps a path out of a protected folder in every spelling the file system accepts.

Asking never leaves the path's own text or reaches past it: a key asks only the directories it reaches from the anchor
without following a symbolic link or junction, never the path's last name, and nothing under a network or device
anchor, a path that starts with two separators, so it contacts no host and no link leads it elsewhere. A name below
the last directory asked takes that directory's answer, as a path that does not exist yet is created there, and where
no directory can be asked, as under a network share, case is folded, the answer that refuses more.

By default the key is lexical: `..` is folded by text and no link in the path is followed, so it decides before
anything opens the path; with `resolve`, every link is followed first, so a path and a link to it are one. With
`textual`, the lexical key is made by text alone, the working directory joined and `.` and `..` folded, with none of
the rewriting Windows' own path parsing adds, such as dropping a name's trailing dots and spaces, so a check that must
judge exactly the path it was given, as the reviewer guard does, compares that.

`name_key` is the form a single name is compared in within a directory, and `same_parts` compares two paths' parts as
written, folded as the directories of the second, trusted path compare them, which `path_containment.within` and the
reviewer guard's exact-spelling match use so that the path being judged is never itself asked. The deployer reaches
these through `platform_support`, its platform seam, and skill scripts and `tools/` import them here.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Sequence
from pathlib import Path, PurePath

from link_probe import is_plain_directory

# The presence reads here that read a path they cannot stat or list as nothing there, each with why that is the answer
# it should give.
PRESENCE_ALLOWED = {
    "_asked_folds_case": "None is its answer when no entry can tell whether the file system folds case, and an entry "
    "it cannot stat is passed for the next, which the caller reads as the answer of the directory above",
}
# The separators a network or device anchor opens with two of: `\\host\share`, `//host/share`, `\\?\`, and `\\.\` on
# Windows, and `//host` where POSIX leaves a path opening with two to the system.
SEPARATORS = "\\/"

# Each directory's answer to folds_case once its file system has given one, by its absolute spelling.
_FOLDS_CASE: dict[str, bool] = {}


def fold(name: str, folds: bool) -> str:
    """`name` as a directory compares it: composed to NFC, and case-folded first when the directory `folds` case.

    Folding decomposes before it folds and composes after, as Unicode's canonical caseless match does, so two
    spellings of one character compare equal whatever form each was written in."""
    if folds:
        name = unicodedata.normalize("NFD", name).casefold()
    return unicodedata.normalize("NFC", name)


def folds_case(directory: str | os.PathLike[str]) -> bool:
    """Whether the file system compares the names in `directory` without regard to case.

    It is asked of an entry whose name has an ASCII letter: the entry is looked up again by the name with each such
    letter's case swapped, and the directory folds when that finds the same file. The answer is kept for the rest of
    the run. A directory that cannot be listed, does not exist, or holds no such entry yet takes the answer of the
    nearest directory above it that gives one, on whose file system it is made, and is asked again next time; one
    under a network or device anchor is never asked, and where nothing gives an answer, case is folded, the answer that
    refuses more.
    """
    current = os.path.abspath(directory)  # noqa: PTH100 - a lexical key, as derived paths are
    if not _asks(PurePath(current).anchor):
        return True
    while True:
        answer = _answer(current)
        if answer is not None:
            return answer
        parent = os.path.dirname(current)  # noqa: PTH120 - the same lexical text, one name shorter
        if parent == current:
            return True
        current = parent


def name_key(name: str, directory: str | os.PathLike[str] | None = None) -> str:
    """The form in which the file system compares a name in `directory`: two names with one key are one file there.

    `directory` is asked through folds_case, and a name in one that does not fold keeps its case. Without a directory,
    case is folded, the answer that refuses more names as one. Either way the name is composed to NFC."""
    return fold(name, directory is None or folds_case(directory))


def part_folds(parts: Sequence[str]) -> tuple[bool, ...]:
    """For each of an absolute path's `parts`, its anchor first, whether the directory holding it folds case: the
    anchor folds; each name takes the answer of the directory before it, asked while the walk reaches it from the
    anchor without a link, and otherwise the last answer given."""
    anchor = parts[0]
    asking = _asks(anchor)
    folds = _answer_or(anchor, True) if asking else True
    result = [True]
    directory = anchor
    for index, name in enumerate(parts[1:], start=1):
        result.append(folds)
        if not asking or index == len(parts) - 1:
            continue
        directory = os.path.join(directory, name)  # noqa: PTH118 - text, as the parts were
        asking = is_plain_directory(directory)
        if asking:
            folds = _answer_or(directory, folds)
    return tuple(result)


def same_parts(parts: Sequence[str], reference: Sequence[str]) -> bool:
    """Whether `parts`, an absolute path's parts from its anchor, name what `reference`'s parts name, each compared as
    the directory holding the reference's part compares names, and neither path normalized. Only `reference` is
    asked, so a caller passes the trusted path there and the path it judges in `parts`. A relative `reference` names
    nothing to compare against, so no path matches it, and nothing is asked relative to the working directory."""
    if len(parts) != len(reference) or not PurePath(*reference).is_absolute():
        return False
    folds = part_folds(reference)
    return all(
        fold(given, folded) == fold(known, folded) for given, known, folded in zip(parts, reference, folds, strict=True)
    )


def path_key(path: str | os.PathLike[str], *, resolve: bool = False, textual: bool = False) -> str:
    """`path` as its file system compares it: absolute and normalized, lexically or with every link followed when
    `resolve` is true, by text alone when `textual` is, each name composed and case-folded where its directory ignores
    case. A key cannot be both resolved and textual, so asking for both raises ValueError."""
    if resolve and textual:
        raise ValueError("a path key is resolved or textual, not both")
    if textual:
        given = Path(path)
        absolute = os.path.normpath(given if given.is_absolute() else Path.cwd() / given)
    else:
        # Path.resolve would follow links where `resolve` is false, and Path.absolute keeps "..".
        absolute = os.path.realpath(path) if resolve else os.path.abspath(path)  # noqa: PTH100 - lexical, as above
    parts = PurePath(absolute).parts
    folded = [fold(part, folds) for part, folds in zip(parts, part_folds(parts), strict=True)]
    return os.path.join(*folded)  # noqa: PTH118 - a key is text


def _asks(anchor: str) -> bool:
    """Whether a directory under `anchor` may be asked: not under a network or device anchor."""
    return not (len(anchor) >= 2 and anchor[0] in SEPARATORS and anchor[1] in SEPARATORS)


def _answer_or(directory: str, otherwise: bool) -> bool:
    answer = _answer(directory)
    return otherwise if answer is None else answer


def _answer(directory: str) -> bool | None:
    """`directory`'s kept answer, or its file system's, kept once given, or None when it gives none."""
    if directory in _FOLDS_CASE:
        return _FOLDS_CASE[directory]
    answer = _asked_folds_case(directory)
    if answer is not None:
        _FOLDS_CASE[directory] = answer
    return answer


def _asked_folds_case(directory: str) -> bool | None:
    """The file system's answer for `directory`, from its first entry that can give one, or None when none can. Any
    entry serves, since one directory compares all its names alike, so the listing stops at the first."""
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                swapped = "".join(
                    character.swapcase() if character.isascii() else character for character in entry.name
                )
                if swapped == entry.name:
                    continue
                try:
                    found = os.lstat(entry.path)
                except OSError:
                    continue  # removed meanwhile
                try:
                    other = os.lstat(os.path.join(directory, swapped))  # noqa: PTH118 - text, as the entry's path is
                except FileNotFoundError:
                    return False
                except OSError:
                    continue
                return (other.st_dev, other.st_ino) == (found.st_dev, found.st_ino)
    except OSError:
        return None
    return None

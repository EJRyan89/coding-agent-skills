"""The one form a path or a name is compared in, so two spellings of one path compare equal as its file system reads
them.

A path's key is the path made absolute against the working directory and normalized, so `a/../b` and `b` are one path,
then each name in it folded as the directory holding that name compares names. Case sensitivity belongs to the file
system, not the platform: NTFS and macOS's default APFS fold case, while Linux's file systems, an APFS volume formatted
case-sensitive, and a Windows directory marked case-sensitive do not. So each directory on the path is asked through
`folds_case`, and a name in one that folds is folded one letter to one letter by each letter's simple uppercase, as
NTFS's upcase table compares names, so `C:\\Work` and `c:\\work` are one path on NTFS and `/Users/you/Work` and
`/users/YOU/work` are one on APFS, while a case-sensitive directory keeps them apart. A letter whose case mapping does
not lead back to it, such as the Kelvin sign, whose lowercase is `k` but whose uppercase is itself, is kept, as NTFS
keeps it apart from `K`. The anchor, a drive letter or a network share's name, is folded on every platform, since no
file system holds it.

A key also composes each name to Unicode's NFC form on every file system. APFS and HFS+ ignore the difference
between a composed and a decomposed spelling, so `é` written as one code point or as `e` and a combining accent names
one file there; NTFS and Linux's file systems keep them apart, where the key merges two names that differ only in it.
A directory can rarely be asked, since it seldom holds a name the two forms spell differently, and a key is only
compared, so it composes everywhere. `same_parts`, which decides containment and the reviewer guard's exact match, has
the trusted path's own entry in hand, so it asks: it takes the two forms as one only where that directory finds one
entry under both, never two hard links listed under each, and keeps them apart where it does not or cannot be asked,
so a spelling that names another folder on NTFS or Linux is never taken for an admitted one.

Asking never leaves the path's own text or reaches past it: a key asks only the directories it reaches from the anchor
without following a symbolic link or junction, never the path's last name, and nothing under a network or device
anchor, a path that starts with two separators, so it contacts no host and no link leads it elsewhere. A name below
the last directory asked takes that directory's answer, as a path that does not exist yet is created there. Where no
directory answers, as under a network share, a key folds case, the answer that refuses more names as one, while
`same_parts` compares the names there as written, the answer that admits less, since a share may keep case.

By default the key is lexical: `..` is folded by text and no link in the path is followed, so it decides before
anything opens the path; with `resolve`, every link is followed first, so a path and a link to it are one. With
`textual`, the lexical key is made by text alone, the working directory joined and `.` and `..` folded, with none of
the rewriting Windows' own path parsing adds, such as dropping a name's trailing dots and spaces, so a check that must
judge exactly the path it was given, as the reviewer guard does, compares that.

`name_key` is the form a single name is compared in within a directory, and `same_parts` compares two paths' parts as
written, folded as the directories of the second, trusted path compare them, which `path_containment.within` and the
reviewer guard's exact-spelling match use so that the path being judged is never itself asked. `same_parts` folds only
ASCII letters by itself, which the probe in `folds_case` has shown the directory folds; two parts that differ in the
case of any other letter, as they differ in normalization form, are one only where the trusted directory finds one
entry under both, so no table of this module's own decides that a spelling names an admitted folder. The deployer
reaches these through `platform_support`, its platform seam, and skill scripts and `tools/` import them here.
"""

from __future__ import annotations

import os
import stat
import string
import unicodedata
from collections.abc import Sequence
from pathlib import Path, PurePath

from link_probe import is_plain_directory
from ordinal_case import simple_uppercase

# The presence reads here that read a path they cannot stat or list as nothing there, each with why that is the answer
# it should give.
PRESENCE_ALLOWED = {
    "_asked_folds_case": "None is its answer when no entry can tell whether the file system folds case, and an entry "
    "it cannot stat is passed for the next, which the caller reads as the answer of the directory above",
    "_one_entry": "a directory it cannot list or a name it cannot stat is not shown to hold the trusted entry under "
    "both names, so the two spellings stay apart, the answer an exact-spelling match and an admitting containment "
    "check refuse on",
    "_still_there": "an entry it cannot stat any more is passed over, so the probe reads no case rule from a lookup "
    "that missed a vanished entry and asks the next entry instead",
}
# The separators a network or device anchor opens with two of: `\\host\share`, `//host/share`, `\\?\`, and `\\.\` on
# Windows, and `//host` where POSIX leaves a path opening with two to the system.
SEPARATORS = "\\/"

# The ASCII letters' case mapping, the one case rule `folds_case`'s probe asks a directory about.
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)

# Each directory's answer to folds_case once its file system has given one, by its absolute spelling.
_FOLDS_CASE: dict[str, bool] = {}


def fold(name: str, folds: bool) -> str:
    """`name` as a directory compares it: composed to NFC, and case-folded first when the directory `folds` case.

    Folding decomposes before it folds and composes after, so two spellings of one character compare equal whatever
    form each was written in. It folds each letter by `_folded`, as NTFS's upcase table maps one letter to one letter,
    never by `str.lower`, which takes the Kelvin sign to `k`, or `str.casefold` or `str.upper`, which expand `ß` to
    `ss`, each merging two folders NTFS keeps apart."""
    if folds:
        name = "".join(_folded(character) for character in unicodedata.normalize("NFD", name))
    return unicodedata.normalize("NFC", name)


def folds_case(directory: str | os.PathLike[str]) -> bool:
    """Whether the file system compares the names in `directory` without regard to case.

    It is asked of an entry whose name has an ASCII letter: the entry is looked up again by the name with each such
    letter's case swapped, and the directory folds when that finds the same file, the swapped name is not listed as
    an entry of its own, and the file has no second link, so two hard links never pass for one name. The answer is
    kept for the rest of the run. A directory that cannot be listed, does not exist, or holds no such entry yet takes
    the answer of the nearest directory above it that gives one, on whose file system it is made, and is asked again
    next time; one under a network or device anchor is never asked, and where nothing gives an answer, case is folded,
    the answer that refuses more names as one, which `same_parts` does not take for an admitting comparison.
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

    `directory` is asked through folds_case, and a name in one that folds is folded one letter to one letter by `fold`,
    as that directory compares it, so `Straße` and `Strasse` are two names there; a name in one that does not fold
    keeps its case. Without a directory, case is folded, the answer that refuses more names as one, and folded fully,
    as `str.casefold` does, so `ß` and `ss` are one name too: a collision check refuses rather than merge nothing.
    Either way the name is composed to NFC."""
    if directory is not None:
        return fold(name, folds_case(directory))
    return unicodedata.normalize("NFC", unicodedata.normalize("NFD", name).casefold())


def part_folds(parts: Sequence[str]) -> tuple[bool, ...]:
    """For each of an absolute path's `parts`, its anchor first, whether the directory holding it folds case: the
    anchor folds; each name takes the answer of the directory before it, asked while the walk reaches it from the
    anchor without a link, and otherwise the last answer given; where none was given, as under a network share, it
    folds, the answer that refuses more names as one."""
    return tuple(True if folds is None else folds for folds, _ in _walk(parts))


def same_parts(parts: Sequence[str], reference: Sequence[str]) -> bool:
    """Whether `parts`, an absolute path's parts from its anchor, name what `reference`'s parts name, part by part,
    neither path normalized. Each part is compared as the directory holding the reference's part compares names: its
    ASCII letters case-folded where it folds case, and two spellings that differ in the case of any other letter or in
    Unicode normalization taken as one only when that directory, reached without a link, finds one entry under both,
    as APFS does for the forms; where it keeps them apart, as NTFS does the Kelvin sign and `K` and NTFS and Linux's
    file systems do the forms, or cannot be asked, the two must be spelled alike. Under an anchor no directory answers
    for, as a network share, names are compared as written, since folding there would admit a spelling a share that
    keeps case takes as another folder. Only `reference`'s directories are asked, and of a name only the one
    canonically equivalent to the reference's own, so a caller passes the trusted path there and the path it judges in
    `parts`. A relative `reference` names nothing to compare against, so no path matches it, and nothing is asked
    relative to the working directory."""
    if len(parts) != len(reference) or not PurePath(*reference).is_absolute():
        return False
    for index, (given, known, (answer, asked)) in enumerate(zip(parts, reference, _walk(reference), strict=True)):
        folds = bool(answer)
        if _cased(given, folds) == _cased(known, folds):
            continue
        if fold(given, folds) != fold(known, folds):
            return False
        if not (asked and _one_entry(os.path.join(*reference[:index]), given, known, folds)):  # noqa: PTH118 - text
            return False
    return True


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


def _walk(parts: Sequence[str]) -> list[tuple[bool | None, bool]]:
    """For each of an absolute path's `parts`, whether the directory holding it folds case, None where no directory
    on the way has answered, and whether that directory was reached from the anchor without a link and so may be
    asked; the anchor itself folds and is held by nothing."""
    anchor = parts[0]
    asking = _asks(anchor)
    folds = _answer(anchor) if asking else None
    result: list[tuple[bool | None, bool]] = [(True, False)]
    directory = anchor
    for index, name in enumerate(parts[1:], start=1):
        result.append((folds, asking))
        if not asking or index == len(parts) - 1:
            continue
        directory = os.path.join(directory, name)  # noqa: PTH118 - text, as the parts were
        asking = is_plain_directory(directory)
        if asking:
            folds = _answer_or(directory, folds)
    return result


def _folded(character: str) -> str:
    """`character` as a directory that folds case compares it, after NTFS's upcase table: its simple uppercase, as
    `ordinal_case.simple_uppercase` gives .NET's ordinal comparison, shown as the lowercase letter that maps back to
    it. A character whose simple uppercase cannot be told is kept, as is one whose full uppercase is more than one
    letter, as `ß`'s is `SS`, and an uppercase whose lowercase does not map back to it, as the Kelvin sign's `k` maps
    to `K`, so none merges with a letter NTFS keeps apart from it."""
    upper = simple_uppercase(character) or character
    lower = upper.lower()
    return lower if len(lower) == 1 and simple_uppercase(lower) == upper else upper


def _cased(name: str, folds: bool) -> str:
    """`name` with its ASCII letters lowercased where its directory `folds`, and otherwise as written, with no
    normalization: the case rule the probe in `folds_case` shows, and no other letter's, which `same_parts` asks the
    directory about instead."""
    return name.translate(_ASCII_LOWER) if folds else name


def _one_entry(directory: str, given: str, known: str, folds: bool) -> bool:
    """Whether `directory`, which compares names as `folds` says, finds one entry under both names, neither followed
    if a link; False when either is missing or cannot be read, so a spelling the directory cannot show to be the known
    one is not taken for it.

    Two hard links are two entries that share one file, so finding one file under both names shows one entry only
    where the listing holds no separate entry under each spelling, and the file has no second link: a second link
    made after the listing was read is reached by its own spelling and listed nowhere this saw."""
    try:
        listed = {_cased(name, folds) for name in _names(directory)}
        known_entry = os.lstat(os.path.join(directory, known))  # noqa: PTH118 - text, as the parts were
        given_entry = os.lstat(os.path.join(directory, given))  # noqa: PTH118 - text, as the parts were
    except OSError:
        return False
    if {_cased(given, folds), _cased(known, folds)} <= listed or _hard_linked(given_entry):
        return False
    return (given_entry.st_dev, given_entry.st_ino) == (known_entry.st_dev, known_entry.st_ino)


def _names(directory: str) -> set[str]:
    """The names `directory` lists, as its file system spells them; OSError when it cannot be listed."""
    with os.scandir(directory) as listing:
        return {entry.name for entry in listing}


def _hard_linked(status: os.stat_result) -> bool:
    """Whether a file other than a folder has a second name, as a hard link gives it, so that finding it under two
    spellings shows nothing of how its directory compares names. A folder is never passed over: Linux and macOS count
    each subfolder's `..` among a folder's links, and no file system this runs on lets a user link a folder twice."""
    return not stat.S_ISDIR(status.st_mode) and status.st_nlink > 1


def _asks(anchor: str) -> bool:
    """Whether a directory under `anchor` may be asked: not under a network or device anchor."""
    return not (len(anchor) >= 2 and anchor[0] in SEPARATORS and anchor[1] in SEPARATORS)


def _answer_or(directory: str, otherwise: bool | None) -> bool | None:
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
    entry serves, since one directory compares all its names alike.

    The answer is the file system's case rule, never a coincidence of links: two hard links are two entries sharing
    one file, so a case-swapped name that finds the entry's file shows the directory folds case only where the swapped
    name is not listed as an entry of its own and the file has no second link. A directory listing both spellings
    keeps case. A file with a second link is passed over rather than asked, since that link may be the swapped name,
    made after the listing was read; the file the swapped name finds is held to that too, so a link made between the
    two lookups is passed over as well; the probe never makes a file of its own to ask with, so it writes nothing in the
    directory it judges, which may be read-only or a reviewer's run."""
    try:
        with os.scandir(directory) as listing:
            entries = list(listing)
    except OSError:
        return None
    names = {entry.name for entry in entries}
    for entry in entries:
        swapped = "".join(character.swapcase() if character.isascii() else character for character in entry.name)
        if swapped == entry.name:
            continue
        if swapped in names:
            return False
        try:
            found = os.lstat(entry.path)
        except OSError:
            continue  # removed meanwhile
        if _hard_linked(found):
            continue
        try:
            other = os.lstat(os.path.join(directory, swapped))  # noqa: PTH118 - text, as the entry's path is
        except FileNotFoundError:
            if _still_there(entry.path, found):
                return False
            continue  # removed meanwhile, so a folding file system misses the swapped name too
        except OSError:
            continue
        if _hard_linked(other):
            continue  # linked meanwhile, perhaps by the swapped name itself
        return (other.st_dev, other.st_ino) == (found.st_dev, found.st_ino)
    return None


def _still_there(path: str, found: os.stat_result) -> bool:
    """Whether the entry at `path` is still the one `found` described, so a case-swapped name that missed it shows
    the directory keeps case, rather than the entry having gone between the two lookups."""
    try:
        current = os.lstat(path)
    except OSError:
        return False
    return (current.st_dev, current.st_ino) == (found.st_dev, found.st_ino)

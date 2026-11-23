"""The one atomic replace of a file for the skills' scripts.

A script that rewrites a plan, a record, or a state file replaces it here, so every script makes the same promise:
a reader sees the old file or the new one, never part of either, and a write that fails, whatever ends it, an
OSError, a KeyboardInterrupt, or a defect, leaves the old file in place and no copy beside it. The deployer does not
replace through it: its writes are journaled, and every one goes through `deployer/fsops.py`, whose `write_atomic`
refuses a read-only target with this module's `is_read_only` and `ReadOnlyTargetError`.

A read-only target is refused before a copy exists, on every platform. Windows refuses to replace a read-only file,
where Linux replaces it as it would any other, so the one answer both can give is to refuse; a file counts as
read-only when its owner may not write it, the bit the Windows read-only attribute clears. A copy that cannot be
removed because it took a read-only `mode` is made writable first, so a replace refused part way leaves nothing.

`create_bytes` makes a file that must not replace anything, such as a copy a script puts beside a user's files, with
the same promise: a reader sees no file or the whole of it, and a write that fails leaves nothing at the target and
no copy beside it. Its last step links the synced copy at the target, which every platform refuses where any entry is
already there, where a rename would replace one on Linux and macOS. A file system with no hard links, such as FAT or
exFAT, has no other step that keeps that promise on every platform: macOS refuses a rename that refuses an existing
entry there too. So a caller whose readers may see an empty file asks for `allow_empty_reservation`, which reserves
the target empty with an exclusive create and then replaces the reservation with the copy; any other caller gets the
link's refusal.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from file_presence import is_occupied
from link_probe import is_link_or_junction
from path_containment import resolved_path

# The mode of a file a tool or a skill's script writes for others to read, such as one a checkout tracks, or a new
# index or graph a script creates: its owner writes it and everyone reads it, as an editor's new file.
TRACKED_FILE_MODE = 0o644

# The errnos a hard link is refused with where the file system has none, whatever is at the target: EPERM on Linux's
# FAT and exFAT, ENOTSUP on macOS's, EINVAL on Windows' (its ERROR_INVALID_FUNCTION), and EOPNOTSUPP or EXDEV from a
# network share. A create that allows an empty reservation falls back to one on these alone.
NO_HARD_LINK_ERRNOS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL, errno.EXDEV})

# The waits, in seconds, before each new try to remove a created copy's own name once a try fails: Windows refuses to
# remove any name of a file another handle holds open without delete sharing, as a reader of the target briefly does,
# and the copy and the target are one file once linked. Short and few, so a removal that keeps failing raises within
# about a third of a second, well inside a hook's time limit.
COPY_NAME_REMOVAL_WAITS = (0.01, 0.02, 0.04, 0.08, 0.16)


class UnfinishedCopyError(OSError):
    """A replace or a create that failed and could not remove the copy it wrote beside its target, or a create that
    made its target and could not then remove the copy's own name.

    It derives from OSError, so a caller that reports a failed write as an OSError reports this one too. `copy` is
    the copy left behind, `failure` what ended the write, and `removal` the OSError that kept the copy. `made` is
    whether the target holds the bytes written: only a create sets it, once its link put the copy in place.
    """

    def __init__(
        self, *, target: Path, copy: Path, failure: BaseException, removal: OSError, made: bool = False
    ) -> None:
        if made:
            message = f"{target} is made, but {copy}, its copy beside it, cannot be removed: {removal}"
        else:
            message = f"Cannot remove {copy}, the unfinished copy of {target}: {removal}"
        super().__init__(message)
        self.target = target
        self.copy = copy
        self.failure = failure
        self.removal = removal
        self.made = made


class UnfinishedReservationError(OSError):
    """A create that reserved its target, failed before or as the copy replaced the reservation, and could not then
    remove what is at `target`: its empty reservation, or an entry another process put in its place.

    It derives from OSError, so a caller that reports a failed write as an OSError reports this one too. `failure` is
    what ended the create, and `removal` the OSError that kept the entry at `target`.
    """

    def __init__(self, *, target: Path, failure: BaseException, removal: OSError) -> None:
        super().__init__(
            f"{target} was reserved by a create that then failed, and what is there now, its empty reservation or "
            f"another entry, cannot be removed: {removal}"
        )
        self.target = target
        self.failure = failure
        self.removal = removal


class ReadOnlyTargetError(PermissionError):
    """A replace refused before a copy exists, because `target`, or the file a link at `target` leads to, is read-only.

    It derives from PermissionError, so a caller that reports a failed write as an OSError reports this one too.
    """

    def __init__(self, target: Path) -> None:
        super().__init__(errno.EACCES, "the file is read-only; make it writable to replace it", str(target))
        self.target = target


def is_read_only(path: Path) -> bool:
    """Whether `path`, or the file a link at `path` leads to, exists and its owner may not write it. A path that does
    not exist, or a link that leads nowhere, is not read-only."""
    for follow in (False, True):
        try:
            mode = path.stat(follow_symlinks=follow).st_mode
        except FileNotFoundError:
            return False
        if not mode & stat.S_IWUSR:
            return True
    return False


def replace_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Replace `path` with `content` encoded as UTF-8, as replace_bytes does. Content that UTF-8 cannot encode raises
    its UnicodeEncodeError before a copy exists, and `path` is untouched."""
    replace_bytes(path, content.encode("utf-8"), mode=mode)


def replace_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Replace `path` with `data` through a synced copy beside it, given `mode`, making its folder first.

    A read-only `path` raises ReadOnlyTargetError before a copy exists. On any failure, including a folder that
    cannot be made, `path` is untouched, the copy is removed, and the failure is raised as it was. A copy that cannot
    be removed raises UnfinishedCopyError, which names it.

    The copy's descriptor is never handed to a stream: this function alone closes it, once, whatever fails. A stream
    that closed it first would free its number for another thread's open, and a second close would close that file.
    """

    def refuse(target: Path) -> None:
        if is_read_only(target):
            raise ReadOnlyTargetError(target)

    _publish(path, data, mode=mode, refuse=refuse, place=Path.replace, creates=False)


def create_bytes(path: Path, data: bytes, *, mode: int = 0o600, allow_empty_reservation: bool = False) -> None:
    """Create `path` holding `data`, never replacing what is there, through a synced copy beside it given `mode`,
    making its folder first.

    A file, a folder, or a link already at `path`, one that leads nowhere included, raises FileExistsError and is left
    as it was. A failure before the link that puts the copy in place leaves nothing at `path`, removes the copy, and
    is raised as it was. Once the link is made, `path` holds `data` whatever follows: removing the copy's own name is
    tried again after each OSError, after each wait of COPY_NAME_REMOVAL_WAITS and then once more at once, so a reader
    that holds the file open for a moment, which on Windows refuses the removal, keeps no create from finishing, and
    an OSError a later try overcomes is no failure, so the create returns; an interrupt or a defect there, in a wait
    included, is raised as it was, the copy removed; and a copy whose name no try can remove raises
    UnfinishedCopyError with `made` set, whose message says `path` is made and names the copy.

    The copy is put in place by a hard link, so a folder whose file system has none, such as FAT or exFAT, or a
    network share that refuses them, cannot hold a file this creates unless the caller allows an empty reservation.
    Without it the refusal, one of NO_HARD_LINK_ERRNOS or any other, is raised as an OSError of its errno that names
    `path` and says the file system refused the link, never a FileExistsError, with nothing at `path` and the copy
    removed.

    `allow_empty_reservation` lets a refusal of one of NO_HARD_LINK_ERRNOS fall back to two steps: an exclusive create
    reserves `path` empty, refusing any entry there as FileExistsError, and the copy then replaces the reservation.
    A reader may see the empty reservation until the replace, so allow it only where every reader takes an empty file
    for one not yet made; the file is still never seen part written. A failure raised once the reservation is made,
    its check included, removes what is at `path`, whether or not it is still there, unless it is a link, and the
    copy, and is raised as it was; an entry at `path` that cannot be looked at or removed raises
    UnfinishedReservationError. An interrupt as the reservation is made or checked, or a kill before the replace, can
    leave an empty file at `path`, which no code here runs to remove. A process that removes the reservation and puts
    its own entry at `path`, or replaces it, before the replace has that entry replaced, or, unless it is a link,
    removed by a failure's clean-up. Windows' exclusive create follows a link, so only a concurrent
    writer that puts a link at `path`, in the caller's own folder, between the look for an entry and the reservation
    has the reservation made where it leads. That is refused as FileExistsError once the reservation is found not to
    be `path`'s own file, and the file made where the link led is removed when the link still leads to it, and stays
    otherwise; one that cannot be looked at or removed raises its OSError instead. Should that writer's link be
    followed and the check then interrupted, the empty file stays where the link leads.

    A `mode` its owner may not write raises ValueError before a copy exists: once linked, the copy and `path` are one
    file, which Windows refuses to remove by either name while it is read-only.
    """
    if not mode & stat.S_IWUSR:
        raise ValueError(f"create_bytes needs a mode its owner may write, not {mode:o}")

    def place(copy: Path, target: Path) -> bool:
        return _link_new(copy, target, reserve=allow_empty_reservation)

    _publish(path, data, mode=mode, refuse=lambda _target: None, place=place, creates=True)


def _link_new(copy: Path, target: Path, *, reserve: bool) -> bool:
    """Give `copy` the name `target` too, which the link refuses where any entry is, and return whether the copy's own
    name remains. A refusal of the link that says the file system has no hard links falls back, when `reserve` allows
    it, to _replace_reservation, which leaves no name of the copy's own. Any other refusal is raised with its errno,
    so as the same OSError subclass, naming `target` rather than the copy that is about to be removed."""
    try:
        os.link(copy, target)
    except FileExistsError:
        raise
    except OSError as refusal:
        if reserve and refusal.errno in NO_HARD_LINK_ERRNOS:
            _replace_reservation(copy, target)
            return False
        reason = f"the file system refused to link the finished copy into place: {refusal.strerror or refusal}"
        raise OSError(refusal.errno, reason, str(target)) from refusal
    return True


def _replace_reservation(copy: Path, target: Path) -> None:
    """Reserve `target` empty with an exclusive create, which raises FileExistsError where any entry is, then replace
    the reservation with `copy`. Whatever is raised once the reservation is made, _remove_reservation removes what is
    at `target` and the failure is raised as it was. An interrupt that lands as os.open returns, or as _reserve hands
    back the checked reservation, is raised outside every clean-up, so it leaves the reservation, as a kill does."""
    descriptor = _reserve(target)
    try:
        os.close(descriptor)
        copy.replace(target)
    except BaseException as failure:
        _remove_reservation(target, failure)
        raise


def _remove_reservation(target: Path, failure: BaseException) -> None:
    """Remove what is at `target` once `failure` ended a create that reserved it: its reservation, or an entry put in
    its place, but never a link or junction, which an exclusive create never makes. Nothing there is no failure. An
    entry that cannot be looked at or removed raises UnfinishedReservationError."""
    try:
        if is_occupied(target) and not is_link_or_junction(target):
            target.unlink(missing_ok=True)
    except OSError as removal:
        raise UnfinishedReservationError(target=target, failure=failure, removal=removal) from removal


def _publish(
    path: Path,
    data: bytes,
    *,
    mode: int,
    refuse: Callable[[Path], None],
    place: Callable[[Path, Path], object],
    creates: bool,
) -> None:
    """Write `data` to a synced copy beside `path`, given `mode`, once its folder is made and `refuse` has not refused
    `path`, then `place` the copy at `path`, and, for a create, whose `place` returns whether the copy's own name
    remains, as it does after a link, remove that name.
    Whatever fails, the copy is removed and the failure raised as it was, or UnfinishedCopyError raised for a copy
    that cannot be removed. A create whose link was made is made: _remove_copy_name tries removing the copy's name
    through its waits, the clean-up here tries once more, an OSError a later try overcomes is no failure, and an
    UnfinishedCopyError says `path` is made.

    A replace's copy is made writable before it is removed, since its mode may be read-only and Windows refuses to
    remove a read-only file. A created copy's mode is writable already, and is left alone: once linked, the copy and
    `path` are one file, and a change would change `path` too."""
    temporary: Path | None = None
    made = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        refuse(path)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            temporary.chmod(mode)
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        remains = place(temporary, path)
        made = creates
        if creates and remains:
            _remove_copy_name(temporary)
        temporary = None
    except BaseException as failure:
        if temporary is not None:
            try:
                if not creates:
                    with contextlib.suppress(OSError):
                        temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
                temporary.unlink(missing_ok=True)
            except OSError as removal:
                raise UnfinishedCopyError(
                    target=path, copy=temporary, failure=failure, removal=removal, made=made
                ) from removal
            if made and isinstance(failure, OSError):
                return
        raise


def _remove_copy_name(copy: Path) -> None:
    """Remove `copy`, a created copy's own name, trying again after each wait of COPY_NAME_REMOVAL_WAITS once a try
    raises an OSError, and raise the last try's OSError when every one fails. A name already gone is removed. Anything
    else, an interrupt in a wait included, is raised as it was."""
    for wait in COPY_NAME_REMOVAL_WAITS:
        try:
            copy.unlink(missing_ok=True)
        except OSError:
            time.sleep(wait)
        else:
            return
    copy.unlink(missing_ok=True)


def _reserve(target: Path) -> int:
    """Create `target` empty and exclusively, returning its descriptor, or raise FileExistsError where any entry is.

    The entry is looked for first, without following a link: Windows' exclusive create follows a link at `target`,
    making the file where it leads, even where it leads nowhere. Windows also refuses an exclusive create over a folder
    as PermissionError, so a create refused while an entry is at `target` raises FileExistsError too. A link put at
    `target` between the look and the create is caught after it: a created file that is not `target`'s own entry is
    closed, removed by _remove_followed, and refused as FileExistsError. A check that fails closes the reservation,
    which _remove_reservation removes, and is raised as it was."""
    if is_occupied(target):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise
    except OSError as refusal:
        if is_occupied(target):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target)) from refusal
        raise
    try:
        reserved = os.fstat(descriptor)
        own = os.path.samestat(reserved, target.lstat())
    except BaseException as failure:
        # A close that fails releases the descriptor all the same, and the failure that ended the check is raised.
        with contextlib.suppress(OSError):
            os.close(descriptor)
        _remove_reservation(target, failure)
        raise
    if own:
        return descriptor
    os.close(descriptor)
    _remove_followed(target, reserved)
    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))


def _remove_followed(target: Path, reserved: os.stat_result) -> None:
    """Remove the file an exclusive create made by following a link at `target`, found where the link now leads and
    removed only when it is that same file, `reserved`. Anything else there, or nothing, stays; a place that cannot
    be looked at, or a file that cannot be removed, raises its OSError, which names it."""
    followed = resolved_path(target)
    if is_occupied(followed) and os.path.samestat(reserved, followed.lstat()):
        followed.unlink()

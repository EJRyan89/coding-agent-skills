"""The one atomic replace of a file for the skills' scripts.

A script that rewrites a plan, a record, or a state file replaces it here, so every script makes the same promise:
a reader sees the old file or the new one, never part of either, and a write that fails, whatever ends it, an
OSError, a KeyboardInterrupt, or a defect, leaves the old file in place and no copy beside it. The deployer does not
call it: its writes are journaled, and every one goes through `deployer/fsops.py`.

A read-only target is refused before a copy exists, on every platform. Windows refuses to replace a read-only file,
where Linux replaces it as it would any other, so the one answer both can give is to refuse; a file counts as
read-only when its owner may not write it, the bit the Windows read-only attribute clears. A copy that cannot be
removed because it took a read-only `mode` is made writable first, so a replace refused part way leaves nothing.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import tempfile
from pathlib import Path


class UnfinishedCopyError(OSError):
    """A replace that failed and could not remove the copy it wrote beside its target.

    It derives from OSError, so a caller that reports a failed write as an OSError reports this one too. `copy` is
    the copy left behind, `failure` what ended the write, and `removal` the OSError that kept the copy.
    """

    def __init__(self, *, target: Path, copy: Path, failure: BaseException, removal: OSError) -> None:
        super().__init__(f"Cannot remove {copy}, the unfinished copy of {target}: {removal}")
        self.target = target
        self.copy = copy
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
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if is_read_only(path):
            raise ReadOnlyTargetError(path)
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
        temporary.replace(path)
        temporary = None
    except BaseException as failure:
        if temporary is not None:
            try:
                with contextlib.suppress(OSError):
                    temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
                temporary.unlink(missing_ok=True)
            except OSError as removal:
                raise UnfinishedCopyError(target=path, copy=temporary, failure=failure, removal=removal) from removal
        raise

"""The one atomic replace of a file for the skills' scripts.

A script that rewrites a plan, a record, or a state file replaces it here, so every script makes the same promise:
a reader sees the old file or the new one, never part of either, and a write that fails, whatever ends it, an
OSError, a KeyboardInterrupt, or a defect, leaves the old file in place and no copy beside it. The deployer does not
call it: its writes are journaled, and every one goes through `deployer/fsops.py`.
"""

from __future__ import annotations

import os
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


def replace_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Replace `path` with `content` encoded as UTF-8, as replace_bytes does. Content that UTF-8 cannot encode raises
    its UnicodeEncodeError before a copy exists, and `path` is untouched."""
    replace_bytes(path, content.encode("utf-8"), mode=mode)


def replace_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Replace `path` with `data` through a synced copy beside it, given `mode`, making its folder first.

    On any failure, including a folder that cannot be made, `path` is untouched, the copy is removed, and the failure
    is raised as it was. A copy that cannot be removed raises UnfinishedCopyError, which names it.

    The copy's descriptor is never handed to a stream: this function alone closes it, once, whatever fails. A stream
    that closed it first would free its number for another thread's open, and a second close would close that file.
    """
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
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
                temporary.unlink(missing_ok=True)
            except OSError as removal:
                raise UnfinishedCopyError(target=path, copy=temporary, failure=failure, removal=removal) from removal
        raise

"""The one lock the operating system holds for the process that takes it, and releases when that process ends.

A lock judged by its holder's process identity can be reclaimed only by moving or deleting what the judge saw, and no
filesystem step deletes a path only while it still holds what was judged, so two reclaimers can each take a lock the
other made. A lock the operating system holds needs no reclaim: it ends with its holder, however the holder ends.

The lock is `fcntl.flock` on Linux and macOS, which holds it per open file, so a second open in the same process, or
another thread's, is refused too, and on Windows `msvcrt.locking` of one byte far past the file's end, held per
handle. Windows refuses every other handle a read of a byte it locks, so a lock on the file's content would stop a
backup, an indexer, or a sync client reading the file; past its end there is nothing to read.
`lockf` would not serve, since it drops every lock a process holds on a file when any of its handles to the file
closes. A descriptor Python opens is never inherited, so no process the holder starts holds the lock after it ends.

`take` opens the file, creating it, and takes the lock without waiting; it answers None while another holds it, or
when a signal interrupted the attempt, and raises `FileLockError` when the file system refuses the lock itself. A
refusal is `unsupported` only when its reason says the file system offers no lock at all, as a mount without lock
support does; any other, such as a lock table that is full or a lock service that does not answer, may come while
another process holds the lock, so the file is no one's to delete only after an unsupported refusal.
`release` deletes the file and lets the lock go, so a run leaves nothing behind. On Linux and macOS a waiter may hold
a descriptor of the file a release deleted and then take the lock of that deleted file, so `take` keeps a lock only
while its path still names the file it locked. On Windows a file cannot be deleted while another handle holds it open,
so `release` closes first and leaves the file to a waiter that holds it open.
"""

from __future__ import annotations

import contextlib
import errno
import os
import sys
from collections.abc import Callable
from pathlib import Path

# The errors a lock attempt gives when another holds the lock: msvcrt.locking sets EACCES, or EDEADLOCK, which its
# blocking form gives after its retries; flock gives EWOULDBLOCK, which is EAGAIN on Linux and macOS.
if sys.platform == "win32":
    import msvcrt

    HELD_ERRNOS = frozenset({errno.EACCES, errno.EDEADLOCK})
else:
    import fcntl

    HELD_ERRNOS = frozenset({errno.EWOULDBLOCK, errno.EAGAIN})
# A lock attempt a signal interrupted, which takes nothing, so it is tried again as one another holds is.
RETRY_ERRNOS = frozenset({errno.EINTR})
# The errors that say the file system offers no lock at all, so no process can hold the file's lock.
UNSUPPORTED_ERRNOS = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS})


# The byte Windows locks: past the end of the empty file, within the offsets every Windows file system takes.
LOCKED_OFFSET = 2**30


class FileLockError(OSError):
    """The file system refused the lock itself, rather than another process holding it."""

    @property
    def unsupported(self) -> bool:
        """Whether the reason says the file system offers no lock at all, so no process holds this file's lock; any
        other refusal may come while another process holds it."""
        return self.errno in UNSUPPORTED_ERRNOS


def open_for_lock(path: Path) -> int:
    """Open `path` for locking, creating it, readable and writable by its owner only."""
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


def _lock(descriptor: int) -> None:
    if sys.platform == "win32":
        os.lseek(descriptor, LOCKED_OFFSET, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(descriptor: int) -> None:
    if sys.platform == "win32":
        os.lseek(descriptor, LOCKED_OFFSET, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


def try_lock(descriptor: int) -> bool:
    """Take the lock of an open file without waiting: True when taken, False while another holds it or when a
    signal interrupted the attempt, and a `FileLockError` with the platform's reason when the file system refuses the
    lock."""
    try:
        _lock(descriptor)
    except OSError as exc:
        if exc.errno in HELD_ERRNOS or exc.errno in RETRY_ERRNOS:
            return False
        raise FileLockError(exc.errno, exc.strerror or str(exc)) from exc
    return True


def _names(descriptor: int, path: Path) -> bool:
    """Whether `path` still names the file open at `descriptor`."""
    try:
        named = path.stat()
    except FileNotFoundError:
        return False
    held = os.fstat(descriptor)
    return (named.st_dev, named.st_ino) == (held.st_dev, held.st_ino)


def take(path: Path, open_file: Callable[[Path], int] = open_for_lock) -> int | None:
    """Open `path` through `open_file`, creating it, and take its lock without waiting: the open descriptor, which
    holds the lock until `release`, or None while another process holds it or a release deleted the file meanwhile.

    The OSError of the open is raised as it came, and a `FileLockError` when the file system refuses the lock.
    """
    descriptor = open_file(path)
    try:
        if try_lock(descriptor):
            if _names(descriptor, path):
                return descriptor
            _unlock(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    os.close(descriptor)
    return None


def release(descriptor: int, path: Path, remove: Callable[[Path], None] = os.unlink) -> None:
    """Delete `path` through `remove` and let the lock taken at `descriptor` go, closing it.

    A file that cannot be deleted stays for the next holder, who takes and deletes it, so a failed deletion is not
    raised; nor is a failed unlock, since closing the descriptor ends the lock all the same.
    """
    if sys.platform == "win32":
        # A file another handle holds open cannot be deleted, this process's own included, so the lock goes first.
        _let_go(descriptor)
        with contextlib.suppress(OSError):
            remove(path)
        return
    try:
        with contextlib.suppress(OSError):
            remove(path)
    finally:
        _let_go(descriptor)


def _let_go(descriptor: int) -> None:
    try:
        with contextlib.suppress(OSError):
            _unlock(descriptor)
    finally:
        with contextlib.suppress(OSError):
            os.close(descriptor)


def held_elsewhere(path: Path) -> bool:
    """Whether another process holds the lock of the file at `path`, creating nothing: False when no file is there.

    The lock is taken and let go at once when it is free, so a process that tries it meanwhile is refused for that
    moment. The OSError of the open is raised as it came, and a `FileLockError` when the file system refuses the lock.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        if not try_lock(descriptor):
            return True
        _unlock(descriptor)
        return False
    finally:
        os.close(descriptor)

"""Atomic JSON persistence, conservative resource locks, and bounded parallel calls."""

from __future__ import annotations

import contextlib
import functools
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, TypeVar

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from review_process import ProcessStatus, process_status, same_process
from skill_roots import skills_directory_holding

# Concurrent GitHub and git network calls; small enough to stay clear of GitHub's secondary rate limits.
NETWORK_WORKERS = 4

Item = TypeVar("Item")
Value = TypeVar("Value")


class PersistenceError(RuntimeError):
    """Raised when durable state cannot be read or committed safely."""


def working_path(explicit: Path | None, prefix: str, name: str) -> Path:
    """Where a command writes a working file: `explicit`, or `name` in a new temporary directory.

    A file left inside a skill directory makes the deployer see that skill as modified and stop updating it, so an
    explicit path inside any skills directory is refused before anything is read or written.
    """
    if explicit is None:
        return Path(tempfile.mkdtemp(prefix=prefix)) / name
    root = skills_directory_holding(explicit)
    if root is not None:
        raise PersistenceError(
            f"{explicit} is inside the skills directory {root}; omit the option to write "
            "under a new temporary directory"
        )
    return explicit


def map_in_order(
    function: Callable[[Item], Value],
    items: Iterable[Item],
    *,
    catch: tuple[type[BaseException], ...] = (),
    fatal: Callable[[BaseException], bool] = lambda error: False,
    workers: int = NETWORK_WORKERS,
) -> list[tuple[Value | None, BaseException | None]]:
    """Call function on each item, up to `workers` at a time, as (value, error) pairs in item order.

    An exception in `catch` becomes that item's error and the others carry on. Any other exception, or one that
    `fatal` accepts, is raised at its item's place in the order, as a sequential loop would: calls not yet
    started are cancelled, and calls already running finish but are discarded.
    """
    items = list(items)

    def outcome(call: Callable[[], Value]) -> tuple[Value | None, BaseException | None]:
        try:
            return call(), None
        except catch as error:
            if fatal(error):
                raise
            return None, error

    if workers <= 1 or len(items) <= 1:
        return [outcome(functools.partial(function, item)) for item in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as executor:
        futures = [executor.submit(function, item) for item in items]
        try:
            return [outcome(future.result) for future in futures]
        except BaseException:
            for future in futures:
                future.cancel()
            raise


def read_json(path: Path, *, maximum_bytes: int = 4 * 1024 * 1024) -> Any:
    try:
        size = path.stat().st_size
        if size > maximum_bytes:
            raise PersistenceError(f"JSON file exceeds {maximum_bytes} bytes: {path}")
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except PersistenceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PersistenceError(f"Cannot read valid JSON from {path}: {exc}") from exc


def open_shared(path: Path) -> IO[bytes]:
    """Open a file for reading through a handle that lets other processes delete or rename it meanwhile.

    open() on Windows denies deletion while the file is open, so a lock's waiter reading its owner file that way
    stops the holder from releasing the lock. Through this handle the holder's delete goes through, and the reader
    still reads what it opened.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    generic_read = 0x80000000
    share_read_write_delete = 0x1 | 0x2 | 0x4
    open_existing = 3
    normal = 0x80
    handle = kernel32.CreateFileW(str(path), generic_read, share_read_write_delete, None, open_existing, normal, None)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        code = ctypes.get_last_error()
        raise OSError(None, ctypes.FormatError(code), str(path), code)
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        raise
    return os.fdopen(descriptor, "rb")


def read_diff(path: Path) -> str:
    """A run's diff.patch, the one way every step reads it.

    prepare writes it as valid UTF-8, its undecodable bytes already U+FFFD; a byte that is not UTF-8 all the same,
    as in a run prepared before that, becomes U+FFFD here too rather than failing one step and not another.
    """
    return path.read_bytes().decode("utf-8", errors="replace")


def atomic_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            temporary.chmod(mode)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            raise
        temporary.replace(path)
        temporary = None
    except OSError as exc:
        raise PersistenceError(f"Cannot atomically replace {path}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write_json(
    path: Path,
    value: Any,
    *,
    validator: Callable[[Any], object] | None = None,
) -> None:
    if validator is not None:
        validator(value)
    content = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    # Validate the exact serialized representation before replacement.
    parsed = json.loads(content)
    if validator is not None:
        validator(parsed)
    atomic_write_text(path, content)


# How long a lock directory may stay without a valid owner file before it is taken for one its creator never
# finished: a holder writes the file at once and atomically, so only a process stopped between the two steps, or
# killed, or a file torn outside the lock's protocol, leaves one that long.
OWNER_GRACE_SECONDS = 60.0
OWNER_MAXIMUM_BYTES = 64 * 1024


@dataclass(frozen=True)
class LockIdentity:
    """Who a lock directory belongs to: its holder's token, or for a directory without a valid owner file, the
    directory itself, or the torn owner file, as its device, file index, and modification time."""

    token: str | None = None
    unowned: tuple[int, int, int] | None = None
    torn: bool = False


# What inspecting a lock finds when the lock was released while it looked: the directory or its owner file is gone.
RELEASED = LockIdentity()


class ResourceLock(AbstractContextManager["ResourceLock"]):
    """Short-lived mkdir lock with token-checked release and conservative recovery.

    The owner file records the holder's PID and start time. A lock is reclaimed, with a warning on stderr, when its
    holder is provably gone (not running, or its PID now names a process with a different start time), or when it
    has had no valid owner file (none, or one torn) for `owner_grace_seconds`. A live PID whose identity cannot be
    checked, and an owner file that cannot be opened, keep the lock, as the deployer's lock does. A waiter reads the
    owner file through open_shared, so it never stops the holder releasing the lock; a release that cannot remove
    the lock all the same warns and returns, since the holder's work is done and the lock is stale once it exits.
    """

    def __init__(
        self,
        directory: Path,
        *,
        timeout_seconds: float = 10.0,
        owner_grace_seconds: float = OWNER_GRACE_SECONDS,
        probe: Callable[[int], ProcessStatus] | None = None,
    ) -> None:
        self.directory = directory
        self.probe = probe or process_status
        self.timeout_seconds = timeout_seconds
        self.owner_grace_seconds = owner_grace_seconds
        self.token = secrets.token_hex(16)
        self._held = False

    @property
    def owner_path(self) -> Path:
        return self.directory / "owner.json"

    def __enter__(self) -> ResourceLock:
        deadline = time.monotonic() + self.timeout_seconds
        try:
            self.directory.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PersistenceError(f"Cannot prepare lock parent {self.directory.parent}: {exc}") from exc
        start_time = self.probe(os.getpid()).start_time
        while True:
            try:
                self.directory.mkdir()
            except FileExistsError:
                if self._reclaim_stale():
                    continue
                if time.monotonic() >= deadline:
                    raise PersistenceError(
                        f"Timed out waiting for lock {self.directory}; delete it if no review is running"
                    ) from None
                time.sleep(0.05)
                continue
            except OSError as exc:
                raise PersistenceError(f"Cannot acquire lock {self.directory}: {exc}") from exc
            try:
                atomic_write_json(
                    self.owner_path,
                    {
                        "pid": os.getpid(),
                        "token": self.token,
                        "created_unix": time.time(),
                        "start_time": start_time,
                    },
                )
            except PersistenceError:
                shutil.rmtree(self.directory, ignore_errors=True)
                raise
            self._held = True
            return self

    @staticmethod
    def _inspect(directory: Path) -> tuple[LockIdentity, dict[str, Any]] | None:
        """Who a lock directory belongs to, with its owner file's content (empty without a valid one): RELEASED when
        the directory or its owner file vanished while it looked, and None when that cannot be told."""
        owner_path = directory / "owner.json"
        try:
            status = owner_path.stat()
        except FileNotFoundError:
            try:
                status = directory.stat()
            except FileNotFoundError:
                return RELEASED, {}
            except OSError:
                return None
            return LockIdentity(unowned=(status.st_dev, status.st_ino, status.st_mtime_ns)), {}
        except OSError:
            return None
        try:
            with open_shared(owner_path) as stream:
                content = stream.read(OWNER_MAXIMUM_BYTES + 1)
        except FileNotFoundError:
            return RELEASED, {}
        except OSError:
            return None
        try:
            owner = json.loads(content.decode("utf-8-sig")) if len(content) <= OWNER_MAXIMUM_BYTES else None
        except (UnicodeError, json.JSONDecodeError):
            owner = None
        token = owner.get("token") if isinstance(owner, dict) else None
        if isinstance(owner, dict) and isinstance(token, str) and token:
            return LockIdentity(token=token), owner
        return LockIdentity(unowned=(status.st_dev, status.st_ino, status.st_mtime_ns), torn=True), {}

    def _stale(self, identity: LockIdentity, owner: dict[str, Any]) -> str | None:
        """Why the inspected lock is stale, or None while it may still be held."""
        if identity.unowned is not None:
            age = time.time() - identity.unowned[2] / 1e9
            if age <= self.owner_grace_seconds:
                return None
            if identity.torn:
                return f"its owner file has not been a valid owner record for {int(age)} seconds"
            return f"it has had no owner file for {int(age)} seconds"
        pid = owner.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        recorded_start = owner.get("start_time")
        if not isinstance(recorded_start, int) or isinstance(recorded_start, bool):
            recorded_start = None
        status = self.probe(pid)
        if same_process(status, recorded_start) is not False:
            return None
        return f"PID {pid} is not running" if not status.alive else f"PID {pid} now names another process"

    def _reclaim_stale(self) -> bool:
        """Whether the lock may be free now: released while it was inspected, or stale and moved aside and deleted
        with a warning. False while the lock may still be held.

        Moving the directory aside is the one atomic step, so the check that it held the lock judged stale comes
        after it: a lock another process took in between is moved back.
        """
        inspected = self._inspect(self.directory)
        if inspected is None:
            return False
        identity, owner = inspected
        if identity == RELEASED:
            return True
        reason = self._stale(identity, owner)
        if reason is None:
            return False
        stale = self.directory.with_name(f"{self.directory.name}.stale.{secrets.token_hex(8)}")
        try:
            self.directory.rename(stale)
        except OSError:
            return False  # removed or reclaimed meanwhile; the next attempt looks again
        moved = self._inspect(stale)
        if moved is None or moved[0] != identity:
            try:
                stale.rename(self.directory)
            except OSError as exc:
                raise PersistenceError(
                    f"A lock taken during a stale-lock reclaim was moved to {stale} and could not be moved back to "
                    f"{self.directory}: {exc}; delete it once no review is running"
                ) from exc
            return False
        print(f"WARNING: Reclaimed the stale lock {self.directory}: {reason}.", file=sys.stderr)
        shutil.rmtree(stale, ignore_errors=True)
        return True

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self._held:
            return
        owner = read_json(self.owner_path, maximum_bytes=OWNER_MAXIMUM_BYTES)
        if not isinstance(owner, dict) or owner.get("token") != self.token or owner.get("pid") != os.getpid():
            raise PersistenceError(f"Lock ownership changed before release: {self.directory}")
        self._held = False
        try:
            self.owner_path.unlink()
            self.directory.rmdir()
        except OSError as exc:
            # The holder's work is committed, so the lock is stale once this process exits, or once the grace period
            # passes when only its owner file went.
            print(
                f"WARNING: Could not remove the lock {self.directory}: {exc.strerror or exc}. A later review "
                "reclaims it once this process has exited.",
                file=sys.stderr,
            )

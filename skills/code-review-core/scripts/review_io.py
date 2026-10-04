"""Atomic JSON persistence, conservative resource locks, and bounded parallel calls."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

# Concurrent GitHub and git network calls; small enough to stay clear of GitHub's secondary rate limits.
NETWORK_WORKERS = 4

Item = TypeVar("Item")
Value = TypeVar("Value")


class PersistenceError(RuntimeError):
    """Raised when durable state cannot be read or committed safely."""


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
        return [outcome(lambda item=item: function(item)) for item in items]
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


def atomic_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, mode)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise PersistenceError(f"Cannot atomically replace {path}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, mode)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        os.replace(temporary, path)
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
    validator: Callable[[Any], None] | None = None,
) -> None:
    if validator is not None:
        validator(value)
    content = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    # Validate the exact serialized representation before replacement.
    parsed = json.loads(content)
    if validator is not None:
        validator(parsed)
    atomic_write_text(path, content)


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class ResourceLock(AbstractContextManager["ResourceLock"]):
    """Short-lived mkdir lock with token-checked release and conservative recovery."""

    def __init__(
        self,
        directory: Path,
        *,
        timeout_seconds: float = 10.0,
        stale_after_seconds: float = 3600.0,
    ) -> None:
        self.directory = directory
        self.timeout_seconds = timeout_seconds
        self.stale_after_seconds = stale_after_seconds
        self.token = secrets.token_hex(16)
        self._held = False

    @property
    def owner_path(self) -> Path:
        return self.directory / "owner.json"

    def __enter__(self) -> "ResourceLock":
        deadline = time.monotonic() + self.timeout_seconds
        try:
            self.directory.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PersistenceError(
                f"Cannot prepare lock parent {self.directory.parent}: {exc}"
            ) from exc
        while True:
            try:
                self.directory.mkdir()
                atomic_write_json(
                    self.owner_path,
                    {
                        "pid": os.getpid(),
                        "token": self.token,
                        "created_unix": time.time(),
                    },
                )
                self._held = True
                return self
            except FileExistsError:
                if self._reclaim_stale():
                    continue
                if time.monotonic() >= deadline:
                    raise PersistenceError(f"Timed out waiting for lock {self.directory}")
                time.sleep(0.05)
            except PersistenceError:
                shutil.rmtree(self.directory, ignore_errors=True)
                raise
            except OSError as exc:
                raise PersistenceError(f"Cannot acquire lock {self.directory}: {exc}") from exc

    def _reclaim_stale(self) -> bool:
        try:
            owner = read_json(self.owner_path, maximum_bytes=64 * 1024)
            pid = owner.get("pid")
            created = owner.get("created_unix")
            token = owner.get("token")
            if not isinstance(pid, int) or not isinstance(created, (int, float)):
                return False
            if not isinstance(token, str) or not token:
                return False
            if time.time() - float(created) <= self.stale_after_seconds:
                return False
            if _pid_is_running(pid):
                return False
            stale = self.directory.with_name(
                f"{self.directory.name}.stale.{secrets.token_hex(8)}"
            )
            os.replace(self.directory, stale)
            shutil.rmtree(stale)
            return True
        except (PersistenceError, OSError, AttributeError):
            return False

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self._held:
            return
        try:
            owner = read_json(self.owner_path, maximum_bytes=64 * 1024)
            if owner.get("token") != self.token or owner.get("pid") != os.getpid():
                raise PersistenceError(f"Lock ownership changed before release: {self.directory}")
            self.owner_path.unlink()
            self.directory.rmdir()
            self._held = False
        except OSError as release_error:
            raise PersistenceError(
                f"Cannot safely release lock {self.directory}: {release_error}"
            ) from release_error

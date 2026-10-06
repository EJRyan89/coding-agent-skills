"""Single-deployment lock with stale-lock detection by process identity."""

from __future__ import annotations

import json
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import fsops, platform_support
from .errors import DeployError, see_recovery
from .paths import Paths

ProcessProbe = Callable[[int], platform_support.ProcessStatus]
LOCK = "The deployment lock"


@dataclass
class Lock:
    paths: Paths
    token: str

    def release(self) -> None:
        token_file = self.paths.lock_dir / "token"
        try:
            current = token_file.read_text(encoding="utf-8")
        except OSError:
            return
        if current == self.token:
            fsops.remove(self.paths.lock_dir)


def _write_metadata(paths: Paths, token: str, probe: ProcessProbe) -> None:
    """Initialize a lock this process just created, removing it again if initialization fails."""
    pid = platform_support.current_process_id()
    info = {"pid": pid, "token": token, "start_time": probe(pid).start_time}
    try:
        fsops.write_atomic(paths.lock_dir / "token", token.encode("utf-8"))
        fsops.write_atomic(paths.lock_dir / "info.json", json.dumps(info).encode("utf-8"))
    except OSError as exc:
        try:
            fsops.remove(paths.lock_dir)
        except OSError:
            raise DeployError(
                f"ERROR: Failed to initialize deployment lock: {exc}",
                f"Remove {platform_support.normalize(paths.lock_dir)} after confirming no deployment is running.",
                see_recovery(LOCK),
            ) from exc
        raise DeployError(f"ERROR: Failed to initialize deployment lock: {exc}", "Retry the deployment.") from exc


def _read_info(lock_dir: Path) -> dict[str, Any]:
    """A lock's metadata, or an empty mapping when it cannot be read."""
    try:
        info = json.loads((lock_dir / "info.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return info if isinstance(info, dict) else {}


def _existing_holder(paths: Paths, probe: ProcessProbe) -> tuple[int, object]:
    """Fail when a live deployment holds the lock; warn and return its (PID, token) when it is stale."""
    hint = (
        "If no deployment is running, the lock is stale: "
        f"remove {platform_support.normalize(paths.lock_dir)}, then retry.",
        see_recovery(LOCK),
    )
    if not (paths.lock_dir / "info.json").is_file():
        raise DeployError("ERROR: Lock exists but has no metadata (may be initializing).", *hint)
    info = _read_info(paths.lock_dir)
    pid = info.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise DeployError("ERROR: Lock metadata is malformed (no PID).", *hint)
    recorded = info.get("start_time")
    status = probe(pid)
    if not status.alive:
        print(f"WARNING: Stale lock detected (PID {pid} not running).", file=sys.stderr)
        return pid, info.get("token")
    if not isinstance(recorded, int) or isinstance(recorded, bool):
        raise DeployError(f"ERROR: Lock PID {pid} is alive but its process identity cannot be verified.", *hint)
    if status.start_time is None:
        raise DeployError(f"ERROR: Lock PID {pid} is alive but its start time cannot be read.", *hint)
    if status.start_time == recorded:
        raise DeployError(f"ERROR: Another deployment is running (PID {pid}).", "Wait for it to finish, then retry.")
    print(f"WARNING: Stale lock detected (PID {pid} was reused).", file=sys.stderr)
    return pid, info.get("token")


def _contention(*detail: str) -> DeployError:
    return DeployError("ERROR: Failed to acquire lock after stale reclaim (contention).", *detail, see_recovery(LOCK))


def _confirm_reclaimed(paths: Paths, stale: Path, judged: tuple[int, object]) -> None:
    """Put back a lock that another deployment took between the stale judgment and the move, and fail.

    Moving the lock directory aside is the only atomic step available, so the check comes after it: what was moved
    must be the lock judged stale, by its PID and token, or it belongs to a deployment that is running.
    """
    info = _read_info(stale)
    if (info.get("pid"), info.get("token")) == judged:
        return
    try:
        fsops.move(stale, paths.lock_dir)
    except OSError as exc:
        raise _contention(
            f"Another deployment's lock was moved to {platform_support.normalize(stale)} and could not be moved back "
            f"to {platform_support.normalize(paths.lock_dir)}: {exc}. Delete it once no deployment is running."
        ) from exc
    raise _contention("Retry the deployment.")


def acquire(paths: Paths, probe: ProcessProbe = platform_support.process_status) -> Lock:
    fsops.make_directories(paths.deployer_dir)
    pid = platform_support.current_process_id()
    token = f"{pid}-{int(time.time())}-{secrets.token_hex(2)}"
    try:
        fsops.make_directory(paths.lock_dir)
    except FileExistsError:
        judged = _existing_holder(paths, probe)
        stale = paths.deployer_dir / f".deploy.lock.stale.{pid}"
        try:
            fsops.move(paths.lock_dir, stale)
        except OSError as exc:
            raise DeployError(
                "ERROR: Failed to reclaim stale lock (another process may have claimed it).",
                "Retry the deployment.",
                see_recovery(LOCK),
            ) from exc
        _confirm_reclaimed(paths, stale, judged)
        try:
            fsops.make_directory(paths.lock_dir)
        except OSError as exc:
            try:
                fsops.move(stale, paths.lock_dir)
            except OSError:
                fsops.remove(stale)
            raise _contention("Retry the deployment.") from exc
        _write_metadata(paths, token, probe)
        fsops.remove(stale)
        return Lock(paths, token)
    _write_metadata(paths, token, probe)
    return Lock(paths, token)

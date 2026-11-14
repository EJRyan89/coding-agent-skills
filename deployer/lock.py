"""Single-deployment lock: a file lock the operating system holds for the run, beside a record of the run.

A run holds `.deploy.lock` through ejryan89-skill-core's `file_lock`, which the operating system releases when the run
ends, however it ends, so no run ever judges whether another's lock is stale. Beside it, the record `.deploy.lock.d`
holds the run's PID, start time, and token: it names the holder when another run is refused, and it keeps out a
deployer from before the file lock, which takes only the record. Only the holder of the file lock removes or makes
the record, so a record left by a run that ended is removed by one run at a time; a dry run, and a run refused the
lock, only read it to name or judge the holder. A deployer from before the file lock may still take the record
between the judgment and the removal, so the record is moved aside first and removed only when it is still the one
judged stale; otherwise it is moved back and the run is refused.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

import file_lock
import file_presence
import json_writer
from process_identity import read_pid
from typed_fields import FieldError

from . import fsops, platform_support, state_files
from .errors import DeployError, os_error, see_recovery
from .paths import Paths

ProcessProbe = Callable[[int], platform_support.ProcessStatus]
LOCK = "The deployment lock"
# The presence reads here that read a path they cannot stat as nothing there, each with why that is the answer.
PRESENCE_ALLOWED = {
    "_abandoned_while_initializing": "None hands the record to _holder_info, which reads its metadata again and "
    "refuses a stat or read that fails, naming the path",
    "_moved_record": "None reads as a record other than the one judged stale, which is moved back and refused, so a "
    "stat or read that fails never removes a record",
    "Lock._remove_record": (
        "a token file it cannot read is not this run's, so the record stays; nothing is removed on a failed read"
    ),
    "_read_info": (
        "an empty mapping names no holder in a refusal that already refuses the run, so a failed read changes only "
        "what the refusal names; read for _moved_record, it gives Judged(None, None), which never equals a record "
        "judged stale, since that judgment holds a valid PID or its folder's identity, so the record is moved back, "
        "never removed"
    ),
}
# How long a run waits for the file lock another process holds before it refuses: long enough for a dry run's check,
# which takes the lock for a moment, and for Windows, which may release a lock a little after its holder ends.
LOCK_WAIT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.1
# How long a record may stay without its metadata before it is taken for one a run stopped while making it left: a
# run writes the metadata at once, and only a deployer from before the file lock makes the record without holding it.
RECORD_GRACE_SECONDS = 60.0
# The names a stale record is moved aside to while it is removed: `.deploy.lock.stale.<pid>` by a deployer from before
# the file lock, `.deploy.lock.stale.<token>` by this one. One left behind is named until it is deleted.
LEFT_ASIDE = ".deploy.lock.stale.*"


@dataclass(frozen=True)
class Judged:
    """Which record a judgment found stale: the PID and token its metadata gave, or, for a record with no metadata,
    its folder's device, file ID, and modification time."""

    pid: object = None
    token: object = None
    unowned: tuple[int, int, int] | None = None


def _unowned(status: os.stat_result) -> Judged:
    return Judged(unowned=(status.st_dev, status.st_ino, status.st_mtime_ns))


@dataclass
class Lock:
    """The lock a run holds, released when the run's `with` block ends however it ends. A retained lock keeps its
    record past the run, for the next run to judge stale and remove; its file lock ends with the run all the same."""

    paths: Paths
    token: str
    handle: int | None = None
    retained: bool = field(default=False, init=False)

    def __enter__(self) -> Lock:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        if self.retained:
            self._let_go()
        else:
            self.release()

    def retain(self) -> None:
        """Keep the record past the run, beside the journal a failed recovery left, for the next run to reclaim."""
        self.retained = True

    def release(self) -> None:
        try:
            self._remove_record()
        finally:
            self._let_go()

    def _remove_record(self) -> None:
        token_file = self.paths.lock_dir / "token"
        try:
            current = token_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            # A token file that does not read, or is not the UTF-8 this run wrote, is not this run's, so it stays.
            return
        if current != self.token:
            return
        try:
            fsops.remove(self.paths.lock_dir)
        except OSError as exc:
            # The deployment already finished or failed and said so; the record is stale once this process exits.
            print(
                f"WARNING: Could not remove the deployment lock {platform_support.normalize(self.paths.lock_dir)}: "
                f"{exc.strerror or exc}. The next deployment reclaims it.",
                file=sys.stderr,
            )

    def _let_go(self) -> None:
        if self.handle is not None:
            handle, self.handle = self.handle, None
            file_lock.release(handle, self.paths.lock_file, fsops.remove)


def _write_metadata(paths: Paths, token: str, probe: ProcessProbe) -> None:
    """Initialize a record this process just created, removing it again if initialization fails or is interrupted."""
    try:
        pid = platform_support.current_process_id()
        info = {"pid": pid, "token": token, "start_time": probe(pid).start_time}
        fsops.write_atomic(paths.lock_dir / "token", token.encode("utf-8"))
        fsops.write_atomic(paths.lock_dir / "info.json", json_writer.dumps(info).encode("utf-8"))
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
    except BaseException:
        # Ctrl+C or an unexpected error: the caller reports it, and the next run must find no half-made record.
        with contextlib.suppress(OSError):
            fsops.remove(paths.lock_dir)
        raise


def _read_info(lock_dir: Path) -> dict[str, object]:
    """A record's metadata, or an empty mapping when it cannot be read: for naming the holder of a lock in a refusal,
    which names none then."""
    try:
        text = (lock_dir / "info.json").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    info = state_files.parse_json(text)
    return info if isinstance(info, dict) else {}


def _holder_info(paths: Paths, step: str, *more: str) -> dict[str, object]:
    """A record's metadata, refused by its fault: no file yet, something else where the file belongs, a file whose
    stat or read fails, or one that is not UTF-8 or not the JSON object the deployer writes. Each refusal ends with
    the step to take and the lines after it."""
    info_file = paths.lock_dir / "info.json"
    shown = platform_support.normalize(info_file)
    try:
        if not file_presence.is_present(info_file):
            raise DeployError("ERROR: Lock exists but has no metadata (may be initializing).", step, *more)
        text = info_file.read_text(encoding="utf-8")
    except file_presence.NotAFile:
        raise DeployError(f"ERROR: The lock's metadata at {shown} is not a file.", step, *more) from None
    except OSError as exc:
        raise DeployError(
            f"ERROR: Could not read the lock's metadata at {shown}: {exc.strerror or exc}.", step, *more
        ) from exc
    except UnicodeError:
        raise DeployError(f"ERROR: Lock metadata is malformed: {shown} is not UTF-8 text.", step, *more) from None
    info = state_files.parse_json(text)
    if not isinstance(info, dict):
        raise DeployError(
            f"ERROR: Lock metadata is malformed: {shown} is not a JSON object the deployer reads.", step, *more
        )
    return info


def _abandoned_while_initializing(paths: Paths) -> tuple[int, Judged] | None:
    """The age in whole seconds of a record with no metadata older than the grace period, with the record it judged,
    or None: a record whose metadata is there, or something else where it belongs, or whose stat fails, is judged by
    its metadata instead."""
    try:
        if file_presence.is_present(paths.lock_dir / "info.json"):
            return None
        status = paths.lock_dir.stat()
    except (file_presence.NotAFile, OSError):
        return None
    age = time.time() - status.st_mtime
    return (int(age), _unowned(status)) if age > RECORD_GRACE_SECONDS else None


def _existing_holder(paths: Paths, probe: ProcessProbe) -> Judged:
    """Fail while the record may belong to a running deployment; warn when it is stale, answering which record it
    judged."""
    hint = (
        "If no deployment is running, the lock is stale: "
        f"remove {platform_support.normalize(paths.lock_dir)}, then retry.",
        see_recovery(LOCK),
    )
    abandoned = _abandoned_while_initializing(paths)
    if abandoned is not None:
        age, judged = abandoned
        print(f"WARNING: Stale lock detected (no metadata for {age} seconds).", file=sys.stderr)
        return judged
    info = _holder_info(paths, *hint)
    judged = Judged(info.get("pid"), info.get("token"))
    try:
        # A PID outside the platform's range is no process's, so it is malformed, never probed as another's.
        pid = read_pid(info.get("pid"), "pid")
    except FieldError as exc:
        raise DeployError(f"ERROR: Lock metadata is malformed (no valid PID): {exc}.", *hint) from None
    recorded = info.get("start_time")
    status = probe(pid)
    if not status.alive:
        print(f"WARNING: Stale lock detected (PID {pid} not running).", file=sys.stderr)
        return judged
    if not isinstance(recorded, int) or isinstance(recorded, bool):
        raise DeployError(f"ERROR: Lock PID {pid} is alive but its process identity cannot be verified.", *hint)
    if status.start_time is None:
        raise DeployError(f"ERROR: Lock PID {pid} is alive but its start time cannot be read.", *hint)
    if status.start_time == recorded:
        raise _running(pid)
    print(f"WARNING: Stale lock detected (PID {pid} was reused).", file=sys.stderr)
    return judged


def _running(pid: object = None) -> DeployError:
    """The refusal while another deployment holds the lock, naming its PID when its record gives one."""
    named = f" (PID {pid})" if isinstance(pid, int) and not isinstance(pid, bool) else ""
    return DeployError(f"ERROR: Another deployment is running{named}.", "Wait for it to finish, then retry.")


def _unlockable(paths: Paths, error: file_lock.FileLockError) -> DeployError:
    return DeployError(
        f"ERROR: Could not lock {platform_support.normalize(paths.lock_file)}: {error.strerror or error}.",
        "The deployment lock needs a folder on a file system that supports file locks, which some network and "
        "synced folders do not; keep the home on a local disk.",
        see_recovery(LOCK),
    )


def _judge_left_record(paths: Paths, probe: ProcessProbe) -> Judged | None:
    """Judge what earlier runs left beside a free file lock, changing nothing: name each record an earlier deployment
    moved aside, refuse a record that may belong to a running deployment, and warn of a stale one, answering which
    record is there to remove, or None. A stat that fails raises its OSError rather than reading as no record."""
    for left in file_presence.matching(paths.deployer_dir, LEFT_ASIDE):
        print(
            f"WARNING: An earlier deployment left a lock moved aside at {platform_support.normalize(left)}. "
            "Delete it once no deployment is running.",
            file=sys.stderr,
        )
    if not file_presence.is_occupied(paths.lock_dir):
        return None
    return _existing_holder(paths, probe)


def check_holder(paths: Paths, probe: ProcessProbe = platform_support.process_status) -> None:
    """Judge the lock as acquire judges it, taking and changing nothing, for the dry run: refuse while another
    deployment holds the file lock, refuse a record acquire refuses, and warn of what the deployment would remove. The
    file lock is tried and let go at once when it is free. A stat or open that fails raises its OSError."""
    try:
        held = file_lock.held_elsewhere(paths.lock_file)
    except file_lock.FileLockError as exc:
        raise _unlockable(paths, exc) from exc
    if held:
        raise _running(_read_info(paths.lock_dir).get("pid"))
    _judge_left_record(paths, probe)


def _take_file_lock(paths: Paths) -> int:
    """Take the file lock, waiting LOCK_WAIT_SECONDS for another holder, or refuse naming the holder."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            handle = file_lock.take(paths.lock_file, fsops.open_lock_file)
        except file_lock.FileLockError as exc:
            if exc.unsupported:
                # A file system with no lock at all lets no process hold this one, so the file is no one's. Any other
                # refusal may come while a holder runs, whose file this run must not delete.
                with contextlib.suppress(OSError):
                    fsops.remove(paths.lock_file)
            raise _unlockable(paths, exc) from exc
        except OSError as exc:
            raise os_error(exc, "open the deployment lock") from exc
        if handle is not None:
            return handle
        if time.monotonic() >= deadline:
            raise _running(_read_info(paths.lock_dir).get("pid"))
        time.sleep(LOCK_POLL_SECONDS)


def _moved_record(aside: Path) -> Judged | None:
    """Which record was moved aside, read as the judgment reads it, or None when that cannot be told."""
    try:
        if file_presence.is_present(aside / "info.json"):
            info = _read_info(aside)
            return Judged(info.get("pid"), info.get("token"))
        return _unowned(aside.stat())
    except OSError:  # file_presence.NotAFile included: such a record is not the one judged, so it is moved back
        return None


def _remove_stale(paths: Paths, token: str, judged: Judged) -> None:
    """Remove the record judged stale, holding the file lock.

    A deployer from before the file lock takes the record without it, so it may reclaim the stale record and take its
    own between the judgment and the removal. No filesystem step removes a folder only while it holds what was judged,
    so the record is moved aside first, under a name no deployer takes as the lock, and removed only when what was
    moved is the record judged stale; a record taken meanwhile is moved back, and the run is refused as contention.
    """
    aside = paths.deployer_dir / (LEFT_ASIDE.removesuffix("*") + token)
    try:
        fsops.move(paths.lock_dir, aside)
    except FileNotFoundError:
        return  # Gone meanwhile; making this run's record decides whether another deployer took it.
    except OSError as exc:
        raise DeployError(
            f"ERROR: Could not remove the stale lock {platform_support.normalize(paths.lock_dir)}: "
            f"{exc.strerror or exc}.",
            "Delete it once no deployment is running, then retry.",
            see_recovery(LOCK),
        ) from exc
    moved = _moved_record(aside)
    if moved != judged:
        try:
            fsops.move(aside, paths.lock_dir)
        except OSError as exc:
            raise DeployError(
                f"ERROR: Another deployment's lock was moved to {platform_support.normalize(aside)} and could not be "
                f"moved back to {platform_support.normalize(paths.lock_dir)}: {exc.strerror or exc}.",
                "Delete it once no deployment is running, then retry.",
                see_recovery(LOCK),
            ) from exc
        raise _running(moved.pid if moved is not None else None)
    try:
        fsops.remove(aside)
    except OSError as exc:
        # The record is out of the lock's place, so this run goes on; only the leftover needs the user.
        print(
            f"WARNING: Could not remove the stale lock moved aside to {platform_support.normalize(aside)}: "
            f"{exc.strerror or exc}. Delete it once no deployment is running.",
            file=sys.stderr,
        )


def _make_record(paths: Paths, token: str, probe: ProcessProbe) -> None:
    """Remove a stale record, then make and initialize this run's, holding the file lock throughout."""
    try:
        judged = _judge_left_record(paths, probe)
    except OSError as exc:
        raise os_error(exc, "read the deployment lock's record") from exc
    if judged is not None:
        _remove_stale(paths, token, judged)
    try:
        fsops.make_directory(paths.lock_dir)
    except FileExistsError:
        # Only a deployer from before the file lock makes a record without holding it.
        raise _running(_read_info(paths.lock_dir).get("pid")) from None
    except OSError as exc:
        raise os_error(exc, "make the deployment lock's record") from exc
    _write_metadata(paths, token, probe)


def acquire(paths: Paths, probe: ProcessProbe = platform_support.process_status) -> Lock:
    fsops.make_directories(paths.deployer_dir)
    pid = platform_support.current_process_id()
    token = f"{pid}-{int(time.time())}-{secrets.token_hex(2)}"
    handle = _take_file_lock(paths)
    try:
        _make_record(paths, token, probe)
    except BaseException:
        file_lock.release(handle, paths.lock_file, fsops.remove)
        raise
    return Lock(paths, token, handle)

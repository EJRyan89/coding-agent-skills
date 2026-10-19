"""code-review-core's watermark state and its locks."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import review_io
from review_io import PersistenceError, PersistenceFieldError, ResourceLock, atomic_write_json, read_json
from review_process import ProcessStatus
from review_state import StateError, StateFieldError, empty_state, load_state, update_state, validate_state


@contextlib.contextmanager
def undeletable(path: Path) -> Iterator[None]:
    """Keep the file `path` from being deleted for the block: on Windows by holding it open, which denies its deletion,
    and elsewhere, where an open file holds nothing, by a mode on its folder that denies removing what it holds."""
    if sys.platform == "win32":
        with path.open("rb"):
            yield
        return
    path.parent.chmod(0o555)
    try:
        yield
    finally:
        path.parent.chmod(0o755)


def reporting(status: ProcessStatus) -> Callable[[int], ProcessStatus]:
    """A process probe that reports the status for every process."""
    return lambda _pid: status


class StateAndLockTests(unittest.TestCase):
    def test_state_update_writes_the_validated_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            initial = empty_state()

            def advance(state: dict) -> dict:
                replacement = copy.deepcopy(state)
                replacement["repositories"]["example/one"] = {
                    "merged_since": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-02T00:00:00Z",
                }
                return replacement

            self.assertEqual(initial, load_state(path))
            updated = update_state(path, advance)
            self.assertEqual(updated, load_state(path))
            self.assertNotEqual(initial, updated)
            with self.assertRaisesRegex(
                StateFieldError, '^state must be an object of known fields, found unknown field "extra"$'
            ):
                update_state(path, lambda state: {**state, "extra": True})
            self.assertEqual(updated, load_state(path), "a refused replacement leaves the state as it was")

    def test_a_watermark_must_begin_with_a_calendar_date(self) -> None:
        def state(value: str) -> dict:
            return {"schema_version": 1, "repositories": {"example/one": {"merged_since": value}}}

        for value in ("2026-03-10", "2026-03-10T00:00:00Z"):
            with self.subTest(value=value):
                self.assertEqual(state(value), validate_state(state(value)))
        # A week date and the basic form parse as ISO 8601 dates in Python, but the watermark is YYYY-MM-DD.
        for value in (
            "not-a-date",
            "",
            "2026-3-10",
            "2026-02-30",
            "2026-W10-1",
            "20260310",
            "\uff12\uff10\uff12\uff16-03-10",
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    StateError, r"State example/one\.merged_since must begin with a YYYY-MM-DD date"
                ),
            ):
                validate_state(state(value))

    def test_contended_lock_fails_without_removing_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            with ResourceLock(path):
                with self.assertRaisesRegex(PersistenceError, "Timed out"), ResourceLock(path, timeout_seconds=0.01):
                    pass
                self.assertTrue(path.exists())

    BACKDATED_SECONDS = 61  # past the lock's grace period

    def _backdate(self, path: Path) -> int:
        """Move `path`'s modification time back BACKDATED_SECONDS and half a second more, so no rounding between the
        clocks reports less, and return it in nanoseconds."""
        old = time.time_ns() - (self.BACKDATED_SECONDS * 2 + 1) * 500_000_000
        os.utime(path, ns=(old, old))
        return old

    def _assert_reclaimed_after(self, warning: str, path: Path, reason: str, backdated: int) -> None:
        """`warning` is the one warning reclaiming the lock at `path` for `reason`, whose `{age}` is the age it
        reports, and that age is at least BACKDATED_SECONDS and no more than the time since `backdated`: a bound
        that moves with the runner's speed, never a fixed window."""
        stated = re.escape(reason).replace(re.escape("{age}"), r"(\d+)")
        match = re.fullmatch(rf"WARNING: Reclaimed the stale lock {re.escape(str(path))}: {stated}\.\n", warning)
        if match is None:
            self.fail(f"Not one warning reclaiming {path} for {reason!r}: {warning!r}")
        elapsed = (time.time_ns() - backdated) // 1_000_000_000
        self.assertGreaterEqual(int(match[1]), self.BACKDATED_SECONDS, warning)
        self.assertLessEqual(int(match[1]), elapsed + 1, warning)

    def _old_lock(self, path: Path, owner: dict) -> None:
        path.mkdir()
        atomic_write_json(path / "owner.json", {"token": "a" * 32, "created_unix": time.time() - 7200, **owner})

    def test_lock_records_its_holder_start_time(self) -> None:
        probed: list[int] = []

        def probe(pid: int) -> ProcessStatus:
            probed.append(pid)
            return ProcessStatus(True, 1234)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            with ResourceLock(path, probe=probe):
                owner = read_json(path / "owner.json")
            self.assertEqual(os.getpid(), owner["pid"])
            self.assertEqual(1234, owner["start_time"])
            self.assertEqual([os.getpid()], probed)

    def test_old_lock_of_live_owner_is_not_reclaimed(self) -> None:
        cases: dict[str, tuple[dict[str, Any], ProcessStatus]] = {
            "same process": ({"pid": 4242, "start_time": 1234}, ProcessStatus(True, 1234)),
            "no recorded start time": ({"pid": 4242}, ProcessStatus(True, 1234)),
            "recorded start time not an integer": ({"pid": 4242, "start_time": True}, ProcessStatus(True, 1)),
            "recorded start time a string": ({"pid": 4242, "start_time": "1234"}, ProcessStatus(True, 1)),
            "recorded start time a float": ({"pid": 4242, "start_time": 1234.0}, ProcessStatus(True, 1)),
            "start time unreadable": ({"pid": 4242, "start_time": 1234}, ProcessStatus(True, None)),
            "pid not positive": ({"pid": 0, "start_time": 1234}, ProcessStatus(False, None)),
            "pid a string": ({"pid": "4242", "start_time": 1234}, ProcessStatus(False, None)),
            "pid a boolean": ({"pid": True, "start_time": 1234}, ProcessStatus(False, None)),
            "pid a float": ({"pid": 4242.0, "start_time": 1234}, ProcessStatus(False, None)),
            "pid an object": ({"pid": {}, "start_time": 1234}, ProcessStatus(False, None)),
        }
        for name, (owner, status) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "resource.lock"
                self._old_lock(path, owner)
                with (
                    self.assertRaisesRegex(PersistenceError, "Timed out"),
                    ResourceLock(path, timeout_seconds=0.01, probe=reporting(status)),
                ):
                    pass
                self.assertEqual(owner["pid"], read_json(path / "owner.json")["pid"])

    def test_old_lock_of_dead_or_reused_owner_is_reclaimed(self) -> None:
        cases = {
            "dead": ({"pid": 4242, "start_time": 1234}, ProcessStatus(False, None)),
            "dead without recorded start time": ({"pid": 4242}, ProcessStatus(False, None)),
            "pid reused": ({"pid": 4242, "start_time": 1234}, ProcessStatus(True, 5678)),
        }
        for name, (owner, status) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "resource.lock"
                self._old_lock(path, owner)

                def probe(pid: int, *, status: ProcessStatus = status) -> ProcessStatus:
                    return ProcessStatus(True, 9999) if pid == os.getpid() else status

                with ResourceLock(path, timeout_seconds=0.5, probe=probe):
                    self.assertEqual(os.getpid(), read_json(path / "owner.json")["pid"])
                self.assertFalse(path.exists())

    def test_recent_lock_of_live_owner_times_out_and_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            path.mkdir()
            owner = {"pid": 4242, "token": "a" * 32, "created_unix": time.time(), "start_time": 1}
            atomic_write_json(path / "owner.json", owner)
            started = time.monotonic()
            with (
                self.assertRaisesRegex(
                    PersistenceError, re.escape(f"Timed out waiting for lock {path}; delete it if no review is running")
                ),
                ResourceLock(path, timeout_seconds=0.2, probe=reporting(ProcessStatus(True, 1))),
            ):
                pass
            self.assertGreaterEqual(time.monotonic() - started, 0.2)
            self.assertEqual(owner, read_json(path / "owner.json"))
            self.assertEqual(["resource.lock"], [item.name for item in Path(temporary).iterdir()])

    def test_recent_lock_of_dead_owner_is_reclaimed_with_a_warning(self) -> None:
        # A dead holder blocked every review of the repository for an hour.
        cases = {
            "dead": (ProcessStatus(False, None), "PID 4242 is not running"),
            "pid reused": (ProcessStatus(True, 5678), "PID 4242 now names another process"),
        }
        for name, (status, reason) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "resource.lock"
                path.mkdir()
                atomic_write_json(
                    path / "owner.json",
                    {"pid": 4242, "token": "a" * 32, "created_unix": time.time(), "start_time": 1234},
                )

                def probe(pid: int, *, status: ProcessStatus = status) -> ProcessStatus:
                    return ProcessStatus(True, 9999) if pid == os.getpid() else status

                errors = io.StringIO()
                with contextlib.redirect_stderr(errors), ResourceLock(path, timeout_seconds=0.5, probe=probe):
                    self.assertEqual(os.getpid(), read_json(path / "owner.json")["pid"])
                self.assertEqual(f"WARNING: Reclaimed the stale lock {path}: {reason}.\n", errors.getvalue())
                self.assertEqual([], list(Path(temporary).iterdir()), "the stale lock is deleted")

    def test_lock_without_owner_file_is_reclaimed_after_the_grace_period(self) -> None:
        # A process killed between creating the lock and writing its owner left a lock nothing reclaimed.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            path.mkdir()
            with self.assertRaisesRegex(PersistenceError, "Timed out"), ResourceLock(path, timeout_seconds=0.1):
                pass
            self.assertTrue(path.is_dir(), "a lock without an owner file is kept during the grace period")
            backdated = self._backdate(path)
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors), ResourceLock(path, timeout_seconds=0.5):
                self.assertEqual(os.getpid(), read_json(path / "owner.json")["pid"])
            self._assert_reclaimed_after(
                errors.getvalue(), path, "it has had no owner file for {age} seconds", backdated
            )
            self.assertEqual([], list(Path(temporary).iterdir()))

    def test_reclaim_puts_back_a_lock_another_process_took_meanwhile(self) -> None:
        # Moving the stale lock aside is the atomic step; a lock taken between the judgment and the move is not it.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            self._old_lock(path, {"pid": 4242, "start_time": 1234})
            taken = {"pid": 5151, "token": "b" * 32, "created_unix": time.time(), "start_time": 1}

            def probe(pid: int) -> ProcessStatus:
                if pid == 4242:
                    shutil.rmtree(path)  # another waiter reclaims the stale lock and takes it
                    path.mkdir()
                    atomic_write_json(path / "owner.json", taken)
                    return ProcessStatus(False, None)
                return ProcessStatus(True, 1)

            with (
                self.assertRaisesRegex(PersistenceError, "Timed out"),
                ResourceLock(path, timeout_seconds=0.1, probe=probe),
            ):
                pass
            self.assertEqual(taken, read_json(path / "owner.json"))
            self.assertEqual(["resource.lock"], [item.name for item in Path(temporary).iterdir()])

    def test_release_succeeds_while_a_waiter_reads_the_owner_file(self) -> None:
        # A waiter reading the owner file made the holder's release fail, and the lock outlived both sessions.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                lock = ResourceLock(path)
                lock.__enter__()
                with review_io.open_shared(path / "owner.json") as reader:
                    lock.__exit__(None, None, None)
                    self.assertFalse(path.exists(), "the lock is released while the owner file is open")
                    self.assertEqual(lock.token, json.loads(reader.read())["token"])
                with (
                    mock.patch.object(review_io.time, "sleep", side_effect=AssertionError("waited")),
                    ResourceLock(path),
                ):
                    pass
            self.assertEqual("", errors.getvalue())
            self.assertEqual([], list(Path(temporary).iterdir()))

    def test_release_that_cannot_remove_the_lock_warns_and_a_later_lock_reclaims_it(self) -> None:
        # The holder's work is done; a lock left behind by a process outside the protocol is stale once it exits.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                lock = ResourceLock(path)
                lock.__enter__()
                with undeletable(path / "owner.json"):
                    lock.__exit__(None, None, None)
            self.assertRegex(
                errors.getvalue(),
                rf"^WARNING: Could not remove the lock {re.escape(str(path))}: .+\. A later review reclaims it once "
                r"this process has exited\.\n$",
            )
            self.assertTrue((path / "owner.json").is_file(), "the lock is left behind")
            lock.__exit__(None, None, None)  # released once: nothing more to do

            def probe(pid: int) -> ProcessStatus:
                return ProcessStatus(False, None) if pid == os.getpid() else ProcessStatus(True, 1)

            errors = io.StringIO()
            with contextlib.redirect_stderr(errors), ResourceLock(path, timeout_seconds=0.5, probe=probe):
                pass
            self.assertEqual(
                f"WARNING: Reclaimed the stale lock {path}: PID {os.getpid()} is not running.\n", errors.getvalue()
            )
            self.assertEqual([], list(Path(temporary).iterdir()))

    def test_release_still_refuses_a_lock_another_session_took(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            lock = ResourceLock(path)
            lock.__enter__()
            atomic_write_json(path / "owner.json", {"pid": os.getpid(), "token": "b" * 32})
            with self.assertRaisesRegex(PersistenceError, "Lock ownership changed before release"):
                lock.__exit__(None, None, None)
            self.assertEqual("b" * 32, read_json(path / "owner.json")["token"])

    def test_release_refuses_an_owner_record_of_the_wrong_kind(self) -> None:
        for content, found in (([], "an array"), ("owner", 'string "owner"'), (None, "null"), (7, "integer 7")):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "resource.lock"
                lock = ResourceLock(path)
                lock.__enter__()
                atomic_write_json(path / "owner.json", content)
                with self.assertRaisesRegex(
                    PersistenceError, f"^Lock ownership changed before release: {re.escape(str(path))}$"
                ) as context:
                    lock.__exit__(None, None, None)
                self.assertIsInstance(context.exception.__cause__, PersistenceFieldError)
                self.assertEqual(f"lock owner must be an object, found {found}", str(context.exception.__cause__))
                self.assertEqual(content, read_json(path / "owner.json"), "a refused release leaves the lock")

    def test_waiter_takes_a_lock_whose_owner_file_vanishes_while_it_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            path.mkdir()
            atomic_write_json(path / "owner.json", {"pid": 4242, "token": "a" * 32, "start_time": 1})
            opened: list[Path] = []

            def release_first(owner: Path) -> object:
                opened.append(owner)
                shutil.rmtree(path)  # the holder releases the lock between the waiter's stat and its read
                raise FileNotFoundError(2, "gone", str(owner))

            lock = ResourceLock(path, timeout_seconds=0.5, probe=reporting(ProcessStatus(True, 1)))
            errors = io.StringIO()
            with (
                mock.patch.object(review_io, "open_shared", side_effect=release_first),
                mock.patch.object(review_io.time, "sleep", side_effect=AssertionError("waited")),
                contextlib.redirect_stderr(errors),
                lock,
            ):
                self.assertEqual(lock.token, read_json(path / "owner.json")["token"])
            self.assertEqual([path / "owner.json"], opened)
            self.assertEqual("", errors.getvalue(), "a released lock is taken, not reclaimed")
            self.assertEqual([], list(Path(temporary).iterdir()))

    def test_torn_owner_file_is_kept_within_the_grace_period_and_reclaimed_after_it(self) -> None:
        cases = {
            "truncated": b'{"pid": 4242, "tok',
            "empty": b"",
            "not an object": b'["a"]',
            "no token": b'{"pid": 4242}',
            "empty token": b'{"pid": 4242, "token": ""}',
            "token an integer": b'{"pid": 4242, "token": 7}',
            "token an array": b'{"pid": 4242, "token": ["a"]}',
            "token an object": b'{"pid": 4242, "token": {}}',
            "token null": b'{"pid": 4242, "token": null}',
            "a string": b'"owner"',
            "null": b"null",
            "an integer": b"4242",
            "not UTF-8": b'{"token": "\xff"}',
            "oversized": b'{"token": "' + b"a" * (64 * 1024) + b'"}',
        }
        for name, content in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "resource.lock"
                path.mkdir()
                owner = path / "owner.json"
                owner.write_bytes(content)
                with self.assertRaisesRegex(PersistenceError, "Timed out"), ResourceLock(path, timeout_seconds=0.1):
                    pass
                self.assertEqual(content, owner.read_bytes(), "a torn owner file is kept during the grace period")
                backdated = self._backdate(owner)
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors), ResourceLock(path, timeout_seconds=0.5):
                    self.assertEqual(os.getpid(), read_json(owner)["pid"])
                self._assert_reclaimed_after(
                    errors.getvalue(),
                    path,
                    "its owner file has not been a valid owner record for {age} seconds",
                    backdated,
                )
                self.assertEqual([], list(Path(temporary).iterdir()))

    def test_lock_probes_identity_by_default_and_never_signals(self) -> None:
        probed: list[int] = []

        def probe(pid: int) -> ProcessStatus:
            probed.append(pid)
            return ProcessStatus(pid == os.getpid(), 1234 if pid == os.getpid() else None)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resource.lock"
            self._old_lock(path, {"pid": 4242, "start_time": 1234})
            with (
                mock.patch.object(review_io, "process_status", probe),
                mock.patch("os.kill", side_effect=AssertionError("os.kill called")),
                ResourceLock(path, timeout_seconds=0.5),
            ):
                self.assertEqual(1234, read_json(path / "owner.json")["start_time"])
            self.assertIn(4242, probed)

    def test_atomic_json_is_restrictive_and_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            atomic_write_json(path, {"answer": 42})
            self.assertEqual({"answer": 42}, read_json(path))
            if os.name != "nt":
                self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_atomic_json_gives_the_replacement_the_mode_it_is_asked_for(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            atomic_write_json(path, {"answer": 42}, mode=0o644)
            self.assertEqual({"answer": 42}, read_json(path))
            if os.name != "nt":  # Windows keeps no permission bits but read-only
                self.assertEqual(0o644, path.stat().st_mode & 0o777)

"""Regression tests for the one lock the operating system holds for its holder and releases when the holder ends."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import file_lock

# A child that takes the lock of the file its argument names, says so, and waits to be ended.
HOLDER = (
    "import sys, time\n"
    f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
    "import file_lock\n"
    "from pathlib import Path\n"
    "handle = file_lock.take(Path(sys.argv[1]))\n"
    "print('held' if handle is not None else 'refused', flush=True)\n"
    "time.sleep(600)\n"
)
# How long the kernel may take to release a killed holder's lock: Windows releases it once the process's handles are
# closed, which may come a little after the process ends.
RELEASE_SECONDS = 30.0


class FileLockTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "resource.held"

    def take(self) -> int:
        handle = file_lock.take(self.path)
        assert handle is not None
        return handle

    def test_a_held_lock_refuses_every_other_open_in_this_process_until_its_release_deletes_the_file(self) -> None:
        handle = self.take()
        self.assertIsNone(file_lock.take(self.path))
        self.assertTrue(file_lock.held_elsewhere(self.path))
        self.assertEqual(b"", self.path.read_bytes(), "a holder's file stays readable")
        file_lock.release(handle, self.path)
        self.assertFalse(self.path.exists())
        self.assertFalse(file_lock.held_elsewhere(self.path))
        self.assertFalse(self.path.exists(), "asking whether a lock is held creates nothing")
        file_lock.release(self.take(), self.path)

    def test_a_free_lock_is_answered_free_and_left_in_place(self) -> None:
        self.path.write_bytes(b"")
        self.assertFalse(file_lock.held_elsewhere(self.path))
        self.assertTrue(self.path.exists())
        file_lock.release(self.take(), self.path)

    def test_the_lock_of_a_holder_that_is_killed_is_released_by_the_operating_system(self) -> None:
        child = subprocess.Popen(
            [sys.executable, "-I", "-c", HOLDER, str(self.path)], stdout=subprocess.PIPE, text=True, encoding="utf-8"
        )
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        assert child.stdout is not None
        self.assertEqual("held", child.stdout.readline().strip())
        self.assertIsNone(file_lock.take(self.path))
        self.assertTrue(file_lock.held_elsewhere(self.path))
        child.kill()
        child.wait()
        deadline = time.monotonic() + RELEASE_SECONDS
        handle = file_lock.take(self.path)
        while handle is None and time.monotonic() < deadline:
            time.sleep(0.1)
            handle = file_lock.take(self.path)
        self.assertIsNotNone(handle, "the killed holder's lock was never released")
        assert handle is not None
        file_lock.release(handle, self.path)

    def test_a_lock_the_file_system_refuses_is_a_file_lock_error_and_closes_its_descriptor(self) -> None:
        opened: list[int] = []

        def open_file(path: Path) -> int:
            opened.append(file_lock.open_for_lock(path))
            return opened[-1]

        with (
            mock.patch.object(file_lock, "_lock", side_effect=OSError(errno.ENOLCK, "No locks available")),
            self.assertRaises(file_lock.FileLockError) as raised,
        ):
            file_lock.take(self.path, open_file)
        self.assertEqual((errno.ENOLCK, "No locks available"), (raised.exception.errno, raised.exception.strerror))
        with self.assertRaises(OSError):
            os.fstat(opened[0])
        with (
            mock.patch.object(file_lock, "_lock", side_effect=OSError(errno.ENOLCK, "No locks available")),
            self.assertRaises(file_lock.FileLockError),
        ):
            file_lock.held_elsewhere(self.path)

    def test_only_a_refusal_that_says_the_file_system_has_no_lock_is_unsupported(self) -> None:
        for number in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
            with self.subTest(errno=errno.errorcode[number]):
                self.assertTrue(file_lock.FileLockError(number, "unsupported").unsupported)
        for number in (errno.ENOLCK, errno.EIO, errno.EBADF, errno.EINVAL):
            with self.subTest(errno=errno.errorcode[number]):
                self.assertFalse(file_lock.FileLockError(number, "transient").unsupported)

    def test_every_error_a_held_or_interrupted_lock_gives_reads_as_not_taken(self) -> None:
        for number in sorted(file_lock.HELD_ERRNOS | {errno.EINTR}):
            with (
                self.subTest(errno=errno.errorcode[number]),
                mock.patch.object(file_lock, "_lock", side_effect=OSError(number, "held")),
            ):
                self.assertIsNone(file_lock.take(self.path))
                self.assertTrue(file_lock.held_elsewhere(self.path))

    def test_an_open_that_fails_is_raised_as_it_came(self) -> None:
        missing = self.path.parent / "missing" / "resource.held"
        with self.assertRaises(FileNotFoundError):
            file_lock.take(missing)
        self.assertFalse(file_lock.held_elsewhere(missing))

    def test_a_release_whose_deletion_or_unlock_fails_still_lets_the_lock_go(self) -> None:
        handle = self.take()

        def refused(path: Path) -> None:
            raise PermissionError(errno.EACCES, "Access is denied", str(path))

        with mock.patch.object(file_lock, "_unlock", side_effect=OSError(errno.EBADF, "bad")):
            file_lock.release(handle, self.path, refused)
        self.assertTrue(self.path.exists(), "a file that cannot be deleted stays for the next holder")
        with self.assertRaises(OSError):
            os.fstat(handle)
        file_lock.release(self.take(), self.path)
        self.assertFalse(self.path.exists())

    @unittest.skipIf(sys.platform == "win32", "Windows deletes no file another handle holds open")
    def test_a_waiter_that_locks_the_file_a_release_deleted_does_not_hold_the_lock(self) -> None:
        holder = self.take()

        def open_then_release(path: Path) -> int:
            descriptor = file_lock.open_for_lock(path)
            file_lock.release(holder, path)  # deletes the file this waiter holds open
            return descriptor

        self.assertIsNone(file_lock.take(self.path, open_then_release))
        file_lock.release(self.take(), self.path)

    @unittest.skipIf(sys.platform != "win32", "only Windows refuses to delete a file another handle holds open")
    def test_a_waiter_holding_the_file_open_keeps_it_through_a_release_and_takes_its_lock(self) -> None:
        holder = self.take()

        def open_then_release(path: Path) -> int:
            descriptor = file_lock.open_for_lock(path)
            file_lock.release(holder, path)  # cannot delete the file this waiter holds open
            return descriptor

        handle = file_lock.take(self.path, open_then_release)
        self.assertIsNotNone(handle)
        assert handle is not None
        self.assertTrue(self.path.exists())
        file_lock.release(handle, self.path)
        self.assertFalse(self.path.exists())

"""Regression tests for the one test of whether a file a reader may find absent is there."""

from __future__ import annotations

import errno
import os
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from file_presence import NotAFile, is_present

# The errno values Python gives a directory and an invalid argument on Windows and on Linux alike, written out so the
# refusal is held to them, not to the constants it uses.
EISDIR = 21
EINVAL = 22


def stat_answers(failure: OSError, *, failed_follow: bool) -> Callable[..., os.stat_result]:
    """A stand-in for Path.stat that raises `failure` from the stat through links when `failed_follow`, and otherwise
    answers that stat "not found" and raises `failure` from the stat of the link itself."""

    def answer(follow_symlinks: bool = True) -> os.stat_result:
        if follow_symlinks and not failed_follow:
            raise FileNotFoundError(errno.ENOENT, "missing")
        raise failure

    return answer


class FilePresenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_a_file_is_present_and_nothing_is_absent(self) -> None:
        path = self.root / "settings.json"
        self.assertFalse(is_present(path))
        path.write_text("{}", encoding="utf-8")
        self.assertTrue(is_present(path))

    def test_a_directory_in_the_file_place_is_refused_as_an_os_error_naming_it(self) -> None:
        path = self.root / "settings.json"
        path.mkdir()
        with self.assertRaises(NotAFile) as raised:
            is_present(path)
        self.assertIsInstance(raised.exception, OSError)
        self.assertEqual(str(path), raised.exception.filename)
        self.assertEqual("not a file", raised.exception.strerror)
        self.assertEqual(EISDIR, raised.exception.errno)

    def test_a_missing_folder_above_the_file_is_absent(self) -> None:
        self.assertFalse(is_present(self.root / "missing" / "settings.json"))

    @unittest.skipIf(sys.platform == "win32", "a symbolic link to a file needs a privilege on Windows")
    def test_a_link_to_nothing_is_refused_not_read_as_absent(self) -> None:
        path = self.root / "settings.json"
        path.symlink_to(self.root / "gone.json")
        with self.assertRaises(NotAFile) as raised:
            is_present(path)
        self.assertEqual(EINVAL, raised.exception.errno)
        self.assertEqual(str(path), raised.exception.filename)

    def test_a_stat_that_fails_with_anything_but_not_found_reaches_the_caller(self) -> None:
        path = self.root / "settings.json"
        path.write_text("{}", encoding="utf-8")
        # Every error a stat raises that is not "nothing is there", generated from the errno catalogue the stat can
        # answer with, so a new reading of absence fails a row.
        failures = [
            PermissionError(errno.EACCES, "denied"),
            PermissionError(errno.EPERM, "not permitted"),
            OSError(errno.EIO, "input/output error"),
            OSError(errno.ELOOP, "too many levels of symbolic links"),
            OSError(errno.ENAMETOOLONG, "file name too long"),
            OSError(errno.EINVAL, "invalid argument"),
        ]
        for failure in failures:
            for followed in (True, False):
                with self.subTest(failure=failure, follow_symlinks=followed):
                    # The stat of the link itself is made only after the stat through it answered "not found", as for
                    # a link to nothing.
                    with (
                        mock.patch.object(Path, "stat", side_effect=stat_answers(failure, failed_follow=followed)),
                        self.assertRaises(OSError) as raised,
                    ):
                        is_present(path)
                    self.assertIs(failure, raised.exception)

    def test_only_not_found_and_a_file_above_read_as_absent(self) -> None:
        for absent in (
            FileNotFoundError(errno.ENOENT, "missing"),
            NotADirectoryError(errno.ENOTDIR, "not a directory"),
        ):
            with (
                self.subTest(error=absent),
                mock.patch.object(Path, "stat", side_effect=absent),
            ):
                self.assertFalse(is_present(self.root / "settings.json"))
        (self.root / "file").write_text("", encoding="utf-8")
        self.assertFalse(is_present(self.root / "file" / "settings.json"))

    def test_a_path_holding_a_nul_is_refused_as_an_os_error_not_read_as_absent(self) -> None:
        for path in (Path("a\0b"), self.root / "settings\0.json"):
            with self.subTest(path=path), self.assertRaises(OSError) as raised:
                is_present(path)
            self.assertEqual(EINVAL, raised.exception.errno)
            self.assertEqual("the path holds a NUL character, which no file name holds", raised.exception.strerror)
            self.assertEqual(str(path), raised.exception.filename)
            self.assertNotIsInstance(raised.exception, NotAFile)

    def test_a_path_whose_link_stat_answers_where_its_target_is_missing_is_refused_naming_it(self) -> None:
        # The stat a link to nothing gives on every platform: the target is missing, the link itself is there.
        path = self.root / "settings.json"
        link = os.stat_result((0o120777, 0, 0, 1, 0, 0, 0, 0, 0, 0))

        def answer(follow_symlinks: bool = True) -> os.stat_result:
            if follow_symlinks:
                raise FileNotFoundError(errno.ENOENT, "missing")
            return link

        with mock.patch.object(Path, "stat", side_effect=answer), self.assertRaises(NotAFile) as raised:
            is_present(path)
        self.assertEqual((EINVAL, str(path)), (raised.exception.errno, raised.exception.filename))

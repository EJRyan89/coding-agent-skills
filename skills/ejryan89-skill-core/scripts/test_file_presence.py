"""Regression tests for the one test of whether a file a reader may find absent is there."""

from __future__ import annotations

import errno
import os
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from file_presence import NotADirectory, NotAFile, is_directory, is_occupied, is_present, matching

# The errno values Python gives a directory and an invalid argument on Windows and on Linux alike, written out so the
# refusal is held to them, not to the constants it uses.
EISDIR = 21
EINVAL = 22
ENOTDIR = 20


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

    def test_a_caller_choosing_files_among_entries_reads_something_else_as_no_file_and_a_failed_stat_still_raises(
        self,
    ) -> None:
        folder = self.root / "notes.md"
        folder.mkdir()
        self.assertFalse(is_present(folder, other_is_absent=True))
        self.assertFalse(is_present(self.root / "missing.md", other_is_absent=True))
        (self.root / "file.md").write_text("", encoding="utf-8")
        self.assertTrue(is_present(self.root / "file.md", other_is_absent=True))
        with self.assertRaises(NotAFile):
            is_present(folder, other_is_absent=False)
        failure = PermissionError(errno.EACCES, "denied")
        with mock.patch.object(Path, "stat", side_effect=failure), self.assertRaises(OSError) as raised:
            is_present(folder, other_is_absent=True)
        self.assertIs(failure, raised.exception)

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


# Each stat error that is not "nothing is there", the rows FilePresenceTests raises, so every helper is held to them.
STAT_FAILURES = (
    PermissionError(errno.EACCES, "denied"),
    PermissionError(errno.EPERM, "not permitted"),
    OSError(errno.EIO, "input/output error"),
    OSError(errno.ELOOP, "too many levels of symbolic links"),
    OSError(errno.ENAMETOOLONG, "file name too long"),
    OSError(errno.EINVAL, "invalid argument"),
)
HELPERS = (is_present, is_directory, is_occupied)


class DirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_a_folder_is_a_directory_and_nothing_is_absent(self) -> None:
        path = self.root / "rules"
        self.assertFalse(is_directory(path))
        self.assertFalse(is_directory(self.root / "missing" / "rules"))
        path.mkdir()
        self.assertTrue(is_directory(path))

    def test_a_file_in_the_folder_place_is_refused_as_an_os_error_naming_it(self) -> None:
        path = self.root / "rules"
        path.write_text("", encoding="utf-8")
        with self.assertRaises(NotADirectory) as raised:
            is_directory(path)
        self.assertEqual(
            (ENOTDIR, "not a directory", str(path)),
            (raised.exception.errno, raised.exception.strerror, raised.exception.filename),
        )
        # A NotADirectoryError is what the stat raises for a file above the path, which reads as absent.
        self.assertNotIsInstance(raised.exception, NotADirectoryError)
        self.assertFalse(is_directory(path / "below"))
        # A caller choosing folders among other entries reads the file as no folder, and a failed stat still raises.
        self.assertFalse(is_directory(path, other_is_absent=True))
        (self.root / "folder").mkdir()
        self.assertTrue(is_directory(self.root / "folder", other_is_absent=True))
        failure = PermissionError(errno.EACCES, "denied")
        with mock.patch.object(Path, "stat", side_effect=failure), self.assertRaises(OSError) as raised_stat:
            is_directory(path, other_is_absent=True)
        self.assertIs(failure, raised_stat.exception)

    def test_a_link_to_nothing_is_refused_by_the_folder_test_and_occupies_its_place(self) -> None:
        path = self.root / "rules"
        link = os.stat_result((0o120777, 0, 0, 1, 0, 0, 0, 0, 0, 0))

        def answer(follow_symlinks: bool = True) -> os.stat_result:
            if follow_symlinks:
                raise FileNotFoundError(errno.ENOENT, "missing")
            return link

        with mock.patch.object(Path, "stat", side_effect=answer):
            with self.assertRaises(NotADirectory):
                is_directory(path)
            self.assertTrue(is_occupied(path))


class OccupiedTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_a_file_or_a_folder_occupies_its_place_and_nothing_does_not(self) -> None:
        self.assertFalse(is_occupied(self.root / "entry"))
        self.assertFalse(is_occupied(self.root / "missing" / "entry"))
        (self.root / "file").write_text("", encoding="utf-8")
        (self.root / "folder").mkdir()
        self.assertTrue(is_occupied(self.root / "file"))
        self.assertTrue(is_occupied(self.root / "folder"))
        self.assertFalse(is_occupied(self.root / "file" / "entry"))

    def test_the_place_is_read_without_following_a_link(self) -> None:
        with mock.patch.object(
            Path, "stat", return_value=os.stat_result((0o120777, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        ) as made:
            self.assertTrue(is_occupied(self.root / "entry"))
        made.assert_called_once_with(follow_symlinks=False)


class EveryHelperTests(unittest.TestCase):
    def test_a_stat_that_fails_with_anything_but_not_found_reaches_the_caller_of_every_helper(self) -> None:
        for helper in HELPERS:
            for failure in STAT_FAILURES:
                with (
                    self.subTest(helper=helper.__name__, failure=failure),
                    mock.patch.object(Path, "stat", side_effect=failure),
                    self.assertRaises(OSError) as raised,
                ):
                    helper(Path("entry"))
                self.assertIs(failure, raised.exception)

    def test_only_not_found_and_a_file_above_read_as_absent_by_every_helper(self) -> None:
        for helper in HELPERS:
            for absent in (
                FileNotFoundError(errno.ENOENT, "missing"),
                NotADirectoryError(errno.ENOTDIR, "not a directory"),
            ):
                with (
                    self.subTest(helper=helper.__name__, error=absent),
                    mock.patch.object(Path, "stat", side_effect=absent),
                ):
                    self.assertFalse(helper(Path("entry")))

    def test_a_path_holding_a_nul_is_refused_by_every_helper(self) -> None:
        for helper in HELPERS:
            with self.subTest(helper=helper.__name__), self.assertRaises(OSError) as raised:
                helper(Path("a\0b"))
            self.assertEqual(EINVAL, raised.exception.errno)


# The glob patterns the repository lists folders with, each matched over a tree that holds a hidden file, a name a
# bracket and a question mark tell apart, a nested folder, and a file whose name is one of the folders'.
MATCHED_PATTERNS = (
    "*",
    "*.py",
    "**/*",
    "**/*.py",
    "*/SKILL.md",
    "**/scripts/*",
    "sub/*",
    "s?b/*.py",
    "[ab].*",
    "**/deep/*.py",
    "*.y*ml",
)
TREE = (
    "a.py",
    "b.txt",
    ".hidden.py",
    "ci.yaml",
    "sub/c.py",
    "sub/SKILL.md",
    "sub/deep/d.py",
    "sub/scripts/e.py",
    "other/SKILL.md",
    "other/scripts/deep/f.py",
)


class MatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in TREE:
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text("", encoding="utf-8")

    def test_it_matches_what_path_glob_matches_in_a_folder_it_can_list(self) -> None:
        for pattern in MATCHED_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertEqual(sorted(self.root.glob(pattern)), matching(self.root, pattern))

    def test_nothing_at_the_folder_matches_nothing_and_something_else_is_refused(self) -> None:
        self.assertEqual([], matching(self.root / "missing", "*"))
        self.assertEqual([], matching(self.root / "a.py" / "below", "*"))
        with self.assertRaises(NotADirectory):
            matching(self.root / "a.py", "*")

    def test_a_folder_it_cannot_list_raises_where_glob_would_match_nothing(self) -> None:
        # The folder itself, one a wildcard part enters, and one a `**` part enters, each by every pattern form.
        real = os.scandir
        for unreadable, pattern in (
            (self.root, "*.py"),
            (self.root / "sub", "*/SKILL.md"),
            (self.root / "sub" / "deep", "**/*.py"),
            (self.root / "other" / "scripts", "**/scripts/*"),
        ):
            failure = PermissionError(errno.EACCES, "denied", str(unreadable))

            def listing(path: Path, unreadable: Path = unreadable, failure: OSError = failure) -> object:
                if Path(path) == unreadable:
                    raise failure
                return real(path)

            with (
                self.subTest(folder=unreadable.relative_to(self.root).as_posix(), pattern=pattern),
                mock.patch.object(os, "scandir", side_effect=listing),
                self.assertRaises(PermissionError) as raised,
            ):
                matching(self.root, pattern)
            self.assertIs(failure, raised.exception)

    def test_a_stat_of_the_folder_that_fails_reaches_the_caller(self) -> None:
        for failure in STAT_FAILURES:
            with (
                self.subTest(failure=failure),
                mock.patch.object(Path, "stat", side_effect=failure),
                self.assertRaises(OSError) as raised,
            ):
                matching(self.root, "*")
            self.assertIs(failure, raised.exception)

    def test_a_skipped_folder_is_never_listed_and_one_it_cannot_list_elsewhere_still_raises(self) -> None:
        (self.root / "sub" / "node_modules" / "deep").mkdir(parents=True)
        (self.root / "sub" / "node_modules" / "deep" / "x.py").write_text("", encoding="utf-8")
        real = os.scandir

        def refusing(folder: Path) -> Any:
            def listing(path: Any = ".") -> Any:
                if Path(path) == folder:
                    raise PermissionError(errno.EACCES, "denied", str(folder))
                return real(path)

            return listing

        def skip(folder: Path) -> bool:
            return folder.name == "node_modules"

        with mock.patch.object(os, "scandir", refusing(self.root / "sub" / "node_modules" / "deep")):
            found = matching(self.root, "**/*.py", skip=skip)
            self.assertNotIn(self.root / "sub" / "node_modules" / "deep" / "x.py", found)
            self.assertIn(self.root / "sub" / "c.py", found)
            self.assertIn(self.root / "sub" / "node_modules", matching(self.root, "sub/*", skip=skip))
        with (
            mock.patch.object(os, "scandir", refusing(self.root / "sub" / "deep")),
            self.assertRaises(PermissionError),
        ):
            matching(self.root, "**/*.py", skip=skip)

    def test_a_pattern_it_cannot_read_alike_on_every_python_is_refused(self) -> None:
        for pattern in ("", "/abs", "a//b", "./a", "../a", "a/..", "**", "sub/**"):
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                matching(self.root, pattern)

    @unittest.skipIf(sys.platform == "win32", "a symbolic link to a folder needs a privilege on Windows")
    def test_a_double_star_enters_no_link_to_a_folder_but_a_wildcard_part_does(self) -> None:
        (self.root / "linked").symlink_to(self.root / "sub", target_is_directory=True)
        self.assertEqual(sorted(self.root.glob("**/c.py")), matching(self.root, "**/c.py"))
        self.assertIn(self.root / "linked" / "c.py", matching(self.root, "*/c.py"))

"""Regression tests for the folder a script keeps the user's own state in, under the temporary directory."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import private_folder
from private_folder import UnsafeFolderError, ensure, location, refusal

# The modes and the id the rules are held to, written out rather than read from the module's constants.
OWNER_ONLY = 0o700
DIRECTORY = stat.S_IFDIR
OTHER_USER = 4242


def metadata(mode: int, owner: int) -> os.stat_result:
    """A folder's own `lstat` result, as far as the check reads it."""
    return cast(os.stat_result, SimpleNamespace(st_mode=mode, st_uid=owner, st_file_attributes=0))


class LocationTests(unittest.TestCase):
    def test_the_folder_carries_the_user_s_id_where_the_platform_has_one(self) -> None:
        base = Path("base")
        with mock.patch.object(private_folder, "user_id", return_value=1000):
            self.assertEqual(base / "state-1000", location("state", base))
        with mock.patch.object(private_folder, "user_id", return_value=None):
            self.assertEqual(base / "state", location("state", base))

    def test_the_folder_lies_in_the_temporary_directory_by_default(self) -> None:
        self.assertEqual(Path(tempfile.gettempdir()), location("state").parent)
        self.assertTrue(location("state").name.startswith("state"))


class RefusalTests(unittest.TestCase):
    """The rule, judged from a folder's metadata, so an owner no test can make is judged as a real one would be."""

    folder = Path("seals")

    def test_a_directory_the_user_owns_that_only_they_can_write_is_accepted(self) -> None:
        for mode in (OWNER_ONLY, 0o755, 0o500):
            with self.subTest(mode=oct(mode)):
                self.assertIsNone(refusal(self.folder, metadata(DIRECTORY | mode, 1000), 1000))

    def test_a_directory_another_user_owns_is_refused_naming_both_ids(self) -> None:
        self.assertEqual(
            f"{self.folder} is owned by user id {OTHER_USER}, not by this user (id 1000)",
            refusal(self.folder, metadata(DIRECTORY | OWNER_ONLY, OTHER_USER), 1000),
        )

    def test_a_directory_its_group_or_anyone_can_write_is_refused_naming_its_mode(self) -> None:
        for mode in (0o720, 0o702, 0o777, 0o1777):
            with self.subTest(mode=oct(mode)):
                self.assertEqual(
                    f"{self.folder} can be written by users other than its owner (mode {mode:o})",
                    refusal(self.folder, metadata(DIRECTORY | mode, 1000), 1000),
                )

    def test_a_link_or_a_file_is_refused_even_where_the_platform_has_no_user_ids(self) -> None:
        for user in (1000, None):
            with self.subTest(user=user):
                self.assertEqual(
                    f"{self.folder} is a link or junction, not a folder of this user's own",
                    refusal(self.folder, metadata(stat.S_IFLNK | 0o777, 1000), user),
                )
                self.assertEqual(
                    f"{self.folder} is not a directory",
                    refusal(self.folder, metadata(stat.S_IFREG | 0o600, 1000), user),
                )

    def test_with_no_user_ids_a_directory_s_owner_and_mode_are_not_judged(self) -> None:
        self.assertIsNone(refusal(self.folder, metadata(DIRECTORY | 0o777, OTHER_USER), None))


class EnsureTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_a_missing_folder_is_made_owner_only_and_returned(self) -> None:
        folder = self.root / "state"
        self.assertEqual(folder, ensure(folder))
        self.assertTrue(folder.is_dir())
        if private_folder.user_id() is not None:
            self.assertEqual(OWNER_ONLY, stat.S_IMODE(folder.stat().st_mode))
        self.assertEqual(folder, ensure(folder), "a folder already there that passes is used as it is")

    @unittest.skipIf(sys.platform == "win32", "Windows mode bits say nothing about who may write a folder")
    def test_a_folder_others_can_write_is_refused_and_left_as_it_is(self) -> None:
        folder = self.root / "state"
        folder.mkdir()
        folder.chmod(0o777)
        with self.assertRaises(UnsafeFolderError) as raised:
            ensure(folder)
        self.assertEqual(f"{folder} can be written by users other than its owner (mode 777)", str(raised.exception))
        self.assertEqual(0o777, stat.S_IMODE(folder.stat().st_mode))

    def test_a_folder_another_user_owns_is_refused(self) -> None:
        folder = self.root / "state"
        folder.mkdir()
        with mock.patch.object(private_folder, "user_id", return_value=OTHER_USER), self.assertRaises(
            UnsafeFolderError
        ) as raised:
            ensure(folder)
        self.assertTrue(str(raised.exception).startswith(f"{folder} is owned by user id "), raised.exception)

    @unittest.skipIf(sys.platform == "win32", "a symbolic link needs a privilege there; refusal tests cover its kind")
    def test_a_link_to_a_folder_of_the_user_s_own_is_refused_and_nothing_is_made_through_it(self) -> None:
        target = self.root / "target"
        target.mkdir(mode=OWNER_ONLY)
        folder = self.root / "state"
        folder.symlink_to(target, target_is_directory=True)
        dangling = self.root / "dangling"
        dangling.symlink_to(self.root / "nowhere", target_is_directory=True)
        for link in (folder, dangling):
            with self.subTest(link=link.name), self.assertRaises(UnsafeFolderError) as raised:
                ensure(link)
            self.assertEqual(f"{link} is a link or junction, not a folder of this user's own", str(raised.exception))
        self.assertFalse((self.root / "nowhere").exists(), "no folder was made where the dangling link points")

    def test_a_file_in_the_folder_s_place_is_refused(self) -> None:
        folder = self.root / "state"
        folder.write_text("x", encoding="utf-8")
        with self.assertRaises(UnsafeFolderError) as raised:
            ensure(folder)
        self.assertEqual(f"{folder} is not a directory", str(raised.exception))

    def test_a_missing_parent_is_raised_rather_than_made(self) -> None:
        with self.assertRaises(FileNotFoundError):
            ensure(self.root / "missing" / "state")

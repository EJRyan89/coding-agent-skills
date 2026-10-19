"""Regression tests for the one test of whether a path is a symbolic link or a Windows junction."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from link_probe import is_link_or_junction, is_plain_directory, is_plain_file

# The value Windows gives a reparse point's attribute, written out so the rule is held to it, not to the constant.
REPARSE_POINT = 0x400


def link_folder(target: Path, link: Path) -> None:
    """A real link to a folder: a junction on Windows, which needs no privilege, else a symbolic link."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def metadata(mode: int, attributes: int = 0) -> os.stat_result:
    """A listing's `lstat` result, as far as the probe reads it."""
    return cast(os.stat_result, SimpleNamespace(st_mode=mode, st_file_attributes=attributes))


class LinkProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.folder = self.root / "folder"
        self.folder.mkdir()
        self.file = self.root / "file.txt"
        self.file.write_text("text\n", encoding="utf-8")

    def test_a_plain_folder_and_file_are_no_links(self) -> None:
        self.assertFalse(is_link_or_junction(self.folder))
        self.assertFalse(is_link_or_junction(self.file))
        self.assertTrue(is_plain_directory(self.folder))
        self.assertTrue(is_plain_file(self.file))
        self.assertFalse(is_plain_directory(self.file))
        self.assertFalse(is_plain_file(self.folder))

    def test_a_link_to_a_folder_is_a_link_and_never_plain(self) -> None:
        link = self.root / "link"
        link_folder(self.folder, link)
        self.assertTrue(is_link_or_junction(link))
        self.assertTrue(is_link_or_junction(link, os.lstat(link)))
        self.assertFalse(is_plain_directory(link))
        self.assertFalse(is_plain_file(link))

    def test_a_dangling_link_is_still_a_link(self) -> None:
        gone = self.root / "gone"
        gone.mkdir()
        link = self.root / "dangling"
        link_folder(gone, link)
        gone.rmdir()
        self.assertTrue(is_link_or_junction(link))
        self.assertFalse(is_plain_directory(link))

    def test_a_symbolic_link_is_judged_from_its_own_mode(self) -> None:
        self.assertTrue(is_link_or_junction(self.file, metadata(stat.S_IFLNK)))
        self.assertFalse(is_plain_file(self.file, metadata(stat.S_IFLNK)))

    def test_any_reparse_point_is_a_link_where_the_platform_marks_one(self) -> None:
        with mock.patch.object(stat, "FILE_ATTRIBUTE_REPARSE_POINT", REPARSE_POINT, create=True):
            for mode in (stat.S_IFDIR, stat.S_IFREG):
                with self.subTest(mode=mode):
                    self.assertTrue(is_link_or_junction(self.folder, metadata(mode, REPARSE_POINT)))
                    self.assertFalse(is_link_or_junction(self.folder, metadata(mode, 0x20)))
            self.assertFalse(is_plain_directory(self.folder, metadata(stat.S_IFDIR, REPARSE_POINT)))
            self.assertFalse(is_plain_file(self.file, metadata(stat.S_IFREG, REPARSE_POINT)))
            self.assertTrue(is_plain_file(self.file, metadata(stat.S_IFREG, 0x20)))

    def test_a_path_that_cannot_be_read_is_raised_by_the_probe_and_plain_to_neither_test(self) -> None:
        missing = self.root / "missing"
        with self.assertRaises(FileNotFoundError):
            is_link_or_junction(missing)
        self.assertFalse(is_plain_directory(missing))
        self.assertFalse(is_plain_file(missing))

        def refused(path: Any) -> os.stat_result:
            raise PermissionError(13, "Permission denied", str(path))

        with mock.patch.object(os, "lstat", refused):
            with self.assertRaises(PermissionError):
                is_link_or_junction(self.folder)
            self.assertFalse(is_plain_directory(self.folder))
            self.assertFalse(is_plain_file(self.file))

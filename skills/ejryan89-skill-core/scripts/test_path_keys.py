"""Regression tests for the one form a path is compared in."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from path_keys import path_key


def link_folder(target: Path, link: Path) -> None:
    """A real link to a folder: a junction on Windows, which needs no privilege, else a symbolic link."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


class PathKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def test_two_spellings_of_one_path_have_one_key(self) -> None:
        folder = self.root / "a folder"
        self.assertEqual(path_key(folder), path_key(self.root / "other" / ".." / "a folder"))
        self.assertEqual(path_key(folder), path_key(str(folder) + os.sep))
        self.assertNotEqual(path_key(folder), path_key(self.root / "another"))

    def test_a_relative_path_is_keyed_against_the_working_directory(self) -> None:
        self.assertEqual(path_key(Path.cwd() / "x"), path_key("x"))
        self.assertEqual(path_key(Path.cwd().parent / "x"), path_key(Path("..") / "x"))

    def test_case_folds_where_the_platform_folds_it(self) -> None:
        upper, lower = path_key(self.root / "Work"), path_key(self.root / "work")
        self.assertEqual(sys.platform == "win32", upper == lower)

    def test_the_lexical_key_follows_no_link_and_the_resolved_key_does(self) -> None:
        target = self.root / "target"
        target.mkdir()
        link = self.root / "link"
        link_folder(target, link)
        self.assertNotEqual(path_key(link), path_key(target))
        self.assertEqual(path_key(link, resolve=True), path_key(target, resolve=True))
        self.assertEqual(path_key(self.root / "missing" / ".."), path_key(self.root, resolve=True))

    def test_the_textual_key_folds_dots_and_case_and_keeps_what_windows_would_strip(self) -> None:
        folder = self.root / "a folder"
        self.assertEqual(path_key(folder), path_key(self.root / "other" / ".." / "a folder", textual=True))
        self.assertEqual(path_key(Path.cwd() / "x"), path_key("x", textual=True))
        self.assertEqual(path_key(Path.cwd().parent / "x"), path_key(Path("..") / "x", textual=True))
        upper, lower = path_key(self.root / "Work", textual=True), path_key(self.root / "work", textual=True)
        self.assertEqual(sys.platform == "win32", upper == lower)
        trailing = str(self.root / "name. ")
        self.assertEqual(os.path.normcase(trailing), path_key(trailing, textual=True))
        # Windows' own parsing drops a name's trailing dots and spaces; the lexical key follows it, the textual not.
        self.assertEqual(sys.platform == "win32", path_key(trailing) != path_key(trailing, textual=True))

    def test_a_key_is_never_both_resolved_and_textual(self) -> None:
        with self.assertRaises(ValueError) as raised:
            path_key(self.root, resolve=True, textual=True)
        self.assertEqual("a path key is resolved or textual, not both", str(raised.exception))

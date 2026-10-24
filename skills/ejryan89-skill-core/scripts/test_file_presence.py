"""Regression tests for the one test of whether a file a reader may find absent is there."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from file_presence import NotAFile, is_present


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

    def test_a_missing_folder_above_the_file_is_absent(self) -> None:
        self.assertFalse(is_present(self.root / "missing" / "settings.json"))

    @unittest.skipIf(sys.platform == "win32", "a symbolic link to a file needs a privilege on Windows")
    def test_a_link_to_nothing_is_refused_not_read_as_absent(self) -> None:
        path = self.root / "settings.json"
        path.symlink_to(self.root / "gone.json")
        with self.assertRaises(NotAFile):
            is_present(path)

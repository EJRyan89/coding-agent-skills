"""Regression tests for the one walk up a folder's parents the skills share."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import path_keys
from folders_up import folders_up

STOP = Path(__file__).resolve().parent / "tree"


def to_root(start: Path) -> list[Path]:
    """`start` and every parent through the file system's root, nearest first."""
    return [start, *start.parents]


class FoldersUpTests(unittest.TestCase):
    def test_a_start_equal_to_the_stop_gives_only_the_start(self) -> None:
        self.assertEqual([STOP], list(folders_up(STOP, STOP)))

    def test_a_start_below_the_stop_gives_each_folder_through_the_stop_nearest_first(self) -> None:
        start = STOP / "src" / "App"
        self.assertEqual([start, STOP / "src", STOP], list(folders_up(start, STOP)))

    def test_a_stop_written_differently_is_the_same_folder(self) -> None:
        start = STOP / "src"
        written = Path(f"{STOP}{os.sep}src{os.sep}..{os.sep}")
        self.assertEqual([start, STOP], list(folders_up(start, written)))

    def test_a_start_differing_from_the_stop_only_in_case_is_it_where_the_file_system_ignores_case(self) -> None:
        # The tree's own file system decides, never the platform's name: NTFS and macOS's default APFS fold case. The
        # tree is not there, so its names take the answer of the folder holding it.
        start = STOP / "Src"
        stop = STOP / "src"
        expected = [start] if path_keys.folds_case(STOP) else to_root(start)
        self.assertEqual(expected, list(folders_up(start, stop)))
        for folds in (True, False):
            with self.subTest(folds=folds), mock.patch.dict(path_keys._FOLDS_CASE, {str(STOP.parent): folds}):
                self.assertEqual([start] if folds else to_root(start), list(folders_up(start, stop)))

    def test_a_start_outside_the_stop_walks_to_the_root(self) -> None:
        start = STOP / "elsewhere"
        self.assertEqual(to_root(start), list(folders_up(start, STOP / "src")))

    def test_without_a_stop_the_walk_ends_at_the_root(self) -> None:
        start = STOP / "src" / "App"
        found = list(folders_up(start))
        self.assertEqual(to_root(start), found)
        self.assertEqual(found[-1], found[-1].parent)

    def test_a_relative_start_ends_at_the_working_directory_it_is_relative_to(self) -> None:
        self.assertEqual([Path("src/App"), Path("src"), Path()], list(folders_up(Path("src/App"))))

    def test_the_walk_reads_nothing_so_folders_that_do_not_exist_are_given(self) -> None:
        start = STOP / "missing" / "deeper"
        self.assertFalse(start.exists())
        self.assertEqual([start, STOP / "missing", STOP], list(folders_up(start, STOP)))

    def test_the_walk_is_lazy_so_a_caller_stops_at_the_first_folder_it_wants(self) -> None:
        walk = folders_up(STOP / "src" / "App", STOP)
        self.assertEqual(STOP / "src" / "App", next(walk))
        self.assertEqual(STOP / "src", next(walk))

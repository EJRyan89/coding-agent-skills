"""Regression tests for the one test of whether a path lies inside a tree."""

from __future__ import annotations

import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import path_containment
from path_containment import posix_within, refusal, within, written_refusal


def link_folder(target: Path, link: Path) -> None:
    """A real link to a folder: a junction on Windows, which needs no privilege, else a symbolic link."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


class WithinTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tree = Path(temporary.name).resolve() / "tree"

    def test_a_path_below_the_tree_is_within_and_the_tree_itself_only_when_not_strict(self) -> None:
        self.assertTrue(within(self.tree / "a" / "b.txt", self.tree))
        self.assertTrue(within(self.tree / "a" / "b.txt", self.tree, strict=True))
        self.assertTrue(within(self.tree, self.tree))
        self.assertFalse(within(self.tree, self.tree, strict=True))
        self.assertFalse(within(self.tree / "a" / "..", self.tree, strict=True))

    def test_a_parent_segment_a_sibling_sharing_the_name_and_the_parent_are_outside(self) -> None:
        self.assertFalse(within(self.tree / ".." / "other", self.tree))
        self.assertFalse(within(self.tree / "a" / ".." / ".." / "tree-other", self.tree))
        self.assertFalse(within(self.tree.parent / "tree-other" / "x", self.tree))
        self.assertFalse(within(self.tree.parent, self.tree))
        self.assertTrue(within(self.tree / "a" / ".." / "b", self.tree, strict=True))

    def test_text_and_path_forms_compare_alike(self) -> None:
        self.assertTrue(within(str(self.tree / "a"), str(self.tree)))
        self.assertTrue(within(os.fspath(self.tree / "a"), self.tree))

    def test_a_relative_path_is_taken_against_the_working_directory(self) -> None:
        with mock.patch.object(Path, "cwd", return_value=self.tree):
            self.assertTrue(within("a/b", self.tree, strict=True))
            self.assertTrue(within(PurePosixPath("a/b"), "a"))
            self.assertFalse(within(PurePosixPath("a/../b"), "a"))
            self.assertFalse(within("../x", self.tree))
        self.assertFalse(within(self.tree / "a", "a"))

    def test_names_compare_as_the_platform_compares_them(self) -> None:
        folds = os.path.normcase("A") == os.path.normcase("a")
        self.assertEqual(folds, within(self.tree / "A", self.tree / "a"))
        self.assertEqual(folds, within(Path(str(self.tree).upper()) / "x", self.tree))

    @unittest.skipUnless(sys.platform == "win32", "only Windows has drives")
    def test_another_drive_is_outside(self) -> None:
        self.assertFalse(within("D:/tree/x", "C:/tree"))
        self.assertTrue(within("c:/Tree/x", "C:/tree"))
        self.assertFalse(within("//host/share/tree/x", "C:/tree"))


class PosixWithinTests(unittest.TestCase):
    def test_a_name_in_the_folder_is_within_and_the_folder_itself_only_when_not_strict(self) -> None:
        self.assertTrue(posix_within("skills/a/b.py", "skills"))
        self.assertTrue(posix_within("skills/a/b.py", "skills/a", strict=True))
        self.assertTrue(posix_within("skills", "skills"))
        self.assertFalse(posix_within("skills", "skills", strict=True))

    def test_a_sibling_sharing_the_name_another_case_and_the_parent_are_outside(self) -> None:
        self.assertFalse(posix_within("skills-extra/a.py", "skills"))
        self.assertFalse(posix_within("Skills/a.py", "skills"))
        self.assertFalse(posix_within("skills", "skills/a"))
        self.assertFalse(posix_within("other/skills/a.py", "skills"))


class WrittenRefusalTests(unittest.TestCase):
    def test_a_plain_relative_entry_is_accepted_in_either_separator(self) -> None:
        for entry in ("a.txt", "docs/a.md", "docs\\a.md", "./docs/a.md", "docs/./a.md"):
            with self.subTest(entry=entry):
                self.assertIsNone(written_refusal(entry))

    def test_each_form_that_leaves_or_names_no_tree_path_is_refused_by_name(self) -> None:
        cases = {
            "": "the entry is empty, so it names no path inside the tree",
            "//?/C:/a.md": "the entry names a device, which is never opened",
            "\\\\.\\pipe\\x": "the entry names a device, which is never opened",
            "\\\\host\\share\\a.md": "the entry names a network share, which is never opened, so no host is contacted",
            "//host/share/a.md": "the entry names a network share, which is never opened, so no host is contacted",
            "/etc/passwd": "the entry is absolute, not a path relative to the tree",
            "\\rooted\\a.md": "the entry is absolute, not a path relative to the tree",
            "C:/a.md": "the entry names a drive, which Windows joins as a new root",
            "c:a.md": "the entry names a drive, which Windows joins as a new root",
            "docs/D:x": "the entry names a drive, which Windows joins as a new root",
            "../a.md": "the entry climbs out of its folder with a `..` segment",
            "docs/../a.md": "the entry climbs out of its folder with a `..` segment",
            "docs\\..\\a.md": "the entry climbs out of its folder with a `..` segment",
            "docs//a.md": "the entry holds an empty segment",
            "docs/": "the entry holds an empty segment",
        }
        for entry, reason in cases.items():
            with self.subTest(entry=entry):
                self.assertEqual(reason, written_refusal(entry))

    def test_a_name_holding_a_colon_past_its_start_is_no_drive(self) -> None:
        self.assertIsNone(written_refusal("docs/ab:c.md"))
        self.assertIsNone(written_refusal("1:a.md"))
        self.assertIsNone(written_refusal("ab:c"))


class RefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.tree = base / "tree"
        (self.tree / "docs").mkdir(parents=True)
        (self.tree / "docs" / "a.md").write_text("inside\n", encoding="utf-8")
        self.outside = base / "outside"
        self.outside.mkdir()
        (self.outside / "a.md").write_text("outside\n", encoding="utf-8")

    def test_a_file_inside_the_tree_and_one_not_yet_written_are_accepted(self) -> None:
        self.assertIsNone(refusal("docs/a.md", self.tree))
        self.assertIsNone(refusal("docs\\a.md", self.tree))
        self.assertIsNone(refusal("docs/new/b.md", self.tree))

    def test_an_entry_below_a_file_is_accepted_since_nothing_lies_there(self) -> None:
        self.assertIsNone(refusal("docs/a.md/b.md", self.tree))

    def test_a_parent_segment_and_an_absolute_entry_are_refused_as_written_before_any_lookup(self) -> None:
        with (
            mock.patch.object(Path, "resolve", side_effect=AssertionError("resolved")),
            mock.patch.object(path_containment, "is_link_or_junction", side_effect=AssertionError("probed")),
        ):
            self.assertEqual(
                "the entry climbs out of its folder with a `..` segment", refusal("../outside/a.md", self.tree)
            )
            self.assertEqual(
                "the entry is absolute, not a path relative to the tree",
                refusal((self.outside / "a.md").as_posix().removeprefix(self.outside.drive), self.tree),
            )

    def test_a_link_that_leads_outside_is_refused_as_resolved(self) -> None:
        link_folder(self.outside, self.tree / "linked")
        self.assertEqual(
            "a symbolic link or junction leads the entry outside the tree", refusal("linked/a.md", self.tree)
        )

    def test_a_link_that_stays_inside_is_refused_by_the_link_probe(self) -> None:
        link_folder(self.tree / "docs", self.tree / "alias")
        self.assertEqual(
            "a symbolic link or junction lies on the entry's path, which is never followed",
            refusal("alias/a.md", self.tree),
        )

    def test_a_link_below_a_plain_folder_is_found(self) -> None:
        link_folder(self.tree / "docs", self.tree / "docs" / "alias")
        self.assertEqual(
            "a symbolic link or junction lies on the entry's path, which is never followed",
            refusal("docs/alias/a.md", self.tree),
        )

    def test_an_entry_that_cannot_be_resolved_is_refused_with_the_reason(self) -> None:
        denied = PermissionError(errno.EACCES, "Permission denied")
        with mock.patch.object(Path, "resolve", side_effect=denied):
            self.assertEqual("the entry cannot be resolved: Permission denied", refusal("docs/a.md", self.tree))
        with mock.patch.object(Path, "resolve", side_effect=RuntimeError("Symlink loop")):
            self.assertEqual("the entry cannot be resolved: Symlink loop", refusal("docs/a.md", self.tree))

    def test_a_link_state_that_cannot_be_read_is_refused_with_the_reason(self) -> None:
        denied = PermissionError(errno.EACCES, "Permission denied")
        with mock.patch.object(path_containment, "is_link_or_junction", side_effect=denied):
            self.assertEqual(
                "the entry's link state cannot be read: Permission denied", refusal("docs/a.md", self.tree)
            )

    def test_the_probe_reads_each_part_from_the_tree_down(self) -> None:
        probed: list[Path] = []

        def recording(path: Path) -> bool:
            probed.append(path)
            return False

        with mock.patch.object(path_containment, "is_link_or_junction", side_effect=recording):
            self.assertIsNone(refusal("docs/a.md", self.tree))
        self.assertEqual([self.tree / "docs", self.tree / "docs" / "a.md"], probed)

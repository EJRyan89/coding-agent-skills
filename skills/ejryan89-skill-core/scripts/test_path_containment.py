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
import path_keys
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

    def test_names_compare_as_the_tree_s_file_system_compares_them(self) -> None:
        # The temporary directory folds case on NTFS and APFS by default and keeps it on Linux: within follows the file
        # system, never the platform's name.
        self.tree.mkdir()
        folds = path_keys.folds_case(self.tree.parent)
        (self.tree.parent / "Probe").mkdir()
        self.assertEqual(folds, (self.tree.parent / "pROBE").is_dir())
        self.assertEqual(folds, within(self.tree / "A", self.tree / "a"))
        self.assertEqual(folds, within(self.tree.parent / "TREE" / "x", self.tree))

    def test_a_spelling_the_tree_s_file_system_folds_is_inside_it_whatever_the_platform(self) -> None:
        for folds in (True, False):
            with (
                self.subTest(folds=folds),
                mock.patch.dict(path_keys._FOLDS_CASE, {str(self.tree.parent): folds}),
            ):
                self.assertEqual(folds, within(self.tree.parent / "TREE" / "skills" / "x", self.tree))
                self.assertEqual(folds, within(self.tree.parent / "Tree", self.tree, strict=False))
                self.assertFalse(within(self.tree.parent / "Tree", self.tree, strict=True))
                self.assertFalse(within(self.tree.parent / "TREE-other" / "x", self.tree))

    def test_a_normalization_form_is_inside_only_where_the_tree_s_directory_finds_one_entry(self) -> None:
        composed, decomposed = "caf\u00e9", "cafe\u0301"
        tree = self.tree.parent / composed
        tree.mkdir()
        try:
            one = os.path.samestat(os.lstat(self.tree.parent / decomposed), os.lstat(tree))
        except FileNotFoundError:
            one = False
        self.assertEqual(one, within(self.tree.parent / decomposed / "x", tree))

    def test_a_sibling_spelled_with_a_sign_lowercase_merges_is_outside_where_the_file_system_keeps_it_apart(
        self,
    ) -> None:
        # str.lower takes the Kelvin, Angstrom, and Ohm signs to k, the small a with ring, and the small omega, but
        # NTFS and Linux's file systems keep each apart from those letters, so a folder spelled with the sign is
        # another folder, outside the tree, wherever the tree's own directory does not find one entry under both.
        for sign, letter in (("\u212a", "K"), ("\u212b", "\u00c5"), ("\u2126", "\u03a9")):
            tree = self.tree.parent / f"{letter}x"
            tree.mkdir()
            sibling = self.tree.parent / f"{sign}x"
            try:
                one = os.path.samestat(os.lstat(sibling), os.lstat(tree))
            except FileNotFoundError:
                one = False
            with self.subTest(sign=sign):
                self.assertEqual(one, within(sibling / "evil", tree))
                if sys.platform in ("win32", "linux"):
                    self.assertFalse(within(sibling / "evil", tree))

    def test_a_tree_on_a_network_share_holds_only_its_own_spelling(self) -> None:
        # Nothing answers for a share, which may keep case, so another case below its anchor is outside.
        self.assertFalse(within("//srv/share/TREE/x", "//srv/share/tree"))
        self.assertTrue(within("//srv/share/tree/x", "//srv/share/tree"))

    def test_the_path_judged_is_never_asked(self) -> None:
        judged = self.tree.parent / "judged"
        (judged / "deep").mkdir(parents=True)
        asked: list[str] = []
        real = path_keys._asked_folds_case

        def answer(directory: str) -> bool | None:
            asked.append(directory)
            return real(directory)

        with mock.patch.object(path_keys, "_asked_folds_case", side_effect=answer):
            self.assertFalse(within(judged / "deep" / "x", self.tree))
        self.assertTrue(asked)
        self.assertFalse([directory for directory in asked if directory.startswith(str(judged))])

    @unittest.skipUnless(sys.platform == "win32", "only Windows has drives")
    def test_another_drive_is_outside(self) -> None:
        self.assertFalse(within("D:/tree/x", "C:/tree"))
        self.assertTrue(within("c:/Tree/x", "C:/tree"))
        self.assertFalse(within("//host/share/tree/x", "C:/tree"))


class ExtendedPrefixTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "only Windows spells a path with an extended-length prefix")
    def test_within_compares_a_path_and_a_tree_without_the_extended_length_prefix(self) -> None:
        # Path.resolve gives a long path, or one whose last segment Windows rewrites, with the prefix and a tree
        # without it, so either spelling on either side compares as the plain path.
        for path, tree in (
            ("\\\\?\\C:\\tree\\a", "C:\\tree"),
            ("C:\\tree\\a", "\\\\?\\C:\\tree"),
            ("\\\\?\\c:\\TREE\\a", "\\\\?\\C:\\tree"),
            ("\\\\?\\UNC\\host\\share\\a", "\\\\host\\share"),
            ("\\\\.\\C:\\tree\\a", "C:\\tree"),
            ("C:\\tree\\a", "\\\\.\\c:\\tree"),
            ("\\\\.\\UNC\\host\\share\\a", "\\\\?\\UNC\\host\\share"),
        ):
            with self.subTest(path=path, tree=tree):
                self.assertTrue(within(path, tree))
                self.assertFalse(within(path, tree.replace("tree", "other").replace("share", "other")))

    @unittest.skipUnless(sys.platform == "win32", "only Windows spells a path with an extended-length prefix")
    def test_either_prefix_is_removed_before_a_drive_letter_or_a_share_and_kept_before_anything_else(self) -> None:
        volume = "\\\\?\\Volume{00000000-0000-0000-0000-000000000000}\\skills"
        cases = {
            "\\\\?\\C:\\skills\\x": "C:\\skills\\x",
            "\\\\?\\c:\\skills": "c:\\skills",
            "\\\\?\\UNC\\host\\share\\skills\\x": "\\\\host\\share\\skills\\x",
            "\\\\?\\unc\\host\\share\\skills": "\\\\host\\share\\skills",
            "C:\\skills\\x": "C:\\skills\\x",
            "\\\\host\\share\\skills": "\\\\host\\share\\skills",
            volume: volume,
            "\\\\.\\C:\\skills\\x": "C:\\skills\\x",
            "\\\\.\\UNC\\host\\share\\skills": "\\\\host\\share\\skills",
            "\\\\.\\pipe\\x": "\\\\.\\pipe\\x",
        }
        for spelled, plain in cases.items():
            with self.subTest(path=spelled):
                self.assertEqual(Path(plain), path_containment.without_extended_prefix(Path(spelled)))

    @unittest.skipIf(sys.platform == "win32", "Windows reads the prefix as part of a drive")
    def test_elsewhere_the_prefix_characters_are_an_ordinary_name(self) -> None:
        for spelled in ("\\\\?\\C:\\skills", "/home/a/\\\\?\\C:\\skills"):
            with self.subTest(path=spelled):
                self.assertEqual(Path(spelled), path_containment.without_extended_prefix(Path(spelled)))


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
        trailing = "the entry has a segment ending in a dot or a space, which Windows drops, so it names another file"
        for entry in ("a.", "a ", "docs./a.md", "docs /a.md", "...", ".. ", ". ", ".. /Roaming", "docs\\a. "):
            cases[entry] = trailing
        for entry, reason in cases.items():
            with self.subTest(entry=entry):
                self.assertEqual(reason, written_refusal(entry))

    def test_a_dot_or_a_space_inside_a_name_and_a_lone_dot_segment_are_accepted(self) -> None:
        for entry in (".", "./a", "a/./b", ".a", "a.b", "a b", "..a", "a/.github/b"):
            with self.subTest(entry=entry):
                self.assertIsNone(written_refusal(entry))

    def test_a_reserved_device_name_in_any_segment_is_refused_on_every_platform(self) -> None:
        for entry in (
            "NUL",
            "sub/NUL",
            "CON",
            "docs\\con.txt",
            "aux .md",
            "a/Com1.tar.gz/b.md",
            "LPT¹",
            "conin$",
            "NUL:",
            "sub/NUL:",
            "CON:",
            "COM1:",
            "NUL:stream",
            "docs\\con.txt:stream",
        ):
            with self.subTest(entry=entry):
                self.assertEqual(
                    "the entry names a reserved device as a segment, which Windows opens instead of a file",
                    written_refusal(entry),
                )

    def test_a_name_that_only_starts_like_a_device_is_accepted(self) -> None:
        for entry in ("console.md", "nul-check/a.md", "com10.txt", "docs/auxiliary"):
            with self.subTest(entry=entry):
                self.assertIsNone(written_refusal(entry))

    def test_a_nul_character_is_refused_wherever_it_sits(self) -> None:
        for entry in ("a\0b", "\0", "docs/a.md\0"):
            with self.subTest(entry=entry):
                self.assertEqual("the entry holds a NUL character, which no file name holds", written_refusal(entry))

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

    def test_a_segment_windows_drops_the_end_of_is_refused_for_that_never_for_a_link(self) -> None:
        # Windows reads `.. /docs` as `docs` and `.../a.md` as `a.md`, a file inside the tree, so the refusal names the
        # spelling, not a link that does not exist.
        trailing = "the entry has a segment ending in a dot or a space, which Windows drops, so it names another file"
        for entry in (".. /docs", ".../a.md", "docs/a.md."):
            with self.subTest(entry=entry):
                self.assertEqual(trailing, refusal(entry, self.tree))

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

    def test_a_device_name_and_a_nul_character_get_a_reason_never_an_exception(self) -> None:
        device = "the entry names a reserved device as a segment, which Windows opens instead of a file"
        cases = {
            "NUL": device,
            "sub/NUL": device,
            "CON": device,
            "a\0b": "the entry holds a NUL character, which no file name holds",
        }
        for entry, reason in cases.items():
            with self.subTest(entry=entry):
                self.assertEqual(reason, refusal(entry, self.tree))

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

    def test_a_link_is_found_whichever_separator_the_entry_is_written_with(self) -> None:
        link_folder(self.tree / "docs", self.tree / "alias")
        for entry in ("alias/a.md", "alias\\a.md"):
            with self.subTest(entry=entry):
                self.assertEqual(
                    "a symbolic link or junction lies on the entry's path, which is never followed",
                    refusal(entry, self.tree),
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


class RelativeWithinTests(unittest.TestCase):
    def test_a_path_in_its_tree_is_given_relative_to_it_and_one_outside_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary) / "tree"
            self.assertEqual(Path("a", "b.py"), path_containment.relative_within(tree / "a" / "b.py", tree))
            self.assertEqual(Path("a"), path_containment.relative_within(tree / "a" / ".." / "a", tree))
            self.assertEqual(Path(), path_containment.relative_within(tree, tree))
            self.assertIsNone(path_containment.relative_within(tree, tree, strict=True))
            for outside in (Path(temporary), Path(temporary) / "tree2" / "a", tree / ".." / "other"):
                with self.subTest(outside=outside):
                    self.assertIsNone(path_containment.relative_within(outside, tree))

    @unittest.skipUnless(sys.platform == "win32", "only Windows spells a path with an extended-length prefix")
    def test_a_prefix_on_one_side_alone_still_gives_the_relative_path(self) -> None:
        # Path.resolve gives a path too long for MAX_PATH with the prefix and a short tree without it.
        for path, tree in (
            ("\\\\?\\C:\\tree\\tools\\calc.py", "C:\\tree"),
            ("C:\\tree\\tools\\calc.py", "\\\\?\\C:\\tree"),
            ("\\\\.\\c:\\TREE\\tools\\calc.py", "C:\\tree"),
        ):
            with self.subTest(path=path, tree=tree):
                self.assertEqual(Path("tools", "calc.py"), path_containment.relative_within(path, tree))

    @unittest.skipUnless(sys.platform == "win32", "only Windows names a drive, whose root NTFS folds")
    def test_a_case_only_difference_is_judged_as_within_judges_it_and_keeps_the_path_s_spelling(self) -> None:
        self.assertEqual(Path("Tools", "X.py"), path_containment.relative_within("C:\\TREE\\Tools\\X.py", "c:\\tree"))

    def test_which_parts_lie_below_the_tree_comes_from_the_compared_form_never_a_relative_to(self) -> None:
        # A case rule other than the one within compared by can never make the answer raise or disagree. within is
        # stubbed, since Python 3.11's is_relative_to calls relative_to itself.
        with (
            mock.patch.object(path_containment, "within", return_value=True),
            mock.patch.object(Path, "relative_to", side_effect=AssertionError("relative_to is never called")),
        ):
            self.assertEqual(Path("a", "b.py"), path_containment.relative_within("tree/a/b.py", "tree"))

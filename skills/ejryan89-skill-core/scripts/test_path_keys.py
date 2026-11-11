"""Regression tests for the one form a path or a name is compared in."""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path, PurePath
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import path_keys
from path_keys import folds_case, name_key, path_key, same_parts


def link_folder(target: Path, link: Path) -> None:
    """A real link to a folder: a junction on Windows, which needs no privilege, else a symbolic link."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def measured_folds(directory: Path) -> bool:
    """Whether `directory`'s file system folds case, asked apart from the probe: it refuses to create a second name
    differing only in case exactly when it folds case."""
    (directory / "Guide.md").write_bytes(b"one")
    try:
        with (directory / "gUIDE.MD").open("xb"):
            pass
    except FileExistsError:
        return True
    finally:
        for name in ("Guide.md", "gUIDE.MD"):
            with contextlib.suppress(FileNotFoundError):
                (directory / name).unlink()
    return False


class KeyedTestCase(unittest.TestCase):
    """A temporary folder, resolved so no link stands on its path, and an empty cache of answers for each test."""

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="path-key-test."))).resolve()
        self.enterContext(mock.patch.dict(path_keys._FOLDS_CASE, clear=True))

    @contextlib.contextmanager
    def answers(self, answers: dict[Path, bool]) -> Iterator[list[str]]:
        """The file system's answer for each directory in `answers`, and the real one for any other; yields the
        directories asked, in order."""
        real = path_keys._asked_folds_case
        asked: list[str] = []

        def answer(directory: str) -> bool | None:
            asked.append(directory)
            known = {str(path): folds for path, folds in answers.items()}
            return known[directory] if directory in known else real(directory)

        with mock.patch.object(path_keys, "_asked_folds_case", side_effect=answer):
            yield asked


class PathKeyTests(KeyedTestCase):
    def test_two_spellings_of_one_path_have_one_key(self) -> None:
        folder = self.root / "a folder"
        self.assertEqual(path_key(folder), path_key(self.root / "other" / ".." / "a folder"))
        self.assertEqual(path_key(folder), path_key(str(folder) + os.sep))
        self.assertNotEqual(path_key(folder), path_key(self.root / "another"))

    def test_a_relative_path_is_keyed_against_the_working_directory(self) -> None:
        self.assertEqual(path_key(Path.cwd() / "x"), path_key("x"))
        self.assertEqual(path_key(Path.cwd().parent / "x"), path_key(Path("..") / "x"))

    def test_case_folds_where_the_file_system_folds_it(self) -> None:
        # The temporary directory is on NTFS on Windows, which folds, on a case-sensitive file system on Linux, and on
        # APFS on macOS, which folds by default but may be formatted case-sensitive: the key follows the file system.
        folds = measured_folds(self.root)
        (self.root / "Work").mkdir()
        for resolve, textual in ((False, False), (True, False), (False, True)):
            with self.subTest(resolve=resolve, textual=textual):
                upper = path_key(self.root / "Work" / "Name", resolve=resolve, textual=textual)
                lower = path_key(self.root / "work" / "name", resolve=resolve, textual=textual)
                self.assertEqual(folds, upper == lower)

    def test_a_case_sensitive_directory_keeps_two_spellings_apart_whatever_the_platform(self) -> None:
        with self.answers({self.root: False}):
            self.assertNotEqual(path_key(self.root / "Work"), path_key(self.root / "work"))
            self.assertNotEqual(path_key(self.root / "Work", textual=True), path_key(self.root / "work", textual=True))
            self.assertTrue(path_key(self.root / "Work").endswith("Work"))

    def test_a_folding_directory_makes_two_spellings_one_whatever_the_platform(self) -> None:
        with self.answers({self.root: True}):
            self.assertEqual(path_key(self.root / "Work"), path_key(self.root / "wORK"))
            self.assertEqual(path_key(self.root / "Work" / "A"), path_key(self.root / "work" / "a"))
            self.assertEqual(path_key(self.root / "Work", textual=True), path_key(self.root / "wORK", textual=True))
            self.assertTrue(path_key(self.root / "Work").endswith("work"))

    def test_each_name_folds_as_the_directory_holding_it_does(self) -> None:
        # A case-sensitive folder inside one that folds, as a case-sensitive volume mounted under a folding one.
        sensitive = self.root / "cs"
        sensitive.mkdir()
        with self.answers({self.root: True, sensitive: False}):
            self.assertEqual(path_key(self.root / "CS"), path_key(sensitive))
            self.assertNotEqual(path_key(sensitive / "Name"), path_key(sensitive / "name"))
            self.assertEqual(path_key(sensitive / "Name"), path_key(self.root / "cs" / "Name"))
            self.assertTrue(path_key(sensitive / "Name").endswith(str(Path("cs", "Name"))))

    def test_a_name_below_the_last_directory_asked_takes_its_answer(self) -> None:
        missing = self.root / "missing" / "deeper"
        for folds in (True, False):
            with self.subTest(folds=folds), mock.patch.dict(path_keys._FOLDS_CASE, clear=True):
                with self.answers({self.root: folds}) as asked:
                    self.assertEqual(folds, path_key(missing / "Name") == path_key(missing / "NAME"))
                self.assertNotIn(str(missing), asked)

    def test_a_link_is_never_followed_to_ask_and_the_last_name_is_never_asked(self) -> None:
        target = self.root / "target"
        (target / "inner").mkdir(parents=True)
        link = self.root / "link"
        link_folder(target, link)
        with self.answers({self.root: False}) as asked:
            key = path_key(link / "inner" / "Name")
            last = path_key(target / "inner")
        self.assertTrue(key.endswith(str(Path("link", "inner", "Name"))))
        self.assertTrue(last.endswith(str(Path("target", "inner"))))
        self.assertIn(str(self.root), asked)
        self.assertIn(str(target), asked)
        for directory in (link, link / "inner", target / "inner"):
            self.assertNotIn(str(directory), asked)

    def test_the_lexical_key_follows_no_link_and_the_resolved_key_does(self) -> None:
        target = self.root / "target"
        target.mkdir()
        link = self.root / "link"
        link_folder(target, link)
        self.assertNotEqual(path_key(link), path_key(target))
        self.assertEqual(path_key(link, resolve=True), path_key(target, resolve=True))
        self.assertEqual(path_key(self.root / "missing" / ".."), path_key(self.root, resolve=True))

    def test_a_network_or_device_path_is_never_asked_and_folds(self) -> None:
        share = f"{os.sep}{os.sep}Host{os.sep}Share{os.sep}Dir{os.sep}Name"
        with mock.patch.object(path_keys, "_asked_folds_case", side_effect=AssertionError("never asked")):
            self.assertEqual(path_key(share), path_key(share.lower()))
            self.assertEqual(path_key(share, textual=True), path_key(share.lower(), textual=True))
            self.assertTrue(folds_case(share))

    def test_composed_and_decomposed_spellings_have_one_key_on_every_file_system(self) -> None:
        composed, decomposed = "caf\u00e9", "cafe\u0301"
        for folds in (True, False):
            with (
                self.subTest(folds=folds),
                mock.patch.dict(path_keys._FOLDS_CASE, clear=True),
                self.answers({self.root: folds}),
            ):
                self.assertEqual(path_key(self.root / composed), path_key(self.root / decomposed))
                self.assertTrue(path_key(self.root / decomposed).endswith(composed))
        with self.answers({self.root: True}):
            # Folding decomposes first, so a capital written either way folds to one composed small letter.
            self.assertEqual(path_key(self.root / "CAF\u00c9"), path_key(self.root / "CAFE\u0301"))
            self.assertTrue(path_key(self.root / "CAFE\u0301").endswith(composed))

    def test_the_textual_key_folds_dots_and_case_and_keeps_what_windows_would_strip(self) -> None:
        folder = self.root / "a folder"
        self.assertEqual(path_key(folder), path_key(self.root / "other" / ".." / "a folder", textual=True))
        self.assertEqual(path_key(Path.cwd() / "x"), path_key("x", textual=True))
        self.assertEqual(path_key(Path.cwd().parent / "x"), path_key(Path("..") / "x", textual=True))
        trailing = str(self.root / "name. ")
        self.assertTrue(path_key(trailing, textual=True).endswith(f"{os.sep}name. "))
        # Windows' own parsing drops a name's trailing dots and spaces; the lexical key follows it, the textual not.
        self.assertEqual(sys.platform == "win32", path_key(trailing) != path_key(trailing, textual=True))

    def test_a_key_is_never_both_resolved_and_textual(self) -> None:
        with self.assertRaises(ValueError) as raised:
            path_key(self.root, resolve=True, textual=True)
        self.assertEqual("a path key is resolved or textual, not both", str(raised.exception))


class SamePartsTests(KeyedTestCase):
    def test_parts_compare_as_the_reference_s_directories_compare_names(self) -> None:
        reference = PurePath(self.root / "Run" / "prompt.md").parts
        other = PurePath(self.root / "run" / "PROMPT.md").parts
        for folds in (True, False):
            with (
                self.subTest(folds=folds),
                mock.patch.dict(path_keys._FOLDS_CASE, clear=True),
                self.answers({self.root: folds}),
            ):
                self.assertEqual(folds, same_parts(other, reference))
                self.assertTrue(same_parts(reference, reference))
        # A case-sensitive Run inside a folding folder: Run's own name folds, the names in it do not.
        (self.root / "Run").mkdir()
        with self.answers({self.root: True, self.root / "Run": False}):
            self.assertTrue(same_parts(PurePath(self.root / "RUN" / "prompt.md").parts, reference))
            self.assertFalse(same_parts(other, reference))

    def test_only_the_reference_is_asked(self) -> None:
        judged = self.root / "judged"
        (judged / "deep").mkdir(parents=True)
        reference = self.root / "known" / "x"
        with self.answers({}) as asked:
            self.assertFalse(same_parts(PurePath(judged / "deep").parts, PurePath(reference).parts))
        self.assertFalse([directory for directory in asked if directory.startswith(str(judged))])

    def test_no_normalization_and_no_relative_reference(self) -> None:
        with self.answers({}) as asked:
            self.assertFalse(same_parts(("a", "b"), ("a", "b")))
            self.assertEqual([], asked)
        dotted = PurePath(self.root / "a" / ".." / "b").parts
        self.assertFalse(same_parts(dotted, PurePath(self.root / "b").parts))
        self.assertFalse(same_parts(PurePath(self.root).parts, PurePath(self.root / "b").parts))


class FoldsCaseTests(KeyedTestCase):
    def swapped_case(self, same: bool) -> Any:
        """os.lstat as a file system that does, or does not, fold case answers a case-swapped name."""
        real = os.lstat

        def lstat(path: Any) -> os.stat_result:
            if Path(path).name == "probe.TXT":
                if not same:
                    raise FileNotFoundError(2, "No such file or directory", str(path))
                return real(Path(path).with_name("PROBE.txt"))
            return real(path)

        return mock.patch.object(os, "lstat", side_effect=lstat)

    def test_without_a_directory_names_compare_without_regard_to_case(self) -> None:
        self.assertEqual(name_key("Guide.md"), name_key("guide.md"))
        self.assertEqual(name_key("ALPHA"), name_key("alpha"))
        self.assertEqual("guide.md", name_key("GUIDE.MD"))
        self.assertNotEqual(name_key("guide.md"), name_key("guide-md"))

    def test_a_directory_that_folds_case_folds_its_names(self) -> None:
        (self.root / "PROBE.txt").write_bytes(b"")
        with self.swapped_case(same=True):
            self.assertTrue(folds_case(self.root))
        self.assertEqual("guide.md", name_key("Guide.MD", self.root))

    def test_a_case_sensitive_directory_folds_nothing(self) -> None:
        (self.root / "PROBE.txt").write_bytes(b"")
        with self.swapped_case(same=False):
            self.assertFalse(folds_case(self.root))
        self.assertEqual("Guide.MD", name_key("Guide.MD", self.root))
        self.assertNotEqual(name_key("Guide.md", self.root), name_key("guide.md", self.root))

    def test_a_directory_with_nothing_to_ask_takes_the_answer_above_and_is_asked_again(self) -> None:
        (self.root / "1234").write_bytes(b"")  # no letter whose case could be swapped
        for folds in (True, False):
            with self.subTest(folds=folds), mock.patch.dict(path_keys._FOLDS_CASE, {str(self.root.parent): folds}):
                self.assertEqual(folds, folds_case(self.root))
                self.assertEqual(folds, folds_case(self.root / "missing"))
                self.assertNotIn(str(self.root), path_keys._FOLDS_CASE)
        (self.root / "PROBE.txt").write_bytes(b"")
        with self.swapped_case(same=False):
            self.assertFalse(folds_case(self.root))

    def test_where_nothing_can_be_asked_case_folds(self) -> None:
        with mock.patch.object(os, "scandir", side_effect=PermissionError(13, "Permission denied")):
            self.assertTrue(folds_case(self.root))
            self.assertTrue(path_key(self.root / "Name") == path_key(self.root / "nAME"))
        self.assertEqual({}, path_keys._FOLDS_CASE)

    def test_this_file_system_gives_its_own_answer(self) -> None:
        folds = measured_folds(self.root)
        (self.root / "Guide.md").write_bytes(b"one")
        self.assertEqual(folds, folds_case(self.root))
        if sys.platform in ("win32", "linux"):
            self.assertEqual(sys.platform == "win32", folds)

"""Regression tests for the one form a path or a name is compared in."""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path, PurePath
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import path_keys
from path_containment import within
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


def case_sensitive_folder(folder: Path) -> Path:
    """`folder`, made to keep two names differing only in case apart: marked case-sensitive on Windows, which needs
    no privilege, and as made on Linux, whose file systems keep case; it fails rather than hand back a folder that
    folds case. macOS's default APFS volume folds case, and only a separate case-sensitive volume would not."""
    folder.mkdir()
    if sys.platform == "win32":
        subprocess.run(
            ["fsutil", "file", "setCaseSensitiveInfo", str(folder), "enable"], check=True, capture_output=True
        )
    if measured_folds(folder):
        raise AssertionError(f"{folder} folds case")
    return folder


def hard_link_pair(folder: Path, name: str, other: str) -> None:
    """An empty file `name` in `folder` and a second hard link to it named `other`."""
    (folder / name).write_bytes(b"")
    os.link(folder / name, folder / other)


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

    def test_a_folding_directory_lowercases_one_letter_to_one_and_keeps_sharp_s_apart_from_ss(self) -> None:
        # NTFS and .NET's ordinal comparison never expand a letter, so Strasse and Stra\u00dfe are two folders there.
        with self.answers({self.root: True}):
            self.assertNotEqual(path_key(self.root / "Stra\u00dfe"), path_key(self.root / "Strasse"))
            self.assertEqual(path_key(self.root / "STRA\u00dfE"), path_key(self.root / "stra\u00dfe"))
            self.assertFalse(
                same_parts(PurePath(self.root / "Strasse").parts, PurePath(self.root / "Stra\u00dfe").parts)
            )
        # A name compared with no directory folds fully, the answer that refuses more names as one.
        self.assertEqual(name_key("Stra\u00dfe"), name_key("STRASSE"))

    def test_a_folding_directory_keeps_a_letter_apart_whose_case_mapping_does_not_lead_back_to_it(self) -> None:
        # NTFS's upcase table leaves the Greek theta symbol and the capital sharp s as they are, while str.lower takes
        # them to the small theta and to sharp s, the letters NTFS keeps apart from them. A letter beyond ASCII never
        # folds onto an ASCII one, as the dotless i's and the long s's uppercase would take them to I and S.
        pairs = (("\u03f4", "\u03b8"), ("\u03f4", "\u0398"), ("\u1e9e", "\u00df"), ("\u0131", "i"), ("\u017f", "s"))
        for sign, letter in pairs:
            with self.subTest(sign=sign, letter=letter):
                self.assertNotEqual(path_keys.fold(sign, True), path_keys.fold(letter, True))
                self.assertEqual(sign, path_keys.fold(sign, True))
        # A letter whose case maps one to one folds, outside ASCII too.
        for upper, lower in (("\u00c4", "\u00e4"), ("\u0398", "\u03b8"), ("\u0416", "\u0436")):
            with self.subTest(upper=upper):
                self.assertEqual(path_keys.fold(lower, True), path_keys.fold(upper, True))
                self.assertEqual(lower, path_keys.fold(upper, True))
        self.assertEqual("\u00df", path_keys.fold("\u00df", True))

    def test_a_sign_canonically_equivalent_to_a_letter_has_its_key_while_same_parts_keeps_them_apart(self) -> None:
        # The Kelvin, Angstrom, and Ohm signs decompose to K, the capital a with ring, and the capital omega, so a key,
        # which composes every name, merges each with its letter in any directory: the side that refuses more names as
        # one. NTFS keeps them apart, so same_parts, which admits, takes them as one only where the directory finds one
        # entry under both.
        for sign, letter in (("\u212a", "K"), ("\u212b", "\u00c5"), ("\u2126", "\u03a9")):
            with self.subTest(sign=sign):
                for folds in (True, False):
                    self.assertEqual(path_keys.fold(letter, folds), path_keys.fold(sign, folds))
                self.assertEqual(letter.lower(), path_keys.fold(sign, True))
                self.assertEqual(letter, path_keys.fold(sign, False))
                for folds in (True, False):
                    with mock.patch.dict(path_keys._FOLDS_CASE, clear=True), self.answers({self.root: folds}):
                        self.assertEqual(name_key(letter, self.root), name_key(sign, self.root))
                self.assertEqual(name_key(letter), name_key(sign))
                folder = self.root / f"{letter}-sign"
                folder.mkdir()
                with mock.patch.object(os, "lstat", side_effect=self.missing(f"{sign}-sign")):
                    self.assertFalse(same_parts(PurePath(self.root / f"{sign}-sign").parts, PurePath(folder).parts))
                    self.assertFalse(within(self.root / f"{sign}-sign" / "x", folder))

    @staticmethod
    def missing(name: str) -> Any:
        """os.lstat as a file system that keeps `name` apart from every other spelling answers it."""
        real = os.lstat

        def lstat(path: Any) -> os.stat_result:
            if Path(path).name == name:
                raise FileNotFoundError(2, "No such file or directory", str(path))
            return real(path)

        return lstat

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

    def test_normalization_forms_are_one_only_where_the_directory_finds_one_entry(self) -> None:
        composed, decomposed = "caf\u00e9", "cafe\u0301"
        (self.root / composed).mkdir()
        reference = PurePath(self.root / composed / "x").parts
        other = PurePath(self.root / decomposed / "x").parts
        try:
            one = os.path.samestat(os.lstat(self.root / decomposed), os.lstat(self.root / composed))
        except FileNotFoundError:
            one = False
        # APFS finds one entry under both forms; NTFS and Linux's file systems keep them apart.
        self.assertEqual(one, same_parts(other, reference))
        real = os.lstat

        def unifying(path: Any) -> os.stat_result:
            given = Path(path)
            return real(given.with_name(composed) if given.name == decomposed else given)

        def apart(path: Any) -> os.stat_result:
            if Path(path).name == decomposed:
                raise FileNotFoundError(2, "No such file or directory", str(path))
            return real(path)

        for lstat, expected in ((unifying, True), (apart, False)):
            with self.subTest(expected=expected), mock.patch.object(os, "lstat", side_effect=lstat):
                self.assertEqual(expected, same_parts(other, reference))
                self.assertTrue(same_parts(reference, reference))
        # A reference whose entry is missing cannot be asked, so the forms stay apart, while a key merges them.
        missing = PurePath(self.root / "na\u00efve" / "x").parts
        self.assertFalse(same_parts(PurePath(self.root / "nai\u0308ve" / "x").parts, missing))
        self.assertEqual(path_key(self.root / composed), path_key(self.root / decomposed))

    def test_a_part_in_another_case_and_form_is_one_where_the_directory_folds_and_finds_one_entry(self) -> None:
        # Both parts are folded before the two forms are compared, so a capital written in either form, on either side,
        # is the other side's small letter in a folding directory that finds one entry under both.
        real = os.lstat
        for index, (known, given) in enumerate((("caf\u00e9", "CAFE\u0301"), ("CAF\u00c9", "cafe\u0301"))):
            folder = self.root / f"pair-{index}"
            (folder / known).mkdir(parents=True)
            reference = PurePath(folder / known / "x").parts
            other = PurePath(folder / given / "x").parts

            def unifying(path: Any, known: str = known, given: str = given) -> os.stat_result:
                spelled = Path(path)
                return real(spelled.with_name(known) if spelled.name == given else spelled)

            for folds in (True, False):
                with (
                    self.subTest(known=known, folds=folds),
                    mock.patch.dict(path_keys._FOLDS_CASE, clear=True),
                    self.answers({folder: folds}),
                    mock.patch.object(os, "lstat", side_effect=unifying),
                ):
                    self.assertEqual(folds, same_parts(other, reference))

    def test_a_sign_folding_to_a_letter_outside_ascii_rules_is_one_only_where_the_directory_finds_one_entry(
        self,
    ) -> None:
        # str.lower takes the Kelvin sign to k, the Angstrom sign to the small a with ring, and the Ohm sign to the
        # small omega, but NTFS keeps each apart from those letters: a folding directory's ASCII rule says nothing of
        # them, so the directory is asked, and a sibling spelled with the sign is never taken for the trusted folder.
        # Each of those three signs is canonically equivalent to its capital, so a directory that finds one entry
        # under both, as APFS may, makes them one; the capital sharp s and the theta symbol are equivalent to nothing,
        # so they stay apart from the letter whatever a lookup finds.
        real = os.lstat
        signs = (("\u212a", "K"), ("\u212a", "k"), ("\u212b", "\u00c5"), ("\u212b", "\u00e5"), ("\u2126", "\u03a9"))
        pairs = [(sign, letter, True) for sign, letter in (*signs, ("\u2126", "\u03c9"))]
        pairs += [("\u1e9e", "\u00df", False), ("\u03f4", "\u03b8", False)]
        for index, (sign, letter, equivalent) in enumerate(pairs):
            folder = self.root / f"signs-{index}"
            (folder / f"{letter}x").mkdir(parents=True)
            (folder / f"{sign}y").mkdir()
            for given, known in ((f"{sign}x", f"{letter}x"), (f"{letter}y", f"{sign}y")):
                reference = PurePath(folder / known / "result.json").parts
                other = PurePath(folder / given / "result.json").parts

                def apart(path: Any, given: str = given) -> os.stat_result:
                    if Path(path).name == given:
                        raise FileNotFoundError(2, "No such file or directory", str(path))
                    return real(path)

                def unifying(path: Any, given: str = given, known: str = known) -> os.stat_result:
                    spelled = Path(path)
                    return real(spelled.with_name(known) if spelled.name == given else spelled)

                for lstat, expected in ((apart, False), (unifying, equivalent)):
                    with (
                        self.subTest(given=given, known=known, expected=expected),
                        mock.patch.dict(path_keys._FOLDS_CASE, clear=True),
                        self.answers({folder: True}),
                        mock.patch.object(os, "lstat", side_effect=lstat),
                    ):
                        self.assertEqual(expected, same_parts(other, reference))
                try:
                    one = os.path.samestat(os.lstat(folder / given), os.lstat(folder / known))
                except FileNotFoundError:
                    one = False
                # This file system's own answer: NTFS and Linux's keep the two apart.
                with self.subTest(given=given, known=known, real=True), mock.patch.dict(path_keys._FOLDS_CASE):
                    self.assertEqual(one and equivalent, same_parts(other, reference))
                    if sys.platform in ("win32", "linux"):
                        self.assertFalse(one)

    def test_a_letter_in_another_case_outside_ascii_is_one_where_the_directory_finds_one_entry(self) -> None:
        (self.root / "\u00c4rger").mkdir()
        reference = PurePath(self.root / "\u00c4rger" / "x").parts
        other = PurePath(self.root / "\u00e4RGER" / "x").parts
        try:
            one = os.path.samestat(os.lstat(self.root / "\u00e4rger"), os.lstat(self.root / "\u00c4rger"))
        except FileNotFoundError:
            one = False
        self.assertEqual(one, same_parts(other, reference))
        if sys.platform == "win32":
            self.assertTrue(one)
        # A directory that keeps case keeps them apart, however a lookup answers.
        with self.answers({self.root: False}), mock.patch.dict(path_keys._FOLDS_CASE, clear=True):
            self.assertFalse(same_parts(other, reference))

    def test_names_under_a_network_share_compare_as_written_and_are_never_asked(self) -> None:
        # Nothing answers for a share, which may keep case, so a containment check that folded there would admit a
        # spelling the share takes as another folder; a key still folds, the answer that refuses more.
        share = PurePath(f"{os.sep}{os.sep}host{os.sep}share{os.sep}tree{os.sep}result.json")
        other = PurePath(f"{os.sep}{os.sep}host{os.sep}share{os.sep}TREE{os.sep}result.json")
        with (
            mock.patch.object(path_keys, "_asked_folds_case", side_effect=AssertionError("never asked")),
            mock.patch.object(os, "lstat", side_effect=AssertionError("never asked")),
        ):
            self.assertFalse(same_parts(other.parts, share.parts))
            self.assertTrue(same_parts(share.parts, share.parts))
            self.assertEqual(path_key(share), path_key(other))
            if sys.platform == "win32":
                # A share's own name is in the anchor, which no file system holds and which folds everywhere.
                anchored = PurePath(str(share).replace("host", "HOST").replace("share", "Share", 1))
                self.assertTrue(same_parts(anchored.parts, share.parts))

    def test_where_no_directory_answers_parts_compare_as_written(self) -> None:
        # A key folds where nothing answers, the answer that refuses more, but an admitting comparison does not.
        with mock.patch.object(os, "scandir", side_effect=PermissionError(13, "Permission denied")):
            reference = PurePath(self.root / "Name").parts
            self.assertFalse(same_parts(PurePath(self.root / "nAME").parts, reference))
            self.assertTrue(same_parts(reference, reference))
            self.assertEqual(path_key(self.root / "Name"), path_key(self.root / "nAME"))

    @unittest.skipUnless(
        sys.platform == "win32", "only Windows names a share, which may hold a non-ASCII name, in an anchor"
    )
    def test_anchors_differing_in_form_alone_are_two_and_are_never_asked(self) -> None:
        composed = PurePath("\\\\host\\caf\u00e9\\x").parts
        decomposed = PurePath("\\\\host\\cafe\u0301\\x").parts
        with mock.patch.object(os, "lstat", side_effect=AssertionError("an anchor is never asked")):
            self.assertFalse(same_parts(decomposed, composed))
        self.assertTrue(same_parts(composed, composed))

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

    def test_a_folding_directory_keys_a_name_one_letter_to_one_and_none_keys_it_fully(self) -> None:
        # NTFS keeps Stra\u00dfe and Strasse apart, so a folding directory keys them apart; with no directory, a name
        # folds fully, the answer that refuses more names as one.
        with self.answers({self.root: True}):
            self.assertNotEqual(name_key("Stra\u00dfe", self.root), name_key("Strasse", self.root))
            self.assertEqual(name_key("STRA\u00dfE", self.root), name_key("stra\u00dfe", self.root))
            self.assertNotEqual(name_key("\u03f4", self.root), name_key("\u03b8", self.root))
        self.assertEqual(name_key("Stra\u00dfe"), name_key("Strasse"))

    def test_a_link_made_between_the_two_lookups_is_passed_over(self) -> None:
        # A case-sensitive folder can gain a hard link by the swapped spelling after the entry's own lookup; the
        # swapped lookup then finds the entry's own file, now with a second link, though the folder keeps case.
        (self.root / "PROBE.txt").write_bytes(b"")
        elsewhere = self.root.parent / f"{self.root.name}.link"
        real = os.lstat
        linked: list[Path] = []

        def lstat(path: Any) -> os.stat_result:
            spelled = Path(path)
            if spelled.name == "probe.TXT" and not linked:
                try:
                    os.link(self.root / "PROBE.txt", spelled)
                    linked.append(spelled)
                except FileExistsError:  # a folding folder already finds the entry by this spelling
                    os.link(self.root / "PROBE.txt", elsewhere)
                    linked.append(elsewhere)
                self.addCleanup(linked[0].unlink)
            return real(path)

        with mock.patch.object(os, "lstat", side_effect=lstat):
            self.assertIsNone(path_keys._asked_folds_case(str(self.root)))
        self.assertTrue(linked)

    def test_a_case_sensitive_directory_folds_nothing(self) -> None:
        (self.root / "PROBE.txt").write_bytes(b"")
        with self.swapped_case(same=False):
            self.assertFalse(folds_case(self.root))
        self.assertEqual("Guide.MD", name_key("Guide.MD", self.root))
        self.assertNotEqual(name_key("Guide.md", self.root), name_key("guide.md", self.root))

    def test_an_entry_removed_between_the_two_lookups_is_passed_over_never_read_as_case_sensitive(self) -> None:
        # A folding file system misses the case-swapped name too once the entry is gone, so that miss shows nothing.
        (self.root / "PROBE.txt").write_bytes(b"")
        real = os.lstat
        looked: list[str] = []

        def lstat(path: Any) -> os.stat_result:
            name = Path(path).name
            if name == "probe.TXT":
                raise FileNotFoundError(2, "No such file or directory", str(path))
            if name == "PROBE.txt":
                looked.append(str(path))
                if len(looked) > 1:
                    raise FileNotFoundError(2, "No such file or directory", str(path))
            return real(path)

        with mock.patch.object(os, "lstat", side_effect=lstat):
            self.assertIsNone(path_keys._asked_folds_case(str(self.root)))
        self.assertEqual(2, len(looked))
        self.assertNotIn(str(self.root), path_keys._FOLDS_CASE)

    def test_an_entry_with_a_second_link_is_passed_over_since_that_link_may_be_the_swapped_name(self) -> None:
        # A case-sensitive folder can gain a hard link spelled in the other case after its listing was read; a lookup
        # by that spelling then finds the entry's own file, though the folder keeps case.
        (self.root / "PROBE.txt").write_bytes(b"")
        elsewhere = self.root.parent / f"{self.root.name}.link"
        os.link(self.root / "PROBE.txt", elsewhere)
        try:
            with self.swapped_case(same=True):
                self.assertIsNone(path_keys._asked_folds_case(str(self.root)))
        finally:
            elsewhere.unlink()
        with self.swapped_case(same=True):
            self.assertTrue(path_keys._asked_folds_case(str(self.root)))

    def test_a_folder_is_asked_whatever_its_link_count(self) -> None:
        # Linux and macOS count each subfolder's `..` among a folder's links, so a folder's count shows no hard link.
        def status(mode: int, links: int) -> os.stat_result:
            return os.stat_result((mode, 1, 1, links, 0, 0, 0, 0, 0, 0))

        self.assertFalse(path_keys._hard_linked(status(stat.S_IFDIR | 0o755, 3)))
        self.assertTrue(path_keys._hard_linked(status(stat.S_IFREG | 0o644, 2)))
        self.assertFalse(path_keys._hard_linked(status(stat.S_IFREG | 0o644, 1)))

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


@unittest.skipUnless(sys.platform in ("win32", "linux"), "only a separate volume keeps case on macOS")
class CaseSensitiveFolderTests(KeyedTestCase):
    def test_two_hard_links_spelled_in_another_case_leave_the_folder_keeping_case(self) -> None:
        # The two names find one file, as one name spelled two ways would in a folder that folds case, but the folder
        # lists each as its own entry, which only a folder that keeps case can.
        folder = case_sensitive_folder(self.root / "work")
        hard_link_pair(folder, "0A.txt", "0a.TXT")
        self.assertFalse(folds_case(folder))
        self.assertNotEqual(path_key(folder / "result.json"), path_key(folder / "RESULT.JSON"))
        reference = PurePath(folder / "result.json").parts
        self.assertFalse(same_parts(PurePath(folder / "RESULT.JSON").parts, reference))
        self.assertTrue(same_parts(reference, reference))

    def test_a_swapped_name_listed_as_its_own_entry_keeps_case_whatever_a_lookup_finds(self) -> None:
        folder = case_sensitive_folder(self.root / "work")
        (folder / "PROBE.txt").write_bytes(b"")
        (folder / "probe.TXT").write_bytes(b"")
        real = os.lstat

        def unifying(path: Any) -> os.stat_result:
            given = Path(path)
            return real(given.with_name("PROBE.txt") if given.name == "probe.TXT" else given)

        with mock.patch.object(os, "lstat", side_effect=unifying):
            self.assertFalse(path_keys._asked_folds_case(str(folder)))


@unittest.skipUnless(sys.platform in ("win32", "linux"), "APFS finds one entry under both forms")
class NormalizationFormLinkTests(KeyedTestCase):
    composed, decomposed = "caf\u00e9", "cafe\u0301"

    def test_two_hard_links_spelled_in_two_forms_are_two_entries(self) -> None:
        hard_link_pair(self.root, self.composed, self.decomposed)
        reference = PurePath(self.root / self.composed).parts
        self.assertFalse(same_parts(PurePath(self.root / self.decomposed).parts, reference))

    def test_a_second_link_missing_from_the_listing_still_keeps_the_forms_apart(self) -> None:
        # A link made after the listing was read is listed nowhere the comparison saw, but its file has two links.
        hard_link_pair(self.root, self.composed, self.decomposed)
        reference = PurePath(self.root / self.composed).parts
        with mock.patch.object(path_keys, "_names", return_value={self.composed}):
            self.assertFalse(same_parts(PurePath(self.root / self.decomposed).parts, reference))

    def test_both_forms_listed_are_two_entries_whatever_a_lookup_finds(self) -> None:
        (self.root / self.composed).write_bytes(b"")
        (self.root / self.decomposed).write_bytes(b"")
        real = os.lstat

        def unifying(path: Any) -> os.stat_result:
            given = Path(path)
            return real(given.with_name(self.composed) if given.name == self.decomposed else given)

        reference = PurePath(self.root / self.composed).parts
        with mock.patch.object(os, "lstat", side_effect=unifying):
            self.assertFalse(same_parts(PurePath(self.root / self.decomposed).parts, reference))

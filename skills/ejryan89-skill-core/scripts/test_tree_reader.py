"""Regression tests for the one reader of a tree the .NET skills promise to read only inside: each call judges its path
by its text and its links before touching it, refuses one leading outside the tree with nothing outside reached, and
reads the rest as file_presence and dotnet_solution read them."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import file_presence
from dotnet_solution import refusal, same_path
from folder_walk import LINKED, LINKED_OUT_REASON, NotEntered
from test_folder_walk import link_folder, reached, recording, remove_link
from tree_reader import CLIMBS_OUT, TreeReader, TreeRefused


class TreeReaderCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "tree"
        self.outside = self.base / "outside"
        (self.root / "Lib").mkdir(parents=True)
        (self.outside / "sub").mkdir(parents=True)
        (self.root / "Lib" / "Lib.csproj").write_text("<Project />\n", encoding="utf-8")
        (self.outside / "Out.csproj").write_text("<Project />\n", encoding="utf-8")
        (self.outside / "sub" / "Deep.csproj").write_text("<Project />\n", encoding="utf-8")
        self.reader = TreeReader(self.root)

    def link(self, target: Path, relative: str) -> Path:
        link = self.root / relative
        link_folder(target, link)
        self.addCleanup(remove_link, link)
        return link

    def outside_reason(self, link: Path, target: Path | str) -> str:
        written = target.as_posix() if isinstance(target, Path) else target
        return (
            f"{link.as_posix()} is a link to {written}, outside the examined tree {self.root.as_posix()}, which the "
            "reader does not follow"
        )


class RefusalWordingTests(unittest.TestCase):
    def test_a_refusal_names_the_path_and_the_reason_as_the_skills_word_a_path_they_do_not_touch(self) -> None:
        # Pinned contract value: both .NET skills name a path they do not stat or read as `<path> cannot be examined:
        # <reason>` (dotnet_format_targets' listed_files, dependency_graph's unexaminable).
        error = TreeRefused(Path("a/b.csproj"), "why")
        self.assertEqual(f"{Path('a/b.csproj')} cannot be examined: why", str(error))
        self.assertEqual((Path("a/b.csproj"), "why"), (error.path, error.reason))
        self.assertIsInstance(error, OSError)
        climbs = "a `..` in it climbs above the examined tree once the links before it are followed"
        self.assertEqual(climbs, CLIMBS_OUT)


class InsideTests(TreeReaderCase):
    def test_a_path_inside_the_tree_with_no_link_is_read_as_file_presence_and_dotnet_solution_read_it(self) -> None:
        project = self.root / "Lib" / "Lib.csproj"
        self.assertIsNone(self.reader.refusal(project))
        self.assertTrue(self.reader.present(project))
        self.assertTrue(self.reader.folder(self.root / "Lib"))
        self.assertTrue(self.reader.there(project))
        self.assertTrue(self.reader.occupied(project))
        self.assertFalse(self.reader.is_link(project))
        self.assertIsNone(self.reader.missing_reason(project))
        self.assertEqual(b"<Project />\n", self.reader.contents(project))
        self.assertEqual("<Project />\n", self.reader.text(project))
        self.assertEqual([project], self.reader.listing(self.root / "Lib"))
        self.assertEqual(project.stat().st_size, self.reader.status(project).st_size)
        self.assertFalse(self.reader.read_only(project))
        self.assertEqual(same_path(project), self.reader.key(project))
        self.assertEqual([project], self.reader.search(self.root, lambda name: name.endswith(".csproj")).files)
        self.assertEqual([self.root / "Lib", self.root], list(self.reader.climb(self.root / "Lib")))

    def test_a_missing_path_inside_the_tree_is_absent_as_file_presence_answers_it(self) -> None:
        missing = self.root / "Gone" / "Gone.csproj"
        self.assertIsNone(self.reader.refusal(missing))
        self.assertFalse(self.reader.present(missing))
        self.assertFalse(self.reader.folder(missing))
        self.assertFalse(self.reader.occupied(missing))
        self.assertEqual(file_presence.MISSING, self.reader.missing_reason(missing))
        with self.assertRaises(OSError) as caught:
            self.reader.contents(missing)
        self.assertNotIsInstance(caught.exception, TreeRefused)

    def test_a_folder_link_that_stays_inside_the_tree_is_read_through(self) -> None:
        via = self.link(self.root / "Lib", "Via")
        self.assertIsNone(self.reader.refusal(via / "Lib.csproj"))
        self.assertTrue(self.reader.present(via / "Lib.csproj"))
        self.assertTrue(self.reader.is_link(via))


class TextRefusalTests(TreeReaderCase):
    def test_a_path_outside_the_tree_by_its_text_is_refused_unopened_in_dotnet_solutions_words(self) -> None:
        # Outside by its text, through its own `..`, as a network share, and a segment Windows opens as a device.
        for path in (
            self.outside / "Out.csproj",
            self.root / ".." / "outside" / "Out.csproj",
            Path("//server/share/x.csproj"),
            self.root / "NUL",
        ):
            with self.subTest(path=path):
                touched: list[tuple[str, bool]] = []
                with recording(touched), self.assertRaises(TreeRefused) as caught:
                    self.reader.contents(path)
                self.assertEqual(refusal(Path(os.path.abspath(path)), self.root), caught.exception.reason)  # noqa: PTH100 - the text alone, as the reader takes it
                self.assertEqual(path, caught.exception.path)
                self.assertEqual([], reached(touched, [], self.outside))
                self.assertNotIn(str(path), [name for name, _ in touched])


@unittest.skipIf(sys.platform == "win32", "a symbolic link to a file needs a privilege on Windows")
class FileLinkTests(TreeReaderCase):
    def test_a_file_link_leading_outside_is_refused_by_every_read_with_nothing_outside_reached(self) -> None:
        link = self.root / "Linked.csproj"
        link.symlink_to(self.outside / "Out.csproj")
        reason = self.outside_reason(link, self.outside / "Out.csproj")
        reads: dict[str, Callable[[Path], Any]] = {
            "present": self.reader.present,
            "folder": self.reader.folder,
            "there": self.reader.there,
            "status": self.reader.status,
            "read_only": self.reader.read_only,
            "contents": self.reader.contents,
            "text": self.reader.text,
            "listing": self.reader.listing,
            "judged": self.reader.judged,
        }
        touched: list[tuple[str, bool]] = []
        for name, read in reads.items():
            with self.subTest(read=name), recording(touched):
                with self.assertRaises(TreeRefused) as caught:
                    read(link)
                self.assertEqual(reason, caught.exception.reason)
        with recording(touched):
            self.assertEqual(f"cannot be examined: {reason}", self.reader.missing_reason(link))
            self.assertEqual(str(link), self.reader.key(link))
        self.assertEqual([], reached(touched, [link], self.outside))

    def test_the_place_itself_is_examined_from_its_own_metadata_never_through_a_link_there(self) -> None:
        # MSBuild's Exists() and a writer ask whether the place is taken; the link there is never followed.
        link = self.root / "Linked.csproj"
        link.symlink_to(self.outside / "Out.csproj")
        dangling = self.root / "Dangling.csproj"
        dangling.symlink_to(self.root / "Nothing.csproj")
        touched: list[tuple[str, bool]] = []
        with recording(touched):
            self.assertTrue(self.reader.occupied(link))
            self.assertTrue(self.reader.is_link(link))
            self.assertTrue(self.reader.occupied(dangling))
        self.assertFalse(self.reader.present(dangling))
        self.assertEqual([], reached(touched, [link], self.outside))

    def test_a_dot_dot_after_a_link_to_the_trees_own_folder_climbs_out_and_is_refused(self) -> None:
        # `Here` leads to the tree's top, so `Here/../outside` is outside once Linux and macOS follow it, though its
        # text reads as inside.
        (self.root / "Here").symlink_to(".", target_is_directory=True)
        path = self.root / "Here" / ".." / "outside" / "Out.csproj"
        touched: list[tuple[str, bool]] = []
        with recording(touched), self.assertRaises(TreeRefused) as caught:
            self.reader.present(path)
        self.assertEqual(CLIMBS_OUT, caught.exception.reason)
        self.assertEqual([], reached(touched, [], self.outside))


class FolderLinkTests(TreeReaderCase):
    def test_a_folder_link_leading_outside_is_refused_listed_or_read_below_with_nothing_outside_reached(
        self,
    ) -> None:
        out = self.link(self.outside / "sub", "Out")
        reason = self.outside_reason(out, self.outside / "sub")
        touched: list[tuple[str, bool]] = []
        with recording(touched):
            for read in (self.reader.listing, self.reader.folder, self.reader.climb):
                with self.subTest(read=read.__name__), self.assertRaises(TreeRefused) as caught:
                    list(read(out)) if read is self.reader.climb else read(out)
                self.assertEqual(reason, caught.exception.reason)
            with self.assertRaises(TreeRefused):
                self.reader.present(out / "Deep.csproj")
            with self.assertRaises(TreeRefused):
                self.reader.occupied(out / "Deep.csproj")
            self.assertEqual(str(out / "Deep.csproj"), self.reader.key(out / "Deep.csproj"))
        self.assertEqual([], reached(touched, [out], self.outside))

    def test_a_walk_is_given_the_tree_so_it_follows_no_link_entry_out_of_it(self) -> None:
        out = self.link(self.outside / "sub", "Out")
        touched: list[tuple[str, bool]] = []
        with recording(touched):
            walk = self.reader.search(self.root, lambda name: name.endswith(".csproj"))
        self.assertEqual([self.root / "Lib" / "Lib.csproj"], walk.files)
        self.assertEqual([NotEntered(out, LINKED, LINKED_OUT_REASON)], walk.not_entered)
        self.assertEqual([], reached(touched, [out], self.outside))

    def test_a_walk_or_climb_from_outside_the_tree_is_refused_before_it_starts(self) -> None:
        touched: list[tuple[str, bool]] = []
        with recording(touched):
            with self.assertRaises(TreeRefused):
                self.reader.search(self.outside, lambda name: True)
            with self.assertRaises(TreeRefused):
                list(self.reader.climb(self.outside / "sub"))
        self.assertEqual([], reached(touched, [], self.outside))
        self.assertNotIn(str(self.outside), [name for name, _ in touched])

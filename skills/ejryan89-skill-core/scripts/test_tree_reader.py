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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import file_presence
import folder_walk
from dotnet_solution import refusal, same_path
from folder_walk import LINKED, LINKED_OUT_REASON, LINKED_REASON, LOOP, LOOP_REASON, NotEntered
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
        # Written as bytes, so the reader's bytes are compared with what was written on every platform.
        (self.root / "Lib" / "Lib.csproj").write_bytes(b"<Project />\n")
        (self.outside / "Out.csproj").write_bytes(b"<Project />\n")
        (self.outside / "sub" / "Deep.csproj").write_bytes(b"<Project />\n")
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


# The first bytes macOS writes in an AppleDouble sidecar on a FAT, exFAT, or SMB volume, written out so the reader is
# held to the bytes macOS writes, not to the constant it uses.
SIDECAR_BYTES = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        \x00\x02"


class SidecarListingTests(TreeReaderCase):
    """A listing leaves out the AppleDouble sidecars macOS writes, planted here with the bytes macOS writes, so these
    hold on every platform; any other `._` entry is listed."""

    def test_a_sidecar_beside_its_file_or_folder_is_not_listed(self) -> None:
        lib = self.root / "Lib"
        (lib / "._Lib.csproj").write_bytes(SIDECAR_BYTES)
        (self.root / "._Lib").write_bytes(SIDECAR_BYTES)
        self.assertEqual([lib / "Lib.csproj"], self.reader.listing(lib))
        self.assertEqual([lib], self.reader.listing(self.root))

    def test_any_other_dot_underscore_entry_is_listed(self) -> None:
        lib = self.root / "Lib"
        (lib / "App.sln").write_bytes(b"solution\n")
        # One with no header beside its file, and one with the header and no sibling, are the tree's own files.
        (lib / "._App.sln").write_bytes(b"solution\n")
        (lib / "._Gone.csproj").write_bytes(SIDECAR_BYTES)
        self.assertEqual(
            [lib / name for name in ("._App.sln", "._Gone.csproj", "App.sln", "Lib.csproj")], self.reader.listing(lib)
        )

    def test_a_listing_of_a_folder_it_cannot_list_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.reader.listing(self.root / "Gone")


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

    def test_a_climb_ends_at_its_stop_or_at_the_trees_top(self) -> None:
        deep = self.root / "Lib" / "Deep"
        deep.mkdir()
        self.assertEqual([deep, self.root / "Lib"], list(self.reader.climb(deep, self.root / "Lib")))
        self.assertEqual([deep, self.root / "Lib", self.root], list(self.reader.climb(deep)))

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
        # Outside by its text, through its own `..`, and as a network share.
        for path in (
            self.outside / "Out.csproj",
            self.root / ".." / "outside" / "Out.csproj",
            Path("//server/share/x.csproj"),
        ):
            with self.subTest(path=path):
                touched: list[tuple[str, bool]] = []
                with recording(touched), self.assertRaises(TreeRefused) as caught:
                    self.reader.contents(path)
                self.assertEqual(refusal(Path(os.path.abspath(path)), self.root), caught.exception.reason)  # noqa: PTH100 - the text alone, as the reader takes it
                self.assertEqual(path, caught.exception.path)
                # Deciding whether the path lies in the tree asks the tree's own folders how they compare names, which
                # may look up a sibling folder's own entry, never following it; nothing below or through one is reached.
                self.assertEqual([], reached(touched, [], self.outside))

    def test_a_name_windows_reserves_as_a_device_is_judged_by_its_links_alone(self) -> None:
        # A listing or git may give `Con.cs` or a folder `Aux` on Linux and macOS; the reader judges such a name by
        # containment and links only, and a caller judging a path a file names asks dotnet_solution.refusal first.
        # On Windows too, `Con.cs` and a folder `Aux` on the way are names its path parsing keeps, so the reader judges
        # them by their links alone there as well, as CI's Windows legs show.
        for path in (self.root / "App" / "Con.cs", self.root / "Aux" / "Helper.cs"):
            with self.subTest(path=path):
                self.assertIsNone(self.reader.refusal(path))
                self.assertIsNotNone(refusal(path, self.root))
        # A bare `NUL` is the device itself on Windows, whose path parsing turns it into `\\.\NUL`, so the reader
        # refuses it there unopened; elsewhere it is an ordinary name, judged by its links alone.
        nul = self.root / "NUL"
        expected = "the path names a device, which the reader does not open" if sys.platform == "win32" else None
        self.assertEqual(expected, self.reader.refusal(nul))


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


class OpensAsTests(TreeReaderCase):
    def test_a_name_in_a_folder_inside_the_tree_answers_as_the_folder_compares_names(self) -> None:
        project = self.root / "Lib" / "Lib.csproj"
        self.assertTrue(self.reader.opens_as(project, "Lib.csproj"))
        self.assertFalse(self.reader.opens_as(project, "Other.csproj"))

    @unittest.skipIf(sys.platform == "linux", "Linux's file systems keep case")
    def test_a_case_folded_name_is_the_entry_where_the_folder_folds_case(self) -> None:
        # Windows' NTFS and macOS's default APFS fold case, so the folder gives Lib.csproj for LIB.CSPROJ.
        self.assertTrue(self.reader.opens_as(self.root / "Lib" / "Lib.csproj", "LIB.CSPROJ"))

    def test_a_folder_a_link_leads_out_of_the_tree_is_refused_with_nothing_outside_reached(self) -> None:
        # A junction on Windows, a symbolic link on Linux and macOS.
        out = self.link(self.outside / "sub", "Out")
        touched: list[tuple[str, bool]] = []
        with recording(touched), self.assertRaises(TreeRefused) as caught:
            self.reader.opens_as(out / "Deep.csproj", "Deep.csproj")
        self.assertEqual(self.outside_reason(out, self.outside / "sub"), caught.exception.reason)
        self.assertEqual([], reached(touched, [out], self.outside))


class LeadsOutTests(TreeReaderCase):
    def test_a_folder_link_leading_out_leads_out_and_one_staying_inside_does_not(self) -> None:
        # A junction on Windows, a symbolic link on Linux and macOS.
        out = self.link(self.outside / "sub", "Out")
        inside = self.link(self.root / "Lib", "Shared")
        self.assertTrue(self.reader.leads_out(out / "Deep.csproj"))
        self.assertFalse(self.reader.leads_out(inside / "Lib.csproj"))
        self.assertFalse(self.reader.leads_out(self.root / "Lib" / "Lib.csproj"))

    def test_a_path_outside_the_tree_by_its_text_leads_out(self) -> None:
        self.assertTrue(self.reader.leads_out(self.outside / "Out.csproj"))
        self.assertTrue(self.reader.leads_out(self.root / ".." / "outside" / "Out.csproj"))

    def test_a_link_refused_for_its_unreadable_metadata_does_not_lead_out(self) -> None:
        # A junction on Windows, a symbolic link on Linux and macOS, to a folder inside the tree whose name cannot be
        # examined, as a folder without search permission makes it: refused, but not as a way out.
        inside = self.link(self.root / "Lib", "Shared")
        locked = self.root / "Lib"
        real_lstat = os.lstat

        def lstat(path: Any, *rest: Any, **options: Any) -> os.stat_result:
            if Path(path) == locked:
                raise PermissionError(13, "Access is denied")
            return real_lstat(path, *rest, **options)

        with mock.patch.object(os, "lstat", lstat):
            self.assertIsNotNone(self.reader.refusal(inside / "Lib.csproj"))
            self.assertFalse(self.reader.leads_out(inside / "Lib.csproj"))


@unittest.skipIf(sys.platform == "win32", "a symbolic link to a file needs a privilege on Windows")
class LeadsOutFileLinkTests(TreeReaderCase):
    def test_a_links_dot_dot_climbing_out_leads_out(self) -> None:
        (self.root / "Rel.csproj").symlink_to("../outside/Out.csproj")
        self.assertTrue(self.reader.leads_out(self.root / "Rel.csproj"))

    def test_a_loop_and_a_chain_past_the_link_limit_are_refused_but_do_not_lead_out(self) -> None:
        loop = self.root / "Loop.csproj"
        loop.symlink_to("Loop.csproj")
        chain = self.root / "Chain.csproj"
        chain.symlink_to("Lib/Lib.csproj")
        self.assertIsNotNone(self.reader.refusal(loop))
        self.assertFalse(self.reader.leads_out(loop))
        with mock.patch.object(folder_walk, "LINK_LIMIT", 0):
            self.assertIsNotNone(self.reader.refusal(chain))
            self.assertFalse(self.reader.leads_out(chain))


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

    def test_a_followed_walk_offers_follow_no_link_out_and_enters_one_that_stays_inside(self) -> None:
        # A junction on Windows, a symbolic link on Linux and macOS: the link leading out is reported as LINKED without
        # ever being offered to follow, and the one that stays inside is offered and entered.
        out = self.link(self.outside / "sub", "Out")
        inside = self.link(self.root / "Lib", "Shared")
        offered: list[Path] = []

        def follow(path: Path) -> bool:
            offered.append(path)
            return True

        touched: list[tuple[str, bool]] = []
        with recording(touched):
            walk = self.reader.search(self.root, lambda name: name.endswith(".csproj"), follow=follow)
        self.assertEqual([inside], offered)
        self.assertEqual([self.root / "Lib" / "Lib.csproj", inside / "Lib.csproj"], walk.files)
        self.assertEqual([NotEntered(out, LINKED, LINKED_OUT_REASON)], walk.not_entered)
        self.assertEqual([], reached(touched, [out], self.outside))

    def test_a_followed_link_back_into_the_walk_is_a_loop_and_the_walk_ends(self) -> None:
        loop = self.link(self.root, "Lib/Back")
        walk = self.reader.search(self.root, lambda name: name.endswith(".csproj"), follow=lambda _path: True)
        self.assertEqual([self.root / "Lib" / "Lib.csproj"], walk.files)
        self.assertEqual([NotEntered(loop, LOOP, LOOP_REASON)], walk.not_entered)

    def test_a_walk_without_follow_enters_no_folder_link(self) -> None:
        inside = self.link(self.root / "Lib", "Shared")
        walk = self.reader.search(self.root, lambda name: name.endswith(".csproj"))
        self.assertEqual([self.root / "Lib" / "Lib.csproj"], walk.files)
        self.assertEqual([NotEntered(inside, LINKED, LINKED_REASON)], walk.not_entered)

    def test_a_walk_or_climb_from_outside_the_tree_is_refused_before_it_starts(self) -> None:
        touched: list[tuple[str, bool]] = []
        with recording(touched):
            with self.assertRaises(TreeRefused):
                self.reader.search(self.outside, lambda name: True)
            with self.assertRaises(TreeRefused):
                list(self.reader.climb(self.outside / "sub"))
        # The case probe of the tree's own parent may look up the sibling `outside` folder's own entry, as NTFS lists
        # it first, never following it or reaching anything below it.
        self.assertEqual([], reached(touched, [], self.outside))

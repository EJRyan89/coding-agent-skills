"""Regression tests for the one folder walk the skills share: its order, the folders it skips or prunes, links,
failures, and its bound."""

from __future__ import annotations

import contextlib
import os
import stat
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, get_args
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import folder_walk
from folder_walk import (
    BOUNDED,
    LINK_LIMIT,
    LINKED,
    LINKED_OUT_REASON,
    LINKED_REASON,
    LOOP,
    LOOP_REASON,
    NESTED_CHECKOUT,
    NESTED_CHECKOUT_REASON,
    SKIPPED_FOLDERS,
    UNEXAMINED,
    UNLISTABLE,
    NotEntered,
    followed_inside,
    is_hidden,
    outside_link_refusal,
    walk_files,
)


def link_folder(target: Path, link: Path) -> None:
    """A real link to a folder: a junction on Windows, which needs no privilege, else a symbolic link."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def remove_link(link: Path) -> None:
    """Remove a link `link_folder` made, never what it points at."""
    if sys.platform == "win32":
        link.rmdir()
    else:
        link.unlink()


def every_file(name: str) -> bool:
    return True


class WalkTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "tree"
        self.root.mkdir()

    def write(self, *relatives: str) -> None:
        for relative in relatives:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n", encoding="utf-8")

    def link(self, target: Path, relative: str) -> Path:
        link = self.root / relative
        link_folder(target, link)
        self.addCleanup(remove_link, link)
        return link

    def files(self, walk: folder_walk.Walk) -> list[str]:
        return [path.relative_to(self.root).as_posix() for path in walk.files]

    def not_entered(self, walk: folder_walk.Walk) -> list[tuple[str, str]]:
        return [(entry.path.relative_to(self.root).as_posix(), entry.kind) for entry in walk.not_entered]


class OrderTests(WalkTestCase):
    def test_each_folders_files_come_in_name_order_before_its_subfolders_which_are_walked_depth_first(self) -> None:
        self.write("b.txt", "a/z.txt", "a/m/one.txt", "a/b.txt", "c/c.txt", "A.txt")
        walk = walk_files(self.root, every_file)
        self.assertEqual(["A.txt", "b.txt", "a/b.txt", "a/z.txt", "a/m/one.txt", "c/c.txt"], self.files(walk))
        self.assertEqual([], walk.not_entered)

    def test_only_the_wanted_names_are_given(self) -> None:
        self.write("App.sln", "src/Lib.slnx", "src/Lib.cs", "README.md")
        walk = walk_files(self.root, lambda name: name.endswith((".sln", ".slnx")))
        self.assertEqual(["App.sln", "src/Lib.slnx"], self.files(walk))


class SkippedFolderTests(WalkTestCase):
    def test_the_skipped_folders_are_version_control_dependency_build_and_visual_studio_folders(self) -> None:
        self.assertEqual(
            {
                ".git",
                ".hg",
                ".svn",
                "node_modules",
                ".venv",
                "venv",
                ".tox",
                ".nox",
                "__pycache__",
                "bin",
                "obj",
                ".vs",
            },
            SKIPPED_FOLDERS,
        )

    def test_a_skipped_folder_is_matched_without_case_and_not_reported(self) -> None:
        self.write("src/keep.txt", "src/BIN/Debug/skip.txt", "Node_Modules/pkg/skip.txt", ".git/skip.txt")
        walk = walk_files(self.root, every_file)
        self.assertEqual((["src/keep.txt"], []), (self.files(walk), walk.not_entered))

    def test_a_caller_names_its_own_set(self) -> None:
        self.write("bin/kept.txt", "packages/Pkg/skipped.txt", "src/Packages/skipped.txt")
        walk = walk_files(self.root, every_file, skipped=frozenset({"packages"}))
        self.assertEqual(["bin/kept.txt"], self.files(walk))

    def test_a_nested_checkout_is_reported_and_not_searched_and_the_top_is_searched(self) -> None:
        self.write(".git/HEAD", "top.txt", "vendor/clone/.git/HEAD", "vendor/clone/inner.txt", "vendor/kept.txt")
        (self.root / "worktrees/task").mkdir(parents=True)
        (self.root / "worktrees/task/.git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        self.write("worktrees/task/copy.txt")
        walk = walk_files(self.root, every_file)
        self.assertEqual(["top.txt", "vendor/kept.txt"], self.files(walk))
        self.assertEqual(
            [("vendor/clone", NESTED_CHECKOUT), ("worktrees/task", NESTED_CHECKOUT)], self.not_entered(walk)
        )
        self.assertEqual(
            "the folder holds another checkout, a clone, submodule, or worktree, which the search skips",
            NESTED_CHECKOUT_REASON,
        )

    def test_a_git_entry_in_another_case_marks_a_checkout_only_where_its_folder_folds_case(self) -> None:
        # On a folding file system .GIT is the clone's .git folder; on a case-sensitive one it is another folder.
        self.write("vendor/clone/.GIT/HEAD", "vendor/clone/inner.txt")
        clone = self.root / "vendor" / "clone"
        folds = (clone / ".git").exists()  # the folder's own lookup, which a folding file system answers for .GIT
        walk = walk_files(self.root, every_file)
        if folds:
            self.assertEqual(([], [("vendor/clone", NESTED_CHECKOUT)]), (self.files(walk), self.not_entered(walk)))
        else:
            self.assertEqual((["vendor/clone/inner.txt"], []), (self.files(walk), self.not_entered(walk)))


class PruneTests(WalkTestCase):
    def listed(self, top: Path, prune: Callable[[Path], bool] | None = None) -> tuple[folder_walk.Walk, list[str]]:
        """The walk, and each folder it listed, relative to the root."""
        listing = os.scandir
        folders: list[str] = []

        def scandir(path: str | os.PathLike[str]) -> object:
            folders.append(Path(path).relative_to(self.root).as_posix())
            return listing(path)

        with mock.patch.object(os, "scandir", scandir):
            walk = walk_files(top, every_file, prune=prune)
        return walk, folders

    def test_is_hidden_accepts_a_name_starting_with_a_dot_and_nothing_else(self) -> None:
        # The prune the contract names: the name alone decides, never a part above it or the file system.
        for name in (".git", ".cache", ".x.csproj", "..hidden"):
            with self.subTest(name=name):
                self.assertTrue(is_hidden(self.base / "missing" / name))
        for name in ("src", "a.b", "dot.", "_cache", " .cache", "\uff0ecache"):
            with self.subTest(name=name):
                self.assertFalse(is_hidden(self.base / ".parent" / name))
        self.assertFalse(is_hidden(Path(".hidden") / "visible"))

    def test_a_pruned_folder_is_neither_entered_nor_reported(self) -> None:
        self.write("kept.txt", ".cache/inner/skip.txt", "src/.idea/skip.txt", "src/kept.txt", ".clone/.git/HEAD")
        walk, folders = self.listed(self.root, prune=is_hidden)
        self.assertEqual(["kept.txt", "src/kept.txt"], self.files(walk))
        self.assertEqual([], walk.not_entered)
        self.assertEqual([".", "src"], folders)

    def test_prune_is_given_the_folders_path_and_skipped_folders_stay_skipped(self) -> None:
        self.write("a/drafts/skip.txt", "b/drafts/kept.txt", "bin/skip.txt")
        seen: list[Path] = []

        def prune(path: Path) -> bool:
            seen.append(path)
            return path == self.root / "a" / "drafts"

        walk = walk_files(self.root, every_file, prune=prune)
        self.assertEqual(["b/drafts/kept.txt"], self.files(walk))
        self.assertEqual([self.root / "a", self.root / "b", self.root / "a/drafts", self.root / "b/drafts"], seen)

    def test_a_pruned_link_is_not_reported_and_the_top_is_never_pruned(self) -> None:
        self.write(".top/file.txt")
        self.link(self.base, ".linked")
        self.assertEqual([], walk_files(self.root, every_file, prune=is_hidden).not_entered)
        walk = walk_files(self.root / ".top", every_file, prune=is_hidden)
        self.assertEqual([".top/file.txt"], self.files(walk))

    def test_without_prune_a_hidden_folder_is_entered(self) -> None:
        self.write(".cache/kept.txt")
        walk, folders = self.listed(self.root)
        self.assertEqual([".cache/kept.txt"], self.files(walk))
        self.assertEqual([".", ".cache"], folders)


class BoundTests(WalkTestCase):
    def test_past_its_limit_the_walk_stops_and_reports_the_next_folder(self) -> None:
        self.write("top.txt", "a/a.txt", "a/inner/inner.txt", "b/b.txt", "c/c.txt")
        walk = walk_files(self.root, every_file, limit=3)
        self.assertEqual(["top.txt", "a/a.txt", "a/inner/inner.txt"], self.files(walk))
        self.assertEqual([NotEntered(self.root / "b", BOUNDED, folder_walk.bounded_reason(3))], walk.not_entered)
        self.assertEqual(
            "the search stopped at its bound of 3 folders, before this folder and the folders after it",
            folder_walk.bounded_reason(3),
        )

    def test_the_limit_counts_the_top_and_a_walk_within_it_is_not_bounded(self) -> None:
        self.write("top.txt", "a/a.txt")
        self.assertEqual([("a", BOUNDED)], self.not_entered(walk_files(self.root, every_file, limit=1)))
        walk = walk_files(self.root, every_file, limit=2)
        self.assertEqual((["top.txt", "a/a.txt"], []), (self.files(walk), walk.not_entered))

    def test_pruned_and_skipped_folders_do_not_count_against_the_limit(self) -> None:
        self.write(".cache/a/x.txt", "node_modules/x.txt", "src/kept.txt")
        walk = walk_files(self.root, every_file, prune=is_hidden, limit=2)
        self.assertEqual((["src/kept.txt"], []), (self.files(walk), walk.not_entered))


class LinkTests(WalkTestCase):
    def test_a_linked_folder_is_reported_and_never_entered_wherever_it_leads(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "outside.txt").write_text("x\n", encoding="utf-8")
        self.write("inside/inside.txt")
        self.link(outside, "out")
        self.link(self.root, "inside/loop")
        self.link(self.root / "inside", "sideways")
        gone = self.base / "gone"
        gone.mkdir()
        self.link(gone, "stale")
        gone.rmdir()
        expected = [("out", LINKED), ("sideways", LINKED), ("stale", LINKED), ("inside/loop", LINKED)]
        walk = walk_files(self.root, every_file)
        self.assertEqual(["inside/inside.txt"], self.files(walk))
        self.assertEqual(expected, self.not_entered(walk))
        self.assertEqual(
            "the folder is a junction or a symbolic link, which the search does not follow", walk.not_entered[0].reason
        )
        self.assertEqual(LINKED_REASON, walk.not_entered[0].reason)

    def test_a_link_is_judged_by_link_probe_from_the_entrys_own_metadata_and_one_to_a_file_is_a_file(self) -> None:
        reparse = stat.FILE_ATTRIBUTE_REPARSE_POINT
        gone = FileNotFoundError(2, "No such file or directory")
        cases = (
            ("a symbolic link to a folder", stat.S_IFLNK, 0, stat.S_IFDIR, True),
            ("a symbolic link that leads nowhere", stat.S_IFLNK, 0, gone, True),
            ("a symbolic link to a file", stat.S_IFLNK, 0, stat.S_IFREG, False),
            ("a symbolic link to a device", stat.S_IFLNK, 0, stat.S_IFCHR, False),
            ("a folder that is a reparse point of any tag", stat.S_IFDIR, reparse, stat.S_IFDIR, True),
            ("a file that is a reparse point", stat.S_IFREG, reparse, stat.S_IFREG, False),
            ("a plain folder", stat.S_IFDIR, 0x10, stat.S_IFDIR, False),
        )
        for case, mode, attributes, target, linked in cases:
            with self.subTest(case=case):
                own = mock.Mock(st_mode=mode, st_file_attributes=attributes)

                def metadata(*, follow_symlinks: bool = True, own: object = own, target: object = target) -> object:
                    if not follow_symlinks:
                        return own
                    if isinstance(target, OSError):
                        raise target
                    return mock.Mock(st_mode=target)

                entry = mock.Mock(spec=os.DirEntry, path=str(self.root / "entry"))
                entry.stat.side_effect = metadata
                self.assertEqual(linked, folder_walk.is_folder_link(entry))

    def test_a_link_named_like_a_skipped_folder_is_skipped_unreported(self) -> None:
        self.link(self.base, "node_modules")
        self.assertEqual([], walk_files(self.root, every_file).not_entered)

    def test_the_top_is_searched_even_when_it_is_a_link(self) -> None:
        self.write("real/file.txt")
        top = self.link(self.root / "real", "top")
        self.assertEqual([top / "file.txt"], walk_files(top, every_file).files)

    def test_a_followed_link_is_entered_but_never_back_into_a_folder_the_walk_is_inside(self) -> None:
        self.write("target/file.txt")
        self.link(self.root / "target", "linked")
        self.link(self.root, "linked_root")
        self.link(self.root / "target", "target/self")
        walk = walk_files(self.root, every_file, follow=lambda path: path.name != "linked_root")
        self.assertEqual(["linked/file.txt", "target/file.txt"], self.files(walk))
        self.assertEqual(
            [("linked_root", LINKED), ("linked/self", LOOP), ("target/self", LOOP)], self.not_entered(walk)
        )
        self.assertEqual(
            "the folder is a link back into a folder the search is inside, which it does not enter again", LOOP_REASON
        )
        walk = walk_files(self.root, every_file, follow=lambda path: True)
        self.assertIn(("linked_root", LOOP), self.not_entered(walk))
        self.assertEqual(["linked/file.txt", "target/file.txt"], self.files(walk))


class RecordedEntry:
    """A listed entry whose metadata calls are recorded in `touched` with whether each follows a link."""

    def __init__(self, entry: os.DirEntry[str], touched: list[tuple[str, bool]]) -> None:
        self.entry, self.touched, self.name, self.path = entry, touched, entry.name, entry.path

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        self.touched.append((self.path, follow_symlinks))
        return self.entry.stat(follow_symlinks=follow_symlinks)

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        self.touched.append((self.path, follow_symlinks))
        return self.entry.is_dir(follow_symlinks=follow_symlinks)

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        self.touched.append((self.path, follow_symlinks))
        return self.entry.is_file(follow_symlinks=follow_symlinks)

    def is_symlink(self) -> bool:
        self.touched.append((self.path, False))
        return self.entry.is_symlink()


@contextlib.contextmanager
def recording(touched: list[tuple[str, bool]]) -> Iterator[None]:
    """Record each path `os.stat`, `os.lstat`, `os.scandir`, `os.readlink`, and a listed entry's metadata calls reach,
    with whether the call follows a link there."""
    real_scandir, real_stat, real_lstat, real_readlink = os.scandir, os.stat, os.lstat, os.readlink

    @contextlib.contextmanager
    def scandir(path: Any = ".") -> Iterator[Iterator[RecordedEntry]]:
        touched.append((os.fsdecode(path), True))
        with real_scandir(path) as listing:
            yield (RecordedEntry(entry, touched) for entry in listing)

    def stat(path: Any, *rest: Any, follow_symlinks: bool = True, **options: Any) -> os.stat_result:
        touched.append((os.fsdecode(path), follow_symlinks))
        return real_stat(path, *rest, follow_symlinks=follow_symlinks, **options)

    def lstat(path: Any, *rest: Any, **options: Any) -> os.stat_result:
        touched.append((os.fsdecode(path), False))
        return real_lstat(path, *rest, **options)

    def readlink(path: Any, *rest: Any, **options: Any) -> Any:
        touched.append((os.fsdecode(path), False))
        return real_readlink(path, *rest, **options)

    with (
        mock.patch.object(os, "scandir", scandir),
        mock.patch.object(os, "stat", stat),
        mock.patch.object(os, "lstat", lstat),
        mock.patch.object(os, "readlink", readlink),
    ):
        yield


def reached(touched: list[tuple[str, bool]], links: list[Path], outside: Path) -> list[str]:
    """The touched paths below `outside`, a stat or listing of it, or a path that follows one of `links` or lies below
    it."""
    found = []
    for name, follows in touched:
        path = Path(name)
        below = path.is_relative_to(outside) and (follows or path != outside)
        if below or any((path == link and follows) or (path != link and path.is_relative_to(link)) for link in links):
            found.append(name)
    return found


@unittest.skipIf(sys.platform == "win32", "a symbolic link to a file needs a privilege on Windows")
class OutsideLinkTests(WalkTestCase):
    """`followed_inside` and `outside_link_refusal` follow a path's links inside a tree as text, never outside it, and
    a walk given that tree follows no link entry out of it."""

    def outside(self, link: Path, target: Path | str) -> str:
        target_text = target.as_posix() if isinstance(target, Path) else target
        return (
            f"{link.as_posix()} is a link to {target_text}, outside the examined tree {self.root.as_posix()}, which "
            "the reader does not follow"
        )

    def test_a_link_whose_target_stays_inside_the_tree_is_followed_there_and_so_is_a_plain_or_missing_path(
        self,
    ) -> None:
        # Relative and absolute targets inside the tree, a folder link inside it, and a `..` that steps back inside
        # are each followed as the system follows them; a missing name ends the look with the rest as written, and a
        # path outside the tree, or whose own `..` climbs out of it, is `within`'s question.
        self.write("Lib/Lib.csproj")
        lib = self.root / "Lib" / "Lib.csproj"
        (self.root / "In.csproj").symlink_to("Lib/Lib.csproj")
        (self.root / "Abs.csproj").symlink_to(lib)
        self.link(self.root / "Lib", "Via")
        (self.root / "Lib" / "Up.csproj").symlink_to("../Via/./Lib.csproj")
        cases = (
            (self.root / "In.csproj", lib),
            (self.root / "Abs.csproj", lib),
            (self.root / "Via" / "Lib.csproj", lib),
            (self.root / "Lib" / "Up.csproj", lib),
            (lib, lib),
            (self.root / "Gone" / "Gone.csproj", self.root / "Gone" / "Gone.csproj"),
            (lib / "Under.csproj", lib / "Under.csproj"),
            (self.base / "Elsewhere.csproj", None),
            (self.root / "Lib" / ".." / ".." / "tree" / "In.csproj", None),
        )
        for path, inside in cases:
            with self.subTest(path=path):
                self.assertEqual((inside, None), followed_inside(path, self.root))
                self.assertIsNone(outside_link_refusal(path, self.root))

    def test_a_link_leading_outside_the_tree_is_refused_naming_the_link_whose_text_leads_out(self) -> None:
        # An absolute target, a relative one climbing out with `..`, a `..` after a folder link that leads outside
        # (which the text alone reads as inside), a target naming the tree through `..`, and a `..` a link's text holds
        # after a second link inside the tree, which names the first link, the one whose `..` climbs out, are each
        # refused with no stat, lstat, readlink, or listing of anything outside the tree.
        outside = self.base / "outside"
        (outside / "sub").mkdir(parents=True)
        (outside / "F.csproj").write_text("x\n", encoding="utf-8")
        (self.root / "x" / "y").mkdir(parents=True)
        (self.root / "Abs.csproj").symlink_to(outside / "F.csproj")
        (self.root / "Rel.csproj").symlink_to("../outside/F.csproj")
        out = self.link(outside / "sub", "Out")
        (self.root / "Back.csproj").symlink_to("Out/../F.csproj")
        alias = f"{self.root.as_posix()}/../tree/Lib.csproj"
        (self.root / "Alias.csproj").symlink_to(alias)
        (self.root / "D2").symlink_to(self.root / "x" / "y", target_is_directory=True)
        climb = "D2/../../../outside/F.csproj"
        (self.root / "V.csproj").symlink_to(climb)
        cases = (
            (self.root / "Abs.csproj", self.outside(self.root / "Abs.csproj", outside / "F.csproj")),
            (self.root / "Rel.csproj", self.outside(self.root / "Rel.csproj", "../outside/F.csproj")),
            (self.root / "Back.csproj", self.outside(out, outside / "sub")),
            (out / "F.csproj", self.outside(out, outside / "sub")),
            (self.root / "Alias.csproj", self.outside(self.root / "Alias.csproj", alias)),
            (self.root / "V.csproj", self.outside(self.root / "V.csproj", climb)),
        )
        touched: list[tuple[str, bool]] = []
        for path, reason in cases:
            with self.subTest(path=path), recording(touched):
                self.assertEqual((None, reason), followed_inside(path, self.root))
        self.assertIn((str(out), False), touched)
        self.assertEqual([], reached(touched, [out], outside))

    def test_an_absolute_target_is_followed_from_the_tree_not_from_the_links_folder(self) -> None:
        # Sub/A.csproj's absolute target names .h/B.csproj from the tree's top, never from Sub, where nothing is; that
        # second link leads outside, so the path is refused naming it, with nothing outside reached.
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "F.csproj").write_text("x\n", encoding="utf-8")
        (self.root / "Sub").mkdir()
        (self.root / ".h").mkdir()
        second = self.root / ".h" / "B.csproj"
        second.symlink_to(outside / "F.csproj")
        (self.root / "Sub" / "A.csproj").symlink_to(second)
        touched: list[tuple[str, bool]] = []
        with recording(touched):
            answer = followed_inside(self.root / "Sub" / "A.csproj", self.root)
        self.assertEqual((None, self.outside(second, outside / "F.csproj")), answer)
        self.assertEqual([], reached(touched, [second], outside))

    def test_a_loop_of_links_an_unreadable_link_and_an_unreadable_name_are_refused(self) -> None:
        loop = self.root / "Loop.csproj"
        loop.symlink_to("Loop.csproj")
        self.assertEqual(40, LINK_LIMIT)
        self.assertEqual(
            "more than 40 links lie on the way to it, which the reader does not follow",
            outside_link_refusal(loop, self.root),
        )
        # A chain as long as the limit is followed; one link more is refused.
        chain = self.root / "Chain.csproj"
        chain.symlink_to("Next.csproj")
        self.write("Next.csproj")
        with mock.patch.object(folder_walk, "LINK_LIMIT", 1):
            self.assertEqual((self.root / "Next.csproj", None), followed_inside(chain, self.root))
        with mock.patch.object(folder_walk, "LINK_LIMIT", 0):
            self.assertEqual(
                "more than 0 links lie on the way to it, which the reader does not follow",
                outside_link_refusal(chain, self.root),
            )
        real_readlink = Path.readlink

        def readlink(path: Path) -> Path:
            if path == chain:
                raise PermissionError(13, "Access is denied")
            return real_readlink(path)

        with mock.patch.object(Path, "readlink", readlink):
            self.assertEqual(
                f"{chain.as_posix()} is a link whose target cannot be read: Access is denied",
                outside_link_refusal(chain, self.root),
            )
        locked = self.root / "Locked"
        real_lstat = os.lstat

        def lstat(path: Any, *rest: Any, **options: Any) -> os.stat_result:
            if Path(path) == locked:
                raise PermissionError(13, "Access is denied")
            return real_lstat(path, *rest, **options)

        with mock.patch.object(os, "lstat", lstat):
            self.assertEqual("Access is denied", outside_link_refusal(locked / "Lib.csproj", self.root))

    def test_a_walk_given_its_tree_follows_no_link_entry_out_of_it_not_even_to_learn_what_it_is(self) -> None:
        # A wanted link to a file outside is given to the caller to refuse, any other link leading outside is reported
        # as a link the search does not follow, a skipped or pruned one is passed over unreported, and a link to a
        # folder inside the tree is still told apart by its target, as before.
        outside = self.base / "outside"
        (outside / "dir").mkdir(parents=True)
        (outside / "F.csproj").write_text("x\n", encoding="utf-8")
        (outside / "README").write_text("x\n", encoding="utf-8")
        self.write("Lib/Lib.csproj")
        (self.root / "F.csproj").symlink_to(outside / "F.csproj")
        (self.root / "README").symlink_to(outside / "README")
        folders = [self.link(outside / "dir", name) for name in ("Ref", "obj", ".hidden")]
        self.link(self.root / "Lib", "Inside")
        links = [self.root / "F.csproj", self.root / "README", *folders]
        touched: list[tuple[str, bool]] = []
        with recording(touched):
            walk = walk_files(self.root, lambda name: name.endswith(".csproj"), prune=is_hidden, tree=self.root)
        self.assertEqual(["F.csproj", "Lib/Lib.csproj"], self.files(walk))
        # A link leading outside is reported as the folder is listed; one inside, as the walk would enter it.
        self.assertEqual([("README", LINKED), ("Ref", LINKED), ("Inside", LINKED)], self.not_entered(walk))
        self.assertEqual(
            [LINKED_OUT_REASON, LINKED_OUT_REASON, LINKED_REASON], [entry.reason for entry in walk.not_entered]
        )
        self.assertEqual(
            "the entry is a junction or a symbolic link that leads outside the examined tree, which the search does "
            "not follow",
            LINKED_OUT_REASON,
        )
        self.assertEqual([], reached(touched, links, outside))
        # Without a tree, the walk tells a link's kind by following it, as it always has.
        touched.clear()
        with recording(touched):
            walk_files(self.root, lambda name: name.endswith(".csproj"), prune=is_hidden)
        self.assertNotEqual([], reached(touched, links, outside))


class FailureTests(WalkTestCase):
    def test_a_folder_that_cannot_be_listed_is_reported_and_the_walk_goes_on(self) -> None:
        self.write("a/locked/hidden.txt", "a/open.txt", "b/open.txt")
        listing = os.scandir

        def scandir(path: str | os.PathLike[str]) -> object:
            if Path(path).name == "locked":
                raise PermissionError(13, "Access is denied")
            return listing(path)

        with mock.patch.object(os, "scandir", scandir):
            walk = walk_files(self.root, every_file)
        self.assertEqual(["a/open.txt", "b/open.txt"], self.files(walk))
        self.assertEqual(
            [NotEntered(self.root / "a/locked", UNLISTABLE, "the folder cannot be listed: Access is denied")],
            walk.not_entered,
        )

    def test_a_top_that_cannot_be_listed_is_reported(self) -> None:
        missing = self.root / "missing"
        for follow in (None, lambda path: True):
            walk = walk_files(missing, every_file, follow=follow)
            self.assertEqual([], walk.files)
            self.assertEqual([(missing, UNLISTABLE)], [(entry.path, entry.kind) for entry in walk.not_entered])

    def test_an_entry_that_cannot_be_examined_is_reported_and_the_walk_goes_on(self) -> None:
        self.write("broken.txt", "kept.txt")
        real = folder_walk.is_folder_link

        def is_folder_link(entry: os.DirEntry[str]) -> bool:
            if entry.name == "broken.txt":
                raise OSError(5, "Input/output error")
            return real(entry)

        with mock.patch.object(folder_walk, "is_folder_link", is_folder_link):
            walk = walk_files(self.root, every_file)
        self.assertEqual(["kept.txt"], self.files(walk))
        self.assertEqual(
            [NotEntered(self.root / "broken.txt", UNEXAMINED, "the entry cannot be examined: Input/output error")],
            walk.not_entered,
        )


def test_each_kind_the_walk_reports_is_named_once() -> None:
    kinds = ("linked", "loop", "nested-checkout", "unlistable", "unexamined", "bounded")
    assert get_args(folder_walk.NotEnteredKind) == kinds
    assert kinds == (LINKED, LOOP, NESTED_CHECKOUT, UNLISTABLE, UNEXAMINED, BOUNDED)

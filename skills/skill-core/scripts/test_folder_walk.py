"""Regression tests for the one folder walk the skills share: its order, the folders it skips, links, and failures."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import folder_walk
from folder_walk import (
    LINKED,
    LINKED_REASON,
    LOOP,
    LOOP_REASON,
    NESTED_CHECKOUT,
    NESTED_CHECKOUT_REASON,
    SKIPPED_FOLDERS,
    UNEXAMINED,
    UNLISTABLE,
    NotEntered,
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


class FailureTests(WalkTestCase):
    def test_a_folder_that_cannot_be_listed_is_reported_and_the_walk_goes_on(self) -> None:
        self.write("a/locked/hidden.txt", "a/open.txt", "b/open.txt")
        listing = os.scandir

        def scandir(path: str | os.PathLike[str]) -> object:
            if Path(path).name == "locked":
                raise PermissionError(13, "Access is denied")
            return listing(path)

        with mock.patch.object(folder_walk.os, "scandir", scandir):
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


if __name__ == "__main__":
    unittest.main()

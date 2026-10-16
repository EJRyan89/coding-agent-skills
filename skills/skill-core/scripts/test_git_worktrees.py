"""Regression tests for the one reading of a repository's worktrees and of what each checkout has changed."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from git_client import GitClient, GitError, GitResult
from git_test_base import GitTestCase
from git_worktrees import Change, Worktree, changed_paths, changes, list_worktrees


def answering(output: str, returncode: int = 0, stderr: str = "") -> tuple[GitClient, list[list[str]]]:
    """A client whose runner records each command and answers every one with output."""
    calls: list[list[str]] = []

    def run(command: Sequence[str], timeout: float) -> GitResult:
        calls.append(list(command))
        return GitResult(returncode, output, stderr)

    return GitClient(run), calls


class RepositoryTestCase(GitTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="git worktrees "))).resolve()
        self.repository = self.root / "a repository"
        self.git(self.root, "init", "-q", "-b", "main", str(self.repository))
        (self.repository / "README.md").write_text("readme\n", encoding="utf-8")
        self.git(self.repository, "add", "README.md")
        self.git(self.repository, "commit", "-q", "-m", "first")

    def git(self, directory: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(directory), *arguments], capture_output=True, text=True, check=True
        ).stdout.strip()


class WorktreeListingTests(RepositoryTestCase):
    def test_each_worktree_is_read_with_its_branch_or_detached_head_and_the_main_one_first(self) -> None:
        head = self.git(self.repository, "rev-parse", "HEAD")
        on_branch, detached = self.root / "on a branch", self.root / "detached"
        self.git(self.repository, "worktree", "add", "-q", "-b", "feat/demo", str(on_branch))
        self.git(self.repository, "worktree", "add", "-q", "--detach", str(detached))
        main, *linked = list_worktrees(GitClient(), on_branch)
        self.assertTrue(Path(main.path).samefile(self.repository), main)
        self.assertEqual((head, "main", True), (main.head, main.branch, main.main))
        # Git lists linked worktrees in the order the filesystem gives their records.
        read = {(tree.head, tree.branch, tree.main) for tree in linked if Path(tree.path).samefile(on_branch)}
        read |= {(tree.head, tree.branch, tree.main) for tree in linked if Path(tree.path).samefile(detached)}
        self.assertEqual({(head, "feat/demo", False), (head, None, False)}, read)
        self.assertEqual(2, len(linked))

    def test_a_path_that_holds_a_line_break_is_read_whole(self) -> None:
        # Windows refuses such a name, so git's own listing of one stands in.
        client, calls = answering(
            "worktree /hub\0HEAD 1111\0branch refs/heads/main\0\0"
            "worktree /hub/.claude/worktrees/feat-a\nb\0HEAD 2222\0detached\0\0"
            "worktree /hub/locked\0HEAD 3333\0branch refs/heads/fix/x\0locked held\nhere\0\0"
        )
        self.assertEqual(
            [
                Worktree("/hub", "1111", "main", True),
                Worktree("/hub/.claude/worktrees/feat-a\nb", "2222", None, False),
                Worktree("/hub/locked", "3333", "fix/x", False),
            ],
            list_worktrees(client, "/hub"),
        )
        self.assertEqual([["git", "-C", "/hub", "--no-optional-locks", "worktree", "list", "--porcelain", "-z"]], calls)

    def test_a_listing_git_refuses_raises_rather_than_reading_no_worktrees(self) -> None:
        client, _ = answering("", 128, "fatal: not a git repository")
        with self.assertRaises(GitError) as raised:
            list_worktrees(client, "/nowhere")
        self.assertEqual((128, "not_repository"), (raised.exception.returncode, raised.exception.kind))


class ChangeTests(RepositoryTestCase):
    def test_an_untracked_file_counts_whatever_status_show_untracked_files_says(self) -> None:
        self.git(self.repository, "config", "status.showUntrackedFiles", "no")
        (self.repository / "draft.txt").write_text("draft\n", encoding="utf-8")
        self.assertEqual("", self.git(self.repository, "status", "--porcelain"), "the setting hides the draft")
        self.assertEqual([Change("??", "draft.txt")], changes(GitClient(), self.repository))
        self.assertEqual(1, changed_paths(GitClient(), self.repository))

    def test_a_rename_is_one_change_naming_the_path_it_came_from(self) -> None:
        self.git(self.repository, "mv", "README.md", "MOVED.md")
        (self.repository / "untracked.txt").write_text("new\n", encoding="utf-8")
        self.assertEqual(
            [Change("R ", "MOVED.md", "README.md"), Change("??", "untracked.txt")],
            changes(GitClient(), self.repository),
        )
        self.assertEqual(2, changed_paths(GitClient(), self.repository))

    def test_the_path_a_rename_came_from_is_never_read_as_an_entry_of_its_own(self) -> None:
        (self.repository / "!! ignored.txt").write_text("content\n", encoding="utf-8")
        self.git(self.repository, "add", "!! ignored.txt")
        self.git(self.repository, "commit", "-q", "-m", "a name like an ignored entry")
        self.git(self.repository, "mv", "!! ignored.txt", "renamed.txt")
        (self.repository / ".gitignore").write_text("*.log\n", encoding="utf-8")
        (self.repository / "build.log").write_text("log\n", encoding="utf-8")
        found = changes(GitClient(), self.repository, "--ignored=matching")
        self.assertEqual(["build.log"], [change.path for change in found if change.code == "!!"])
        self.assertIn(Change("R ", "renamed.txt", "!! ignored.txt"), found)

    def test_reading_a_checkout_never_writes_its_index(self) -> None:
        # A file whose timestamp moved but whose content did not: a status with optional locks refreshes its index
        # entry and writes the index back under its lock.
        readme = self.repository / "README.md"
        moved = readme.stat().st_mtime + 5
        os.utime(readme, (moved, moved))
        index = self.repository / ".git" / "index"
        before = hashlib.sha256(index.read_bytes()).hexdigest()
        self.assertEqual(0, changed_paths(GitClient(), self.repository))
        self.assertEqual(before, hashlib.sha256(index.read_bytes()).hexdigest())
        self.git(self.repository, "status", "--porcelain")
        self.assertNotEqual(before, hashlib.sha256(index.read_bytes()).hexdigest(), "a plain status writes it")

    def test_a_checkout_git_cannot_read_raises_rather_than_counting_no_changes(self) -> None:
        broken = self.root / "broken"
        broken.mkdir()
        (broken / ".git").write_text(f"gitdir: {self.root / 'gone'}\n", encoding="utf-8")
        with self.assertRaises(GitError) as raised:
            changed_paths(GitClient(), broken)
        self.assertIsNotNone(raised.exception.returncode)

    def test_status_runs_with_its_named_untracked_mode_and_no_optional_locks(self) -> None:
        client, calls = answering("")
        changes(client, "/checkout", "--ignored=matching")
        self.assertEqual(
            [
                [
                    "git",
                    "-C",
                    "/checkout",
                    "--no-optional-locks",
                    "status",
                    "--porcelain",
                    "-z",
                    "--untracked-files=normal",
                    "--ignored=matching",
                ]
            ],
            calls,
        )


if __name__ == "__main__":
    unittest.main()

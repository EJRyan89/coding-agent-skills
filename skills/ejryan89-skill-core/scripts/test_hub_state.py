"""Regression tests for the one check of how a main checkout differs from a clean one of its default branch at the
commit its remote's default branch holds."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from git_client import GitClient, GitError, GitResult
from hub_state import Difference, changed_paths, differences


class HubTestCase(unittest.TestCase):
    """A hub cloned from a bare origin, on main at origin/main with nothing changed."""

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="hub state "))).resolve()
        self.origin = self.root / "origin.git"
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.root / "seed"
        self.git(self.root, "clone", "-q", str(self.origin), str(seed))
        self.commit(seed, "README.md", "first")
        self.git(seed, "push", "-q", "origin", "main")
        self.hub = self.root / "the hub"
        self.git(self.root, "clone", "-q", str(self.origin), str(self.hub))
        self.seed = seed

    def git(self, directory: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(directory), *arguments], capture_output=True, text=True, encoding="utf-8", check=True
        ).stdout.strip()

    def commit(self, checkout: Path, name: str, message: str) -> None:
        (checkout / name).write_text(f"{message}\n", encoding="utf-8")
        self.git(checkout, "add", name)
        self.git(checkout, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", message)

    def found(self) -> list[Difference]:
        return differences(GitClient(), self.hub, "main")


class DifferenceTests(HubTestCase):
    def test_a_clean_hub_on_main_at_origin_s_main_differs_in_nothing(self) -> None:
        self.assertEqual([], self.found())

    def test_a_tracked_change_and_an_untracked_file_are_named(self) -> None:
        (self.hub / "README.md").write_text("edited\n", encoding="utf-8")
        (self.hub / "stray file.txt").write_text("stray\n", encoding="utf-8")
        self.assertEqual(
            [Difference("changed", "it has 2 uncommitted or untracked paths: README.md, stray file.txt")],
            self.found(),
        )

    def test_past_the_named_paths_the_rest_are_counted(self) -> None:
        for name in ("a", "b", "c", "d", "e"):
            (self.hub / name).write_text(name, encoding="utf-8")
        self.assertEqual(
            [Difference("changed", "it has 5 uncommitted or untracked paths: a, b, c, and 2 more")], self.found()
        )

    def test_an_ignored_file_is_no_change(self) -> None:
        self.commit(self.seed, ".gitignore", "ignored.txt")
        self.git(self.seed, "push", "-q", "origin", "main")
        self.git(self.hub, "pull", "-q", "--ff-only", "origin", "main")
        (self.hub / "ignored.txt").write_text("ignored\n", encoding="utf-8")
        self.assertEqual([], self.found())

    def test_a_hub_on_another_branch_is_named_with_it(self) -> None:
        self.git(self.hub, "switch", "-q", "-c", "feat/stray")
        self.assertEqual([Difference("branch", "it is on feat/stray, not main")], self.found())

    def test_a_detached_hub_is_named_detached(self) -> None:
        self.git(self.hub, "switch", "-q", "--detach")
        self.assertEqual([Difference("branch", "it is on a detached HEAD, not main")], self.found())

    def test_a_commit_made_in_the_hub_is_counted_ahead(self) -> None:
        self.commit(self.hub, "stray.txt", "a commit made in the hub")
        self.assertEqual([Difference("ahead", "it is 1 commit ahead of origin/main")], self.found())

    def test_a_hub_origin_moved_past_is_counted_behind(self) -> None:
        self.commit(self.seed, "a.txt", "second")
        self.commit(self.seed, "b.txt", "third")
        self.git(self.seed, "push", "-q", "origin", "main")
        self.git(self.hub, "fetch", "-q", "origin")
        self.assertEqual([Difference("behind", "it is 2 commits behind origin/main")], self.found())

    def test_a_hub_that_diverged_is_counted_both_ways(self) -> None:
        self.commit(self.seed, "a.txt", "second")
        self.git(self.seed, "push", "-q", "origin", "main")
        self.git(self.hub, "fetch", "-q", "origin")
        self.commit(self.hub, "stray.txt", "a commit made in the hub")
        self.assertEqual(
            [
                Difference("ahead", "it is 1 commit ahead of origin/main"),
                Difference("behind", "it is 1 commit behind origin/main"),
            ],
            self.found(),
        )

    def test_a_hub_with_no_remote_tracking_ref_says_so(self) -> None:
        self.git(self.hub, "update-ref", "-d", "refs/remotes/origin/main")
        self.assertEqual([Difference("unknown", "git holds no origin/main to compare it with")], self.found())

    def test_a_branch_or_tag_named_like_the_tracking_ref_is_not_read_for_it(self) -> None:
        self.git(self.hub, "update-ref", "-d", "refs/remotes/origin/main")
        self.git(self.hub, "tag", "origin/main")
        self.git(self.hub, "branch", "remotes/origin/main")
        self.assertEqual([Difference("unknown", "git holds no origin/main to compare it with")], self.found())

    def test_every_difference_is_reported_together(self) -> None:
        self.commit(self.hub, "stray.txt", "a commit made in the hub")
        self.git(self.hub, "switch", "-q", "-c", "feat/stray")
        (self.hub / "README.md").write_text("edited\n", encoding="utf-8")
        self.assertEqual(
            ["changed", "branch", "ahead"],
            [difference.kind for difference in self.found()],
        )

    if sys.platform != "win32":  # Windows refuses a file name holding a line break

        def test_a_path_holding_a_line_break_is_named_on_one_line(self) -> None:
            (self.hub / "two\nlines").write_text("x", encoding="utf-8")
            self.assertEqual([Difference("changed", "it has 1 uncommitted or untracked path: two lines")], self.found())


class ReadingTests(unittest.TestCase):
    def test_a_rename_names_its_new_path_once(self) -> None:
        self.assertEqual(["new", "x"], changed_paths(["R  new", "old", "?? x"]))
        self.assertEqual(["copy"], changed_paths([" C copy", "source"]))

    def test_a_checkout_git_cannot_read_raises(self) -> None:
        def run(command: Sequence[str], timeout: float) -> GitResult:
            return GitResult(128, "", "fatal: not a git repository")

        with self.assertRaises(GitError):
            differences(GitClient(run), Path("/nowhere"), "main")

    def test_counts_git_does_not_give_raise(self) -> None:
        for counts in ("x\n", "1\n", "1\t\n", "\u0661\t0\n"):
            answers = iter(["", "main\n", "c0ffee\0refs/remotes/origin/main\n", counts])

            def run(command: Sequence[str], timeout: float, answers: Iterator[str] = answers) -> GitResult:
                return GitResult(0, next(answers), "")

            with self.subTest(counts=counts), self.assertRaises(GitError):
                differences(GitClient(run), Path("/hub"), "main")

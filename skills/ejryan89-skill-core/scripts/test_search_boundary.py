"""Regression tests for the git root that ends a runtime's upward search from a throwaway folder."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from git_client import GitClient, GitError
from search_boundary import bound_search


class BoundSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.git = GitClient(timeout=60)

    def top(self, folder: Path) -> Path | None:
        return self.git.rev_parse_path("--show-toplevel", directory=folder)

    def test_a_folder_outside_any_repository_becomes_its_own_git_root(self) -> None:
        folder = self.root / "run" / "workspace"
        folder.mkdir(parents=True)
        self.assertIsNone(self.top(folder))
        bound_search(folder)
        top = self.top(folder)
        self.assertIsNotNone(top)
        self.assertEqual(folder, Path(str(top)).resolve())
        self.assertEqual([".git"], [entry.name for entry in folder.iterdir()])

    def test_a_folder_inside_another_repository_becomes_its_own_git_root(self) -> None:
        outer = self.root / "outer"
        self.git.output(["init", "--quiet", str(outer)])
        folder = outer / "home"
        folder.mkdir()
        bound_search(folder)
        self.assertEqual(folder, Path(str(self.top(folder))).resolve())

    def test_a_folder_that_already_holds_files_keeps_them(self) -> None:
        folder = self.root / "home"
        (folder / ".claude" / "skills").mkdir(parents=True)
        bound_search(folder)
        self.assertEqual([".claude", ".git"], sorted(entry.name for entry in folder.iterdir()))

    def test_the_client_given_runs_git_init_in_the_folder(self) -> None:
        client = mock.Mock()
        bound_search(self.root, client)
        client.output.assert_called_once_with(["init", "--quiet"], directory=self.root)

    def test_a_folder_git_cannot_make_a_repository_of_raises_git_s_reason(self) -> None:
        client = mock.Mock()
        client.output.side_effect = GitError("git init failed:\nfatal: cannot make it\n", returncode=128)
        with self.assertRaises(GitError) as raised:
            bound_search(self.root, client)
        self.assertEqual("git init failed:\nfatal: cannot make it\n", str(raised.exception))

    def test_a_missing_folder_is_refused(self) -> None:
        with self.assertRaises(GitError):
            bound_search(self.root / "missing")

"""Regression tests for the deployer's readers that ask whether a path is there: a stat that fails is refused or
raised, never read as nothing being there."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import DeployerTestCase, forward

from deployer import hashing, journal, platform_support, source, source_ref
from deployer.errors import DeployError

COMMIT = "0123456789abcdef0123456789abcdef01234567"


def denied(path: Path) -> PermissionError:
    return PermissionError(13, "Access is denied", str(path))


def stat_failing_at(path: Path) -> mock._patch[mock.MagicMock | mock.AsyncMock]:
    """Patch Path.stat so it raises PermissionError for one path, through links or not, and answers every other."""
    real = Path.stat

    def answer(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == path:
            raise denied(path)
        return real(self, follow_symlinks=follow_symlinks)

    return mock.patch.object(Path, "stat", autospec=True, side_effect=answer)


class PresenceReadTests(DeployerTestCase):
    def git_file(self, git_dir: Path) -> Path:
        """A source whose .git file names git_dir, as a linked worktree's does."""
        git_dir.mkdir(parents=True, exist_ok=True)
        marker = self.source / ".git"
        marker.write_text(f"gitdir: {forward(git_dir)}\n", encoding="utf-8")
        return marker

    def bash_beside_git(self, root: Path) -> mock._patch[mock.MagicMock | mock.AsyncMock]:
        """Patch the locator and the git run so git reports its exec path under root, as Git for Windows does."""
        exec_path = root / "mingw64" / "libexec" / "git-core"
        result = SimpleNamespace(returncode=0, output=f"{exec_path}\n")
        return mock.patch.multiple(
            platform_support,
            find_executable=mock.DEFAULT,
            run_tool=mock.Mock(return_value=result),
        )

    def readers(self) -> list[tuple[str, Path, Callable[[], object]]]:
        """Each reader, the path whose stat the row fails, and the call that reads it, after making what it reads."""
        tree = self.root / "tree"
        tree.mkdir()
        target = self.root / "target"
        target.write_text("content", encoding="utf-8")
        self.paths.staging_root.mkdir(parents=True)
        self.paths.agents_src.mkdir(parents=True, exist_ok=True)
        bash_root = self.root / "Git"
        return [
            ("hashing.find_link", tree, lambda: hashing.find_link(tree)),
            ("journal._hash_existing", target, lambda: journal._hash_existing(target)),
            ("journal.pending_runs", self.paths.staging_root, lambda: journal.pending_runs(self.paths)),
            ("source._discover_agents", self.paths.agents_src, lambda: source._discover_agents(self.paths)),
            ("source.is_linked_worktree", self.source / ".git", lambda: source.is_linked_worktree(self.source)),
            (
                "source_ref.is_release_pin",
                self.source / ".git",
                lambda: source_ref.is_release_pin(self.source, "refs/tags/v1.0.0", COMMIT),
            ),
            (
                "platform_support._bash_beside_git",
                bash_root / "bin" / "bash.exe",
                lambda: self.with_bash_beside_git(bash_root),
            ),
        ]

    def with_bash_beside_git(self, root: Path) -> str | None:
        with self.bash_beside_git(root):
            return platform_support._bash_beside_git()

    def test_a_path_whose_stat_fails_is_never_read_as_nothing_there(self) -> None:
        for name, path, read in self.readers():
            with self.subTest(reader=name):
                with stat_failing_at(path), self.assertRaises(Exception) as raised:
                    read()
                failure = raised.exception
                if isinstance(failure, DeployError):
                    self.assertIn(f"{forward(path)}: Access is denied", "\n".join(failure.lines))
                elif isinstance(failure, source_ref.TagsUnreadable):
                    self.assertEqual(f"{path}: Access is denied", str(failure))
                elif isinstance(failure, PermissionError):
                    self.assertEqual(str(path), failure.filename)
                else:
                    self.fail(f"{name} raised {failure!r}, not the stat's PermissionError")

    def test_the_refusals_name_what_could_not_be_read(self) -> None:
        self.paths.agents_src.mkdir(parents=True, exist_ok=True)
        cases: list[tuple[Path, Callable[[], object], str]] = [
            (self.paths.agents_src, lambda: source._discover_agents(self.paths), "read the agents folder"),
            (self.source / ".git", lambda: source.is_linked_worktree(self.source), "read the source's .git file"),
        ]
        for path, read, action in cases:
            with self.subTest(action=action):
                with stat_failing_at(path), self.assertRaises(DeployError) as raised:
                    read()
                self.assertEqual(
                    f"ERROR: Could not {action}: {forward(path)}: Access is denied", raised.exception.lines[0]
                )

    def test_a_staging_root_that_cannot_be_stat_ed_reports_no_runs_rather_than_none_committed(self) -> None:
        self.paths.staging_root.mkdir(parents=True)
        with (
            mock.patch.object(journal, "_committed_run_id", return_value="20260101-000000-done"),
            stat_failing_at(self.paths.staging_root),
        ):
            self.assertIsNone(journal.journaled_runs(self.paths))
        with mock.patch.object(journal, "_committed_run_id", return_value="20260101-000000-done"):
            self.assertEqual([], journal.journaled_runs(self.paths))

    def test_a_run_whose_journal_cannot_be_stat_ed_is_pending_for_the_user(self) -> None:
        run = self.staging_run("20260101-000000-held")
        run.mkdir(parents=True, exist_ok=True)
        empty = self.staging_run("20260101-000000-none")
        empty.mkdir(parents=True, exist_ok=True)
        with stat_failing_at(run / "journal.jsonl"):
            self.assertEqual([("20260101-000000-held", False)], journal.pending_runs(self.paths))

    def test_a_missing_staging_root_or_agents_folder_is_nothing_there(self) -> None:
        self.assertEqual([], journal.pending_runs(self.paths))
        self.assertEqual({}, source._discover_agents(self.paths))
        self.assertIsNone(journal._hash_existing(self.root / "missing"))
        self.assertIsNone(hashing.find_link(self.root / "missing"))

    def test_a_file_where_a_folder_is_read_is_refused_or_holds_no_link(self) -> None:
        self.paths.agents_src.parent.mkdir(parents=True, exist_ok=True)
        self.paths.agents_src.write_text("", encoding="utf-8")
        with self.assertRaises(DeployError) as raised:
            source._discover_agents(self.paths)
        self.assertEqual(
            f"ERROR: agents is not a directory: {forward(self.paths.agents_src)}", raised.exception.lines[0]
        )
        self.assertIsNone(hashing.find_link(self.paths.agents_src))

    def test_a_git_file_that_cannot_be_read_is_refused_not_read_as_no_worktree(self) -> None:
        marker = self.git_file(self.root / "git" / "worktrees" / "task")
        with self.unreadable(marker), self.assertRaises(DeployError) as raised:
            source.is_linked_worktree(self.source)
        self.assertIn("Could not read the source's .git file", raised.exception.lines[0])
        marker.write_bytes(b"gitdir: \xff\n")
        with self.assertRaises(DeployError) as raised:
            source.is_linked_worktree(self.source)
        self.assertEqual(
            f"ERROR: Could not read the source's .git file: {forward(marker)}: it is not UTF-8 text",
            raised.exception.lines[0],
        )

    def test_a_common_directory_that_cannot_be_stat_ed_is_refused_and_a_git_folder_is_no_worktree(self) -> None:
        git_dir = self.root / "git" / "worktrees" / "task"
        self.git_file(git_dir)
        (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
        self.assertTrue(source.is_linked_worktree(self.source))
        with stat_failing_at(git_dir / "commondir"), self.assertRaises(DeployError) as raised:
            source.is_linked_worktree(self.source)
        self.assertEqual(
            f"ERROR: Could not read the git directory the source's .git file names: {forward(git_dir / 'commondir')}: "
            "Access is denied",
            raised.exception.lines[0],
        )
        (self.source / ".git").unlink()
        (self.source / ".git").mkdir()
        self.assertFalse(source.is_linked_worktree(self.source))

    def test_a_folder_where_the_bash_beside_git_stands_is_no_bash(self) -> None:
        root = self.root / "Git"
        bash = root / "bin" / "bash.exe"
        self.assertIsNone(self.with_bash_beside_git(root))
        bash.mkdir(parents=True)
        self.assertIsNone(self.with_bash_beside_git(root))
        bash.rmdir()
        bash.write_text("", encoding="utf-8")
        self.assertEqual(str(bash), self.with_bash_beside_git(root))

"""How the report names each repository's main worktree that now differs from a clean checkout of its default branch
at origin's, through ejryan89-skill-core's hub_state.py: a write a session made there by mistake is seen at the next
cleanup. On the repositories test_repo_cleanup.py's Fixture builds."""

from __future__ import annotations

import contextlib
import io
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import git_client
import repo_cleanup as rc
from git_client import GitClient, GitResult
from test_repo_cleanup import GIT_CONFIG, SweepFixture, build_template, facts, git

# The settings conftest.py adds to the global configuration of every home this suite's git runs in.
git_config = GIT_CONFIG


def setUpModule() -> None:
    """Build the repositories every Fixture test starts from, with Git isolated as each test isolates it."""
    build_template()


class MainStateTests(SweepFixture):
    """The report names each way a planned repository's main worktree differs, and counts none as an error."""

    def test_the_report_names_how_each_main_worktree_now_differs_and_folds_only_a_quiet_one_that_does_not(
        self,
    ) -> None:
        stray = self.make_clone("stray repo")
        self.make_clone("quiet repo")
        code, lines = self.sweep()
        self.assertEqual(0, code, lines)
        root = self.repos.as_posix()
        (stray / "stray file.txt").write_text("stray\n", encoding="utf-8")
        self.commit(self.clone, "a commit made in the main worktree")

        code, lines = self.report()
        self.assertEqual(0, code, lines)
        self.assertEqual([[f"{root}/quiet repo"]], facts(lines, "QUIET"))
        blocks = self.blocks(lines)
        self.assertEqual([["it is 1 commit ahead of origin/main"]], facts(blocks["my repo"], "MAIN_DIFFERS"))
        self.assertEqual(
            [["it has 1 uncommitted or untracked path: stray file.txt"]], facts(blocks["stray repo"], "MAIN_DIFFERS")
        )
        self.assertEqual(
            [["Processed 3 repositories; skipped: 0 fetch failed, 0 aborted, 0 with an error."]],
            facts(lines, "PROCESSED"),
            "a difference is for the user, and no error of the cleanup's",
        )

    def test_a_main_worktree_left_off_its_default_branch_is_named_and_an_aborted_one_is_not(self) -> None:
        aborted = self.make_clone("aborted repo")
        (aborted / "draft.txt").write_text("draft\n", encoding="utf-8")
        git(self.clone, "switch", "--quiet", "-c", "topic/kept")
        (self.clone / "draft.txt").write_text("draft\n", encoding="utf-8")
        self.sweep()
        self.invoke(
            "sweep", "--repos-root", str(self.repos), "--record", str(self.root / "plans"), "--skip-checkout", "my repo"
        )
        root = self.repos.as_posix()
        code, lines = self.report(f"{root}/aborted repo")
        self.assertEqual(0, code, lines)
        blocks = self.blocks(lines)
        self.assertEqual(
            [["it has 1 uncommitted or untracked path: draft.txt"], ["it is on topic/kept, not main"]],
            facts(blocks["my repo"], "MAIN_DIFFERS"),
        )
        self.assertEqual([], blocks["aborted repo"])

    def test_a_main_worktree_git_cannot_read_is_named_with_git_s_reason(self) -> None:
        self.sweep()

        def refuse_status(command: Sequence[str], timeout: float) -> GitResult:
            if "status" in command:
                return GitResult(128, "", "fatal: index file corrupt")
            return git_client.subprocess_runner(command, timeout)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = rc.main(
                ["report", "--record", str(self.root / "plans")],
                rc.Services(git=GitClient(refuse_status), gh=self.github),
            )
        self.assertEqual(0, code)
        differing = facts(output.getvalue().splitlines(), "MAIN_DIFFERS")
        self.assertEqual(1, len(differing), differing)
        self.assertTrue(differing[0][0].startswith("it could not be read: "), differing)
        self.assertIn("fatal: index file corrupt", differing[0][0])

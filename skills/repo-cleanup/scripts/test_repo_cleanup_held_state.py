"""held_state, which keeps a worktree when Git cannot say what removing it would lose, against a Git that answers
from a table."""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import repo_cleanup as rc
from git_client import GitClient, GitResult
from test_repo_cleanup import GIT_CONFIG, CleanupTestCase, remove_tree

# The settings conftest.py adds to the global configuration of every home this suite's git runs in.
git_config = GIT_CONFIG


class HeldStateTests(CleanupTestCase):
    """held_state keeps a worktree when Git cannot say what its removal would lose, as its docstring says."""

    def setUp(self) -> None:
        super().setUp()
        self.worktree = Path(tempfile.mkdtemp(prefix="repo cleanup held ")).resolve()
        self.addCleanup(remove_tree, self.worktree)
        (self.worktree / "marked.txt").write_text("edited\n", encoding="utf-8")

    def held(self, answers: Mapping[str, GitResult]) -> str:
        """held_state against a Git that answers each subcommand from `answers`, by its name."""

        def answering(command: Sequence[str], timeout: float) -> GitResult:
            return next(answer for name, answer in answers.items() if name in command)

        return rc.held_state(rc.Services(git=GitClient(answering)), str(self.worktree))

    def answers(self, **changed: GitResult) -> dict[str, GitResult]:
        """What a worktree with no operation under way, no refs of its own, and one marked file answers."""
        markers = "".join(f"{self.worktree / 'git' / marker}\n" for marker, _ in rc.OPERATION_MARKERS)
        listed = f"S 100644 {'a' * 40} 0\tmarked.txt\0"
        return {
            "rev-parse": GitResult(0, markers, ""),
            "for-each-ref": GitResult(0, "", ""),
            "ls-files": GitResult(0, listed, ""),
            "hash-object": GitResult(0, f"{'a' * 40}\n", ""),
            **{name.replace("_", "-"): answer for name, answer in changed.items()},
        }

    def test_a_worktree_git_answers_for_in_full_holds_nothing(self) -> None:
        self.assertEqual("", self.held(self.answers()))

    def test_each_failure_of_git_to_say_keeps_the_worktree_with_why(self) -> None:
        lost = "cannot be read for what its removal would lose: "
        cases = {
            "a failing git": (
                {"rev_parse": GitResult(128, "", "fatal: not a git repository")},
                f"git rev-parse failed in {self.worktree}: fatal: not a git repository",
            ),
            "a marker left unnamed": (
                {"rev_parse": GitResult(0, f"{self.worktree / 'git' / 'BISECT_START'}\n", "")},
                f"git rev-parse did not name each operation marker of worktree {self.worktree}",
            ),
            "a marked file left unhashed": (
                {"hash_object": GitResult(0, "", "")},
                f"git hash-object did not hash each marked file of worktree {self.worktree}",
            ),
        }
        for name, (changed, why) in cases.items():
            with self.subTest(name):
                self.assertEqual(lost + why, self.held(self.answers(**changed)))

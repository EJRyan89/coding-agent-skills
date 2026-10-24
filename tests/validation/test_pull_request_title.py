"""Fixture tests for tests/validation/pull_request_title.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import executable_locator
import pull_request_title
from git_client import GitError, GitResult
from pull_request_title import head_branch, title_problems, title_types_problems

from tools import release_notes
from tools.worktrees import TITLE_TYPES


def git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(repository), *arguments], capture_output=True, check=True)


@contextlib.contextmanager
def git_not_on_path() -> Iterator[None]:
    """PATH names a folder that does not exist and no link folder is searched, so the real locator finds no git and
    the real client raises its own failure."""
    nowhere = str(Path(__file__).resolve().parent / "no git here")
    with mock.patch.dict(os.environ, {"PATH": nowhere}), mock.patch.object(executable_locator, "link_folders", list):
        yield


def timed_out(command: list[str], timeout: float) -> GitResult:
    """The real runner's failure when git does not finish within the client's time limit."""
    raise GitError(f"{' '.join(command)} did not finish within {timeout:g} seconds", kind="timeout")


class PullRequestTitlePolicy(unittest.TestCase):
    def test_the_types_are_the_agreed_ones(self) -> None:
        self.assertEqual(("feat", "fix", "docs", "test", "refactor", "chore", "ci", "build"), TITLE_TYPES)

    def test_the_type_agrees_with_a_branch_kind_that_is_a_type(self) -> None:
        self.assertEqual([], title_problems("fix(deployer): refuse a bad name", "fix/bad-name"))
        problems = title_problems("feat(deployer): refuse a bad name", "fix/bad-name")
        self.assertEqual(1, len(problems))
        self.assertIn("branch fix/bad-name", problems[0])

    def test_a_guide_must_name_every_type(self) -> None:
        listed = "Types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `ci`, and `build`.\n"
        self.assertEqual([], title_types_problems("GUIDE.md", listed))
        problems = title_types_problems("GUIDE.md", listed.replace(", and `build`", ""))
        self.assertEqual(["GUIDE.md does not name the pull request title type `build`"], problems)


@pytest.mark.parametrize(
    "title",
    [
        "fix(deployer): --at refuses a filtered archive member by name",
        "fix(review-insights, repo-cleanup): a schema version is a non-boolean integer",
        "docs: name the targeted local run",
        "build: raise the ruff pin\n",
    ],
)
def test_a_typed_title_passes_with_or_without_a_scope(title: str) -> None:
    assert title_problems(title, None) == []


@pytest.mark.parametrize(
    "title",
    [
        "Reuse shared flat_text in skill formatters",
        "fix:no space after the colon",
        "fix: ",
        "(deployer): a scope without a type",
        "fix(): an empty scope",
        pytest.param("", id="empty"),
    ],
)
def test_a_title_without_a_type_fails(title: str) -> None:
    problems = title_problems(title, None)
    assert len(problems) == 1
    assert "does not open with a type" in problems[0]


@pytest.mark.parametrize("title", ["style: sort the imports", "Fix: capitalized", "feature(deployer): spelled out"])
def test_an_unknown_type_fails_and_names_the_types(title: str) -> None:
    problems = title_problems(title, None)
    assert len(problems) == 1
    assert "is not a known type" in problems[0]
    assert "`feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `ci`, or `build`" in problems[0]


@pytest.mark.parametrize("title", ["feat!: drop the old flag", "feat(deployer)!: drop the old flag"])
def test_a_breaking_change_mark_fails(title: str) -> None:
    problems = title_problems(title, None)
    assert len(problems) == 1
    assert "`!`" in problems[0]
    assert "## Upgrade note" in problems[0]


# A branch whose first segment names no type, or that has none, constrains nothing.
@pytest.mark.parametrize(
    "branch", ["inv/flaky-suite", "claude/conventions-1a2b3c", "main", "fix", pytest.param("", id="empty")]
)
def test_a_branch_that_names_no_type_constrains_nothing(branch: str) -> None:
    assert title_problems("docs: anything", branch) == []


class HeadBranch(unittest.TestCase):
    """head_branch reads GITHUB_HEAD_REF in CI, where the checkout is a detached merge ref, and the checkout's own
    branch elsewhere; when neither names a branch, the title's type is left unconstrained."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = Path(temporary.name) / "checkout"
        self.repository.mkdir()
        git(self.repository, "init", "--initial-branch", "feat/local-branch")
        git(self.repository, "commit", "--allow-empty", "--message", "Initial")
        self.enterContext(mock.patch.object(pull_request_title, "REPOSITORY_ROOT", self.repository))
        environment = {name: value for name, value in os.environ.items() if name != "GITHUB_HEAD_REF"}
        self.enterContext(mock.patch.dict(os.environ, environment, clear=True))

    def test_the_pull_request_branch_ci_names_wins_over_the_checkouts(self) -> None:
        os.environ["GITHUB_HEAD_REF"] = "fix/ci-branch"
        self.assertEqual("fix/ci-branch", head_branch())

    def test_without_one_the_checkouts_branch_is_used(self) -> None:
        self.assertEqual("feat/local-branch", head_branch())
        # An empty value, as a push event's run sets, names no branch either.
        os.environ["GITHUB_HEAD_REF"] = ""
        self.assertEqual("feat/local-branch", head_branch())

    def test_the_branch_is_the_checkouts_whatever_repository_git_dir_names(self) -> None:
        other = self.repository.parent / "another repository"
        other.mkdir()
        git(other, "init", "--initial-branch", "fix/another-branch")
        git(other, "commit", "--allow-empty", "--message", "Initial")
        os.environ["GIT_DIR"] = str(other / ".git")
        self.assertEqual("feat/local-branch", head_branch())

    def test_a_detached_head_names_no_branch(self) -> None:
        git(self.repository, "checkout", "--detach")
        self.assertIsNone(head_branch())

    def test_a_git_failure_names_no_branch(self) -> None:
        # Outside any repository git fails; so does any git command the patched runner says failed.
        self.enterContext(mock.patch.object(pull_request_title, "REPOSITORY_ROOT", self.repository / "missing"))
        self.assertIsNone(head_branch())
        failed = GitResult(128, "feat/local-branch\n", "fatal: failed\n")
        with mock.patch.object(pull_request_title, "git", return_value=failed) as run:
            self.assertIsNone(head_branch())
        self.assertEqual(1, run.call_count)
        # Nor does a git that cannot run at all: the real helper's git missing, or not finishing in its time limit.
        self.enterContext(mock.patch.object(pull_request_title, "REPOSITORY_ROOT", self.repository))
        self.assertEqual("feat/local-branch", head_branch())
        for failing in (git_not_on_path(), mock.patch.object(release_notes.CLIENT, "runner", timed_out)):
            with self.subTest(failing=failing), failing:
                self.assertIsNone(head_branch())

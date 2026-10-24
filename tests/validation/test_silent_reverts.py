"""Fixture tests for tests/validation/silent_reverts.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import silent_reverts
from git_client import MISSING_GIT, GitError, GitResult
from silent_reverts import Revert, declarations, find_reverts, silent_revert_problems
from validation_support import REPOSITORY_ROOT

from tests import run_validation
from tools import release_notes
from tools.release_notes import ReleaseNotesError

# The audit's case: a pull request merged, and sixteen minutes later the next one's squash put five of its files
# back as they were.
UNDONE = "3915bc7deb83c80b8f5b8dbbdf26df6b1c6d774c"
UNDOING = "f8330dc129a59fa33c78106c1755cdca31e1a946"
# The paths as those commits named them, before the hidden cores took their prefix.
UNDONE_FILES = [
    "docs/code-review-operations.md",
    "skills/code-review-core/scripts/review_github.py",
    "skills/code-review-core/scripts/test_review_core.py",
    "skills/review-document/scripts/review_document.py",
    "skills/review-document/scripts/test_review_document.py",
]


def git_not_on_path() -> contextlib.AbstractContextManager[object]:
    """PATH and the link folders name a folder that does not exist, so the real locator finds no git and the real
    client raises its own failure."""
    nowhere = str(Path(__file__).resolve().parent / "no git here")
    return mock.patch.dict(os.environ, {"PATH": nowhere, "LOCALAPPDATA": nowhere})


def timed_out(command: list[str], timeout: float) -> GitResult:
    """The real runner's failure when git does not finish within the client's time limit."""
    raise GitError(f"{' '.join(command)} did not finish within {timeout:g} seconds", kind="timeout")


class SilentRevertFixture(unittest.TestCase):
    """A throwaway repository whose main squash-merges pull requests, and a branch off it."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / "repository with spaces"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")

    def git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.root), *arguments], check=True, capture_output=True, encoding="utf-8"
        )
        return completed.stdout.strip()

    def commit(self, message: str, files: Mapping[str, str | None]) -> str:
        """Commit files, a None deleting one, and return the commit."""
        for name, text in files.items():
            path = self.root / name
            if text is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(text.encode())
        self.git("add", "-A")
        message_file = self.home / "message.txt"
        message_file.write_bytes(message.encode())
        self.git("commit", "-q", "--allow-empty", "-F", str(message_file))
        return self.git("rev-parse", "HEAD")

    def publish(self) -> None:
        """Point origin/main at main, as a fetched clone has it."""
        self.git("update-ref", "refs/remotes/origin/main", "refs/heads/main")

    def history(self) -> str:
        """Main with a first commit and a pull request that changed a.txt; returns the pull request's commit."""
        self.commit("start (#1)", {"a.txt": "one\n", "b.txt": "bee\n"})
        merged = self.commit("feat: change a (#11)", {"a.txt": "two\n"})
        self.publish()
        self.git("checkout", "-q", "-b", "feat/branch")
        return merged

    def declaring(self, path: str, commit: str) -> str:
        return f"Closes #7\n\nWhy.\n\n## Changes\n\n- Reverts `{path}` to its state before {commit[:7]}.\n"

    def test_a_silent_revert_fails_naming_the_file_the_commit_and_its_pull_request(self) -> None:
        merged = self.history()
        self.commit("fix: unrelated, with a stale a.txt", {"a.txt": "one\n", "b.txt": "bees\n"})
        problems = silent_revert_problems(self.root, "Closes #7\n")
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith(f"a.txt: the pull request returns it to its state before {merged[:12]}"))
        self.assertIn("(pull request #11: feat: change a (#11))", problems[0])
        self.assertIn(f"`Reverts `a.txt` to its state before {merged[:12]}`", problems[0])

    def test_a_declared_revert_passes(self) -> None:
        merged = self.history()
        self.commit("revert: a", {"a.txt": "one\n"})
        self.assertEqual([], silent_revert_problems(self.root, self.declaring("a.txt", merged)))
        self.assertEqual([], silent_revert_problems(self.root, f"Reverts `a.txt` to its state before {merged.upper()}"))

    def test_a_declaration_in_a_comment_or_of_another_file_or_commit_does_not_count(self) -> None:
        merged = self.history()
        start = self.git("rev-parse", "main~1")
        self.commit("revert: a", {"a.txt": "one\n"})
        for body in (
            f"<!-- {self.declaring('a.txt', merged)} -->",
            self.declaring("b.txt", merged),
            self.declaring("a.txt", start),
            f"This reverts `a.txt` to its state before {merged[:7]}.",
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    ["a.txt"], [problem.split(":", 1)[0] for problem in silent_revert_problems(self.root, body)]
                )

    def test_an_unrelated_change_a_new_file_and_a_deletion_pass(self) -> None:
        self.history()
        self.commit("feat: more", {"b.txt": "bees\n", "c.txt": "new\n"})
        self.assertEqual([], silent_revert_problems(self.root, ""))
        self.commit("chore: drop a", {"a.txt": None})
        self.assertEqual([], silent_revert_problems(self.root, ""))

    def test_a_branch_left_behind_that_merges_main_in_is_judged_by_its_content(self) -> None:
        self.commit("start (#1)", {"a.txt": "one\n", "b.txt": "bee\n"})
        self.git("checkout", "-q", "-b", "feat/branch")
        self.commit("feat: b", {"b.txt": "bees\n"})
        self.git("checkout", "-q", "main")
        self.commit("feat: change a (#11)", {"a.txt": "two\n"})
        self.publish()
        self.git("checkout", "-q", "feat/branch")
        # Behind main, the branch never touched a.txt, so its diff from the merge base leaves a.txt alone.
        self.assertEqual([], silent_revert_problems(self.root, ""))
        self.git("merge", "-q", "--no-edit", "origin/main")
        self.assertEqual([], silent_revert_problems(self.root, ""))

    def test_a_merge_whose_conflict_took_the_branchs_stale_side_fails(self) -> None:
        # Continuous integration checks out the merge of the pull request into main, so main's tip is the merge base.
        self.commit("start (#1)", {"a.txt": "one\n", "b.txt": "bee\n"})
        self.git("checkout", "-q", "-b", "feat/branch")
        self.commit("feat: a and b", {"a.txt": "one\nmore\n", "b.txt": "bees\n"})
        self.git("checkout", "-q", "main")
        merged = self.commit("feat: change a (#11)", {"a.txt": "two\n"})
        self.publish()
        self.git("checkout", "-q", "--detach", "main")
        subprocess.run(["git", "-C", str(self.root), "merge", "-q", "feat/branch"], capture_output=True, check=False)
        self.commit("Merge feat/branch into main", {"a.txt": "one\n"})
        problems = silent_revert_problems(self.root, "")
        self.assertEqual(["a.txt"], [problem.split(":", 1)[0] for problem in problems])
        self.assertIn(merged[:12], problems[0])

    def test_a_file_main_deleted_brought_back_as_it_was_is_a_revert(self) -> None:
        self.commit("start (#1)", {"a.txt": "one\n", "b.txt": "bee\n"})
        removed = self.commit("chore: drop a (#12)", {"a.txt": None})
        self.publish()
        self.git("checkout", "-q", "-b", "feat/branch")
        self.commit("feat: b, with a stale a.txt", {"a.txt": "one\n", "b.txt": "bees\n"})
        self.assertEqual([Revert("a.txt", removed, "chore: drop a (#12)")], find_reverts(self.root))

    def test_an_added_file_matching_a_version_main_changed_before_deleting_it_names_that_change(self) -> None:
        self.commit("start (#1)", {"a.txt": "one\n", "b.txt": "bee\n"})
        changed = self.commit("feat: change a (#11)", {"a.txt": "two\n"})
        self.commit("chore: drop a (#12)", {"a.txt": None})
        self.publish()
        self.git("checkout", "-q", "-b", "feat/branch")
        self.commit("feat: b, with the first a.txt", {"a.txt": "one\n", "b.txt": "bees\n"})
        self.assertEqual([Revert("a.txt", changed, "feat: change a (#11)")], find_reverts(self.root))

    def test_the_newest_state_the_file_returns_to_is_named(self) -> None:
        self.commit("start (#1)", {"a.txt": "one\n"})
        self.commit("feat: two (#2)", {"a.txt": "two\n"})
        third = self.commit("feat: three (#3)", {"a.txt": "three\n"})
        fourth = self.commit("feat: two again (#4)", {"a.txt": "two\n"})
        self.publish()
        self.git("checkout", "-q", "-b", "feat/branch")
        self.commit("feat: three", {"a.txt": "three\n"})
        self.assertEqual([Revert("a.txt", fourth, "feat: two again (#4)")], find_reverts(self.root))
        self.commit("feat: one", {"a.txt": "two\n"})
        self.assertEqual([], find_reverts(self.root))
        self.assertNotEqual(third, fourth)

    def test_a_state_older_than_the_window_is_not_named(self) -> None:
        self.commit("start (#1)", {"a.txt": "one\n"})
        self.commit("feat: two (#2)", {"a.txt": "two\n"})
        self.commit("feat: unrelated (#3)", {"b.txt": "bee\n"})
        self.publish()
        self.git("checkout", "-q", "-b", "feat/branch")
        self.commit("fix: a", {"a.txt": "one\n"})
        self.assertEqual(["a.txt"], [revert.path for revert in find_reverts(self.root)])
        with mock.patch.object(silent_reverts, "COMMITS_BEFORE_MERGE_BASE", 1):
            self.assertEqual([], find_reverts(self.root))

    def test_without_a_body_the_branchs_commit_messages_declare_and_with_one_they_do_not(self) -> None:
        merged = self.history()
        self.commit(f"revert: a\n\n{self.declaring('a.txt', merged)}", {"a.txt": "one\n"})
        self.assertEqual([], silent_revert_problems(self.root))
        self.assertEqual(["a.txt"], [problem.split(":", 1)[0] for problem in silent_revert_problems(self.root, "")])

    def test_a_subject_holding_the_old_separators_or_an_entrys_shape_names_its_commit(self) -> None:
        # The bytes an earlier reader split commits and subjects on, and a subject that reads as a --raw entry.
        for index, subject in enumerate(
            ("feat: \x01 change a (#11)", "feat: \x02 change a (#11)", ":100644 100644 a b M (#11)")
        ):
            with self.subTest(subject=repr(subject)):
                self.root = self.home / f"repository {index}"
                self.root.mkdir()
                self.git("init", "-q", "-b", "main")
                self.commit("start (#1)", {"a.txt": "one\n", "b.txt": "bee\n"})
                merged = self.commit(subject, {"a.txt": "two\n"})
                self.publish()
                self.git("checkout", "-q", "-b", "feat/branch")
                self.commit("fix: unrelated, with a stale a.txt", {"a.txt": "one\n", "b.txt": "bees\n"})
                [problem] = silent_revert_problems(self.root, "Closes #7\n")
                self.assertIn(f"before {merged[:12]} (pull request #11: {subject})", problem)

    def test_a_git_that_cannot_run_or_finish_is_a_problem_line(self) -> None:
        # The real helper's git missing, or out of time: before and after the check finds origin/main.
        self.history()
        self.commit("fix: unrelated, with a stale a.txt", {"a.txt": "one\n"})
        for failing, reason in (
            (git_not_on_path(), MISSING_GIT),
            (mock.patch.object(release_notes.CLIENT, "runner", timed_out), "did not finish within "),
            (
                mock.patch.object(silent_reverts, "find_reverts", side_effect=ReleaseNotesError("no history")),
                "no history",
            ),
        ):
            with self.subTest(reason=reason), failing:
                [problem] = silent_revert_problems(self.root, "")
                self.assertTrue(problem.startswith("the silent-revert check cannot read the history: "), problem)
                self.assertIn(reason, problem)

    def test_without_origin_main_the_check_says_how_to_fetch_it(self) -> None:
        self.commit("start (#1)", {"a.txt": "one\n"})
        problems = silent_revert_problems(self.root, "")
        self.assertEqual(1, len(problems))
        self.assertIn("`git fetch origin main`", problems[0])

    def run_with_body(self, body: str | None) -> tuple[int, str]:
        body_file = self.home / "pull request body.md"
        if body is not None:
            body_file.write_text(body, encoding="utf-8")
        output = io.StringIO()
        with (
            mock.patch.object(silent_reverts, "REPOSITORY_ROOT", self.root),
            mock.patch.object(run_validation, "tool_versions", return_value={}),
            mock.patch.object(run_validation, "report_prerequisite_problems", return_value=[]),
            mock.patch.object(run_validation, "all_jobs", return_value=[]),
            mock.patch.object(run_validation, "append_step_summary"),
            contextlib.redirect_stdout(output),
        ):
            arguments = ["-k", "*SilentRevertPolicies.*"]
            if body is not None:
                arguments += ["--pr-body", str(body_file)]
            status = run_validation.main(arguments)
        return status, output.getvalue()

    def test_the_runner_reads_the_declaration_from_the_body_file(self) -> None:
        merged = self.history()
        self.commit("revert: a", {"a.txt": "one\n"})
        status, output = self.run_with_body("Closes #7\n")
        self.assertEqual(1, status, output)
        self.assertIn(f"a.txt: the pull request returns it to its state before {merged[:12]}", output)
        status, output = self.run_with_body(self.declaring("a.txt", merged))
        self.assertEqual(0, status, output)
        self.assertIn("1 policy checks and 0 suite jobs", output)


class SilentRevertDeclarations(unittest.TestCase):
    def test_a_declaration_is_a_line_of_its_own_or_a_list_item_outside_comments(self) -> None:
        body = (
            "Reverts `a b.txt` to its state before ABCDEF1.\r\n"
            "* Reverts `c.txt` to its state before 1234567890\n"
            "<!-- Reverts `d.txt` to its state before 1234567 -->\n"
            "It reverts `e.txt` to its state before 1234567.\n"
            "Reverts `f.txt` to its state before 123456.\n"
        )
        self.assertEqual({("a b.txt", "abcdef1"), ("c.txt", "1234567890")}, declarations(body))


class AuditReplay(unittest.TestCase):
    def test_the_pull_request_that_undid_the_one_before_it_is_named_when_replayed(self) -> None:
        # Replayed in this repository's own history: the squash's tree against main as it stood before it merged.
        reverts = find_reverts(REPOSITORY_ROOT, UNDOING, f"{UNDOING}^")
        self.assertEqual(UNDONE_FILES, [revert.path for revert in reverts])
        self.assertEqual({(UNDONE, "#1220")}, {(revert.commit, revert.pull_request) for revert in reverts})

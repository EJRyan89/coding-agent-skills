"""Regression suite for tools/worktrees.py: worktree creation, the fleet survey, and the git it and the guard run."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "validation"))

import git_client
import git_worktrees
import skill_scripts
from worktree_fixture import (
    GUARD_SCRIPT,
    HUB,
    REPOSITORY_ROOT,
    TEMPLATE_DIRECTORY,
    TREE,
    WORKTREES_SCRIPT,
    HubTestCase,
    build_hub,
    copy_hub,
    isolated_environment,
    run_git,
    run_script,
)

from tools import worktrees


class WorktreesTestCase(HubTestCase):
    script = WORKTREES_SCRIPT


class FixtureTemplateTests(unittest.TestCase):
    def test_a_copied_hub_matches_a_freshly_built_one_and_its_worktree_is_its_own(self) -> None:
        def shape(root: Path, environment: dict[str, str]) -> dict[str, str]:
            hub, tree = root / HUB, root / HUB / TREE

            def output(cwd: Path, *arguments: str) -> str:
                result = run_git(cwd, environment, *arguments)
                self.assertEqual(0, result.returncode, result.stderr)
                return result.stdout.replace(root.as_posix(), "<root>").replace(str(root), "<root>")

            return {
                "worktrees": output(hub, "worktree", "list", "--porcelain").replace(
                    output(hub, "rev-parse", "HEAD").strip(), "<head>"
                ),
                "refs": output(hub, "for-each-ref", "--format=%(refname) %(tree) %(subject)"),
                "config": output(hub, "config", "--local", "--list"),
                "tree common directory": output(tree, "rev-parse", "--path-format=absolute", "--git-common-dir"),
                "tree status": output(tree, "status", "--porcelain", "--branch"),
                "hub status": output(hub, "status", "--porcelain", "--branch"),
            }

        with tempfile.TemporaryDirectory() as fresh_directory, tempfile.TemporaryDirectory() as copy_directory:
            fresh, copied = Path(fresh_directory).resolve(), Path(copy_directory).resolve()
            fresh_environment, copied_environment = isolated_environment(), isolated_environment()
            build_hub(fresh / HUB, fresh_environment)
            copy_hub(copied / HUB, copied_environment)
            copied_shape = shape(copied, copied_environment)
            self.assertEqual(shape(fresh, fresh_environment), copied_shape)
            self.assertEqual("<root>/Repo With Spaces/.git\n", copied_shape["tree common directory"])
            template = Path(TEMPLATE_DIRECTORY.name).resolve()
            for record in (copied / HUB / TREE / ".git", *(copied / HUB / ".git" / "worktrees").rglob("gitdir")):
                text = record.read_text(encoding="utf-8")
                self.assertNotIn(template.as_posix(), text)
                self.assertIn(copied.as_posix(), text)


class NewWorktreeTests(WorktreesTestCase):
    def test_new_creates_a_nested_worktree_on_a_branch_from_main_without_upstream(self) -> None:
        self.assertTrue((self.tree / "README.md").is_file())
        self.assertEqual("feat/demo", self.git(self.tree, "branch", "--show-current").strip())
        self.assertEqual(self.git(self.hub, "rev-parse", "main"), self.git(self.tree, "rev-parse", "HEAD"))
        upstream = subprocess.run(
            ["git", "-C", str(self.tree), "rev-parse", "--abbrev-ref", "@{upstream}"],
            capture_output=True,
            env=self.environment,
            check=False,
        )
        self.assertNotEqual(0, upstream.returncode)

    def test_new_from_inside_a_worktree_still_creates_it_in_the_hub(self) -> None:
        result = self.run_script(["new", "fix", "second"], cwd=self.tree)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((self.worktrees / "fix-second" / "README.md").is_file())

    def test_new_rejects_unsafe_names_and_existing_branches_or_paths(self) -> None:
        (self.worktrees / "fix-taken").mkdir()
        self.git(self.hub, "branch", "inv/existing")
        for arguments, message in (
            (["Feat", "x"], "must be one of"),
            (["feat", "a/b"], "lowercase"),
            (["feat", "-x"], "lowercase"),
            (["feat", "demo"], "already exists"),
            (["inv", "existing"], "already exists"),
            (["fix", "taken"], "already exists"),
            # A kind outside the title types escapes the title check's agreement rule, so new refuses it.
            (["fixx", "thing"], "must be one of feat, fix, docs, test, refactor, chore, ci, build, inv"),
            (["feature", "thing"], "must be one of"),
            (["bugfix", "thing"], "must be one of"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_script(["new", "--", *arguments], cwd=self.hub)
                self.assertEqual(1, result.returncode)
                self.assertIn(message, result.stderr)

    def test_new_accepts_every_title_type_and_inv_as_the_kind(self) -> None:
        # The kinds pinned as literals, independently of the constant new reads.
        for kind in ("feat", "fix", "docs", "test", "refactor", "chore", "ci", "build", "inv"):
            with self.subTest(kind=kind):
                result = self.run_script(["new", kind, "accepted"], cwd=self.hub)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(
                    f"{kind}/accepted",
                    self.git(self.worktrees / f"{kind}-accepted", "branch", "--show-current").strip(),
                )
        refused = self.run_script(["new", "fixx", "thing"], cwd=self.hub)
        self.assertEqual(1, refused.returncode)
        self.assertFalse((self.worktrees / "fixx-thing").exists())
        self.assertNotEqual(
            0,
            subprocess.run(
                ["git", "-C", str(self.hub), "show-ref", "--verify", "--quiet", "refs/heads/fixx/thing"],
                env=self.environment,
                check=False,
            ).returncode,
        )

    def test_new_refuses_while_the_hub_does_not_ignore_its_worktrees(self) -> None:
        (self.hub / ".gitignore").write_text("local.json\n", encoding="utf-8")
        result = self.run_script(["new", "fix", "unignored"], cwd=self.hub)
        self.assertEqual(1, result.returncode)
        self.assertIn(".claude/worktrees/ must be ignored", result.stderr)
        self.assertFalse((self.worktrees / "fix-unignored").exists())


class ListTests(WorktreesTestCase):
    def rows(self, environment: dict[str, str] | None = None, cwd: Path | None = None) -> dict[str, list[str]]:
        result = self.run_script(["list"], cwd=cwd or self.tree, environment=environment)
        self.assertEqual(0, result.returncode, result.stderr)
        lines = result.stdout.splitlines()
        self.assertTrue(lines[0].startswith("BRANCH"))
        return {line.split()[0]: [cell.strip() for cell in line.split("  ") if cell.strip()] for line in lines[1:]}

    def test_list_reports_each_worktree_state(self) -> None:
        rows = self.rows()
        self.assertEqual(["main", "hub", "0", "0", "0"], rows["main"][:5])
        self.assertEqual(["feat/demo", "no commits", "0", "0", "0"], rows["feat/demo"][:5])
        (self.tree / "work.txt").write_text("work\n", encoding="utf-8")
        self.git(self.tree, "add", "work.txt")
        self.git(self.tree, "commit", "-q", "-m", "work")
        self.assertEqual(["feat/demo", "in progress", "1", "0", "0"], self.rows()["feat/demo"][:5])

    def test_list_flags_a_hub_that_is_off_main_or_dirty(self) -> None:
        self.git(self.hub, "switch", "-q", "-c", "stray")
        (self.hub / "README.md").write_text("edited in the hub\n", encoding="utf-8")
        self.assertEqual(["stray", "hub · off main · dirty", "0", "0", "1"], self.rows()["stray"][:5])

    def test_list_counts_untracked_files_whatever_status_shows_and_a_rename_once(self) -> None:
        self.git(self.tree, "config", "status.showUntrackedFiles", "no")
        (self.tree / "untracked.txt").write_text("new\n", encoding="utf-8")
        self.git(self.tree, "mv", "README.md", "MOVED.md")
        self.assertEqual(["feat/demo", "no commits", "0", "0", "2"], self.rows()["feat/demo"][:5])

    def test_list_reads_each_checkouts_own_index_whatever_index_the_shell_names(self) -> None:
        empty_index = self.root / "an empty index"
        rows = self.rows({**self.environment, "GIT_INDEX_FILE": str(empty_index)})
        self.assertEqual(["main", "hub", "0", "0", "0"], rows["main"][:5])
        self.assertEqual(["feat/demo", "no commits", "0", "0", "0"], rows["feat/demo"][:5])

    def test_list_never_writes_a_checkouts_index(self) -> None:
        # A file whose timestamp moved and whose content did not: a status with optional locks refreshes the index
        # and writes it back under its lock, which a session committing in that worktree then finds held.
        readme = self.tree / "README.md"
        moved = readme.stat().st_mtime + 5
        os.utime(readme, (moved, moved))
        index = Path(self.git(self.tree, "rev-parse", "--path-format=absolute", "--git-path", "index").strip())
        before = index.read_bytes()
        self.assertEqual(["feat/demo", "no commits", "0", "0", "0"], self.rows()["feat/demo"][:5])
        self.assertEqual(before, index.read_bytes())

    def test_list_marks_a_checkout_git_cannot_read_rather_than_counting_it_clean(self) -> None:
        # Rewritten in place: git for Windows hides the file, and Windows refuses to create a hidden file anew.
        with (self.tree / ".git").open("r+", encoding="utf-8") as dot_git:
            dot_git.write(f"gitdir: {self.root / 'gone'}\n")
            dot_git.truncate()
        self.assertEqual(["feat/demo", "unreadable", "0", "0", "-"], self.rows(cwd=self.hub)["feat/demo"][:5])

    def test_list_shows_a_count_git_cannot_take_as_unknown_never_in_progress(self) -> None:
        # With no main to count against, git answers nonzero for every worktree's ahead and behind.
        self.git(self.hub, "branch", "-m", "main", "trunk")
        rows = self.rows()
        self.assertEqual(["trunk", "hub · off main", "?", "?", "0"], rows["trunk"][:5])
        self.assertEqual(["feat/demo", "unknown", "?", "?", "0"], rows["feat/demo"][:5])


class CheckoutTests(WorktreesTestCase):
    def test_a_checkout_git_cannot_read_raises_and_a_place_outside_every_checkout_is_none(self) -> None:
        self.assertEqual(worktrees.Checkout(self.tree, self.hub), worktrees.checkout_of(self.tree / "README.md"))
        outside = self.root / "outside"
        outside.mkdir()
        self.assertIsNone(worktrees.checkout_of(outside))
        # Inside a git directory there is no work tree to show, and that is no failure either.
        self.assertIsNone(worktrees.checkout_of(self.hub / ".git" / "refs"))
        # Rewritten in place: git for Windows hides the file, and Windows refuses to create a hidden file anew.
        with (self.tree / ".git").open("r+", encoding="utf-8") as dot_git:
            dot_git.write(f"gitdir: {self.root / 'gone'}\n")
            dot_git.truncate()
        with self.assertRaises(git_client.GitError) as raised:
            worktrees.checkout_of(self.tree)
        self.assertIn(f"git could not read the checkout at {self.tree}", str(raised.exception))


class CheckoutFailureTests(unittest.TestCase):
    def test_a_checkout_git_fails_on_names_git_s_message_or_its_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            checkout = Path(folder) / "checkout"
            (checkout / ".git").mkdir(parents=True)
            for stderr, said in (("fatal: one\n  two\n", "fatal: one two"), ("", "git rev-parse exited with 128")):
                with (
                    self.subTest(stderr=stderr),
                    mock.patch.object(worktrees, "git", return_value=git_client.GitResult(128, "", stderr)),
                    self.assertRaises(git_client.GitError) as raised,
                ):
                    worktrees.checkout_of(checkout)
                self.assertEqual(f"git could not read the checkout at {checkout}: {said}", str(raised.exception))
                self.assertEqual(128, raised.exception.returncode)

    def counted_row(self, head: str, answer: git_client.GitResult) -> tuple[worktrees.Row, list[object]]:
        """The row list shows for a worktree whose count git answers with answer, and each git call it made."""
        with tempfile.TemporaryDirectory() as folder:
            hub, tree = Path(folder) / "hub", Path(folder) / "tree"
            tree.mkdir()
            calls: list[object] = []

            def git(cwd: Path, *arguments: str, timeout: float | None = 10.0) -> git_client.GitResult:
                calls.append((cwd == hub, arguments, timeout))
                return answer

            with (
                mock.patch.object(worktrees, "git", git),
                mock.patch.object(git_worktrees, "changed_paths", return_value=0),
            ):
                return worktrees.row(hub, git_worktrees.Worktree(str(tree), head, "feat/x", False)), calls

    def test_list_counts_each_worktree_from_the_hub_under_the_client_s_own_limit(self) -> None:
        row, calls = self.counted_row("abc", git_client.GitResult(0, "2\t3\n", ""))
        self.assertEqual([(True, ("rev-list", "--left-right", "--count", "main...abc"), None)], calls)
        self.assertEqual(("in progress", "3", "2"), (row.state, row.ahead, row.behind))
        # A worktree with no commit of its own recorded is counted from its HEAD.
        _, calls = self.counted_row("", git_client.GitResult(0, "0\t0\n", ""))
        self.assertEqual([(True, ("rev-list", "--left-right", "--count", "main...HEAD"), None)], calls)

    def test_list_shows_any_answer_that_is_not_two_counts_as_unknown(self) -> None:
        for answer in (
            git_client.GitResult(0, "garbage\n", ""),
            git_client.GitResult(0, "1\tx\n", ""),
            git_client.GitResult(0, "", ""),
            git_client.GitResult(128, "2\t3\n", "fatal: bad revision"),
        ):
            with self.subTest(answer=answer):
                row, _ = self.counted_row("abc", answer)
                self.assertEqual(("unknown", "?", "?"), (row.state, row.ahead, row.behind))


class GitProgramTests(unittest.TestCase):
    def runner(self, output: str = "") -> tuple[list[tuple[list[str], float]], Any]:
        """A runner for worktrees.CLIENT that records each command and its time limit and answers with output."""
        calls: list[tuple[list[str], float]] = []

        def run(command: Sequence[str], timeout: float) -> git_client.GitResult:
            calls.append((list(command), timeout))
            return git_client.GitResult(0, output, "")

        return calls, run

    def test_git_runs_through_skill_cores_client_on_the_checkout(self) -> None:
        # The client's runner starts git at the path the locator finds, never by name, and without the
        # repository-selecting variables; ejryan89-skill-core's suites hold both.
        self.assertIs(git_client.subprocess_runner, worktrees.CLIENT.runner)
        calls, run = self.runner("out")
        with mock.patch.object(worktrees.CLIENT, "runner", run):
            result = worktrees.git(Path("a checkout"), "status")
        self.assertEqual([(["git", "-C", "a checkout", "status"], worktrees.GUARD_GIT_TIMEOUT_SECONDS)], calls)
        self.assertEqual((0, "out"), (result.returncode, result.stdout))

    def test_a_git_that_cannot_run_or_finish_is_a_git_error(self) -> None:
        def missing(command: Sequence[str], timeout: float) -> git_client.GitResult:
            raise git_client.GitError(git_client.MISSING_GIT, kind="prerequisite")

        with (
            mock.patch.object(worktrees.CLIENT, "runner", missing),
            self.assertRaisesRegex(git_client.GitError, "Git executable 'git' was not found"),
        ):
            worktrees.git(Path("a checkout"), "status")


class ToolsGitTests(unittest.TestCase):
    def test_every_tool_runs_git_and_gh_only_through_skill_cores_clients(self) -> None:
        # The policy validation applies to skill scripts, applied to tools/: a command a tool starts itself has no
        # time limit, no prompt turned off, and its own copy of the environment rule.
        found = [
            f"{path.relative_to(REPOSITORY_ROOT).as_posix()}:{line} runs {command} itself"
            for path in sorted((REPOSITORY_ROOT / "tools").glob("*.py"))
            for reader in [skill_scripts.command_reader(path)]
            for line, command in sorted(
                {found for node in ast.walk(reader.tree) for found in skill_scripts._client_commands(node, reader)}
            )
        ]
        self.assertEqual([], found)


class GuardSubcommandTests(WorktreesTestCase):
    """A Claude Code session keeps the hook command it loaded when it started, which ran this script's guard subcommand
    before the hub guard moved into tools/hub_guard.py; the subcommand runs the guard as its own program."""

    def test_the_guard_subcommand_answers_as_the_hub_guard_does(self) -> None:
        def event(tool: str, key: str, value: str, cwd: Path) -> str:
            return json.dumps({"tool_name": tool, "tool_input": {key: value}, "cwd": str(cwd)})

        inherited = {**self.environment, "GIT_DIR": str(self.hub / ".git")}
        cases = (
            (event("Edit", "file_path", str(self.hub / "README.md"), self.root), self.environment, "deny"),
            (event("Edit", "file_path", str(self.tree / "README.md"), self.root), self.environment, None),
            # The guard reads the location variables of the environment the hook was given.
            (event("Bash", "command", "git reset --hard", self.tree), inherited, "deny"),
            (event("Bash", "command", "git reset --hard", self.tree), self.environment, None),
            ("{not json", self.environment, None),
        )
        errors: list[str] = []
        for stdin, environment, expected in cases:
            with self.subTest(stdin=stdin, inherited="GIT_DIR" in environment):
                moved = run_script(GUARD_SCRIPT, [], self.root, environment, stdin)
                forwarded = self.run_script(["guard"], cwd=self.root, stdin=stdin, environment=environment)
                errors.append(forwarded.stderr)
                self.assertEqual(
                    (0, moved.stdout, moved.stderr), (forwarded.returncode, forwarded.stdout, forwarded.stderr)
                )
                decision = json.loads(forwarded.stdout)["hookSpecificOutput"] if forwarded.stdout.strip() else None
                self.assertEqual(expected, None if decision is None else decision["permissionDecision"])
        self.assertIn("hub guard skipped", errors[-1])

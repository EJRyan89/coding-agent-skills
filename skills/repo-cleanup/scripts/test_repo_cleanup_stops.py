"""What a cleanup reports when a step stopped, Git refused one, or a branch was never offered: each is reported as
itself, never as a completed cleanup, and no command deletes what the plan's offer did not name. On the
repositories test_repo_cleanup.py's Fixture builds."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import git_client
import repo_cleanup as rc
from git_client import GitClient, GitError, GitResult
from test_repo_cleanup import GIT_CONFIG, REMOTE_URL, SweepFixture, build_template, facts, git, refusing_branch_deletion

# The settings conftest.py adds to the global configuration of every home this suite's git runs in.
git_config = GIT_CONFIG


def setUpModule() -> None:
    """Build the repositories every Fixture test starts from, with Git isolated as each test isolates it."""
    build_template()


class StopAndRefusalTests(SweepFixture):
    """A run that stopped or that Git refused is reported as itself, and only an offered branch is deleted."""

    def test_a_branch_that_moves_as_git_refuses_its_deletion_is_kept_as_moved(self) -> None:
        # git branch -d refuses a branch whose tip moved to a commit the default branch lacks; the moved branch is
        # kept, as a branch that moved before the deletion is, so nothing of the user's is lost.
        self.finished_branch("done")

        def moving(command: Sequence[str], timeout: float) -> GitResult:
            if "branch" in command and "-d" in command and "done" in command:
                moved = git(self.clone, "commit-tree", "done^{tree}", "-p", "done", "-m", "new work")
                git(self.clone, "update-ref", "refs/heads/done", moved)
                return GitResult(1, "", "error: the branch 'done' is not fully merged\n")
            return git_client.subprocess_runner(command, timeout)

        code, lines = self.sweep(rc.Services(git=GitClient(moving), gh=self.github))
        self.assertEqual(0, code, lines)
        self.assertIn("  Preserved:               1 — done: moved", self.summary(lines))
        self.assertIsNotNone(self.tip("done"), "the moved branch is kept")
        _, reported = self.report()
        self.assertEqual(
            [["Processed 1 repository; skipped: 0 fetch failed, 0 aborted, 0 with an error."]],
            facts(reported, "PROCESSED"),
        )

    def test_a_branch_a_stopped_apply_never_offered_is_refused_and_the_stop_outlasts_later_commands(self) -> None:
        git(self.clone, "branch", "scratch", "main")
        self.push_branch("behind")
        self.advance_remotely("behind")

        def failing(command: Sequence[str], timeout: float) -> GitResult:
            if "update-ref" in command and "refs/heads/behind" in command:
                raise GitError("git vanished", kind="execution")
            return git_client.subprocess_runner(command, timeout)

        _, swept = self.sweep(rc.Services(git=GitClient(failing), gh=self.github), "my repo")
        self.assertEqual([], facts(swept, "CONFIRM_LOCAL"), "the apply stopped before its offers ran")
        plan = (self.root / "plans" / "my repo.json").as_posix()
        stopped = "my repo cleanup stopped partway (could not run git: git vanished); done before it:"
        code, lines = self.report()
        self.assertEqual(0, code, lines)
        self.assertEqual([[f"{self.repos.as_posix()}/my repo", "git-failed"]], facts(lines, "REPO"))
        self.assertEqual(stopped, self.summary(lines).splitlines()[0])
        self.assertEqual([], facts(lines, "ERROR"), "the summary's heading names the failure")
        self.assertEqual(
            [["Processed 1 repository; skipped: 0 fetch failed, 0 aborted, 1 with an error."]],
            facts(lines, "PROCESSED"),
        )

        # The plan planned to ask about scratch, but the offer never named it, so no confirmation may delete it.
        code, lines = self.invoke("delete-local", "--plan", plan, "--branch", "scratch")
        self.assertEqual(1, code, lines)
        self.assertEqual([], facts(lines, "DELETED"), lines)
        self.assertEqual(
            [["scratch", "not a local-only or closed branch offered by this plan"]], facts(lines, "PRESERVED")
        )
        self.assertIsNotNone(self.tip("scratch"))
        self.assertEqual(stopped, self.summary(lines).splitlines()[0], "a later run never clears the apply's stop")
        recorded = json.loads(Path(plan).read_text(encoding="utf-8"))
        self.assertEqual("could not run git: git vanished", recorded["stopped"])
        code, reported = self.report()
        self.assertEqual(0, code, reported)
        self.assertEqual("POLICY", reported[0].split("\t")[0], "the lists the sweep used come first")
        summary = [line for line in lines if line.startswith("SUMMARY\t")]
        self.assertEqual(summary, reported[2:-1], "the report shows the last summary printed")
        self.assertEqual(stopped, self.summary(reported).splitlines()[0])
        self.assertEqual(
            [["Processed 1 repository; skipped: 0 fetch failed, 0 aborted, 1 with an error."]],
            facts(reported, "PROCESSED"),
            "the stopped apply stays outstanding",
        )

    def test_a_confirmation_that_stops_is_reported_until_a_later_one_completes(self) -> None:
        git(self.clone, "branch", "scratch", "main")
        self.sweep(None, "my repo")
        plan = (self.root / "plans" / "my repo.json").as_posix()

        def failing(command: Sequence[str], timeout: float) -> GitResult:
            if "branch" in command and "-d" in command:
                raise GitError("git vanished", kind="execution")
            return git_client.subprocess_runner(command, timeout)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = rc.main(
                ["delete-local", "--plan", plan, "--branch", "scratch"],
                rc.Services(git=GitClient(failing), gh=self.github),
            )
        lines = output.getvalue().splitlines()
        self.assertEqual(1, code, lines)
        self.assertEqual(
            "my repo cleanup stopped partway (could not run git: git vanished); done before it:",
            self.summary(lines).splitlines()[0],
        )
        recorded = json.loads(Path(plan).read_text(encoding="utf-8"))
        self.assertEqual("could not run git: git vanished", recorded["confirmation_stopped"])
        self.assertNotIn("stopped", recorded, "the apply completed")
        code, reported = self.report()
        self.assertEqual([[f"{self.repos.as_posix()}/my repo", "cleaned"]], facts(reported, "REPO"))
        self.assertEqual(
            [["Processed 1 repository; skipped: 0 fetch failed, 0 aborted, 1 with an error."]],
            facts(reported, "PROCESSED"),
            "a confirmation that stopped is outstanding until one completes",
        )

        # remove-directory is a confirmation too: one that completes, even refusing a path never offered, clears it.
        code, lines = self.invoke("remove-directory", "--plan", plan, "--path", self.clone.as_posix())
        self.assertEqual(1, code, lines)
        self.assertEqual("my repo cleanup complete:", self.summary(lines).splitlines()[0])
        self.assertNotIn("confirmation_stopped", json.loads(Path(plan).read_text(encoding="utf-8")))

        # A failure with no message is recorded by its type.
        output = io.StringIO()
        with mock.patch.object(rc, "delete_merged", side_effect=ValueError()), contextlib.redirect_stdout(output):
            code = rc.main(["delete-local", "--plan", plan, "--branch", "scratch"], rc.Services(gh=self.github))
        lines = output.getvalue().splitlines()
        self.assertEqual(1, code, lines)
        self.assertEqual(
            "my repo cleanup stopped partway (ValueError); done before it:", self.summary(lines).splitlines()[0]
        )
        self.assertEqual("ValueError", json.loads(Path(plan).read_text(encoding="utf-8"))["confirmation_stopped"])

        code, lines = self.invoke("delete-local", "--plan", plan, "--branch", "scratch")
        self.assertEqual((0, [["scratch"]]), (code, facts(lines, "DELETED")), lines)
        self.assertEqual("my repo cleanup complete:", self.summary(lines).splitlines()[0])
        self.assertTrue({"stopped", "confirmation_stopped"}.isdisjoint(json.loads(Path(plan).read_text("utf-8"))))
        code, reported = self.report()
        self.assertEqual(
            [["Processed 1 repository; skipped: 0 fetch failed, 0 aborted, 0 with an error."]],
            facts(reported, "PROCESSED"),
        )

    def test_a_default_branch_held_by_its_changes_is_reported_with_its_reason_and_is_not_an_error(self) -> None:
        # A designed hold, like a branch whose worktree has changes: nothing was tried, so nothing failed.
        self.commit(self.other, "upstream work")
        git(self.other, "push", "--quiet", "origin", "main")
        (self.clone / "draft.txt").write_text("draft\n", encoding="utf-8")
        code, lines = self.sweep(None, "--skip-checkout", "my repo")
        self.assertEqual(0, code, lines)
        held = f"main (behind 1; worktree {self.clone.as_posix()} has 1 uncommitted change)"
        self.assertIn(f"  Fast-forward skipped:    1 — {held}", self.summary(lines))
        _, reported = self.report()
        self.assertIn(f"  Fast-forward skipped:    1 — {held}", self.summary(reported))
        self.assertEqual(
            [["Processed 1 repository; skipped: 0 fetch failed, 0 aborted, 0 with an error."]],
            facts(reported, "PROCESSED"),
        )

    def test_a_continued_dirty_repository_whose_fetch_fails_is_recorded_fetch_failed_and_the_report_runs(self) -> None:
        (self.clone / "draft.txt").write_text("draft\n", encoding="utf-8")
        code, lines = self.sweep(None, "my repo")
        self.assertEqual([[f"{self.repos.as_posix()}/my repo", "dirty"]], facts(lines, "REPO"))
        git(self.clone, "config", f"url.{(self.root / 'missing remote').as_posix()}.insteadOf", REMOTE_URL)
        git(self.clone, "config", "--unset", f"url.{self.remote.as_posix()}.insteadOf")

        code, lines = self.sweep(None, "--record", str(self.root / "plans"), "--skip-checkout", "my repo")
        self.assertEqual(1, code, lines)
        self.assertEqual([[f"{self.repos.as_posix()}/my repo", "fetch-failed"]], facts(lines, "REPO"))
        self.assertEqual(
            [["1", "cleaned=0", "quiet=0", "dirty=0", "fetch-failed=1", "error=0", "git-failed=0"]],
            facts(lines, "SWEPT"),
        )
        code, lines = self.report()
        self.assertEqual(0, code, lines)
        self.assertEqual([[f"{self.repos.as_posix()}/my repo", "fetch-failed"]], facts(lines, "REPO"))
        self.assertTrue(facts(lines, "SUMMARY")[0][0].startswith("my repo skipped: fetch failed — "), lines)
        self.assertEqual(
            [["Processed 1 repository; skipped: 1 fetch failed, 0 aborted, 0 with an error."]],
            facts(lines, "PROCESSED"),
        )

    def test_no_planned_action_lets_a_deleting_command_act_without_the_offer_the_apply_wrote(self) -> None:
        # Every action the plan can give a branch, and every offer kind: eligibility comes from the offer's events.
        git(self.clone, "branch", "scratch", "main")
        self.clean()
        saved = rc.load_plan(self.plan_file)
        offers = ("CONFIRM_LOCAL", "UNMERGED", "CONFIRM_DETACHED", "ORPHAN")
        actions = ("keep", "delete", "remove-worktree", "remove-worktree-ask", "ask-remove-worktree", "ask-delete")
        commands = {
            "delete-local": ["--branch", "scratch"],
            "force-delete": ["--branch", "scratch"],
            "remove-directory": ["--path", self.clone.as_posix()],
        }
        for action in actions:
            for command, named in commands.items():
                with self.subTest(action=action, command=command):
                    plan = json.loads(json.dumps(saved))
                    plan["events"] = [event for event in plan["events"] if event[0] not in offers]
                    for branch in plan["branches"]:
                        if branch["name"] == "scratch":
                            branch["action"] = action
                    rc.save_plan(self.plan_file, plan)
                    code, lines = self.invoke(command, "--plan", str(self.plan_file), *named)
                    self.assertEqual(1, code, lines)
                    self.assertEqual([], facts(lines, "DELETED") + facts(lines, "DELETED_DIR"), lines)
                    self.assertEqual(1, len(facts(lines, "PRESERVED")), lines)
                    self.assertTrue(facts(lines, "PRESERVED")[0][1].startswith("not "), lines)
                    self.assertIsNotNone(self.tip("scratch"))

    def test_what_git_refuses_a_confirming_command_counts_as_an_error_until_a_later_one_carries_it_out(self) -> None:
        git(self.clone, "branch", "scratch", "main")
        git(self.clone, "switch", "--quiet", "-c", "work", "main")
        self.commit(self.clone, "unpublished work")
        git(self.clone, "switch", "--quiet", "main")
        task = self.clone / ".claude" / "worktrees" / "old task"
        task.mkdir(parents=True)
        (task / "notes.md").write_text("notes\n", encoding="utf-8")
        (self.clone / ".git" / "info").mkdir(exist_ok=True)
        (self.clone / ".git" / "info" / "exclude").write_text(".claude/\n", encoding="utf-8")
        _, swept = self.sweep(None, "my repo")
        plan = (self.root / "plans" / "my repo.json").as_posix()
        orphan = facts(swept, "ORPHAN")[0][0]
        code, lines = self.invoke("delete-local", "--plan", plan, "--branch", "work")
        self.assertEqual((0, [["work"]]), (code, [fields[:1] for fields in facts(lines, "UNMERGED")]), lines)

        def refused(*arguments: str) -> tuple[int, list[str]]:
            output = io.StringIO()
            refusing = refusing_branch_deletion("scratch", "work")
            failing = mock.patch.object(rc, "delete_tree", side_effect=PermissionError("in use"))
            with failing, contextlib.redirect_stdout(output):
                code = rc.main([*arguments, "--plan", plan], rc.Services(git=refusing, gh=self.github))
            return code, output.getvalue().splitlines()

        pending: list[str] = []
        for command, named in (("delete-local", "scratch"), ("force-delete", "work"), ("remove-directory", orphan)):
            with self.subTest(command=command):
                code, lines = refused(command, "--path" if command == "remove-directory" else "--branch", named)
                self.assertEqual(1, code, lines)
                pending.append(named)
                recorded = json.loads(Path(plan).read_text(encoding="utf-8"))
                self.assertEqual(pending, recorded["confirmation_refused"])
                code, reported = self.report()
                self.assertEqual(
                    [["Processed 1 repository; skipped: 0 fetch failed, 0 aborted, 1 with an error."]],
                    facts(reported, "PROCESSED"),
                    "what Git refused is not a completed cleanup",
                )

        # Each retry names first what the plan never offered: the command refuses it and goes on to the next name.
        unoffered = {
            "delete-local": "never-offered",
            "force-delete": "never-offered",
            "remove-directory": self.root.as_posix(),
        }
        for command, named in (("delete-local", "scratch"), ("force-delete", "work"), ("remove-directory", orphan)):
            with self.subTest(retried=command):
                flag = "--path" if command == "remove-directory" else "--branch"
                code, lines = self.invoke(command, "--plan", plan, flag, unoffered[command], flag, named)
                self.assertEqual(1, code, "a name the plan never offered needs the agent")
                self.assertEqual(unoffered[command], facts(lines, "PRESERVED")[0][0], lines)
                gone = facts(lines, "DELETED_DIR" if command == "remove-directory" else "DELETED")
                self.assertEqual([named], [fields[0] for fields in gone], lines)
                pending.remove(named)
                recorded = json.loads(Path(plan).read_text(encoding="utf-8"))
                self.assertEqual(pending, recorded.get("confirmation_refused", []))
        code, reported = self.report()
        self.assertEqual(
            [["Processed 1 repository; skipped: 0 fetch failed, 0 aborted, 0 with an error."]],
            facts(reported, "PROCESSED"),
        )

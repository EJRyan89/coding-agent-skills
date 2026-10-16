from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "skill-core" / "scripts"))

import shepherd_pull_requests as shepherd
from git_client import GitClient
from git_test_base import GitTestCase
from github_client import Backoff, CommandResult, GitHubClient

GIT_CONFIG: Mapping[str, str] = MappingProxyType({"init.defaultBranch": "main"})
REPOSITORY = "owner/repo"
REMOTE_URL = "https://github.com/owner/repo"
HEAD = "a" * 40
OTHER = "b" * 40
RUN = 7001
RULE = "=" * 70
DASHES = "-" * 70
PREFIX = "Windows shard 1/4, Python 3.11\tUNKNOWN STEP\t2026-10-16T05:51:51.7718573Z "
VALIDATION_STEP = "Run repository validation"
# No waits between retries, so a read that fails transiently fails at once.
NO_WAIT = Backoff(base=0.0, transient_base=0.0)


def rollup(state: str = "SUCCESS", run: int = RUN) -> list[dict[str, Any]]:
    """A head's checks: the legs, and the required check once they finished; PENDING has the legs running alone."""
    url = f"https://github.com/{REPOSITORY}/actions/runs/{run}/job/9"
    leg = {"__typename": "CheckRun", "name": "Validation on Linux", "workflowName": "Validation", "detailsUrl": url}
    if state == "PENDING":
        return [{**leg, "status": "IN_PROGRESS", "conclusion": ""}]
    if state == "MISSING":
        return [{"__typename": "CheckRun", "name": "Analyze (python)", "workflowName": "CodeQL", "detailsUrl": url}]
    required = {**leg, "name": "Validation", "status": "COMPLETED", "conclusion": state, "startedAt": "2026-10-16"}
    return [{**leg, "status": "COMPLETED", "conclusion": "SUCCESS"}, required]


def pull(
    number: int,
    *,
    state: str = "SUCCESS",
    merge: str = "CLEAN",
    armed: bool = True,
    draft: bool = False,
    files: Sequence[str] = ("docs/a.md",),
    head: str = HEAD,
    run: int = RUN,
) -> dict[str, Any]:
    return {
        "number": number,
        "headRefName": f"fix/topic-{number}",
        "headRefOid": head,
        "isDraft": draft,
        "mergeStateStatus": merge,
        "autoMergeRequest": {"mergeMethod": "SQUASH"} if armed else None,
        "files": [{"path": path} for path in files],
        "statusCheckRollup": rollup(state, run),
    }


def job(name: str, conclusion: str, failed_step: str | None = VALIDATION_STEP, number: int = 1) -> dict[str, Any]:
    steps = [{"name": "Check out source", "conclusion": "success"}]
    if failed_step:
        steps.append({"name": failed_step, "conclusion": "failure"})
    return {"name": name, "conclusion": conclusion, "databaseId": number, "steps": steps}


def run_view(
    jobs: Sequence[dict[str, Any]], *, head: str = HEAD, attempt: int = 1, conclusion: str = "failure"
) -> dict[str, Any]:
    required = {"name": "Validation", "conclusion": "failure", "databaseId": 99, "steps": []}
    return {
        "headSha": head,
        "attempt": attempt,
        "conclusion": conclusion,
        "status": "completed",
        "jobs": [*jobs, required],
    }


def failed_log(*blocks: tuple[str, Sequence[str]], extra: Sequence[str] = ()) -> str:
    """A leg's --log-failed output: the runner's FAILED blocks and its closing line, each with GitHub's prefixes."""
    lines = ["ok    0.3s tests/tools/test_checkout.py", *extra]
    for label, body in blocks:
        lines += ["", RULE, f"FAILED {label}", DASHES, *body]
    lines += [
        "",
        "Shard 1/4: 25 policy checks and 146 suite jobs in 228s: validation FAILED (1 suite jobs failed).",
        "##[error]Process completed with exit code 1.",
    ]
    return "\n".join(PREFIX + line for line in lines) + "\n"


MYPY_BLOCK = (
    "static type check (mypy skills/review-insights/scripts)",
    [
        "Command failed with exit code 1: mypy.EXE --config-file pyproject.toml .",
        'test_review_guidance.py:298: error: Argument 2 has incompatible type "object"  [arg-type]',
        "",
        "Found 1 error in 1 file (checked 8 source files)",
    ],
)
SILENT_BLOCK = (
    "skills/update-coding-agent-skills/scripts/test_update.sh",
    ["Command failed with exit code 4: bash.exe -c set -euo pipefail; bash test_update.sh", "", ""],
)
TERMINATED_BLOCK = (
    "tests/deployer/test_agents.py [shard 2/3]",
    [
        "Ended before its first line of output on each of 2 attempts, a termination rather than a failed check:",
        "  python.exe was ended with status 0xC0000142 before its first line of output: python.exe -B test_agents.py",
    ],
)
GALLERY_BLOCK = (
    "tests/validation/test_toolchain.py",
    ["Command failed with exit code 1: python -B test_toolchain.py", "Unable to resolve package source 'x'."],
)


class StubGh:
    """gh as the tick calls it: the open and merged listings, each run, each job's failed log, and the writes."""

    def __init__(
        self,
        pulls: Sequence[dict[str, Any]] = (),
        *,
        runs: Mapping[int, dict[str, Any]] | None = None,
        logs: Mapping[int, str] | None = None,
        merged: Sequence[dict[str, Any]] = (),
        failing: Sequence[str] = (),
    ) -> None:
        self.pulls = list(pulls)
        self.runs = dict(runs or {})
        self.logs = dict(logs or {})
        self.merged = list(merged)
        self.failing = tuple(failing)
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> CommandResult:
        self.calls.append(list(command))
        arguments = list(command[1:])
        verb = " ".join(arguments[:2])
        if verb in self.failing:
            return CommandResult(1, "", f"{verb} refused (HTTP 422)")
        if verb == "pr list":
            state = arguments[arguments.index("--state") + 1]
            return CommandResult(0, json.dumps(self.pulls if state == "open" else self.merged), "")
        if verb == "run view" and "--log-failed" in arguments:
            return CommandResult(0, self.logs[int(arguments[arguments.index("--job") + 1])], "")
        if verb == "run view":
            return CommandResult(0, json.dumps(self.runs[int(arguments[2])]), "")
        if verb in ("pr merge", "pr update-branch", "run rerun"):
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected gh call: {command}")

    def writes(self) -> list[list[str]]:
        return [call[1:] for call in self.calls if " ".join(call[1:3]) in ("pr merge", "pr update-branch", "run rerun")]


def git(*arguments: str, directory: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


class FixtureTestCase(GitTestCase):
    """A hub on main whose origin URL names a GitHub repository and fetches from a bare repository beside it."""

    git_config = GIT_CONFIG

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # The long form of the path, which is the one git prints, where Windows gives the temporary folder a short one.
        self.root = Path(os.path.realpath(temporary.name))
        self.origin = self.root / "origin.git"
        self.hub = self.root / "hub"
        subprocess.run(["git", "init", "--bare", "-q", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", str(self.hub)], check=True)
        (self.hub / "README.md").write_text("hub\n", encoding="utf-8")
        git("add", "README.md", directory=self.hub)
        git("commit", "-q", "-m", "first", directory=self.hub)
        git("remote", "add", "origin", REMOTE_URL, directory=self.hub)
        git("config", f"url.{self.origin.as_posix()}.insteadOf", REMOTE_URL, directory=self.hub)
        git("push", "-q", "origin", "main", directory=self.hub)
        git("fetch", "-q", "origin", directory=self.hub)

    def advance_origin(self) -> str:
        """Push a commit to origin's main from a second clone, and return it."""
        clone = self.root / "clone"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(clone)], check=True)
        (clone / "next.md").write_text("next\n", encoding="utf-8")
        git("add", "next.md", directory=clone)
        git("commit", "-q", "-m", "next", directory=clone)
        git("push", "-q", "origin", "main", directory=clone)
        return git("rev-parse", "HEAD", directory=clone)

    def tick(self, gh: StubGh, *, directory: Path | None = None, dry_run: bool = False) -> tuple[int, list[str]]:
        lines: list[str] = []
        client = GitHubClient(gh, backoff=NO_WAIT, sleeper=lambda _: None)
        status = shepherd.tick(GitClient(), client, directory or self.hub, dry_run=dry_run, emit=lines.append)
        return status, lines

    def line(self, lines: Sequence[str], prefix: str) -> str:
        found = [line for line in lines if line.startswith(prefix)]
        self.assertEqual(1, len(found), f"one {prefix!r} line in {lines}")
        return found[0]


class HubTest(FixtureTestCase):
    def test_a_tick_in_the_hub_fast_forwards_main_to_origin(self) -> None:
        before = git("rev-parse", "HEAD", directory=self.hub)
        after = self.advance_origin()
        status, lines = self.tick(StubGh())
        self.assertEqual(0, status)
        self.assertEqual(after, git("rev-parse", "HEAD", directory=self.hub))
        self.assertEqual(
            f'HUB "{self.hub.as_posix()}" fast-forwarded {before[:7]} to {after[:7]}', self.line(lines, "HUB ")
        )
        self.assertEqual(["REPOSITORY owner/repo"], [line for line in lines if line.startswith("REPOSITORY")])
        self.assertEqual("REMAINING 0", lines[-1])

    def test_a_hub_already_at_origin_is_current(self) -> None:
        head = git("rev-parse", "HEAD", directory=self.hub)
        _, lines = self.tick(StubGh())
        self.assertEqual(f'HUB "{self.hub.as_posix()}" current at {head[:7]}', self.line(lines, "HUB "))

    def test_a_dry_run_reads_origin_and_moves_nothing(self) -> None:
        before = git("rev-parse", "HEAD", directory=self.hub)
        tracking = git("rev-parse", "origin/main", directory=self.hub)
        after = self.advance_origin()
        status, lines = self.tick(StubGh(), dry_run=True)
        self.assertEqual(0, status)
        self.assertTrue(lines[0].startswith("DRY_RUN "))
        self.assertEqual(
            f'HUB "{self.hub.as_posix()}" would fast-forward {before[:7]} to {after[:7]}', self.line(lines, "HUB ")
        )
        self.assertEqual(before, git("rev-parse", "HEAD", directory=self.hub))
        self.assertEqual(tracking, git("rev-parse", "origin/main", directory=self.hub))

    def test_a_tick_in_a_linked_worktree_leaves_the_hub_to_its_own_session(self) -> None:
        before = git("rev-parse", "HEAD", directory=self.hub)
        self.advance_origin()
        worktree = self.root / "hub-worktree"
        git("worktree", "add", "-q", "-b", "fix/elsewhere", str(worktree), directory=self.hub)
        status, lines = self.tick(StubGh(), directory=worktree)
        self.assertEqual(0, status)
        self.assertIn("not fast-forwarded: this tick runs in the linked worktree", self.line(lines, "HUB "))
        self.assertIn(f'"{worktree.as_posix()}"', self.line(lines, "HUB "))
        self.assertEqual(before, git("rev-parse", "HEAD", directory=self.hub))

    def test_a_hub_off_main_or_with_a_tracked_change_is_not_fast_forwarded(self) -> None:
        self.advance_origin()
        (self.hub / "README.md").write_text("edited\n", encoding="utf-8")
        _, lines = self.tick(StubGh())
        self.assertTrue(self.line(lines, "HUB ").endswith("not fast-forwarded: it has uncommitted changes"))
        git("checkout", "-q", "README.md", directory=self.hub)
        git("switch", "-q", "-c", "side", directory=self.hub)
        _, lines = self.tick(StubGh())
        self.assertTrue(self.line(lines, "HUB ").endswith("not fast-forwarded: it is on side, not main"))

    def test_a_hub_whose_main_diverged_names_git_s_refusal(self) -> None:
        self.advance_origin()
        (self.hub / "local.md").write_text("local\n", encoding="utf-8")
        git("add", "local.md", directory=self.hub)
        git("commit", "-q", "-m", "local", directory=self.hub)
        local = git("rev-parse", "HEAD", directory=self.hub)
        _, lines = self.tick(StubGh())
        line = self.line(lines, "HUB ")
        self.assertTrue(line.startswith(f'HUB "{self.hub.as_posix()}" not fast-forwarded: '), line)
        self.assertGreater(len(line), len(f'HUB "{self.hub.as_posix()}" not fast-forwarded: '))
        self.assertEqual(local, git("rev-parse", "HEAD", directory=self.hub))

    def test_a_checkout_whose_origin_is_not_on_github_fails(self) -> None:
        git("remote", "set-url", "origin", self.origin.as_posix(), directory=self.hub)
        status, lines = self.tick(StubGh())
        self.assertEqual(1, status)
        self.assertEqual(["FAILED origin names no GitHub repository: " + self.origin.as_posix()], lines)

    def test_a_directory_outside_a_checkout_fails(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        status, lines = self.tick(StubGh(), directory=outside)
        self.assertEqual(1, status)
        self.assertTrue(lines[0].startswith("FAILED "), lines)


class ListingTest(FixtureTestCase):
    def test_each_open_pull_request_that_is_not_a_draft_gets_one_line_oldest_first(self) -> None:
        gh = StubGh([pull(12), pull(11, draft=True), pull(10, state="PENDING"), pull(13, state="MISSING")])
        status, lines = self.tick(gh)
        self.assertEqual(0, status)
        self.assertEqual(
            [
                "PR #10 fix/topic-10 CLEAN auto-merge on head aaaaaaa Validation PENDING none",
                "PR #12 fix/topic-12 CLEAN auto-merge on head aaaaaaa Validation SUCCESS none",
                "PR #13 fix/topic-13 CLEAN auto-merge on head aaaaaaa Validation MISSING none",
            ],
            [line for line in lines if line.startswith("PR ")],
        )
        self.assertEqual("REMAINING 3", lines[-1])
        self.assertEqual([], gh.writes())

    def test_auto_merge_is_armed_as_a_squash_where_it_is_unarmed(self) -> None:
        gh = StubGh([pull(10, armed=False), pull(11)])
        _, lines = self.tick(gh)
        self.assertEqual([["pr", "merge", "10", "--repo", REPOSITORY, "--auto", "--squash"]], gh.writes())
        self.assertTrue(self.line(lines, "PR #10 ").endswith("auto-merge off head aaaaaaa Validation SUCCESS armed"))
        self.assertTrue(self.line(lines, "PR #11 ").endswith(" none"))

    def test_a_listing_that_fails_ends_the_tick(self) -> None:
        status, lines = self.tick(StubGh(failing=["pr list"]))
        self.assertEqual(1, status)
        self.assertTrue(lines[-1].startswith("FAILED cannot list the open pull requests: "), lines)

    def test_a_write_that_fails_is_named_for_its_pull_request_and_the_tick_goes_on(self) -> None:
        gh = StubGh([pull(10, armed=False), pull(11, armed=False)], failing=["pr merge"])
        status, lines = self.tick(gh)
        self.assertEqual(1, status)
        self.assertEqual(
            ["PR_FAILED #10 arm pr merge refused (HTTP 422)", "PR_FAILED #11 arm pr merge refused (HTTP 422)"],
            [line for line in lines if line.startswith("PR_FAILED")],
        )
        self.assertEqual("REMAINING 2", lines[-1])

    def test_the_required_check_is_pending_while_its_legs_run_and_missing_without_them(self) -> None:
        self.assertEqual(shepherd.Check("PENDING", RUN), shepherd.required_check(rollup("PENDING")))
        self.assertEqual(shepherd.Check("MISSING", None), shepherd.required_check(rollup("MISSING")))
        self.assertEqual(shepherd.Check("FAILURE", RUN), shepherd.required_check(rollup("FAILURE")))
        self.assertEqual(shepherd.Check("MISSING", None), shepherd.required_check(None))


class UpdateTest(FixtureTestCase):
    def test_at_most_two_behind_pull_requests_are_updated_oldest_first_by_a_merge(self) -> None:
        gh = StubGh(
            [
                pull(14, merge="BEHIND"),
                pull(11, merge="BEHIND", state="PENDING"),
                pull(13, merge="BEHIND"),
                pull(12, merge="CLEAN"),
            ]
        )
        _, lines = self.tick(gh)
        self.assertEqual(
            [["pr", "update-branch", "11", "--repo", REPOSITORY], ["pr", "update-branch", "13", "--repo", REPOSITORY]],
            gh.writes(),
        )
        self.assertTrue(self.line(lines, "PR #11 ").endswith(" updated"))
        self.assertTrue(self.line(lines, "PR #13 ").endswith(" updated"))
        self.assertTrue(self.line(lines, "PR #14 ").endswith(" next-tick"))
        self.assertTrue(self.line(lines, "PR #12 ").endswith(" none"))
        self.assertFalse(any("--rebase" in call for call in gh.calls))

    def test_a_behind_pull_request_whose_check_is_red_or_missing_is_not_updated(self) -> None:
        gh = StubGh(
            [pull(10, merge="BEHIND", state="MISSING"), pull(11, merge="BEHIND", state="CANCELLED")],
            runs={RUN: run_view([], conclusion="cancelled", attempt=2)},
        )
        _, lines = self.tick(gh)
        self.assertEqual([], [call for call in gh.writes() if call[1] == "update-branch"])
        self.assertTrue(self.line(lines, "PR #10 ").endswith(" none"))

    def test_a_pull_request_changing_the_policies_holds_every_other_update_but_its_own(self) -> None:
        gh = StubGh(
            [
                pull(10, merge="BEHIND"),
                pull(11, merge="BEHIND", files=("docs/a.md", "tests/validation/job_selection.py")),
                pull(12, merge="BEHIND"),
            ]
        )
        _, lines = self.tick(gh)
        self.assertEqual("HOLD #11 changes tests/validation/job_selection.py", self.line(lines, "HOLD "))
        self.assertEqual([["pr", "update-branch", "11", "--repo", REPOSITORY]], gh.writes())
        self.assertTrue(self.line(lines, "PR #10 ").endswith(" held"))
        self.assertTrue(self.line(lines, "PR #12 ").endswith(" held"))

    def test_a_pull_request_changing_the_runner_holds_the_updates_too(self) -> None:
        gh = StubGh([pull(10, merge="BEHIND"), pull(11, files=("tests/run_validation.py",))])
        _, lines = self.tick(gh)
        self.assertEqual("HOLD #11 changes tests/run_validation.py", self.line(lines, "HOLD "))
        self.assertEqual([], gh.writes())
        self.assertTrue(self.line(lines, "PR #10 ").endswith(" held"))

    def test_other_test_paths_hold_nothing(self) -> None:
        self.assertIsNone(shepherd.holding_path(["tests/validation_notes.md", "tests/run_validation.pyc", "x.py"]))
        self.assertEqual("tests/validation/x.py", shepherd.holding_path(["a", "tests/validation/x.py"]))


class JudgementTest(FixtureTestCase):
    def red(
        self,
        jobs: Sequence[dict[str, Any]],
        logs: Mapping[int, str] | None = None,
        *,
        attempt: int = 1,
        conclusion: str = "failure",
        head: str = HEAD,
        dry_run: bool = False,
        state: str = "FAILURE",
    ) -> tuple[int, list[str], StubGh]:
        gh = StubGh(
            [pull(20, state=state)],
            runs={RUN: run_view(jobs, head=head, attempt=attempt, conclusion=conclusion)},
            logs=logs,
        )
        status, lines = self.tick(gh, dry_run=dry_run)
        return status, lines, gh

    def assert_rerun(self, lines: Sequence[str], gh: StubGh, reason: str, leg: str) -> None:
        self.assertEqual(f"JUDGED #20 run {RUN} attempt 1 head aaaaaaa current", self.line(lines, "JUDGED "))
        self.assertEqual(f'RERUN #20 run {RUN} {reason} leg "{leg}"', self.line(lines, "RERUN "))
        self.assertEqual([["run", "rerun", str(RUN), "--repo", REPOSITORY, "--failed"]], gh.writes())
        self.assertTrue(self.line(lines, "PR #20 ").endswith(" rerun"))

    def test_a_red_run_on_a_superseded_head_is_no_failure(self) -> None:
        status, lines, gh = self.red([job("Validation on Linux", "failure")], head=OTHER)
        self.assertEqual(0, status)
        self.assertEqual(
            f"JUDGED #20 run {RUN} attempt 1 head bbbbbbb superseded by aaaaaaa", self.line(lines, "JUDGED ")
        )
        self.assertEqual([], gh.writes())
        self.assertFalse(any(line.startswith(("RERUN", "DEFECT")) for line in lines))
        self.assertFalse(any("--log-failed" in call for call in gh.calls))

    def test_a_termination_is_rerun(self) -> None:
        status, lines, gh = self.red([job("Windows shard 2/4", "failure")], {1: failed_log(TERMINATED_BLOCK)})
        self.assertEqual(0, status)
        self.assert_rerun(lines, gh, "termination", "Windows shard 2/4")

    def test_a_suite_that_exited_with_no_output_is_rerun(self) -> None:
        _, lines, gh = self.red([job("Windows shard 1/4", "failure")], {1: failed_log(SILENT_BLOCK)})
        self.assert_rerun(lines, gh, "silent-exit", "Windows shard 1/4")

    def test_a_cancelled_leg_is_rerun(self) -> None:
        _, lines, gh = self.red([job("Windows shard 3/4", "cancelled", None)])
        self.assert_rerun(lines, gh, "cancelled", "Windows shard 3/4")

    def test_a_cancelled_run_with_no_failed_leg_is_rerun(self) -> None:
        _, lines, gh = self.red([], conclusion="cancelled", state="SKIPPED")
        self.assert_rerun(lines, gh, "cancelled", "the run")

    def test_a_failed_setup_step_is_a_service_failure(self) -> None:
        _, lines, gh = self.red([job("Windows shard 4/4", "failure", "Install validation tools")])
        self.assert_rerun(lines, gh, "service", "Windows shard 4/4")
        self.assertFalse(any("--log-failed" in call for call in gh.calls))

    def test_a_package_gallery_failure_in_the_log_is_a_service_failure(self) -> None:
        _, lines, gh = self.red([job("Validation on Linux", "failure")], {1: failed_log(GALLERY_BLOCK)})
        self.assert_rerun(lines, gh, "service", "Validation on Linux")

    def test_a_required_check_that_failed_with_every_leg_passed_is_a_service_failure(self) -> None:
        _, lines, gh = self.red([job("Windows shard 1/4", "success", None)])
        self.assert_rerun(lines, gh, "service", "Validation")

    def test_a_failing_check_is_a_defect_naming_the_pull_request_the_leg_and_the_check(self) -> None:
        status, lines, gh = self.red([job("Windows shard 4/4, Python 3.11", "failure")], {1: failed_log(MYPY_BLOCK)})
        self.assertEqual(1, status)
        self.assertEqual(
            'DEFECT #20 fix/topic-20 leg "Windows shard 4/4, Python 3.11" '
            'check "static type check (mypy skills/review-insights/scripts)"',
            self.line(lines, "DEFECT "),
        )
        self.assertEqual([], gh.writes())

    def test_a_failed_policy_check_is_a_defect_naming_it(self) -> None:
        policy = ["", RULE, "FAIL: test_skill_grants (tests.validation.skill_grants.Test.test_skill_grants)", DASHES]
        _, lines, _ = self.red([job("Validation on Linux", "failure")], {1: failed_log(SILENT_BLOCK, extra=policy)})
        self.assertEqual(
            'DEFECT #20 fix/topic-20 leg "Validation on Linux" check "test_skill_grants"', self.line(lines, "DEFECT ")
        )

    def test_one_defect_among_rerun_cases_is_a_defect_and_reruns_nothing(self) -> None:
        jobs = [job("Windows shard 1/4", "failure", number=1), job("Windows shard 2/4", "failure", number=2)]
        logs = {1: failed_log(TERMINATED_BLOCK), 2: failed_log(SILENT_BLOCK, MYPY_BLOCK)}
        _, lines, gh = self.red(jobs, logs)
        self.assertEqual(
            'DEFECT #20 fix/topic-20 leg "Windows shard 2/4" '
            'check "static type check (mypy skills/review-insights/scripts)"',
            self.line(lines, "DEFECT "),
        )
        self.assertEqual([], gh.writes())

    def test_a_timed_out_leg_or_a_log_with_no_report_is_a_defect(self) -> None:
        _, lines, _ = self.red([job("Windows shard 1/4", "timed_out")])
        self.assertEqual(
            f'DEFECT #20 fix/topic-20 leg "Windows shard 1/4" check "the leg\'s time limit in {VALIDATION_STEP}"',
            self.line(lines, "DEFECT "),
        )
        _, lines, _ = self.red(
            [job("Windows shard 1/4", "failure")], {1: PREFIX + "Traceback (most recent call last):\n"}
        )
        self.assertEqual(
            f'DEFECT #20 fix/topic-20 leg "Windows shard 1/4" check "{VALIDATION_STEP}"', self.line(lines, "DEFECT ")
        )

    def test_a_rerun_case_on_a_head_already_rerun_is_left_to_judgment(self) -> None:
        status, lines, gh = self.red(
            [job("Windows shard 2/4", "failure")], {1: failed_log(TERMINATED_BLOCK)}, attempt=2
        )
        self.assertEqual(1, status)
        self.assertEqual(
            f'RERUN_SPENT #20 run {RUN} attempt 2 termination leg "Windows shard 2/4"', self.line(lines, "RERUN_SPENT ")
        )
        self.assertEqual([], gh.writes())

    def test_a_check_naming_no_run_is_a_defect(self) -> None:
        entry = pull(20, state="FAILURE")
        for check in entry["statusCheckRollup"]:
            check["detailsUrl"] = "https://github.com/owner/repo/runs/5"
        _, lines = self.tick(StubGh([entry]))
        self.assertEqual(
            'DEFECT #20 fix/topic-20 leg "Validation" check "the check names no workflow run"',
            self.line(lines, "DEFECT "),
        )

    def test_a_judgment_that_cannot_read_the_run_is_named(self) -> None:
        status, lines = self.tick(StubGh([pull(20, state="FAILURE")], failing=["run view"]))
        self.assertEqual(1, status)
        self.assertTrue(self.line(lines, "PR_FAILED ").startswith("PR_FAILED #20 judge "))

    def test_a_dry_run_judges_and_writes_nothing(self) -> None:
        gh = StubGh(
            [pull(20, state="FAILURE", armed=False), pull(21, merge="BEHIND")],
            runs={RUN: run_view([job("Windows shard 2/4", "failure")])},
            logs={1: failed_log(TERMINATED_BLOCK)},
        )
        status, lines = self.tick(gh, dry_run=True)
        self.assertEqual(0, status)
        self.assertEqual([], gh.writes())
        self.assertEqual(
            f'WOULD_RERUN #20 run {RUN} termination leg "Windows shard 2/4"', self.line(lines, "WOULD_RERUN ")
        )
        self.assertTrue(self.line(lines, "PR #20 ").endswith(" would-arm,would-rerun"))
        self.assertTrue(self.line(lines, "PR #21 ").endswith(" would-update"))


class LogTest(unittest.TestCase):
    def test_log_lines_drop_github_s_prefixes_and_terminal_escapes(self) -> None:
        text = PREFIX + "\x1b[31mFAIL\x1b[0m one\n2026-10-16T05:51:51Z two\nthree\twith a tab\n"
        self.assertEqual(["FAIL one", "two", "three\twith a tab"], shepherd.log_lines(text))

    def test_a_block_ends_at_the_next_rule_or_the_closing_line(self) -> None:
        lines = shepherd.log_lines(failed_log(SILENT_BLOCK, MYPY_BLOCK))
        blocks = shepherd.failure_blocks(lines)
        self.assertEqual([SILENT_BLOCK[0], MYPY_BLOCK[0]], [label for label, _ in blocks])
        self.assertEqual(["silent-exit", None], [shepherd.block_reason(body) for _, body in blocks])

    def test_a_retried_block_is_not_a_failure(self) -> None:
        lines = shepherd.log_lines(
            failed_log(extra=["", RULE, "RETRIED tests/x.py", DASHES, "Ended before its first line of output"])
        )
        self.assertEqual([], shepherd.failure_blocks(lines))


class MergedTest(FixtureTestCase):
    def test_a_worktree_whose_branch_merged_is_named_for_retirement(self) -> None:
        done, busy, reused = (self.root / name for name in ("done", "busy", "reused"))
        git("worktree", "add", "-q", "-b", "fix/done", str(done), directory=self.hub)
        git("worktree", "add", "-q", "-b", "fix/busy", str(busy), directory=self.hub)
        git("worktree", "add", "-q", "-b", "fix/topic-30", str(reused), directory=self.hub)
        merged = [
            {"number": 5, "headRefName": "fix/done"},
            {"number": 8, "headRefName": "fix/done"},
            {"number": 6, "headRefName": "fix/topic-30"},
            {"number": 7, "headRefName": "main"},
        ]
        status, lines = self.tick(StubGh([pull(30)], merged=merged), dry_run=True)
        self.assertEqual(0, status)
        self.assertEqual(
            [f'MERGED #8 worktree "{done.as_posix()}" branch fix/done'],
            [line for line in lines if line.startswith("MERGED")],
        )

    def test_a_merged_listing_that_fails_is_named(self) -> None:
        gh = StubGh()
        original = gh.__call__

        def refuse_merged(command: Sequence[str]) -> CommandResult:
            if "merged" in command:
                return CommandResult(1, "", "HTTP 403: forbidden")
            return original(command)

        lines: list[str] = []
        client = GitHubClient(refuse_merged, backoff=NO_WAIT, sleeper=lambda _: None)
        status = shepherd.tick(GitClient(), client, self.hub, dry_run=True, emit=lines.append)
        self.assertEqual(1, status)
        self.assertTrue(self.line(lines, "MERGED_FAILED ").startswith("MERGED_FAILED HTTP 403"))
        self.assertEqual("REMAINING 0", lines[-1])


class MainTest(unittest.TestCase):
    def test_main_runs_one_tick_from_the_working_directory(self) -> None:
        with mock.patch.object(shepherd, "tick", return_value=0) as tick:
            self.assertEqual(0, shepherd.main(["--dry-run"]))
        self.assertTrue(tick.call_args.kwargs["dry_run"])
        self.assertEqual(Path.cwd(), tick.call_args.args[2])

    def test_an_unknown_argument_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            shepherd.main(["--rebase"])
        self.assertEqual(2, raised.exception.code)


if __name__ == "__main__":
    unittest.main()

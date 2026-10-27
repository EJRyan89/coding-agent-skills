from __future__ import annotations

import contextlib
import dataclasses
import importlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))
# The validation runner's pool, whose own reports the log tests judge, and the repository root its imports need.
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tests" / "validation"))

import premerge_review
import shepherd_pull_requests as shepherd
import whole_number
from git_client import GitClient, GitError, GitResult, subprocess_runner
from git_worktrees import list_worktrees
from github_client import Backoff, CommandResult, GitHubClient, GitHubError
from review_records import RecordError

# Imported by name, since mypy checks this folder from inside it, where the deployer the pool imports does not resolve;
# the runner, whose report the script reads, likewise.
job_pool = importlib.import_module("job_pool")
run_validation = importlib.import_module("tests.run_validation")

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
# The fields gh 2.102.0 lists for each command's --json, written out here rather than read from the script, so a
# field the script misspells is refused as gh refuses it.
GH_JSON_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "pr list": frozenset(
            [
                "additions",
                "assignees",
                "author",
                "autoMergeRequest",
                "baseRefName",
                "baseRefOid",
                "body",
                "changedFiles",
                "closed",
                "closedAt",
                "closingIssuesReferences",
                "comments",
                "commits",
                "createdAt",
                "deletions",
                "files",
                "fullDatabaseId",
                "headRefName",
                "headRefOid",
                "headRepository",
                "headRepositoryOwner",
                "id",
                "isCrossRepository",
                "isDraft",
                "labels",
                "latestReviews",
                "maintainerCanModify",
                "mergeCommit",
                "mergeStateStatus",
                "mergeable",
                "mergedAt",
                "mergedBy",
                "milestone",
                "number",
                "potentialMergeCommit",
                "projectCards",
                "projectItems",
                "reactionGroups",
                "reviewDecision",
                "reviewRequests",
                "reviews",
                "state",
                "statusCheckRollup",
                "title",
                "updatedAt",
                "url",
            ]
        ),
        "run list": frozenset(
            [
                "attempt",
                "conclusion",
                "createdAt",
                "databaseId",
                "displayTitle",
                "event",
                "headBranch",
                "headSha",
                "name",
                "number",
                "startedAt",
                "status",
                "updatedAt",
                "url",
                "workflowDatabaseId",
                "workflowName",
            ]
        ),
        "run view": frozenset(
            [
                "attempt",
                "conclusion",
                "createdAt",
                "databaseId",
                "displayTitle",
                "event",
                "headBranch",
                "headSha",
                "jobs",
                "name",
                "number",
                "startedAt",
                "status",
                "updatedAt",
                "url",
                "workflowDatabaseId",
                "workflowName",
            ]
        ),
    }
)
# A branch name and a step name holding a C1 control and a line separator, which GitHub returns as they are, and
# what each line prints for them.
ODD_BRANCH = "fix/odd\x85name\u2028here"
FLAT_BRANCH = "fix/odd name here"
ODD_STEP = "Run\x85repository\u2028validation"


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
    cross: bool = False,
    branch: str | None = None,
) -> dict[str, Any]:
    return {
        "number": number,
        "headRefName": branch or f"fix/topic-{number}",
        "headRefOid": head,
        "isCrossRepository": cross,
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

SUITE_COMMAND = ["python.exe", "-B", "tests/x.py"]
# A termination whose command names a package gallery, as a suite that installs from one would.
ENDING_NAMING_A_SERVICE = (
    "pwsh.exe was ended with status 0xC0000142 before its first line of output: pwsh.exe -File install.ps1 "
    "-Source powershellgallery.com"
)


def ended() -> None:
    raise job_pool.Terminated("0xC0000142", SUITE_COMMAND)


def failed() -> None:
    raise AssertionError(f"Command failed with exit code 1: {' '.join(SUITE_COMMAND)}\nE   assert 1 == 2\n\n1 failed")


def exited_silently() -> None:
    raise AssertionError(f"Command failed with exit code 4: {' '.join(SUITE_COMMAND)}\n\n")


def pool_report(*attempts: Callable[[], None]) -> str:
    """The report the validation runner's own pool gives a suite whose attempts run in turn, one per start."""
    remaining = iter(attempts)
    suite = job_pool.Job("tests/x.py", "tests/x.py", 1, lambda: next(remaining)())
    with contextlib.redirect_stdout(io.StringIO()):
        failures, _ = job_pool.run_jobs([suite], verbose=False, workers=1)
    return str(failures[0].report)


class StubGh:
    """gh as the tick calls it: the open and merged listings, each run, the runs on a head, each job's failed log,
    and the writes."""

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
        self.requested: dict[str, set[str]] = {}

    def __call__(self, command: Sequence[str]) -> CommandResult:
        self.calls.append(list(command))
        arguments = list(command[1:])
        verb = " ".join(arguments[:2])
        if verb in self.failing:
            return CommandResult(1, "", f"{verb} refused (HTTP 422)")
        if "--json" in arguments:
            fields = arguments[arguments.index("--json") + 1].split(",")
            unknown = [name for name in fields if name not in GH_JSON_FIELDS.get(verb, frozenset())]
            if unknown:
                return CommandResult(1, "", f'Unknown JSON field: "{unknown[0]}"')
            self.requested.setdefault(verb, set()).update(fields)
        if verb == "pr list":
            state = arguments[arguments.index("--state") + 1]
            return CommandResult(0, json.dumps(limited(arguments, self.pulls if state == "open" else self.merged)), "")
        if verb == "run list":
            self.check_run_list(arguments)
            commit = arguments[arguments.index("--commit") + 1]
            listed = [{"databaseId": number} for number, run in self.runs.items() if run["headSha"] == commit]
            return CommandResult(
                0, json.dumps(limited(arguments, sorted(listed, key=lambda run: -run["databaseId"]))), ""
            )
        if verb == "run view" and "--log-failed" in arguments:
            return CommandResult(0, self.logs[int(arguments[arguments.index("--job") + 1])], "")
        if verb == "run view":
            return CommandResult(0, json.dumps(self.runs[int(arguments[2])]), "")
        if verb in ("pr merge", "pr update-branch", "run rerun"):
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected gh call: {command}")

    @staticmethod
    def check_run_list(arguments: Sequence[str]) -> None:
        """The runs on a head are the required check's workflow's pull request runs, as gh filters them."""
        for option, value in (("--workflow", "Validation"), ("--event", "pull_request"), ("--repo", REPOSITORY)):
            if arguments[arguments.index(option) + 1] != value:
                raise AssertionError(f"gh run list without {option} {value}: {arguments}")

    def writes(self) -> list[list[str]]:
        return [call[1:] for call in self.calls if " ".join(call[1:3]) in ("pr merge", "pr update-branch", "run rerun")]


def limited(arguments: Sequence[str], items: list[Any]) -> list[Any]:
    """The first items a gh listing given `--limit` prints, as gh cuts a listing at its limit."""
    if "--limit" not in arguments:
        raise AssertionError(f"a gh listing without --limit: {arguments}")
    return items[: int(arguments[arguments.index("--limit") + 1])]


def git(*arguments: str, directory: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


class FixtureTestCase(unittest.TestCase):
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

    def branches_behind_and_current(self) -> tuple[str, str]:
        """Advance origin's main past the hub's first commit, fetch it, and return the first commit and the new main."""
        first = git("rev-parse", "HEAD", directory=self.hub)
        after = self.advance_origin()
        git("fetch", "-q", "origin", directory=self.hub)
        return first, after

    def push_branch(self, commit: str, branch: str) -> None:
        git("push", "-q", "origin", f"{commit}:refs/heads/{branch}", directory=self.hub)

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
        self.assertEqual("DRY_RUN the hub is not fetched, and nothing is armed, disarmed, updated, or rerun", lines[0])
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
        status, lines = self.tick(StubGh())
        self.assertEqual(1, status)
        self.assertTrue(self.line(lines, "HUB ").endswith("not fast-forwarded: it has uncommitted changes"))
        git("checkout", "-q", "README.md", directory=self.hub)
        git("switch", "-q", "-c", "side", directory=self.hub)
        status, lines = self.tick(StubGh(), dry_run=True)
        self.assertEqual(1, status)
        self.assertTrue(self.line(lines, "HUB ").endswith("not fast-forwarded: it is on side, not main"))

    def test_a_fetch_that_fails_is_named_for_the_shepherd(self) -> None:
        output = GitClient.output

        def refuse_fetch(client: GitClient, arguments: Sequence[str], **options: Any) -> str:
            if arguments[0] == "fetch":
                raise GitError("fatal: unable to access origin", returncode=128)
            return output(client, arguments, **options)

        with mock.patch.object(GitClient, "output", refuse_fetch):
            status, lines = self.tick(StubGh())
        self.assertEqual(1, status)
        self.assertTrue(self.line(lines, "HUB ").endswith("not fast-forwarded: fatal: unable to access origin"))
        self.assertEqual("REMAINING 0", lines[-1])

    def test_a_hub_whose_main_diverged_names_git_s_refusal(self) -> None:
        self.advance_origin()
        (self.hub / "local.md").write_text("local\n", encoding="utf-8")
        git("add", "local.md", directory=self.hub)
        git("commit", "-q", "-m", "local", directory=self.hub)
        local = git("rev-parse", "HEAD", directory=self.hub)
        status, lines = self.tick(StubGh())
        self.assertEqual(1, status)
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
        self.assertEqual([f"FAILED {outside.as_posix()} is not in a git checkout"], lines)

    def test_a_checkout_git_cannot_read_fails_naming_git_not_as_no_checkout(self) -> None:
        (self.hub / ".git" / "config").write_text("[core\nbogus\n", encoding="utf-8")
        status, lines = self.tick(StubGh())
        self.assertEqual(1, status)
        self.assertEqual(1, len(lines), lines)
        self.assertTrue(lines[0].startswith(f"FAILED git could not read the checkout at {self.hub.as_posix()}: "))
        self.assertIn("bad config line 1", lines[0])


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
        listing = next(call for call in gh.calls if call[1:3] == ["pr", "list"])
        self.assertEqual(REPOSITORY, listing[listing.index("--repo") + 1])

    def test_auto_merge_is_armed_as_a_squash_where_it_is_unarmed(self) -> None:
        gh = StubGh([pull(10, armed=False), pull(11)])
        _, lines = self.tick(gh)
        self.assertEqual([["pr", "merge", "10", "--repo", REPOSITORY, "--auto", "--squash"]], gh.writes())
        self.assertTrue(self.line(lines, "PR #10 ").endswith("auto-merge off head aaaaaaa Validation SUCCESS armed"))
        self.assertTrue(self.line(lines, "PR #11 ").endswith(" none"))

    def test_more_open_pull_requests_than_one_tick_lists_ends_the_tick_naming_the_cap(self) -> None:
        gh = StubGh([pull(10), pull(11), pull(12)])
        with mock.patch.object(shepherd, "OPEN_LISTED", 2):
            status, lines = self.tick(gh)
        self.assertEqual(1, status)
        self.assertEqual(
            "FAILED cannot list the open pull requests: more than 2 are open, more than one tick lists", lines[-1]
        )
        self.assertEqual([], gh.writes())
        listing = next(call for call in gh.calls if call[1:3] == ["pr", "list"])
        self.assertEqual("3", listing[listing.index("--limit") + 1])

    def test_a_cut_open_listing_is_an_incomplete_answer(self) -> None:
        client = GitHubClient(StubGh([pull(10), pull(11)]), backoff=NO_WAIT, sleeper=lambda _: None)
        with mock.patch.object(shepherd, "OPEN_LISTED", 1), self.assertRaises(GitHubError) as context:
            shepherd.read_pulls(client, REPOSITORY)
        self.assertEqual("incomplete", context.exception.kind)

    def test_the_merged_listing_asks_for_the_newest_merged_pull_requests_of_the_repository(self) -> None:
        gh = StubGh(merged=[{"number": 9, "headRefName": "fix/old", "headRefOid": "b" * 40}])
        client = GitHubClient(gh, backoff=NO_WAIT, sleeper=lambda _: None)
        self.assertEqual([shepherd.Merged(9, "fix/old", "b" * 40)], shepherd.read_merged(client, REPOSITORY))
        self.assertEqual(
            [
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    REPOSITORY,
                    "--state",
                    "merged",
                    "--json",
                    "number,headRefName,headRefOid",
                    "--limit",
                    str(shepherd.MERGED_LISTED + 1),
                ]
            ],
            gh.calls,
        )

    def test_exactly_as_many_open_pull_requests_as_one_tick_lists_are_all_judged(self) -> None:
        with mock.patch.object(shepherd, "OPEN_LISTED", 2):
            status, lines = self.tick(StubGh([pull(10), pull(11)]))
        self.assertEqual((0, "REMAINING 2"), (status, lines[-1]))

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
        self.assertEqual(shepherd.Check("FAILURE", RUN, 1.0), shepherd.required_check(rollup("FAILURE")))
        self.assertEqual(shepherd.Check("MISSING", None), shepherd.required_check(None))


class FieldTest(FixtureTestCase):
    def full_tick(self) -> tuple[int, list[str], StubGh]:
        """A tick that makes every gh request that names --json fields: both listings, a run, and the runs on a head."""
        gh = StubGh(
            [pull(20, state="FAILURE")],
            runs={RUN: run_view([job("Windows shard 2/4", "failure")])},
            logs={1: failed_log(TERMINATED_BLOCK)},
            merged=[{"number": 5, "headRefName": "fix/done", "headRefOid": OTHER}],
        )
        status, lines = self.tick(gh, dry_run=True)
        return status, lines, gh

    def test_every_field_the_tick_requests_is_one_gh_lists(self) -> None:
        git("worktree", "add", "-q", "-b", "fix/done", str(self.root / "done"), directory=self.hub)
        status, lines, gh = self.full_tick()
        self.assertEqual(0, status, lines)
        self.assertEqual({"pr list", "run list", "run view"}, set(gh.requested))
        for verb, fields in gh.requested.items():
            with self.subTest(verb=verb):
                self.assertLessEqual(fields, GH_JSON_FIELDS[verb])

    def test_a_misspelt_field_fails_the_tick_as_gh_refuses_it(self) -> None:
        misspelt = (
            ("PULL_FIELDS", "headRefOid", "headRefOID", "FAILED cannot list the open pull requests: "),
            ("RUN_FIELDS", "headSha", "headSHA", "PR_FAILED #20 judge "),
        )
        for constant, name, wrong, failure in misspelt:
            replaced = getattr(shepherd, constant).replace(name, wrong)
            with self.subTest(constant=constant), mock.patch.object(shepherd, constant, replaced):
                status, lines, _ = self.full_tick()
                self.assertEqual(1, status)
                self.assertTrue(any(line.startswith(failure) and wrong in line for line in lines), lines)


class AdmissionTest(FixtureTestCase):
    def fork_beside_own(self) -> StubGh:
        """An unarmed pull request from another repository that is behind, red, and changes the runner, beside an
        unarmed one from this repository that is behind and green."""
        fork = pull(30, armed=False, cross=True, merge="BEHIND", state="FAILURE", files=("tests/run_validation.py",))
        return StubGh(
            [fork, pull(31, armed=False, merge="BEHIND")],
            runs={RUN: run_view([job("Windows shard 2/4", "failure")])},
            logs={1: failed_log(TERMINATED_BLOCK)},
        )

    def test_a_pull_request_from_another_repository_is_never_armed_and_is_named(self) -> None:
        gh = self.fork_beside_own()
        status, lines = self.tick(gh)
        self.assertEqual(1, status)
        self.assertEqual(
            "NOT_ADMITTED #30 fix/topic-30 head aaaaaaa from another repository", self.line(lines, "NOT_ADMITTED ")
        )
        self.assertEqual(
            "PR #30 fix/topic-30 BEHIND auto-merge off head aaaaaaa Validation FAILURE none",
            self.line(lines, "PR #30 "),
        )
        self.assertFalse(any(line.startswith(("HOLD", "JUDGED", "RERUN", "DEFECT")) for line in lines), lines)
        self.assertFalse(any(call[1:3] == ["run", "view"] for call in gh.calls))
        self.assertFalse(any("30" in write for write in gh.writes()), gh.writes())

    def test_a_pull_request_from_this_repository_is_still_armed_and_updated(self) -> None:
        gh = self.fork_beside_own()
        _, lines = self.tick(gh)
        self.assertEqual(
            [
                ["pr", "merge", "31", "--repo", REPOSITORY, "--auto", "--squash"],
                ["pr", "update-branch", "31", "--repo", REPOSITORY],
            ],
            gh.writes(),
        )
        self.assertTrue(self.line(lines, "PR #31 ").endswith(" armed,updated"))

    def test_a_dry_run_names_it_and_would_arm_only_this_repository_s(self) -> None:
        status, lines = self.tick(self.fork_beside_own(), dry_run=True)
        self.assertEqual(1, status)
        self.assertTrue(self.line(lines, "NOT_ADMITTED #30 ").endswith(" from another repository"))
        self.assertTrue(self.line(lines, "PR #30 ").endswith(" none"))
        self.assertTrue(self.line(lines, "PR #31 ").endswith(" would-arm,would-update"))


class ConflictTest(FixtureTestCase):
    def test_a_pull_request_in_conflict_with_main_is_named_for_its_owner_and_not_updated(self) -> None:
        gh = StubGh([pull(15, merge="DIRTY"), pull(16, merge="BEHIND")])
        status, lines = self.tick(gh)
        self.assertEqual(1, status)
        self.assertEqual(
            "CONFLICT #15 fix/topic-15 head aaaaaaa merge origin/main and resolve", self.line(lines, "CONFLICT ")
        )
        self.assertEqual(
            "PR #15 fix/topic-15 DIRTY auto-merge on head aaaaaaa Validation SUCCESS none", self.line(lines, "PR #15 ")
        )
        self.assertEqual([["pr", "update-branch", "16", "--repo", REPOSITORY]], gh.writes())
        self.assertLess(lines.index(self.line(lines, "PR #15 ")), lines.index(self.line(lines, "CONFLICT ")))

    def test_a_dry_run_names_the_conflict_too(self) -> None:
        status, lines = self.tick(StubGh([pull(15, merge="DIRTY")]), dry_run=True)
        self.assertEqual(1, status)
        self.assertTrue(self.line(lines, "CONFLICT #15 ").endswith(" merge origin/main and resolve"))


class HoldTest(FixtureTestCase):
    def held(self, number: int, reason: str = "owes an evaluate-skill citation", **fields: Any) -> dict[str, Any]:
        return {**pull(number, **fields), "body": f"## Summary\n\nA change.\n\nShepherd-hold: {reason}\n"}

    def test_a_held_pull_request_is_not_armed_but_is_judged_and_the_others_are_armed_and_updated(self) -> None:
        held = self.held(20, armed=False, merge="BEHIND", state="FAILURE")
        gh = StubGh(
            [held, pull(21, armed=False, merge="BEHIND")],
            runs={RUN: run_view([job("Windows shard 2/4", "failure")])},
            logs={1: failed_log(TERMINATED_BLOCK)},
        )
        status, lines = self.tick(gh)
        self.assertEqual(0, status, lines)
        self.assertEqual('HELD #20 fix/topic-20 "owes an evaluate-skill citation"', self.line(lines, "HELD "))
        self.assertTrue(self.line(lines, "PR #20 ").endswith(" Validation FAILURE rerun"))
        self.line(lines, "JUDGED #20 ")
        self.assertEqual(
            [
                ["run", "rerun", str(RUN), "--repo", REPOSITORY, "--failed"],
                ["pr", "merge", "21", "--repo", REPOSITORY, "--auto", "--squash"],
                ["pr", "update-branch", "21", "--repo", REPOSITORY],
            ],
            gh.writes(),
        )

    def test_an_armed_pull_request_that_is_held_is_disarmed(self) -> None:
        gh = StubGh([self.held(20)])
        _, lines = self.tick(gh)
        self.assertEqual([["pr", "merge", "20", "--repo", REPOSITORY, "--disable-auto"]], gh.writes())
        self.assertTrue(self.line(lines, "PR #20 ").endswith(" auto-merge on head aaaaaaa Validation SUCCESS disarmed"))
        gh = StubGh([self.held(20)])
        _, lines = self.tick(gh, dry_run=True)
        self.assertEqual([], gh.writes())
        self.assertTrue(self.line(lines, "PR #20 ").endswith(" would-disarm"))

    def test_a_disarm_that_fails_is_named(self) -> None:
        status, lines = self.tick(StubGh([self.held(20)], failing=["pr merge"]))
        self.assertEqual(1, status)
        self.assertEqual("PR_FAILED #20 disarm pr merge refused (HTTP 422)", self.line(lines, "PR_FAILED "))

    def test_only_a_line_of_its_own_holds_and_its_reason_is_flattened(self) -> None:
        self.assertIsNone(shepherd.hold_of("Do not add a Shepherd-hold: line yet"))
        self.assertIsNone(shepherd.hold_of(None))
        self.assertEqual("no reason given", shepherd.hold_of("Text\nShepherd-hold:\n"))
        self.assertEqual("waits on #1 then #2", shepherd.hold_of("Shepherd-hold:  waits on #1\x85then #2  "))
        # A pull request from another repository is not admitted, held or not.
        _, lines = self.tick(StubGh([self.held(20, cross=True)]))
        self.assertFalse(any(line.startswith("HELD") for line in lines), lines)


class FlatTextTest(FixtureTestCase):
    def test_every_branch_path_and_step_from_github_is_flattened_onto_its_line(self) -> None:
        first, after = self.branches_behind_and_current()
        self.push_branch(after, ODD_BRANCH + "-3")
        gh = StubGh(
            [
                pull(21, state="FAILURE", branch=ODD_BRANCH, files=("tests/validation/odd\u2028name.py",)),
                pull(22, merge="DIRTY", branch=ODD_BRANCH + "-2"),
                pull(23, head=first, branch=ODD_BRANCH + "-3"),
            ],
            runs={RUN: run_view([job("Windows shard 1/4", "timed_out", ODD_STEP)])},
        )
        status, lines = self.tick(gh, dry_run=True)
        self.assertEqual(1, status)
        self.assertEqual("HOLD #21 changes tests/validation/odd name.py", self.line(lines, "HOLD "))
        self.assertEqual(
            f"PR #21 {FLAT_BRANCH} CLEAN auto-merge on head aaaaaaa Validation FAILURE none",
            self.line(lines, "PR #21 "),
        )
        self.assertEqual(
            f'DEFECT #21 {FLAT_BRANCH} leg "Windows shard 1/4" check "the leg\'s time limit in Run repository '
            'validation"',
            self.line(lines, "DEFECT "),
        )
        self.assertEqual(
            f"CONFLICT #22 {FLAT_BRANCH}-2 head aaaaaaa merge origin/main and resolve", self.line(lines, "CONFLICT ")
        )
        self.assertEqual(
            f"OUT_OF_STEP #23 {FLAT_BRANCH}-3 head {first[:7]} branch {after[:7]}", self.line(lines, "OUT_OF_STEP ")
        )
        for line in lines:
            self.assertEqual([line], line.splitlines(), repr(line))
            self.assertIsNone(re.search(r"[\x00-\x1f\x7f-\x9f]", line), repr(line))

    def test_a_merged_branch_name_is_flattened(self) -> None:
        worktree = self.root / "odd"
        git("worktree", "add", "-q", "-b", ODD_BRANCH, str(worktree), directory=self.hub)
        head = git("rev-parse", "HEAD", directory=worktree)
        merged = [{"number": 9, "headRefName": ODD_BRANCH, "headRefOid": head}]
        _, lines = self.tick(StubGh(merged=merged), dry_run=True)
        self.assertEqual(
            f'MERGED #9 worktree "{worktree.as_posix()}" branch {FLAT_BRANCH}', self.line(lines, "MERGED ")
        )


def legs_finished(finished: int, legs: int, run: int = RUN) -> list[dict[str, Any]]:
    """A head's checks while its run's legs run, `finished` of `legs` of them done."""
    url = f"https://github.com/{REPOSITORY}/actions/runs/{run}/job/9"
    return [
        {
            "__typename": "CheckRun",
            "name": f"Windows shard {index}/{legs}",
            "workflowName": "Validation",
            "detailsUrl": url,
            "status": "COMPLETED" if index <= finished else "IN_PROGRESS",
            "conclusion": "SUCCESS" if index <= finished else "",
        }
        for index in range(1, legs + 1)
    ]


class UpdateTest(FixtureTestCase):
    def test_one_behind_pull_request_is_updated_the_one_furthest_along_by_a_merge(self) -> None:
        gh = StubGh([pull(14, merge="BEHIND"), pull(11, merge="BEHIND", state="PENDING"), pull(13, merge="BEHIND")])
        _, lines = self.tick(gh)
        self.assertEqual([["pr", "update-branch", "13", "--repo", REPOSITORY]], gh.writes())
        self.assertTrue(self.line(lines, "PR #13 ").endswith(" updated"))
        self.assertTrue(self.line(lines, "PR #11 ").endswith(" next-tick"))
        self.assertTrue(self.line(lines, "PR #14 ").endswith(" next-tick"))
        self.assertFalse(any("--rebase" in call for call in gh.calls))

    def test_among_pending_checks_the_one_with_more_legs_finished_is_updated(self) -> None:
        behind = [pull(number, merge="BEHIND", state="PENDING") for number in (21, 22, 23)]
        behind[0]["statusCheckRollup"] = legs_finished(1, 4)
        behind[1]["statusCheckRollup"] = legs_finished(3, 4)
        behind[2]["statusCheckRollup"] = legs_finished(3, 4)
        gh = StubGh(behind)
        _, lines = self.tick(gh)
        self.assertEqual([["pr", "update-branch", "22", "--repo", REPOSITORY]], gh.writes())
        self.assertEqual(
            ["next-tick", "updated", "next-tick"],
            [self.line(lines, f"PR #{number} ").rsplit(" ", 1)[1] for number in (21, 22, 23)],
        )
        self.assertEqual(0.75, shepherd.required_check(legs_finished(3, 4)).progress)
        # The required check's own entry is no leg, even while it runs.
        url = f"https://github.com/{REPOSITORY}/actions/runs/{RUN}/job/9"
        running = {"name": "Validation", "workflowName": "Validation", "detailsUrl": url, "status": "IN_PROGRESS"}
        self.assertEqual(shepherd.Check("PENDING", RUN, 0.75), shepherd.required_check([*legs_finished(3, 4), running]))
        self.assertEqual(shepherd.Check("PENDING", RUN, 0.0), shepherd.required_check([running]))
        ended = {**running, "status": "COMPLETED", "conclusion": ""}
        self.assertEqual(shepherd.Check("UNKNOWN", RUN, 1.0), shepherd.required_check([ended]))

    def test_nothing_is_updated_while_an_armed_current_pull_request_is_green_or_pending(self) -> None:
        for state in ("SUCCESS", "PENDING"):
            with self.subTest(state=state):
                gh = StubGh([pull(12, state=state), pull(13, merge="BEHIND"), pull(14, merge="BEHIND")])
                _, lines = self.tick(gh)
                self.assertEqual([], gh.writes())
                self.assertTrue(self.line(lines, "PR #13 ").endswith(" next-tick"))
                self.assertTrue(self.line(lines, "PR #14 ").endswith(" next-tick"))
        # An unarmed one is armed by the same tick, so it merges next too.
        gh = StubGh([pull(12, armed=False), pull(13, merge="BEHIND")])
        self.tick(gh)
        self.assertEqual([["pr", "merge", "12", "--repo", REPOSITORY, "--auto", "--squash"]], gh.writes())

    def test_a_current_pull_request_that_cannot_merge_next_does_not_stop_the_update(self) -> None:
        held = {**pull(12), "body": "Shepherd-hold: owes a citation"}
        gh = StubGh(
            [held, pull(15, merge="DIRTY"), pull(16, state="MISSING"), pull(17, armed=False), pull(18, merge="BEHIND")],
            failing=["pr merge"],
        )
        _, lines = self.tick(gh)
        self.assertEqual(
            [["pr", "update-branch", "18", "--repo", REPOSITORY]],
            [call for call in gh.writes() if call[1] == "update-branch"],
        )
        self.assertTrue(self.line(lines, "PR #18 ").endswith(" updated"))

    def test_a_behind_pull_request_that_is_not_armed_is_not_updated(self) -> None:
        held = {**pull(10, merge="BEHIND"), "body": "Shepherd-hold: owes a citation"}
        gh = StubGh([held, pull(11, merge="BEHIND", armed=False)], failing=["pr merge"])
        _, lines = self.tick(gh)
        self.assertEqual([], [call for call in gh.writes() if call[1] == "update-branch"])
        self.assertTrue(self.line(lines, "PR #10 ").endswith(" none"))
        self.assertTrue(self.line(lines, "PR #11 ").endswith(" none"))

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

    def test_a_pull_request_whose_merge_state_is_unknown_is_updated_when_git_shows_it_behind(self) -> None:
        first, after = self.branches_behind_and_current()
        self.push_branch(first, "fix/topic-41")
        self.push_branch(after, "fix/topic-42")
        gh = StubGh(
            [
                pull(41, merge="UNKNOWN", head=first),
                # Neither may merge next, so neither stops the update.
                pull(42, merge="UNKNOWN", head=after, state="MISSING"),
                pull(43, merge="UNKNOWN", head="c" * 40, state="MISSING"),
            ]
        )
        status, lines = self.tick(gh)
        self.assertEqual(0, status)
        self.assertEqual([["pr", "update-branch", "41", "--repo", REPOSITORY]], gh.writes())
        self.assertEqual(
            f"PR #41 fix/topic-41 UNKNOWN auto-merge on head {first[:7]} Validation SUCCESS updated",
            self.line(lines, "PR #41 "),
        )
        self.assertTrue(self.line(lines, "PR #42 ").endswith(" none"))
        self.assertTrue(self.line(lines, "PR #43 ").endswith(" none"))

    def test_a_dry_run_reads_an_unknown_merge_state_from_the_fetched_heads(self) -> None:
        first, _ = self.branches_behind_and_current()
        gh = StubGh([pull(41, merge="UNKNOWN", head=first)])
        _, lines = self.tick(gh, dry_run=True)
        self.assertEqual([], gh.writes())
        self.assertTrue(self.line(lines, "PR #41 ").endswith(" would-update"))

    def test_other_test_paths_hold_nothing(self) -> None:
        self.assertIsNone(shepherd.holding_path(["tests/validation_notes.md", "tests/run_validation.pyc", "x.py"]))
        self.assertEqual("tests/validation/x.py", shepherd.holding_path(["a", "tests/validation/x.py"]))


class OutOfStepTest(FixtureTestCase):
    def test_a_recorded_head_behind_its_branch_is_named_and_neither_judged_nor_updated(self) -> None:
        first, after = self.branches_behind_and_current()
        self.push_branch(after, "fix/topic-44")
        self.push_branch(first, "fix/topic-45")
        gh = StubGh(
            [pull(44, merge="BEHIND", state="FAILURE", head=first), pull(45, merge="BEHIND", head=first)],
            runs={RUN: run_view([job("Windows shard 2/4", "cancelled", None)], head=first)},
        )
        status, lines = self.tick(gh)
        self.assertEqual(1, status)
        self.assertEqual(
            f"OUT_OF_STEP #44 fix/topic-44 head {first[:7]} branch {after[:7]}", self.line(lines, "OUT_OF_STEP ")
        )
        self.assertEqual([["pr", "update-branch", "45", "--repo", REPOSITORY]], gh.writes())
        self.assertTrue(self.line(lines, "PR #44 ").endswith(" none"))
        self.assertFalse(any(line.startswith(("JUDGED", "RERUN", "WOULD_RERUN", "DEFECT")) for line in lines))
        self.assertFalse(any(call[1:3] == ["run", "view"] for call in gh.calls))

    def test_a_branch_from_another_repository_is_not_compared_with_origin_s(self) -> None:
        first, after = self.branches_behind_and_current()
        self.push_branch(after, "fix/topic-46")
        _, lines = self.tick(StubGh([pull(46, head=first, cross=True)]), dry_run=True)
        self.line(lines, "NOT_ADMITTED #46 ")
        self.assertFalse(any(line.startswith("OUT_OF_STEP") for line in lines))

    def test_origin_s_branches_that_cannot_be_read_end_the_tick(self) -> None:
        missing = "https://github.com/owner/missing"
        git("remote", "set-url", "origin", missing, directory=self.hub)
        git("config", f"url.{(self.root / 'missing.git').as_posix()}.insteadOf", missing, directory=self.hub)
        status, lines = self.tick(StubGh([pull(47)]), dry_run=True)
        self.assertEqual(1, status)
        self.assertTrue(lines[-1].startswith("FAILED cannot read origin's branches: "), lines)


class ShapeTest(unittest.TestCase):
    """What GitHub returns in a shape the shepherd does not know is the malformed error naming it, never a verdict."""

    def setUp(self) -> None:
        self.commands: list[list[str]] = []

    def malformed(self, reason: str, call: Callable[..., object], *arguments: object) -> None:
        with self.assertRaises(GitHubError) as raised:
            call(*arguments)
        self.assertEqual(("malformed", f"GitHub returned {reason}"), (raised.exception.kind, str(raised.exception)))

    def test_a_pull_request_member_of_another_kind_is_malformed(self) -> None:
        listed = pull(20)
        check = rollup("FAILURE")[1]
        cases = {
            "number": (True, "a pull request whose number is not an integer"),
            "number ": ("20", "a pull request whose number is not an integer"),
            "number  ": (None, "a pull request without its number, branch, or head"),
            "headRefName ": (None, "a pull request without its number, branch, or head"),
            "headRefOid ": (None, "a pull request without its number, branch, or head"),
            "headRefName": (["fix/topic-20"], "a pull request whose headRefName is not text"),
            "headRefOid": (7, "a pull request whose headRefOid is not text"),
            "files": ("docs/a.md", "a pull request's files that is not a list of objects"),
            "files ": ([{"additions": 1}], "a pull request file without its path"),
            "isDraft": ("true", "a pull request whose isDraft is not a boolean"),
            "isCrossRepository": (1, "a pull request whose isCrossRepository is not a boolean"),
            "autoMergeRequest": ("SQUASH", "a pull request whose autoMergeRequest is not an object"),
            "mergeStateStatus": ({"state": "CLEAN"}, "a pull request whose mergeStateStatus is not text"),
            "body": (["Closes #1"], "a pull request whose body is not text"),
            "statusCheckRollup": ("FAILURE", "a check rollup that is not a list of objects"),
            "statusCheckRollup ": ([{**check, "name": 5}], "a check whose name is not text"),
            "statusCheckRollup  ": ([{**check, "detailsUrl": 1}], "a check whose detailsUrl is not text"),
            "statusCheckRollup   ": ([{**check, "conclusion": {"value": 1}}], "a check whose conclusion is not text"),
        }
        for key, (value, reason) in cases.items():
            with self.subTest(member=key, value=value):
                self.malformed(reason, shepherd.pull_of, {**listed, key.strip(): value})

    def test_a_pull_request_without_auto_merge_status_checks_or_body_still_reads(self) -> None:
        found = shepherd.pull_of({**pull(20, armed=False), "statusCheckRollup": None, "body": None})
        self.assertEqual((False, shepherd.Check("MISSING", None), None), (found.armed, found.check, found.hold))
        listed = pull(20)
        del listed["mergeStateStatus"]
        self.assertEqual("UNKNOWN", shepherd.pull_of(listed).merge_state)

    def test_a_run_with_no_attempt_is_its_first_and_a_nameless_leg_is_named_so(self) -> None:
        nameless = {
            key: value for key, value in job("unused", "failure", failed_step="Set up job").items() if key != "name"
        }
        run = {key: value for key, value in run_view([nameless]).items() if key != "attempt"}
        found = shepherd.pull_of(pull(20, state="FAILURE"))
        judged = shepherd.judge(self.client(run, [{"databaseId": RUN}]), "owner/repo", found)
        self.assertEqual((1, "unnamed leg", "service"), (judged.attempt, judged.leg, judged.reason))

    def test_a_failed_validation_leg_reads_its_log_by_the_job_id(self) -> None:
        failed = job("Linux shard 1/4", "failure", number=8)
        found = shepherd.pull_of(pull(20, state="FAILURE"))
        shepherd.judge(self.client(run_view([failed]), [{"databaseId": RUN}]), "owner/repo", found)
        logs = [command for command in self.commands if "--log-failed" in command]
        self.assertEqual(
            [["run", "view", "--repo", "owner/repo", "--job", "8", "--log-failed"]], [log[-7:] for log in logs]
        )

    def client(self, run: object = None, runs: Sequence[object] = ()) -> GitHubClient:
        """gh answering a run view with `run`, a run listing with `runs`, and a failed leg's log with nothing."""

        def answer(command: Sequence[str]) -> CommandResult:
            self.commands.append(list(command))
            if "--log-failed" in command:
                return CommandResult(0, "", "")
            return CommandResult(0, json.dumps(run if "view" in command else list(runs)), "")

        return GitHubClient(answer, backoff=NO_WAIT)

    def test_a_workflow_run_member_of_another_kind_is_malformed(self) -> None:
        failed = job("Linux shard 1/4", "failure")
        cases: list[tuple[dict[str, Any], str]] = [
            ({"jobs": None}, "a workflow run's jobs that is not a list of objects"),
            ({"jobs": []}, "a workflow run that lists no jobs"),
            ({"headSha": 5}, "a workflow run whose headSha is not text"),
            ({"headSha": None}, "a workflow run without its head"),
            ({"attempt": "2"}, "a workflow run whose attempt is not an integer"),
            ({"attempt": True}, "a workflow run whose attempt is not an integer"),
            ({"conclusion": ["failure"]}, "a workflow run whose conclusion is not text"),
            ({"jobs": [{**failed, "conclusion": {"value": 1}}]}, "a job whose conclusion is not text"),
            ({"jobs": [{**failed, "name": 5}]}, "a job whose name is not text"),
            ({"jobs": [{**failed, "steps": "Validate"}]}, "a job's steps that is not a list of objects"),
            ({"jobs": [{**failed, "steps": [{"name": 3, "conclusion": "success"}]}]}, "a step whose name is not text"),
            (
                {"jobs": [{**failed, "steps": [{"name": "Run repository validation", "conclusion": 0}]}]},
                "a step whose conclusion is not text",
            ),
            ({"jobs": [{**failed, "databaseId": "8"}]}, "a job whose databaseId is not an integer"),
            ({"jobs": [{**failed, "databaseId": True}]}, "a job whose databaseId is not an integer"),
            ({"jobs": [{**failed, "databaseId": None}]}, "a failed job without its id"),
        ]
        for change, reason in cases:
            with self.subTest(change=change):
                run = {**run_view([failed]), **change}
                found = shepherd.pull_of(pull(20, state="FAILURE"))
                self.malformed(reason, shepherd.judge, self.client(run, [{"databaseId": RUN}]), "owner/repo", found)

    def test_a_merged_listing_of_another_shape_is_malformed(self) -> None:
        merged = {"number": 5, "headRefName": "fix/done", "headRefOid": HEAD}
        for listing, reason in (
            (["5"], "a merged pull request listing that is not a list of objects"),
            ([{**merged, "number": "5"}], "a merged pull request whose number is not an integer"),
            ([{**merged, "number": True}], "a merged pull request whose number is not an integer"),
            ([{**merged, "headRefName": ["fix/done"]}], "a merged pull request whose headRefName is not text"),
            ([{**merged, "headRefOid": 7}], "a merged pull request whose headRefOid is not text"),
            ([{"headRefName": "fix/done"}], "a merged pull request without its number or branch"),
        ):
            with self.subTest(listing=listing):
                self.malformed(reason, shepherd.read_merged, self.client(runs=listing), "o/r")
        found = shepherd.read_merged(self.client(runs=[merged, {"number": 6, "headRefName": "fix/two"}]), "o/r")
        self.assertEqual([shepherd.Merged(5, "fix/done", HEAD), shepherd.Merged(6, "fix/two", "")], found)

    def test_a_run_listing_of_another_shape_is_malformed(self) -> None:
        for listing, reason in (
            (["7001"], "a workflow run listing that is not a list of objects"),
            ([{"databaseId": "7001"}], "a workflow run without its integer id"),
            ([{"databaseId": True}], "a workflow run without its integer id"),
        ):
            with self.subTest(listing=listing):
                self.malformed(reason, shepherd.newest_run, self.client(runs=listing), "o/r", HEAD)
        self.assertEqual(0, shepherd.newest_run(self.client(runs=[]), "o/r", HEAD))


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

    def test_a_cancelled_run_a_newer_run_on_the_same_head_replaced_is_superseded(self) -> None:
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                cancelled = run_view([job("Windows shard 1/4", "cancelled", None)], conclusion="cancelled")
                gh = StubGh(
                    [pull(20, state="CANCELLED")],
                    runs={
                        RUN: {**cancelled, "attempt": attempt},
                        RUN + 1: run_view([job("Windows shard 1/4", "success", None)], conclusion="success"),
                    },
                )
                status, lines = self.tick(gh)
                self.assertEqual(0, status)
                self.assertEqual(
                    f"JUDGED #20 run {RUN} attempt {attempt} head aaaaaaa superseded by run {RUN + 1}",
                    self.line(lines, "JUDGED "),
                )
                self.assertEqual([], gh.writes())
                self.assertFalse(any(line.startswith(("RERUN", "DEFECT")) for line in lines))

    def test_a_run_id_past_the_digit_limit_or_in_other_digits_is_malformed(self) -> None:
        # The pattern's `\d` admits an Arabic-Indic three, which is no run id either.
        for run in ("1" * 5000, chr(0x0663)):
            url = f"https://github.com/o/r/actions/runs/{run}/job/2"
            with self.subTest(run=run[:4]), self.assertRaises(GitHubError) as raised:
                shepherd.run_of({"detailsUrl": url})
            self.assertEqual(
                ("malformed", f"GitHub returned a run id that is not at most {whole_number.DIGIT_LIMIT} ASCII digits"),
                (raised.exception.kind, str(raised.exception)),
            )
        self.assertEqual(RUN, shepherd.run_of({"detailsUrl": f"https://github.com/o/r/actions/runs/{RUN}/job/2"}))
        self.assertIsNone(shepherd.run_of({"detailsUrl": None}))

    def test_the_newest_run_on_a_head_decides_its_check(self) -> None:
        newer_pending = [*rollup("CANCELLED"), *rollup("PENDING", RUN + 1)]
        self.assertEqual(shepherd.Check("PENDING", RUN + 1), shepherd.required_check(newer_pending))
        # A rerun of the older run starts its required check after the newer run's.
        older = [{**entry, "startedAt": "2026-10-17"} for entry in rollup("CANCELLED")]
        self.assertEqual(
            shepherd.Check("SUCCESS", RUN + 1, 1.0), shepherd.required_check([*older, *rollup(run=RUN + 1)])
        )
        entry = {**pull(20), "statusCheckRollup": newer_pending}
        gh = StubGh([entry])
        status, lines = self.tick(gh)
        self.assertEqual(0, status)
        self.assertTrue(self.line(lines, "PR #20 ").endswith(" Validation PENDING none"))
        self.assertFalse(any(call[1:3] == ["run", "view"] for call in gh.calls))

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

    def test_a_suite_whose_retry_failed_is_a_defect(self) -> None:
        retried = ("tests/x.py", pool_report(ended, failed).split("\n"))
        status, lines, gh = self.red([job("Windows shard 2/4", "failure")], {1: failed_log(retried)})
        self.assertEqual(1, status)
        self.assertEqual(
            'DEFECT #20 fix/topic-20 leg "Windows shard 2/4" check "tests/x.py"', self.line(lines, "DEFECT ")
        )
        self.assertEqual([], gh.writes())

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


class SourceTest(unittest.TestCase):
    """The runner's report wording and the workflow's step names the script reads, held to their sources."""

    def runner_log(self) -> str:
        """A leg's log of the runner's own report: a failed policy check, a failed suite, and the closing line."""

        class Policy(unittest.TestCase):
            def test_policy(self) -> None:
                self.fail("a policy failed")

        out = io.StringIO()
        result = run_validation.run_policies(unittest.TestSuite([Policy("test_policy")]), False, out)
        suite = job_pool.Job("tests/x.py", "tests/x.py", 1, lambda: None)
        failures = [job_pool.Failure(suite, "Command failed with exit code 1: python -B tests/x.py\nE   boom")]
        run = run_validation.Run("Full run.", "1/4", {}, 1.0)
        run_validation.report_run(result, job_pool.PoolResult(failures, []), 1, run, out, {})
        return "".join(PREFIX + line + "\n" for line in out.getvalue().split("\n"))

    def test_the_runner_s_report_is_read_as_the_script_reads_it(self) -> None:
        lines = shepherd.log_lines(self.runner_log())
        self.assertEqual("test_policy", shepherd.first_failing_check(lines))
        blocks = shepherd.failure_blocks(lines)
        self.assertEqual(["tests/x.py"], [label for label, _ in blocks])
        self.assertEqual("E   boom", [line for line in blocks[0][1] if line][-1])
        self.assertTrue(any(shepherd.CLOSING.search(line) for line in lines), lines)

    def test_the_validation_steps_are_the_workflow_s_steps_that_run_the_runner(self) -> None:
        workflow = (Path(__file__).resolve().parents[4] / ".github" / "workflows" / "validate.yml").read_text("utf-8")
        steps = re.split(r"^\s*- name: ", workflow, flags=re.MULTILINE)[1:]
        running = {step.split("\n", 1)[0].strip() for step in steps if "tests/run_validation.py" in step}
        self.assertEqual(shepherd.VALIDATION_STEPS, running)


class LogTest(unittest.TestCase):
    def test_log_lines_drop_github_s_prefixes_and_terminal_escapes(self) -> None:
        text = PREFIX + "\x1b[31mFAIL\x1b[0m one\n2026-10-16T05:51:51Z two\nthree\twith a tab\n"
        self.assertEqual(["FAIL one", "two", "three\twith a tab"], shepherd.log_lines(text))

    def test_a_block_ends_at_the_next_rule_or_the_closing_line(self) -> None:
        lines = shepherd.log_lines(failed_log(SILENT_BLOCK, MYPY_BLOCK))
        blocks = shepherd.failure_blocks(lines)
        self.assertEqual([SILENT_BLOCK[0], MYPY_BLOCK[0]], [label for label, _ in blocks])
        self.assertEqual(["silent-exit", None], [shepherd.block_reason(body) for _, body in blocks])

    def test_the_pool_s_termination_on_every_attempt_is_a_termination(self) -> None:
        report = pool_report(ended, ended)
        self.assertEqual("termination", shepherd.block_reason(shepherd.log_lines(report)))

    def test_the_pool_s_failed_retry_is_judged_by_the_retry_s_report(self) -> None:
        self.assertIsNone(shepherd.block_reason(shepherd.log_lines(pool_report(ended, failed))))
        self.assertEqual("silent-exit", shepherd.block_reason(shepherd.log_lines(pool_report(ended, exited_silently))))

    def test_a_lone_retry_note_is_read_without_its_failure_line(self) -> None:
        ending = (
            f"python.exe was ended with status 0xC0000142 before its first line of output: {' '.join(SUITE_COMMAND)}"
        )
        self.assertIsNone(shepherd.block_reason([f"{shepherd.RETRIED}{ending}"]))

    def test_a_retry_that_failed_with_an_empty_report_is_judged_by_that_report_alone(self) -> None:
        # The ending's command names a service marker, which only the retry's own report may decide on.
        body = [f"{shepherd.RETRIED}{ENDING_NAMING_A_SERVICE}", shepherd.RETRY_FAILED, ""]
        self.assertIsNone(shepherd.block_reason(body))

    def test_a_retried_block_is_not_a_failure(self) -> None:
        lines = shepherd.log_lines(
            failed_log(extra=["", RULE, "RETRIED tests/x.py", DASHES, "Ended before its first line of output"])
        )
        self.assertEqual([], shepherd.failure_blocks(lines))


class MergedTest(FixtureTestCase):
    def test_the_worktrees_are_read_through_skill_core(self) -> None:
        with mock.patch.object(shepherd, "list_worktrees", wraps=list_worktrees) as listed:
            status, _ = self.tick(StubGh(), dry_run=True)
        self.assertEqual(0, status)
        listed.assert_called_once()

    def test_a_worktree_whose_branch_merged_is_named_for_retirement(self) -> None:
        done, busy, reused = (self.root / name for name in ("done", "busy", "reused"))
        git("worktree", "add", "-q", "-b", "fix/done", str(done), directory=self.hub)
        git("worktree", "add", "-q", "-b", "fix/busy", str(busy), directory=self.hub)
        git("worktree", "add", "-q", "-b", "fix/topic-30", str(reused), directory=self.hub)
        pushed = self.commit_in(done, "done")
        open_head = self.commit_in(reused, "reused")
        merged = [
            {"number": 5, "headRefName": "fix/done", "headRefOid": OTHER},
            {"number": 8, "headRefName": "fix/done", "headRefOid": pushed},
            {"number": 6, "headRefName": "fix/topic-30", "headRefOid": open_head},
            {"number": 7, "headRefName": "main"},
        ]
        status, lines = self.tick(StubGh([pull(30)], merged=merged), dry_run=True)
        self.assertEqual(0, status)
        self.assertEqual(
            [f'MERGED #8 worktree "{done.as_posix()}" branch fix/done'],
            [line for line in lines if line.startswith("MERGED")],
        )

    def commit_in(self, directory: Path, name: str) -> str:
        (directory / f"{name}.md").write_text(f"{name}\n", encoding="utf-8")
        git("add", f"{name}.md", directory=directory)
        git("commit", "-q", "-m", name, directory=directory)
        return git("rev-parse", "HEAD", directory=directory)

    def add_worktree(self, name: str, branch: str) -> Path:
        path = self.root / name
        git("worktree", "add", "-q", "-b", branch, str(path), directory=self.hub)
        return path

    def merged_lines(self, gh: StubGh) -> list[str]:
        status, lines = self.tick(gh, dry_run=True)
        self.assertEqual(0, status, lines)
        return [line for line in lines if line.startswith("MERGED")]

    def test_a_worktree_on_another_branch_is_matched_by_the_merged_head_commit(self) -> None:
        carried, unrelated, fresh, reopened = (
            self.add_worktree(name, f"claude/{name}") for name in ("carried", "unrelated", "fresh", "reopened")
        )
        pushed = self.commit_in(carried, "carried")
        self.commit_in(unrelated, "unrelated")
        again = self.commit_in(reopened, "reopened")
        main = git("rev-parse", "main", directory=self.hub)
        # GitHub's update-branch merge on the pushed branch, which the worktree never pulled.
        tree = git("rev-parse", f"{pushed}^{{tree}}", directory=self.hub)
        updated = git("commit-tree", tree, "-p", pushed, "-p", main, "-m", "Merge main", directory=self.hub)
        merged = [
            {"number": 9, "headRefName": "fix/pushed", "headRefOid": updated},
            {"number": 11, "headRefName": "fix/reopened", "headRefOid": again},
            {"number": 12, "headRefName": "fix/never-fetched", "headRefOid": "c" * 40},
        ]
        self.assertEqual(
            [f'MERGED #9 worktree "{carried.as_posix()}" branch claude/carried'],
            self.merged_lines(StubGh([pull(31, head=again)], merged=merged)),
        )
        self.assertNotIn(fresh.as_posix(), " ".join(self.merged_lines(StubGh(merged=merged))))

    def assert_a_reused_name_is_not_named(self, own_commit: bool) -> None:
        """A worktree created on a merged branch's name, at main's head or with a commit of its own, is never named,
        and the same worktree at the head that pull request merged is."""
        reused = self.add_worktree("reused", "fix/reused")
        if own_commit:
            self.commit_in(reused, "reused")
        main = git("rev-parse", "main", directory=self.hub)
        tree = git("rev-parse", f"{main}^{{tree}}", directory=self.hub)
        # The head the earlier pull request of that name merged, which the new branch never held.
        earlier = git("commit-tree", tree, "-p", main, "-m", "earlier", directory=self.hub)
        merged = [{"number": 14, "headRefName": "fix/reused", "headRefOid": earlier}]
        self.assertEqual([], self.merged_lines(StubGh(merged=merged)))
        git("reset", "-q", "--hard", earlier, directory=reused)
        self.assertEqual(
            [f'MERGED #14 worktree "{reused.as_posix()}" branch fix/reused'], self.merged_lines(StubGh(merged=merged))
        )

    def test_a_worktree_at_main_that_reuses_a_merged_branch_s_name_is_not_named(self) -> None:
        self.assert_a_reused_name_is_not_named(own_commit=False)

    def test_a_worktree_with_its_own_commit_that_reuses_a_merged_branch_s_name_is_not_named(self) -> None:
        self.assert_a_reused_name_is_not_named(own_commit=True)

    def test_a_worktree_that_reuses_the_name_of_a_merged_head_git_does_not_hold_is_not_named(self) -> None:
        unfetched = self.add_worktree("unfetched", "fix/unfetched")
        merged = [{"number": 16, "headRefName": "fix/unfetched", "headRefOid": "c" * 40}]
        self.assertNotIn(unfetched.as_posix(), " ".join(self.merged_lines(StubGh(merged=merged))))

    def test_a_local_branch_that_reuses_a_merged_branch_s_name_is_not_named_for_deletion(self) -> None:
        carried = self.add_worktree("carried", "claude/carried")
        pushed = self.commit_in(carried, "carried")
        git("branch", "fix/carried", "main", directory=self.hub)
        merged = [{"number": 17, "headRefName": "fix/carried", "headRefOid": pushed}]
        self.assertEqual(
            [f'MERGED #17 worktree "{carried.as_posix()}" branch claude/carried'],
            self.merged_lines(StubGh(merged=merged)),
        )

    def test_a_local_branch_the_merged_head_follows_through_update_merges_is_named(self) -> None:
        carried = self.add_worktree("carried", "claude/carried")
        pushed = self.commit_in(carried, "carried")
        main = git("rev-parse", "main", directory=self.hub)
        tree = git("rev-parse", f"{pushed}^{{tree}}", directory=self.hub)
        updated = git("commit-tree", tree, "-p", pushed, "-p", main, "-m", "Merge main", directory=self.hub)
        git("branch", "fix/pushed", pushed, directory=self.hub)
        merged = [{"number": 18, "headRefName": "fix/pushed", "headRefOid": updated}]
        self.assertEqual(
            [f'MERGED #18 worktree "{carried.as_posix()}" branch claude/carried branch fix/pushed'],
            self.merged_lines(StubGh(merged=merged)),
        )

    def test_the_local_branches_of_several_carrying_pull_requests_are_named_newest_first(self) -> None:
        both = self.add_worktree("both", "claude/both")
        pushed = self.commit_in(both, "both")
        git("branch", "fix/first", pushed, directory=self.hub)
        git("branch", "fix/second", pushed, directory=self.hub)
        merged = [
            {"number": 19, "headRefName": "fix/first", "headRefOid": pushed},
            {"number": 20, "headRefName": "fix/second", "headRefOid": pushed},
        ]
        self.assertEqual(
            [f'MERGED #20 worktree "{both.as_posix()}" branch claude/both branch fix/second branch fix/first'],
            self.merged_lines(StubGh(merged=merged)),
        )

    def test_a_worktree_matched_by_commit_names_the_pull_request_s_local_branch_too(self) -> None:
        both = self.add_worktree("both", "claude/both")
        pushed = self.commit_in(both, "both")
        git("branch", "fix/both", pushed, directory=self.hub)
        merged = [{"number": 10, "headRefName": "fix/both", "headRefOid": pushed}]
        self.assertEqual(
            [f'MERGED #10 worktree "{both.as_posix()}" branch claude/both branch fix/both'],
            self.merged_lines(StubGh(merged=merged)),
        )
        # A head branch another worktree has checked out is that worktree's to name.
        holder = self.add_worktree("holder", "fix/held")
        git("reset", "-q", "--hard", pushed, directory=holder)
        merged = [{"number": 13, "headRefName": "fix/held", "headRefOid": pushed}]
        self.assertEqual(
            [
                f'MERGED #13 worktree "{both.as_posix()}" branch claude/both',
                f'MERGED #13 worktree "{holder.as_posix()}" branch fix/held',
            ],
            self.merged_lines(StubGh(merged=merged)),
        )

    def test_no_worktree_is_named_while_git_does_not_hold_origin_s_main(self) -> None:
        # Origin's main moved since the last fetch, so the commits it gained would count as each merged head's own.
        carried = self.add_worktree("carried", "claude/carried")
        pushed = self.commit_in(carried, "carried")
        newer = self.advance_origin()
        merged = [{"number": 9, "headRefName": "fix/pushed", "headRefOid": pushed}]
        status, lines = self.tick(StubGh(merged=merged), dry_run=True)
        self.assertEqual(1, status)
        self.assertEqual([], [line for line in lines if line.startswith("MERGED ")])
        self.assertEqual(
            f"MERGED_FAILED git does not hold origin's main {newer[:7]}, so a merged head's own commits cannot be told "
            "from main's; fetch origin in the hub",
            self.line(lines, "MERGED_FAILED "),
        )

    def answering(self, word: str, answer: GitResult) -> GitClient:
        """Real git, but `answer` for the command that holds `word`."""

        def run(command: Sequence[str], timeout: float) -> GitResult:
            return answer if word in command else subprocess_runner(command, timeout)

        return GitClient(run)

    def test_each_lookup_names_a_git_failure_instead_of_reading_it_as_absent(self) -> None:
        broken = GitResult(128, "", "fatal: bad config line 1 in file .git/config\n")
        head = git("rev-parse", "HEAD", directory=self.hub)
        for name, call in (
            ("the origin URL", lambda: shepherd.locate(self.answering("--get", broken), self.hub)),
            ("the hub's branch", lambda: shepherd.fast_forward(self.answering("symbolic-ref", broken), self.hub, True)),
            ("origin's main", lambda: shepherd.own_history(self.answering("--verify", broken), self.hub, [head], head)),
        ):
            with self.subTest(name), self.assertRaises(GitError) as raised:
                call()
            self.assertIn("bad config line 1", str(raised.exception))

    def test_a_hub_on_a_detached_head_is_named_so(self) -> None:
        git("switch", "--quiet", "--detach", directory=self.hub)
        self.assertEqual(
            "not fast-forwarded: it is on a detached HEAD, not main", shepherd.fast_forward(GitClient(), self.hub, True)
        )

    def test_a_pull_is_behind_only_when_git_answers_that_main_is_no_ancestor(self) -> None:
        unknown = dataclasses.replace(shepherd.pull_of(pull(10)), merge_state="UNKNOWN")
        for answer, behind in (
            (GitResult(1, "", ""), True),
            (GitResult(0, "", ""), False),
            (GitResult(1, "", "warning: ignoring broken ref refs/heads/main\n"), False),
            (GitResult(128, "", "fatal: Not a valid commit name"), False),
        ):
            with self.subTest(answer=answer):
                self.assertEqual(
                    behind, shepherd.is_behind(self.answering("merge-base", answer), self.hub, unknown, "a" * 40)
                )

    def test_a_merged_head_s_own_commits_are_refused_without_origin_s_main(self) -> None:
        head = git("rev-parse", "HEAD", directory=self.hub)
        with self.assertRaises(shepherd.ShepherdError) as raised:
            shepherd.own_history(GitClient(), self.hub, [head], None)
        self.assertEqual(
            "origin has no main, so a merged head's own commits cannot be told from main's", str(raised.exception)
        )
        self.assertEqual({}, shepherd.own_history(GitClient(), self.hub, [], None))

    def test_a_worktree_an_open_draft_carries_is_never_named(self) -> None:
        reused = self.add_worktree("reused", "fix/reused")
        merged = [{"number": 6, "headRefName": "fix/reused", "headRefOid": OTHER}]
        status, lines = self.tick(StubGh([pull(40, draft=True, branch="fix/reused")], merged=merged), dry_run=True)
        self.assertEqual(0, status, lines)
        self.assertNotIn(reused.as_posix(), " ".join(lines))
        self.assertEqual("REMAINING 0", lines[-1])

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


REPORT = "reviews/pull requests/review.md"


def reviewed(
    head: str, version: int | None = 1, verdict: str = "APPROVE", report: str | None = REPORT
) -> premerge_review.Reviewed:
    return premerge_review.Reviewed(head, verdict, version, report)


class PremergeReviewTest(FixtureTestCase):
    """The pre-merge review step, with the archive's latest review of each pull request given by a fake reader."""

    def review_tick(
        self, gh: StubGh, records: Mapping[int, premerge_review.Reviewed | Exception]
    ) -> tuple[int, list[str], list[str]]:
        opened: list[str] = []

        def reviews(repository: str) -> premerge_review.Reader:
            opened.append(repository)

            def read(number: int) -> premerge_review.Reviewed | None:
                found = records.get(number)
                if isinstance(found, Exception):
                    raise found
                return found

            return read

        lines: list[str] = []
        client = GitHubClient(gh, backoff=NO_WAIT, sleeper=lambda _: None)
        status = shepherd.tick(GitClient(), client, self.hub, dry_run=True, emit=lines.append, reviews=reviews)
        return status, lines, opened

    def topic(self) -> tuple[str, str, str]:
        """A branch's own commit on the hub's first commit, a merge of origin's newer main into it, and another
        commit of its own after that merge."""
        first, after = self.branches_behind_and_current()
        git("checkout", "-q", "-b", "topic", first, directory=self.hub)
        (self.hub / "own.md").write_text("own\n", encoding="utf-8")
        git("add", "own.md", directory=self.hub)
        git("commit", "-q", "-m", "own", directory=self.hub)
        own = git("rev-parse", "HEAD", directory=self.hub)
        git("merge", "-q", "--no-edit", after, directory=self.hub)
        merged = git("rev-parse", "HEAD", directory=self.hub)
        (self.hub / "more.md").write_text("more\n", encoding="utf-8")
        git("add", "more.md", directory=self.hub)
        git("commit", "-q", "-m", "more", directory=self.hub)
        more = git("rev-parse", "HEAD", directory=self.hub)
        git("checkout", "-q", "main", directory=self.hub)
        return own, merged, more

    def test_each_head_is_named_reviewed_carried_or_due_and_due_ones_get_one_set_of_arguments(self) -> None:
        own, merged, more = self.topic()
        gh = StubGh([pull(20, head=own), pull(21, head=own), pull(22, head=merged), pull(23, head=more), pull(24)])
        records = {
            21: reviewed(own),
            22: reviewed(own, 2, "CHANGES_REQUESTED"),
            23: reviewed(own, None),
            24: reviewed(HEAD, None, report=None),
        }
        status, lines, opened = self.review_tick(gh, records)
        self.assertEqual(0, status, lines)
        self.assertEqual([REPOSITORY], opened)
        self.assertEqual(
            [
                f"REVIEW_DUE #20 fix/topic-20 head {own[:7]} first",
                f'REVIEWED #21 fix/topic-21 head {own[:7]} v1 APPROVE report "{REPORT}"',
                f'REVIEWED #22 fix/topic-22 head {merged[:7]} v2 CHANGES_REQUESTED report "{REPORT}" carried from '
                f"{own[:7]}",
                f"REVIEW_DUE #23 fix/topic-23 head {more[:7]} since {own[:7]}",
                f"REVIEWED #24 fix/topic-24 head {HEAD[:7]} legacy APPROVE report none",
                "REVIEW_ARGUMENTS --pull owner/repo#20 --re-review owner/repo#23 --scope auto",
            ],
            [line for line in lines if line.startswith("REVIEW")],
        )
        # After the PR lines, and before the count.
        self.assertGreater(lines.index(self.line(lines, "REVIEW_DUE #20 ")), lines.index(self.line(lines, "PR #24 ")))
        self.assertEqual("REMAINING 5", lines[-1])
        self.assertEqual([], gh.writes())

    def test_first_reviews_alone_take_no_scope(self) -> None:
        _, lines, _ = self.review_tick(StubGh([pull(20)]), {})
        self.assertEqual("REVIEW_ARGUMENTS --pull owner/repo#20", self.line(lines, "REVIEW_ARGUMENTS "))

    def test_a_head_that_is_out_of_step_in_conflict_red_missing_or_from_another_repository_is_not_reviewed(
        self,
    ) -> None:
        first, after = self.branches_behind_and_current()
        self.push_branch(after, "fix/topic-30")
        pulls = [
            pull(30, head=first),
            pull(31, merge="DIRTY"),
            pull(32, state="MISSING"),
            pull(33, cross=True),
            pull(34, state="CANCELLED"),
        ]
        runs = {RUN: run_view([job("Windows shard 2/4", "failure")])}
        _, lines, opened = self.review_tick(StubGh(pulls, runs=runs, logs={1: failed_log()}), {})
        self.assertEqual([], [line for line in lines if line.startswith("REVIEW")])
        self.assertEqual([], opened, "the archive is not opened when no head is to be reviewed")

    def test_a_pending_or_held_head_is_reviewed(self) -> None:
        held = {**pull(41), "body": "Shepherd-hold: owes a citation\n"}
        _, lines, _ = self.review_tick(StubGh([pull(40, state="PENDING"), held]), {})
        self.assertEqual(
            "REVIEW_ARGUMENTS --pull owner/repo#40 --pull owner/repo#41", self.line(lines, "REVIEW_ARGUMENTS ")
        )

    def test_an_unreadable_archive_or_record_is_named_and_changes_no_exit_status(self) -> None:
        unreadable = premerge_review.ReviewUnread("Review JSON payload hash mismatch")
        status, lines, _ = self.review_tick(StubGh([pull(50), pull(51)]), {50: unreadable})
        self.assertEqual(0, status, lines)
        self.assertEqual("REVIEW_UNREAD #50 Review JSON payload hash mismatch", self.line(lines, "REVIEW_UNREAD "))
        self.assertEqual("REVIEW_ARGUMENTS --pull owner/repo#51", self.line(lines, "REVIEW_ARGUMENTS "))

        def refused(repository: str) -> premerge_review.Reader:
            raise premerge_review.ReviewUnread(f"the code-review configuration does not configure {repository}")

        lines = []
        client = GitHubClient(StubGh([pull(50)]), backoff=NO_WAIT, sleeper=lambda _: None)
        status = shepherd.tick(GitClient(), client, self.hub, dry_run=True, emit=lines.append, reviews=refused)
        self.assertEqual(0, status, lines)
        self.assertEqual(
            ["REVIEW_UNREAD the code-review configuration does not configure owner/repo"],
            [line for line in lines if line.startswith("REVIEW")],
        )

    def test_a_head_git_cannot_relate_to_its_reviewed_head_is_due(self) -> None:
        own, merged, _ = self.topic()
        base = git("rev-parse", "origin/main", directory=self.hub)
        self.assertTrue(premerge_review.merges_only(GitClient(), self.hub, own, merged, base))
        self.assertFalse(premerge_review.merges_only(GitClient(), self.hub, own, merged, None))
        self.assertFalse(premerge_review.merges_only(GitClient(), self.hub, merged, own, base))
        self.assertFalse(premerge_review.merges_only(GitClient(), self.hub, "c" * 40, merged, base))
        self.assertFalse(premerge_review.merges_only(GitClient(), self.hub, own, merged, "c" * 40))

    def test_any_git_failure_carries_no_review(self) -> None:
        own, merged, _ = self.topic()
        base = git("rev-parse", "origin/main", directory=self.hub)

        def answering(word: str, answer: GitResult) -> GitClient:
            def run(command: Sequence[str], timeout: float) -> GitResult:
                return answer if word in command else subprocess_runner(command, timeout)

            return GitClient(run)

        for word, answer in (
            ("merge-base", GitResult(1, "", "warning: ignoring broken ref refs/heads/main\n")),
            ("merge-base", GitResult(128, "", "fatal: bad config line 1 in file .git/config\n")),
            ("rev-list", GitResult(128, "", "fatal: bad config line 1 in file .git/config\n")),
        ):
            with self.subTest(word=word, answer=answer):
                self.assertFalse(premerge_review.merges_only(answering(word, answer), self.hub, own, merged, base))
        clean = answering("merge-base", GitResult(0, "", ""))
        self.assertTrue(premerge_review.merges_only(clean, self.hub, own, merged, base), "a clean ancestor carries it")


class ArchiveReaderTest(unittest.TestCase):
    """The reader over a code-review configuration and archive written under a temporary folder."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(os.path.realpath(temporary.name))
        self.config = self.root / "config.json"
        patched = mock.patch.dict(os.environ, {"CODE_REVIEW_CONFIG": str(self.config)})
        patched.start()
        self.addCleanup(patched.stop)

    def configure(self, repository: str = "Owner/Repo") -> None:
        reviews = self.root / "reviews"
        config = {
            "schema_version": 1,
            "default_repository_set": "primary",
            "repository_sets": {"primary": [repository]},
            "repositories": {
                repository.lower(): {
                    "reviewer": {
                        "id": "generic",
                        "protocol_version": 1,
                        "trusted_ref": None,
                        "scope": "generic",
                        "manifest_path": None,
                    },
                    "checkout_path": str(self.root / "checkout"),
                }
            },
            "archive_root": str(reviews / "archive"),
            "local_mirror_root": None,
            "summary_root": str(reviews / "summaries"),
            "dashboard_file": str(reviews / "dashboard.md"),
            "runtime": "auto",
            "verdict_policy": {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
            "dashboard": {},
        }
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def test_a_missing_or_invalid_configuration_is_unread(self) -> None:
        with self.assertRaisesRegex(premerge_review.ReviewUnread, "^cannot read the code-review configuration: "):
            premerge_review.archive_reader(REPOSITORY)
        self.config.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(premerge_review.ReviewUnread, "^cannot read the code-review configuration: "):
            premerge_review.archive_reader(REPOSITORY)

    def test_a_repository_the_configuration_does_not_name_is_unread(self) -> None:
        self.configure("owner/other")
        with self.assertRaisesRegex(
            premerge_review.ReviewUnread, "^the code-review configuration does not configure owner/repo$"
        ):
            premerge_review.archive_reader(REPOSITORY)

    def test_a_pull_request_with_no_review_reads_as_none_whatever_the_identity_s_case(self) -> None:
        self.configure()
        self.assertIsNone(premerge_review.archive_reader(REPOSITORY)(20))

    def test_a_review_reads_as_its_head_verdict_and_version_and_an_unreadable_one_is_unread(self) -> None:
        self.configure()
        read = premerge_review.archive_reader(REPOSITORY)
        found = {"head_sha": HEAD, "verdict": "COMMENT", "version": 3, "source": "record", "report": REPORT}
        with mock.patch.object(premerge_review, "reviewed_head", return_value=found) as latest:
            self.assertEqual(reviewed(HEAD, 3, "COMMENT"), read(20))
        with mock.patch.object(premerge_review, "reviewed_head", return_value={**found, "report": None}):
            self.assertEqual(reviewed(HEAD, 3, "COMMENT", None), read(20))
        self.assertEqual((self.root / "reviews" / "archive", REPOSITORY, 20), latest.call_args.args)
        failing = mock.patch.object(
            premerge_review,
            "reviewed_head",
            side_effect=RecordError("Review JSON payload hash mismatch"),
        )
        with failing, self.assertRaisesRegex(premerge_review.ReviewUnread, "^Review JSON payload hash mismatch$"):
            read(20)


class MainTest(unittest.TestCase):
    def test_main_runs_one_tick_from_the_working_directory(self) -> None:
        with mock.patch.object(shepherd, "tick", return_value=0) as tick:
            self.assertEqual(0, shepherd.main(["--dry-run"]))
        self.assertIs(True, tick.call_args.kwargs["dry_run"])
        git_client, github_client, directory = tick.call_args.args
        self.assertIsInstance(git_client, GitClient)
        self.assertIsInstance(github_client, GitHubClient)
        self.assertEqual(Path.cwd(), directory)
        self.assertIs(print, tick.call_args.kwargs["emit"])
        self.assertIs(premerge_review.archive_reader, tick.call_args.kwargs["reviews"])
        with mock.patch.object(shepherd, "tick", return_value=1) as tick:
            self.assertEqual(1, shepherd.main([]))
        self.assertIs(False, tick.call_args.kwargs["dry_run"])

    def test_the_review_step_finds_skill_core_and_the_code_review_core_on_its_own(self) -> None:
        # A clean interpreter, with neither on sys.path, imports the step. Its own two statements put the code-review
        # core in front of skill-core, each at its folder in this checkout, ahead of the folder the step was found in;
        # the core's modules put skill-core in front again as they load, which leaves that order behind them.
        scripts = Path(premerge_review.__file__).resolve().parent
        skills = scripts.parents[3] / "skills"
        probe = "import sys; sys.path.insert(0, sys.argv[1]); import premerge_review; print(*sys.path, sep='\\n')"
        ran = subprocess.run(
            [sys.executable, "-B", "-I", "-c", probe, str(scripts)], capture_output=True, text=True, check=False
        )
        self.assertEqual(0, ran.returncode, ran.stderr)
        path = ran.stdout.splitlines()
        core = str(skills / "ejryan89-code-review-core" / "scripts")
        self.assertIn(core, path)
        self.assertEqual(
            [core, str(skills / "ejryan89-skill-core" / "scripts"), str(scripts)],
            path[path.index(core) : path.index(str(scripts)) + 1],
        )

    def test_an_unknown_argument_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            shepherd.main(["--rebase"])
        self.assertEqual(2, raised.exception.code)


def link_folder(target: Path, link: Path) -> None:
    """A real link to a folder: a junction on Windows, which needs no privilege, else a symbolic link."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_a_folder_and_a_link_to_it_are_one_path_in_either_order(tmp_path: Path) -> None:
    target, other = tmp_path / "hub", tmp_path / "other"
    target.mkdir()
    other.mkdir()
    link = tmp_path / "linked hub"
    link_folder(target, link)
    assert shepherd.same_path(link, target)
    assert shepherd.same_path(target, link)
    assert not shepherd.same_path(link, other)

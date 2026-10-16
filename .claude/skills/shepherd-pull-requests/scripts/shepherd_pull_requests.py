"""One tick of the merge shepherd over this repository's open pull requests, so the session that runs it only
messages the sessions that own them, judges what the script cannot, and hands each merge to repo-cleanup.

Run it from the hub on main. One tick:

- fast-forwards the hub to origin's main, when it runs in the hub itself, on main, with no tracked change; from a
  linked worktree it leaves the hub to the hub's own session;
- lists the open pull requests that are not drafts, with their merge state, auto-merge, head, and the state of the
  required check Validation on that head;
- arms squash auto-merge on each one that has none;
- names each pull request whose recorded head is not the commit its branch on origin holds, because GitHub missed a
  push, and neither judges nor updates it, since GitHub refuses to update a stale head;
- judges each red Validation check: it compares the head the check's run tested with the pull request's head, and the
  run with the newest pull request run on that head, first, since a red run on a superseded head, or one a newer run
  on the same head replaced, is no failure; then it reruns the failed legs, once per head, when every failure is a
  program's termination, a cancellation, a service failure, or a suite that exited with no output, and names any
  other failure as a defect;
- merges main into at most two pull requests behind main whose check is green or pending, oldest first, through
  GitHub's update-branch merge, never a rebase or a force-push; a pull request is behind when GitHub's merge state is
  BEHIND, or, while GitHub has not computed it (UNKNOWN), when origin's main is not an ancestor of its head; while a
  pull request that changes the validation policies or the runner's selection is open, every other pull request's
  update waits for it;
- names each worktree whose branch's pull request merged, for repo-cleanup to retire.

With --dry-run it lists and judges the same way and writes nothing: it reads origin's main instead of fetching, and
prints what it would arm, update, and rerun. Either way it reads origin's branches with ls-remote.

Every line is one fact:

    DRY_RUN <what is not written>      --dry-run: the first line
    REPOSITORY <owner/name>            the GitHub repository origin names
    HUB "<path>" <what happened>       fast-forwarded <old> to <new>, current at <commit>, would fast-forward <old>
                                       to <new>, or not fast-forwarded: <reason>
    HOLD #<number> changes <path>      a pull request that holds every other one's update while it is open
    PR #<number> <branch> <merge state> auto-merge <on|off> head <commit> Validation <state> <actions>
                                       one per pull request, oldest first; <merge state> is GitHub's, <state> is a
                                       conclusion of the newest run on the head, such as SUCCESS or FAILURE,
                                       PENDING, or MISSING, and <actions> is none or a comma-separated
                                       list of armed, updated, rerun (would-arm, would-update, and would-rerun with
                                       --dry-run), held, and next-tick, a BEHIND pull request past this tick's limit
    OUT_OF_STEP #<number> <branch> head <commit> branch <commit>
                                       a pull request whose recorded head is not its branch's commit on origin,
                                       neither judged nor updated until its owner pushes again
    JUDGED #<number> run <id> attempt <n> head <commit> current|superseded by <commit>|superseded by run <id>
                                       the head a red check's run tested, beside the pull request's own, or the newer
                                       pull request run on the same head that replaced it
    RERUN #<number> run <id> <reason> leg "<leg>"
                                       the failed legs rerun; <reason> is termination, cancelled, service, or
                                       silent-exit; WOULD_RERUN with --dry-run
    RERUN_SPENT #<number> run <id> attempt <n> <reason> leg "<leg>"
                                       a rerun case on a head already rerun once, left to the shepherd's judgment
    DEFECT #<number> <branch> leg "<leg>" check "<check>"
                                       a failure for the session that owns the pull request: the leg and the first
                                       failing check
    PR_FAILED #<number> <step> <reason>
                                       a step for one pull request that failed: arm, judge, update, or rerun
    MERGED #<number> worktree "<path>" branch <branch>
                                       a worktree whose branch's pull request merged, for repo-cleanup to retire
    MERGED_FAILED <reason>             the merged pull requests could not be listed
    REMAINING <count>                  the open pull requests that are not drafts; 0 means none remains

Exit status 0 means the tick ran with nothing left for the shepherd's judgment; 1 that it printed an OUT_OF_STEP,
DEFECT, RERUN_SPENT, PR_FAILED, or MERGED_FAILED line, or a last line FAILED <reason>; 2 a usage error.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "skill-core" / "scripts"))

from console import run_main
from flat_text import flat_text
from git_client import GitClient, GitError
from github_client import GitHubClient, GitHubError
from github_remote import github_repository

REMOTE = "origin"
BASE = "main"
# The required check branch protection names, which is also the name of the workflow that runs it and its legs.
REQUIRED_CHECK = "Validation"
UPDATES_PER_TICK = 2
RERUNS_PER_HEAD = 1
# The paths whose change holds every other pull request's update while it is open: the validation policies, which
# hold the job selection, and the runner that selects what a pull request runs.
HOLDING_PATHS = ("tests/validation/", "tests/run_validation.py")
# A leg's steps that run the validation itself; a failure in any other is the runner's setup: the checkout, the
# interpreter, or a tool installed from a package gallery.
VALIDATION_STEPS = frozenset({"Run repository validation", "Validate with the latest ShellCheck"})
OPEN_LISTED = 100
MERGED_LISTED = 100
PULL_FIELDS = (
    "number,headRefName,headRefOid,isCrossRepository,isDraft,mergeStateStatus,autoMergeRequest,files,statusCheckRollup"
)
RUN_FIELDS = "headSha,attempt,conclusion,status,jobs"
# The event of the runs that report the required check on a pull request; the workflow's other events run on main.
PULL_EVENT = "pull_request"
RUNS_LISTED = 20
UPDATABLE = frozenset({"SUCCESS", "NEUTRAL", "PENDING"})
UNJUDGED = UPDATABLE | {"MISSING"}
FAILED_LEG = frozenset({"failure", "cancelled", "timed_out", "startup_failure"})
RUN_URL = re.compile(r"/actions/runs/(\d+)")
# What GitHub puts before each log line: a timestamp, and with --log-failed the job's and step's names before it.
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# The validation runner's report: each failed job between rules, then one closing line.
RULE = "=" * 70
FAILED_HEADER = "FAILED "
CLOSING = re.compile(r"policy checks and \d+ suite jobs in ")
POLICY_FAILURE = re.compile(r"^(?:FAIL|ERROR): (\S+)")
TERMINATION = "before its first line of output"
SILENT_EXIT = re.compile(r"^Command failed with exit code -?\d+: ")
SERVICE_MARKERS = (
    "powershellgallery.com",
    "unable to resolve package source",
    "could not resolve host",
    "service unavailable",
    "bad gateway",
    "the remote server returned an error",
)


class ShepherdError(RuntimeError):
    """A checkout the tick cannot shepherd, such as one whose origin names no GitHub repository."""


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str | None


@dataclass(frozen=True)
class Place:
    top: Path
    hub: Path
    worktrees: tuple[Worktree, ...]
    repository: str


@dataclass(frozen=True)
class Check:
    state: str
    run: int | None


@dataclass(frozen=True)
class Pull:
    number: int
    branch: str
    head: str
    merge_state: str
    armed: bool
    check: Check
    files: tuple[str, ...]
    cross_repository: bool = False


@dataclass
class Outcome:
    actions: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    flagged: bool = False

    def failed(self, number: int, step: str, error: Exception) -> None:
        self.lines.append(f"PR_FAILED #{number} {step} {flat_text(str(error))}")
        self.flagged = True


@dataclass(frozen=True)
class Leg:
    name: str
    reason: str | None  # the rerun case, or None for a defect
    check: str


@dataclass(frozen=True)
class Judgement:
    run: int
    attempt: int
    head: str
    kind: str  # superseded, rerun, spent, or defect
    reason: str
    leg: str
    check: str
    superseded_by: str = ""  # the newer head, or the newer run on the same head, that replaced a superseded run


def short(commit: str) -> str:
    return commit[:7] or "none"


def same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def parse_worktrees(listing: str) -> tuple[Worktree, ...]:
    """The worktrees `git worktree list --porcelain` names, the main one first, with each one's branch."""
    worktrees: list[Worktree] = []
    path: Path | None = None
    branch: str | None = None
    for line in [*listing.splitlines(), ""]:
        if line.startswith("worktree "):
            path, branch = Path(line[len("worktree ") :]), None
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/") :]
        elif not line and path is not None:
            worktrees.append(Worktree(path, branch))
            path = None
    return tuple(worktrees)


def locate(git: GitClient, directory: Path) -> Place:
    """The checkout `directory` lies in, its hub, every worktree of it, and the GitHub repository origin names."""
    top = git.rev_parse_path("--show-toplevel", directory=directory)
    if top is None:
        raise ShepherdError(f"{directory.as_posix()} is not in a git checkout")
    worktrees = parse_worktrees(git.output(["worktree", "list", "--porcelain"], directory=top))
    if not worktrees:
        raise ShepherdError(f"git lists no worktree for {top.as_posix()}")
    url = git.run(["config", "--get", f"remote.{REMOTE}.url"], directory=top)
    if url.returncode != 0:
        raise ShepherdError(f"{top.as_posix()} has no {REMOTE} remote")
    repository = github_repository(url.stdout)
    if repository is None:
        raise ShepherdError(f"{REMOTE} names no GitHub repository: {flat_text(url.stdout)}")
    return Place(top, worktrees[0].path, worktrees, repository)


def hub_line(git: GitClient, place: Place, dry_run: bool) -> str:
    where = f'HUB "{place.hub.as_posix()}"'
    if not same_path(place.top, place.hub):
        return (
            f'{where} not fast-forwarded: this tick runs in the linked worktree "{place.top.as_posix()}", so the '
            "hub's own session fast-forwards it"
        )
    try:
        return f"{where} {fast_forward(git, place.hub, dry_run)}"
    except GitError as exc:
        return f"{where} not fast-forwarded: {flat_text(str(exc))}"


def fast_forward(git: GitClient, hub: Path, dry_run: bool) -> str:
    """Fast-forward the hub to origin's main, or with `dry_run` compare it with origin's main, and say what happened."""
    branch = git.run(["symbolic-ref", "--quiet", "--short", "HEAD"], directory=hub)
    name = branch.stdout.strip() if branch.returncode == 0 else ""
    if name != BASE:
        return f"not fast-forwarded: it is on {name or 'a detached HEAD'}, not {BASE}"
    if git.output(["status", "--porcelain", "--untracked-files=no"], directory=hub).strip():
        return "not fast-forwarded: it has uncommitted changes"
    before = git.output(["rev-parse", "HEAD"], directory=hub).strip()
    if dry_run:
        listed = git.output(["ls-remote", REMOTE, f"refs/heads/{BASE}"], directory=hub).split()
        if not listed:
            return f"not fast-forwarded: {REMOTE} has no {BASE}"
        return (
            f"current at {short(before)}"
            if listed[0] == before
            else f"would fast-forward {short(before)} to {short(listed[0])}"
        )
    git.output(["fetch", "--prune", REMOTE], directory=hub)
    merged = git.run(["merge", "--ff-only", f"{REMOTE}/{BASE}"], directory=hub)
    if merged.returncode != 0:
        reason = flat_text(merged.stderr) or f"git merge --ff-only exited {merged.returncode}"
        return f"not fast-forwarded: {reason}"
    after = git.output(["rev-parse", "HEAD"], directory=hub).strip()
    return f"current at {short(after)}" if after == before else f"fast-forwarded {short(before)} to {short(after)}"


def malformed(what: str) -> GitHubError:
    return GitHubError(f"GitHub returned {what}", kind="malformed")


def run_of(entry: dict[str, Any]) -> int | None:
    match = RUN_URL.search(str(entry.get("detailsUrl") or ""))
    return int(match.group(1)) if match else None


def required_check(rollup: Any) -> Check:
    """The required check's state in the newest run on the head, PENDING while that run's legs run before it starts,
    and MISSING without it. An older run on the same head, such as one a newer run cancelled, is not read."""
    entries = [
        entry
        for entry in (rollup if isinstance(rollup, list) else [])
        if isinstance(entry, dict) and entry.get("workflowName") == REQUIRED_CHECK
    ]
    newest = max((run_of(entry) or 0 for entry in entries), default=0)
    entries = [entry for entry in entries if (run_of(entry) or 0) == newest]
    named = [entry for entry in entries if entry.get("name") == REQUIRED_CHECK]
    if named:
        entry = max(named, key=lambda item: str(item.get("startedAt") or ""))
        if entry.get("status") != "COMPLETED":
            return Check("PENDING", run_of(entry))
        return Check(str(entry.get("conclusion") or "UNKNOWN").upper(), run_of(entry))
    if entries:
        return Check("PENDING", run_of(entries[0]))
    return Check("MISSING", None)


def pull_of(item: Any) -> Pull:
    if not isinstance(item, dict):
        raise malformed("a pull request that is not an object")
    number, branch, head = item.get("number"), item.get("headRefName"), item.get("headRefOid")
    if not isinstance(number, int) or not isinstance(branch, str) or not isinstance(head, str):
        raise malformed("a pull request without its number, branch, or head")
    listed = item.get("files")
    files = listed if isinstance(listed, list) else []
    paths = tuple(str(entry.get("path")) for entry in files if isinstance(entry, dict) and entry.get("path"))
    return Pull(
        number,
        branch,
        head,
        str(item.get("mergeStateStatus") or "UNKNOWN"),
        bool(item.get("autoMergeRequest")),
        required_check(item.get("statusCheckRollup")),
        paths,
        bool(item.get("isCrossRepository")),
    )


def read_pulls(client: GitHubClient, repository: str) -> list[Pull]:
    """The open pull requests that are not drafts, oldest first."""
    arguments = ["pr", "list", "--repo", repository, "--state", "open", "--limit", str(OPEN_LISTED)]
    payload = client.json([*arguments, "--json", PULL_FIELDS])
    if not isinstance(payload, list):
        raise malformed("a pull request listing that is not a list")
    pulls = [pull_of(item) for item in payload if not (isinstance(item, dict) and item.get("isDraft"))]
    return sorted(pulls, key=lambda pull: pull.number)


def branch_heads(git: GitClient, top: Path) -> dict[str, str]:
    """The commit each branch on origin holds, by its name, read from origin itself rather than a fetched ref."""
    heads: dict[str, str] = {}
    for line in git.output(["ls-remote", "--heads", REMOTE], directory=top).splitlines():
        commit, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            heads[ref[len("refs/heads/") :]] = commit
    return heads


def out_of_step(pull: Pull, heads: dict[str, str]) -> str | None:
    """The commit `pull`'s branch holds on origin when it is not the head GitHub recorded, as when GitHub missed a
    push; None when they agree or the branch is not origin's to read."""
    if pull.cross_repository:
        return None
    branch = heads.get(pull.branch)
    return branch if branch is not None and branch != pull.head else None


def is_behind(git: GitClient, top: Path, pull: Pull, base: str | None) -> bool:
    """Whether `pull` is behind main: GitHub's BEHIND, or, while GitHub has not computed its merge state, `base` not
    an ancestor of its head. False when git cannot tell, as when either commit has not been fetched."""
    if pull.merge_state == "BEHIND":
        return True
    if pull.merge_state != "UNKNOWN" or base is None:
        return False
    try:
        # merge-base --is-ancestor exits 1 for "not an ancestor", and 128 for a commit it cannot read.
        return git.run(["merge-base", "--is-ancestor", base, pull.head], directory=top).returncode == 1
    except GitError:
        return False


def holding_path(files: Sequence[str]) -> str | None:
    for path in files:
        if any(path == held or (held.endswith("/") and path.startswith(held)) for held in HOLDING_PATHS):
            return path
    return None


def log_lines(text: str) -> list[str]:
    """A job log's lines without GitHub's job, step, and timestamp prefixes or terminal escapes."""
    lines = []
    for raw in text.splitlines():
        parts = raw.split("\t", 2)
        line = parts[2] if len(parts) == 3 else raw
        lines.append(ESCAPE.sub("", TIMESTAMP.sub("", line)).rstrip())
    return lines


def failure_blocks(lines: Sequence[str]) -> list[tuple[str, list[str]]]:
    """Each failed job the validation runner reports, by its label, with the lines of its report."""
    blocks: list[tuple[str, list[str]]] = []
    index = 0
    while index < len(lines):
        if lines[index] != RULE or index + 1 == len(lines) or not lines[index + 1].startswith(FAILED_HEADER):
            index += 1
            continue
        label = lines[index + 1][len(FAILED_HEADER) :]
        index += 2
        if index < len(lines) and lines[index] and set(lines[index]) == {"-"}:
            index += 1
        body: list[str] = []
        while index < len(lines) and not ends_block(lines[index]):
            body.append(lines[index])
            index += 1
        blocks.append((label, body))
    return blocks


def ends_block(line: str) -> bool:
    return line == RULE or bool(CLOSING.search(line)) or line.startswith("##[")


def first_failing_check(lines: Sequence[str]) -> str | None:
    """The first failed job or policy check the log names, in the order the runner printed them."""
    for index, line in enumerate(lines):
        if line == RULE and index + 1 < len(lines) and lines[index + 1].startswith(FAILED_HEADER):
            return lines[index + 1][len(FAILED_HEADER) :]
        policy = POLICY_FAILURE.match(line)
        if policy:
            return policy.group(1)
    return None


def block_reason(body: Sequence[str]) -> str | None:
    """The rerun case a failed job's report shows, or None when it shows a failure of the change."""
    text = "\n".join(body)
    if TERMINATION in text:
        return "termination"
    content = [line for line in body if line.strip()]
    if len(content) == 1 and SILENT_EXIT.match(content[0]):
        return "silent-exit"
    lowered = text.casefold()
    if any(marker in lowered for marker in SERVICE_MARKERS):
        return "service"
    return None


def classify_log(lines: Sequence[str]) -> tuple[str | None, str | None]:
    """The rerun case a failed validation step's log shows, or None for a defect, and the first failing check."""
    first = first_failing_check(lines)
    blocks = failure_blocks(lines)
    if not blocks or any(POLICY_FAILURE.match(line) for line in lines):
        return None, first
    for label, body in blocks:
        if block_reason(body) is None:
            return None, label
    return block_reason(blocks[0][1]), blocks[0][0]


def judge_leg(client: GitHubClient, repository: str, job: dict[str, Any]) -> Leg:
    name = flat_text(str(job.get("name") or "unnamed leg"))
    conclusion = job.get("conclusion")
    if conclusion == "cancelled":
        return Leg(name, "cancelled", "the leg was cancelled")
    if conclusion == "startup_failure":
        return Leg(name, "service", "the leg did not start")
    steps = [step for step in job.get("steps") or [] if isinstance(step, dict)]
    failed = [str(step.get("name")) for step in steps if step.get("conclusion") == "failure"]
    if conclusion == "timed_out":
        return Leg(name, None, f"the leg's time limit{f' in {failed[0]}' if failed else ''}")
    setup = next((step for step in failed if step not in VALIDATION_STEPS), None)
    if setup is not None:
        return Leg(name, "service", setup)
    log = client.run(["run", "view", "--repo", repository, "--job", str(job.get("databaseId")), "--log-failed"])
    reason, check = classify_log(log_lines(log.stdout))
    return Leg(name, reason, flat_text(check or (failed[0] if failed else "unknown")))


def judge(client: GitHubClient, repository: str, pull: Pull) -> Judgement:
    """What a red required check on `pull` means: a superseded head, a rerun case, a spent rerun, or a defect."""
    if pull.check.run is None:
        return Judgement(0, 0, "", "defect", "", REQUIRED_CHECK, "the check names no workflow run")
    run = client.json(["run", "view", str(pull.check.run), "--repo", repository, "--json", RUN_FIELDS])
    if not isinstance(run, dict):
        raise malformed("a workflow run that is not an object")
    head = str(run.get("headSha") or "")
    counted = run.get("attempt")
    attempt = counted if isinstance(counted, int) else 1
    if head != pull.head:
        return Judgement(pull.check.run, attempt, head, "superseded", "", "", "", short(pull.head))
    newest = newest_run(client, repository, head)
    if newest > pull.check.run:
        return Judgement(pull.check.run, attempt, head, "superseded", "", "", "", f"run {newest}")
    jobs = [job for job in run.get("jobs") or [] if isinstance(job, dict)]
    legs = [job for job in jobs if job.get("name") != REQUIRED_CHECK and job.get("conclusion") in FAILED_LEG]
    if legs:
        judged = [judge_leg(client, repository, job) for job in legs]
        leg = next((each for each in judged if each.reason is None), judged[0])
    elif run.get("conclusion") == "cancelled":
        leg = Leg("the run", "cancelled", "the run was cancelled")
    else:
        leg = Leg(REQUIRED_CHECK, "service", f"{REQUIRED_CHECK} failed with every leg passed")
    # A run's first attempt is the head's own; each rerun of it adds one.
    kind = "defect" if leg.reason is None else "rerun" if attempt <= RERUNS_PER_HEAD else "spent"
    return Judgement(pull.check.run, attempt, head, kind, leg.reason or "", leg.name, leg.check)


def newest_run(client: GitHubClient, repository: str, head: str) -> int:
    """The newest pull request run of the required check's workflow on `head`, by its id, or 0 when none is listed."""
    arguments = ["run", "list", "--repo", repository, "--workflow", REQUIRED_CHECK, "--commit", head]
    payload = client.json([*arguments, "--event", PULL_EVENT, "--limit", str(RUNS_LISTED), "--json", "databaseId"])
    if not isinstance(payload, list):
        raise malformed("a workflow run listing that is not a list")
    ids = [item.get("databaseId") for item in payload if isinstance(item, dict)]
    return max((each for each in ids if isinstance(each, int)), default=0)


def judge_red(client: GitHubClient, repository: str, pull: Pull, outcome: Outcome, dry_run: bool) -> None:
    if pull.check.state in UNJUDGED:
        return
    try:
        judgement = judge(client, repository, pull)
    except GitHubError as exc:
        outcome.failed(pull.number, "judge", exc)
        return
    if judgement.run:
        state = "current" if judgement.kind != "superseded" else f"superseded by {judgement.superseded_by}"
        tested = f"run {judgement.run} attempt {judgement.attempt} head {short(judgement.head)}"
        outcome.lines.append(f"JUDGED #{pull.number} {tested} {state}")
    if judgement.kind == "defect":
        outcome.lines.append(f'DEFECT #{pull.number} {pull.branch} leg "{judgement.leg}" check "{judgement.check}"')
        outcome.flagged = True
    elif judgement.kind == "spent":
        outcome.lines.append(
            f"RERUN_SPENT #{pull.number} run {judgement.run} attempt {judgement.attempt} {judgement.reason} "
            f'leg "{judgement.leg}"'
        )
        outcome.flagged = True
    elif judgement.kind == "rerun":
        rerun(client, repository, pull, judgement, outcome, dry_run)


def rerun(
    client: GitHubClient, repository: str, pull: Pull, judgement: Judgement, outcome: Outcome, dry_run: bool
) -> None:
    line = f'RERUN #{pull.number} run {judgement.run} {judgement.reason} leg "{judgement.leg}"'
    if dry_run:
        outcome.actions.append("would-rerun")
        outcome.lines.append(f"WOULD_{line}")
        return
    try:
        client.run(["run", "rerun", str(judgement.run), "--repo", repository, "--failed"])
    except GitHubError as exc:
        outcome.failed(pull.number, "rerun", exc)
        return
    outcome.actions.append("rerun")
    outcome.lines.append(line)


def arm(client: GitHubClient, repository: str, pull: Pull, outcome: Outcome, dry_run: bool) -> None:
    if pull.armed:
        return
    if dry_run:
        outcome.actions.append("would-arm")
        return
    try:
        client.run(["pr", "merge", str(pull.number), "--repo", repository, "--auto", "--squash"])
    except GitHubError as exc:
        outcome.failed(pull.number, "arm", exc)
        return
    outcome.actions.append("armed")


def update(
    client: GitHubClient,
    repository: str,
    pulls: Sequence[Pull],
    behind: set[int],
    holds: dict[int, str],
    outcomes: dict[int, Outcome],
    dry_run: bool,
) -> None:
    """Merge main into the oldest pull requests in `behind` whose check is green or pending, up to the tick's limit."""
    started = 0
    for pull in pulls:
        outcome = outcomes[pull.number]
        if pull.number not in behind or pull.check.state not in UPDATABLE:
            continue
        if holds and pull.number not in holds:
            outcome.actions.append("held")
            continue
        if started == UPDATES_PER_TICK:
            outcome.actions.append("next-tick")
            continue
        started += 1
        if dry_run:
            outcome.actions.append("would-update")
            continue
        try:
            client.run(["pr", "update-branch", str(pull.number), "--repo", repository])
        except GitHubError as exc:
            outcome.failed(pull.number, "update", exc)
            continue
        outcome.actions.append("updated")


def merged_lines(client: GitHubClient, place: Place, pulls: Sequence[Pull]) -> list[str]:
    """A MERGED line for each worktree but the hub whose branch's pull request merged and has no open one."""
    arguments = ["pr", "list", "--repo", place.repository, "--state", "merged", "--limit", str(MERGED_LISTED)]
    payload = client.json([*arguments, "--json", "number,headRefName"])
    if not isinstance(payload, list):
        raise malformed("a merged pull request listing that is not a list")
    merged: dict[str, int] = {}
    for item in payload:
        if isinstance(item, dict) and isinstance(item.get("number"), int) and isinstance(item.get("headRefName"), str):
            merged[item["headRefName"]] = max(item["number"], merged.get(item["headRefName"], 0))
    open_branches = {pull.branch for pull in pulls}
    return [
        f'MERGED #{merged[worktree.branch]} worktree "{worktree.path.as_posix()}" branch {worktree.branch}'
        for worktree in place.worktrees[1:]
        if worktree.branch in merged and worktree.branch not in open_branches
    ]


def pull_line(pull: Pull, outcome: Outcome) -> str:
    return (
        f"PR #{pull.number} {pull.branch} {pull.merge_state} auto-merge {'on' if pull.armed else 'off'} head "
        f"{short(pull.head)} {REQUIRED_CHECK} {pull.check.state} {','.join(outcome.actions) or 'none'}"
    )


def tick(git: GitClient, client: GitHubClient, directory: Path, *, dry_run: bool, emit: Callable[[str], None]) -> int:
    """Run one tick from `directory`, printing each line through `emit`, and return the exit status."""
    try:
        place = locate(git, directory)
    except (GitError, ShepherdError) as exc:
        emit(f"FAILED {flat_text(str(exc))}")
        return 1
    if dry_run:
        emit("DRY_RUN the hub is not fetched, and nothing is armed, updated, or rerun")
    emit(f"REPOSITORY {place.repository}")
    emit(hub_line(git, place, dry_run))
    try:
        pulls = read_pulls(client, place.repository)
    except GitHubError as exc:
        emit(f"FAILED cannot list the open pull requests: {flat_text(str(exc))}")
        return 1
    try:
        heads = branch_heads(git, place.top) if pulls else {}
    except GitError as exc:
        emit(f"FAILED cannot read {REMOTE}'s branches: {flat_text(str(exc))}")
        return 1
    holds = {pull.number: path for pull in pulls if (path := holding_path(pull.files)) is not None}
    for number, path in holds.items():
        emit(f"HOLD #{number} changes {path}")
    outcomes = {pull.number: Outcome() for pull in pulls}
    stale = {pull.number: branch for pull in pulls if (branch := out_of_step(pull, heads)) is not None}
    behind = {
        pull.number for pull in pulls if pull.number not in stale and is_behind(git, place.top, pull, heads.get(BASE))
    }
    for pull in pulls:
        outcome = outcomes[pull.number]
        arm(client, place.repository, pull, outcome, dry_run)
        if pull.number in stale:
            outcome.lines.append(
                f"OUT_OF_STEP #{pull.number} {pull.branch} head {short(pull.head)} branch {short(stale[pull.number])}"
            )
            outcome.flagged = True
            continue
        judge_red(client, place.repository, pull, outcome, dry_run)
    update(client, place.repository, pulls, behind, holds, outcomes, dry_run)
    flagged = False
    for pull in pulls:
        emit(pull_line(pull, outcomes[pull.number]))
        for line in outcomes[pull.number].lines:
            emit(line)
        flagged = flagged or outcomes[pull.number].flagged
    try:
        for line in merged_lines(client, place, pulls):
            emit(line)
    except GitHubError as exc:
        emit(f"MERGED_FAILED {flat_text(str(exc))}")
        flagged = True
    emit(f"REMAINING {len(pulls)}")
    return 1 if flagged else 0


def parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one merge-shepherd tick over the open pull requests.")
    parser.add_argument(
        "--dry-run", action="store_true", help="list and judge, and print what would change, without writing"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    return tick(GitClient(), GitHubClient(), Path.cwd(), dry_run=arguments.dry_run, emit=print)


if __name__ == "__main__":
    run_main(main)

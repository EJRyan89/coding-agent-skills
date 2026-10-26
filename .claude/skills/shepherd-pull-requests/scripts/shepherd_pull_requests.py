"""One tick of the merge shepherd over this repository's open pull requests, so the session that runs it only
messages the sessions that own them, judges what the script cannot, and hands each merge to repo-cleanup.

Run it from the hub on main. One tick:

- fast-forwards the hub to origin's main, when it runs in the hub itself, on main, with no tracked change; from a
  linked worktree it leaves the hub to the hub's own session; a hub it runs in and cannot fast-forward, because the
  fetch failed or the hub is off main, has a tracked change, or diverged, is named for the shepherd's judgment;
- lists the open pull requests that are not drafts, with their merge state, auto-merge, head, and the state of the
  required check Validation on that head;
- holds each admitted pull request whose body has a line `Shepherd-hold: <reason>`: it neither arms it nor lets it
  merge, turning off an auto-merge already armed, and names it with the reason, while it still names, judges, and
  reruns for it as for any other; deleting the line releases it;
- admits only the pull requests whose head branch is in this repository, and names every other one, such as one
  opened from a fork, as not admitted: it arms, judges, updates, and reruns nothing for it, and it holds no other
  pull request's update;
- arms squash auto-merge on each admitted one that has none and is not held;
- names each pull request whose recorded head is not the commit its branch on origin holds, because GitHub missed a
  push, and neither judges nor updates it, since GitHub refuses to update a stale head;
- names each pull request in conflict with main (GitHub's merge state DIRTY), which only its owner can resolve, and
  does not update it;
- judges each red Validation check: it compares the head the check's run tested with the pull request's head, and the
  run with the newest pull request run on that head, first, since a red run on a superseded head, or one a newer run
  on the same head replaced, is no failure; then it reruns the failed legs, once per head, when every failure is a
  program's termination, a cancellation, a service failure, or a suite that exited with no output, and names any
  other failure as a defect;
- merges main into one armed pull request behind main whose check is green or pending, through GitHub's
  update-branch merge, never a rebase or a force-push, and only while no armed pull request is up to date with main
  and green or pending, since that one merges next; it takes the one furthest along in Validation, the share of the
  newest run's legs that finished, and otherwise the oldest, and leaves every other one alone, even behind, because
  an update cancels the Validation run in flight on it; a pull request is behind when GitHub's merge state is
  BEHIND, or, while GitHub has not computed it (UNKNOWN), when origin's main is not an ancestor of its head; while a
  pull request that changes the validation policies or the runner's selection is open, every other pull request's
  update waits for it;
- checks, for each admitted pull request in step, not in conflict, and with a check neither red nor missing, held
  or not, whether the machine's code-review archive holds a review-repository review of its head, and prints the
  review-prs arguments that review those it does not; the review is report-only, and premerge_review.py's docstring
  lists its lines, which change no exit status;
- names each worktree a merged pull request carried and no open pull request, a draft included, still carries, for
  repo-cleanup to retire: one whose checked-out branch is the pull request's head branch, or whose HEAD is the pull
  request's head commit or a commit that head reaches and origin's main does not, as when a session pushed its
  branch's commits under another name; a head commit git does not hold, as when it was never fetched, is matched by
  its branch's name alone, and while git does not hold origin's main, which leaves each merged head's own commits
  unknown, it names none.

With --dry-run it lists and judges the same way and writes nothing: it reads origin's main instead of fetching, and
prints what it would arm, update, and rerun. Either way it reads origin's branches with ls-remote.

Every line is one fact:

    DRY_RUN <what is not written>      --dry-run: the first line
    REPOSITORY <owner/name>            the GitHub repository origin names
    HUB "<path>" <what happened>       fast-forwarded <old> to <new>, current at <commit>, would fast-forward <old>
                                       to <new>, or not fast-forwarded: <reason>; the last is for the shepherd's
                                       judgment unless the tick runs in a linked worktree
    HOLD #<number> changes <path>      an admitted pull request that holds every other one's update while it is open
    HELD #<number> <branch> "<reason>" an admitted pull request whose body holds it from arming and merging
    PR #<number> <branch> <merge state> auto-merge <on|off> head <commit> Validation <state> <actions>
                                       one per pull request, oldest first; <merge state> is GitHub's, <state> is a
                                       conclusion of the newest run on the head, such as SUCCESS or FAILURE,
                                       PENDING, or MISSING, and <actions> is none or a comma-separated
                                       list of armed, disarmed, updated, rerun (would-arm, would-disarm,
                                       would-update, and would-rerun with --dry-run), held, an update a HOLD line
                                       holds, and next-tick, a behind pull request left for a later tick
    NOT_ADMITTED #<number> <branch> head <commit> from another repository
                                       a pull request whose head branch is not in this repository, left to the
                                       maintainer: never armed, judged, updated, or rerun
    OUT_OF_STEP #<number> <branch> head <commit> branch <commit>
                                       a pull request whose recorded head is not its branch's commit on origin,
                                       neither judged nor updated until its owner pushes again
    CONFLICT #<number> <branch> head <commit> merge origin/main and resolve
                                       a pull request in conflict with main, not updated until its owner merges
                                       origin/main into the branch, resolves the conflict, and pushes
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
    REVIEWED, REVIEW_DUE, REVIEW_ARGUMENTS, REVIEW_UNREAD
                                       the pre-merge review of each head, after the PR lines (premerge_review.py)
    MERGED #<number> worktree "<path>" branch <branch> [branch <branch>]
                                       a worktree a merged pull request carried, for repo-cleanup to retire, with one
                                       branch <branch> for each local branch to delete: the worktree's checked-out
                                       branch first, then the pull request's head branch when a local branch of that
                                       name exists and no other worktree has it checked out
    MERGED_FAILED <reason>             the merged pull requests could not be listed or matched to the worktrees
    REMAINING <count>                  the open pull requests that are not drafts; 0 means none remains

Every branch, path, state, leg, step, and check name that GitHub returned is flattened onto its line.

Exit status 0 means the tick ran with nothing left for the shepherd's judgment; 1 that it printed a HUB line naming
a hub it ran in and did not fast-forward, a NOT_ADMITTED, OUT_OF_STEP, CONFLICT, DEFECT, RERUN_SPENT, PR_FAILED, or
MERGED_FAILED line, or a last line FAILED <reason>; 2 a usage error.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))

import whole_number
from console import run_main
from flat_text import flat_text
from git_client import GitClient, GitError
from git_worktrees import Worktree, list_worktrees
from github_client import GitHubClient, GitHubError
from github_remote import github_repository
from path_keys import path_key
from premerge_review import Head, Reader, ReviewUnread, archive_reader, review_lines

REMOTE = "origin"
BASE = "main"
# The required check branch protection names, which is also the name of the workflow that runs it and its legs.
REQUIRED_CHECK = "Validation"
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
    "number,headRefName,headRefOid,isCrossRepository,isDraft,mergeStateStatus,autoMergeRequest,files,statusCheckRollup,"
    "body"
)
# The line in a pull request's body that holds it from arming and merging, with its reason; deleting it releases it.
HOLD_LINE = re.compile(r"^Shepherd-hold:[ 	]*(.*?)[ 	]*$", re.MULTILINE)
RUN_FIELDS = "headSha,attempt,conclusion,status,jobs"
# GitHub's merge state of a pull request whose merge commit cannot be made cleanly: a conflict with main.
CONFLICTED = "DIRTY"
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
# The validation runner's report, tests/run_validation.py's report_run: each failed job between rules, then one
# closing line. The shepherd's suite pins these against the runner's own report, and VALIDATION_STEPS against the
# step names in .github/workflows/validate.yml.
RULE = "=" * 70
FAILED_HEADER = "FAILED "
CLOSING = re.compile(r"policy checks and \d+ suite jobs in ")
POLICY_FAILURE = re.compile(r"^(?:FAIL|ERROR): (\S+)")
# How tests/validation/job_pool.py's _settle reports a suite whose program was ended before its first line of output:
# ended on every attempt, a termination; or started once more and failed, a retry note whose report after RETRY_FAILED
# is the retry's own. The shepherd's suite pins both against the pool's own output.
TERMINATED = re.compile(r"^Ended before its first line of output on each of \d+ attempts")
RETRIED = "Started once more after: "
RETRY_FAILED = "The retry failed:"
SILENT_EXIT = re.compile(r"^Command failed with exit code -?\d+: ")
SERVICE_MARKERS = (
    "powershellgallery.com",
    "unable to resolve package source",
    "could not resolve host",
    "service unavailable",
    "bad gateway",
    "the remote server returned an error",
)


GIT_STATUS_ALLOWED = {
    "fast_forward": "reports git's reason to the user: a fast-forward git refuses is the HUB line, naming git's stderr",
}


class ShepherdError(RuntimeError):
    """A checkout the tick cannot shepherd, such as one whose origin names no GitHub repository."""


@dataclass(frozen=True)
class Place:
    top: Path
    hub: Path
    worktrees: tuple[Worktree, ...]
    repository: str


@dataclass(frozen=True)
class Merged:
    number: int
    branch: str
    head: str


@dataclass(frozen=True)
class Check:
    state: str
    run: int | None
    progress: float = 0.0  # the share of the newest run's legs that finished


@dataclass(frozen=True)
class Pull:
    number: int
    branch: str
    head: str
    merge_state: str
    armed: bool
    check: Check
    files: tuple[str, ...]
    cross_repository: bool
    draft: bool
    hold: str | None  # the reason a Shepherd-hold line gives, or None when none holds it


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
    return path_key(first, resolve=True) == path_key(second, resolve=True)


def locate(git: GitClient, directory: Path) -> Place:
    """The checkout `directory` lies in, its hub, every worktree of it, and the GitHub repository origin names."""
    try:
        top = git.rev_parse_path("--show-toplevel", directory=directory)
    except GitError as exc:
        raise ShepherdError(f"git could not read the checkout at {directory.as_posix()}: {exc}") from exc
    if top is None:
        raise ShepherdError(f"{directory.as_posix()} is not in a git checkout")
    worktrees = tuple(list_worktrees(git, top))
    if not worktrees:
        raise ShepherdError(f"git lists no worktree for {top.as_posix()}")
    # Exit 1 with nothing on stderr is git's answer that the URL is unset; any other failure raises with git's reason.
    url = git.quiet_output(["config", "--get", f"remote.{REMOTE}.url"], directory=top)
    if url is None:
        raise ShepherdError(f"{top.as_posix()} has no {REMOTE} remote")
    repository = github_repository(url)
    if repository is None:
        raise ShepherdError(f"{REMOTE} names no GitHub repository: {flat_text(url)}")
    return Place(top, Path(worktrees[0].path), worktrees, repository)


def hub_line(git: GitClient, place: Place, dry_run: bool) -> tuple[str, bool]:
    """The HUB line, and whether it names a hub this tick runs in and could not bring to origin's main."""
    where = f'HUB "{place.hub.as_posix()}"'
    if not same_path(place.top, place.hub):
        line = (
            f'{where} not fast-forwarded: this tick runs in the linked worktree "{place.top.as_posix()}", so the '
            "hub's own session fast-forwards it"
        )
        return line, False
    try:
        happened = fast_forward(git, place.hub, dry_run)
    except GitError as exc:
        happened = f"not fast-forwarded: {flat_text(str(exc))}"
    return f"{where} {happened}", happened.startswith("not fast-forwarded")


def fast_forward(git: GitClient, hub: Path, dry_run: bool) -> str:
    """Fast-forward the hub to origin's main, or with `dry_run` compare it with origin's main, and say what happened."""
    # None only for a detached HEAD; a HEAD git cannot read raises, which the HUB line names.
    name = (git.quiet_output(["symbolic-ref", "--quiet", "--short", "HEAD"], directory=hub) or "").strip()
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


def objects(value: Any, what: str) -> list[dict[str, Any]]:
    """The objects a listing GitHub returned holds, or the malformed GitHubError naming `what` when it is not a list of
    objects: a shape the shepherd does not know is never read as an empty listing."""
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise malformed(f"{what} that is not a list of objects")
    return value


# How a refusal names the kind of value a member must hold.
KIND_NAMES: dict[type, str] = {str: "text", int: "an integer", bool: "a boolean", dict: "an object"}


def member(entry: dict[str, Any], key: str, kind: type, what: str) -> Any:
    """`entry`'s `key`, None when it is absent or null, or the malformed GitHubError naming `what` when it holds a value
    of another kind than `kind`, which the shepherd would otherwise read as some verdict it never meant."""
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, kind) or (kind is not bool and isinstance(value, bool)):
        raise malformed(f"{what} whose {key} is not {KIND_NAMES[kind]}")
    return value


def run_of(entry: dict[str, Any]) -> int | None:
    url = member(entry, "detailsUrl", str, "a check")
    match = RUN_URL.search(url) if url is not None else None
    refused = malformed(f"a run id that is not at most {whole_number.DIGIT_LIMIT} ASCII digits")
    return whole_number.read(match.group(1), refused) if match else None


def required_check(rollup: Any) -> Check:
    """The required check's state in the newest run on the head, PENDING while that run's legs run before it starts,
    and MISSING without it, with the share of that run's legs that finished. An older run on the same head, such as
    one a newer run cancelled, is not read."""
    # GitHub gives a head with no status at all a null rollup.
    listed = objects([] if rollup is None else rollup, "a check rollup")
    for entry in listed:
        # A check's URL is read, and checked, only where the run it names matters (run_of).
        for key in ("name", "workflowName", "status", "conclusion", "startedAt"):
            member(entry, key, str, "a check")
    entries = [entry for entry in listed if entry.get("workflowName") == REQUIRED_CHECK]
    newest = max((run_of(entry) or 0 for entry in entries), default=0)
    entries = [entry for entry in entries if (run_of(entry) or 0) == newest]
    named = [entry for entry in entries if entry.get("name") == REQUIRED_CHECK]
    legs = [entry for entry in entries if entry.get("name") != REQUIRED_CHECK]
    progress = sum(entry.get("status") == "COMPLETED" for entry in legs) / len(legs) if legs else 0.0
    if named:
        entry = max(named, key=lambda item: str(item.get("startedAt") or ""))
        if entry.get("status") != "COMPLETED":
            return Check("PENDING", run_of(entry), progress)
        return Check(str(entry.get("conclusion") or "UNKNOWN").upper(), run_of(entry), 1.0)
    if entries:
        return Check("PENDING", run_of(entries[0]), progress)
    return Check("MISSING", None)


def hold_of(body: Any) -> str | None:
    """The reason the first Shepherd-hold line in a pull request's body gives, or None when no line holds it."""
    match = HOLD_LINE.search(body) if isinstance(body, str) else None
    if match is None:
        return None
    return flat_text(match.group(1)) or "no reason given"


def pull_of(item: Any) -> Pull:
    if not isinstance(item, dict):
        raise malformed("a pull request that is not an object")
    number = member(item, "number", int, "a pull request")
    branch = member(item, "headRefName", str, "a pull request")
    head = member(item, "headRefOid", str, "a pull request")
    if number is None or branch is None or head is None:
        raise malformed("a pull request without its number, branch, or head")
    listed_paths = [entry.get("path") for entry in objects(item.get("files"), "a pull request's files")]
    paths = tuple(path for path in listed_paths if isinstance(path, str) and path)
    if len(paths) != len(listed_paths):
        raise malformed("a pull request file without its path")
    what = "a pull request"
    return Pull(
        number,
        branch,
        head,
        member(item, "mergeStateStatus", str, what) or "UNKNOWN",
        member(item, "autoMergeRequest", dict, what) is not None,
        required_check(item.get("statusCheckRollup")),
        paths,
        bool(member(item, "isCrossRepository", bool, what)),
        bool(member(item, "isDraft", bool, what)),
        hold_of(member(item, "body", str, what)),
    )


def read_pulls(client: GitHubClient, repository: str) -> list[Pull]:
    """The open pull requests, drafts included, oldest first. More than OPEN_LISTED raises an `incomplete`
    GitHubError, since a tick that judged only some of them would say nothing of the rest."""
    arguments = ["pr", "list", "--repo", repository, "--state", "open", "--json", PULL_FIELDS]
    listing = client.capped_list(arguments, OPEN_LISTED)
    if listing.cut:
        raise GitHubError(f"more than {OPEN_LISTED} are open, more than one tick lists", kind="incomplete")
    return sorted((pull_of(item) for item in listing.items), key=lambda pull: pull.number)


def branch_heads(git: GitClient, top: Path) -> dict[str, str]:
    """The commit each branch on origin holds, by its name, read from origin itself rather than a fetched ref."""
    heads: dict[str, str] = {}
    # Split on git's own line ends alone: a branch name may hold a character splitlines breaks on, such as U+2028.
    for line in git.output(["ls-remote", "--heads", REMOTE], directory=top).split("\n"):
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
        # merge-base --is-ancestor exits 1 saying nothing for "not an ancestor"; any other failure cannot tell.
        return git.quiet_output(["merge-base", "--is-ancestor", base, pull.head], directory=top) is None
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
    """The rerun case a failed job's report shows, or None when it shows a failure of the change; a retry that
    failed is judged by the retry's report alone."""
    content = [line for line in body if line.strip()]
    if len(content) > 1 and content[0].startswith(RETRIED) and content[1] == RETRY_FAILED:
        return block_reason(body[body.index(RETRY_FAILED) + 1 :])
    if content and TERMINATED.match(content[0]):
        return "termination"
    text = "\n".join(body)
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
    """Why a failed leg failed. `judge` has checked the job's name and conclusion by kind; its steps are checked
    here."""
    name = flat_text(job.get("name") or "unnamed leg")
    conclusion = job.get("conclusion")
    if conclusion == "cancelled":
        return Leg(name, "cancelled", "the leg was cancelled")
    if conclusion == "startup_failure":
        return Leg(name, "service", "the leg did not start")
    steps = objects(job.get("steps"), "a job's steps")
    for step in steps:
        for key in ("name", "conclusion"):
            member(step, key, str, "a step")
    failed = [str(step.get("name")) for step in steps if step.get("conclusion") == "failure"]
    if conclusion == "timed_out":
        return Leg(name, None, f"the leg's time limit{f' in {flat_text(failed[0])}' if failed else ''}")
    setup = next((step for step in failed if step not in VALIDATION_STEPS), None)
    if setup is not None:
        return Leg(name, "service", flat_text(setup))
    job_id = member(job, "databaseId", int, "a job")
    if job_id is None:
        raise malformed("a failed job without its id")
    log = client.run(["run", "view", "--repo", repository, "--job", str(job_id), "--log-failed"])
    reason, check = classify_log(log_lines(log.stdout))
    return Leg(name, reason, flat_text(check or (failed[0] if failed else "unknown")))


def judge(client: GitHubClient, repository: str, pull: Pull) -> Judgement:
    """What a red required check on `pull` means: a superseded head, a rerun case, a spent rerun, or a defect."""
    if pull.check.run is None:
        return Judgement(0, 0, "", "defect", "", REQUIRED_CHECK, "the check names no workflow run")
    run = client.json(["run", "view", str(pull.check.run), "--repo", repository, "--json", RUN_FIELDS])
    if not isinstance(run, dict):
        raise malformed("a workflow run that is not an object")
    head = member(run, "headSha", str, "a workflow run")
    if not head:
        raise malformed("a workflow run without its head")
    counted = member(run, "attempt", int, "a workflow run")
    attempt = 1 if counted is None else counted
    if head != pull.head:
        return Judgement(pull.check.run, attempt, head, "superseded", "", "", "", short(pull.head))
    newest = newest_run(client, repository, head)
    if newest > pull.check.run:
        return Judgement(pull.check.run, attempt, head, "superseded", "", "", "", f"run {newest}")
    jobs = objects(run.get("jobs"), "a workflow run's jobs")
    for job in jobs:
        for key in ("name", "conclusion"):
            member(job, key, str, "a job")
    if not jobs:
        # A finished run lists its jobs, so judging one that lists none would read no legs as legs that passed.
        raise malformed("a workflow run that lists no jobs")
    member(run, "conclusion", str, "a workflow run")
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
    # GitHub lists the newest run first, so a listing cut at RUNS_LISTED leaves out only older runs.
    listing = client.capped_list([*arguments, "--event", PULL_EVENT, "--json", "databaseId"], RUNS_LISTED)
    listed = [item.get("databaseId") for item in objects(listing.items, "a workflow run listing")]
    ids = [each for each in listed if isinstance(each, int) and not isinstance(each, bool)]
    if len(ids) != len(listed):
        raise malformed("a workflow run without its integer id")
    return max(ids, default=0)


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
        outcome.lines.append(
            f'DEFECT #{pull.number} {flat_text(pull.branch)} leg "{judgement.leg}" check "{judgement.check}"'
        )
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
    """Arm squash auto-merge on `pull`, or, when a Shepherd-hold line holds it, turn off an auto-merge already armed."""
    if pull.hold is not None:
        outcome.lines.append(f'HELD #{pull.number} {flat_text(pull.branch)} "{pull.hold}"')
        if pull.armed:
            merge_option(client, repository, pull, outcome, ("disarm", "disarmed", "--disable-auto"), dry_run)
        return
    if not pull.armed:
        merge_option(client, repository, pull, outcome, ("arm", "armed", "--auto", "--squash"), dry_run)


def merge_option(
    client: GitHubClient, repository: str, pull: Pull, outcome: Outcome, step: tuple[str, ...], dry_run: bool
) -> None:
    """Run `gh pr merge` on `pull` with the options after `step`'s step and action, and add the action, or the
    step's would- form with `dry_run`."""
    name, action, *options = step
    if dry_run:
        outcome.actions.append(f"would-{name}")
        return
    try:
        client.run(["pr", "merge", str(pull.number), "--repo", repository, *options])
    except GitHubError as exc:
        outcome.failed(pull.number, name, exc)
        return
    outcome.actions.append(action)


def armed(pull: Pull, outcome: Outcome) -> bool:
    """Whether `pull` is armed once this tick's writes are made: armed before and not held, or armed now."""
    return (pull.armed and pull.hold is None) or bool({"armed", "would-arm"} & set(outcome.actions))


def update(
    client: GitHubClient,
    repository: str,
    pulls: Sequence[Pull],
    places: tuple[set[int], set[int]],
    holds: dict[int, str],
    outcomes: dict[int, Outcome],
    dry_run: bool,
) -> None:
    """Merge main into the one armed pull request that is behind, green or pending, and furthest along in Validation,
    the oldest among equals, while no armed one that is current, green or pending, and not blocked merges first.
    `places` holds the numbers of the pull requests behind main and of those blocked: out of step or in conflict."""
    behind, blocked = places
    ready = [pull for pull in pulls if pull.check.state in UPDATABLE and armed(pull, outcomes[pull.number])]
    merging = any(pull.number not in behind and pull.number not in blocked for pull in ready)
    candidates = [pull for pull in ready if pull.number in behind and (not holds or pull.number in holds)]
    due = None if merging else min(candidates, key=lambda pull: (-pull.check.progress, pull.number), default=None)
    for pull in ready:
        outcome = outcomes[pull.number]
        if pull.number not in behind:
            continue
        if holds and pull.number not in holds:
            outcome.actions.append("held")
        elif pull is not due:
            outcome.actions.append("next-tick")
        elif dry_run:
            outcome.actions.append("would-update")
        else:
            try:
                client.run(["pr", "update-branch", str(pull.number), "--repo", repository])
            except GitHubError as exc:
                outcome.failed(pull.number, "update", exc)
                continue
            outcome.actions.append("updated")


def read_merged(client: GitHubClient, repository: str) -> list[Merged]:
    """The MERGED_LISTED most recently merged pull requests, with each one's head branch and head commit. GitHub lists
    the newest first, so a listing cut there leaves out only pull requests merged before them, which earlier ticks
    saw."""
    arguments = ["pr", "list", "--repo", repository, "--state", "merged", "--json", "number,headRefName,headRefOid"]
    listing = client.capped_list(arguments, MERGED_LISTED)
    found = []
    for item in objects(listing.items, "a merged pull request listing"):
        number = member(item, "number", int, "a merged pull request")
        branch = member(item, "headRefName", str, "a merged pull request")
        if number is None or branch is None:
            raise malformed("a merged pull request without its number or branch")
        found.append(Merged(number, branch, member(item, "headRefOid", str, "a merged pull request") or ""))
    return found


def own_history(git: GitClient, top: Path, heads: Sequence[str], base: str | None) -> dict[str, list[str]]:
    """The parents of each commit `heads` reach that origin's main, `base`, does not, by the commit. A head git does
    not hold is passed over. Raises ShepherdError when origin has no main, or git does not hold `base`, as when the hub
    was not fetched since main moved, since every commit main gained since the last fetch would then count as a head's
    own, and GitError naming git's reason when git cannot tell whether it holds `base`."""
    if not heads:
        return {}
    if base is None:
        raise ShepherdError(f"{REMOTE} has no {BASE}, so a merged head's own commits cannot be told from {BASE}'s")
    if git.commit_of(base, directory=top) is None:
        raise ShepherdError(
            f"git does not hold {REMOTE}'s {BASE} {short(base)}, so a merged head's own commits cannot be told "
            f"from {BASE}'s; fetch {REMOTE} in the hub"
        )
    listing = git.output(["rev-list", "--parents", "--ignore-missing", *heads, "--not", base], directory=top)
    return {commit: parents for commit, *parents in (line.split() for line in listing.splitlines() if line)}


def reaches(history: dict[str, list[str]], head: str, commit: str) -> bool:
    """Whether `commit` is `head` or one of its ancestors within `history`."""
    seen: set[str] = set()
    pending = [head]
    while pending:
        current = pending.pop()
        if current == commit:
            return True
        if current in seen or current not in history:
            continue
        seen.add(current)
        pending.extend(history[current])
    return False


def local_branches(git: GitClient, top: Path) -> set[str]:
    listing = git.output(["for-each-ref", "--format=%(refname)", "refs/heads/"], directory=top)
    return {line[len("refs/heads/") :] for line in listing.split("\n") if line.startswith("refs/heads/")}


def merged_lines(
    git: GitClient, client: GitHubClient, place: Place, pulls: Sequence[Pull], base: str | None
) -> list[str]:
    """A MERGED line for each worktree but the hub that a merged pull request carried and no open one, a draft
    included, does."""
    merged = read_merged(client, place.repository)
    open_branches = {pull.branch for pull in pulls}
    open_heads = {pull.head for pull in pulls}
    candidates = [
        worktree
        for worktree in place.worktrees[1:]
        if worktree.branch not in open_branches and worktree.head not in open_heads
    ]
    if not candidates or not merged:
        return []
    history = own_history(git, place.top, sorted({each.head for each in merged if each.head}), base)
    local = local_branches(git, place.top)
    lines = []
    for worktree in candidates:
        carried = [
            each
            for each in merged
            if each.branch == worktree.branch
            or (worktree.head in history and reaches(history, each.head, worktree.head))
        ]
        if not carried:
            continue
        others = {other.branch for other in place.worktrees if other.path != worktree.path}
        branches = [worktree.branch] if worktree.branch else []
        for each in sorted(carried, key=lambda item: -item.number):
            if each.branch in local and each.branch not in others and each.branch not in branches:
                branches.append(each.branch)
        named = "".join(f" branch {flat_text(branch)}" for branch in branches)
        number = max(each.number for each in carried)
        lines.append(f'MERGED #{number} worktree "{Path(worktree.path).as_posix()}"{named}')
    return lines


def pull_line(pull: Pull, outcome: Outcome) -> str:
    return (
        f"PR #{pull.number} {flat_text(pull.branch)} {flat_text(pull.merge_state)} auto-merge "
        f"{'on' if pull.armed else 'off'} head {short(pull.head)} {REQUIRED_CHECK} {flat_text(pull.check.state)} "
        f"{','.join(outcome.actions) or 'none'}"
    )


def act(client: GitHubClient, repository: str, pull: Pull, outcome: Outcome, stale: str | None, dry_run: bool) -> None:
    """Name `pull` when it is not admitted; otherwise arm it, and name it when it is out of step, where `stale` is its
    branch's commit on origin, or in conflict, and judge its red check."""
    named = f"{flat_text(pull.branch)} head {short(pull.head)}"
    if pull.cross_repository:
        outcome.lines.append(f"NOT_ADMITTED #{pull.number} {named} from another repository")
        outcome.flagged = True
        return
    arm(client, repository, pull, outcome, dry_run)
    if stale is not None:
        outcome.lines.append(f"OUT_OF_STEP #{pull.number} {named} branch {short(stale)}")
        outcome.flagged = True
        return
    if pull.merge_state == CONFLICTED:
        outcome.lines.append(f"CONFLICT #{pull.number} {named} merge {REMOTE}/{BASE} and resolve")
        outcome.flagged = True
    judge_red(client, repository, pull, outcome, dry_run)


def premerge_reviews(
    git: GitClient, place: Place, pulls: Sequence[Pull], heads: dict[str, str], reviews: Callable[[str], Reader]
) -> list[str]:
    """The pre-merge review's lines (premerge_review.py) for each admitted pull request in step with its branch, not
    in conflict, and with a Validation check that is neither red nor missing; a red head waits for its fix."""
    ready = [
        Head(pull.number, pull.branch, pull.head)
        for pull in pulls
        if not pull.cross_repository
        and out_of_step(pull, heads) is None
        and pull.merge_state != CONFLICTED
        and pull.check.state in UPDATABLE
    ]
    if not ready:
        return []
    try:
        read = reviews(place.repository)
    except ReviewUnread as exc:
        return [f"REVIEW_UNREAD {flat_text(str(exc))}"]
    return review_lines(git, place.top, heads.get(BASE), place.repository, ready, read)


def tick(
    git: GitClient,
    client: GitHubClient,
    directory: Path,
    *,
    dry_run: bool,
    emit: Callable[[str], None],
    reviews: Callable[[str], Reader] | None = None,
) -> int:
    """Run one tick from `directory`, printing each line through `emit`, and return the exit status. `reviews` opens
    the reader of a repository's pre-merge reviews; without it the tick checks none."""
    try:
        place = locate(git, directory)
    except (GitError, ShepherdError) as exc:
        emit(f"FAILED {flat_text(str(exc))}")
        return 1
    if dry_run:
        emit("DRY_RUN the hub is not fetched, and nothing is armed, disarmed, updated, or rerun")
    emit(f"REPOSITORY {place.repository}")
    hub, flagged = hub_line(git, place, dry_run)
    emit(hub)
    try:
        listed = read_pulls(client, place.repository)
    except GitHubError as exc:
        emit(f"FAILED cannot list the open pull requests: {flat_text(str(exc))}")
        return 1
    try:
        heads = branch_heads(git, place.top)
    except GitError as exc:
        emit(f"FAILED cannot read {REMOTE}'s branches: {flat_text(str(exc))}")
        return 1
    pulls = [pull for pull in listed if not pull.draft]
    flagged = shepherd_pulls(git, client, place, pulls, heads, dry_run, emit) or flagged
    for line in premerge_reviews(git, place, pulls, heads, reviews) if reviews is not None else []:
        emit(line)
    try:
        for line in merged_lines(git, client, place, listed, heads.get(BASE)):
            emit(line)
    except (GitError, GitHubError, ShepherdError) as exc:
        emit(f"MERGED_FAILED {flat_text(str(exc))}")
        flagged = True
    emit(f"REMAINING {len(pulls)}")
    return 1 if flagged else 0


def shepherd_pulls(
    git: GitClient,
    client: GitHubClient,
    place: Place,
    pulls: Sequence[Pull],
    heads: dict[str, str],
    dry_run: bool,
    emit: Callable[[str], None],
) -> bool:
    """Arm, hold, name, judge, and update the open pull requests that are not drafts, print their lines through
    `emit`, and return whether any line is for the shepherd's judgment."""
    admitted = [pull for pull in pulls if not pull.cross_repository]
    holds = {pull.number: path for pull in admitted if (path := holding_path(pull.files)) is not None}
    for number, path in holds.items():
        emit(f"HOLD #{number} changes {flat_text(path)}")
    outcomes = {pull.number: Outcome() for pull in pulls}
    stale = {pull.number: branch for pull in pulls if (branch := out_of_step(pull, heads)) is not None}
    behind = {
        pull.number
        for pull in admitted
        if pull.number not in stale and is_behind(git, place.top, pull, heads.get(BASE))
    }
    blocked = set(stale) | {pull.number for pull in pulls if pull.merge_state == CONFLICTED}
    for pull in pulls:
        act(client, place.repository, pull, outcomes[pull.number], stale.get(pull.number), dry_run)
    update(client, place.repository, pulls, (behind, blocked), holds, outcomes, dry_run)
    for pull in pulls:
        emit(pull_line(pull, outcomes[pull.number]))
        for line in outcomes[pull.number].lines:
            emit(line)
    return any(outcome.flagged for outcome in outcomes.values())


def parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one merge-shepherd tick over the open pull requests.")
    parser.add_argument(
        "--dry-run", action="store_true", help="list and judge, and print what would change, without writing"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    return tick(GitClient(), GitHubClient(), Path.cwd(), dry_run=arguments.dry_run, emit=print, reviews=archive_reader)


if __name__ == "__main__":
    run_main(main)

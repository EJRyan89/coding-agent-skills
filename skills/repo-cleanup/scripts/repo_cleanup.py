"""Deterministic repository cleanup steps, so the agent only relays results and asks the user's confirmations.

    sweep         find the repositories to clean, then sync, plan, and apply every one at once, printing only what
                  needs the agent: decisions, failures, and summaries; plans go to a new temporary directory by
                  default, or with --record to the directory an earlier sweep printed and recorded its repositories
                  in
    delete-local  delete local-only branches the user chose to delete (git branch -d), with the clean worktree a
                  branch offered with its path is checked out in
    force-delete  force-delete branches reported UNMERGED, after the user confirmed
    remove-directory
                  remove detached worktrees reported CONFIRM_DETACHED and delete directories reported ORPHAN, after
                  the user confirmed
    summary       print the repository's summary from the plan file
    report        print, from the directory a sweep printed and recorded its repositories in, each repository's
                  current summary and the closing count of the repositories processed and skipped, naming the dirty
                  repositories the user aborted

Every command reads a plan or sweep record only when its bytes match the seal this script wrote when it last saved
one at that path, and refuses a plan with a field missing or not of the kind a sweep writes. delete-local,
force-delete, and remove-directory also refuse a plan whose repository root is no longer a Git repository or whose
worktree areas are not the ones a sweep derives from it, so they delete only what a sweep offered.

Every command prints one tab-separated fact per line. Exit status 0 means every repository was cleaned, or every
confirmed deletion ran or was preserved because what it named changed since the plan: it moved, or its commit left
the default and remote branches. 1 means something needs the agent: a repository that stopped (DIRTY_MAIN,
FETCH_FAILED) or was skipped with an ERROR line, a deletion or removal Git refused, a confirmed worktree removal that
did not happen because what the worktree holds kept it, a directory whose deletion failed, that no longer resolves
inside its area, or in which Git now tracks a file, or a last line FAILED <reason> when the command could not run at
all. report exits 0, or 1 with its FAILED line. 2 is a usage error.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import cleanup_policy
import git_client
import git_worktrees
import pr_status
from atomic_file import replace_bytes, replace_text
from cleanup_policy import Policy, Regenerable
from console import run_main
from flat_text import single_line
from git_client import GitClient, GitResult
from git_worktrees import Worktree
from github_client import GitHubClient, GitHubError, replace_undecodable, subprocess_runner
from github_client import Runner as GhRunner
from github_remote import github_repository
from link_probe import is_plain_directory, is_plain_file
from skill_roots import skills_directory_holding
from typed_fields import FieldError, FieldReader, describe

T = TypeVar("T")
PLAN_SCHEMA_VERSION = 1
# Where each plan's and sweep record's seal lives: the SHA-256 of its bytes as this script last saved them, in a file
# named by the SHA-256 of its resolved path. Every command reads a plan or record only when its bytes match its seal, so
# what the confirmation commands may delete is what a sweep offered and the later commands recorded: a file the caller
# names that this script never saved there has no seal, and one changed since it saved it no longer matches. The seals
# lie outside every plans directory, so no directory a plan sits in carries a seal for it.
SEALS = Path(tempfile.gettempdir()) / "repo-cleanup-seals"
# The fields a plan holds, as build_plan writes them, and those a plan built outside a sweep, or a command that never
# stopped, leaves out.
PLAN_FIELDS = (
    "schema_version",
    "repo_root",
    "repo_name",
    "worktree_area",
    "orphan_areas",
    "default",
    "branches",
    "worktrees",
    "fastforward",
    "diverged",
    "applied",
    "events",
)
OPTIONAL_PLAN_FIELDS = ("fastforward_skipped", "policy", "sync", "stopped")
# How many fields after its kind each event a plan records carries, at least and at most.
EVENT_FIELDS = {
    "DELETED": (1, 1),
    "PRESERVED": (2, 2),
    "UNMERGED": (2, 2),
    "CONFIRM_LOCAL": (1, 2),
    "CONFIRM_DETACHED": (2, 2),
    "ORPHAN": (1, 1),
    "REMOVED": (2, 3),
    "DELETED_DIR": (1, 1),
    "PRUNED_DIR": (1, 1),
    "FF": (2, 2),
    "DIVERGED": (3, 3),
}
STALE_STATES = ("MERGED", "CLOSED")
# What a worktree's own Git directory holds while an operation is under way, with the operation's name. Removing the
# worktree deletes that directory and the operation with it.
OPERATION_MARKERS = (
    ("BISECT_START", "a bisect"),
    ("BISECT_LOG", "a bisect"),
    ("rebase-merge", "a rebase"),
    ("rebase-apply", "a rebase or git am"),
    ("MERGE_HEAD", "a merge"),
    ("CHERRY_PICK_HEAD", "a cherry-pick"),
    ("REVERT_HEAD", "a revert"),
    ("sequencer", "a cherry-pick or revert sequence"),
)
# The refs only one worktree sees, which its removal deletes with it.
WORKTREE_REFS = ("refs/worktree/", "refs/bisect/", "refs/rewritten/")
# The index tags `git ls-files -v` gives a file whose skip-worktree bit is set; assume-unchanged ones are lower case.
SKIP_WORKTREE_TAG = "S"
# File modes whose content `git hash-object` reads from the file itself.
REGULAR_MODES = ("100644", "100755")
# How many paths one `git hash-object` names, so its command line stays inside Windows' limit.
HASHED_PER_CALL = 100
# How many paths a report line names before it counts the rest, so a worktree full of them does not flood the line.
NAMED_PATHS = 5
# git worktree remove checks for changes with a status of its own, which reads status.showUntrackedFiles, so it is
# named as git_worktrees.STATUS_ARGUMENTS names the untracked-files mode of every reading of a worktree's changes.
SHOW_UNTRACKED = ("-c", "status.showUntrackedFiles=normal")
KEPT_STATES = {"OPEN": "pr-open", "UNMATCHED": "pr-unmatched", "UNKNOWN": "pr-unknown"}
WORKTREE_AREA = "Worktrees"
# What a refusal of a plan's state tells the agent to do: the command that makes and applies plans.
SWEEP_AGAIN = "run the sweep again for a new plan"
# Where Claude Code, and many repositories' own worktree tools, put a repository's worktrees. With
# WORKTREE_AREA/<repository name>, the folders a sweep searches for directories a worktree removal left behind.
REPOSITORY_AREA = (".claude", "worktrees")
# What a clean linked worktree becomes, by its branch's planned action: removed, offered, or else kept.
WORKTREE_ACTIONS = {"remove-worktree": "remove", "remove-worktree-ask": "remove", "ask-remove-worktree": "ask"}
SUMMARY_WIDTH = 25
# The file in a sweep's plans directory naming each repository a sweep there reported, its state, and its plan file,
# so the report covers every repository, those no plan was made for included. No plan file is named like it: plan
# files end in .json.
SWEEP_RECORD = "sweep.record"
RECORD_SCHEMA_VERSION = 1
# The configured values a plan records its policy by, in the order cleanup_policy.configured takes them.
POLICY_FIELDS = ("regenerable", "protected")
# A repository's lines the record keeps when no plan holds its summary: what the report repeats for it.
RECORDED_KINDS = ("SUMMARY", "ERROR", "CHECKOUT", "FF_DEFAULT")
SWEEP_STATES = ("cleaned", "quiet", "dirty", "fetch-failed", "error", "git-failed")
FAILED_STATES = ("error", "git-failed")
# Repositories a sweep cleans at once: fetches and gh queries are network-bound, and four stays clear of GitHub's
# secondary rate limits.
SWEEP_WORKERS = 4
# The lines a sweep passes on: what the agent must act on or report. Everything else is in the summary.
SWEEP_KINDS = {
    "DIRTY_MAIN",
    "FETCH_FAILED",
    "ERROR",
    "CONFIRM_LOCAL",
    "UNMERGED",
    "CONFIRM_DETACHED",
    "ORPHAN",
    "SUMMARY",
}
# What sync did to the default branch, passed on only for a repository with no plan summary to carry it.
SYNC_KINDS = {"CHECKOUT": {"switched"}, "FF_DEFAULT": {"forwarded", "skipped", "diverged", "failed"}}
# Why the GitHub CLI cannot reach github.com, by GitHubError.kind; other kinds are named as they are.
ACCESS_FAILURES = {
    "prerequisite": "GitHub CLI not installed",
    "network": "cannot reach github.com",
    "timeout": "github.com did not answer",
    "rate_limit": "GitHub's rate limit refused the GitHub CLI",
}


class CleanupError(Exception):
    """An expected problem with one repository: report it and skip the repository."""


class GitUnavailable(Exception):
    """Git itself could not run: report it and skip the repository."""


@dataclass
class Services:
    """External effects, replaceable in tests."""

    git: GitClient = dataclasses.field(default_factory=GitClient)
    gh: GhRunner = subprocess_runner


# Output ---------------------------------------------------------------------------------------------------------


def one_line(value: Any) -> str:
    """`value` on one line, as `single_line` keeps it, with each byte Git gave that is not UTF-8, such as in a branch
    name, as U+FFFD."""
    return single_line(replace_undecodable(str(value))[0])


_capture = threading.local()


def emit(kind: str, *fields: Any) -> None:
    line = "\t".join([kind, *(one_line(field) if str(field).strip() else "-" for field in fields)])
    captured = getattr(_capture, "lines", None)
    if captured is None:
        print(line)
    else:
        captured.append(line)  # a sweep worker: printed with its repository once every repository is done


def reason(result: GitResult) -> str:
    return one_line(result.stderr or result.stdout or "").strip() or f"exit status {result.returncode}"


# Git ------------------------------------------------------------------------------------------------------------


def git(services: Services, directory: str | Path, *arguments: str) -> GitResult:
    try:
        return services.git.run(["--no-optional-locks", *arguments], directory=directory)
    except git_client.GitError as exc:
        raise GitUnavailable(f"could not run git: {exc}") from exc


def git_output(services: Services, directory: str | Path, *arguments: str) -> str:
    result = git(services, directory, *arguments)
    if result.returncode != 0:
        raise CleanupError(f"git {arguments[0]} failed in {directory}: {reason(result)}")
    return result.stdout


def resolve(services: Services, root: str | Path, ref: str) -> str | None:
    result = git(services, root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and pr_status.SHA_PATTERN.fullmatch(sha) else None


def branch_tip(services: Services, root: str | Path, branch: str) -> str | None:
    return resolve(services, root, f"refs/heads/{branch}")


def default_refs(default: str) -> tuple[str, str]:
    """The local and the fetched default branch, either of which proves a commit landed."""
    return f"refs/heads/{default}", f"refs/remotes/origin/{default}"


def reachable(services: Services, root: str | Path, sha: str, *refs: str) -> bool:
    """Whether one of `refs` contains `sha`; a ref names itself or, as `refs/remotes` does, every ref below it."""
    result = git(services, root, "for-each-ref", "--count=1", f"--contains={sha}", "--format=%(refname)", *refs)
    return result.returncode == 0 and bool(result.stdout.strip())


def ahead_behind(services: Services, root: str | Path, local: str, upstream: str) -> tuple[int, int]:
    ahead, behind = git_output(services, root, "rev-list", "--left-right", "--count", f"{local}...{upstream}").split()
    return int(ahead), int(behind)


def read_worktree(
    services: Services, directory: str | Path, subcommand: str, reader: Callable[..., T], *options: str
) -> T:
    """What ejryan89-skill-core's `reader` reads of the checkout at `directory` through `git <subcommand>`, a git that
    could not run raised as GitUnavailable and one that failed as CleanupError, as every other git call here raises
    them."""
    try:
        return reader(services.git, directory, *options)
    except git_client.GitError as exc:
        if exc.returncode is None:
            raise GitUnavailable(f"could not run git: {exc}") from exc
        raise CleanupError(f"git {subcommand} failed in {directory}: {one_line(exc).strip()}") from exc


def list_worktrees(services: Services, root: str | Path) -> list[Worktree]:
    """Every worktree of the repository; the first is the main worktree."""
    return read_worktree(services, root, "worktree", git_worktrees.list_worktrees)


def checkouts(services: Services, root: str | Path) -> dict[str, Worktree]:
    return {tree.branch: tree for tree in list_worktrees(services, root) if tree.branch}


def same_path(first: str, second: str) -> bool:
    # Lexical: Path.absolute keeps ".." and Path.resolve follows junctions, which would change what matches.
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))  # noqa: PTH100 - lexical


def dirty_count(services: Services, directory: str | Path) -> int:
    """Number of changed or untracked paths, a rename or copy counted once."""
    return read_worktree(services, directory, "status", git_worktrees.changed_paths)


def uncommitted(services: Services, path: str) -> str:
    """Why a fast-forward must leave the worktree at `path` alone, or an empty string when it has no changes."""
    try:
        count = dirty_count(services, path)
    except CleanupError as exc:
        return str(exc)
    return f"worktree {path} has {count} uncommitted change{'' if count == 1 else 's'}" if count else ""


def name_with_owner(services: Services, root: str | Path) -> str | None:
    """owner/name of a github.com origin, from the configured URL or its insteadOf expansion."""
    for arguments in (("config", "--get", "remote.origin.url"), ("remote", "get-url", "origin")):
        result = git(services, root, *arguments)
        repository = github_repository(result.stdout) if result.returncode == 0 else None
        if repository:
            return repository
    return None


# Branches and worktrees -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DefaultBranch:
    """The branch a sweep treats as the default, and `note`, what was done about a dangling origin/HEAD to find it:
    empty when origin/HEAD named a branch origin has, or was unset."""

    name: str
    note: str = ""


def origin_has(services: Services, root: str | Path, name: str) -> bool:
    return git(services, root, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{name}").returncode == 0


def origin_head(services: Services, root: str | Path) -> str | None:
    """The branch refs/remotes/origin/HEAD names, whether or not origin has it, or None when it is unset."""
    head = git(services, root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    target = head.stdout.strip()
    name = target.removeprefix("refs/remotes/origin/")
    return name if head.returncode == 0 and name and name != target else None


def refresh_origin_head(services: Services, root: str | Path) -> tuple[str | None, str]:
    """Point origin/HEAD at the branch origin names as its default now, as `git remote set-head origin --auto` does.

    The branch it now names, or None with the reason: origin could not be reached, did not name a default branch, or
    named one the fetch did not bring. The command asks the remote within the time limit the fetch has.
    """
    result = git(services, root, "remote", "set-head", "origin", "--auto")
    if result.returncode != 0:
        first = next((line for line in (result.stderr or result.stdout).splitlines() if line.strip()), "")
        return None, one_line(first).strip() or f"exit status {result.returncode}"
    name = origin_head(services, root)
    if name is None or not origin_has(services, root, name):
        return None, "origin/HEAD still names no branch origin has"
    return name, ""


def current_branch(services: Services, root: str | Path) -> str:
    head = git(services, root, "symbolic-ref", "--quiet", "--short", "HEAD")
    return head.stdout.strip() if head.returncode == 0 and head.stdout.strip() else "a detached HEAD"


def default_branch(services: Services, root: str | Path) -> DefaultBranch:
    """The branch origin/HEAD names when origin has it, else main or master when origin has it.

    A dangling origin/HEAD, one naming a branch origin no longer has, is never followed, since a sweep switches the
    repository to the branch this returns: origin renamed its default branch after the clone, or the clone was taken
    from a checkout on a topic branch. It is refreshed from origin, and when that fails main or master stands in.
    When nothing gives a branch origin has, the repository is left on its branch with a CleanupError that names it.
    """
    named = origin_head(services, root)
    if named is not None and origin_has(services, root, named):
        return DefaultBranch(named)
    failure = ""
    if named is not None:
        refreshed, failure = refresh_origin_head(services, root)
        if refreshed is not None:
            return DefaultBranch(refreshed, f"origin/HEAD named {named}, which origin lacks: refreshed it from origin")
    for name in ("main", "master"):
        if origin_has(services, root, name):
            if named is None:
                return DefaultBranch(name)
            note = (
                f"origin/HEAD names {named}, which origin lacks: fell back to {name} — refreshing it failed: {failure}"
            )
            return DefaultBranch(name, note)
    why = (
        f"origin/HEAD names {named}, which origin lacks, refreshing it failed ({failure}),"
        if named is not None
        else "origin/HEAD is unset"
    )
    raise CleanupError(
        f"cannot determine default branch: {why} and origin has no main or master; left on "
        f"{current_branch(services, root)}"
    )


def is_protected(branch: str, path: str, policy: Policy) -> bool:
    """A branch starting with a protected prefix, or a worktree with a folder anywhere in its path that starts with
    one, as a release worktree's folder does, in any letter case."""
    if policy.protecting(branch):
        return True
    folders = f"/{path.replace(chr(92), '/').removeprefix('/')}/".casefold()
    return any(f"/{prefix.casefold()}" in folders for prefix in policy.protected)


def pull_state(
    services: Services, root: str | Path, nwo: str | None, default: str, branch: str, sha: str
) -> tuple[str, str]:
    """The pr_status classification of the branch, or UNKNOWN with the reason it could not be verified."""
    if nwo is None:
        return "UNKNOWN", "origin is not a github.com repository"
    try:
        head, upstream = pr_status.branch_tips(str(root), branch, services.git)
        if head != sha:
            return "UNKNOWN", "branch moved while planning"
        bases = pr_status.fetched_bases(str(root), services.git)
        return pr_status.classify(nwo, branch, head, upstream, services.gh, bases, default), ""
    except pr_status.QueryError as exc:
        return "UNKNOWN", str(exc)


def fast_forward(services: Services, root: str, branch: str, old: str, new: str, worktree: str | None) -> str:
    """Fast-forward only; returns an empty string on success, otherwise the reason.

    Never in a worktree with changes: Git refuses only when a changed file conflicts, so the base would move under
    someone's uncommitted work.
    """
    if worktree is None:
        result = git(services, root, "update-ref", "-m", "repo-cleanup: fast-forward", f"refs/heads/{branch}", new, old)
    else:
        head = git(services, worktree, "symbolic-ref", "--quiet", "HEAD").stdout.strip()
        if head != f"refs/heads/{branch}":
            return "the worktree no longer has the branch checked out"
        held = uncommitted(services, worktree)
        if held:
            return held
        result = git(services, worktree, "merge", "--ff-only", "--quiet", new)
    return "" if result.returncode == 0 else reason(result)


def require_repository(root: str | Path) -> None:
    if not (Path(root) / ".git").is_dir():
        raise CleanupError(f"{root} is not a git repository")


# discover -------------------------------------------------------------------------------------------------------


def discover(target: str | None, repos_root: str, services: Services) -> list[str]:
    # A plugin passes its repos_root option as typed, with no allowlist behind it: an empty root would sweep the
    # working directory, and a relative one would resolve against it.
    if not repos_root.strip():
        raise CleanupError("--repos-root is empty; set the root directory for your git repositories")
    if not Path(repos_root).is_absolute():
        raise CleanupError(f"--repos-root {repos_root} is not an absolute path")
    if target:
        candidate = Path(target)
        if not candidate.is_absolute():
            if target in (".", "..") or any(separator in target for separator in "/\\:"):
                raise CleanupError(f"{target} is not a repository name or an absolute path")
            candidate = Path(repos_root) / target
        if not (candidate / ".git").is_dir():
            raise CleanupError(f"{target} is not a git repository")
        repositories = [candidate]
    else:
        root = Path(repos_root)
        if not root.is_dir():
            raise CleanupError(f"{repos_root} is not a directory")
        entries = sorted(root.iterdir(), key=lambda path: path.name.casefold())
        repositories = [path for path in entries if path.is_dir() and (path / ".git").is_dir()]
    # Not `gh auth status`: it reports every failure but a timeout as an invalid token, and fails for any stale account
    # on the host. The rate_limit endpoint costs no quota and answers for the account gh uses.
    try:
        GitHubClient(services.gh).run(["api", "--hostname", "github.com", "rate_limit"])
    except GitHubError as exc:
        raise CleanupError(access_failure(exc)) from exc
    return [path.as_posix() for path in repositories]


def access_failure(error: GitHubError) -> str:
    if error.kind == "authentication":
        return "GitHub CLI not authenticated — run 'gh auth login'"
    label = ACCESS_FAILURES.get(error.kind, f"GitHub CLI access check failed ({error.kind})")
    return f"{label} — {one_line(error)}"


# sync -----------------------------------------------------------------------------------------------------------


def sync(root: str, skip_checkout: bool, services: Services) -> dict[str, Any] | None:
    """Fetch and prune, then switch to the default branch and fast-forward it.

    The default branch is chosen after the fetch, from the remote-tracking refs it left: before it, a branch origin
    renamed or deleted still has its stale remote-tracking ref. None when it stopped for the user; otherwise what it
    did to the default branch, for the repository's summary.
    """
    require_repository(root)
    dirty = dirty_count(services, root)
    if dirty:
        emit("DIRTY_MAIN", dirty)
        if not skip_checkout:
            return None
    fetched = git(services, root, "fetch", "--all", "--prune", "--quiet")
    if fetched.returncode != 0:
        error = reason(fetched)
        emit("FETCH_FAILED", error)
        for line in fetch_failed_summary(Path(root).name, error):
            emit("SUMMARY", line)
        return None
    default = default_branch(services, root)
    if skip_checkout:
        checkout = "skipped"
        emit("CHECKOUT", checkout)
    elif git(services, root, "symbolic-ref", "--quiet", "HEAD").stdout.strip() == f"refs/heads/{default.name}":
        checkout = "current"
        emit("CHECKOUT", checkout)
    else:
        switched = git(services, root, "switch", "--quiet", default.name)
        checkout = "switched" if switched.returncode == 0 else "failed"
        emit("CHECKOUT", checkout, *([] if switched.returncode == 0 else [reason(switched)]))
    git_output(services, root, "worktree", "prune")
    outcome, behind = fast_forward_default(services, root, default.name)
    emit("FF_DEFAULT", *outcome)
    return {
        "default": dataclasses.asdict(default),
        "checkout": checkout,
        "fast_forward": [one_line(field) for field in outcome],
        "behind": behind,
    }


def fast_forward_default(services: Services, root: str, default: str) -> tuple[tuple[Any, ...], int]:
    """The FF_DEFAULT outcome, and how far the default branch was behind origin before it."""
    local = branch_tip(services, root, default)
    target = resolve(services, root, f"refs/remotes/origin/{default}")
    if local is None or target is None:
        return ("skipped", f"no {'local branch' if local is None else 'origin/' + default}"), 0
    ahead, behind = ahead_behind(services, root, local, target)
    if behind == 0:
        return ("current",), 0
    if ahead:
        return ("diverged", ahead, behind), behind
    tree = checkouts(services, root).get(default)
    error = fast_forward(services, root, default, local, target, tree.path if tree else None)
    return (("forwarded", target) if not error else ("failed", error)), behind


def fetch_failed_summary(name: str, error: str) -> list[str]:
    return [
        f"{name} skipped: fetch failed — {error}",
        "  No branches were deleted, no worktrees were pruned or removed, and nothing was fast-forwarded.",
    ]


# plan -----------------------------------------------------------------------------------------------------------


def decide(
    category: str,
    state: str,
    error: str,
    tree: Worktree | None,
    linked: dict[str, Any] | None,
    landed: bool = False,
) -> tuple[str, str]:
    """The planned action for one non-release branch, mirroring the cleanup rules.

    A clean linked worktree of a stale branch is removed. Its branch is deleted with it unless its pull request closed
    without merging: that work was set aside rather than landed, so the branch is kept and the user is asked. A branch
    with a remote and no pull request is stale when `landed`, the default branch holding its tip and its upstream's,
    since nothing on it is its own. A clean linked worktree of a local-only branch with no pull request is offered:
    the user is asked before it and its branch go, since it may hold a task just started.
    """
    if state in KEPT_STATES:
        return "keep", f"{KEPT_STATES[state]}: {error}" if error else KEPT_STATES[state]
    merged = landed and category == "tracking" and state == "NONE"
    stale = state in STALE_STATES or (category == "gone" and state == "NONE") or merged
    if tree is not None and tree.main:
        return "keep", "checked-out-main" if stale or category == "local" else ""
    if linked is not None:
        if linked["action"] == "candidate":
            if stale:
                return ("remove-worktree-ask", "") if state == "CLOSED" else ("remove-worktree", "")
            if category == "local" and state == "NONE":
                return "ask-remove-worktree", ""
            return "keep", "in-worktree"
        return "keep", f"worktree-{linked['action'].split(':')[0]}"
    if (category == "gone" and stale) or merged:
        return "delete", ""
    if category == "local" and state in (*STALE_STATES, "NONE"):
        return "ask-delete", ""
    return "keep", ""


def evaluate_worktree(
    services: Services, root: str, default: str, tree: Worktree, policy: Policy | None = None
) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": tree.path, "branch": tree.branch, "action": "keep", "detail": ""}
    if is_protected(tree.branch or "", tree.path, policy or cleanup_policy.configured()):
        entry["action"] = "protected"
        return entry
    try:
        changed = dirty_count(services, tree.path)
    except CleanupError as exc:
        entry["detail"] = str(exc)
        return entry
    held = "" if changed else held_state(services, tree.path)
    if tree.branch is None:
        entry.update(judge_detached(services, root, default, tree.head, changed, held))
    elif changed:
        entry["action"] = f"dirty:{changed}"
    elif held:
        entry.update(action="held", detail=held)
    else:
        entry["action"] = "candidate"
    return entry


def judge_detached(
    services: Services, root: str, default: str, head: str, changed: int, held: str = ""
) -> dict[str, Any]:
    """A worktree with no branch is offered for removal only when it is clean, holds nothing else its removal would
    lose (`held`, from held_state), and the default branch or a remote branch holds its commit, so removing it loses
    nothing; otherwise it is kept, and the detail says why."""
    if changed:
        return {"sha": head, "detail": counted(changed, "uncommitted change")}
    if held:
        return {"sha": head, "detail": held}
    if not head or not reachable(services, root, head, default_refs(default)[0], "refs/remotes"):
        return {"sha": head, "detail": f"its commit {head[:12] or '-'} is on neither {default} nor a remote branch"}
    return {"sha": head, "action": "ask", "detail": f"clean, its commit is on {default} or a remote branch"}


def landed_in_default(services: Services, root: str, default: str, name: str, sha: str, upstream: str) -> bool:
    """Whether the branch was pushed under its own name and the default branch holds its tip and its upstream's:
    nothing on it is its own.

    Its own name is exactly origin/<name>. A branch whose upstream is another branch, as one started from origin/main
    tracks origin/main and foo started from origin/feature/foo tracks that, never counts: it is a task just started,
    not one finished.
    """
    if upstream != f"refs/remotes/origin/{name}":
        return False
    tips = [sha, resolve(services, root, upstream) or sha]
    return all(reachable(services, root, tip, *default_refs(default)) for tip in dict.fromkeys(tips))


def build_plan(
    root: str,
    repos_root: str,
    services: Services,
    chosen: DefaultBranch | None = None,
    policy: Policy | None = None,
) -> dict[str, Any]:
    """The repository's plan, against `chosen`, the default branch sync found, or the one found now when None, under
    `policy`, the shipped lists alone when None, which the plan records for every later command on it."""
    policy = policy or cleanup_policy.configured()
    require_repository(root)
    default = (chosen or default_branch(services, root)).name
    nwo = name_with_owner(services, root)
    trees = list_worktrees(services, root)
    located = {tree.branch: tree for tree in trees if tree.branch}
    worktrees = [evaluate_worktree(services, root, default, tree, policy) for tree in trees if not tree.main]
    linked = {entry["branch"]: entry for entry in worktrees if entry["branch"]}
    branches: list[dict[str, Any]] = []
    moves: dict[str, list[dict[str, Any]]] = {"fastforward": [], "fastforward_skipped": [], "diverged": []}
    listing = git_output(
        services,
        root,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(upstream)%00%(upstream:track)",
        "refs/heads/",
    )
    # Newlines only: str.splitlines also breaks on characters a branch name may hold, such as U+2028.
    for line in filter(None, listing.split("\n")):
        ref, sha, upstream, track = line.split("\0")
        name = ref[len("refs/heads/") :]
        if name == default:
            continue
        category = "local" if not upstream else "gone" if track == "[gone]" else "tracking"
        tree, entry = located.get(name), linked.get(name)
        candidate = entry is not None and entry["action"] == "candidate"
        state, error, landed = "-", "", False
        if policy.protecting(name):
            action, detail = "keep", "protected"
        else:
            if category == "tracking" and (candidate or tree is None):
                landed = landed_in_default(services, root, default, name, sha, upstream)
            if category != "tracking" or candidate or landed:
                state, error = pull_state(services, root, nwo, default, name, sha)
            action, detail = decide(category, state, error, tree, entry, landed)
        if entry is not None and candidate:
            entry["action"] = WORKTREE_ACTIONS.get(action, "keep")
            entry["detail"] = "" if entry["action"] != "keep" else detail
        branches.append(
            {
                "name": name,
                "category": category,
                "pr": state,
                "sha": sha,
                "action": action,
                "detail": detail,
                "worktree": tree.path if tree else None,
            }
        )
        if category == "tracking" and action == "keep":
            follow_upstream(services, root, name, sha, upstream, tree, moves)
    for entry in worktrees:
        if entry["action"] == "candidate":
            entry["action"] = "keep"
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "repo_root": str(root),
        "repo_name": Path(root).name,
        "worktree_area": str(Path(repos_root) / WORKTREE_AREA),
        "orphan_areas": [str(Path(repos_root) / WORKTREE_AREA / Path(root).name), str(Path(root, *REPOSITORY_AREA))],
        "default": {"name": default, "status": default_status(services, root, default)},
        "branches": branches,
        "worktrees": worktrees,
        **moves,
        "policy": dict(zip(POLICY_FIELDS, policy.given, strict=True)),
        "applied": False,
        "events": [],
    }


def follow_upstream(
    services: Services,
    root: str,
    name: str,
    sha: str,
    upstream: str,
    tree: Worktree | None,
    moves: dict[str, list[dict[str, Any]]],
) -> None:
    """Plan a kept branch behind its upstream: a fast-forward, a skip when its worktree has changes, or divergence."""
    target = resolve(services, root, upstream)
    if target is None or target == sha:
        return
    ahead, behind = ahead_behind(services, root, sha, target)
    if ahead:
        if behind:
            moves["diverged"].append({"branch": name, "ahead": ahead, "behind": behind})
        return
    held = uncommitted(services, tree.path) if tree else ""
    if held:
        moves["fastforward_skipped"].append({"branch": name, "behind": behind, "reason": held})
    else:
        moves["fastforward"].append(
            {"branch": name, "sha": sha, "target": target, "worktree": tree.path if tree else None}
        )


def default_status(services: Services, root: str, default: str) -> str:
    local = branch_tip(services, root, default)
    remote = resolve(services, root, f"refs/remotes/origin/{default}")
    if local is None:
        return "no local branch"
    if remote is None:
        return f"no origin/{default}"
    ahead, behind = ahead_behind(services, root, local, remote)
    if ahead and behind:
        return f"diverged: ahead {ahead}, behind {behind}"
    if ahead or behind:
        return f"ahead {ahead}" if ahead else f"behind {behind}"
    return "up to date"


# Plan file ------------------------------------------------------------------------------------------------------


def policy_of(plan: dict[str, Any]) -> Policy:
    """The policy the plan was made under, rebuilt from the configured values it recorded, or the shipped lists
    alone for a plan that recorded none."""
    recorded = plan.get("policy") or {}
    return cleanup_policy.configured(*(recorded.get(field, "") for field in POLICY_FIELDS))


def save_plan(path: str | Path, document: dict[str, Any]) -> None:
    """Write a plan, or a sweep record, in one step, so a reader sees the old or the new one, never part of either;
    then its seal: the digest of the bytes written, which every command compares with the file before it reads it."""
    data = (json.dumps(document, indent=2) + "\n").encode("utf-8")
    replace_bytes(Path(path), data)
    replace_text(seal_file(path), hashlib.sha256(data).hexdigest() + "\n")


def seal_file(path: str | Path) -> Path:
    """Where the seal of the plan or record at `path` lies: named by its path resolved through links, in any letter
    case the file system treats as the same."""
    resolved = os.path.normcase(os.path.realpath(path))
    return SEALS / hashlib.sha256(resolved.encode("utf-8", "surrogatepass")).hexdigest()


def read_sealed(path: str | Path, what: str) -> bytes:
    """The bytes of the plan or record at `path`, named `what` in a refusal: refused unless they match the seal this
    script wrote when it last saved one there, so what they name is what a sweep recorded. A path with no seal, which
    the script never saved one at, and one whose bytes changed since are refused apart."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise CleanupError(f"cannot read {what} {path}: {exc}") from exc
    try:
        seal = seal_file(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CleanupError(
            f"{path} is not a {what} repo-cleanup saved at this path, so it may name what no sweep offered; "
            f"{SWEEP_AGAIN}"
        ) from exc
    if seal != hashlib.sha256(data).hexdigest():
        raise CleanupError(
            f"{path} changed since repo-cleanup last saved this {what} there, so it may name what no sweep offered; "
            f"{SWEEP_AGAIN}"
        )
    return data


def load_plan(path: str | Path) -> dict[str, Any]:
    """The plan at `path`, as this script last saved it there, refused with the first field that is not as
    build_plan writes it, so no command reads a missing or malformed field."""
    data = read_sealed(path, "plan")
    try:
        plan = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise CleanupError(f"cannot read plan {path}: {exc}") from exc
    try:
        read_plan_fields(plan)
    except FieldError as exc:
        raise CleanupError(f"{path} is not a repo-cleanup plan: {exc}") from exc
    result: dict[str, Any] = plan
    return result


def plan_in_scope(path: str | Path) -> dict[str, Any]:
    """The plan at `path`, for a command that deletes: as load_plan reads it, and refused unless what it names still
    holds, its repository root a Git repository and its worktree areas the ones build_plan derives from it."""
    plan = load_plan(path)
    require_derived_areas(path, plan)
    require_repository(plan["repo_root"])
    return plan


def require_derived_areas(path: str | Path, plan: dict[str, Any]) -> None:
    """Refuse a plan whose repository name and worktree areas are not the ones build_plan derives from its repository
    root and shared worktree area."""
    root, area = Path(plan["repo_root"]), Path(plan["worktree_area"])
    derived = [str(area / root.name), str(root.joinpath(*REPOSITORY_AREA))]
    areas: list[str] = plan["orphan_areas"]
    if not (
        root.is_absolute()
        and area.is_absolute()
        and area.name == WORKTREE_AREA
        and plan["repo_name"] == root.name
        and len(areas) == len(derived)
        and all(same_path(given, expected) for given, expected in zip(areas, derived, strict=True))
    ):
        raise CleanupError(f"{path} names worktree areas other than the ones a sweep derives for {root}; {SWEEP_AGAIN}")


def read_plan_fields(plan: object) -> None:
    """Hold every field of a plan to the kind build_plan, sync, and the commands write, raising FieldError for the
    first that is not."""
    fields = FieldReader()
    document = fields.json_object(plan, "plan", required=PLAN_FIELDS, optional=OPTIONAL_PLAN_FIELDS)
    fields.one_of(document["schema_version"], "schema_version", (PLAN_SCHEMA_VERSION,))
    for name in ("repo_root", "repo_name", "worktree_area"):
        fields.string(document[name], name)
    fields.array(document["orphan_areas"], "orphan_areas", fields.string)
    default = fields.json_object(document["default"], "default", required=("name", "status"))
    read_branch_name(fields, default["name"], "default.name")
    fields.string(default["status"], "default.status")
    fields.array(document["branches"], "branches", lambda value, path: read_branch(fields, value, path))
    fields.array(document["worktrees"], "worktrees", lambda value, path: read_worktree_entry(fields, value, path))
    fields.array(document["fastforward"], "fastforward", lambda value, path: read_move(fields, value, path))
    fields.array(
        document.get("fastforward_skipped", []),
        "fastforward_skipped",
        lambda value, path: read_counted(fields, value, path, ("reason",), ("behind",)),
    )
    fields.array(
        document["diverged"],
        "diverged",
        lambda value, path: read_counted(fields, value, path, (), ("ahead", "behind")),
    )
    fields.boolean(document["applied"], "applied")
    fields.array(document["events"], "events", lambda value, path: read_event(fields, value, path))
    if "policy" in document and not recorded_policy(document["policy"]):
        raise fields.fail(
            "policy", "the configured values a sweep records, each a string", describe(document["policy"])
        )
    if "sync" in document:
        read_sync(fields, document["sync"])
    if "stopped" in document:
        fields.string(document["stopped"], "stopped")


def read_branch_name(fields: FieldReader, value: object, path: str) -> str:
    """A branch name: any string, a blank one included, since Git accepts a name of Unicode whitespace alone, such as
    a no-break space, and the sweep keeps such a branch under Preserved rather than refusing its own plan."""
    return fields.string(value, path, empty=True)


def read_branch(fields: FieldReader, value: object, path: str) -> None:
    branch = fields.json_object(value, path, required=("name", "category", "pr", "sha", "action", "detail", "worktree"))
    read_branch_name(fields, branch["name"], f"{path}.name")
    for name in ("pr", "sha", "action"):
        fields.string(branch[name], f"{path}.{name}")
    fields.one_of(branch["category"], f"{path}.category", ("local", "gone", "tracking"))
    fields.string(branch["detail"], f"{path}.detail", empty=True)
    fields.optional(fields.string, branch["worktree"], f"{path}.worktree")


def read_worktree_entry(fields: FieldReader, value: object, path: str) -> None:
    entry = fields.json_object(value, path, required=("path", "branch", "action", "detail"), optional=("sha",))
    fields.string(entry["path"], f"{path}.path")
    if entry["branch"] is not None:
        read_branch_name(fields, entry["branch"], f"{path}.branch")
    fields.string(entry["action"], f"{path}.action")
    fields.string(entry["detail"], f"{path}.detail", empty=True)
    if "sha" in entry:
        fields.string(entry["sha"], f"{path}.sha", empty=True)


def read_move(fields: FieldReader, value: object, path: str) -> None:
    entry = fields.json_object(value, path, required=("branch", "sha", "target", "worktree"))
    read_branch_name(fields, entry["branch"], f"{path}.branch")
    for name in ("sha", "target"):
        fields.string(entry[name], f"{path}.{name}")
    fields.optional(fields.string, entry["worktree"], f"{path}.worktree")


def read_counted(
    fields: FieldReader, value: object, path: str, strings: tuple[str, ...], counts: tuple[str, ...]
) -> None:
    """An entry naming a branch, with the other strings `strings` and the counts `counts`."""
    entry = fields.json_object(value, path, required=("branch", *strings, *counts))
    read_branch_name(fields, entry["branch"], f"{path}.branch")
    for name in strings:
        fields.string(entry[name], f"{path}.{name}")
    for name in counts:
        fields.integer(entry[name], f"{path}.{name}", minimum=0)


def read_event(fields: FieldReader, value: object, path: str) -> None:
    """An event: its kind, then as many fields, each a string, as EVENT_FIELDS gives that kind."""
    event = fields.array(value, path, nonempty=True)
    kind = fields.one_of(event[0], f"{path}[0]", tuple(EVENT_FIELDS))
    least, most = EVENT_FIELDS[kind]
    if not least <= len(event) - 1 <= most:
        count = f"{least}" if least == most else f"{least} to {most}"
        found = len(event) - 1
        raise fields.fail(path, f"a {kind} event with {count} fields", f"{found} field{'' if found == 1 else 's'}")
    for index, field in enumerate(event[1:], start=1):
        fields.string(field, f"{path}[{index}]", empty=True)


def read_sync(fields: FieldReader, value: object) -> None:
    synced = fields.json_object(value, "sync", required=("default", "checkout", "fast_forward", "behind"))
    default = fields.json_object(synced["default"], "sync.default", required=("name",), optional=("note",))
    read_branch_name(fields, default["name"], "sync.default.name")
    if "note" in default:
        fields.string(default["note"], "sync.default.note", empty=True)
    fields.string(synced["checkout"], "sync.checkout")
    outcome = fields.array(synced["fast_forward"], "sync.fast_forward", fields.string, nonempty=True)
    if outcome[0] == "failed" and len(outcome) < 2:
        raise fields.fail("sync.fast_forward", "a failed outcome with its reason", "no reason")
    fields.integer(synced["behind"], "sync.behind", minimum=0)


def recorded_policy(value: Any) -> bool:
    """Whether a plan's policy is the configured values a sweep records, each a string."""
    return (
        isinstance(value, dict)
        and set(value) <= set(POLICY_FIELDS)
        and all(isinstance(given, str) for given in value.values())
    )


def record(plan: dict[str, Any], kind: str, *fields: Any) -> None:
    plan["events"].append([kind, *(one_line(field) for field in fields)])
    emit(kind, *fields)


def offer_line(plan: dict[str, Any], kind: str, *fields: Any) -> bool:
    """Record an offer the user answers by naming its first fields back, unless a name in it does not survive being
    printed on one line: a confirmation could then only name it by its flattened form, which names nothing.

    Such an offer is a PRESERVED fact instead, naming the first field by its flattened form with the reason. False
    then.
    """
    altered = next((str(field) for field in fields if one_line(field) != str(field)), None)
    if altered is None:
        record(plan, kind, *fields)
        return True
    record(
        plan,
        "PRESERVED",
        fields[0],
        f"not offered: the name {one_line(altered)} holds a character a fact line cannot carry, so no confirmation "
        "could name it",
    )
    return False


def events(plan: dict[str, Any], kind: str) -> list[list[str]]:
    return [event[1:] for event in plan["events"] if event[0] == kind]


def deleted(plan: dict[str, Any]) -> set[str]:
    return {fields[0] for fields in events(plan, "DELETED")}


def closed_branches(plan: dict[str, Any]) -> set[str]:
    """Branches whose worktree is removed but which are kept and asked about, since their pull request closed."""
    return {branch["name"] for branch in plan["branches"] if branch["action"] == "remove-worktree-ask"}


# apply and confirmations ----------------------------------------------------------------------------------------


def in_default(services: Services, plan: dict[str, Any], sha: str) -> bool:
    """Whether the local or the fetched default branch contains `sha`."""
    return reachable(services, plan["repo_root"], sha, *default_refs(plan["default"]["name"]))


def delete_merged(services: Services, plan: dict[str, Any], name: str, sha: str, pr: str) -> bool:
    """Delete an unchanged branch the default branch contains, and hand any other to unmerged.

    git branch -d judges a branch against its upstream, or against HEAD when that is gone or unset, and HEAD is not
    the default branch after --skip-checkout or a failed switch. So the default branch decides, and -D deletes a
    branch -d refused only because HEAD lacks it. False when Git refused the deletion.
    """
    if not in_default(services, plan, sha):
        return unmerged(services, plan, name, sha, pr)
    result = git(services, plan["repo_root"], "branch", "-d", name)
    if result.returncode == 0:
        record(plan, "DELETED", name)
    elif branch_tip(services, plan["repo_root"], name) == sha:
        return force_delete(services, plan, name)
    else:
        record(plan, "PRESERVED", name, "moved")
    return True


def force_delete(services: Services, plan: dict[str, Any], name: str) -> bool:
    """git branch -D; False when Git refused it."""
    result = git(services, plan["repo_root"], "branch", "-D", name)
    if result.returncode == 0:
        record(plan, "DELETED", name)
        return True
    record(plan, "PRESERVED", name, reason(result))
    return False


def unmerged(services: Services, plan: dict[str, Any], name: str, sha: str, pr: str) -> bool:
    """Force-delete an unchanged branch Git calls unmerged only when its pull request status is MERGED.

    That is a squash or rebase merge, at this exact tip or of this tip's whole change, into the default branch or
    another base: GitHub proved the work landed although no commit of the branch is on the default branch. Any other
    unmerged branch waits for the user's confirmation. False when Git refused a deletion.
    """
    if pr != "MERGED":
        offer_line(plan, "UNMERGED", name, sha)
        return True
    return force_delete(services, plan, name)


def unchanged(
    services: Services, plan: dict[str, Any], name: str, sha: str, located: dict[str, Worktree], worktree: str | None
) -> bool:
    """True when the branch is still at the recorded tip and checked out where the plan saw it."""
    where = located.get(name)
    if branch_tip(services, plan["repo_root"], name) != sha:
        record(plan, "PRESERVED", name, "moved")
        return False
    if (where is None) != (worktree is None) or (
        where is not None and worktree is not None and not same_path(where.path, worktree)
    ):
        record(plan, "PRESERVED", name, "moved" if worktree is not None else "checked out")
        return False
    return True


def strictly_inside(path: str, boundary: str, *, lexical: bool = False) -> bool:
    """Whether `path` lies below `boundary`, both resolved through links, or with `lexical` as written."""
    normalize = os.path.abspath if lexical else os.path.realpath
    path, boundary = os.path.normcase(normalize(path)), os.path.normcase(normalize(boundary))
    try:
        return path != boundary and os.path.commonpath([path, boundary]) == boundary
    except ValueError:
        return False


def prune_empty_parents(removed: str, boundaries: list[str]) -> list[str]:
    """Remove the removed worktree's now-empty directories, walking up but never reaching a boundary.

    Only Path.rmdir is used, so a directory with any content stops the walk.
    """
    boundary = next((candidate for candidate in boundaries if strictly_inside(removed, candidate)), None)
    pruned: list[str] = []
    # Lexical, like strictly_inside: Path.absolute keeps ".." and Path.resolve follows junctions past the boundary.
    current = Path(os.path.abspath(removed))  # noqa: PTH100 - lexical, as the comment says
    while boundary is not None and strictly_inside(str(current), boundary):
        if os.path.lexists(current):
            if not is_plain_directory(current):
                break
            try:
                current.rmdir()
            except OSError:
                break
            pruned.append(current.as_posix())
        current = current.parent
    return pruned


def ignored_entries(services: Services, directory: str | Path) -> list[str]:
    """The ignored paths under the worktree, as `git status --porcelain -z --ignored=matching` lists them, a directory
    ignored as a whole as one entry ending in a slash.

    The matching mode lists each ignored path itself; the default mode folds a directory whose every file is ignored
    into one entry for the directory, which would hide a cache under an untracked package.
    """
    listed = read_worktree(services, directory, "status", git_worktrees.changes, "--ignored=matching")
    return [change.path for change in listed if change.code == "!!"]


def regenerable_holders(path: str, worktree: str, entries: tuple[Regenerable, ...]) -> list[tuple[Regenerable, str]]:
    """Each of the regenerable `entries` the ignored path is, or lies inside, with the directory it matched, from the
    worktree, in forward slashes.

    The directory counts only when it and every directory from the worktree down to it is a plain directory, so
    nothing outside the worktree is reached through it, and, when its entry names projects, one of them sits beside
    it to rebuild it from.
    """
    parts = [part for part in path.replace(chr(92), "/").split("/") if part]
    holders: list[tuple[Regenerable, str]] = []
    for start in range(len(parts)):
        for entry in entries:
            components = list(entry.directory)
            if parts[start : start + len(components)] != components:
                continue
            holder = Path(worktree, *parts[:start])
            if not linkless_below(str(holder.joinpath(*components)), worktree):
                continue
            if not entry.projects or any(
                found.is_file() for pattern in entry.projects for found in holder.glob(pattern)
            ):
                holders.append((entry, "/".join(parts[: start + len(components)])))
    return holders


def is_regenerable(path: str, worktree: str, entries: tuple[Regenerable, ...]) -> bool:
    """Whether the ignored path is one of the regenerable `entries`, or lies inside one."""
    return bool(regenerable_holders(path, worktree, entries))


def tracked_below(services: Services, worktree: str, directory: str) -> bool:
    """Whether Git tracks a file at or below `directory` of the worktree, named literally, never as a pattern."""
    return bool(git_output(services, worktree, "ls-files", "-z", "--", f":(literal){directory}"))


def named(paths: list[str]) -> str:
    """The first paths, comma-separated, then how many more there are."""
    rest = len(paths) - NAMED_PATHS
    return ", ".join(paths[:NAMED_PATHS]) + (f", and {rest} more" if rest > 0 else "")


def counted(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def regenerable_under(
    services: Services, path: str, entries: list[str], policy: Policy
) -> tuple[set[str], dict[str, str]]:
    """The ignored `entries` of the worktree at `path` that are regenerable under `policy`, and each added entry
    refused here, as written, with the directory it matched that holds tracked files.

    A shipped entry is used as it is. An added one is used only where the directory it matched holds no tracked file:
    a directory Git tracks content in holds the user's work beside whatever it ignores.
    """
    regenerable: set[str] = set()
    refused: dict[str, str] = {}
    tracked: dict[str, bool] = {}
    for ignored in entries:
        holders = regenerable_holders(ignored, path, policy.regenerable)
        if any(not entry.added for entry, _ in holders):
            regenerable.add(ignored)
            continue
        for entry, directory in holders:
            if directory not in tracked:
                tracked[directory] = tracked_below(services, path, directory)
            if not tracked[directory]:
                regenerable.add(ignored)
                break
            refused.setdefault(entry.written(), directory)
    return regenerable, refused


def ignored_paths(services: Services, path: str, policy: Policy) -> tuple[str, str]:
    """Why a worktree's ignored files keep it, and what is deleted with it.

    git worktree remove counts a worktree holding only ignored files, such as a .env, as clean and deletes them, so
    a worktree holding any ignored path that is not regenerable under `policy` is kept, and the reason names the
    paths, then each added entry refused because the directory it matched holds tracked files. The second string
    names the regenerable paths, present with the reason or, when they are all the worktree holds, alone with an
    empty reason, so the removal can say what it deleted. Both are empty for a worktree with no ignored path.
    """
    try:
        entries = ignored_entries(services, path)
        regenerable, refused = regenerable_under(services, path, entries, policy)
    except CleanupError as exc:
        return str(exc), ""
    kept = [entry for entry in entries if entry not in regenerable]
    rebuilt = [entry for entry in entries if entry in regenerable]
    disposed = f"deleted {counted(len(rebuilt), 'regenerable path')}: {named(rebuilt)}" if rebuilt else ""
    if not kept:
        return "", disposed
    reason = (
        f"worktree {path} holds {counted(len(kept), 'ignored path')} git worktree remove would delete: {named(kept)}"
    )
    for written, directory in refused.items():
        reason += f"; refused added regenerable entry {written}: {directory} holds tracked files"
    if rebuilt:
        reason += f"; also {counted(len(rebuilt), 'regenerable path')}: {named(rebuilt)}"
    return reason, disposed


def operations(services: Services, path: str) -> list[str]:
    """The operations under way in the worktree at `path`, as the markers in its own Git directory show them."""
    arguments = [argument for marker, _ in OPERATION_MARKERS for argument in ("--git-path", marker)]
    listing = git_output(services, path, "rev-parse", "--path-format=absolute", *arguments)
    located = [line.rstrip("\r") for line in listing.split("\n") if line.strip()]
    if len(located) != len(OPERATION_MARKERS):
        raise CleanupError(f"git rev-parse did not name each operation marker of worktree {path}")
    found = [label for (_, label), marker in zip(OPERATION_MARKERS, located, strict=True) if os.path.lexists(marker)]
    return list(dict.fromkeys(found))


def hidden_edits(services: Services, path: str) -> list[str]:
    """The tracked files status leaves out because their skip-worktree or assume-unchanged bit is set and whose file
    in the worktree differs from the index: edits removing the worktree would lose.

    A marked file that is absent, as a sparse checkout leaves each path outside it, holds nothing to lose. One that is
    not a regular file, such as a link, counts as edited, since its content is not compared.
    """
    compared: list[tuple[str, str]] = []
    edited: list[str] = []
    for entry in git_output(services, path, "ls-files", "-v", "-s", "-z").split("\0"):
        tag, _, rest = entry.partition(" ")
        if tag != SKIP_WORKTREE_TAG and not tag.islower():
            continue
        fields, _, name = rest.partition("\t")
        mode, sha, *_ = fields.split(" ")
        target = Path(path, name)
        if not os.path.lexists(target):
            continue
        if mode in REGULAR_MODES and is_plain_file(target):
            compared.append((name, sha))
        else:
            edited.append(name)
    for start in range(0, len(compared), HASHED_PER_CALL):
        batch = compared[start : start + HASHED_PER_CALL]
        hashes = git_output(services, path, "hash-object", "--", *(name for name, _ in batch)).split()
        if len(hashes) != len(batch):
            raise CleanupError(f"git hash-object did not hash each marked file of worktree {path}")
        edited += [name for (name, indexed), hashed in zip(batch, hashes, strict=True) if hashed != indexed]
    return sorted(edited)


def held_state(services: Services, path: str) -> str:
    """What the worktree at `path` holds that its removal would lose although status shows no change, or an empty
    string: an operation under way, refs only it sees, and edits to files whose bits hide them from status.

    git worktree remove deletes the worktree's own Git directory, and with it each of these, without asking. When Git
    cannot say, the reason is why, so the worktree is kept.
    """
    try:
        under_way = operations(services, path)
        listing = git_output(services, path, "for-each-ref", "--format=%(refname)", *WORKTREE_REFS)
        edits = hidden_edits(services, path)
    except CleanupError as exc:
        return f"cannot be read for what its removal would lose: {exc}"
    refs = [ref for ref in listing.split("\n") if ref.strip()]
    held = [f"has {' and '.join(under_way)} in progress"] if under_way else []
    if refs:
        held.append(f"holds {counted(len(refs), 'ref')} only it sees: {named(refs)}")
    if edits:
        held.append(
            f"holds edits status does not show, to {counted(len(edits), 'file')} marked skip-worktree or "
            f"assume-unchanged: {named(edits)}"
        )
    return "; ".join(held)


def registered(services: Services, plan: dict[str, Any], path: str) -> bool:
    """Whether Git still lists a worktree at `path`; True when it cannot say."""
    try:
        return any(same_path(tree.path, path) for tree in list_worktrees(services, plan["repo_root"]))
    except CleanupError:
        return True


def remove_checkout(services: Services, plan: dict[str, Any], path: str, name: str, label: str) -> str:
    """git worktree remove, which refuses a locked worktree or one with changes: "removed", "kept" when what it holds
    keeps it, or "refused" when Git refused or did not finish.

    A worktree is kept, asked just before the removal, when it holds what held_state names, which Git would discard
    without asking, or ignored files, which Git would delete with it, unless every one is regenerable, which the
    REMOVED fact names after `label`. A PRESERVED fact names `name` with the reason it stayed. Git deletes the
    worktree's record even when it cannot delete the folder, as when a process holds it on Windows; the reason then
    says so, and the folder is left to the ORPHAN offer.
    """
    held = held_state(services, path)
    if held:
        record(plan, "PRESERVED", name, f"worktree {path} {held}")
        return "kept"
    kept, disposed = ignored_paths(services, path, policy_of(plan))
    if kept:
        record(plan, "PRESERVED", name, kept)
        return "kept"
    removed = git(services, plan["repo_root"], *SHOW_UNTRACKED, "worktree", "remove", path)
    if removed.returncode != 0:
        if registered(services, plan, path):
            record(plan, "PRESERVED", name, f"Git refused to remove worktree {path}: {reason(removed)}")
        else:
            record(
                plan,
                "PRESERVED",
                name,
                f"Git deleted worktree {path}'s record and files but not its folder: {reason(removed)}; a sweep "
                "offers what is left there as a directory Git no longer tracks",
            )
        return "refused"
    record(plan, "REMOVED", path, label, *([disposed] if disposed else []))
    return "removed"


@dataclass(frozen=True)
class Removal:
    """What remove_worktree did: whether the worktree is gone, and whether Git refused its removal or the deletion
    of its branch after it."""

    removed: bool
    refused: bool


def remove_worktree(
    services: Services, plan: dict[str, Any], entry: dict[str, Any], sha: str, pr: str, ask: bool = False
) -> Removal:
    """remove_checkout on the branch's worktree, then delete_merged on its branch.

    With `ask`, the branch is kept and offered for the user's confirmation instead.
    """
    outcome = remove_checkout(services, plan, entry["path"], entry["branch"], entry["branch"])
    if outcome != "removed":
        return Removal(removed=False, refused=outcome == "refused")
    if ask:
        offer_line(plan, "CONFIRM_LOCAL", entry["branch"])
        return Removal(removed=True, refused=False)
    return Removal(removed=True, refused=not delete_merged(services, plan, entry["branch"], sha, pr))


def apply(plan_path: str, services: Services) -> bool:
    """Apply the plan; False when Git refused a deletion or removal."""
    plan = plan_in_scope(plan_path)
    if plan["applied"]:
        raise CleanupError(f"this plan was already applied; {SWEEP_AGAIN}")
    plan["applied"] = True
    return run_and_report(plan_path, plan, lambda: apply_plan(plan, services))


def run_and_report(plan_path: str, plan: dict[str, Any], action: Callable[[], T]) -> T:
    """Run `action` on the plan, then save the plan and print its summary, also when the action failed partway.

    Every branch deleted and worktree removed before a failure is then in the summary, printed ahead of the failure.
    The stopped marker describes the last run on the plan, so a later run that completes clears it.
    """
    plan.pop("stopped", None)
    try:
        return action()
    except Exception as exc:
        plan["stopped"] = one_line(exc) or type(exc).__name__
        raise
    finally:
        try:
            save_plan(plan_path, plan)
        finally:
            print_summary(plan)


def apply_plan(plan: dict[str, Any], services: Services) -> bool:
    """The planned deletions, removals, and fast-forwards, then the offers; False when Git refused a deletion or
    removal."""
    root = plan["repo_root"]
    branches = {branch["name"]: branch for branch in plan["branches"]}
    located = checkouts(services, root)
    accepted = True
    for branch in plan["branches"]:
        if branch["action"] == "delete" and unchanged(services, plan, branch["name"], branch["sha"], located, None):
            accepted = delete_merged(services, plan, branch["name"], branch["sha"], branch["pr"]) and accepted
    removed: list[str] = []
    for entry in plan["worktrees"]:
        if entry["action"] != "remove":
            continue
        branch = branches[entry["branch"]]
        sha, ask = branch["sha"], branch["action"] == "remove-worktree-ask"
        if unchanged(services, plan, entry["branch"], sha, located, entry["path"]):
            outcome = remove_worktree(services, plan, entry, sha, branch["pr"], ask)
            accepted = not outcome.refused and accepted
            if outcome.removed:
                removed.append(entry["path"])
    for path in removed:
        prune_parents(plan, path)
    located = checkouts(services, root)
    for entry in plan["fastforward"]:
        if not unchanged(services, plan, entry["branch"], entry["sha"], located, entry["worktree"]):
            continue
        error = fast_forward(services, root, entry["branch"], entry["sha"], entry["target"], entry["worktree"])
        if error:
            record(plan, "PRESERVED", entry["branch"], f"fast-forward failed: {error}")
        else:
            record(plan, "FF", entry["branch"], entry["target"])
    for entry in plan["diverged"]:
        record(plan, "DIVERGED", entry["branch"], entry["ahead"], entry["behind"])
    offer(services, plan, located)
    return accepted


def offer(services: Services, plan: dict[str, Any], located: dict[str, Worktree]) -> None:
    """Offer what only the user may delete: local-only branches, with the clean worktree one is checked out in,
    detached worktrees, then the directories Git no longer tracks that are left once every removal ran. A directory
    a repository holding it tracks files in is kept instead, with the reason."""
    root = plan["repo_root"]
    for branch in plan["branches"]:
        if branch["action"] == "ask-delete":
            if branch_tip(services, root, branch["name"]) == branch["sha"]:
                offer_line(plan, "CONFIRM_LOCAL", branch["name"])
            else:
                record(plan, "PRESERVED", branch["name"], "moved")
        elif branch["action"] == "ask-remove-worktree" and unchanged(
            services, plan, branch["name"], branch["sha"], located, branch["worktree"]
        ):
            offer_line(plan, "CONFIRM_LOCAL", branch["name"], branch["worktree"])
    offer_detached(services, plan)
    registered = [tree.path for tree in list_worktrees(services, root)]
    for path in orphan_directories(registered, plan["orphan_areas"], area_anchors(plan)):
        tracked = tracked_reason(services, plan, path)
        if tracked:
            record(plan, "PRESERVED", path, tracked)
        else:
            offer_line(plan, "ORPHAN", path)


def prune_parents(plan: dict[str, Any], removed: str) -> None:
    """Remove the empty directories a removal left, never at or above the worktree areas or the repository root."""
    boundaries = [plan["worktree_area"], *plan["orphan_areas"], plan["repo_root"]]
    for directory in prune_empty_parents(removed, boundaries):
        record(plan, "PRUNED_DIR", directory)


def detached_at(services: Services, plan: dict[str, Any], path: str, sha: str) -> Worktree | None:
    """The linked worktree at `path` when it is still detached at `sha`, else None."""
    for tree in list_worktrees(services, plan["repo_root"]):
        if not tree.main and same_path(tree.path, path):
            return tree if tree.branch is None and tree.head == sha else None
    return None


def offer_detached(services: Services, plan: dict[str, Any]) -> None:
    """Offer each detached worktree the plan found clean and reachable, when it has not moved since."""
    for entry in plan["worktrees"]:
        if entry["branch"] is None and entry["action"] == "ask":
            if detached_at(services, plan, entry["path"], entry["sha"]):
                offer_line(plan, "CONFIRM_DETACHED", entry["path"], entry["sha"])
            else:
                record(plan, "PRESERVED", entry["path"], "moved")


def holds_checkout(directory: Path, registered: list[str]) -> bool:
    """Whether a registered worktree, or a `.git` entry of any repository or worktree, is inside `directory`.

    Links are never followed. A directory that cannot be read counts as holding one, so it is never reported.
    """
    if any(strictly_inside(path, str(directory)) for path in registered):
        return True
    pending = [directory]
    while pending:
        try:
            children = list(pending.pop().iterdir())
        except OSError:
            return True
        for child in children:
            if child.name == ".git":
                return True
            if is_plain_directory(child):
                pending.append(child)
    return False


def linkless_below(path: str, anchor: str) -> bool:
    """Whether `path` lies lexically below `anchor` and every component from the anchor down, `path` included, is a
    plain directory: no link or junction, so the path resolves where it reads."""
    # Lexical, like prune_empty_parents: Path.resolve would follow the very links this looks for.
    path, anchor = os.path.abspath(path), os.path.abspath(anchor)  # noqa: PTH100 - lexical, as the comment says
    try:
        parts = Path(path).relative_to(anchor).parts
    except ValueError:
        return False
    current = Path(anchor)
    for part in parts:
        current /= part
        if not is_plain_directory(current):
            return False
    return bool(parts)


def searchable_areas(areas: list[str], anchors: list[str]) -> list[str]:
    """The worktree areas the orphan search may enter: each with no link or junction below the innermost anchor, the
    repository root or the folder holding the worktree area, that holds it. Any other area is skipped."""
    found = []
    for area in areas:
        holding = [anchor for anchor in anchors if strictly_inside(area, anchor, lexical=True)]
        if holding and linkless_below(area, max(holding, key=len)):
            found.append(area)
    return found


def area_anchors(plan: dict[str, Any]) -> list[str]:
    """Where the worktree areas hang from: the repository root, and the folder holding the shared worktree area."""
    return [plan["repo_root"], str(Path(plan["worktree_area"]).parent)]


def orphan_directories(registered: list[str], areas: list[str], anchors: list[str]) -> list[str]:
    """The directories under the worktree areas that Git no longer tracks, the outermost of each.

    Such a directory is no registered worktree, has no `.git` entry, and holds no worktree or repository, as a
    worktree whose removal Git finished but Windows could not complete leaves behind. A directory that holds a
    worktree is searched instead, and a link is neither followed nor reported: one inside an area, and one on the
    way to it from `anchors`, which skips the area (searchable_areas).
    """
    found: list[str] = []

    def search(directory: Path) -> None:
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name.casefold())
        except OSError:
            return
        for child in children:
            if not is_plain_directory(child) or os.path.lexists(child / ".git"):
                continue
            if any(same_path(str(child), path) for path in registered):
                continue
            if holds_checkout(child, registered):
                search(child)
            else:
                found.append(child.as_posix())

    for area in searchable_areas(areas, anchors):
        search(Path(area))
    return found


def holding_repositories(path: str, anchors: list[str]) -> list[str]:
    """Every directory holding `path` that has a `.git` entry, from the outermost anchor holding it, itself included,
    down to its parent: the swept repository for its own area, and a repository such as one named like the shared
    worktree area."""
    # Lexical, like linkless_below, which the search already held for every component below the anchor.
    path = os.path.abspath(path)  # noqa: PTH100 - lexical, as the comment says
    holding = [anchor for anchor in anchors if strictly_inside(path, anchor, lexical=True)]
    if not holding:
        return []
    current = Path(os.path.abspath(min(holding, key=len)))  # noqa: PTH100 - lexical, as the comment says
    found = []
    for part in Path(path).relative_to(current).parts:
        if os.path.lexists(current / ".git"):
            found.append(current.as_posix())
        current /= part
    return found


def tracked_reason(services: Services, plan: dict[str, Any], path: str) -> str:
    """Why an orphan candidate stays: a repository holding it tracks files in it, as `git ls-files` lists them, or
    Git could not say; empty when no repository holding it tracks any file in it.

    The pathspec is literal and matches in any letter case, so a folder Git tracks under another case is kept too.
    """
    for holder in holding_repositories(path, area_anchors(plan)):
        relative = Path(os.path.relpath(path, holder)).as_posix()
        listed = git(services, holder, "ls-files", "-z", "--", f":(literal,icase){relative}")
        if listed.returncode != 0:
            return f"could not ask Git whether {holder} tracks files in it: {reason(listed)}"
        files = [entry for entry in listed.stdout.split("\0") if entry]
        if files:
            return f"Git tracks {counted(len(files), 'file')} in it, in {holder}: {named(files)}"
    return ""


def delete_tree(path: Path) -> None:
    """shutil.rmtree, which removes a link inside the tree without following it, clearing the read-only attribute
    Windows refuses to delete a file with."""

    def writable(function: Callable[..., Any], target: str, *_: Any) -> None:
        Path(target).chmod(stat.S_IWRITE)
        function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:
        shutil.rmtree(path, onerror=writable)


def remove_detached(services: Services, plan: dict[str, Any], path: str, sha: str) -> bool:
    """Remove an offered detached worktree that has not moved and whose commit is still on the default branch or a
    remote branch.

    True when it was removed, or stays because it moved or its commit is no longer on such a branch; False when Git
    refused or did not finish the removal, or what the worktree holds kept it.
    """
    if detached_at(services, plan, path, sha) is None:
        record(plan, "PRESERVED", path, "moved")
        return True
    default = plan["default"]["name"]
    if not reachable(services, plan["repo_root"], sha, default_refs(default)[0], "refs/remotes"):
        record(plan, "PRESERVED", path, f"its commit {sha[:12]} is no longer on {default} or a remote branch")
        return True
    if remove_checkout(services, plan, path, path, "detached") != "removed":
        return False
    prune_parents(plan, path)
    return True


def delete_orphan(services: Services, plan: dict[str, Any], path: str) -> bool:
    """Delete an offered orphan directory that is still one.

    True when it was deleted, or stays because it is gone or Git tracks it as a worktree or repository again; False
    when deleting it failed, it no longer resolves inside a worktree area, or a repository holding it tracks files in
    it. Just before the deletion, and before anything else is asked of it, a directory that still exists must be no
    link and, resolved through any link, lie inside an area the search may enter, resolved the same way, so the
    deletion never reaches a directory the search did not; then the search must still find it, and no repository
    holding it may track a file in it, as tracked_reason asks again.
    """
    areas = plan["orphan_areas"]
    if not os.path.lexists(path):
        record(plan, "PRESERVED", path, "no longer a directory Git does not track")
        return True
    searchable = searchable_areas(areas, area_anchors(plan))
    if not is_plain_directory(Path(path)) or not any(strictly_inside(path, area) for area in searchable):
        record(plan, "PRESERVED", path, "it resolves outside the worktree areas")
        return False
    registered = [tree.path for tree in list_worktrees(services, plan["repo_root"])]
    if not any(same_path(path, found) for found in orphan_directories(registered, areas, area_anchors(plan))):
        record(plan, "PRESERVED", path, "no longer a directory Git does not track")
        return True
    tracked = tracked_reason(services, plan, path)
    if tracked:
        record(plan, "PRESERVED", path, tracked)
        return False
    try:
        delete_tree(Path(path))
    except OSError as exc:
        record(plan, "PRESERVED", path, f"could not delete it: {one_line(exc)}")
        return False
    record(plan, "DELETED_DIR", path)
    prune_parents(plan, path)
    return True


def remove_directories(plan_path: str, paths: list[str], services: Services) -> bool:
    """Remove the confirmed detached worktrees and orphan directories the plan offered; False when any stayed."""
    plan = plan_in_scope(plan_path)
    if not plan["applied"]:
        raise CleanupError(f"this plan was not applied by a sweep; {SWEEP_AGAIN}")

    def remove() -> bool:
        done = [fields[0] for kind in ("REMOVED", "DELETED_DIR") for fields in events(plan, kind)]
        detached = {fields[0]: fields[1] for fields in events(plan, "CONFIRM_DETACHED")}
        orphans = [fields[0] for fields in events(plan, "ORPHAN")]
        succeeded = True
        for path in dict.fromkeys(paths):
            offered = next((known for known in [*detached, *orphans] if same_path(known, path)), None)
            if offered is None or any(same_path(offered, gone) for gone in done):
                record(plan, "PRESERVED", path, "not a detached worktree or orphan directory offered by this plan")
            elif offered in detached:
                succeeded = remove_detached(services, plan, offered, detached[offered]) and succeeded
            else:
                succeeded = delete_orphan(services, plan, offered) and succeeded
        return succeeded

    return run_and_report(plan_path, plan, remove)


def confirm(plan_path: str, names: list[str], services: Services, force: bool) -> bool:
    """Delete the confirmed branches the plan offered; False when Git refused any deletion."""
    plan = plan_in_scope(plan_path)
    if not plan["applied"]:
        raise CleanupError(f"this plan was not applied by a sweep; {SWEEP_AGAIN}")
    states = {branch["name"]: branch["pr"] for branch in plan["branches"]}
    worktrees: dict[str, str] = {}
    if force:
        eligible = {fields[0]: fields[1] for fields in events(plan, "UNMERGED")}
        refusal = "not reported UNMERGED by this plan"
    else:
        # A closed pull request's branch is offered only once its worktree is gone, and a local-only branch in a
        # clean worktree with the worktree's path, to be removed with it.
        offered = events(plan, "CONFIRM_LOCAL")
        closed = {fields[0] for fields in offered} & closed_branches(plan)
        worktrees = {fields[0]: fields[1] for fields in offered if len(fields) > 1}
        eligible = {
            branch["name"]: branch["sha"]
            for branch in plan["branches"]
            if branch["action"] == "ask-delete"
            or branch["name"] in closed
            or (branch["action"] == "ask-remove-worktree" and branch["name"] in worktrees)
        }
        refusal = "not a local-only or closed branch offered by this plan"

    def delete() -> bool:
        located = checkouts(services, plan["repo_root"])
        succeeded = True
        for name in dict.fromkeys(names):
            if name not in eligible or name in deleted(plan):
                record(plan, "PRESERVED", name, refusal)
            elif unchanged(services, plan, name, eligible[name], located, worktrees.get(name)):
                if force:
                    succeeded = force_delete(services, plan, name) and succeeded
                elif name in worktrees:
                    entry = {"path": worktrees[name], "branch": name}
                    outcome = remove_worktree(services, plan, entry, eligible[name], states[name])
                    if outcome.removed:
                        prune_parents(plan, worktrees[name])
                    succeeded = outcome.removed and not outcome.refused and succeeded
                else:
                    succeeded = delete_merged(services, plan, name, eligible[name], states[name]) and succeeded
        return succeeded

    return run_and_report(plan_path, plan, delete)


# summary --------------------------------------------------------------------------------------------------------


def summary_row(label: str, items: list[str], suffix: str = "") -> str:
    value = f"{len(items)} — {', '.join(items)}{suffix}" if items else "0"
    return f"  {(label + ':').ljust(SUMMARY_WIDTH - 1)} {value}"


def summary_items(plan: dict[str, Any]) -> dict[str, list[str]]:
    """Every list the summary reports, by label; all empty means nothing happened and nothing was kept."""
    removed_branches = deleted(plan)
    unmerged = [fields[0] for fields in events(plan, "UNMERGED") if fields[0] not in removed_branches]
    closed = closed_branches(plan)
    offered = [
        fields
        for fields in events(plan, "CONFIRM_LOCAL")
        if fields[0] not in removed_branches and fields[0] not in unmerged
    ]
    removed_paths = [fields[0] for kind in ("REMOVED", "DELETED_DIR") for fields in events(plan, kind)]
    detached = [
        f"{entry['path']} ({entry['detail']})"
        for entry in plan["worktrees"]
        if entry["branch"] is None
        and entry["action"] in ("keep", "ask")
        and not any(same_path(entry["path"], path) for path in removed_paths)
    ]
    orphans = [
        fields[0] for fields in events(plan, "ORPHAN") if not any(same_path(fields[0], path) for path in removed_paths)
    ]
    dirty = [
        f"{entry['path']} ({entry['branch']}, {entry['action'].split(':')[1]} changed)"
        if entry["action"].startswith("dirty:")
        else f"{entry['path']} ({entry['branch']}, {entry['detail']})"
        for entry in plan["worktrees"]
        if entry["action"].startswith("dirty:") or entry["action"] == "held"
    ]
    unknown = [
        f"{branch['name']} ({branch['detail'].removeprefix('pr-unknown: ')})"
        for branch in plan["branches"]
        if branch["pr"] == "UNKNOWN"
    ]
    unmatched = [branch["name"] for branch in plan["branches"] if branch["pr"] == "UNMATCHED"]
    synced = plan.get("sync") or {}  # absent from a plan built outside a sweep
    skipped = [
        f"{entry['branch']} (behind {entry['behind']}; {entry['reason']})"
        for entry in plan.get("fastforward_skipped", [])  # absent from a plan an earlier version wrote
    ]
    if synced.get("fast_forward", [""])[0] == "failed":
        skipped.insert(0, f"{plan['default']['name']} (behind {synced['behind']}; {synced['fast_forward'][1]})")
    return {
        "Branches deleted": [fields[0] for fields in events(plan, "DELETED")],
        "Worktrees removed": [f"{path} ({'; '.join(rest)})" for path, *rest in events(plan, "REMOVED")],
        "Branches fast-forwarded": [fields[0] for fields in events(plan, "FF")],
        "Fast-forward skipped": skipped,
        "Diverged (manual)": [
            f"{name} (ahead {ahead}, behind {behind})" for name, ahead, behind in events(plan, "DIVERGED")
        ],
        "Dirty worktrees skipped": dirty,
        "PR status unverified": unknown,
        "PR history unmatched": unmatched,
        "Unmerged (kept)": unmerged,
        "Local-only (kept)": [
            f"{name} (in worktree {where[0]})" if where else name for name, *where in offered if name not in closed
        ],
        "Closed PR (kept)": [fields[0] for fields in offered if fields[0] in closed],
        "Detached (kept)": detached,
        "Orphans (kept)": orphans,
        "Orphans deleted": [fields[0] for fields in events(plan, "DELETED_DIR")],
        "Preserved": [f"{name}: {why}" for name, why in events(plan, "PRESERVED")],
        "Open PR (kept)": [branch["name"] for branch in plan["branches"] if branch["pr"] == "OPEN"],
        "Empty directories removed": [fields[0] for fields in events(plan, "PRUNED_DIR")],
    }


def summary_lines(plan: dict[str, Any]) -> list[str]:
    items = summary_items(plan)
    protected = [entry for entry in plan["worktrees"] if entry["action"] == "protected"]
    stopped = plan.get("stopped")
    heading = f"cleanup stopped partway ({stopped}); done before it" if stopped else "cleanup complete"
    lines = [
        f"{plan['repo_name']} {heading}:",
        f"  {'Default branch:'.ljust(SUMMARY_WIDTH - 1)} {plan['default']['name']} ({default_notes(plan)})",
        summary_row("Branches deleted", items["Branches deleted"]),
        summary_row("Worktrees removed", items["Worktrees removed"]),
        summary_row("Branches fast-forwarded", items["Branches fast-forwarded"]),
        summary_row("Diverged (manual)", items["Diverged (manual)"]),
        summary_row("Dirty worktrees skipped", items["Dirty worktrees skipped"]),
        summary_row("PR status unverified", items["PR status unverified"], " (kept)"),
        summary_row("PR history unmatched", items["PR history unmatched"], " (kept)"),
        f"  {'Protected worktrees:'.ljust(SUMMARY_WIDTH - 1)} {len(protected)} (preserved)",
    ]
    lines.extend(
        summary_row(label, items[label])
        for label in (
            "Fast-forward skipped",
            "Unmerged (kept)",
            "Local-only (kept)",
            "Closed PR (kept)",
            "Detached (kept)",
            "Orphans (kept)",
            "Orphans deleted",
            "Preserved",
            "Open PR (kept)",
            "Empty directories removed",
        )
        if items[label]
    )
    return lines


def default_notes(plan: dict[str, Any]) -> str:
    """The default branch's status, then what the sweep's sync did to it, then how it was found when origin/HEAD
    named a branch origin lacks."""
    synced = plan.get("sync") or {}
    notes = [plan["default"]["status"]]
    if synced.get("fast_forward", [""])[0] == "forwarded":
        notes.append("fast-forwarded")
    if synced.get("checkout") == "switched":
        notes.append("switched to it")
    elif synced.get("checkout") == "failed":
        notes.append("switch to it failed")
    found = (synced.get("default") or {}).get("note")  # absent from a plan an earlier version wrote
    if found:
        notes.append(found)
    return "; ".join(notes)


def is_quiet(plan: dict[str, Any]) -> bool:
    """Nothing changed and nothing needs the user: its summary would only say the default branch is up to date."""
    return default_notes(plan) == "up to date" and not plan.get("stopped") and not any(summary_items(plan).values())


def print_summary(plan: dict[str, Any]) -> None:
    for line in summary_lines(plan):
        emit("SUMMARY", line)


# sweep ----------------------------------------------------------------------------------------------------------


def plans_location(explicit: str | None) -> Path | None:
    """The explicit directory for a sweep's plan files, or None for a new temporary directory.

    A file left inside a skill directory makes the deployer see that skill as modified and stop updating it, so an
    explicit directory inside any skills directory is refused before anything runs.
    """
    if explicit is None:
        return None
    root = skills_directory_holding(Path(explicit))
    if root is not None:
        raise ValueError(
            f"{explicit} is inside the skills directory {root}; omit --plans to keep the plans in a new temporary "
            "directory"
        )
    return Path(explicit)


def recorded_location(directory: str) -> Path:
    """The directory an earlier sweep printed and recorded its repositories in, for a sweep that adds to its record.

    Only a directory holding a sweep's record is accepted, so the agent can name no other place for a sweep to write,
    and never one inside a skills directory, which plans_location refuses for --plans: a record copied there would
    otherwise make it one.
    """
    located = Path(directory)
    root = skills_directory_holding(located)
    if root is not None:
        raise CleanupError(
            f"{directory} is inside the skills directory {root}; name the directory a sweep printed as PLANS"
        )
    read_record(located)
    return located


def applied_plan(path: Path | None) -> str | None:
    """The plan file of a repository whose apply started, so its summary and confirmations stay usable."""
    if path is None:
        return None
    try:
        return path.as_posix() if load_plan(path)["applied"] else None
    except CleanupError:
        return None


def sweep_repository(
    root: str,
    repos_root: str,
    plans: str,
    services: Services,
    skip_checkout: bool = False,
    policy: Policy | None = None,
) -> dict[str, Any]:
    """sync, plan, and apply one repository, capturing its lines. Runs on a sweep worker thread."""
    lines: list[str] = []
    _capture.lines = lines
    plan_path: Path | None = None
    refused = False
    try:
        synced = sync(root, skip_checkout, services)
        if synced is None:
            dirty = any(line.split("\t")[0] == "DIRTY_MAIN" for line in lines)
            state = "dirty" if dirty else "fetch-failed"
        else:
            plan = build_plan(root, repos_root, services, DefaultBranch(**synced["default"]), policy)
            plan["sync"] = synced
            plan_path = Path(plans) / f"{Path(root).name}.json"
            save_plan(plan_path, plan)
            refused = not apply(str(plan_path), services)
            state = "quiet" if is_quiet(load_plan(plan_path)) else "cleaned"
    except (CleanupError, OSError) as exc:
        emit("ERROR", exc)
        state = "error"
    except GitUnavailable as exc:
        emit("ERROR", exc)
        state = "git-failed"
    except Exception as exc:  # noqa: BLE001 - a defect in one repository's run must not discard every other repository's report
        emit("ERROR", f"unexpected {type(exc).__name__}: {exc}")
        state = "error"
    finally:
        _capture.lines = None
    return {"root": root, "lines": lines, "state": state, "plan": applied_plan(plan_path), "refused": refused}


def passed_on(line: str, result: dict[str, Any]) -> bool:
    """Whether the sweep prints a repository's line: what the agent acts on or reports, without the step chatter.

    What sync did to the default branch is in the plan's summary, so its lines are printed only when no plan summary
    follows; a failed checkout is always printed.
    """
    kind, *fields = line.split("\t")
    if kind == "SUMMARY":
        state: str = result["state"]
        return state != "quiet"
    if kind == "CHECKOUT" and fields[0] == "failed":
        return True
    if kind in SYNC_KINDS:
        return result["plan"] is None and fields[0] in SYNC_KINDS[kind]
    return kind in SWEEP_KINDS


def policy_lines(policy: Policy) -> list[str]:
    """The lines saying which lists the sweep used: POLICY, with `defaults` or `defaults+additions` and the shipped
    file, then ADDED for each addition used and REFUSED_ENTRY for each refused one, with its reason."""
    regenerable, protected = policy.added_regenerable(), list(policy.added_protected)
    lists = "defaults+additions" if regenerable or protected else "defaults"
    lines = [f"POLICY\t{lists}\t{cleanup_policy.POLICY_FILE.as_posix()}"]
    lines += [f"ADDED\tregenerable\t{one_line(entry)}" for entry in regenerable]
    lines += [f"ADDED\tprotected\t{one_line(prefix)}" for prefix in protected]
    lines += [f"REFUSED_ENTRY\tregenerable\t{one_line(entry.entry)}\t{entry.reason}" for entry in policy.refused]
    return lines


def sweep(
    target: str | None,
    repos_root: str,
    plans: Path | None,
    services: Services,
    skip_checkout: bool = False,
    policy: Policy | None = None,
) -> int:
    """Clean every repository at once; print each one's decisions, failures, and summary in discovery order.

    The GitHub CLI's access is checked before the plans directory is created, so a sweep that cannot start leaves
    nothing behind. Each repository is synced, planned, and applied in turn, re-checking every recorded branch tip.
    A failure in one repository never stops the others: every result is reported, and the sweep exits 1 when any
    repository needs the agent or Git refused a deletion or removal in one. skip_checkout cleans a dirty main worktree
    without switching its branch. `policy`, the shipped lists alone when None, decides what a worktree's removal may
    delete and which branches are protected; the lines naming it follow PLANS.
    """
    policy = policy or cleanup_policy.configured()
    repositories = discover(target, repos_root, services)
    directory = plans or Path(tempfile.mkdtemp(prefix="repo-cleanup-plans-"))
    entries = read_record(directory)[0] if (directory / SWEEP_RECORD).exists() else []
    emit("PLANS", directory.as_posix())
    used = policy_lines(policy)
    for line in used:
        print(line)
    with ThreadPoolExecutor(max_workers=max(1, min(SWEEP_WORKERS, len(repositories)))) as pool:
        results = list(
            pool.map(
                lambda root: sweep_repository(root, repos_root, str(directory), services, skip_checkout, policy),
                repositories,
            )
        )
    states: dict[str, int] = {}
    for result in results:
        states[result["state"]] = states.get(result["state"], 0) + 1
        emit("REPO", result["root"], result["state"])
        if result["plan"]:
            emit("PLAN", result["plan"])
        for line in result["lines"]:
            if passed_on(line, result):
                print(line)
    emit(
        "SWEPT",
        len(results),
        *(f"{state}={states.get(state, 0)}" for state in SWEEP_STATES),
    )
    write_record(directory, entries, results, used)
    return 0 if set(states) <= {"cleaned", "quiet"} and not any(result["refused"] for result in results) else 1


# report ---------------------------------------------------------------------------------------------------------


def recorded_lines(result: dict[str, Any]) -> list[str]:
    """The lines the report repeats for a repository no plan holds the summary of."""
    if result["plan"]:
        return []
    return [line for line in result["lines"] if line.split("\t")[0] in RECORDED_KINDS and passed_on(line, result)]


def write_record(
    directory: Path, entries: list[dict[str, Any]], results: list[dict[str, Any]], policy: list[str]
) -> None:
    """Record each swept repository's state and plan in the plans directory, replacing what an earlier sweep there
    recorded for it, as sweeping a dirty repository again does, and keeping every other repository in its place, with
    the lines naming the lists the latest sweep used."""
    for result in results:
        entry = {
            "root": result["root"],
            "state": result["state"],
            "plan": result["plan"],
            "lines": recorded_lines(result),
        }
        index = next((i for i, known in enumerate(entries) if same_path(known["root"], entry["root"])), None)
        if index is None:
            entries.append(entry)
        else:
            entries[index] = entry
    save_plan(
        directory / SWEEP_RECORD,
        {"schema_version": RECORD_SCHEMA_VERSION, "repositories": entries, "policy": policy},
    )


def is_entry(entry: Any) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("root"), str)
        and entry.get("state") in SWEEP_STATES
        and (entry.get("plan") is None or isinstance(entry.get("plan"), str))
        and isinstance(entry.get("lines"), list)
        and all(isinstance(line, str) for line in entry["lines"])
    )


def read_record(directory: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """The recorded repositories, and the lines naming the lists the latest sweep used, from the record as a sweep
    last saved it."""
    path = directory / SWEEP_RECORD
    if not path.is_file():
        raise CleanupError(f"{directory} holds no sweep record; name the directory a sweep printed as PLANS")
    data = read_sealed(path, "sweep record")
    try:
        record = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise CleanupError(f"cannot read the sweep record {path}: {exc}") from exc
    if (
        not isinstance(record, dict)
        or type(record.get("schema_version")) is not int
        or record["schema_version"] != RECORD_SCHEMA_VERSION
        or not isinstance(record.get("repositories"), list)
        or not all(is_entry(entry) for entry in record["repositories"])
        or not isinstance(record.get("policy", []), list)
        or not all(isinstance(line, str) for line in record.get("policy", []))
    ):
        raise CleanupError(f"{path} is not a repo-cleanup sweep record")
    repositories: list[dict[str, Any]] = record["repositories"]
    return repositories, record.get("policy", [])


def aborted_entries(entries: list[dict[str, Any]], aborted: list[str]) -> list[dict[str, Any]]:
    """The recorded repositories the user aborted; refused unless each named one was left dirty and every other
    dirty one was swept again."""
    for name in aborted:
        if not any(same_path(entry["root"], name) and entry["state"] == "dirty" for entry in entries):
            raise CleanupError(
                f"{name} was not left dirty by a sweep of this directory; name only a repository you aborted"
            )
    dropped = [entry for entry in entries if any(same_path(entry["root"], name) for name in aborted)]
    waiting = [entry["root"] for entry in entries if entry["state"] == "dirty" and entry not in dropped]
    if waiting:
        raise CleanupError(
            f"still dirty: {', '.join(waiting)}; sweep each again with --skip-checkout and the same --record, or name "
            "it with --aborted"
        )
    return dropped


def report(directory: Path, aborted: list[str]) -> int:
    """Print each repository's current summary, then the closing count, from what the sweeps there recorded.

    A plan's summary is its latest: every command saves it, and one that completes clears an earlier stop, which then
    no longer counts as an error. Quiet repositories share one line. The lines naming the lists the sweep used come
    first, once the record and the plans are read, so a report that fails prints only its FAILED line.
    """
    entries, policy = read_record(directory)
    dropped = aborted_entries(entries, aborted)
    plans = {entry["root"]: load_plan(entry["plan"]) for entry in entries if entry["plan"]}
    for line in policy:
        print(line)
    quiet: list[str] = []
    failed = 0
    for entry in entries:
        plan = plans.get(entry["root"])
        if entry["state"] == "quiet":
            quiet.append(entry["root"])
            continue
        if entry["state"] in FAILED_STATES and (plan is None or "stopped" in plan):
            failed += 1
        emit("REPO", entry["root"], "aborted" if entry in dropped else entry["state"])
        if plan is not None:
            print_summary(plan)
        for line in entry["lines"]:
            print(line)
    if quiet:
        emit("QUIET", *quiet)
    fetch_failed = sum(entry["state"] == "fetch-failed" for entry in entries)
    noun = "repository" if len(entries) == 1 else "repositories"
    emit(
        "PROCESSED",
        f"Processed {len(entries)} {noun}; skipped: {fetch_failed} fetch failed, {len(dropped)} aborted, {failed} "
        "with an error.",
    )
    return 0


# CLI ------------------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    sweep_parser = commands.add_parser("sweep")
    sweep_parser.add_argument("--repos-root", required=True)
    location = sweep_parser.add_mutually_exclusive_group()
    location.add_argument(
        "--plans", help="directory for each repository's plan file; defaults to a new temporary directory"
    )
    location.add_argument(
        "--record", help="the directory an earlier sweep printed as PLANS, to keep this sweep's plans and record there"
    )
    sweep_parser.add_argument(
        "--skip-checkout", action="store_true", help="clean a dirty main worktree without switching branches"
    )
    sweep_parser.add_argument(
        "--regenerable",
        default="",
        help="regenerable directories to add to the shipped list, separated by spaces, each <directory> or "
        "<directory>:<project file>,<project file>",
    )
    sweep_parser.add_argument(
        "--protected", default="", help="branch prefixes to protect beside the shipped ones, separated by spaces"
    )
    sweep_parser.add_argument("target", nargs="?")
    commands.add_parser("summary").add_argument("--plan", required=True)
    report_parser = commands.add_parser("report")
    report_parser.add_argument("--record", required=True, help="the directory a sweep printed as PLANS")
    report_parser.add_argument(
        "--aborted", action="append", default=[], help="a dirty repository the user aborted, by the path REPO gave"
    )
    for name in ("delete-local", "force-delete"):
        confirm_parser = commands.add_parser(name)
        confirm_parser.add_argument("--plan", required=True)
        confirm_parser.add_argument("--branch", required=True, action="append", dest="branches")
    remove_parser = commands.add_parser("remove-directory")
    remove_parser.add_argument("--plan", required=True)
    remove_parser.add_argument("--path", required=True, action="append", dest="paths")
    return parser


def main(arguments: list[str] | None = None, services: Services | None = None) -> int:
    options = build_parser().parse_args(arguments)
    services = services or Services()
    try:
        if options.command == "sweep":
            policy = cleanup_policy.configured(options.regenerable, options.protected)
            plans = recorded_location(options.record) if options.record else plans_location(options.plans)
            return sweep(options.target, options.repos_root, plans, services, options.skip_checkout, policy)
        if options.command == "report":
            return report(Path(options.record), options.aborted)
        if options.command == "summary":
            print_summary(load_plan(options.plan))
        elif options.command == "remove-directory":
            if not remove_directories(options.plan, options.paths, services):
                return 1
        elif not confirm(options.plan, options.branches, services, force=options.command == "force-delete"):
            return 1
    except (CleanupError, GitUnavailable, OSError, ValueError) as exc:
        emit("FAILED", exc)
        return 1
    return 0


if __name__ == "__main__":
    run_main(main, newline="\n")

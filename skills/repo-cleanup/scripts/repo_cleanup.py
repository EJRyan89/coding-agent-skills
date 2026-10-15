"""Deterministic repository cleanup steps, so the agent only relays results and asks the user's confirmations.

    sweep         find the repositories to clean, then sync, plan, and apply every one at once, printing only what
                  needs the agent: decisions, failures, and summaries; plans go to a new temporary directory by
                  default
    delete-local  delete local-only branches the user chose to delete (git branch -d), with the clean worktree a
                  branch offered with its path is checked out in
    force-delete  force-delete branches reported UNMERGED, after the user confirmed
    remove-directory
                  remove detached worktrees reported CONFIRM_DETACHED and delete directories reported ORPHAN, after
                  the user confirmed
    summary       print the repository's summary from the plan file

Every command prints one tab-separated fact per line. Exit status 0 means every repository was cleaned, or every
confirmed deletion ran. 1 means something needs the agent: a repository that stopped (DIRTY_MAIN, FETCH_FAILED) or
was skipped with an ERROR line, a deletion Git refused, or a last line FAILED <reason> when the command could not run
at all. 2 is a usage error.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import git_client
import pr_status
from console import run_main
from git_client import GitClient, GitResult
from github_client import GitHubClient, GitHubError, replace_undecodable, subprocess_runner
from github_client import Runner as GhRunner
from github_remote import github_repository
from skill_roots import skills_directory_holding

T = TypeVar("T")
PLAN_SCHEMA_VERSION = 1
# Matched in any letter case: Windows treats Release and release as one directory, and refs stored as files there
# cannot hold both, so protection errs toward keeping.
RELEASE_PREFIX = "release/"
STALE_STATES = ("MERGED", "CLOSED")
# Cache directories a tool rebuilds on its next run and nothing else reads: an ignored path inside one carries no
# work of the user's, so a worktree holding only such paths is removed with them. Any other ignored path, such as a
# `.env` or build output, preserves the worktree. Add a name here only for a cache whose loss costs nothing but time.
DISPOSABLE_CACHES = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules/.cache"})
# How many paths a report line names before it counts the rest, so a worktree full of them does not flood the line.
NAMED_PATHS = 5
# The status listing every check of a worktree reads. status.showUntrackedFiles=no would hide untracked files from it,
# and fail it with --ignored, so the untracked-files mode is named rather than taken from the user's configuration.
STATUS_ARGUMENTS = ("status", "--porcelain", "-z", "--untracked-files=normal")
# git worktree remove checks for changes with a status of its own, which reads the same setting.
SHOW_UNTRACKED = ("-c", "status.showUntrackedFiles=normal")
KEPT_STATES = {"OPEN": "pr-open", "UNMATCHED": "pr-unmatched", "UNKNOWN": "pr-unknown"}
WORKTREE_AREA = "Worktrees"
# Where Claude Code, and many repositories' own worktree tools, put a repository's worktrees. With
# WORKTREE_AREA/<repository name>, the folders a sweep searches for directories a worktree removal left behind.
REPOSITORY_AREA = (".claude", "worktrees")
# What a clean linked worktree becomes, by its branch's planned action: removed, offered, or else kept.
WORKTREE_ACTIONS = {"remove-worktree": "remove", "remove-worktree-ask": "remove", "ask-remove-worktree": "ask"}
# Control characters and the line separators str.splitlines also breaks on, so every fact stays on one line.
CONTROL = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]+")
SUMMARY_WIDTH = 25
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
    """`value` on one line, with each byte Git gave that is not UTF-8, such as in a branch name, as U+FFFD."""
    return CONTROL.sub(" ", replace_undecodable(str(value))[0]).rstrip()


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


@dataclass
class Worktree:
    path: str
    branch: str | None
    main: bool
    head: str = ""


def list_worktrees(services: Services, root: str | Path) -> list[Worktree]:
    """Every worktree from Git's NUL-separated porcelain listing; the first is the main worktree."""
    records: list[dict[str, Any]] = []
    for field in git_output(services, root, "worktree", "list", "--porcelain", "-z").split("\0"):
        if field.startswith("worktree "):
            records.append({"path": field[len("worktree ") :], "branch": None, "head": ""})
        elif records and field.startswith("branch refs/heads/"):
            records[-1]["branch"] = field[len("branch refs/heads/") :]
        elif records and field.startswith("HEAD "):
            records[-1]["head"] = field[len("HEAD ") :]
    return [
        Worktree(record["path"], record["branch"], index == 0, record["head"]) for index, record in enumerate(records)
    ]


def checkouts(services: Services, root: str | Path) -> dict[str, Worktree]:
    return {tree.branch: tree for tree in list_worktrees(services, root) if tree.branch}


def same_path(first: str, second: str) -> bool:
    # Lexical: Path.absolute keeps ".." and Path.resolve follows junctions, which would change what matches.
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))  # noqa: PTH100 - lexical


def status_codes(services: Services, directory: str | Path, *options: str) -> list[str]:
    """The two-letter code of each path `git status --porcelain -z` lists, a rename or copy counted once."""
    codes: list[str] = []
    fields = iter(git_output(services, directory, *STATUS_ARGUMENTS, *options).split("\0"))
    for entry in fields:
        if not entry:
            continue
        codes.append(entry[:2])
        if "R" in entry[:2] or "C" in entry[:2]:
            next(fields, None)  # a rename or copy is followed by its original path
    return codes


def dirty_count(services: Services, directory: str | Path) -> int:
    """Number of changed or untracked paths, from `git status --porcelain -z`."""
    return len(status_codes(services, directory))


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


def is_release(branch: str) -> bool:
    return branch.casefold().startswith(RELEASE_PREFIX)


def is_protected(branch: str, path: str) -> bool:
    """A release branch, or a worktree with a directory named release anywhere in its path, in any letter case."""
    if is_release(branch):
        return True
    return "/release/" in f"/{path.replace(chr(92), '/').removeprefix('/')}/".casefold()


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
    emit("DEFAULT", default.name, *([default.note] if default.note else []))
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


def evaluate_worktree(services: Services, root: str, default: str, tree: Worktree) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": tree.path, "branch": tree.branch, "action": "keep", "detail": ""}
    if is_protected(tree.branch or "", tree.path):
        entry["action"] = "protected"
        return entry
    try:
        changed = dirty_count(services, tree.path)
    except CleanupError as exc:
        entry["detail"] = str(exc)
        return entry
    if tree.branch is not None:
        entry["action"] = f"dirty:{changed}" if changed else "candidate"
    else:
        entry.update(judge_detached(services, root, default, tree.head, changed))
    return entry


def judge_detached(services: Services, root: str, default: str, head: str, changed: int) -> dict[str, Any]:
    """A worktree with no branch is offered for removal only when it is clean and the default branch or a remote
    branch holds its commit, so removing it loses nothing; otherwise it is kept, and the detail says why."""
    if changed:
        return {"sha": head, "detail": counted(changed, "uncommitted change")}
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


def build_plan(root: str, repos_root: str, services: Services, chosen: DefaultBranch | None = None) -> dict[str, Any]:
    """The repository's plan, against `chosen`, the default branch sync found, or the one found now when None."""
    require_repository(root)
    default = (chosen or default_branch(services, root)).name
    nwo = name_with_owner(services, root)
    trees = list_worktrees(services, root)
    located = {tree.branch: tree for tree in trees if tree.branch}
    worktrees = [evaluate_worktree(services, root, default, tree) for tree in trees if not tree.main]
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
        if is_release(name):
            action, detail = "keep", "release"
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


def save_plan(path: str | Path, plan: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".repo-cleanup-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(plan, handle, indent=2)
            handle.write("\n")
        Path(temporary).replace(target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def load_plan(path: str | Path) -> dict[str, Any]:
    try:
        plan = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CleanupError(f"cannot read plan {path}: {exc}") from exc
    if (
        not isinstance(plan, dict)
        or type(plan.get("schema_version")) is not int
        or plan["schema_version"] != PLAN_SCHEMA_VERSION
    ):
        raise CleanupError(f"{path} is not a repo-cleanup plan")
    return plan


def record(plan: dict[str, Any], kind: str, *fields: Any) -> None:
    plan["events"].append([kind, *(one_line(field) for field in fields)])
    emit(kind, *fields)


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
        record(plan, "UNMERGED", name, sha)
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


def is_plain_directory(path: Path) -> bool:
    try:
        status = os.lstat(path)
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISDIR(status.st_mode) and not getattr(status, "st_file_attributes", 0) & reparse


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
    listing = git_output(services, directory, *STATUS_ARGUMENTS, "--ignored=matching")
    return [entry[3:] for entry in listing.split("\0") if entry.startswith("!! ")]


def is_disposable(path: str) -> bool:
    """Whether the ignored path is one of the regenerable caches, or lies inside one."""
    inside = f"/{path.replace(chr(92), '/').strip('/')}/"
    return any(f"/{cache}/" in inside for cache in DISPOSABLE_CACHES)


def named(paths: list[str]) -> str:
    """The first paths, comma-separated, then how many more there are."""
    rest = len(paths) - NAMED_PATHS
    return ", ".join(paths[:NAMED_PATHS]) + (f", and {rest} more" if rest > 0 else "")


def counted(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def ignored_paths(services: Services, path: str) -> tuple[str, str]:
    """Why a worktree's ignored files keep it, and what is deleted with it.

    git worktree remove counts a worktree holding only ignored files, such as a .env, as clean and deletes them, so
    a worktree holding any ignored path outside DISPOSABLE_CACHES is kept, and the reason names the paths. The second
    string names the caches, present with the reason or, when the caches are all the worktree holds, alone with an
    empty reason, so the removal can say what it deleted. Both are empty for a worktree with no ignored path.
    """
    try:
        entries = ignored_entries(services, path)
    except CleanupError as exc:
        return str(exc), ""
    kept = [entry for entry in entries if not is_disposable(entry)]
    caches = [entry for entry in entries if is_disposable(entry)]
    disposed = f"deleted {counted(len(caches), 'regenerable cache')}: {named(caches)}" if caches else ""
    if not kept:
        return "", disposed
    reason = (
        f"worktree {path} holds {counted(len(kept), 'ignored path')} git worktree remove would delete: {named(kept)}"
    )
    if caches:
        reason += f"; also {counted(len(caches), 'regenerable cache')}: {named(caches)}"
    return reason, disposed


def remove_checkout(services: Services, plan: dict[str, Any], path: str, name: str, label: str) -> str:
    """git worktree remove, which refuses a locked worktree or one with changes: "removed", "kept" when its ignored
    files keep it, or "refused" when Git refused.

    A worktree with ignored files is kept, since Git would delete them with it, unless every one is a regenerable
    cache, which the REMOVED fact names after `label`. A PRESERVED fact names `name` with the reason it stayed.
    """
    held, disposed = ignored_paths(services, path)
    if held:
        record(plan, "PRESERVED", name, held)
        return "kept"
    removed = git(services, plan["repo_root"], *SHOW_UNTRACKED, "worktree", "remove", path)
    if removed.returncode != 0:
        record(plan, "PRESERVED", name, f"Git refused to remove worktree {path}: {reason(removed)}")
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
        record(plan, "CONFIRM_LOCAL", entry["branch"])
        return Removal(removed=True, refused=False)
    return Removal(removed=True, refused=not delete_merged(services, plan, entry["branch"], sha, pr))


def apply(plan_path: str, services: Services) -> bool:
    """Apply the plan; False when Git refused a deletion or removal."""
    plan = load_plan(plan_path)
    if plan["applied"]:
        raise CleanupError("this plan was already applied; run plan again")
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
    detached worktrees, then the directories Git no longer tracks that are left once every removal ran."""
    root = plan["repo_root"]
    for branch in plan["branches"]:
        if branch["action"] == "ask-delete":
            if branch_tip(services, root, branch["name"]) == branch["sha"]:
                record(plan, "CONFIRM_LOCAL", branch["name"])
            else:
                record(plan, "PRESERVED", branch["name"], "moved")
        elif branch["action"] == "ask-remove-worktree" and unchanged(
            services, plan, branch["name"], branch["sha"], located, branch["worktree"]
        ):
            record(plan, "CONFIRM_LOCAL", branch["name"], branch["worktree"])
    offer_detached(services, plan)
    registered = [tree.path for tree in list_worktrees(services, root)]
    # orphan_areas is absent from an earlier version's plan.
    for path in orphan_directories(registered, plan.get("orphan_areas", []), area_anchors(plan)):
        record(plan, "ORPHAN", path)


def prune_parents(plan: dict[str, Any], removed: str) -> None:
    """Remove the empty directories a removal left, never at or above the worktree areas or the repository root."""
    boundaries = [plan["worktree_area"], *plan.get("orphan_areas", []), plan["repo_root"]]
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
                record(plan, "CONFIRM_DETACHED", entry["path"], entry["sha"])
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
    remote branch; False when it stays for a reason other than having moved."""
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
    """Delete an offered orphan directory that is still one; False when deleting it failed or it no longer resolves
    inside a worktree area.

    Just before the deletion, the directory must still be no link and, resolved through any link, lie inside an area
    the search may enter, resolved the same way, so the deletion never reaches a directory the search did not.
    """
    registered = [tree.path for tree in list_worktrees(services, plan["repo_root"])]
    areas = plan.get("orphan_areas", [])
    if not any(same_path(path, found) for found in orphan_directories(registered, areas, area_anchors(plan))):
        record(plan, "PRESERVED", path, "no longer a directory Git does not track")
        return True
    searchable = searchable_areas(areas, area_anchors(plan))
    if not is_plain_directory(Path(path)) or not any(strictly_inside(path, area) for area in searchable):
        record(plan, "PRESERVED", path, "it resolves outside the worktree areas")
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
    plan = load_plan(plan_path)
    if not plan["applied"]:
        raise CleanupError("run apply with this plan first")

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
    plan = load_plan(plan_path)
    if not plan["applied"]:
        raise CleanupError("run apply with this plan first")
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
        for entry in plan["worktrees"]
        if entry["action"].startswith("dirty:")
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
        f"  Protected release worktrees: {len(protected)} (preserved)",
    ]
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
    ):
        if items[label]:
            lines.append(summary_row(label, items[label]))
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


def applied_plan(path: Path | None) -> str | None:
    """The plan file of a repository whose apply started, so its summary and confirmations stay usable."""
    if path is None:
        return None
    try:
        return path.as_posix() if load_plan(path)["applied"] else None
    except CleanupError:
        return None


def sweep_repository(
    root: str, repos_root: str, plans: str, services: Services, skip_checkout: bool = False
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
            plan = build_plan(root, repos_root, services, DefaultBranch(**synced["default"]))
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
    except Exception as exc:  # a defect in one repository's run must not discard every other repository's report
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
        return result["state"] != "quiet"
    if kind == "CHECKOUT" and fields[0] == "failed":
        return True
    if kind in SYNC_KINDS:
        return result["plan"] is None and fields[0] in SYNC_KINDS[kind]
    return kind in SWEEP_KINDS


def sweep(
    target: str | None, repos_root: str, plans: Path | None, services: Services, skip_checkout: bool = False
) -> int:
    """Clean every repository at once; print each one's decisions, failures, and summary in discovery order.

    The GitHub CLI's access is checked before the plans directory is created, so a sweep that cannot start leaves
    nothing behind. Each repository is synced, planned, and applied in turn, re-checking every recorded branch tip.
    A failure in one repository never stops the others: every result is reported, and the sweep exits 1 when any
    repository needs the agent or Git refused a deletion or removal in one. skip_checkout cleans a dirty main worktree
    without switching its branch.
    """
    repositories = discover(target, repos_root, services)
    directory = plans or Path(tempfile.mkdtemp(prefix="repo-cleanup-plans-"))
    emit("PLANS", directory.as_posix())
    with ThreadPoolExecutor(max_workers=max(1, min(SWEEP_WORKERS, len(repositories)))) as pool:
        results = list(
            pool.map(
                lambda root: sweep_repository(root, repos_root, str(directory), services, skip_checkout), repositories
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
        *(
            f"{state}={states.get(state, 0)}"
            for state in ("cleaned", "quiet", "dirty", "fetch-failed", "error", "git-failed")
        ),
    )
    return 0 if set(states) <= {"cleaned", "quiet"} and not any(result["refused"] for result in results) else 1


# CLI ------------------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    sweep_parser = commands.add_parser("sweep")
    sweep_parser.add_argument("--repos-root", required=True)
    sweep_parser.add_argument(
        "--plans", help="directory for each repository's plan file; defaults to a new temporary directory"
    )
    sweep_parser.add_argument(
        "--skip-checkout", action="store_true", help="clean a dirty main worktree without switching branches"
    )
    sweep_parser.add_argument("target", nargs="?")
    commands.add_parser("summary").add_argument("--plan", required=True)
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
            plans = plans_location(options.plans)
            return sweep(options.target, options.repos_root, plans, services, options.skip_checkout)
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

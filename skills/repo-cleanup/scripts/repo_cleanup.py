"""Deterministic repository cleanup steps, so the agent only relays results and asks the user's confirmations.

    sweep         find the repositories to clean, then sync, plan, and apply every one at once, printing only what
                  needs the agent: decisions, failures, and summaries; plans go to a new temporary directory by
                  default
    delete-local  delete local-only branches the user chose to delete (git branch -d)
    force-delete  force-delete branches reported UNMERGED, after the user confirmed
    summary       print the repository's summary from the plan file

Every command prints one tab-separated fact per line. Exit status 0 means every repository was cleaned. 1 means
something needs the agent: a repository that stopped (DIRTY_MAIN, FETCH_FAILED) or was skipped with an ERROR line,
or a last line FAILED <reason> when the command could not run at all. 2 is a usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import pr_status
from console import use_utf8_output
from github_client import GitHubClient, GitHubError, subprocess_runner
from github_client import Runner as GhRunner
from skill_roots import deployed_skill_roots

# The skills directory holding this script: the source tree's skills/, or the deployed ~/.claude/skills.
SKILLS_ROOT = Path(__file__).resolve().parents[2]
PLAN_SCHEMA_VERSION = 1
RELEASE_PREFIX = "release/"
STALE_STATES = ("MERGED", "CLOSED")
KEPT_STATES = {"OPEN": "pr-open", "UNMATCHED": "pr-unmatched", "UNKNOWN": "pr-unknown"}
WORKTREE_AREA = "Worktrees"
GITHUB_REMOTE = re.compile(
    r"(?:https?://(?:[^@/]+@)?github\.com/|ssh://git@github\.com(?::\d+)?/|git@github\.com:)"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?",
    re.IGNORECASE,
)
CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
SUMMARY_WIDTH = 25
# Repositories a sweep cleans at once: fetches and gh queries are network-bound, and four stays clear of GitHub's
# secondary rate limits.
SWEEP_WORKERS = 4
# The lines a sweep passes on: what the agent must act on or report. Everything else is in the summary.
SWEEP_KINDS = {"DIRTY_MAIN", "FETCH_FAILED", "ERROR", "CONFIRM_LOCAL", "UNMERGED", "SUMMARY"}

Runner = pr_status.Runner


class CleanupError(Exception):
    """An expected problem with one repository: report it and skip the repository."""


class GitError(Exception):
    """Git itself could not run: report it and skip the repository."""


@dataclass
class Services:
    """External effects, replaceable in tests."""

    git: Runner = pr_status.run_git
    gh: GhRunner = subprocess_runner


# Output ---------------------------------------------------------------------------------------------------------


def one_line(value: Any) -> str:
    return CONTROL.sub(" ", str(value)).rstrip()


_capture = threading.local()


def emit(kind: str, *fields: Any) -> None:
    line = "\t".join([kind, *(one_line(field) if str(field).strip() else "-" for field in fields)])
    captured = getattr(_capture, "lines", None)
    if captured is None:
        print(line)
    else:
        captured.append(line)  # a sweep worker: printed with its repository once every repository is done


def reason(result: subprocess.CompletedProcess) -> str:
    return one_line(result.stderr or result.stdout or "").strip() or f"exit status {result.returncode}"


# Git ------------------------------------------------------------------------------------------------------------


def git(services: Services, directory: str | Path, *arguments: str) -> subprocess.CompletedProcess:
    try:
        return services.git(["-C", str(directory), "--no-optional-locks", *arguments])
    except OSError as exc:
        raise GitError(f"could not run git: {exc}") from exc


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


def ahead_behind(services: Services, root: str | Path, local: str, upstream: str) -> tuple[int, int]:
    ahead, behind = git_output(services, root, "rev-list", "--left-right", "--count", f"{local}...{upstream}").split()
    return int(ahead), int(behind)


@dataclass
class Worktree:
    path: str
    branch: str | None
    main: bool


def list_worktrees(services: Services, root: str | Path) -> list[Worktree]:
    """Every worktree from Git's NUL-separated porcelain listing; the first is the main worktree."""
    records: list[dict[str, Any]] = []
    for field in git_output(services, root, "worktree", "list", "--porcelain", "-z").split("\0"):
        if field.startswith("worktree "):
            records.append({"path": field[len("worktree ") :], "branch": None})
        elif records and field.startswith("branch refs/heads/"):
            records[-1]["branch"] = field[len("branch refs/heads/") :]
    return [Worktree(record["path"], record["branch"], index == 0) for index, record in enumerate(records)]


def checkouts(services: Services, root: str | Path) -> dict[str, Worktree]:
    return {tree.branch: tree for tree in list_worktrees(services, root) if tree.branch}


def same_path(first: str, second: str) -> bool:
    # Lexical: Path.absolute keeps ".." and Path.resolve follows junctions, which would change what matches.
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))  # noqa: PTH100 - lexical


def dirty_count(services: Services, directory: str | Path) -> int:
    """Number of changed or untracked paths, from `git status --porcelain -z`."""
    count = 0
    fields = iter(git_output(services, directory, "status", "--porcelain", "-z").split("\0"))
    for entry in fields:
        if not entry:
            continue
        count += 1
        if "R" in entry[:2] or "C" in entry[:2]:
            next(fields, None)  # a rename or copy is followed by its original path
    return count


def name_with_owner(services: Services, root: str | Path) -> str | None:
    """owner/name of a github.com origin, from the configured URL or its insteadOf expansion."""
    for arguments in (("config", "--get", "remote.origin.url"), ("remote", "get-url", "origin")):
        result = git(services, root, *arguments)
        match = GITHUB_REMOTE.fullmatch(result.stdout.strip()) if result.returncode == 0 else None
        if match:
            return match.group(1)
    return None


# Branches and worktrees -----------------------------------------------------------------------------------------


def default_branch(services: Services, root: str | Path) -> str:
    """The branch origin/HEAD names, else main or master when origin has it."""
    head = git(services, root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    name = head.stdout.strip().removeprefix("refs/remotes/origin/") if head.returncode == 0 else ""
    if name and name != head.stdout.strip():
        return name
    for name in ("main", "master"):
        if git(services, root, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{name}").returncode == 0:
            return name
    raise CleanupError("cannot determine default branch")


def is_protected(branch: str, path: str) -> bool:
    """A release branch, or a worktree with a directory named release anywhere in its path."""
    if branch.startswith(RELEASE_PREFIX):
        return True
    return "/release/" in f"/{path.replace(chr(92), '/').removeprefix('/')}/"


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
        in_base = pr_status.base_contains(str(root), f"refs/remotes/origin/{default}", services.git)
        return pr_status.classify(nwo, branch, head, upstream, services.gh, in_base), ""
    except pr_status.QueryError as exc:
        return "UNKNOWN", str(exc)


def fast_forward(services: Services, root: str, branch: str, old: str, new: str, worktree: str | None) -> str:
    """Fast-forward only; returns an empty string on success, otherwise Git's reason."""
    if worktree is None:
        result = git(services, root, "update-ref", "-m", "repo-cleanup: fast-forward", f"refs/heads/{branch}", new, old)
    else:
        head = git(services, worktree, "symbolic-ref", "--quiet", "HEAD").stdout.strip()
        if head != f"refs/heads/{branch}":
            return "the worktree no longer has the branch checked out"
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
    try:
        GitHubClient(services.gh).run(["auth", "status"])
        authenticated = True
    except (GitHubError, OSError):
        authenticated = False
    if not authenticated:
        raise CleanupError("GitHub CLI not authenticated — run 'gh auth login'")
    return [path.as_posix() for path in repositories]


# sync -----------------------------------------------------------------------------------------------------------


def sync(root: str, skip_checkout: bool, services: Services) -> bool:
    """Switch to the default branch, fetch and prune, and fast-forward it; False when it stopped for the user."""
    require_repository(root)
    default = default_branch(services, root)
    emit("DEFAULT", default)
    dirty = dirty_count(services, root)
    if dirty:
        emit("DIRTY_MAIN", dirty)
        if not skip_checkout:
            return False
    if skip_checkout:
        emit("CHECKOUT", "skipped")
    elif git(services, root, "symbolic-ref", "--quiet", "HEAD").stdout.strip() == f"refs/heads/{default}":
        emit("CHECKOUT", "current")
    else:
        switched = git(services, root, "switch", "--quiet", default)
        if switched.returncode == 0:
            emit("CHECKOUT", "switched")
        else:
            emit("CHECKOUT", "failed", reason(switched))
    fetched = git(services, root, "fetch", "--all", "--prune", "--quiet")
    if fetched.returncode != 0:
        error = reason(fetched)
        emit("FETCH_FAILED", error)
        for line in fetch_failed_summary(Path(root).name, error):
            emit("SUMMARY", line)
        return False
    git_output(services, root, "worktree", "prune")
    emit("FF_DEFAULT", *fast_forward_default(services, root, default))
    return True


def fast_forward_default(services: Services, root: str, default: str) -> tuple[Any, ...]:
    local = branch_tip(services, root, default)
    target = resolve(services, root, f"refs/remotes/origin/{default}")
    if local is None or target is None:
        return ("skipped", f"no {'local branch' if local is None else 'origin/' + default}")
    ahead, behind = ahead_behind(services, root, local, target)
    if behind == 0:
        return ("ok",)
    if ahead:
        return ("diverged", ahead, behind)
    tree = checkouts(services, root).get(default)
    error = fast_forward(services, root, default, local, target, tree.path if tree else None)
    return ("ok",) if not error else ("failed", error)


def fetch_failed_summary(name: str, error: str) -> list[str]:
    return [
        f"{name} skipped: fetch failed — {error}",
        "  No branches were deleted, no worktrees were pruned or removed, and nothing was fast-forwarded.",
    ]


# plan -----------------------------------------------------------------------------------------------------------


def decide(
    category: str, state: str, error: str, tree: Worktree | None, linked: dict[str, Any] | None
) -> tuple[str, str]:
    """The planned action for one non-release branch, mirroring the cleanup rules."""
    if state in KEPT_STATES:
        return "keep", f"{KEPT_STATES[state]}: {error}" if error else KEPT_STATES[state]
    stale = state in STALE_STATES or (category == "gone" and state == "NONE")
    if tree is not None and tree.main:
        return "keep", "checked-out-main" if stale or category == "local" else ""
    if linked is not None:
        if linked["action"] == "candidate":
            return ("remove-worktree", "") if stale else ("keep", "in-worktree")
        return "keep", f"worktree-{linked['action'].split(':')[0]}"
    if category == "gone" and stale:
        return "delete", ""
    if category == "local" and state in (*STALE_STATES, "NONE"):
        return "ask-delete", ""
    return "keep", ""


def evaluate_worktree(services: Services, tree: Worktree) -> dict[str, Any]:
    entry = {"path": tree.path, "branch": tree.branch, "action": "keep", "detail": ""}
    if tree.branch is None:
        entry["detail"] = "detached"
        return entry
    if is_protected(tree.branch, tree.path):
        entry["action"] = "protected"
        return entry
    try:
        changed = dirty_count(services, tree.path)
        entry["action"] = f"dirty:{changed}" if changed else "candidate"
    except CleanupError as exc:
        entry["detail"] = str(exc)
    return entry


def build_plan(root: str, repos_root: str, services: Services) -> dict[str, Any]:
    require_repository(root)
    default = default_branch(services, root)
    nwo = name_with_owner(services, root)
    trees = list_worktrees(services, root)
    located = {tree.branch: tree for tree in trees if tree.branch}
    worktrees = [evaluate_worktree(services, tree) for tree in trees if not tree.main]
    linked = {entry["branch"]: entry for entry in worktrees if entry["branch"]}
    branches: list[dict[str, Any]] = []
    fastforward: list[dict[str, Any]] = []
    diverged: list[dict[str, Any]] = []
    listing = git_output(
        services,
        root,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(upstream)%00%(upstream:track)",
        "refs/heads/",
    )
    for line in listing.splitlines():
        ref, sha, upstream, track = line.split("\0")
        name = ref[len("refs/heads/") :]
        if name == default:
            continue
        category = "local" if not upstream else "gone" if track == "[gone]" else "tracking"
        tree, entry = located.get(name), linked.get(name)
        state, error = "-", ""
        if name.startswith(RELEASE_PREFIX):
            action, detail = "keep", "release"
        else:
            if category != "tracking" or (entry is not None and entry["action"] == "candidate"):
                state, error = pull_state(services, root, nwo, default, name, sha)
            action, detail = decide(category, state, error, tree, entry)
        if entry is not None and entry["action"] == "candidate":
            entry["action"] = "remove" if action == "remove-worktree" else "keep"
            entry["detail"] = "" if action == "remove-worktree" else detail
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
            target = resolve(services, root, upstream)
            if target is None or target == sha:
                continue
            ahead, behind = ahead_behind(services, root, sha, target)
            if behind and not ahead:
                fastforward.append(
                    {"branch": name, "sha": sha, "target": target, "worktree": tree.path if tree else None}
                )
            elif behind:
                diverged.append({"branch": name, "ahead": ahead, "behind": behind})
    for entry in worktrees:
        if entry["action"] == "candidate":
            entry["action"] = "keep"
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "repo_root": str(root),
        "repo_name": Path(root).name,
        "worktree_area": str(Path(repos_root) / WORKTREE_AREA),
        "default": {"name": default, "status": default_status(services, root, default)},
        "branches": branches,
        "worktrees": worktrees,
        "fastforward": fastforward,
        "diverged": diverged,
        "applied": False,
        "events": [],
    }


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
    if not isinstance(plan, dict) or plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise CleanupError(f"{path} is not a repo-cleanup plan")
    return plan


def record(plan: dict[str, Any], kind: str, *fields: Any) -> None:
    plan["events"].append([kind, *(one_line(field) for field in fields)])
    emit(kind, *fields)


def events(plan: dict[str, Any], kind: str) -> list[list[str]]:
    return [event[1:] for event in plan["events"] if event[0] == kind]


def deleted(plan: dict[str, Any]) -> set[str]:
    return {fields[0] for fields in events(plan, "DELETED")}


# apply and confirmations ----------------------------------------------------------------------------------------


def delete_merged(services: Services, plan: dict[str, Any], name: str, sha: str, pr: str) -> None:
    """git branch -d after the tip check; a refusal on an unchanged branch means it is not fully merged."""
    result = git(services, plan["repo_root"], "branch", "-d", name)
    if result.returncode == 0:
        record(plan, "DELETED", name)
    elif branch_tip(services, plan["repo_root"], name) == sha:
        unmerged(services, plan, name, sha, pr)
    else:
        record(plan, "PRESERVED", name, reason(result))


def unmerged(services: Services, plan: dict[str, Any], name: str, sha: str, pr: str) -> None:
    """Force-delete an unchanged branch Git calls unmerged only when its pull request merged at this exact tip.

    That is a squash or rebase merge: GitHub proved the work landed although no commit of the branch is on the
    default branch. Any other unmerged branch waits for the user's confirmation.
    """
    if pr != "MERGED":
        record(plan, "UNMERGED", name, sha)
        return
    result = git(services, plan["repo_root"], "branch", "-D", name)
    if result.returncode == 0:
        record(plan, "DELETED", name)
    else:
        record(plan, "PRESERVED", name, reason(result))


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


def strictly_inside(path: str, boundary: str) -> bool:
    path, boundary = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(boundary))
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


def remove_worktree(services: Services, plan: dict[str, Any], entry: dict[str, Any], sha: str, pr: str) -> bool:
    """git worktree remove, which refuses a locked worktree or one with changes, then git branch -d."""
    removed = git(services, plan["repo_root"], "worktree", "remove", entry["path"])
    if removed.returncode != 0:
        record(plan, "PRESERVED", entry["branch"], f"Git refused to remove worktree {entry['path']}: {reason(removed)}")
        return False
    record(plan, "REMOVED", entry["path"], entry["branch"])
    if git(services, plan["repo_root"], "branch", "-d", entry["branch"]).returncode == 0:
        record(plan, "DELETED", entry["branch"])
    elif branch_tip(services, plan["repo_root"], entry["branch"]) == sha:
        unmerged(services, plan, entry["branch"], sha, pr)
    else:
        record(plan, "PRESERVED", entry["branch"], "moved")
    return True


def apply(plan_path: str, services: Services) -> None:
    plan = load_plan(plan_path)
    if plan["applied"]:
        raise CleanupError("this plan was already applied; run plan again")
    plan["applied"] = True
    try:
        apply_plan(plan, services)
    finally:
        save_plan(plan_path, plan)
    print_summary(plan)


def apply_plan(plan: dict[str, Any], services: Services) -> None:
    root = plan["repo_root"]
    branches = {branch["name"]: branch for branch in plan["branches"]}
    located = checkouts(services, root)
    for branch in plan["branches"]:
        if branch["action"] == "delete" and unchanged(services, plan, branch["name"], branch["sha"], located, None):
            delete_merged(services, plan, branch["name"], branch["sha"], branch["pr"])
    removed: list[str] = []
    for entry in plan["worktrees"]:
        if entry["action"] != "remove":
            continue
        sha, pr = branches[entry["branch"]]["sha"], branches[entry["branch"]]["pr"]
        if unchanged(services, plan, entry["branch"], sha, located, entry["path"]) and remove_worktree(
            services, plan, entry, sha, pr
        ):
            removed.append(entry["path"])
    boundaries = [plan["worktree_area"], root]
    for path in removed:
        for directory in prune_empty_parents(path, boundaries):
            record(plan, "PRUNED_DIR", directory)
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
    for branch in plan["branches"]:
        if branch["action"] == "ask-delete":
            if branch_tip(services, root, branch["name"]) == branch["sha"]:
                record(plan, "CONFIRM_LOCAL", branch["name"])
            else:
                record(plan, "PRESERVED", branch["name"], "moved")


def confirm(plan_path: str, names: list[str], services: Services, force: bool) -> None:
    plan = load_plan(plan_path)
    if not plan["applied"]:
        raise CleanupError("run apply with this plan first")
    states = {branch["name"]: branch["pr"] for branch in plan["branches"]}
    if force:
        eligible = {fields[0]: fields[1] for fields in events(plan, "UNMERGED")}
        refusal = "not reported UNMERGED by this plan"
    else:
        eligible = {branch["name"]: branch["sha"] for branch in plan["branches"] if branch["action"] == "ask-delete"}
        refusal = "not a local-only branch offered by this plan"
    try:
        located = checkouts(services, plan["repo_root"])
        for name in dict.fromkeys(names):
            if name not in eligible or name in deleted(plan):
                record(plan, "PRESERVED", name, refusal)
            elif unchanged(services, plan, name, eligible[name], located, None):
                if not force:
                    delete_merged(services, plan, name, eligible[name], states[name])
                    continue
                result = git(services, plan["repo_root"], "branch", "-D", name)
                if result.returncode == 0:
                    record(plan, "DELETED", name)
                else:
                    record(plan, "PRESERVED", name, reason(result))
    finally:
        save_plan(plan_path, plan)
    print_summary(plan)


# summary --------------------------------------------------------------------------------------------------------


def summary_row(label: str, items: list[str], suffix: str = "") -> str:
    value = f"{len(items)} — {', '.join(items)}{suffix}" if items else "0"
    return f"  {(label + ':').ljust(SUMMARY_WIDTH - 1)} {value}"


def summary_items(plan: dict[str, Any]) -> dict[str, list[str]]:
    """Every list the summary reports, by label; all empty means nothing happened and nothing was kept."""
    removed_branches = deleted(plan)
    unmerged = [fields[0] for fields in events(plan, "UNMERGED") if fields[0] not in removed_branches]
    local = [
        fields[0]
        for fields in events(plan, "CONFIRM_LOCAL")
        if fields[0] not in removed_branches and fields[0] not in unmerged
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
    open_gone = [
        branch["name"] for branch in plan["branches"] if branch["pr"] == "OPEN" and branch["category"] == "gone"
    ]
    return {
        "Branches deleted": [fields[0] for fields in events(plan, "DELETED")],
        "Worktrees removed": [f"{path} ({branch})" for path, branch in events(plan, "REMOVED")],
        "Branches fast-forwarded": [fields[0] for fields in events(plan, "FF")],
        "Diverged (manual)": [
            f"{name} (ahead {ahead}, behind {behind})" for name, ahead, behind in events(plan, "DIVERGED")
        ],
        "Dirty worktrees skipped": dirty,
        "PR status unverified": unknown,
        "PR history unmatched": unmatched,
        "Unmerged (kept)": unmerged,
        "Local-only (kept)": local,
        "Preserved": [f"{name}: {why}" for name, why in events(plan, "PRESERVED")],
        "Gone with open PR": open_gone,
        "Empty directories removed": [fields[0] for fields in events(plan, "PRUNED_DIR")],
    }


def summary_lines(plan: dict[str, Any]) -> list[str]:
    items = summary_items(plan)
    protected = [entry for entry in plan["worktrees"] if entry["action"] == "protected"]
    lines = [
        f"{plan['repo_name']} cleanup complete:",
        f"  {'Default branch:'.ljust(SUMMARY_WIDTH - 1)} {plan['default']['name']} ({plan['default']['status']})",
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
        "Unmerged (kept)",
        "Local-only (kept)",
        "Preserved",
        "Gone with open PR",
        "Empty directories removed",
    ):
        if items[label]:
            lines.append(summary_row(label, items[label]))
    return lines


def is_quiet(plan: dict[str, Any]) -> bool:
    """Nothing changed and nothing needs the user: its summary would only say the default branch is up to date."""
    return plan["default"]["status"] == "up to date" and not any(summary_items(plan).values())


def print_summary(plan: dict[str, Any]) -> None:
    for line in summary_lines(plan):
        emit("SUMMARY", line)


# sweep ----------------------------------------------------------------------------------------------------------


def plans_directory(explicit: str | None) -> Path:
    """Where a sweep keeps its plan files: `explicit`, or a new temporary directory.

    A file left inside a skill directory makes the deployer see that skill as modified and stop updating it, so an
    explicit directory inside any skills directory is refused before anything runs.
    """
    if explicit is None:
        return Path(tempfile.mkdtemp(prefix="repo-cleanup-plans-"))
    target = Path(explicit).resolve()
    for root in (SKILLS_ROOT, *deployed_skill_roots()):
        if target.is_relative_to(root.resolve()):
            raise ValueError(
                f"{explicit} is inside the skills directory {root}; omit --plans to keep the plans "
                "in a new temporary directory"
            )
    return Path(explicit)


def sweep_repository(
    root: str, repos_root: str, plans: str, services: Services, skip_checkout: bool = False
) -> dict[str, Any]:
    """sync, plan, and apply one repository, capturing its lines. Runs on a sweep worker thread."""
    lines: list[str] = []
    _capture.lines = lines
    result: dict[str, Any] = {"root": root, "lines": lines, "plan": None}
    try:
        if not sync(root, skip_checkout, services):
            dirty = any(line.split("\t")[0] == "DIRTY_MAIN" for line in lines)
            return {**result, "state": "dirty" if dirty else "fetch-failed"}
        plan_path = Path(plans) / f"{Path(root).name}.json"
        save_plan(plan_path, build_plan(root, repos_root, services))
        apply(str(plan_path), services)
        state = "quiet" if is_quiet(load_plan(plan_path)) else "cleaned"
        return {**result, "state": state, "plan": plan_path.as_posix()}
    except (CleanupError, OSError) as exc:
        emit("ERROR", exc)
        return {**result, "state": "error"}
    except GitError as exc:
        emit("ERROR", exc)
        return {**result, "state": "git-failed"}
    finally:
        _capture.lines = None


def sweep(target: str | None, repos_root: str, plans: str, services: Services, skip_checkout: bool = False) -> int:
    """Clean every repository at once; print each one's decisions, failures, and summary in discovery order.

    Each repository is synced, planned, and applied in turn, re-checking every recorded branch tip. A failure in one
    repository never stops the others: every result is reported, and the sweep exits 1 when any repository needs
    the agent. skip_checkout cleans a dirty main worktree without switching its branch.
    """
    repositories = discover(target, repos_root, services)
    with ThreadPoolExecutor(max_workers=max(1, min(SWEEP_WORKERS, len(repositories)))) as pool:
        results = list(
            pool.map(lambda root: sweep_repository(root, repos_root, plans, services, skip_checkout), repositories)
        )
    states: dict[str, int] = {}
    for result in results:
        states[result["state"]] = states.get(result["state"], 0) + 1
        emit("REPO", result["root"], result["state"])
        if result["plan"]:
            emit("PLAN", result["plan"])
        for line in result["lines"]:
            kind = line.split("\t")[0]
            if (kind in SWEEP_KINDS and not (kind == "SUMMARY" and result["state"] == "quiet")) or (
                kind == "CHECKOUT" and line.split("\t")[1] == "failed"
            ):
                print(line)
    emit(
        "SWEPT",
        len(results),
        *(
            f"{state}={states.get(state, 0)}"
            for state in ("cleaned", "quiet", "dirty", "fetch-failed", "error", "git-failed")
        ),
    )
    return 0 if set(states) <= {"cleaned", "quiet"} else 1


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
    return parser


def main(arguments: list[str] | None = None, services: Services | None = None) -> int:
    options = build_parser().parse_args(arguments)
    services = services or Services()
    try:
        if options.command == "sweep":
            plans = plans_directory(options.plans)
            emit("PLANS", plans.as_posix())
            return sweep(options.target, options.repos_root, str(plans), services, options.skip_checkout)
        if options.command == "summary":
            print_summary(load_plan(options.plan))
        else:
            confirm(options.plan, options.branches, services, force=options.command == "force-delete")
    except (CleanupError, GitError, OSError, ValueError) as exc:
        emit("FAILED", exc)
        return 1
    return 0


if __name__ == "__main__":
    use_utf8_output(newline="\n")
    raise SystemExit(main())

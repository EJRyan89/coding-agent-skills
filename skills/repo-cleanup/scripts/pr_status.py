"""Classify a branch by the pull requests that match its current work, failing closed on any query problem."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from typing import Any

LIMIT = 1000
FIELDS = "number,state,headRefOid,headRepository,headRepositoryOwner"
# GitHub's pull request commits endpoint lists at most this many commits, so a list this long may be truncated.
COMMIT_LIMIT = 250
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

Runner = Callable[[list[str]], subprocess.CompletedProcess]
InBase = Callable[[str], bool]


class QueryError(Exception):
    pass


def run_gh(arguments: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *arguments], capture_output=True, text=True, encoding="utf-8")


def _head_repository(pull: dict[str, Any]) -> str | None:
    owner, repository = pull.get("headRepositoryOwner"), pull.get("headRepository")
    if not isinstance(owner, dict) or not isinstance(repository, dict):
        return None
    login, name = owner.get("login"), repository.get("name")
    if not isinstance(login, str) or not isinstance(name, str):
        return None
    return f"{login}/{name}".casefold()


def run_git(arguments: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *arguments], capture_output=True, text=True, encoding="utf-8")


def _git_output(runner: Runner, arguments: list[str]) -> subprocess.CompletedProcess:
    try:
        return runner(arguments)
    except OSError as exc:
        raise QueryError(f"could not run git: {exc}") from exc


def branch_tips(repository_root: str, branch: str, runner: Runner = run_git) -> tuple[str, str | None]:
    """Return the branch's local tip and its fetched upstream tip, or None when it has no existing upstream."""
    local = _git_output(
        runner, ["-C", repository_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"]
    )
    head = local.stdout.strip()
    if local.returncode != 0 or not SHA_PATTERN.fullmatch(head):
        raise QueryError(f"cannot resolve local branch {branch!r}")
    configured = _git_output(
        runner,
        ["-C", repository_root, "for-each-ref", "--format=%(upstream)|%(upstream:track)", f"refs/heads/{branch}"],
    )
    if configured.returncode != 0 or "|" not in configured.stdout:
        raise QueryError(f"cannot read the upstream of {branch!r}: {(configured.stderr or '').strip()}")
    upstream_ref, track = configured.stdout.strip().split("|", 1)
    if not upstream_ref or track == "[gone]":
        return head, None
    upstream = _git_output(
        runner, ["-C", repository_root, "rev-parse", "--verify", "--quiet", f"{upstream_ref}^{{commit}}"]
    )
    upstream_sha = upstream.stdout.strip()
    if upstream.returncode != 0 or not SHA_PATTERN.fullmatch(upstream_sha):
        raise QueryError(f"cannot resolve upstream {upstream_ref!r} of {branch!r}")
    return head, upstream_sha


def base_contains(repository_root: str, base_ref: str, runner: Runner = run_git) -> InBase:
    """A test of whether a commit is reachable from base_ref; a commit missing locally is not."""

    def contains(sha: str) -> bool:
        present = _git_output(runner, ["-C", repository_root, "cat-file", "-e", f"{sha}^{{commit}}"])
        if present.returncode != 0:
            return False
        result = _git_output(runner, ["-C", repository_root, "merge-base", "--is-ancestor", sha, base_ref])
        if result.returncode not in (0, 1):
            raise QueryError(f"cannot test whether {sha} is in {base_ref}: {(result.stderr or '').strip()}")
        return result.returncode == 0

    return contains


def _commit_parents(commit: Any) -> tuple[str, list[str]] | None:
    """A commit object's SHA and its parents' SHAs, or None unless every one is a full lowercase SHA."""
    if not isinstance(commit, dict) or not isinstance(commit.get("parents"), list):
        return None
    shas = [
        commit.get("sha"),
        *(parent.get("sha") if isinstance(parent, dict) else None for parent in commit["parents"]),
    ]
    valid = [sha for sha in shas if isinstance(sha, str) and SHA_PATTERN.fullmatch(sha)]
    if len(valid) != len(shas):
        return None
    return valid[0], valid[1:]


def pull_commits(repository: str, number: int, runner: Runner = run_gh) -> dict[str, list[str]]:
    """Map each commit of the pull request to its parents, failing closed on any query problem."""
    try:
        result = runner(["api", "--paginate", "--slurp", f"repos/{repository}/pulls/{number}/commits?per_page=100"])
    except OSError as exc:
        raise QueryError(f"could not run gh: {exc}") from exc
    if result.returncode != 0:
        raise QueryError(
            f"gh api for the commits of pull request {number} failed ({result.returncode}): "
            f"{(result.stderr or '').strip()}"
        )
    # --slurp wraps the pages in one array, so the output is a list of pages, each a list of commits.
    try:
        pages = json.loads(result.stdout)
    except ValueError:
        pages = None
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        raise QueryError(f"gh api returned malformed commits for pull request {number}: {result.stdout.strip()[:200]}")
    commits: dict[str, list[str]] = {}
    for commit in (commit for page in pages for commit in page):
        parsed = _commit_parents(commit)
        if parsed is None:
            raise QueryError(f"gh api returned an unexpected commit for pull request {number}: {repr(commit)[:200]}")
        commits[parsed[0]] = parsed[1]
    if len(commits) >= COMMIT_LIMIT:
        raise QueryError(f"pull request {number} has {COMMIT_LIMIT} or more commits; the list may be incomplete")
    return commits


def only_base_merged_after(tip: str, pull_head: str, commits: dict[str, list[str]], in_base: InBase) -> bool:
    """Whether the pull request's head follows tip only through merges that brought in base commits.

    Walks first parents from the pull request's head back to tip, which must itself be a pull request commit.
    Every commit passed on the way must be a pull request commit with two or more parents, each of whose
    non-first parents is reachable from the base.
    """
    if tip not in commits:
        return False
    current = pull_head
    for _ in range(len(commits)):
        if current == tip:
            return True
        parents = commits.get(current)
        if parents is None or len(parents) < 2 or not all(in_base(parent) for parent in parents[1:]):
            return False
        current = parents[0]
    return False


def classify(
    repository: str,
    branch: str,
    head_sha: str,
    upstream_sha: str | None = None,
    runner: Runner = run_gh,
    in_base: InBase | None = None,
) -> str:
    """Return the branch state.

    OPEN: any pull request with this head name is open.
    MERGED or CLOSED: a same-repository pull request was merged or closed at exactly the branch's current tip,
    and the fetched upstream, when it exists, has no other commits. Given in_base, a merged same-repository pull
    request also proves the branch MERGED when the tip is one of its commits and every commit after the tip is a
    merge that only brought in commits reachable from the base, as GitHub's "Update branch" makes.
    UNMATCHED: same-repository pull requests exist, but none describes the branch's current work.
    NONE: no same-repository pull request uses this head name.
    """
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise QueryError(f"repository must be owner/name, got {repository!r}")
    for label, sha in (("head", head_sha), ("upstream", upstream_sha)):
        if sha is not None and not SHA_PATTERN.fullmatch(sha):
            raise QueryError(f"{label} SHA must be a full lowercase commit SHA, got {sha!r}")
    try:
        result = runner(
            [
                "pr",
                "list",
                "--repo",
                repository,
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                FIELDS,
                "--limit",
                str(LIMIT),
            ]
        )
    except OSError as exc:
        raise QueryError(f"could not run gh: {exc}") from exc
    if result.returncode != 0:
        raise QueryError(f"gh pr list failed ({result.returncode}): {(result.stderr or '').strip()}")
    try:
        pulls = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise QueryError(f"gh pr list returned invalid JSON: {exc}") from exc
    if not isinstance(pulls, list):
        raise QueryError("gh pr list did not return a list")
    if len(pulls) >= LIMIT:
        raise QueryError(f"branch matches {LIMIT} or more pull requests; the result may be incomplete")
    for pull in pulls:
        state = pull.get("state") if isinstance(pull, dict) else None
        if state not in ("OPEN", "MERGED", "CLOSED"):
            raise QueryError(f"gh pr list returned an unexpected pull request state: {state!r}")
    if any(pull["state"] == "OPEN" for pull in pulls):
        return "OPEN"
    expected = repository.casefold()
    same_repository = False
    matched: set[str] = set()
    for pull in pulls:
        state = pull["state"]
        if _head_repository(pull) != expected:
            continue
        same_repository = True
        if pull.get("headRefOid") == head_sha and upstream_sha in (None, head_sha):
            matched.add(state)
    if "MERGED" in matched:
        return "MERGED"
    if in_base is not None and upstream_sha in (None, head_sha):
        for pull in pulls:
            number, pull_head = pull.get("number"), pull.get("headRefOid")
            if (
                pull["state"] == "MERGED"
                and _head_repository(pull) == expected
                and isinstance(number, int)
                and isinstance(pull_head, str)
                and only_base_merged_after(head_sha, pull_head, pull_commits(repository, number, runner), in_base)
            ):
                return "MERGED"
    if "CLOSED" in matched:
        return "CLOSED"
    return "UNMATCHED" if same_repository else "NONE"

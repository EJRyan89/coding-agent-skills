"""Classify a branch by the pull requests that match its current work, failing closed on any query problem."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from git_client import GitClient, GitError, GitResult
from github_client import GitHubClient, GitHubError, subprocess_runner
from github_client import Runner as GhRunner

LIMIT = 1000
FIELDS = "number,state,headRefOid,headRepository,headRepositoryOwner"
# GitHub's pull request commits endpoint lists at most this many commits, so a list this long may be truncated.
COMMIT_LIMIT = 250
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# GitHubError kinds meaning gh itself did not run to completion: missing, unable to start, or out of time.
RUN_FAILURES = frozenset({"prerequisite", "execution", "timeout"})

InBase = Callable[[str], bool]


class QueryError(Exception):
    pass


def _head_repository(pull: dict[str, Any]) -> str | None:
    owner, repository = pull.get("headRepositoryOwner"), pull.get("headRepository")
    if not isinstance(owner, dict) or not isinstance(repository, dict):
        return None
    login, name = owner.get("login"), repository.get("name")
    if not isinstance(login, str) or not isinstance(name, str):
        return None
    return f"{login}/{name}".casefold()


def _git_output(git: GitClient, repository_root: str, arguments: list[str]) -> GitResult:
    try:
        return git.run(arguments, directory=repository_root)
    except GitError as exc:
        raise QueryError(f"could not run git: {exc}") from exc


def branch_tips(repository_root: str, branch: str, git: GitClient | None = None) -> tuple[str, str | None]:
    """Return the branch's local tip and its fetched upstream tip, or None when it has no existing upstream."""
    git = git or GitClient()
    local = _git_output(git, repository_root, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"])
    head = local.stdout.strip()
    if local.returncode != 0 or not SHA_PATTERN.fullmatch(head):
        raise QueryError(f"cannot resolve local branch {branch!r}")
    configured = _git_output(
        git, repository_root, ["for-each-ref", "--format=%(upstream)|%(upstream:track)", f"refs/heads/{branch}"]
    )
    if configured.returncode != 0 or "|" not in configured.stdout:
        raise QueryError(f"cannot read the upstream of {branch!r}: {(configured.stderr or '').strip()}")
    upstream_ref, track = configured.stdout.strip().split("|", 1)
    if not upstream_ref or track == "[gone]":
        return head, None
    upstream = _git_output(git, repository_root, ["rev-parse", "--verify", "--quiet", f"{upstream_ref}^{{commit}}"])
    upstream_sha = upstream.stdout.strip()
    if upstream.returncode != 0 or not SHA_PATTERN.fullmatch(upstream_sha):
        raise QueryError(f"cannot resolve upstream {upstream_ref!r} of {branch!r}")
    return head, upstream_sha


def base_contains(repository_root: str, base_ref: str, git: GitClient | None = None) -> InBase:
    """A test of whether a commit is reachable from base_ref; a commit missing locally is not."""
    client = git or GitClient()

    def contains(sha: str) -> bool:
        present = _git_output(client, repository_root, ["cat-file", "-e", f"{sha}^{{commit}}"])
        if present.returncode != 0:
            return False
        result = _git_output(client, repository_root, ["merge-base", "--is-ancestor", sha, base_ref])
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


def _gh_json(runner: GhRunner, arguments: list[str], failure: str) -> Any:
    """Run gh through the shared client, which retries rate limits, and return its JSON; any failure, output that is
    not JSON included, is a QueryError."""
    try:
        return GitHubClient(runner).json(arguments)
    except GitHubError as exc:
        if exc.kind in RUN_FAILURES:
            raise QueryError(f"could not run gh: {exc}") from exc
        # A refusal the client makes itself, such as a rate limit asking for too long a wait, has no exit status.
        status = "" if exc.returncode is None else f" ({exc.returncode})"
        raise QueryError(f"{failure}{status}: {exc}") from exc
    except OSError as exc:
        raise QueryError(f"could not run gh: {exc}") from exc


def pull_commits(repository: str, number: int, runner: GhRunner = subprocess_runner) -> dict[str, list[str]]:
    """Map each commit of the pull request to its parents, failing closed on any query problem."""
    pages = _gh_json(
        runner,
        ["api", "--paginate", "--slurp", f"repos/{repository}/pulls/{number}/commits?per_page=100"],
        f"gh api for the commits of pull request {number} failed",
    )
    # --slurp wraps the pages in one array, so the output is a list of pages, each a list of commits.
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        raise QueryError(f"gh api returned malformed commits for pull request {number}: {json.dumps(pages)[:200]}")
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
    runner: GhRunner = subprocess_runner,
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
    _check_identities(repository, head_sha, upstream_sha)
    pulls = _list_pulls(repository, branch, runner)
    if any(pull["state"] == "OPEN" for pull in pulls):
        return "OPEN"
    same_repository, matched = _matched_states(pulls, repository.casefold(), head_sha, upstream_sha)
    if "MERGED" in matched:
        return "MERGED"
    if (
        in_base is not None
        and upstream_sha in (None, head_sha)
        and _merged_after_update(pulls, repository, head_sha, runner, in_base)
    ):
        return "MERGED"
    if "CLOSED" in matched:
        return "CLOSED"
    return "UNMATCHED" if same_repository else "NONE"


def _check_identities(repository: str, head_sha: str, upstream_sha: str | None) -> None:
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise QueryError(f"repository must be owner/name, got {repository!r}")
    for label, sha in (("head", head_sha), ("upstream", upstream_sha)):
        if sha is not None and not SHA_PATTERN.fullmatch(sha):
            raise QueryError(f"{label} SHA must be a full lowercase commit SHA, got {sha!r}")


def _list_pulls(repository: str, branch: str, runner: GhRunner) -> list[dict[str, Any]]:
    """Every pull request with this head name, each with a known state, failing closed on any query problem."""
    arguments = ["pr", "list", "--repo", repository, "--head", branch, "--state", "all", "--json", FIELDS]
    pulls = _gh_json(runner, [*arguments, "--limit", str(LIMIT)], "gh pr list failed")
    if not isinstance(pulls, list):
        raise QueryError("gh pr list did not return a list")
    if len(pulls) >= LIMIT:
        raise QueryError(f"branch matches {LIMIT} or more pull requests; the result may be incomplete")
    for pull in pulls:
        state = pull.get("state") if isinstance(pull, dict) else None
        if state not in ("OPEN", "MERGED", "CLOSED"):
            raise QueryError(f"gh pr list returned an unexpected pull request state: {state!r}")
    return pulls


def _matched_states(
    pulls: list[dict[str, Any]], expected: str, head_sha: str, upstream_sha: str | None
) -> tuple[bool, set[str]]:
    """Whether any pull request is from this repository, and the states of those at exactly the branch's tip."""
    same_repository = False
    matched: set[str] = set()
    for pull in pulls:
        state = pull["state"]
        if _head_repository(pull) != expected:
            continue
        same_repository = True
        if pull.get("headRefOid") == head_sha and upstream_sha in (None, head_sha):
            matched.add(state)
    return same_repository, matched


def _merged_after_update(
    pulls: list[dict[str, Any]], repository: str, head_sha: str, runner: GhRunner, in_base: InBase
) -> bool:
    """Whether a merged same-repository pull request only merged the base after the tip, read in listing order."""
    expected = repository.casefold()
    for pull in pulls:
        number, pull_head = pull.get("number"), pull.get("headRefOid")
        if (
            pull["state"] == "MERGED"
            and _head_repository(pull) == expected
            and isinstance(number, int)
            and isinstance(pull_head, str)
            and only_base_merged_after(head_sha, pull_head, pull_commits(repository, number, runner), in_base)
        ):
            return True
    return False

"""Classify a branch by the pull requests that match its current work, failing closed on any query problem."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from git_client import GitClient, GitError, GitResult
from github_client import GitHubClient, GitHubError, subprocess_runner
from github_client import Runner as GhRunner

LIMIT = 1000
FIELDS = "number,state,headRefOid,headRepository,headRepositoryOwner,mergeCommit,baseRefName"
# GitHub's pull request commits endpoint lists at most this many commits, so a list this long may be truncated.
COMMIT_LIMIT = 250
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# GitHubError kinds meaning gh itself did not run to completion: missing, unable to start, or out of time.
RUN_FAILURES = frozenset({"prerequisite", "execution", "timeout"})
# A branch name git accepts in a reference, apart from the sequences `..` and `@{`, which _usable_base_name refuses.
BASE_NAME_PATTERN = re.compile(r"[^\s\x00-\x1f\x7f~^:?*\[\\]+")
# The change the content proof compares: git's plumbing lists each changed file with its mode and object name before
# and after, which name the file's whole content, and writes no patch, so no context, diff setting, external program,
# or environment variable such as GIT_DIFF_OPTS shapes it; -z names each file as itself, unquoted.
CHANGE_ARGUMENTS = ("diff-tree", "-r", "-z", "--raw", "--no-abbrev", "--no-renames", "--ignore-submodules=none")
# One file's entry in that listing, before its name: both modes, both object names, and the kind of change.
CHANGE_ENTRY = re.compile(rb":[0-7]{6} [0-7]{6} [0-9a-f]{40,64} [0-9a-f]{40,64} [A-Z]")

InBase = Callable[[str], bool]
# Whether a branch tip's whole change since it left the base is the change a merge commit made.
SameChange = Callable[[str, str], bool]


@dataclass(frozen=True)
class BaseTests:
    """The proofs one fetched base branch gives: whether it contains a commit, and whether a tip's whole change
    since it left the base is the change a merge commit made."""

    contains: InBase
    same_change: SameChange


# The tests of a base branch by its name, or None for a base not fetched locally, which proves nothing.
Bases = Callable[[str], BaseTests | None]


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


def _commit_present(git: GitClient, repository_root: str, sha: str) -> bool:
    return _git_output(git, repository_root, ["cat-file", "-e", f"{sha}^{{commit}}"]).returncode == 0


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
        if not _commit_present(client, repository_root, sha):
            return False
        result = _git_output(client, repository_root, ["merge-base", "--is-ancestor", sha, base_ref])
        if result.returncode not in (0, 1):
            raise QueryError(f"cannot test whether {sha} is in {base_ref}: {(result.stderr or '').strip()}")
        return result.returncode == 0

    return contains


Change = frozenset[tuple[bytes, bytes]]


def change_identity(listing: bytes) -> Change | None:
    """Each file a `CHANGE_ARGUMENTS` listing names, with its modes and object names before and after, or None for an
    empty listing; a listing in any other shape is a QueryError.

    The object names stand for each file's whole content, so two changes are the same only when every file they touch
    held the same bytes before and holds the same bytes after, whitespace included; the order of the files is left
    out.
    """
    if not listing:
        return None
    fields = listing.split(b"\0")
    if fields[-1] or len(fields) % 2 == 0:
        raise QueryError(f"git diff-tree listed a change in an unexpected shape: {listing[:200]!r}")
    entries = list(zip(fields[:-1:2], fields[1:-1:2], strict=True))
    if not all(CHANGE_ENTRY.fullmatch(entry) and path for entry, path in entries):
        raise QueryError(f"git diff-tree listed a change in an unexpected shape: {listing[:200]!r}")
    return frozenset(entries)


def change_match(repository_root: str, base_ref: str, git: GitClient | None = None) -> SameChange:
    """A test of whether a branch tip's whole change since it left base_ref is the change a merge commit made.

    Compares the change from the merge base of base_ref and the tip to the tip with the change from the merge
    commit's first parent to the merge commit, so a tip amended or rebased after its pull request merged still
    matches the squash commit. Each change is every file it touches with that file's whole content before and after,
    so a base that moved in other files before the merge still matches, while every byte of a touched file counts:
    whitespace, line endings, and where in the file a change sits. The same edit made at another place, however
    alike the lines around it, is another change, and so is a touched file the base changed elsewhere before the
    merge, which keeps the branch rather than guess. A tip sharing no history with base_ref, a merge commit missing
    locally, or an empty change on either side is no match; a merge base or diff that fails for any other reason,
    such as a base_ref that does not resolve, is a QueryError. The tip's change is read once, however many merge
    commits it is compared with.
    """
    client = git or GitClient()
    tips: dict[str, Change | None] = {}

    def change(start: str, end: str) -> Change | None:
        diff = _git_output(client, repository_root, [*CHANGE_ARGUMENTS, start, end])
        if diff.returncode != 0:
            raise QueryError(f"cannot diff {start} {end}: {(diff.stderr or '').strip()}")
        return change_identity(diff.output_bytes())

    def tip_change(tip: str) -> Change | None:
        found = _git_output(client, repository_root, ["merge-base", base_ref, tip])
        if found.returncode == 1:
            return None  # no merge base: the tip never left base_ref, so no change of it is a change since then
        fork_point = found.stdout.strip()
        if found.returncode != 0 or not SHA_PATTERN.fullmatch(fork_point):
            raise QueryError(f"cannot find the merge base of {base_ref} and {tip}: {(found.stderr or '').strip()}")
        return change(fork_point, tip)

    def same_change(tip: str, merge_commit: str) -> bool:
        if tip not in tips:
            tips[tip] = tip_change(tip)
        if tips[tip] is None or not _commit_present(client, repository_root, merge_commit):
            return False
        return tips[tip] == change(f"{merge_commit}^", merge_commit)

    return same_change


def fetched_bases(repository_root: str, git: GitClient | None = None) -> Bases:
    """The tests each fetched base branch gives, by name, built once per base from `refs/remotes/origin/<name>`.

    A name origin has no fetched branch for, or that resolves to no commit, gives None: such a base proves nothing.
    """
    client = git or GitClient()
    known: dict[str, BaseTests | None] = {}

    def tests(base: str) -> BaseTests | None:
        if base not in known:
            ref = f"refs/remotes/origin/{base}"
            resolved = _git_output(client, repository_root, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
            fetched = resolved.returncode == 0 and SHA_PATTERN.fullmatch(resolved.stdout.strip()) is not None
            known[base] = (
                BaseTests(base_contains(repository_root, ref, client), change_match(repository_root, ref, client))
                if fetched
                else None
            )
        return known[base]

    return tests


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
    bases: Bases | None = None,
    default_base: str | None = None,
) -> str:
    """Return the branch state.

    OPEN: any pull request with this head name is open.
    MERGED or CLOSED: a same-repository pull request was merged or closed at exactly the branch's current tip, and the
    fetched upstream, when it exists, has no newer work: no other commit, or none the pull request's base does not
    already hold. Given bases, each merged same-repository pull request is judged against its own base branch, the
    `baseRefName` gh lists, or default_base when that is not a usable name; a base not fetched locally proves
    nothing. Such a pull request also proves the branch MERGED when the tip is one of its commits and every commit
    after the tip is a merge that only brought in commits reachable from the base, as GitHub's "Update branch"
    makes, or when its merge commit made the branch's whole change since the branch left the base, as a tip amended
    or rebased after the pull request opened keeps it.
    UNMATCHED: same-repository pull requests exist, but none describes the branch's current work.
    NONE: no same-repository pull request uses this head name.
    """
    _check_identities(repository, head_sha, upstream_sha)
    pulls = _list_pulls(repository, branch, runner)
    if any(pull["state"] == "OPEN" for pull in pulls):
        return "OPEN"
    expected = repository.casefold()
    own = [pull for pull in pulls if _head_repository(pull) == expected]
    if not own:
        return "NONE"
    settled = [pull for pull in own if not _newer_upstream_work(pull, head_sha, upstream_sha, bases, default_base)]
    exact = {pull["state"] for pull in settled if pull.get("headRefOid") == head_sha}
    if "MERGED" in exact:
        return "MERGED"
    merged = [pull for pull in settled if pull["state"] == "MERGED"]
    if bases is not None and _proven_merged(merged, repository, head_sha, runner, bases, default_base):
        return "MERGED"
    if "CLOSED" in exact:
        return "CLOSED"
    return "UNMATCHED"


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


def _usable_base_name(name: Any) -> bool:
    """Whether gh listed a base name git could hold as a branch, so `refs/remotes/origin/<name>` names its fetch."""
    return (
        isinstance(name, str)
        and BASE_NAME_PATTERN.fullmatch(name) is not None
        and ".." not in name
        and "@{" not in name
    )


def _base_tests(pull: dict[str, Any], bases: Bases | None, default_base: str | None) -> BaseTests | None:
    """The tests of the pull request's base: its `baseRefName` when that is a usable name, else default_base; None
    without bases, without a name, or for a base not fetched locally."""
    if bases is None:
        return None
    name = pull.get("baseRefName")
    if not _usable_base_name(name):
        name = default_base
    return bases(name) if name else None


def _newer_upstream_work(
    pull: dict[str, Any], head_sha: str, upstream_sha: str | None, bases: Bases | None, default_base: str | None
) -> bool:
    """Whether the fetched upstream holds work the pull request cannot describe: a commit other than the tip that the
    pull request's base does not already hold, as a branch tracking its base has none."""
    if upstream_sha in (None, head_sha):
        return False
    tests = _base_tests(pull, bases, default_base)
    return tests is None or not tests.contains(upstream_sha)


def _proven_merged(
    merged: list[dict[str, Any]],
    repository: str,
    head_sha: str,
    runner: GhRunner,
    bases: Bases,
    default_base: str | None,
) -> bool:
    """Whether one of the merged same-repository pull requests proves the branch merged without ending at its tip,
    each judged against its own base: by its commits, and then by its merge commit's change."""
    judged = [(pull, tests) for pull in merged if (tests := _base_tests(pull, bases, default_base)) is not None]
    if _merged_after_update(judged, repository, head_sha, runner):
        return True
    return _merged_by_change(judged, head_sha)


def _merged_after_update(
    judged: list[tuple[dict[str, Any], BaseTests]], repository: str, head_sha: str, runner: GhRunner
) -> bool:
    """Whether a merged pull request only merged its base after the tip, read in listing order."""
    for pull, tests in judged:
        number, pull_head = pull.get("number"), pull.get("headRefOid")
        if (
            isinstance(number, int)
            and isinstance(pull_head, str)
            and only_base_merged_after(head_sha, pull_head, pull_commits(repository, number, runner), tests.contains)
        ):
            return True
    return False


def _merge_commit(pull: dict[str, Any]) -> str | None:
    """The pull request's merge commit SHA, or None unless gh listed one as a full lowercase SHA."""
    merge = pull.get("mergeCommit")
    oid = merge.get("oid") if isinstance(merge, dict) else None
    return oid if isinstance(oid, str) and SHA_PATTERN.fullmatch(oid) else None


def _merged_by_change(judged: list[tuple[dict[str, Any], BaseTests]], head_sha: str) -> bool:
    """Whether a merged pull request's merge commit made the branch's whole change since it left that pull request's
    base, in listing order."""
    for pull, tests in judged:
        merge_commit = _merge_commit(pull)
        if merge_commit is not None and tests.same_change(head_sha, merge_commit):
            return True
    return False

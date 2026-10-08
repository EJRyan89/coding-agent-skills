"""Decide whether a pull request's contribution changed since an earlier head commit."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TypeVar, cast
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "code-review-core" / "scripts"))

from review_github import GitHubClient, GitHubError
from review_io import NETWORK_WORKERS, map_in_order

UNCHANGED = "unchanged"
CHANGED = "changed"
UNKNOWN = "unknown"

# GitHub's compare API lists at most this many changed files, so a list that long may be incomplete.
COMPARE_FILE_LIMIT = 300
# Failures that invalidate the whole run rather than one pull request's evidence.
FATAL_ERROR_KINDS = {"prerequisite", "execution", "authentication", "rate_limit"}

Fingerprint = tuple[tuple[str, str, str, str, str], ...]
# A fingerprint without its modes: each changed file's path, status, previous path, and blob ID.
ChangedFiles = tuple[tuple[str, str, str, str], ...]
# What `detect` is asked: a repository, its base branch, and the earlier and later head commits.
Query = tuple[str, str, str, str]
Key = TypeVar("Key")
Cached = TypeVar("Cached")


def tree_modes(tree: object) -> dict[str, str] | None:
    """Map every path in a recursive Git tree to its mode; None when the tree may be incomplete."""
    if not isinstance(tree, dict) or tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
        return None
    modes: dict[str, str] = {}
    for entry in tree["tree"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("mode"), str)
        ):
            return None
        modes[entry["path"]] = entry["mode"]
    return modes


def changed_files(comparison: object) -> ChangedFiles | None:
    """Every file a comparison changes, as its path, status, previous path, and blob ID, sorted.

    Returns None when the comparison is malformed or may be truncated.
    """
    if not isinstance(comparison, dict) or not isinstance(comparison.get("files"), list):
        return None
    files = comparison["files"]
    if len(files) >= COMPARE_FILE_LIMIT:
        return None
    entries: list[tuple[str, str, str, str]] = []
    for entry in files:
        if not isinstance(entry, dict):
            return None
        filename, status, content = entry.get("filename"), entry.get("status"), entry.get("sha")
        previous = entry.get("previous_filename") or ""
        if not (
            isinstance(filename, str)
            and filename
            and isinstance(status, str)
            and status
            and isinstance(content, str)
            and content
            and isinstance(previous, str)
        ):
            return None
        entries.append((filename, status, previous, content))
    return tuple(sorted(entries))


def with_modes(files: ChangedFiles | None, modes: dict[str, str] | None) -> Fingerprint | None:
    """The changed files with the tree mode each leaves behind, empty for a removed file; None when the files are
    unknown or the tree may be incomplete or lacks one of them."""
    if files is None or modes is None:
        return None
    entries: list[tuple[str, str, str, str, str]] = []
    for filename, status, previous, content in files:
        if status == "removed":
            mode = ""
        elif filename in modes:
            mode = modes[filename]
        else:
            return None
        entries.append((filename, status, previous, content, mode))
    return tuple(entries)


def contribution_fingerprint(comparison: object, modes: dict[str, str] | None) -> Fingerprint | None:
    """Identify every file a comparison changes and the exact tree entry it leaves behind.

    The blob ID covers a file's bytes and the tree mode covers its executable,
    symlink, or submodule type, so two commits with the same fingerprint leave
    every changed file identical and a review of one applies to the other.
    Returns None when the comparison or tree is malformed or may be truncated.
    """
    return with_modes(changed_files(comparison), modes)


def needs_modes(before: ChangedFiles, after: ChangedFiles) -> bool:
    """Whether only the trees' modes can tell two commits' fingerprints apart.

    A fingerprint is the changed files with a mode appended to each, in the same order, so two commits whose changed
    files differ have different fingerprints whatever their modes are. Only matching files, at least one of them not
    removed, need the trees.
    """
    return before == after and any(status != "removed" for _, status, _, _ in before)


def at_or_before(client: GitHubClient, repository: str, earlier: str, later: str) -> bool | None:
    """Whether commit `earlier` is `later` or one of its ancestors; None when GitHub cannot say, such as for a commit
    a force-push removed. Failures that invalidate the whole run are raised."""
    try:
        comparison = client.api_json(f"repos/{repository}/compare/{earlier}...{later}?per_page=1")
    except GitHubError as exc:
        if exc.kind in FATAL_ERROR_KINDS:
            raise
        return None
    status = comparison.get("status") if isinstance(comparison, dict) else None
    return (
        {"identical": True, "ahead": True, "behind": False, "diverged": False}.get(status)
        if isinstance(status, str)
        else None
    )


class ChangeDetector:
    """Answers `detect` from GitHub's comparison of each commit with the base branch, reading a commit's tree only
    when `needs_modes` says the comparisons alone cannot decide, and each comparison and tree at most once.

    `prefetch` reads what a batch of queries needs, `workers` calls at a time, all through the one client, so its
    rate-limit backoff governs every call. `detect` reads anything still missing one call at a time.
    """

    def __init__(self, client: GitHubClient, *, workers: int = NETWORK_WORKERS) -> None:
        self.client = client
        self.workers = workers
        self._files: dict[tuple[str, str, str], ChangedFiles | None] = {}
        self._modes: dict[tuple[str, str], dict[str, str] | None] = {}

    def prefetch(self, queries: Iterable[Query]) -> None:
        """Read every comparison, and then every tree, that detecting `queries` needs."""
        pending = [query for query in queries if query[2] != query[3]]
        self._gather(
            self._files,
            [(repository, base, sha) for repository, base, since, head in pending for sha in (since, head)],
            self._read_files,
        )
        self._gather(
            self._modes,
            [
                (repository, sha)
                for repository, base, since, head in pending
                if self._comparable(repository, base, since, head)
                for sha in (since, head)
            ],
            self._read_modes,
        )

    def detect(
        self,
        repository: str,
        number: int,
        base_ref: str,
        since_sha: str,
        head_sha: str,
    ) -> str:
        if since_sha == head_sha:
            return UNCHANGED
        before = self._cached(self._files, (repository, base_ref, since_sha), self._read_files)
        after = self._cached(self._files, (repository, base_ref, head_sha), self._read_files)
        # Two empty contributions mean the commits already reached the base branch,
        # which proves nothing about whether the pull request changed.
        if before is None or after is None or (not before and not after):
            return UNKNOWN
        if not needs_modes(before, after):
            return UNCHANGED if before == after else CHANGED
        before_modes = with_modes(before, self._cached(self._modes, (repository, since_sha), self._read_modes))
        after_modes = with_modes(after, self._cached(self._modes, (repository, head_sha), self._read_modes))
        if before_modes is None or after_modes is None:
            return UNKNOWN
        return UNCHANGED if before_modes == after_modes else CHANGED

    def _comparable(self, repository: str, base_ref: str, since_sha: str, head_sha: str) -> bool:
        before = self._files[(repository, base_ref, since_sha)]
        after = self._files[(repository, base_ref, head_sha)]
        return before is not None and after is not None and needs_modes(before, after)

    def _gather(self, cache: dict[Key, Cached], keys: list[Key], read: Callable[[Key], Cached]) -> None:
        missing = list(dict.fromkeys(key for key in keys if key not in cache))
        # Nothing is caught, so every outcome carries its value and any failure is raised here.
        outcomes = map_in_order(read, missing, workers=self.workers)
        cache.update((key, cast(Cached, value)) for key, (value, _) in zip(missing, outcomes, strict=True))

    @staticmethod
    def _cached(cache: dict[Key, Cached], key: Key, read: Callable[[Key], Cached]) -> Cached:
        if key not in cache:
            cache[key] = read(key)
        return cache[key]

    def _read_files(self, key: tuple[str, str, str]) -> ChangedFiles | None:
        repository, base_ref, sha = key
        return changed_files(self._evidence(f"repos/{repository}/compare/{quote(base_ref, safe='/')}...{sha}"))

    def _read_modes(self, key: tuple[str, str]) -> dict[str, str] | None:
        repository, sha = key
        return tree_modes(self._evidence(f"repos/{repository}/git/trees/{sha}?recursive=1"))

    def _evidence(self, endpoint: str) -> object:
        """GitHub's answer, or None when it has none for this commit; failures that invalidate the run are raised."""
        try:
            return self.client.api_json(endpoint)
        except GitHubError as exc:
            if exc.kind in FATAL_ERROR_KINDS:
                raise
            return None

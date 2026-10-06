"""Decide whether a pull request's contribution changed since an earlier head commit."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote

CORE_SCRIPTS = Path(__file__).resolve().parents[2] / "code-review-core" / "scripts"
sys.path.insert(0, str(CORE_SCRIPTS))

from review_github import GitHubClient, GitHubError

UNCHANGED = "unchanged"
CHANGED = "changed"
UNKNOWN = "unknown"

# GitHub's compare API lists at most this many changed files, so a list that long may be incomplete.
COMPARE_FILE_LIMIT = 300
# Failures that invalidate the whole run rather than one pull request's evidence.
FATAL_ERROR_KINDS = {"prerequisite", "execution", "authentication", "rate_limit"}

Fingerprint = tuple[tuple[str, str, str, str, str], ...]


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


def contribution_fingerprint(comparison: object, modes: dict[str, str] | None) -> Fingerprint | None:
    """Identify every file a comparison changes and the exact tree entry it leaves behind.

    The blob ID covers a file's bytes and the tree mode covers its executable,
    symlink, or submodule type, so two commits with the same fingerprint leave
    every changed file identical and a review of one applies to the other.
    Returns None when the comparison or tree is malformed or may be truncated.
    """
    if modes is None or not isinstance(comparison, dict) or not isinstance(comparison.get("files"), list):
        return None
    files = comparison["files"]
    if len(files) >= COMPARE_FILE_LIMIT:
        return None
    entries = []
    for entry in files:
        if not isinstance(entry, dict):
            return None
        filename, status, content = entry.get("filename"), entry.get("status"), entry.get("sha")
        previous = entry.get("previous_filename") or ""
        if not all(isinstance(value, str) and value for value in (filename, status, content)) or not isinstance(
            previous, str
        ):
            return None
        if status == "removed":
            mode = ""
        elif filename in modes:
            mode = modes[filename]
        else:
            return None
        entries.append((filename, status, previous, content, mode))
    return tuple(sorted(entries))


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
    def __init__(self, client: GitHubClient) -> None:
        self.client = client
        self._fingerprints: dict[tuple[str, str, str], Fingerprint | None] = {}

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
        before = self._fingerprint(repository, base_ref, since_sha)
        after = self._fingerprint(repository, base_ref, head_sha)
        # Two empty contributions mean the commits already reached the base branch,
        # which proves nothing about whether the pull request changed.
        if before is None or after is None or (not before and not after):
            return UNKNOWN
        return UNCHANGED if before == after else CHANGED

    def _fingerprint(self, repository: str, base_ref: str, sha: str) -> Fingerprint | None:
        key = (repository, base_ref, sha)
        if key not in self._fingerprints:
            try:
                comparison = self.client.api_json(f"repos/{repository}/compare/{quote(base_ref, safe='/')}...{sha}")
                modes = tree_modes(self.client.api_json(f"repos/{repository}/git/trees/{sha}?recursive=1"))
                self._fingerprints[key] = contribution_fingerprint(comparison, modes)
            except GitHubError as exc:
                if exc.kind in FATAL_ERROR_KINDS:
                    raise
                self._fingerprints[key] = None
        return self._fingerprints[key]

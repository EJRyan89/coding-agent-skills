"""The malformed-input suite's known failures: each names the open issue that fixes it, and the list only shrinks.

tests/malformed_input/test_malformed_input.py holds every reader of a file or record to every corpus case of its
format, and a reader that fails a case today is listed in tests/malformed_input/known_failures.json with the issue
that fixes it, never marked as an expected failure. The suite fails a listed case that now passes, so the fix removes
its entry, and an unlisted case that fails, so the list never grows in silence. This module holds the list itself: each
entry names its reader, its case, and its issue, once, in order. A pull request that closes an issue an entry still
names is refused, in every form GitHub closes it by, since the issue would close while its case still fails; the pull
request that fixes the reader removes the entry with it. A pull request check also asks GitHub, through gh, that each
issue an entry names is open, and asks nothing while the list is empty; a gh that cannot answer fails the check.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import json_reader
from closing_references import closing_references
from github_client import GitHubClient, GitHubError, subprocess_runner
from github_remote import github_repository
from markdown_sections import HTML_COMMENT
from validation_support import REPOSITORY_ROOT, PullRequestChecks

from tools.release_notes import ReleaseNotesError, git_output

KNOWN_FAILURES = Path("tests") / "malformed_input" / "known_failures.json"
ENTRY_KEYS = ("reader", "case", "issue")
# The state gh reports for an open issue.
OPEN = "OPEN"


@dataclass(frozen=True, order=True)
class KnownFailure:
    """A reader that fails a corpus case today, and the open issue that fixes it."""

    reader: str
    case: str
    issue: int


class KnownFailuresUnreadable(ValueError):
    """The list is not one this module reads; the message names what is wrong."""


def _entry(index: int, raw: object) -> KnownFailure:
    if not isinstance(raw, dict) or sorted(raw) != sorted(ENTRY_KEYS):
        raise KnownFailuresUnreadable(f"entry {index} must be an object with exactly the keys {', '.join(ENTRY_KEYS)}")
    reader, case, issue = raw["reader"], raw["case"], raw["issue"]
    if not isinstance(reader, str) or not reader or not isinstance(case, str) or not case:
        raise KnownFailuresUnreadable(f"entry {index} must name its reader and its case as non-empty strings")
    if not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0:
        raise KnownFailuresUnreadable(f"entry {index} ({reader}, {case}) must name its issue by a positive number")
    return KnownFailure(reader, case, issue)


def read_known_failures(path: Path) -> list[KnownFailure]:
    """The list's entries, in its order, or KnownFailuresUnreadable naming why it cannot be read."""
    try:
        raw = json_reader.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise KnownFailuresUnreadable(f"cannot be read: {error}") from error
    except ValueError as error:
        raise KnownFailuresUnreadable(f"is not JSON this module reads: {type(error).__name__}") from error
    if not isinstance(raw, list):
        raise KnownFailuresUnreadable("must be a list of entries")
    return [_entry(index, entry) for index, entry in enumerate(raw, start=1)]


def known_failure_problems(root: Path) -> list[str]:
    """Each way the list breaks its shape: unreadable, an entry twice, or out of order."""
    name = KNOWN_FAILURES.as_posix()
    try:
        entries = read_known_failures(root / KNOWN_FAILURES)
    except KnownFailuresUnreadable as error:
        return [f"{name} {error}"]
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if (entry.reader, entry.case) in seen:
            problems.append(f"{name} lists {entry.reader} on case {entry.case} more than once")
        seen.add((entry.reader, entry.case))
    if entries != sorted(entries):
        problems.append(f"{name} must list its entries by reader, then case, then issue")
    return problems


def closed_issues(body: str, repository: str | None) -> set[int]:
    """The issues of `repository`, `owner/name`, a pull request body closes when it merges, in every form GitHub
    reads, outside an HTML comment; with no repository known, every reference counts, so none is missed. A number
    past the digit limit, or in digits other than ASCII, names no issue."""
    return {
        reference.number
        for reference in closing_references(HTML_COMMENT.sub("", body))
        if repository is None or reference.in_repository(repository)
    }


def checkout_repository(root: Path) -> str | None:
    """The GitHub repository the checkout's origin names, or None when git cannot say or it names none."""
    try:
        return github_repository(git_output(root, "remote", "get-url", "origin"))
    except ReleaseNotesError:
        return None


def closed_known_failure_problems(root: Path, body: str | None) -> list[str]:
    """Each issue the body closes that a known failure still names, with the case that would go on failing."""
    if body is None:
        return []
    try:
        entries = read_known_failures(root / KNOWN_FAILURES)
    except KnownFailuresUnreadable:
        # known_failure_problems names the unreadable list; this check has nothing to compare.
        return []
    if not entries:
        return []
    closing = closed_issues(body, checkout_repository(root))
    return [
        f"the pull request closes #{entry.issue}, but {KNOWN_FAILURES.as_posix()} still lists {entry.reader} failing "
        f"case {entry.case} under it; fix the reader and remove the entry, or do not close the issue"
        for entry in entries
        if entry.issue in closing
    ]


def open_issue_problems(root: Path, body: str | None, client: GitHubClient | None = None) -> list[str]:
    """Each issue a known failure names that GitHub does not say is open, in a pull request check; none while the
    list is empty, which asks GitHub nothing. A gh that cannot answer, or a checkout whose origin names no GitHub
    repository, is a problem, never an open issue."""
    if body is None:
        return []
    try:
        entries = read_known_failures(root / KNOWN_FAILURES)
    except KnownFailuresUnreadable:
        # known_failure_problems names the unreadable list; this check has nothing to ask about.
        return []
    if not entries:
        return []
    repository = checkout_repository(root)
    if repository is None:
        return [
            f"{KNOWN_FAILURES.as_posix()} names issues, but the checkout's origin names no GitHub repository, so the "
            "known-failure check cannot ask whether they are open"
        ]
    client = client or GitHubClient(runner=partial(subprocess_runner, cwd=root))
    problems: list[str] = []
    for issue in sorted({entry.issue for entry in entries}):
        try:
            answer = client.json(["issue", "view", str(issue), "--repo", repository, "--json", "state"])
        except GitHubError as error:
            problems.append(f"the known-failure check cannot ask GitHub whether #{issue} is open: {error}")
            continue
        state = answer.get("state") if isinstance(answer, dict) else None
        if state != OPEN:
            problems.append(
                f"{KNOWN_FAILURES.as_posix()} names #{issue}, which GitHub reports as {state}, not {OPEN}; a known "
                "failure names the open issue that fixes it"
            )
    return problems


class KnownFailurePolicies(unittest.TestCase):
    def test_the_known_failure_list_names_each_case_once_in_order_with_its_issue(self) -> None:
        self.assertEqual([], known_failure_problems(REPOSITORY_ROOT))


class KnownFailurePullRequestPolicies(PullRequestChecks):
    def test_the_pull_request_closes_no_issue_a_known_failure_still_names(self) -> None:
        self.assertEqual([], closed_known_failure_problems(REPOSITORY_ROOT, self.pull_request.body_text()))

    def test_each_issue_a_known_failure_names_is_open(self) -> None:
        self.assertEqual([], open_issue_problems(REPOSITORY_ROOT, self.pull_request.body_text()))

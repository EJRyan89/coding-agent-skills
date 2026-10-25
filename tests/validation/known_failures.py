"""The malformed-input suite's known failures: each names the open issue that fixes it, and the list only shrinks.

tests/malformed_input/test_malformed_input.py holds every reader of a file or record to every corpus case of its
format, and a reader that fails a case today is listed in tests/malformed_input/known_failures.json with the issue
that fixes it, never marked as an expected failure. The suite fails a listed case that now passes, so the fix removes
its entry, and an unlisted case that fails, so the list never grows in silence. This module holds the list itself: each
entry names its reader, its case, and its issue, once, in order. A pull request that closes an issue an entry still
names is refused, since the issue would close while its case still fails; the pull request that fixes the reader
removes the entry with it.
"""

from __future__ import annotations

import re
import unittest
from dataclasses import dataclass
from pathlib import Path

import json_reader
import whole_number
from markdown_sections import HTML_COMMENT
from validation_support import REPOSITORY_ROOT, PullRequestChecks

KNOWN_FAILURES = Path("tests") / "malformed_input" / "known_failures.json"
ENTRY_KEYS = ("reader", "case", "issue")
# A line GitHub reads as closing an issue when the pull request merges: a closing keyword, an optional colon, and the
# issue by its number, anywhere in the body outside an HTML comment.
CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?[ \t]+#(\d+)\b", re.IGNORECASE)


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


def closed_issues(body: str) -> set[int]:
    """The issues a pull request body closes when it merges; a number past the digit limit, or in digits other than
    ASCII, names no issue."""
    numbers = (whole_number.parse(number) for number in CLOSING.findall(HTML_COMMENT.sub("", body)))
    return {number for number in numbers if number is not None}


def closed_known_failure_problems(root: Path, body: str | None) -> list[str]:
    """Each issue the body closes that a known failure still names, with the case that would go on failing."""
    if body is None:
        return []
    try:
        entries = read_known_failures(root / KNOWN_FAILURES)
    except KnownFailuresUnreadable:
        # known_failure_problems names the unreadable list; this check has nothing to compare.
        return []
    closing = closed_issues(body)
    return [
        f"the pull request closes #{entry.issue}, but {KNOWN_FAILURES.as_posix()} still lists {entry.reader} failing "
        f"case {entry.case} under it; fix the reader and remove the entry, or do not close the issue"
        for entry in entries
        if entry.issue in closing
    ]


class KnownFailurePolicies(unittest.TestCase):
    def test_the_known_failure_list_names_each_case_once_in_order_with_its_issue(self) -> None:
        self.assertEqual([], known_failure_problems(REPOSITORY_ROOT))


class KnownFailurePullRequestPolicies(PullRequestChecks):
    def test_the_pull_request_closes_no_issue_a_known_failure_still_names(self) -> None:
        self.assertEqual([], closed_known_failure_problems(REPOSITORY_ROOT, self.pull_request.body_text()))

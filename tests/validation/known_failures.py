"""The malformed-input suite's known failures: each names the open issue that fixes it, and the list only shrinks.

tests/malformed_input/test_malformed_input.py holds every reader of a file or record to every corpus case of its
format, and a reader that fails a case today is listed in tests/malformed_input/known_failures.json with the issue
that fixes it, never marked as an expected failure. The suite fails a listed case that now passes, so the fix removes
its entry, and an unlisted case that fails, so the list never grows in silence. This module holds the list itself: each
entry names its reader, its case, and its issue, once, in order. A pull request that closes an issue an entry still
names is refused, in every form GitHub closes it by, in its title or its body, since the issue would close while its
case still fails; the pull request that fixes the reader removes the entry with it. tests/malformed_input/readers.py's
PENDING, each reader no table holds yet with the issue whose pull request adds it, is held to the same two rules.
`tests/run_validation.py --known-failure-issues`, a run of its own that runs no suite, also asks GitHub, through gh,
that each issue either list names is an open issue, never a pull request, and asks nothing while both are empty; a gh
that cannot answer fails it.
"""

from __future__ import annotations

import ast
import unittest
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import json_reader
from closing_references import pull_request_references
from github_client import GitHubClient, GitHubError, subprocess_runner
from github_remote import github_repository
from validation_support import REPOSITORY_ROOT, PullRequestChecks, UnreadablePython, parse_python

from tools.release_notes import ReleaseNotesError, git_output

KNOWN_FAILURES = Path("tests") / "malformed_input" / "known_failures.json"
ENTRY_KEYS = ("reader", "case", "issue")
# The table of readers owed a reader-table entry, each with the issue whose pull request adds it, and its name there.
PENDING_MODULE = Path("tests") / "malformed_input" / "readers.py"
PENDING = "PENDING"
# The state gh reports for an open issue, and what a pull request's URL holds where an issue's holds /issues/.
OPEN = "OPEN"
PULL_PATH = "/pull/"


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


@dataclass(frozen=True)
class OwedIssue:
    """An issue a list names as the one whose pull request ends what it lists: its number, the list, and what the list
    says is owed under it."""

    issue: int
    source: str
    owed: str


def read_pending(path: Path) -> dict[str, int]:
    """The PENDING table a readers module assigns: each reader's name and its issue, read from the syntax tree, never
    run; KnownFailuresUnreadable when the module assigns none, or one that is not a literal dict of names to positive
    issue numbers."""
    try:
        tree = parse_python(path)
    except (OSError, UnreadablePython) as error:
        raise KnownFailuresUnreadable(f"cannot be read: {error}") from error
    values = [
        node.value
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and node.value is not None
        and any(
            isinstance(target, ast.Name) and target.id == PENDING
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    ]
    if len(values) != 1:
        raise KnownFailuresUnreadable(f"must assign {PENDING} once at module level")
    try:
        table = ast.literal_eval(values[0])
    except ValueError as error:
        raise KnownFailuresUnreadable(f"must assign {PENDING} a literal dict") from error
    if not isinstance(table, dict) or not all(
        isinstance(name, str) and isinstance(issue, int) and not isinstance(issue, bool) and issue > 0
        for name, issue in table.items()
    ):
        raise KnownFailuresUnreadable(f"must map each name in {PENDING} to a positive issue number")
    return table


def pending_problems(root: Path) -> list[str]:
    """Why the PENDING table cannot be read, or nothing."""
    try:
        read_pending(root / PENDING_MODULE)
    except KnownFailuresUnreadable as error:
        return [f"{PENDING_MODULE.as_posix()} {error}"]
    return []


def owed_issues(root: Path) -> list[OwedIssue]:
    """Each issue the known-failure list and the PENDING table name, with what each still lists under it; a list that
    cannot be read adds nothing, since known_failure_problems and pending_problems name it."""
    owed: list[OwedIssue] = []
    try:
        entries = read_known_failures(root / KNOWN_FAILURES)
    except KnownFailuresUnreadable:
        entries = []
    owed += [
        OwedIssue(entry.issue, KNOWN_FAILURES.as_posix(), f"lists {entry.reader} failing case {entry.case}")
        for entry in entries
    ]
    try:
        pending = read_pending(root / PENDING_MODULE)
    except KnownFailuresUnreadable:
        pending = {}
    owed += [
        OwedIssue(issue, f"{PENDING_MODULE.as_posix()}'s {PENDING}", f"names {reader} as owed its table entry")
        for reader, issue in sorted(pending.items())
    ]
    return owed


def closed_issues(body: str | None, repository: str | None, title: str | None = None) -> set[int]:
    """The issues of `repository`, `owner/name`, a pull request closes when it is squash-merged, by its title or its
    body, in every form closing_references reads, HTML comments included, since the squash commit carries the body as
    written; with no repository known, every reference counts, so none is missed. A number past the digit limit, or in
    digits other than ASCII, names no issue."""
    return {
        reference.number
        for reference in pull_request_references(title, body)
        if repository is None or reference.in_repository(repository)
    }


def checkout_repository(root: Path) -> str | None:
    """The GitHub repository the checkout's origin names, or None when git cannot say or it names none."""
    try:
        return github_repository(git_output(root, "remote", "get-url", "origin"))
    except ReleaseNotesError:
        return None


def closed_known_failure_problems(root: Path, body: str | None, title: str | None = None) -> list[str]:
    """Each issue the pull request closes, by its title or its body, since the squash commit carries both and GitHub
    closes an issue either names, that a known failure or a PENDING reader still names, with what is still owed."""
    if body is None and title is None:
        return []
    owed = owed_issues(root)
    if not owed:
        return []
    closing = closed_issues(body, checkout_repository(root), title)
    return [
        f"the pull request closes #{item.issue}, but {item.source} still {item.owed} under it; finish that work and "
        "remove the entry, or do not close the issue"
        for item in owed
        if item.issue in closing
    ]


def open_issue_problems(root: Path, client: GitHubClient | None = None) -> list[str]:
    """Each issue the known-failure list or the PENDING table names that GitHub does not report as an open issue: one
    in another state, or a pull request, which gh's `issue view` answers for as well, and whose URL holds /pull/; none
    while both are empty, which asks GitHub nothing. A gh that cannot answer, or a checkout whose origin names no
    GitHub repository, is a problem, never an open issue. Only `tests/run_validation.py --known-failure-issues` runs it,
    so the token gh reads reaches no suite."""
    owed = owed_issues(root)
    if not owed:
        return []
    repository = checkout_repository(root)
    if repository is None:
        return [
            f"{owed[0].source} names issues, but the checkout's origin names no GitHub repository, so the "
            "known-failure check cannot ask whether they are open"
        ]
    client = client or GitHubClient(runner=partial(subprocess_runner, cwd=root))
    problems: list[str] = []
    for issue in sorted({item.issue for item in owed}):
        sources = sorted({item.source for item in owed if item.issue == issue})
        named = f"{' and '.join(sources)} {'name' if len(sources) > 1 else 'names'} #{issue}"
        try:
            answer = client.json(["issue", "view", str(issue), "--repo", repository, "--json", "state,url"])
        except GitHubError as error:
            problems.append(f"the known-failure check cannot ask GitHub whether #{issue} is open: {error}")
            continue
        state = answer.get("state") if isinstance(answer, dict) else None
        url = answer.get("url") if isinstance(answer, dict) else None
        if not isinstance(url, str) or PULL_PATH in url:
            problems.append(
                f"{named}, which GitHub reports as a pull request or without its URL ({url}), not an issue; each "
                "names the open issue whose pull request ends what it lists"
            )
        elif state != OPEN:
            problems.append(
                f"{named}, which GitHub reports as {state}, not {OPEN}; each names the open issue whose pull request "
                "ends what it lists"
            )
    return problems


class KnownFailurePolicies(unittest.TestCase):
    def test_the_known_failure_list_names_each_case_once_in_order_with_its_issue(self) -> None:
        self.assertEqual([], known_failure_problems(REPOSITORY_ROOT))

    def test_the_pending_table_is_a_literal_this_module_reads(self) -> None:
        self.assertEqual([], pending_problems(REPOSITORY_ROOT))


class KnownFailurePullRequestPolicies(PullRequestChecks):
    def test_the_pull_request_closes_no_issue_a_known_failure_still_names(self) -> None:
        self.assertEqual(
            [],
            closed_known_failure_problems(
                REPOSITORY_ROOT, self.pull_request.body_text(), self.pull_request.title_text()
            ),
        )

"""The malformed-input suite's known failures: each names the open issue that fixes it, and the list only shrinks.

tests/malformed_input/test_malformed_input.py holds every reader of a file or record to every corpus case of its
format, and a reader that fails a case today is listed in tests/malformed_input/known_failures.json with the issue
that fixes it, never marked as an expected failure. The suite fails a listed case that now passes, so the fix removes
its entry, and an unlisted case that fails, so the list never grows in silence. This module holds the list itself: each
entry names its reader, its case, and its issue, once, in order. A pull request that closes an issue an entry still
names is refused, in every form GitHub closes it by, in its title or its body, since the issue would close while its
case still fails; the pull request that fixes the reader removes the entry with it. tests/malformed_input/readers.py's
PENDING, each reader no table holds yet with the issue whose pull request adds it, and each fault driver's
refusal_gaps in tests/malformed_input/faults_*.py, each run whose refusal is no refusal line yet with the issue that
gives it one, are held to the same two rules. `tests/known_failure_issues.py`, a program of its own that runs no
policy and no suite, also asks GitHub, through gh, that each issue any of them names is an open issue, never a pull
request, and asks nothing while none names one; a gh that cannot answer fails it.
"""

from __future__ import annotations

import ast
import re
import unittest
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import file_presence
import json_reader
import python_reader
import whole_number
from closing_references import pull_request_references
from github_client import GitHubClient, GitHubError, subprocess_runner
from github_remote import github_repository
from module_assignments import sole_assignment
from validation_support import REPOSITORY_ROOT, PullRequestChecks, UnreadablePython, parse_python

from tools.release_notes import ReleaseNotesError, git_output

KNOWN_FAILURES = Path("tests") / "malformed_input" / "known_failures.json"
ENTRY_KEYS = ("reader", "case", "issue")
# The table of readers owed a reader-table entry, each with the issue whose pull request adds it, and its name there.
PENDING_MODULE = Path("tests") / "malformed_input" / "readers.py"
PENDING = "PENDING"
# The fault drivers' tables, read without running them, and the driver argument that names a run whose refusal is no
# refusal line yet, with the open issue that gives it one: `#<issue> <what it lacks>`.
FAULT_TABLES = Path("tests") / "malformed_input"
FAULT_TABLE_PATTERN = "faults_*.py"
REFUSAL_GAPS = "refusal_gaps"
GAP_REASON = re.compile(r"#(?P<issue>[1-9][0-9]*) \S", re.ASCII)
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


def _named(node: ast.AST) -> str | None:
    """The identifier a node spells, as a name, an attribute, a string, or a binding of any kind, or None. Each
    binding Python spells outside a Name node is here: a parameter, a keyword, an import, a global or nonlocal
    declaration, a match mapping's rest, and, by their `name`, a definition, an exception or match capture, and a
    type parameter."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.arg):
        return node.arg
    if isinstance(node, ast.keyword):
        return node.arg
    if isinstance(node, ast.alias):
        return node.asname or node.name.partition(".")[0]
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return PENDING if PENDING in node.names else None
    if isinstance(node, ast.MatchMapping):
        return node.rest
    name = getattr(node, "name", None)
    return name if isinstance(name, str) else None


def _other_mentions(tree: ast.Module, assignment: ast.Name) -> list[int]:
    """The line of each place the module names PENDING other than its one assignment's target and the strings its
    `__all__` lists: any other binding, write, call, alias, or read could change the table the module ends with."""
    exported = {
        id(node)
        for statement in tree.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in (statement.targets if isinstance(statement, ast.Assign) else [statement.target])
        )
        and statement.value is not None
        for node in ast.walk(statement.value)
        if isinstance(node, ast.Constant)
    }
    return sorted(
        getattr(node, "lineno", 0)
        for node in ast.walk(tree)
        if node is not assignment and id(node) not in exported and _named(node) == PENDING
    )


def read_pending(path: Path) -> dict[str, int]:
    """The PENDING table a readers module assigns: each reader's name and its issue, read from the syntax tree, never
    run; KnownFailuresUnreadable when the module assigns none, assigns it more than once, names it anywhere else, by
    a write, a call, an alias, a binding, or a string other than `__all__`'s, so that the table it runs with could
    differ from the one read, or assigns one that is not a literal dict of names to positive issue numbers. A write
    through a string the module builds, such as `globals()["PEND" + "ING"]`, and a write from another module are not
    seen."""
    try:
        tree = parse_python(path)
    except (OSError, UnreadablePython) as error:
        raise KnownFailuresUnreadable(f"cannot be read: {error}") from error
    assignments = [
        assignment
        for node in tree.body
        if (assignment := sole_assignment(node)) is not None and assignment[0].id == PENDING
    ]
    if len(assignments) != 1:
        raise KnownFailuresUnreadable(f"must assign {PENDING} once at module level")
    [(target, value)] = assignments
    if lines := _other_mentions(tree, target):
        raise KnownFailuresUnreadable(
            f"must name {PENDING} only in its one assignment, but names it again on line "
            f"{', '.join(str(line) for line in lines)}"
        )
    try:
        table = python_reader.literal(value)
    except python_reader.CONTENT_ERRORS as error:
        raise KnownFailuresUnreadable(f"must assign {PENDING} a literal dict") from error
    if not isinstance(table, dict) or not all(
        isinstance(name, str) and isinstance(issue, int) and not isinstance(issue, bool) and issue > 0
        for name, issue in table.items()
    ):
        raise KnownFailuresUnreadable(f"must map each name in {PENDING} to a positive issue number")
    return table


@dataclass(frozen=True)
class RefusalGap:
    """A run a fault driver names in its refusal_gaps: its table, its driver's name, the run, and the open issue."""

    table: str
    driver: str
    run: str
    issue: int


def _driver_name(call: ast.Call) -> str:
    """A driver's name, its entry point and its command, as fault_table's Driver.name gives it, read from the call's
    literal arguments; KnownFailuresUnreadable when either is not a literal string."""
    given = dict(zip(("entry", "command"), call.args[:2], strict=False))
    given |= {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg in ("entry", "command")}
    parts = [given.get(name) for name in ("entry", "command")]
    if not all(isinstance(part, ast.Constant) and isinstance(part.value, str) for part in parts):
        raise KnownFailuresUnreadable(
            f"line {call.lineno}: a driver with {REFUSAL_GAPS} must name its entry and command"
        )
    return " ".join(str(part.value) for part in parts if isinstance(part, ast.Constant)).strip()


def read_refusal_gaps(path: Path, table: str) -> list[RefusalGap]:
    """The refusal gaps a fault table's drivers name, read from the syntax tree, never run: each call given a
    refusal_gaps keyword, which must be a literal dict of runs to reasons that open with `#<issue> `, beside an entry
    and a command given as literal strings; KnownFailuresUnreadable otherwise. A gap given another way, such as through
    a `**` unpacking, is not seen here; test_faults holds what this reads to each driver's own refusal_gaps."""
    try:
        tree = parse_python(path)
    except (OSError, UnreadablePython) as error:
        raise KnownFailuresUnreadable(f"cannot be read: {error}") from error
    gaps: list[RefusalGap] = []
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        for keyword in call.keywords:
            if keyword.arg != REFUSAL_GAPS:
                continue
            try:
                runs = python_reader.literal(keyword.value)
            except python_reader.CONTENT_ERRORS as error:
                raise KnownFailuresUnreadable(f"line {call.lineno}: {REFUSAL_GAPS} must be a literal dict") from error
            if not isinstance(runs, dict) or not all(isinstance(run, str) for run in runs):
                raise KnownFailuresUnreadable(f"line {call.lineno}: {REFUSAL_GAPS} must map each run to its reason")
            driver = _driver_name(call)
            for run, reason in runs.items():
                match = GAP_REASON.match(reason) if isinstance(reason, str) else None
                refused = KnownFailuresUnreadable(
                    f"line {call.lineno}: the refusal gap {run!r} of {driver} must open with the open issue that "
                    "closes it, as #<number>, then say what the run lacks"
                )
                if match is None:
                    raise refused
                gaps.append(RefusalGap(table, driver, run, whole_number.read(match.group("issue"), refused)))
    return gaps


def refusal_gaps(root: Path) -> list[RefusalGap]:
    """Every fault table's refusal gaps, table by table in path order; KnownFailuresUnreadable, naming the table, when
    one cannot be read."""
    gaps: list[RefusalGap] = []
    try:
        tables = file_presence.matching(root / FAULT_TABLES, FAULT_TABLE_PATTERN)
    except OSError as error:
        raise KnownFailuresUnreadable(f"{FAULT_TABLES.as_posix()} cannot be listed: {error}") from error
    for path in tables:
        table = path.relative_to(root).as_posix()
        try:
            gaps += read_refusal_gaps(path, table)
        except KnownFailuresUnreadable as error:
            raise KnownFailuresUnreadable(f"{table} {error}") from error
    return gaps


def refusal_gap_problems(root: Path) -> list[str]:
    """Why a fault table's refusal gaps cannot be read, or nothing."""
    try:
        refusal_gaps(root)
    except KnownFailuresUnreadable as error:
        return [str(error)]
    return []


def pending_problems(root: Path) -> list[str]:
    """Why the PENDING table cannot be read, or nothing."""
    try:
        read_pending(root / PENDING_MODULE)
    except KnownFailuresUnreadable as error:
        return [f"{PENDING_MODULE.as_posix()} {error}"]
    return []


def unreadable_lists(root: Path) -> list[str]:
    """Why the known-failure list, the PENDING table, or a fault table's refusal gaps cannot be read, or nothing;
    owed_issues reads any of them as empty, which a check that asks GitHub about them must not."""
    try:
        read_known_failures(root / KNOWN_FAILURES)
    except KnownFailuresUnreadable as error:
        return [f"{KNOWN_FAILURES.as_posix()} {error}", *pending_problems(root), *refusal_gap_problems(root)]
    return [*pending_problems(root), *refusal_gap_problems(root)]


def owed_issues(root: Path) -> list[OwedIssue]:
    """Each issue the known-failure list, the PENDING table, and the fault tables' refusal gaps name, with what each
    still lists under it; a list that cannot be read adds nothing, since known_failure_problems, pending_problems, and
    refusal_gap_problems name it."""
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
    try:
        gaps = refusal_gaps(root)
    except KnownFailuresUnreadable:
        gaps = []
    owed += [OwedIssue(gap.issue, gap.table, f"names {gap.driver}'s refusal gap {gap.run!r}") for gap in gaps]
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
    closes an issue either names, that a known failure, a PENDING reader, or a fault driver's refusal gap still names,
    with what is still owed."""
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
    """Each issue the known-failure list, the PENDING table, or a fault table's refusal gaps name that GitHub does not
    report as an open issue: one in another state, or a pull request, which gh's `issue view` answers for as well, and
    whose URL holds /pull/; none while none names one, which asks GitHub nothing. A list that cannot be read, a git
    that cannot read the checkout's origin, a gh that cannot answer, or a checkout whose origin names no GitHub
    repository, is a problem, never an empty list or an open issue. Only `tests/known_failure_issues.py` runs it, so
    the token gh reads reaches no policy and no suite."""
    unreadable = unreadable_lists(root)
    if unreadable:
        cannot = "so the known-failure check cannot ask whether its issues are open"
        return [f"{problem}, {cannot}" for problem in unreadable]
    owed = owed_issues(root)
    if not owed:
        return []
    try:
        repository = github_repository(git_output(root, "remote", "get-url", "origin"))
    except ReleaseNotesError as error:
        return [f"{owed[0].source} names issues, but git cannot read the checkout's origin: {error}"]
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

    def test_each_fault_table_s_refusal_gaps_are_literals_this_module_reads(self) -> None:
        self.assertEqual([], refusal_gap_problems(REPOSITORY_ROOT))


class KnownFailurePullRequestPolicies(PullRequestChecks):
    def test_the_pull_request_closes_no_issue_a_known_failure_still_names(self) -> None:
        self.assertEqual(
            [],
            closed_known_failure_problems(
                REPOSITORY_ROOT, self.pull_request.body_text(), self.pull_request.title_text()
            ),
        )

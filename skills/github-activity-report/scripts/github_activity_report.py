"""Month-by-month GitHub contribution report for one user, a list of users, or a team in one organization.

Prints the Markdown report on stdout and progress on stderr. A failure prints no table, only the line
`FAILED <reason> [<kind>]` on stdout, and exits 1; an invalid argument exits 2. A group, or a history file the
caller names, gets one comparison table of a metric instead of the per-user table; the history is written only once
every user's searches have succeeded, so a failure leaves it as it was.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from atomic_file import replace_text
from console import run_main
from flat_text import flat_text
from github_client import (
    Clock,
    CommandResult,
    GitHubClient,
    GitHubError,
    Runner,
    Sleeper,
    graphql_failure,
    json_body,
    split_response,
    subprocess_runner,
)
from github_logins import is_login
from skill_roots import skills_directory_holding
from typed_fields import FieldError, FieldReader, describe

T = TypeVar("T")
SEARCH_PAGE_SIZE = 100
SEARCH_RESULT_CAP = 1000
SEARCH_INTERVAL = 2.1
MAX_INTERVAL = 8.0
INTERVAL_GROWTH = 1.5
RECOVERY_STREAK = 10
PAGINATION_PASSES = 3
MAX_MONTHS = 36
READ = FieldReader()
TEAM_PAGE_SIZE = 100
# A team's slug, as GitHub derives it from the team's name.
TEAM_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
# Any of these classic token scopes lets a token read an organization's teams.
ORG_READ_SCOPES = frozenset({"read:org", "write:org", "admin:org"})
# Each metric's history column, the Activity field that holds it, and its table heading.
METRICS = {
    "prs_authored": ("authored", "PRs authored"),
    "prs_merged": ("merged", "PRs merged"),
    "commits": ("commits", "Commits"),
    "prs_reviewed": ("prs_reviewed", "PRs reviewed"),
    "reviews_submitted": ("reviews_submitted", "Reviews submitted"),
}
DEFAULT_METRIC = "prs_merged"
# The columns of a history file written before it recorded the cap, which the report still reads.
FIRST_HISTORY_COLUMNS = ("month", "user", *METRICS, "is_partial", "captured_at")
HISTORY_COLUMNS = (*FIRST_HISTORY_COLUMNS, "capped")
# The `capped` value of a row read from a file that did not record the cap; the report searches its month again.
CAP_UNKNOWN = "unknown"
# The metrics each search a cap can truncate counts: `_monthly` names a search by its metric's heading.
CAPPED_METRICS = {heading: (metric,) for metric, (_, heading) in METRICS.items()} | {
    "Reviews": ("prs_reviewed", "reviews_submitted")
}
MONTH_LABEL = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
COUNT = re.compile(r"0|[1-9][0-9]*")
# The longest history field a refusal quotes; a longer one is named by its length.
QUOTED_FIELD = 40
PARTIAL = {"true": True, "false": False}

REVIEW_SEARCH_QUERY = """
query($q: String!, $user: String!, $after: String) {
  search(type: ISSUE, query: $q, first: 100, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        id
        author { login }
        reviews(author: $user, first: 100) {
          pageInfo { hasNextPage endCursor }
          nodes { state submittedAt }
        }
      }
    }
  }
}
"""

REVIEW_PAGE_QUERY = """
query($id: ID!, $user: String!, $after: String!) {
  node(id: $id) {
    ... on PullRequest {
      id
      reviews(author: $user, first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { state submittedAt }
      }
    }
  }
}
"""


class SearchPacer:
    """Spaces search requests to stay under GitHub's search rate limits.

    It leaves at least `interval` between the end of one search request and the start of the next, widens the
    interval after each rate-limited attempt, and narrows it again after a run of clean searches.
    """

    def __init__(self, sleeper: Sleeper, clock: Clock) -> None:
        self.sleeper = sleeper
        self.clock = clock
        self.interval = SEARCH_INTERVAL
        self._last_search: float | None = None
        self._clean_searches = 0

    def before(self) -> None:
        if self._last_search is not None:
            remaining = self.interval - (self.clock() - self._last_search)
            if remaining > 0:
                self.sleeper(remaining)

    def after(self, error: GitHubError | None) -> None:
        self._last_search = self.clock()
        if error is None:
            self._clean_searches += 1
            if self._clean_searches >= RECOVERY_STREAK and self.interval > SEARCH_INTERVAL:
                self.interval = max(SEARCH_INTERVAL, self.interval / INTERVAL_GROWTH)
                self._clean_searches = 0
        elif error.kind == "rate_limit":
            self.interval = min(self.interval * INTERVAL_GROWTH, MAX_INTERVAL)
            self._clean_searches = 0


class ReportFailure(Exception):
    """A failure of the report's own rather than of a GitHub request: a history file it cannot use (`history`), a
    token without the scope reading a team needs (`scope`), or a team it cannot resolve to members (`team`)."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


@contextmanager
def _shape_of(response: str) -> Iterator[None]:
    """Fail a field read inside the block as a malformed response, naming the field and what it held."""
    try:
        yield
    except FieldError as exc:
        raise GitHubError(f"{response} has an unexpected shape: {exc}", kind="malformed") from exc


def _search_payload(result: CommandResult) -> dict[str, Any]:
    """A REST search response, retrying one GitHub cut short."""
    with _shape_of("Search response"):
        response = READ.json_object(json_body(result), "response")
        incomplete = READ.boolean(response.get("incomplete_results"), "response.incomplete_results")
    if incomplete:
        raise GitHubError("GitHub search timed out and returned incomplete results", kind="incomplete", retryable=True)
    return response


def _graphql_payload(result: CommandResult) -> Any:
    """A GraphQL response, failing on the errors its body reports."""
    payload = json_body(result)
    problem = graphql_failure(payload)
    if problem is not None:
        raise problem
    return payload


def _next_cursor(page_info: Any, path: str, followed: set[str]) -> str | None:
    """The cursor of the next page a GraphQL connection's `pageInfo` promises, or None on its last page.

    `hasNextPage` must be a boolean and, when it promises a page, `endCursor` a string not already in `followed`, so a
    page that repeats fails instead of paging without end. The cursor is added to `followed`.
    """
    info = READ.json_object(page_info, path)
    if not READ.boolean(info.get("hasNextPage"), f"{path}.hasNextPage"):
        return None
    cursor = READ.string(info.get("endCursor"), f"{path}.endCursor")
    if cursor in followed:
        raise READ.fail(f"{path}.endCursor", "a cursor not already followed", describe(cursor))
    followed.add(cursor)
    return cursor


class GitHubSearchClient:
    """The REST and GraphQL searches the report makes, over ejryan89-skill-core's GitHub CLI client.

    Every request reads the response headers, so GitHub's Retry-After and quota reset set the wait, and a response
    missing results from organizations the token is not SSO-authorized for fails. Searches are also paced.
    """

    def __init__(
        self, runner: Runner = subprocess_runner, sleeper: Sleeper = time.sleep, clock: Clock = time.time
    ) -> None:
        self.github = GitHubClient(runner, sleeper=sleeper, clock=clock)
        self.pacer = SearchPacer(sleeper, clock)

    @property
    def interval(self) -> float:
        return self.pacer.interval

    def _request(self, arguments: list[str], parse: Callable[[CommandResult], T], *, search: bool) -> T:
        return self.github.request(["api", *arguments], parse, headers=True, pacer=self.pacer if search else None)

    def _search_page(self, endpoint: str, query: str, page: int, per_page: int) -> dict[str, Any]:
        response = self._request(
            [f"{endpoint}?q={quote(query, safe='')}&per_page={per_page}&page={page}"], _search_payload, search=True
        )
        with _shape_of(f"Search response for {query!r}"):
            READ.array(response.get("items"), "response.items", READ.json_object)
            READ.integer(response.get("total_count"), "response.total_count", minimum=0)
        return response

    def search_all(
        self, endpoint: str, query: str, key: tuple[str, ...], first: dict[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], bool]:
        """Fetch every distinct result up to the Search API's 1000-result cap; report whether the cap truncated them.

        A pass that ends with fewer results than GitHub reported means results shifted between pages, so the
        search is repeated from the first page and fails closed if it never comes out complete. Commit search can
        legitimately list one commit in several repositories, so only pull request searches must be distinct.
        """
        repeats_allowed = endpoint == "search/commits"
        found = expected = 0
        for attempt in range(PAGINATION_PASSES):
            payload = (
                first if first is not None and attempt == 0 else self._search_page(endpoint, query, 1, SEARCH_PAGE_SIZE)
            )
            items: list[dict[str, Any]] = []
            page = 1
            while True:
                batch = payload["items"]
                items.extend(batch)
                expected = min(payload["total_count"], SEARCH_RESULT_CAP)
                if len(batch) < SEARCH_PAGE_SIZE or len(items) >= expected:
                    break
                page += 1
                payload = self._search_page(endpoint, query, page, SEARCH_PAGE_SIZE)
            unique = _unique(items, key, "Search")
            found = min(len(items), SEARCH_RESULT_CAP) if repeats_allowed else len(unique)
            if found >= expected:
                return unique[:SEARCH_RESULT_CAP], payload["total_count"] > SEARCH_RESULT_CAP
        raise GitHubError(_shortfall(query, found, expected), kind="incomplete")

    def search_range(
        self, endpoint: str, template: str, start: date, end: date, key: tuple[str, ...]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Search `template` with `{span}` set to start..end, halving the range until each part fits under the cap.

        Returns the items and the spans that still exceeded the cap because they were a single day.
        """
        query = template.replace("{span}", f"{start}..{end}")
        first = self._search_page(endpoint, query, 1, SEARCH_PAGE_SIZE)
        if first["total_count"] > SEARCH_RESULT_CAP and start < end:
            middle = start + (end - start) // 2
            left, left_capped = self.search_range(endpoint, template, start, middle, key)
            right, right_capped = self.search_range(endpoint, template, middle + timedelta(days=1), end, key)
            return left + right, left_capped + right_capped
        items, capped = self.search_all(endpoint, query, key, first)
        return items, [str(start)] if capped else []

    def token_scopes(self) -> frozenset[str] | None:
        """The classic OAuth scopes of gh's token, or None when GitHub names none, as for a fine-grained token."""

        def parse(result: CommandResult) -> frozenset[str] | None:
            scopes = split_response(result.stdout).headers.get("x-oauth-scopes")
            return None if scopes is None else frozenset(part.strip() for part in scopes.split(",") if part.strip())

        return self.github.request(["api", "-i", "user"], parse)

    def team_members(self, org: str, team: str) -> list[str]:
        """The logins of the team's current members, refusing a token whose scopes cannot read the organization's
        teams before asking, and a team GitHub does not show or that has no members."""
        scopes = self.token_scopes()
        if scopes is not None and not scopes & ORG_READ_SCOPES:
            raise ReportFailure(
                f"Reading the members of team {org}/{team} needs the read:org scope, which the gh token lacks; "
                "run gh auth refresh -s read:org and rerun",
                "scope",
            )
        logins: list[str] = []
        page = 1
        try:
            while True:
                batch = self._request(
                    [f"orgs/{org}/teams/{team}/members?per_page={TEAM_PAGE_SIZE}&page={page}"],
                    _member_logins,
                    search=False,
                )
                known = {login.casefold() for login in logins}
                new = [login for login in batch if login.casefold() not in known]
                if batch and not new:
                    raise GitHubError(f"Page {page} of team {org}/{team} repeats earlier members", kind="malformed")
                logins.extend(new)
                if len(batch) < TEAM_PAGE_SIZE:
                    break
                page += 1
        except GitHubError as exc:
            if exc.kind not in ("not_found", "forbidden"):
                raise
            raise ReportFailure(
                f"Team {org}/{team} was not found, or the gh token cannot see it: {exc}; check the team's slug",
                "team",
            ) from exc
        if not logins:
            raise ReportFailure(f"Team {org}/{team} has no members", "team")
        return logins

    def graphql(self, query: str, variables: dict[str, str], *, search: bool = False) -> dict[str, Any]:
        arguments = ["graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            arguments.extend(["-f", f"{name}={value}"])
        payload = self._request(arguments, _graphql_payload, search=search)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise GitHubError("GraphQL response has no data", kind="malformed")
        return data

    def _graphql_search_page(self, query: str, user: str, after: str | None) -> dict[str, Any]:
        variables = {"q": query, "user": user}
        if after is not None:
            variables["after"] = after
        data = self.graphql(REVIEW_SEARCH_QUERY, variables, search=True)
        with _shape_of(f"GraphQL search for {query!r}"):
            search = READ.json_object(data.get("search"), "data.search")
            READ.integer(search.get("issueCount"), "data.search.issueCount", minimum=0)
            READ.array(search.get("nodes"), "data.search.nodes")
            READ.json_object(search.get("pageInfo"), "data.search.pageInfo")
        return search

    def graphql_search_range(self, template: str, user: str, start: date, end: date) -> tuple[list[Any], list[str]]:
        """GraphQL counterpart of `search_range`, returning pull request nodes with the user's reviews inline."""
        query = template.replace("{span}", f"{start}..{end}")
        page = self._graphql_search_page(query, user, None)
        if page["issueCount"] > SEARCH_RESULT_CAP and start < end:
            middle = start + (end - start) // 2
            left, left_capped = self.graphql_search_range(template, user, start, middle)
            right, right_capped = self.graphql_search_range(template, user, middle + timedelta(days=1), end)
            return left + right, left_capped + right_capped
        unique: list[Any] = []
        expected = 0
        for attempt in range(PAGINATION_PASSES):
            if attempt:
                page = self._graphql_search_page(query, user, None)
            nodes = list(page["nodes"])
            followed: set[str] = set()
            while True:
                with _shape_of(f"GraphQL search for {query!r}"):
                    cursor = _next_cursor(page["pageInfo"], "data.search.pageInfo", followed)
                if cursor is None or len(nodes) >= SEARCH_RESULT_CAP:
                    break
                page = self._graphql_search_page(query, user, cursor)
                nodes.extend(page["nodes"])
            expected = min(page["issueCount"], SEARCH_RESULT_CAP)
            unique = _distinct_pull_requests(nodes)
            if len(unique) >= expected:
                return unique[:SEARCH_RESULT_CAP], [str(start)] if page["issueCount"] > SEARCH_RESULT_CAP else []
        raise GitHubError(_shortfall(query, len(unique), expected), kind="incomplete")


def _member_logins(result: CommandResult) -> list[str]:
    """The logins on one page of a team's members."""
    with _shape_of("Team members response"):
        members = READ.array(json_body(result), "response", READ.json_object)
        logins = [READ.string(member.get("login"), f"response[{index}].login") for index, member in enumerate(members)]
    for login in logins:
        if not is_login(login):
            raise GitHubError(f"Team members response names {login!r}, which is not a GitHub login", kind="malformed")
    return logins


def _shortfall(query: str, found: int, expected: int) -> str:
    return (
        f"Search for {query!r} returned {found} distinct results but GitHub reported {expected} after "
        f"{PAGINATION_PASSES} attempts; results changed during pagination, so rerun the report"
    )


def _distinct_pull_requests(nodes: list[Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for node in nodes:
        node_id = node.get("id") if isinstance(node, dict) else None
        if not isinstance(node_id, str):
            raise GitHubError("Review search returned a result that is not a readable pull request", kind="forbidden")
        if node_id not in seen:
            seen.add(node_id)
            unique.append(node)
    return unique


def months_window(today: date, months: int) -> list[date]:
    """First day of each of the last `months` calendar months, ending with today's month."""
    year, month = today.year, today.month
    firsts: list[date] = []
    for _ in range(months):
        firsts.append(date(year, month, 1))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return list(reversed(firsts))


def month_label(value: date) -> str:
    return f"{value:%Y-%m}"


def _utc_date(value: Any, path: str) -> date:
    """The UTC date of an ISO 8601 timestamp that states its offset or `Z`.

    A timestamp without one is refused: read in the machine's zone, the same text would fall in a different month
    depending on where the report runs.
    """
    expected = "an ISO 8601 timestamp with a UTC offset"
    if not isinstance(value, str):
        raise READ.fail(path, expected, describe(value))
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise READ.fail(path, expected, describe(value)) from None
    if moment.utcoffset() is None:
        raise READ.fail(path, expected, describe(value))
    return moment.astimezone(UTC).date()


@dataclass
class Activity:
    months: list[str]
    authored: dict[str, int]
    merged: dict[str, int]
    commits: dict[str, int]
    prs_reviewed: dict[str, int]
    reviews_submitted: dict[str, int]
    prs_reviewed_total: int
    capped: dict[str, list[str]] = field(default_factory=dict)


def _field(item: Any, path: tuple[str, ...]) -> Any:
    value = item
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _unique(items: list[dict[str, Any]], key: tuple[str, ...], context: str) -> list[dict[str, Any]]:
    """Drop repeats that appear when results shift between pages or a commit exists in several repositories."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in items:
        identity = _field(item, key)
        if not isinstance(identity, str):
            raise GitHubError(f"{context} search result has no {'.'.join(key)}", kind="malformed")
        if identity not in seen:
            seen.add(identity)
            unique.append(item)
    return unique


def _monthly(
    client: GitHubSearchClient,
    endpoint: str,
    template: str,
    spans: list[tuple[date, date]],
    labels: list[str],
    date_path: tuple[str, ...],
    key: tuple[str, ...],
    column: str,
    capped: dict[str, list[str]],
) -> dict[str, int]:
    """Count search results per UTC month of `labels` by the timestamp at `date_path`, searching each span once."""
    counts = dict.fromkeys(labels, 0)
    items: list[dict[str, Any]] = []
    for start, end in spans:
        found, truncated = client.search_range(endpoint, template, start, end, key)
        items += found
        if truncated:
            capped.setdefault(column, []).extend(truncated)
    for item in items:
        with _shape_of(f"{column} result {_field(item, key)}"):
            label = month_label(_utc_date(_field(item, date_path), ".".join(date_path)))
        if label in counts:
            counts[label] += 1
    return counts


def _review_connection(node: Any, node_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One page of a pull request's reviews and its `pageInfo`. A missing connection means the token cannot read the
    pull request; a review that is not an object is malformed."""
    reviews = node.get("reviews") if isinstance(node, dict) else None
    if (
        not isinstance(reviews, dict)
        or not isinstance(reviews.get("nodes"), list)
        or not isinstance(reviews.get("pageInfo"), dict)
    ):
        raise GitHubError(f"Pull request {node_id} is not readable with this token", kind="forbidden")
    with _shape_of(f"Reviews of pull request {node_id}"):
        return READ.array(reviews["nodes"], "reviews.nodes", READ.json_object), reviews["pageInfo"]


def _submitted_dates(reviews: list[dict[str, Any]], node_id: str) -> list[date]:
    """The submission dates of the reviews that are not pending; a pending review has none yet."""
    dates: list[date] = []
    with _shape_of(f"Reviews of pull request {node_id}"):
        for index, review in enumerate(reviews):
            path = f"reviews.nodes[{index}]"
            if READ.string(review.get("state"), f"{path}.state") != "PENDING":
                dates.append(_utc_date(review.get("submittedAt"), f"{path}.submittedAt"))
    return dates


def _author_login(node: dict[str, Any], node_id: str) -> str | None:
    """The pull request author's login, or None for an author GitHub no longer has, such as a deleted account."""
    with _shape_of(f"Pull request {node_id}"):
        author = READ.optional(READ.json_object, node.get("author"), "author")
        return None if author is None else READ.string(author.get("login"), "author.login")


def _review_dates(
    client: GitHubSearchClient, org: str, user: str, start: date, today: date, capped: dict[str, list[str]]
) -> dict[str, list[date]]:
    """Submission dates of the user's non-pending reviews, per pull request someone else authored and updated from
    `start` through `today`."""
    template = f"org:{org} reviewed-by:{user} -author:{user} is:pr updated:{{span}}"
    nodes, truncated = client.graphql_search_range(template, user, start, today)
    if truncated:
        capped.setdefault("Reviews", []).extend(truncated)
    dates: dict[str, list[date]] = {}
    for node in nodes:
        node_id = node.get("id") if isinstance(node, dict) else None
        if not isinstance(node_id, str):
            raise GitHubError("Review search returned a result that is not a readable pull request", kind="forbidden")
        login = _author_login(node, node_id)
        if node_id in dates or (login is not None and login.casefold() == user.casefold()):
            continue
        reviews, page_info = _review_connection(node, node_id)
        followed: set[str] = set()
        while True:
            with _shape_of(f"Reviews of pull request {node_id}"):
                cursor = _next_cursor(page_info, "reviews.pageInfo", followed)
            if cursor is None:
                break
            data = client.graphql(REVIEW_PAGE_QUERY, {"id": node_id, "user": user, "after": cursor})
            more, page_info = _review_connection(data.get("node"), node_id)
            reviews = reviews + more
        dates[node_id] = _submitted_dates(reviews, node_id)
    return dates


def month_spans(firsts: list[date], today: date) -> list[tuple[date, date]]:
    """Each run of consecutive months among `firsts`, the first days of months in order, as its first day and its
    last, which is today for the current month."""
    spans: list[tuple[date, date]] = []
    for first in firsts:
        following = date(first.year + first.month // 12, first.month % 12 + 1, 1)
        last = min(following - timedelta(days=1), today)
        if spans and spans[-1][1] + timedelta(days=1) == first:
            spans[-1] = (spans[-1][0], last)
        else:
            spans.append((first, last))
    return spans


def collect_activity(
    client: GitHubSearchClient, org: str, user: str, months: int, today: date, pulled: list[str] | None = None
) -> Activity:
    """The user's activity in each of the last `months` months, or only in those of them `pulled` names. The searches
    by a date cover only those months, one range for each run of consecutive ones. The review search finds pull
    requests by their last update, which can follow a review's month, so it covers every pull request updated from
    the first of those months through today, and counts only the reviews submitted in those months."""
    window = months_window(today, months)
    labels = [month_label(first) for first in window if pulled is None or month_label(first) in pulled]
    spans = month_spans([first for first in window if month_label(first) in labels], today)
    capped: dict[str, list[str]] = {}
    authored = _monthly(
        client,
        "search/issues",
        f"org:{org} author:{user} is:pr created:{{span}}",
        spans,
        labels,
        ("created_at",),
        ("node_id",),
        "PRs authored",
        capped,
    )
    merged = _monthly(
        client,
        "search/issues",
        f"org:{org} author:{user} is:pr is:merged merged:{{span}}",
        spans,
        labels,
        ("pull_request", "merged_at"),
        ("node_id",),
        "PRs merged",
        capped,
    )
    commits = _monthly(
        client,
        "search/commits",
        f"org:{org} author:{user} author-date:{{span}}",
        spans,
        labels,
        ("commit", "author", "date"),
        ("sha",),
        "Commits",
        capped,
    )
    prs_reviewed = dict.fromkeys(labels, 0)
    reviews_submitted = dict.fromkeys(labels, 0)
    reviewed_in_window: set[str] = set()
    for node_id, dates in _review_dates(client, org, user, spans[0][0], today, capped).items():
        months_touched: set[str] = set()
        for value in dates:
            label = month_label(value)
            if label in reviews_submitted:
                reviews_submitted[label] += 1
                months_touched.add(label)
        for label in months_touched:
            prs_reviewed[label] += 1
        if months_touched:
            reviewed_in_window.add(node_id)
    return Activity(labels, authored, merged, commits, prs_reviewed, reviews_submitted, len(reviewed_in_window), capped)


def render_report(activity: Activity, org: str, user: str, today: date) -> str:
    columns = tuple(field for field, _ in METRICS.values())
    lines = [
        f"## GitHub activity for {user} in {org}",
        "",
        f"{activity.months[0]} through {today.isoformat()}. "
        "All dates and months are UTC; the current month is partial.",
        "",
        "| Month | PRs authored | PRs merged | Commits | PRs reviewed | Reviews submitted |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label in activity.months:
        values = " | ".join(str(getattr(activity, column)[label]) for column in columns)
        lines.append(f"| {label} | {values} |")
    totals = [sum(getattr(activity, column).values()) for column in columns]
    totals[3] = activity.prs_reviewed_total
    lines.append(f"| **Total** | {' | '.join(f'**{value}**' for value in totals)} |")
    lines += [
        "",
        "Notes:",
        f"- PRs reviewed counts distinct pull requests {user} reviewed in each month; a pull request reviewed in two "
        "months appears in both, but only once in the total. Reviews submitted counts every submitted review, "
        f"including re-reviews and comment-only reviews. Reviews on {user}'s own pull requests are excluded.",
        f"- Commits counts distinct default-branch commits whose author email is linked to {user}'s account.",
    ]
    lines.append(
        f"- Counts cover only {org} repositories the current token can read; GitHub search omits other "
        "repositories without reporting an error."
    )
    lines += _cap_warnings(activity.capped, "")
    return "\n".join(lines) + "\n"


def _cap_warnings(capped: dict[str, list[str]], whose: str) -> list[str]:
    """A WARNING note for each search the Search API's cap truncated; `whose` names the user in a group."""
    lines: list[str] = []
    for column, days in sorted(capped.items()):
        if column == "Reviews":
            lines.append(
                f"- WARNING: {whose}the review search exceeded GitHub's 1000-result Search API cap for pull requests "
                f"last updated on {', '.join(days)}. Their reviews can fall in any month, so PRs reviewed and "
                "Reviews submitted may be undercounted in every month and in both totals."
            )
        else:
            lines.append(
                f"- WARNING: {whose}{column} for {', '.join(days)} exceeded GitHub's 1000-result Search API cap; "
                "those counts are lower than the true totals."
            )
    return lines


@dataclass(frozen=True)
class HistoryRow:
    """One month of one user's activity in the history file."""

    month: str
    user: str
    counts: dict[str, int]  # keyed by metric
    is_partial: bool
    captured_at: str
    capped: tuple[str, ...] | None  # the metrics a search cap truncated, or None when the file did not record it


History = dict[tuple[str, str], HistoryRow]  # keyed by month and casefolded login


def _history_key(month: str, user: str) -> tuple[str, str]:
    return month, user.casefold()


def _capped_field(value: str) -> tuple[str, ...] | None:
    """The metrics a row's `capped` field names, in METRICS order, or None for CAP_UNKNOWN; ValueError otherwise."""
    if value == CAP_UNKNOWN:
        return None
    metrics = tuple(value.split(" ")) if value else ()
    if metrics != tuple(metric for metric in METRICS if metric in metrics):
        raise ValueError(value)
    return metrics


def _history_row(fields: list[str], line: int, path: Path, columns: tuple[str, ...]) -> HistoryRow:
    """One data row of a history file with `columns`, refusing any field that is not what the report writes."""

    def refuse(what: str) -> ReportFailure:
        return ReportFailure(f"History file {path} line {line} {what}; fix or move the file and rerun", "history")

    if len(fields) != len(columns):
        raise refuse(f"has {len(fields)} fields, not {len(columns)}")
    capped_text = fields[-1] if columns == HISTORY_COLUMNS else CAP_UNKNOWN
    month, user, *counts, partial, captured_at = fields[: len(FIRST_HISTORY_COLUMNS)]
    if not MONTH_LABEL.fullmatch(month):
        raise refuse(f"has month {month!r}, not YYYY-MM")
    if not is_login(user):
        raise refuse(f"has user {user!r}, not a GitHub login")
    counted: dict[str, int] = {}
    for metric, count in zip(METRICS, counts, strict=True):
        if not COUNT.fullmatch(count):
            raise refuse(f"has {metric} {_quoted(count)}, not a count")
        try:
            counted[metric] = int(count)
        except ValueError:  # more digits than Python's integer-digit limit converts
            raise refuse(f"has {metric} {_quoted(count)}, not a count") from None
    if partial not in PARTIAL:
        raise refuse(f"has is_partial {partial!r}, not true or false")
    try:
        _utc_date(captured_at, "captured_at")
    except FieldError:
        raise refuse(f"has captured_at {captured_at!r}, not a timestamp with a UTC offset") from None
    try:
        capped = _capped_field(capped_text)
    except ValueError:
        raise refuse(f"has capped {capped_text!r}, not {CAP_UNKNOWN} or metrics in the header's order") from None
    return HistoryRow(month, user, counted, PARTIAL[partial], captured_at, capped)


def _quoted(field: str) -> str:
    """A history field as a refusal names it: quoted when it is short, else by its length."""
    return repr(field) if len(field) <= QUOTED_FIELD else f"of {len(field)} characters"


def read_history(path: Path) -> History:
    """The rows of the history file at `path`; a file that does not exist yet, or is empty, holds none.

    A file written before the report recorded the cap has no `capped` column; its rows read with `capped` None.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ReportFailure(f"History file {path} cannot be read: {exc.strerror or exc}", "history") from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ReportFailure(f"History file {path} is not UTF-8 text", "history") from None
    try:
        records = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        raise ReportFailure(f"History file {path} is not CSV the report wrote: {exc}", "history") from exc
    if not records:
        return {}
    columns = tuple(records[0])
    if columns not in (HISTORY_COLUMNS, FIRST_HISTORY_COLUMNS):
        raise ReportFailure(
            f"History file {path} does not start with the header {','.join(HISTORY_COLUMNS)}; name a history file "
            "this report wrote, or a new path",
            "history",
        )
    history: History = {}
    for line, fields in enumerate(records[1:], start=2):
        row = _history_row(fields, line, path, columns)
        key = _history_key(row.month, row.user)
        if key in history:
            raise ReportFailure(f"History file {path} line {line} repeats {row.month} for {row.user}", "history")
        history[key] = row
    return history


def render_history(history: History) -> str:
    """The history file's text: the header, then one row per month and user, sorted by both."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HISTORY_COLUMNS)
    for row in sorted(history.values(), key=lambda row: (row.month, row.user.casefold())):
        flag = "true" if row.is_partial else "false"
        capped = CAP_UNKNOWN if row.capped is None else " ".join(row.capped)
        counts = (row.counts[metric] for metric in METRICS)
        writer.writerow([row.month, row.user, *counts, flag, row.captured_at, capped])
    return buffer.getvalue()


def months_to_pull(history: History, user: str, labels: list[str]) -> list[str]:
    """The months of `labels` to search for the user's activity: the last, which is the current, partial month, and
    each month the history lacks or captured while it was still partial, so the first run after a month ends pulls
    it again, and each month a file written before the report recorded the cap holds, so its cap becomes known. A
    closed month already in the history with its cap recorded is never searched for again."""
    pulled = []
    for label in labels:
        row = history.get(_history_key(label, user))
        if label == labels[-1] or row is None or row.is_partial or row.capped is None:
            pulled.append(label)
    return pulled


def capped_months(activity: Activity) -> dict[str, set[str]]:
    """The metrics a search cap truncated in each of the activity's months.

    A capped day lowers the counts of its own month, except for the review search, which is by the day a pull
    request was last updated: its reviews fall on or before that day, so every month up to the day's is lowered.
    """
    capped: dict[str, set[str]] = {label: set() for label in activity.months}
    for column, days in activity.capped.items():
        for day in days:
            for label in activity.months:
                if label == day[:7] or (column == "Reviews" and label <= day[:7]):
                    capped[label].update(CAPPED_METRICS[column])
    return capped


def _rows_of(activity: Activity, user: str, labels: list[str], current: str, captured_at: str) -> History:
    """History rows for the user's months in `labels`, from this run's activity."""
    capped = capped_months(activity)
    return {
        _history_key(label, user): HistoryRow(
            label,
            user,
            {metric: getattr(activity, field)[label] for metric, (field, _) in METRICS.items()},
            label == current,
            captured_at,
            tuple(metric for metric in METRICS if metric in capped[label]),
        )
        for label in labels
    }


def _recorded_cap_warnings(history: History, user: str, labels: list[str]) -> list[str]:
    """A WARNING note for each of `labels` read back from the history whose row records a capped search."""
    lines: list[str] = []
    for label in labels:
        row = history[_history_key(label, user)]
        if row.capped:
            headings = ", ".join(METRICS[metric][1] for metric in row.capped)
            lines.append(
                f"- WARNING: for {user}, {label} is read from the history file, which records that GitHub's "
                f"1000-result Search API cap truncated its {headings} when it was captured; those counts are lower "
                "than the true totals."
            )
    return lines


METRIC_NOTES = {
    "prs_authored": "PRs authored counts the pull requests each user opened in the month.",
    "prs_merged": "PRs merged counts each user's pull requests merged in the month.",
    "commits": "Commits counts distinct default-branch commits whose author email is linked to each user's account.",
    "prs_reviewed": (
        "PRs reviewed counts distinct pull requests each user reviewed in the month, excluding their own; a pull "
        "request reviewed in two months appears in both, and twice in the total."
    ),
    "reviews_submitted": (
        "Reviews submitted counts every review each user submitted in the month, including re-reviews and "
        "comment-only reviews, excluding reviews on their own pull requests."
    ),
}


@dataclass(frozen=True)
class Comparison:
    """What a comparison table shows: `metric` for `users` over the months `labels`, from `rows`."""

    org: str
    users: list[str]
    labels: list[str]
    metric: str
    rows: History
    warnings: list[str]
    from_history: bool


def render_comparison(comparison: Comparison, today: date) -> str:
    """One table of the metric with a row per month and a column per user, then its notes."""
    users, labels, metric = comparison.users, comparison.labels, comparison.metric
    lines = [
        f"## GitHub activity in {comparison.org}: {METRICS[metric][1]}",
        "",
        f"{labels[0]} through {today.isoformat()}. All dates and months are UTC; the current month is partial.",
        "",
        f"| Month | {' | '.join(users)} |",
        f"|---|{'---:|' * len(users)}",
    ]
    totals = dict.fromkeys(users, 0)
    for label in labels:
        values = [comparison.rows[_history_key(label, user)].counts[metric] for user in users]
        for user, value in zip(users, values, strict=True):
            totals[user] += value
        lines.append(f"| {label} | {' | '.join(map(str, values))} |")
    lines.append(f"| **Total** | {' | '.join(f'**{totals[user]}**' for user in users)} |")
    lines += [
        "",
        "Notes:",
        f"- {METRIC_NOTES[metric]} Total sums the months shown.",
        f"- Counts cover only {comparison.org} repositories the current token can read; GitHub search omits other "
        "repositories without reporting an error.",
    ]
    if comparison.from_history:
        lines.append(
            "- Closed months already in the history file are read from it, not searched again, except once for each "
            "month of a file written before the report recorded GitHub's search cap; the current month is searched "
            "on every run, and a month the history captured while it was partial once more after it ends."
        )
    lines += comparison.warnings
    return "\n".join(lines) + "\n"


def _captured_at(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Request:
    """A group report's arguments: whom to report, over how many months, on which metric, and the history file."""

    org: str
    users: list[str]
    months: int
    metric: str
    history_path: Path | None


def compare(client: GitHubSearchClient, request: Request, history: History, now: datetime) -> Comparison:
    """Search each user's activity, merge it into `history`, and write the history file only once every user has
    succeeded, so a failure for any of them leaves the file as it was. A user who is not in the request keeps their
    rows."""
    today = now.astimezone(UTC).date()
    org, users, history_path = request.org, request.users, request.history_path
    labels = [month_label(first) for first in months_window(today, request.months)]
    rows = dict(history)
    warnings: list[str] = []
    for index, user in enumerate(users, start=1):
        pulled = months_to_pull(history, user, labels)
        print(f"Querying GitHub for {user} ({index} of {len(users)}): {len(pulled)} month(s).", file=sys.stderr)
        try:
            activity = collect_activity(client, org, user, request.months, today, pulled)
        except GitHubError as exc:
            raise GitHubError(f"{user}: {exc}", kind=exc.kind) from exc
        rows.update(_rows_of(activity, user, pulled, labels[-1], _captured_at(now)))
        warnings += _recorded_cap_warnings(history, user, [label for label in labels if label not in pulled])
        warnings += _cap_warnings(activity.capped, f"for {user}, ")
    if history_path is not None:
        try:
            replace_text(history_path, render_history(rows))
        except OSError as exc:
            raise ReportFailure(f"History file {history_path} cannot be written: {exc}", "history") from exc
    return Comparison(org, users, labels, request.metric, rows, warnings, history_path is not None)


def _login(value: str) -> str:
    if not is_login(value):
        raise argparse.ArgumentTypeError(f"not a valid GitHub login: {value!r}")
    return value


def _team(value: str) -> str:
    if not TEAM_SLUG.fullmatch(value):
        raise argparse.ArgumentTypeError(f"not a GitHub team slug: {value!r}")
    return value


def _months(value: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= MAX_MONTHS:
        raise argparse.ArgumentTypeError(f"must be an integer from 1 to {MAX_MONTHS}")
    return int(value)


def _history_path(value: str) -> Path:
    """The history file the caller names: outside every skills directory, and in a folder that exists, since the
    report neither chooses nor creates a place for per-person data."""
    path = Path(value)
    holder = skills_directory_holding(path)
    if holder is not None:
        raise argparse.ArgumentTypeError(f"{value} is inside the skills directory {holder}; name a path outside it")
    if path.is_dir():
        raise argparse.ArgumentTypeError(f"{value} is a folder; name the history file inside it")
    if not path.parent.is_dir():
        raise argparse.ArgumentTypeError(f"the folder of {value} does not exist; the report creates no folder")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", type=_login, required=True)
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--user", type=_login, action="append", help="a login to report; repeat it for a group")
    who.add_argument("--team", type=_team, help="the slug of an organization team, reported as its current members")
    parser.add_argument("--months", type=_months, default=12)
    parser.add_argument("--history", type=_history_path, help="a CSV file of every run's months, kept private")
    parser.add_argument("--metric", choices=tuple(METRICS), help=f"the comparison's metric (default {DEFAULT_METRIC})")
    return parser


def _distinct(logins: list[str]) -> list[str]:
    """`logins` without repeats, compared without case as GitHub compares logins."""
    seen: set[str] = set()
    distinct: list[str] = []
    for login in logins:
        if login.casefold() not in seen:
            seen.add(login.casefold())
            distinct.append(login)
    return distinct


def main(
    arguments: list[str] | None = None,
    client: GitHubSearchClient | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> int:
    parser = _parser()
    options = parser.parse_args(arguments)
    users = _distinct(options.user or [])
    group = options.team is not None or len(users) > 1
    if not group and options.history is None and options.metric is not None:
        parser.error("argument --metric: needs a group (--team, or --user more than once) or --history")
    if now is None:
        now = datetime.combine(today, datetime.min.time(), UTC) if today is not None else datetime.now(UTC)
    current = now.astimezone(UTC).date()
    search = client or GitHubSearchClient()
    print(
        f"Querying GitHub for {options.months} month(s); searches are spaced {SEARCH_INTERVAL}s apart.", file=sys.stderr
    )
    try:
        if not group and options.history is None:
            activity = collect_activity(search, options.org, users[0], options.months, current)
            sys.stdout.write(render_report(activity, options.org, users[0], current))
            return 0
        history = read_history(options.history) if options.history is not None else {}
        if options.team is not None:
            users = search.team_members(options.org, options.team)
        comparison = compare(
            search,
            Request(options.org, users, options.months, options.metric or DEFAULT_METRIC, options.history),
            history,
            now,
        )
    except (GitHubError, ReportFailure) as exc:
        # The reason can carry gh's multi-line stderr; the FAILED line stays one line.
        print(f"FAILED {flat_text(str(exc))} [{exc.kind}]")
        return 1
    sys.stdout.write(render_comparison(comparison, current))
    return 0


if __name__ == "__main__":
    run_main(main)

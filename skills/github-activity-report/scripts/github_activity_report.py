"""Month-by-month GitHub contribution report for one user in one organization.

Prints the Markdown report on stdout and progress on stderr. A failure prints no table, only the line
`FAILED <reason> [<kind>]` on stdout, and exits 1; an invalid argument exits 2.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

SEARCH_PAGE_SIZE = 100
SEARCH_RESULT_CAP = 1000
SEARCH_INTERVAL = 2.1
MAX_INTERVAL = 8.0
INTERVAL_GROWTH = 1.5
RECOVERY_STREAK = 10
BACKOFF_BASE = 5.0
MAX_RETRIES = 5
MAX_WAIT = 300.0
PAGINATION_PASSES = 3
MAX_MONTHS = 36
LOGIN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")

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


class GitHubActivityError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "api") -> None:
        super().__init__(message)
        self.kind = kind


# Definitions that tests/run_validation.py allows to be copied in another file, with the reason.
DUPLICATION_ALLOWED = {
    "CommandResult": "also in code-review-core's review_github.py and review_runtime.py; #27's shared core "
    "replaces the copies",
}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], CommandResult]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]


def subprocess_runner(arguments: Sequence[str]) -> CommandResult:
    try:
        process = subprocess.run(list(arguments), capture_output=True, text=True, encoding="utf-8", check=False)
    except FileNotFoundError as exc:
        raise GitHubActivityError(
            "GitHub CLI executable 'gh' was not found; install GitHub CLI and authenticate first",
            kind="prerequisite",
        ) from exc
    except OSError as exc:
        raise GitHubActivityError(f"GitHub CLI could not be started: {exc}", kind="execution") from exc
    return CommandResult(process.returncode, process.stdout, process.stderr)


def _classify_failure(stderr: str) -> str:
    lowered = stderr.casefold()
    if "rate limit" in lowered or "http 429" in lowered:
        return "rate_limit"
    if "http 401" in lowered or "not logged" in lowered:
        return "authentication"
    if "only the first 1000 search results" in lowered:
        return "search_cap"
    if "http 403" in lowered:
        return "forbidden"
    return "api"


@dataclass
class Response:
    status: int | None
    headers: dict[str, str]
    body: str


def _split_response(stdout: str) -> Response:
    """Separate the status line and headers printed by `gh api -i` from the body."""
    if not stdout.startswith("HTTP/"):
        return Response(None, {}, stdout)
    parts = re.split(r"\r?\n\r?\n", stdout, maxsplit=1)
    head, body = parts[0], parts[1] if len(parts) > 1 else ""
    lines = head.splitlines()
    status_parts = lines[0].split()
    status = int(status_parts[1]) if len(status_parts) > 1 and status_parts[1].isdigit() else None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().casefold()] = value.strip()
    return Response(status, headers, body)


class GitHubSearchClient:
    def __init__(
        self, runner: Runner = subprocess_runner, sleeper: Sleeper = time.sleep, clock: Clock = time.time
    ) -> None:
        self.runner = runner
        self.sleeper = sleeper
        self.clock = clock
        self.interval = SEARCH_INTERVAL
        self._last_search: float | None = None
        self._clean_searches = 0

    def _pace_search(self) -> None:
        """Leave at least `interval` between the end of one search request and the start of the next."""
        if self._last_search is not None:
            remaining = self.interval - (self.clock() - self._last_search)
            if remaining > 0:
                self.sleeper(remaining)

    def _record_search(self, rate_limited: bool) -> None:
        if rate_limited:
            self.interval = min(self.interval * INTERVAL_GROWTH, MAX_INTERVAL)
            self._clean_searches = 0
            return
        self._clean_searches += 1
        if self._clean_searches >= RECOVERY_STREAK and self.interval > SEARCH_INTERVAL:
            self.interval = max(SEARCH_INTERVAL, self.interval / INTERVAL_GROWTH)
            self._clean_searches = 0

    def _retry_wait(self, response: Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after", "")
        if retry_after.isdigit():
            wait = float(retry_after)
        elif (
            response.headers.get("x-ratelimit-remaining") == "0"
            and response.headers.get("x-ratelimit-reset", "").isdigit()
        ):
            wait = max(1.0, float(response.headers["x-ratelimit-reset"]) - self.clock() + 1.0)
        else:
            wait = BACKOFF_BASE * 2**attempt
        if wait > MAX_WAIT:
            raise GitHubActivityError(
                f"GitHub asked to wait {wait:.0f}s before retrying; rerun the report later", kind="rate_limit"
            )
        return wait

    def _request(self, arguments: list[str], *, search: bool) -> Any:
        for attempt in range(MAX_RETRIES + 1):
            if search:
                self._pace_search()
            result = self.runner(["gh", "api", "-i", *arguments])
            if search:
                self._last_search = self.clock()
            response = _split_response(result.stdout)
            kind, message = None, ""
            sso = response.headers.get("x-github-sso", "")
            if "partial-results" in sso.casefold():
                raise GitHubActivityError(
                    "GitHub omitted results from organizations this token is not SSO-authorized for "
                    f"({sso}); authorize the token for SSO and rerun",
                    kind="sso_partial",
                )
            if result.returncode != 0:
                kind = _classify_failure(result.stderr)
                if response.status == 429 or (
                    response.status == 403 and response.headers.get("x-ratelimit-remaining") == "0"
                ):
                    kind = "rate_limit"
                message = result.stderr.strip() or "GitHub API request failed"
            else:
                try:
                    payload = json.loads(response.body)
                except json.JSONDecodeError as exc:
                    raise GitHubActivityError(f"GitHub returned malformed JSON: {exc}", kind="malformed") from exc
                kind, message = _payload_problem(payload)
                if kind is None:
                    if search:
                        self._record_search(rate_limited=False)
                    return payload
            if kind not in ("rate_limit", "incomplete"):
                raise GitHubActivityError(message, kind=kind)
            if search and kind == "rate_limit":
                self._record_search(rate_limited=True)
            if attempt == MAX_RETRIES:
                raise GitHubActivityError(f"{message} (gave up after {MAX_RETRIES} retries)", kind=kind)
            self.sleeper(self._retry_wait(response, attempt))
        raise AssertionError("unreachable")

    def _search_page(self, endpoint: str, query: str, page: int, per_page: int) -> dict[str, Any]:
        payload = self._request([f"{endpoint}?q={quote(query, safe='')}&per_page={per_page}&page={page}"], search=True)
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("items"), list)
            or not isinstance(payload.get("total_count"), int)
        ):
            raise GitHubActivityError(f"Search response for {query!r} has an unexpected shape", kind="malformed")
        return payload

    def search_all(
        self, endpoint: str, query: str, key: tuple[str, ...], first: dict[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], bool]:
        """Fetch every distinct result up to the Search API's 1000-result cap; report whether the cap truncated them.

        A pass that ends with fewer results than GitHub reported means results shifted between pages, so the
        search is repeated from the first page and fails closed if it never comes out complete. Commit search can
        legitimately list one commit in several repositories, so only pull request searches must be distinct.
        """
        repeats_allowed = endpoint == "search/commits"
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
        raise GitHubActivityError(_shortfall(query, found, expected), kind="incomplete")

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

    def graphql(self, query: str, variables: dict[str, str], *, search: bool = False) -> dict[str, Any]:
        arguments = ["graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            arguments.extend(["-f", f"{name}={value}"])
        payload = self._request(arguments, search=search)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise GitHubActivityError("GraphQL response has no data", kind="malformed")
        return data

    def _graphql_search_page(self, query: str, user: str, after: str | None) -> dict[str, Any]:
        variables = {"q": query, "user": user}
        if after is not None:
            variables["after"] = after
        search = self.graphql(REVIEW_SEARCH_QUERY, variables, search=True).get("search")
        if (
            not isinstance(search, dict)
            or not isinstance(search.get("issueCount"), int)
            or not isinstance(search.get("nodes"), list)
            or not isinstance(search.get("pageInfo"), dict)
        ):
            raise GitHubActivityError(f"GraphQL search for {query!r} has an unexpected shape", kind="malformed")
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
        for attempt in range(PAGINATION_PASSES):
            if attempt:
                page = self._graphql_search_page(query, user, None)
            nodes = list(page["nodes"])
            while page["pageInfo"].get("hasNextPage") and len(nodes) < SEARCH_RESULT_CAP:
                page = self._graphql_search_page(query, user, str(page["pageInfo"].get("endCursor")))
                nodes.extend(page["nodes"])
            expected = min(page["issueCount"], SEARCH_RESULT_CAP)
            unique = _distinct_pull_requests(nodes)
            if len(unique) >= expected:
                return unique[:SEARCH_RESULT_CAP], [str(start)] if page["issueCount"] > SEARCH_RESULT_CAP else []
        raise GitHubActivityError(_shortfall(query, len(unique), expected), kind="incomplete")


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
            raise GitHubActivityError(
                "Review search returned a result that is not a readable pull request", kind="forbidden"
            )
        if node_id not in seen:
            seen.add(node_id)
            unique.append(node)
    return unique


def _payload_problem(payload: Any) -> tuple[str | None, str]:
    if isinstance(payload, dict) and payload.get("incomplete_results") is True:
        return "incomplete", "GitHub search timed out and returned incomplete results"
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if errors:
        types = sorted({str(error.get("type", "")) for error in errors if isinstance(error, dict)})
        messages = "; ".join(str(error.get("message", "")) for error in errors if isinstance(error, dict))
        if "RATE_LIMITED" in types:
            return "rate_limit", f"GraphQL rate limit: {messages}"
        return "api", f"GraphQL request failed ({', '.join(types) or 'unknown'}): {messages}"
    return None, ""


def months_window(today: date, months: int) -> list[date]:
    """First day of each of the last `months` calendar months, ending with today's month."""
    year, month = today.year, today.month
    firsts: list[date] = []
    for _ in range(months):
        firsts.append(date(year, month, 1))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return list(reversed(firsts))


def month_end(first: date, today: date) -> date:
    following = date(first.year + 1, 1, 1) if first.month == 12 else date(first.year, first.month + 1, 1)
    return min(following - timedelta(days=1), today)


def month_label(value: date) -> str:
    return f"{value:%Y-%m}"


def _utc_date(timestamp: Any, context: str) -> date:
    if not isinstance(timestamp, str):
        raise GitHubActivityError(f"{context} has no timestamp", kind="malformed")
    try:
        return datetime.fromisoformat(timestamp).astimezone(UTC).date()
    except ValueError as exc:
        raise GitHubActivityError(f"{context} has an invalid timestamp: {timestamp!r}", kind="malformed") from exc


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
            raise GitHubActivityError(f"{context} search result has no {'.'.join(key)}", kind="malformed")
        if identity not in seen:
            seen.add(identity)
            unique.append(item)
    return unique


def _monthly(
    client: GitHubSearchClient,
    endpoint: str,
    template: str,
    window: list[date],
    today: date,
    date_path: tuple[str, ...],
    key: tuple[str, ...],
    column: str,
    capped: dict[str, list[str]],
) -> dict[str, int]:
    """Count search results per UTC month of the timestamp at `date_path`, searching the whole window at once."""
    counts = {month_label(first): 0 for first in window}
    items, truncated = client.search_range(endpoint, template, window[0], today, key)
    if truncated:
        capped.setdefault(column, []).extend(truncated)
    for item in items:
        label = month_label(_utc_date(_field(item, date_path), f"{column} result {_field(item, key)}"))
        if label in counts:
            counts[label] += 1
    return counts


def _review_connection(node: Any, node_id: str) -> tuple[list[Any], dict[str, Any]]:
    reviews = node.get("reviews") if isinstance(node, dict) else None
    if (
        not isinstance(reviews, dict)
        or not isinstance(reviews.get("nodes"), list)
        or not isinstance(reviews.get("pageInfo"), dict)
    ):
        raise GitHubActivityError(f"Pull request {node_id} is not readable with this token", kind="forbidden")
    return reviews["nodes"], reviews["pageInfo"]


def _review_dates(
    client: GitHubSearchClient, org: str, user: str, window: list[date], today: date, capped: dict[str, list[str]]
) -> dict[str, list[date]]:
    """Submission dates of the user's non-pending reviews, per pull request someone else authored."""
    template = f"org:{org} reviewed-by:{user} -author:{user} is:pr updated:{{span}}"
    nodes, truncated = client.graphql_search_range(template, user, window[0], today)
    if truncated:
        capped.setdefault("Reviews", []).extend(truncated)
    dates: dict[str, list[date]] = {}
    for node in nodes:
        node_id = node.get("id") if isinstance(node, dict) else None
        if not isinstance(node_id, str):
            raise GitHubActivityError(
                "Review search returned a result that is not a readable pull request", kind="forbidden"
            )
        login = _field(node, ("author", "login"))
        if node_id in dates or (isinstance(login, str) and login.casefold() == user.casefold()):
            continue
        reviews, page_info = _review_connection(node, node_id)
        while page_info.get("hasNextPage"):
            data = client.graphql(
                REVIEW_PAGE_QUERY, {"id": node_id, "user": user, "after": str(page_info.get("endCursor"))}
            )
            more, page_info = _review_connection(data.get("node"), node_id)
            reviews = reviews + more
        dates[node_id] = [
            _utc_date(review.get("submittedAt"), f"Review on {node_id}")
            for review in reviews
            if isinstance(review, dict) and review.get("state") != "PENDING"
        ]
    return dates


def collect_activity(client: GitHubSearchClient, org: str, user: str, months: int, today: date) -> Activity:
    window = months_window(today, months)
    labels = [month_label(first) for first in window]
    capped: dict[str, list[str]] = {}
    authored = _monthly(
        client,
        "search/issues",
        f"org:{org} author:{user} is:pr created:{{span}}",
        window,
        today,
        ("created_at",),
        ("node_id",),
        "PRs authored",
        capped,
    )
    merged = _monthly(
        client,
        "search/issues",
        f"org:{org} author:{user} is:pr is:merged merged:{{span}}",
        window,
        today,
        ("pull_request", "merged_at"),
        ("node_id",),
        "PRs merged",
        capped,
    )
    commits = _monthly(
        client,
        "search/commits",
        f"org:{org} author:{user} author-date:{{span}}",
        window,
        today,
        ("commit", "author", "date"),
        ("sha",),
        "Commits",
        capped,
    )
    prs_reviewed = {label: 0 for label in labels}
    reviews_submitted = {label: 0 for label in labels}
    reviewed_in_window: set[str] = set()
    for node_id, dates in _review_dates(client, org, user, window, today, capped).items():
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
    columns = ("authored", "merged", "commits", "prs_reviewed", "reviews_submitted")
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
    for column, days in sorted(activity.capped.items()):
        if column == "Reviews":
            lines.append(
                "- WARNING: the review search exceeded GitHub's 1000-result Search API cap for pull requests "
                f"last updated on {', '.join(days)}. Their reviews can fall in any month, so PRs reviewed and "
                "Reviews submitted may be undercounted in every month and in both totals."
            )
        else:
            lines.append(
                f"- WARNING: {column} for {', '.join(days)} exceeded GitHub's 1000-result Search API cap; "
                "those counts are lower than the true totals."
            )
    return "\n".join(lines) + "\n"


def _login(value: str) -> str:
    if not LOGIN_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(f"not a valid GitHub login: {value!r}")
    return value


def _months(value: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= MAX_MONTHS:
        raise argparse.ArgumentTypeError(f"must be an integer from 1 to {MAX_MONTHS}")
    return int(value)


def main(
    arguments: list[str] | None = None, client: GitHubSearchClient | None = None, today: date | None = None
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", type=_login, required=True)
    parser.add_argument("--user", type=_login, required=True)
    parser.add_argument("--months", type=_months, default=12)
    options = parser.parse_args(arguments)
    current = today or datetime.now(UTC).date()
    print(
        f"Querying GitHub for {options.months} month(s); searches are spaced {SEARCH_INTERVAL}s apart.", file=sys.stderr
    )
    try:
        activity = collect_activity(client or GitHubSearchClient(), options.org, options.user, options.months, current)
    except GitHubActivityError as exc:
        # The reason can carry gh's multi-line stderr; the FAILED line stays one line.
        print(f"FAILED {' '.join(str(exc).split())} [{exc.kind}]")
        return 1
    sys.stdout.write(render_report(activity, options.org, options.user, current))
    return 0


if __name__ == "__main__":
    sys.exit(main())

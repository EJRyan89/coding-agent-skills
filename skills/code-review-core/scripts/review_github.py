"""Pull-request-shaped GitHub reads for the code-review pipeline, over skill-core's GitHub CLI client."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import github_client
from github_client import Clock, GitHubError, Runner, Sleeper, subprocess_runner
from review_config import validate_repository_identity

PULL_PAGE_SIZE = 100  # the most GitHub's REST API returns per page


@dataclass(frozen=True)
class PullListing:
    """The pull requests a listing found and the number of pages it read."""

    pulls: list[dict[str, Any]]
    pages: int


def _updated_date(value: Any) -> date:
    try:
        return date.fromisoformat(value["updated_at"][:10])
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubError("Pull response has no valid update time", kind="malformed") from exc


def _normalize_pull(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        raise GitHubError("Pull response has an unexpected shape", kind="malformed")
    try:
        state = value["state"]
        merged_at = value["merged_at"]
        base = value["base"]
        head = value["head"]
        if state == "open":
            normalized_state = "OPEN"
        elif state == "closed" and isinstance(merged_at, str) and merged_at:
            normalized_state = "MERGED"
        elif state == "closed" and merged_at is None:
            return None
        else:
            raise KeyError("state")
        normalized = {
            "number": value["number"],
            "title": value["title"],
            "url": value["html_url"],
            "state": normalized_state,
            "isDraft": value["draft"],
            "baseRefName": base["ref"],
            "baseRefOid": base["sha"],
            "headRefOid": head["sha"],
            "headRefName": head["ref"],
            "mergedAt": merged_at,
        }
    except (KeyError, TypeError) as exc:
        raise GitHubError("Pull response has an unexpected shape", kind="malformed") from exc
    return normalized


REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved isOutdated path line originalLine
          comments(first: 1) { nodes { body url author { __typename login } } }
        }
      }
    }
  }
}
""".strip()


class GitHubClient:
    """The pull request reads the pipeline and the tracker make, through skill-core's GitHub CLI client.

    That client replaces each byte that is not UTF-8 with U+FFFD, because every caller writes, fingerprints, or
    prints what it gets, and waits out rate limits with the one backoff policy every skill shares.
    """

    def __init__(
        self, runner: Runner = subprocess_runner, *, sleeper: Sleeper | None = None, clock: Clock | None = None
    ) -> None:
        self.github = github_client.GitHubClient(runner, sleeper=sleeper, clock=clock)

    @property
    def call_count(self) -> int:
        """The gh commands this client has run, retries included."""
        return self.github.call_count

    def api_json(self, endpoint: str, *, paginate: bool = False, allow_absent: bool = False) -> Any:
        arguments = ["api"]
        if paginate:
            arguments.extend(["--paginate", "--slurp"])
        arguments.append(endpoint)
        try:
            return self.github.json(arguments)
        except GitHubError as exc:
            if allow_absent and exc.kind == "not_found":
                return None
            raise

    def authenticated_login(self) -> str:
        """The login of the account GitHub CLI is authenticated as."""
        user = self.api_json("user")
        if not isinstance(user, dict) or not isinstance(user.get("login"), str) or not user["login"]:
            raise GitHubError("Authenticated user response has an unexpected shape", kind="malformed")
        return user["login"]

    def graphql_nodes(
        self, query: str, variables: dict[str, str | int], connection: Sequence[str]
    ) -> list[dict[str, Any]]:
        """Every node of one paginated GraphQL connection, or an error; never a partial list.

        The query must declare `$after: String` and select `pageInfo { hasNextPage endCursor }` and
        `nodes` on the connection found by following `connection` from `data`. GraphQL errors, a
        missing connection, a malformed page, and a cursor that does not advance all fail. Integer
        variables are sent typed (`-F`), so they can fill `Int` parameters; strings are sent raw (`-f`).
        """
        nodes: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            arguments = ["api", "graphql", "-f", f"query={query}"]
            for key, value in variables.items():
                flag = "-F" if isinstance(value, int) and not isinstance(value, bool) else "-f"
                arguments.extend([flag, f"{key}={value}"])
            if cursor is not None:
                arguments.extend(["-f", f"after={cursor}"])
            response = self.github.json(arguments, graphql=True)
            if not isinstance(response, dict):
                raise GitHubError("GraphQL response has an unexpected shape", kind="malformed")
            page: Any = response.get("data")
            for key in connection:
                if not isinstance(page, dict):
                    raise GitHubError("GraphQL response has an unexpected shape", kind="malformed")
                page = page.get(key)
                if page is None:
                    raise GitHubError(f"GraphQL {'.'.join(connection)} was not found", kind="not_found")
            info = page.get("pageInfo") if isinstance(page, dict) else None
            if (
                not isinstance(page, dict)
                or not isinstance(page.get("nodes"), list)
                or any(not isinstance(node, dict) for node in page["nodes"])
                or not isinstance(info, dict)
                or not isinstance(info.get("hasNextPage"), bool)
            ):
                raise GitHubError("GraphQL page has an unexpected shape", kind="malformed")
            nodes.extend(page["nodes"])
            if not info["hasNextPage"]:
                return nodes
            cursor = info.get("endCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise GitHubError("GraphQL pagination did not advance", kind="malformed")
            seen.add(cursor)

    def api_text(self, endpoint: str, *, accept: str) -> tuple[str, int]:
        """The response as text, and how many of its bytes were not UTF-8 and became U+FFFD."""
        result = self.github.run(["api", "-H", f"Accept: {accept}", endpoint])
        return result.stdout, result.replaced

    def get_pull_diff(self, repository: str, number: int) -> tuple[str, int]:
        """The pull request's unified diff as GitHub computes it against the merge base, and how many undecodable
        bytes in it became U+FFFD."""
        repository = validate_repository_identity(repository)
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise GitHubError("Pull number must be positive", kind="input")
        return self.api_text(f"repos/{repository}/pulls/{number}", accept="application/vnd.github.diff")

    def list_open_review_threads(self, repository: str, number: int) -> list[dict[str, Any]]:
        """Unresolved review threads a person started, as C1, C2, ... in thread order.

        Each is the thread's first comment: the request a reviewer must give a disposition for. Resolved
        threads were already handled, and threads started by a bot (including AI reviewers) are not a
        person's request. A deleted account counts as a person.
        """
        repository = validate_repository_identity(repository)
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise GitHubError("Pull number must be positive", kind="input")
        owner, name = repository.split("/", 1)
        threads = self.graphql_nodes(
            REVIEW_THREADS_QUERY,
            {"owner": owner, "name": name, "number": number},
            ("repository", "pullRequest", "reviewThreads"),
        )
        comments: list[dict[str, Any]] = []
        for thread in threads:
            try:
                if thread["isResolved"]:
                    continue
                first = thread["comments"]["nodes"][0] if thread["comments"]["nodes"] else None
                if first is None:
                    continue
                author = first["author"]
                if author is not None and author["__typename"] != "User" and author["__typename"] != "Mannequin":
                    continue
                comments.append(
                    {
                        "id": f"C{len(comments) + 1}",
                        "author": author["login"] if author is not None else "ghost",
                        "path": thread["path"],
                        "line": thread["line"] or thread["originalLine"],
                        "outdated": bool(thread["isOutdated"]),
                        "body": first["body"],
                        "url": first["url"],
                    }
                )
            except (KeyError, TypeError, IndexError) as exc:
                raise GitHubError("Review thread has an unexpected shape", kind="malformed") from exc
        return comments

    def list_pulls(self, repository: str, *, state: str) -> PullListing:
        """Every pull request in `state`, through every page; closed pull requests that were not merged are dropped."""
        repository = validate_repository_identity(repository)
        if state not in {"open", "closed", "all"}:
            raise GitHubError(f"Invalid pull state: {state}", kind="input")
        pages = self.api_json(f"repos/{repository}/pulls?state={state}&per_page={PULL_PAGE_SIZE}", paginate=True)
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise GitHubError("Paginated pull response has an unexpected shape", kind="malformed")
        pulls = [_normalize_pull(pull) for page in pages for pull in page]
        return PullListing([pull for pull in pulls if pull is not None], len(pages))

    def list_closed_pulls_since(self, repository: str, since: date) -> PullListing:
        """The merged pull requests updated on or after `since`, most recently updated first, and possibly some older.

        Pages are read sorted by update time until one ends with a pull request updated before `since`, or is short.
        Merging updates a pull request, so every one merged on or after `since` is on a page read. A walk of several
        pages reads the first page again at the end, because a pull request updated during the walk moves to it, and
        a pull request seen twice is kept once, as first seen.
        """
        repository = validate_repository_identity(repository)
        endpoint = f"repos/{repository}/pulls?state=closed&sort=updated&direction=desc&per_page={PULL_PAGE_SIZE}"
        raw: list[Any] = []
        pages = 0
        while True:
            pages += 1
            page = self._closed_page(endpoint, pages)
            raw.extend(page)
            if len(page) < PULL_PAGE_SIZE or _updated_date(page[-1]) < since:
                break
        if pages > 1:
            raw.extend(self._closed_page(endpoint, 1))
            pages += 1
        pulls: dict[int, dict[str, Any]] = {}
        for value in raw:
            pull = _normalize_pull(value)
            if pull is not None:
                pulls.setdefault(pull["number"], pull)
        return PullListing(list(pulls.values()), pages)

    def _closed_page(self, endpoint: str, page: int) -> list[Any]:
        values = self.api_json(f"{endpoint}&page={page}")
        if not isinstance(values, list):
            raise GitHubError("Pull page has an unexpected shape", kind="malformed")
        return values

    def get_pull(self, repository: str, number: int) -> dict[str, Any]:
        repository = validate_repository_identity(repository)
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise GitHubError("Pull number must be positive", kind="input")
        pull = _normalize_pull(self.api_json(f"repos/{repository}/pulls/{number}"))
        if pull is None:
            raise GitHubError("Pull request is closed without being merged", kind="ineligible")
        return pull

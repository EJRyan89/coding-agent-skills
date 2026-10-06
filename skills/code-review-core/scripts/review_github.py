"""Fail-closed GitHub CLI wrapper with explicit pagination."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from review_config import validate_repository_identity


class GitHubError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "api") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], CommandResult]


def subprocess_runner(arguments: Sequence[str]) -> CommandResult:
    """Run a command; output that is not UTF-8 is kept losslessly, one lone surrogate per undecodable byte."""
    try:
        process = subprocess.run(
            list(arguments),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitHubError(
            "GitHub CLI executable 'gh' was not found; install GitHub CLI and "
            "authenticate before running code-review operations",
            kind="prerequisite",
        ) from exc
    except OSError as exc:
        raise GitHubError(f"GitHub CLI could not be started: {exc}", kind="execution") from exc
    return CommandResult(process.returncode, process.stdout, process.stderr)


# surrogateescape decodes each byte that is not UTF-8 to one of these, and never yields one otherwise.
_UNDECODABLE = re.compile("[\udc80-\udcff]")


def replace_undecodable(text: str) -> tuple[str, int]:
    """`text` with each undecodable byte the runner kept as U+FFFD, and how many bytes were replaced."""
    return _UNDECODABLE.subn("\ufffd", text)


def _classify_failure(stderr: str) -> str:
    lowered = stderr.casefold()
    if "rate limit" in lowered or "http 429" in lowered:
        return "rate_limit"
    if "http 401" in lowered or "authentication" in lowered or "not logged" in lowered:
        return "authentication"
    if "http 403" in lowered:
        return "forbidden"
    if "http 404" in lowered or "not found" in lowered:
        return "not_found"
    return "api"


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
    def __init__(self, runner: Runner = subprocess_runner) -> None:
        self.runner = runner

    def _run(self, arguments: Sequence[str]) -> tuple[CommandResult, int]:
        """Run gh with its output made valid Unicode, and how many undecodable bytes stdout held.

        GitHub output is untrusted bytes: a pull request's diff carries files in any encoding, and every caller
        writes, fingerprints, or prints what it gets, so nothing that is not UTF-8 gets past this point.
        """
        result = self.runner(arguments)
        stdout, replaced = replace_undecodable(result.stdout)
        return CommandResult(result.returncode, stdout, replace_undecodable(result.stderr)[0]), replaced

    def api_json(self, endpoint: str, *, paginate: bool = False, allow_absent: bool = False) -> Any:
        arguments = ["gh", "api"]
        if paginate:
            arguments.extend(["--paginate", "--slurp"])
        arguments.append(endpoint)
        result, _ = self._run(arguments)
        if result.returncode != 0:
            kind = _classify_failure(result.stderr)
            if allow_absent and kind == "not_found":
                return None
            raise GitHubError(result.stderr.strip() or "GitHub API request failed", kind=kind)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubError(f"GitHub returned malformed JSON: {exc}", kind="malformed") from exc

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
            arguments = ["gh", "api", "graphql", "-f", f"query={query}"]
            for key, value in variables.items():
                flag = "-F" if isinstance(value, int) and not isinstance(value, bool) else "-f"
                arguments.extend([flag, f"{key}={value}"])
            if cursor is not None:
                arguments.extend(["-f", f"after={cursor}"])
            result, _ = self._run(arguments)
            if result.returncode != 0:
                raise GitHubError(
                    result.stderr.strip() or "GitHub GraphQL request failed", kind=_classify_failure(result.stderr)
                )
            try:
                response = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise GitHubError(f"GitHub returned malformed JSON: {exc}", kind="malformed") from exc
            if not isinstance(response, dict):
                raise GitHubError("GraphQL response has an unexpected shape", kind="malformed")
            errors = response.get("errors")
            if errors:
                errors = errors if isinstance(errors, list) else [errors]
                messages = [
                    str(error.get("message", error)) if isinstance(error, dict) else str(error) for error in errors
                ]
                types = {error.get("type") for error in errors if isinstance(error, dict)}
                kind = (
                    "rate_limit"
                    if "RATE_LIMITED" in types
                    else "not_found"
                    if "NOT_FOUND" in types
                    else "forbidden"
                    if "FORBIDDEN" in types
                    else "api"
                )
                raise GitHubError("GraphQL: " + "; ".join(messages), kind=kind)
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
        result, replaced = self._run(["gh", "api", "-H", f"Accept: {accept}", endpoint])
        if result.returncode != 0:
            raise GitHubError(
                result.stderr.strip() or "GitHub API request failed", kind=_classify_failure(result.stderr)
            )
        return result.stdout, replaced

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
        comments = []
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

    def list_pulls(self, repository: str, *, state: str) -> list[dict[str, Any]]:
        repository = validate_repository_identity(repository)
        if state not in {"open", "closed", "all"}:
            raise GitHubError(f"Invalid pull state: {state}", kind="input")
        pages = self.api_json(f"repos/{repository}/pulls?state={state}&per_page=100", paginate=True)
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise GitHubError("Paginated pull response has an unexpected shape", kind="malformed")
        pulls = [_normalize_pull(pull) for page in pages for pull in page]
        return [pull for pull in pulls if pull is not None]

    def get_pull(self, repository: str, number: int) -> dict[str, Any]:
        repository = validate_repository_identity(repository)
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise GitHubError("Pull number must be positive", kind="input")
        pull = _normalize_pull(self.api_json(f"repos/{repository}/pulls/{number}"))
        if pull is None:
            raise GitHubError("Pull request is closed without being merged", kind="ineligible")
        return pull

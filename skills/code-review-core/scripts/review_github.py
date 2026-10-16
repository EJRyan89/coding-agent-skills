"""Pull-request-shaped GitHub reads for the code-review pipeline, over skill-core's GitHub CLI client."""

from __future__ import annotations

import base64
import binascii
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import github_client
from github_client import Clock, GitHubError, Runner, Sleeper, subprocess_runner
from review_config import validate_repository_identity
from review_state import utc_date
from typed_fields import FieldError, FieldReader

PULL_PAGE_SIZE = 100  # the most GitHub's REST API returns per page


class GitHubFieldError(FieldError, GitHubError):
    """A GitHub response field of the wrong kind: a malformed response."""

    kind_of_error = "malformed"

    def __init__(self, path: str, expected: str, found: str) -> None:
        super().__init__(path, expected, found)
        # GitHubError's own __init__ comes after FieldError's in the method order, so its attributes are set here.
        self.kind = self.kind_of_error
        self.retryable = False
        self.returncode = None


class GitHubInputError(GitHubFieldError):
    """An argument of the wrong kind, given to a read before anything is asked of GitHub."""

    kind_of_error = "input"


READ = FieldReader(GitHubFieldError)


ARGUMENTS = FieldReader(GitHubInputError)


@dataclass(frozen=True)
class PullListing:
    """The pull requests a listing found and the number of pages it read."""

    pulls: list[dict[str, Any]]
    pages: int


def _updated_date(value: Any) -> date:
    updated = READ.string(READ.json_object(value, "pull").get("updated_at"), "pull.updated_at")
    try:
        return utc_date(updated)
    except ValueError as exc:
        raise GitHubError("Pull response has no valid update time", kind="malformed") from exc


def _normalize_pull(value: Any) -> dict[str, Any] | None:
    pull = READ.json_object(value, "pull")
    state = READ.one_of(pull.get("state"), "pull.state", ("open", "closed"))
    merged_at = READ.optional(READ.string, pull.get("merged_at"), "pull.merged_at")
    if state == "closed" and merged_at is None:
        return None
    base = READ.json_object(pull.get("base"), "pull.base")
    head = READ.json_object(pull.get("head"), "pull.head")
    try:
        return {
            "number": pull["number"],
            "title": pull["title"],
            "url": pull["html_url"],
            "state": "OPEN" if state == "open" else "MERGED",
            "isDraft": pull["draft"],
            "baseRefName": base["ref"],
            "baseRefOid": base["sha"],
            "headRefOid": head["sha"],
            "headRefName": head["ref"],
            "mergedAt": merged_at,
        }
    except KeyError as exc:
        raise GitHubError("Pull response has an unexpected shape", kind="malformed") from exc


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
        return READ.string(READ.json_object(self.api_json("user"), "user").get("login"), "user.login")

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
            path = "graphql response.data"
            page: Any = READ.json_object(self.github.json(arguments, graphql=True), "graphql response").get("data")
            for key in connection:
                page = READ.json_object(page, path).get(key)
                path = f"{path}.{key}"
                if page is None:
                    raise GitHubError(f"GraphQL {'.'.join(connection)} was not found", kind="not_found")
            page = READ.json_object(page, path)
            nodes.extend(READ.array(page.get("nodes"), f"{path}.nodes", READ.json_object))
            info = READ.json_object(page.get("pageInfo"), f"{path}.pageInfo")
            if not READ.boolean(info.get("hasNextPage"), f"{path}.pageInfo.hasNextPage"):
                return nodes
            cursor = READ.string(info.get("endCursor"), f"{path}.pageInfo.endCursor")
            if cursor in seen:
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
        ARGUMENTS.integer(number, "pull number", minimum=1)
        return self.api_text(f"repos/{repository}/pulls/{number}", accept="application/vnd.github.diff")

    def get_merge_base(self, repository: str, base: str, head: str) -> str:
        """The commit GitHub diffs a pull request's head against: the merge base of its base and head commits."""
        repository = validate_repository_identity(repository)
        ARGUMENTS.hex_digest(base, "base commit", 40, 64)
        ARGUMENTS.hex_digest(head, "head commit", 40, 64)
        compare = READ.json_object(self.api_json(f"repos/{repository}/compare/{base}...{head}?per_page=1"), "compare")
        commit = READ.json_object(compare.get("merge_base_commit"), "compare.merge_base_commit")
        return READ.hex_digest(commit.get("sha"), "compare.merge_base_commit.sha", 40, 64)

    def get_file_blob(self, repository: str, commit: str, path: str, *, maximum: int) -> tuple[str, bytes | None]:
        """The blob id of the file at `path` in `commit`, and its bytes, or None for a file over `maximum` bytes, whose
        content is never fetched."""
        repository = validate_repository_identity(repository)
        ARGUMENTS.hex_digest(commit, "commit", 40, 64)
        entry = READ.json_object(
            self.api_json(f"repos/{repository}/contents/{quote(path, safe='/')}?ref={commit}"), "contents"
        )
        if entry.get("type") != "file":
            raise GitHubError("Contents response is not one file", kind="malformed")
        sha = READ.hex_digest(entry.get("sha"), "contents.sha", 40, 64)
        if READ.integer(entry.get("size"), "contents.size", minimum=0) > maximum:
            return sha, None
        blob = READ.json_object(self.api_json(f"repos/{repository}/git/blobs/{sha}"), "blob")
        if blob.get("encoding") != "base64":
            raise GitHubError("Blob response has an unexpected shape", kind="malformed")
        content = READ.string(blob.get("content"), "blob.content", empty=True)
        try:
            return sha, base64.b64decode(content)
        except (binascii.Error, ValueError) as exc:
            raise GitHubError("Blob response is not base64", kind="malformed") from exc

    def list_open_review_threads(self, repository: str, number: int) -> list[dict[str, Any]]:
        """Unresolved review threads a person started, as C1, C2, ... in thread order.

        Each is the thread's first comment: the request a reviewer must give a disposition for. Resolved
        threads were already handled, and threads started by a bot (including AI reviewers) are not a
        person's request. A deleted account counts as a person.
        """
        repository = validate_repository_identity(repository)
        ARGUMENTS.integer(number, "pull number", minimum=1)
        owner, name = repository.split("/", 1)
        threads = self.graphql_nodes(
            REVIEW_THREADS_QUERY,
            {"owner": owner, "name": name, "number": number},
            ("repository", "pullRequest", "reviewThreads"),
        )
        comments: list[dict[str, Any]] = []
        for index, thread in enumerate(threads):
            comment = _open_thread_comment(thread, f"review threads[{index}]")
            if comment is not None:
                comments.append({"id": f"C{len(comments) + 1}", **comment})
        return comments

    def list_pulls(self, repository: str, *, state: str) -> PullListing:
        """Every pull request in `state`, through every page; closed pull requests that were not merged are dropped."""
        repository = validate_repository_identity(repository)
        ARGUMENTS.one_of(state, "pull state", ("open", "closed", "all"))
        pages = self.api_json(f"repos/{repository}/pulls?state={state}&per_page={PULL_PAGE_SIZE}", paginate=True)
        listed = READ.array(pages, "pull pages", lambda page, path: READ.array(page, path))
        pulls = [_normalize_pull(pull) for page in listed for pull in page]
        return PullListing([pull for pull in pulls if pull is not None], len(listed))

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
        return READ.array(self.api_json(f"{endpoint}&page={page}"), "pull page")

    def get_pull(self, repository: str, number: int) -> dict[str, Any]:
        repository = validate_repository_identity(repository)
        ARGUMENTS.integer(number, "pull number", minimum=1)
        value = self.api_json(f"repos/{repository}/pulls/{number}")
        pull = _normalize_pull(value)
        if pull is None:
            raise GitHubError("Pull request is closed without being merged", kind="ineligible")
        # Only the pull request a review reads carries its description: a listing's pulls are kept in the batch file,
        # where an author's text has no place.
        body = READ.optional(READ.string, value.get("body"), "pull.body", empty=True)
        return {**pull, "body": body or ""}


def _open_thread_comment(thread: dict[str, Any], path: str) -> dict[str, Any] | None:
    """An unresolved review thread's first comment, when a person wrote it: its author, place, and text."""
    if READ.boolean(thread.get("isResolved"), f"{path}.isResolved"):
        return None
    comments = READ.json_object(thread.get("comments"), f"{path}.comments")
    nodes = READ.array(comments.get("nodes"), f"{path}.comments.nodes", READ.json_object)
    if not nodes:
        return None
    first = nodes[0]
    where = f"{path}.comments.nodes[0]"
    author = READ.optional(READ.json_object, first.get("author"), f"{where}.author")
    if author is not None and READ.string(author.get("__typename"), f"{where}.author.__typename") not in (
        "User",
        "Mannequin",
    ):
        return None
    line = READ.optional(READ.integer, thread.get("line"), f"{path}.line")
    return {
        "author": "ghost" if author is None else READ.string(author.get("login"), f"{where}.author.login"),
        "path": READ.string(thread.get("path"), f"{path}.path"),
        "line": line or READ.optional(READ.integer, thread.get("originalLine"), f"{path}.originalLine"),
        "outdated": READ.boolean(thread.get("isOutdated"), f"{path}.isOutdated"),
        "body": READ.string(first.get("body"), f"{where}.body", empty=True),
        "url": READ.string(first.get("url"), f"{where}.url"),
    }

"""code-review-core's GitHub reads: pull requests, review comments, and changed documents."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import review_github
from github_client import CommandResult
from review_github import GitHubClient, GitHubError, GitHubFieldError, GitHubInputError
from review_operation import ReviewOperationFieldError, validate_pull
from test_review_records import NOT_HEX, OBJECT_ID, field_message, field_pattern

INVALID_COMMITS = (
    ("main", "a string of 4 characters"),
    ("", "a string of 0 characters"),
    ("a" * 39, "a string of 39 characters"),
    ("a" * 41, "a string of 41 characters"),
    ("a" * 63, "a string of 63 characters"),
    ("a" * 65, "a string of 65 characters"),
    ("A" * 40, NOT_HEX),
    ("g" * 64, NOT_HEX),
    ("a" * 40 + "\n", "a string of 41 characters"),
)


def answering(result: CommandResult) -> Callable[[Sequence[str]], CommandResult]:
    """A command runner that answers every command with the result."""
    return lambda _arguments: result


class GitHubTests(unittest.TestCase):
    @staticmethod
    def _api_pull(number: int, *, state: str = "open", merged_at: str | None = None) -> dict:
        return {
            "number": number,
            "title": f"Pull {number}",
            "html_url": f"https://github.com/example/one/pull/{number}",
            "state": state,
            "draft": False,
            "base": {"ref": "main", "sha": "a" * 40},
            "head": {"ref": f"pull-{number}", "sha": "b" * 40},
            "merged_at": merged_at,
        }

    def test_missing_cli_fails_with_prerequisite_error(self) -> None:
        with (
            mock.patch("github_client.run_bounded", side_effect=FileNotFoundError("gh was not found")),
            self.assertRaises(GitHubError) as context,
        ):
            review_github.subprocess_runner(["gh", "api", "user"])
        self.assertEqual("prerequisite", context.exception.kind)
        self.assertIn("install GitHub CLI", str(context.exception))

    def test_pagination_is_flattened(self) -> None:
        calls: list[list[str]] = []

        def runner(arguments: Sequence[str]) -> CommandResult:
            calls.append(list(arguments))
            return CommandResult(0, json.dumps([[self._api_pull(1)], [self._api_pull(2)]]), "")

        listing = GitHubClient(runner).list_pulls("example/one", state="open")
        self.assertEqual([1, 2], [item["number"] for item in listing.pulls])
        self.assertEqual(2, listing.pages)
        self.assertIn("--paginate", calls[0])
        self.assertIn("--slurp", calls[0])

    @staticmethod
    def _closed_page(numbers: range | list[int], updated: str, *, merged: str | None = None) -> list[dict]:
        pulls = []
        for number in numbers:
            value = GitHubTests._api_pull(number, state="closed", merged_at=merged or updated)
            value["updated_at"] = updated
            pulls.append(value)
        return pulls

    def _walk(self, pages: dict[int, Any], since: date) -> tuple[review_github.PullListing, list[list[str]]]:
        calls: list[list[str]] = []

        def runner(arguments: Sequence[str]) -> CommandResult:
            calls.append(list(arguments))
            page = int(arguments[-1].rsplit("&page=", 1)[1])
            return CommandResult(0, json.dumps(pages.get(page, [])), "")

        return GitHubClient(runner).list_closed_pulls_since("example/one", since), calls

    def test_closed_walk_stops_at_the_first_page_behind_the_watermark(self) -> None:
        pages = {
            1: self._closed_page(range(300, 200, -1), "2026-03-09T10:00:00Z"),
            2: self._closed_page(range(200, 100, -1), "2026-02-27T10:00:00Z"),
            3: self._closed_page(range(100, 0, -1), "2026-01-01T10:00:00Z"),
        }
        listing, calls = self._walk(pages, date(2026, 3, 1))
        # Page 2 ends before the watermark, so page 3 is never read; page 1 is read again after a walk of two pages.
        self.assertEqual(
            [
                "repos/example/one/pulls?state=closed&sort=updated&direction=desc&per_page=100&page=1",
                "repos/example/one/pulls?state=closed&sort=updated&direction=desc&per_page=100&page=2",
                "repos/example/one/pulls?state=closed&sort=updated&direction=desc&per_page=100&page=1",
            ],
            [call[-1] for call in calls],
        )
        self.assertTrue(all("--paginate" not in call for call in calls))
        self.assertEqual(3, listing.pages)
        self.assertEqual(list(range(300, 100, -1)), [pull["number"] for pull in listing.pulls])

    def test_closed_walk_reads_one_page_when_it_reaches_the_watermark_or_the_end(self) -> None:
        cases: dict[str, dict[int, Any]] = {
            "behind the watermark": {1: self._closed_page(range(100, 0, -1), "2026-02-01T10:00:00Z")},
            "short page": {1: self._closed_page(range(5, 0, -1), "2026-03-05T10:00:00Z")},
            "no closed pull": {1: []},
        }
        for name, pages in cases.items():
            with self.subTest(name):
                listing, calls = self._walk(pages, date(2026, 3, 1))
                self.assertEqual(1, len(calls))
                self.assertEqual(1, listing.pages)

    def test_closed_walk_keeps_a_page_that_ends_on_the_watermark_day(self) -> None:
        pages = {
            1: self._closed_page(range(200, 100, -1), "2026-03-01T00:00:00Z"),
            2: self._closed_page(range(100, 98, -1), "2026-02-28T23:59:59Z"),
        }
        listing, calls = self._walk(pages, date(2026, 3, 1))
        self.assertEqual(["page=1", "page=2", "page=1"], [call[-1].rsplit("&", 1)[1] for call in calls])
        self.assertEqual(102, len(listing.pulls))

    def test_closed_walk_reads_update_times_as_utc_dates(self) -> None:
        # The first page ends just before the watermark's UTC day, though its local date east of UTC is already that
        # day, so the walk stops; one that ends just after it, on the previous local day west of UTC, reads on.
        for updated, reads in (("2026-03-01T01:00:00+02:00", 1), ("2026-02-28T20:00:00-05:00", 3)):
            with self.subTest(updated=updated):
                pages = {
                    1: self._closed_page(range(200, 100, -1), updated),
                    2: self._closed_page(range(100, 98, -1), "2026-02-20T10:00:00Z"),
                }
                _, calls = self._walk(pages, date(2026, 3, 1))
                self.assertEqual(reads, len(calls))

    def test_closed_walk_finds_a_pull_updated_onto_a_page_it_already_read(self) -> None:
        # Pull 50 is merged while page 1 is being read, so it moves from page 2 to the top of page 1, and page 1's last
        # pull shifts onto page 2: the second page repeats it, and the reread of page 1 finds pull 50.
        first = self._closed_page(range(300, 200, -1), "2026-03-09T10:00:00Z")
        moved = self._closed_page([50], "2026-03-10T10:00:00Z")
        reads = {1: 0}
        calls: list[str] = []

        def runner(arguments: Sequence[str]) -> CommandResult:
            calls.append(arguments[-1])
            page = int(arguments[-1].rsplit("&page=", 1)[1])
            if page == 1:
                reads[1] += 1
                return CommandResult(0, json.dumps(first if reads[1] == 1 else moved + first[:99]), "")
            shifted = first[99:] + self._closed_page(range(200, 101, -1), "2026-02-27T10:00:00Z")
            return CommandResult(0, json.dumps(shifted if page == 2 else []), "")

        listing = GitHubClient(runner).list_closed_pulls_since("example/one", date(2026, 3, 1))
        numbers = [pull["number"] for pull in listing.pulls]
        self.assertEqual(len(numbers), len(set(numbers)))
        self.assertIn(50, numbers)
        self.assertEqual(3, listing.pages)

    def test_closed_walk_drops_closed_unmerged_pulls_but_pages_past_them(self) -> None:
        page = self._closed_page(range(200, 100, -1), "2026-03-05T10:00:00Z")
        for value in page[:50]:
            value["merged_at"] = None
        listing, calls = self._walk({1: page}, date(2026, 3, 1))
        self.assertEqual(3, len(calls))  # a full page that reaches the watermark is followed by page 2
        self.assertEqual(list(range(150, 100, -1)), [pull["number"] for pull in listing.pulls])
        self.assertTrue(all(pull["state"] == "MERGED" for pull in listing.pulls))

    def test_closed_walk_fails_on_a_page_it_cannot_read(self) -> None:
        for name, page in {
            "not a list": {"message": "unexpected"},
            "no update time": [
                {k: v for k, v in p.items() if k != "updated_at"}
                for p in self._closed_page(range(100, 0, -1), "2026-03-05T10:00:00Z")
            ],
            "bad update time": self._closed_page(range(100, 0, -1), "yesterday"),
        }.items():
            with self.subTest(name), self.assertRaises(GitHubError) as context:
                self._walk({1: page}, date(2026, 3, 1))
            self.assertEqual("malformed", context.exception.kind)

    @staticmethod
    def _graphql_page(numbers: list[int], cursor: str | None) -> str:
        return json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequests": {
                            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                            "nodes": [{"number": number} for number in numbers],
                        }
                    }
                }
            }
        )

    def test_graphql_connection_is_paginated_until_complete(self) -> None:
        calls: list[list[str]] = []
        pages = {None: self._graphql_page([1, 2], "c1"), "c1": self._graphql_page([3], None)}

        def runner(arguments: Sequence[str]) -> CommandResult:
            calls.append(list(arguments))
            after = next((value.removeprefix("after=") for value in arguments if value.startswith("after=")), None)
            return CommandResult(0, pages[after], "")

        nodes = GitHubClient(runner).graphql_nodes(
            "query($after: String) { x }", {"owner": "example", "name": "one"}, ("repository", "pullRequests")
        )
        self.assertEqual([1, 2, 3], [node["number"] for node in nodes])
        self.assertEqual(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                "query=query($after: String) { x }",
                "-f",
                "owner=example",
                "-f",
                "name=one",
            ],
            calls[0],
        )
        self.assertEqual(["-f", "after=c1"], calls[1][-2:])

    def test_graphql_failures_never_return_partial_nodes(self) -> None:
        connection = ("repository", "pullRequests")
        cases = (
            (
                CommandResult(0, json.dumps({"errors": [{"type": "RATE_LIMITED", "message": "slow down"}]}), ""),
                "rate_limit",
                "slow down",
            ),
            (CommandResult(0, json.dumps({"data": {"repository": None}}), ""), "not_found", "was not found"),
            (
                CommandResult(0, json.dumps({"data": {"repository": {"pullRequests": {"nodes": []}}}}), ""),
                "malformed",
                field_pattern("graphql response.data.repository.pullRequests.pageInfo", "an object", "null"),
            ),
            (CommandResult(0, self._graphql_page([1], "same"), ""), "malformed", "did not advance"),
            (CommandResult(1, "", "HTTP 401: Bad credentials"), "authentication", "Bad credentials"),
            (CommandResult(0, "not-json", ""), "malformed", "malformed JSON"),
        )
        for response, kind, message in cases:
            with self.subTest(kind=kind, message=message):
                waits: list[float] = []
                client = GitHubClient(answering(response), sleeper=waits.append)
                with self.assertRaisesRegex(GitHubError, message) as context:
                    client.graphql_nodes("query", {}, connection)
                self.assertEqual(kind, context.exception.kind)
                self.assertEqual(5 if kind == "rate_limit" else 0, len(waits))

    def test_authenticated_login_is_shape_checked(self) -> None:
        self.assertEqual(
            "octo",
            GitHubClient(lambda arguments: CommandResult(0, json.dumps({"login": "octo"}), "")).authenticated_login(),
        )
        with self.assertRaisesRegex(GitHubFieldError, field_pattern("user.login", "a non-empty string", "null")):
            GitHubClient(lambda arguments: CommandResult(0, json.dumps({}), "")).authenticated_login()

    def test_single_pull_fetch_is_explicit_and_shape_checked(self) -> None:
        calls: list[list[str]] = []

        def runner(arguments: Sequence[str]) -> CommandResult:
            calls.append(list(arguments))
            return CommandResult(0, json.dumps(self._api_pull(42)), "")

        pull = GitHubClient(runner).get_pull("Example/One", 42)
        self.assertEqual(42, pull["number"])
        self.assertEqual("repos/example/one/pulls/42", calls[0][-1])
        self.assertNotIn("--paginate", calls[0])
        with self.assertRaisesRegex(
            GitHubInputError, field_pattern("pull number", "an integer of at least 1", "integer 0")
        ) as refused:
            GitHubClient(runner).get_pull("example/one", 0)
        self.assertEqual("input", refused.exception.kind)
        malformed = GitHubClient(lambda arguments: CommandResult(0, json.dumps([]), ""))
        with self.assertRaisesRegex(GitHubFieldError, field_pattern("pull", "an object", "an array")) as context:
            malformed.get_pull("example/one", 42)
        self.assertEqual("malformed", context.exception.kind)

    def test_the_pull_a_review_reads_carries_its_description_and_a_null_one_is_empty(self) -> None:
        for body, expected in (("## Upgrade note\n\nNone\n", "## Upgrade note\n\nNone\n"), (None, ""), ("", "")):
            with self.subTest(body=body):
                value = {**self._api_pull(42), "body": body}
                served = CommandResult(0, json.dumps(value), "")

                def serve(arguments: Sequence[str], served: CommandResult = served) -> CommandResult:
                    return served

                client = GitHubClient(serve)
                pull = client.get_pull("example/one", 42)
                self.assertEqual(expected, pull["body"])
                self.assertEqual(pull, validate_pull(pull))
        value = {**self._api_pull(42), "body": ["not", "text"]}
        with self.assertRaisesRegex(GitHubFieldError, field_pattern("pull.body", "a string or null", "an array")):
            GitHubClient(lambda arguments: CommandResult(0, json.dumps(value), "")).get_pull("example/one", 42)
        with self.assertRaisesRegex(ReviewOperationFieldError, field_pattern("pull.body", "a string", "null")):
            validate_pull({**pull, "body": None})

    def test_a_response_field_of_the_wrong_kind_is_a_malformed_response(self) -> None:
        pull = self._api_pull(42)
        page = json.loads(self._graphql_page([1], "c1"))
        connection = "graphql response.data.repository.pullRequests"

        def with_page(**fields: Any) -> dict:
            changed = copy.deepcopy(page)
            changed["data"]["repository"]["pullRequests"].update(fields)
            return changed

        cases: tuple[tuple[str, Any, str, str, str], ...] = (
            ("pull", [], "pull", "an object", "an array"),
            ("pull", {**pull, "state": 1}, "pull.state", 'one of "open", "closed"', "integer 1"),
            ("pull", {**pull, "state": "OPEN"}, "pull.state", 'one of "open", "closed"', 'string "OPEN"'),
            ("pull", {**pull, "merged_at": 5}, "pull.merged_at", "a non-empty string or null", "integer 5"),
            ("pull", {**pull, "merged_at": ""}, "pull.merged_at", "a non-empty string or null", "an empty string"),
            ("pull", {**pull, "base": "main"}, "pull.base", "an object", 'string "main"'),
            ("pull", {**pull, "head": []}, "pull.head", "an object", "an array"),
            ("pull", {**pull, "body": 7}, "pull.body", "a string or null", "integer 7"),
            ("pull", {**pull, "body": {}}, "pull.body", "a string or null", "an object"),
            ("user", [], "user", "an object", "an array"),
            ("user", {"login": 5}, "user.login", "a non-empty string", "integer 5"),
            ("user", {"login": ""}, "user.login", "a non-empty string", "an empty string"),
            ("graphql", [], "graphql response", "an object", "an array"),
            ("graphql", {"data": []}, "graphql response.data", "an object", "an array"),
            ("graphql", {"data": {"repository": []}}, "graphql response.data.repository", "an object", "an array"),
            ("graphql", {"data": {"repository": {"pullRequests": "x"}}}, connection, "an object", 'string "x"'),
            ("graphql", with_page(nodes="x"), f"{connection}.nodes", "an array", 'string "x"'),
            ("graphql", with_page(nodes=[1]), f"{connection}.nodes[0]", "an object", "integer 1"),
            ("graphql", with_page(pageInfo=[]), f"{connection}.pageInfo", "an object", "an array"),
            (
                "graphql",
                with_page(pageInfo={"hasNextPage": "yes", "endCursor": None}),
                f"{connection}.pageInfo.hasNextPage",
                "a boolean",
                'string "yes"',
            ),
            (
                "graphql",
                with_page(pageInfo={"hasNextPage": True, "endCursor": 5}),
                f"{connection}.pageInfo.endCursor",
                "a non-empty string",
                "integer 5",
            ),
        )
        for read, value, path, expected, found in cases:
            with self.subTest(read=read, path=path, found=found):
                client = GitHubClient(answering(CommandResult(0, json.dumps(value), "")))
                with self.assertRaises(GitHubFieldError) as context:
                    if read == "pull":
                        client.get_pull("example/one", 42)
                    elif read == "user":
                        client.authenticated_login()
                    else:
                        client.graphql_nodes("query", {}, ("repository", "pullRequests"))
                self.assertNotIsInstance(context.exception, GitHubInputError)
                self.assertEqual(field_message(path, expected, found), str(context.exception))
                self.assertEqual("malformed", context.exception.kind)
                self.assertFalse(context.exception.retryable)

    def test_an_argument_of_the_wrong_kind_fails_before_github_is_asked(self) -> None:
        number = "an integer of at least 1"
        # Each read, the arguments it is given, and the argument of the wrong kind it names.
        cases: tuple[tuple[str, tuple[Any, ...], dict[str, Any], str, str, str], ...] = (
            ("get_pull", ("example/one", "42"), {}, "pull number", number, 'string "42"'),
            ("get_pull", ("example/one", True), {}, "pull number", number, "boolean true"),
            ("get_pull", ("example/one", 42.0), {}, "pull number", number, "number 42.0"),
            ("get_pull_diff", ("example/one", -1), {}, "pull number", number, "integer -1"),
            ("list_open_review_threads", ("example/one", 0), {}, "pull number", number, "integer 0"),
            (
                "list_pulls",
                ("example/one",),
                {"state": "merged"},
                "pull state",
                'one of "open", "closed", "all"',
                'string "merged"',
            ),
            ("get_merge_base", ("example/one", 1, "b" * 40), {}, "base commit", OBJECT_ID, "integer 1"),
            ("get_file_blob", ("example/one", None, "a.txt"), {"maximum": 1}, "commit", OBJECT_ID, "null"),
        )
        for name, arguments, options, path, expected, found in cases:
            with self.subTest(read=name, path=path, found=found):
                calls: list[list[str]] = []

                def runner(arguments: Sequence[str], calls: list[list[str]] = calls) -> CommandResult:
                    calls.append(list(arguments))
                    return CommandResult(0, "{}", "")

                with self.assertRaises(GitHubInputError) as context:
                    getattr(GitHubClient(runner), name)(*arguments, **options)
                self.assertEqual(field_message(path, expected, found), str(context.exception))
                self.assertEqual("input", context.exception.kind)
                self.assertEqual([], calls)

    def test_closed_unmerged_pulls_are_ineligible(self) -> None:
        closed = self._api_pull(42, state="closed")
        client = GitHubClient(lambda arguments: CommandResult(0, json.dumps(closed), ""))
        with self.assertRaisesRegex(GitHubError, "closed without being merged"):
            client.get_pull("example/one", 42)

    def test_api_errors_fail_closed_and_expected_404_can_be_absent(self) -> None:
        def runner(arguments: Sequence[str]) -> CommandResult:
            return CommandResult(1, "", "HTTP 404: Not Found")

        client = GitHubClient(runner)
        with self.assertRaises(GitHubError) as context:
            client.api_json("repos/example/one")
        self.assertEqual("not_found", context.exception.kind)
        self.assertIsNone(client.api_json("repos/example/one", allow_absent=True))

    def test_malformed_json_fails(self) -> None:
        client = GitHubClient(lambda arguments: CommandResult(0, "not-json", ""))
        with self.assertRaises(GitHubError) as context:
            client.api_json("repos/example/one")
        self.assertEqual("malformed", context.exception.kind)

    def test_api_failure_kinds_are_classified(self) -> None:
        limited = [5.0, 10.0, 20.0, 40.0, 80.0]
        cases: tuple[tuple[str, str, list[float]], ...] = (
            ("API rate limit exceeded", "rate_limit", limited),
            ("HTTP 429: too many requests", "rate_limit", limited),
            ("HTTP 401: authentication required", "authentication", []),
            ("not logged into GitHub", "authentication", []),
            ("HTTP 403: forbidden", "forbidden", []),
            ("HTTP 404: not found", "not_found", []),
            ("connection refused", "network", []),
            ("connection reset", "network", [2.0, 4.0]),
            ("HTTP 502: Bad Gateway", "api", [2.0, 4.0]),
            ("HTTP 500: Internal Server Error", "api", []),
        )
        for stderr, expected, retried in cases:
            with self.subTest(stderr=stderr):
                waits: list[float] = []
                client = GitHubClient(answering(CommandResult(1, "", stderr)), sleeper=waits.append)
                with self.assertRaises(GitHubError) as context:
                    client.api_json("repos/example/one")
                self.assertEqual(expected, context.exception.kind)
                # skill-core's bounded backoff waits out a rate limit, and its short retries a transient failure on
                # this read, before it fails; nothing else is waited out.
                self.assertEqual(retried, waits)

    def test_a_rate_limit_is_retried_until_it_clears(self) -> None:
        answers = [CommandResult(1, "", "gh: API rate limit exceeded (HTTP 403)"), CommandResult(0, "[]", "")]
        calls: list[list[str]] = []

        def runner(arguments: Sequence[str]) -> CommandResult:
            calls.append(list(arguments))
            return answers.pop(0)

        waits: list[float] = []
        self.assertEqual([], GitHubClient(runner, sleeper=waits.append).api_json("repos/example/one/pulls"))
        self.assertEqual([5.0], waits)
        self.assertEqual([["gh", "api", "repos/example/one/pulls"]] * 2, calls)

    def test_runner_decodes_bytes_that_are_not_utf8_without_losing_them(self) -> None:
        result = review_github.subprocess_runner(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'caf\\xe9'); sys.stderr.buffer.write(b'bad \\xff')",
            ]
        )
        self.assertEqual((0, "caf\udce9", "bad \udcff"), (result.returncode, result.stdout, result.stderr))

    def test_the_diff_replaces_and_counts_each_undecodable_byte(self) -> None:
        def undecodable(data: bytes) -> str:
            return data.decode("utf-8", "surrogateescape")  # what subprocess_runner returns for these bytes

        cases = (
            (b"+caf\xe9\n", "+caf\ufffd\n", 1),
            (b"+\xe2\x82 cut short\n", "+\ufffd\ufffd cut short\n", 2),  # a truncated sequence is two bytes
            ("+caf\ufffd already\n".encode("utf-8"), "+caf\ufffd already\n", 0),  # a real U+FFFD is not counted
            (b"+plain\n", "+plain\n", 0),
        )
        for data, text, count in cases:
            with self.subTest(data=data):
                value = undecodable(data)
                client = GitHubClient(answering(CommandResult(0, value, "")))
                self.assertEqual((text, count), client.get_pull_diff("example/one", 7))

    def test_json_and_errors_replace_undecodable_bytes(self) -> None:
        body = b'{"login": "caf\xe9"}'.decode("utf-8", "surrogateescape")
        self.assertEqual("caf\ufffd", GitHubClient(lambda arguments: CommandResult(0, body, "")).authenticated_login())
        page = json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequests": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [{"body": "BODY"}],
                        }
                    }
                }
            }
        )
        page = page.replace("BODY", b"caf\xe9".decode("utf-8", "surrogateescape"))
        nodes = GitHubClient(lambda arguments: CommandResult(0, page, "")).graphql_nodes(
            "query", {}, ("repository", "pullRequests")
        )
        self.assertEqual([{"body": "caf\ufffd"}], nodes)
        failing = GitHubClient(
            lambda arguments: CommandResult(1, "", b"HTTP 404: caf\xe9 not found".decode("utf-8", "surrogateescape"))
        )
        with self.assertRaises(GitHubError) as context:
            failing.get_pull_diff("example/one", 7)
        # The message reaches a strict UTF-8 stderr as a FAILED line, so it must encode.
        self.assertEqual("HTTP 404: caf\ufffd not found", str(context.exception))
        self.assertEqual("not_found", context.exception.kind)
        str(context.exception).encode("utf-8")


class GitHubDocumentTests(unittest.TestCase):
    @staticmethod
    def _client(*values: Any) -> tuple[GitHubClient, list[list[str]]]:
        responses = [CommandResult(0, json.dumps(value), "") for value in values]
        calls: list[list[str]] = []

        def runner(arguments: Sequence[str]) -> CommandResult:
            calls.append(list(arguments))
            return responses[len(calls) - 1]

        return GitHubClient(runner), calls

    def test_merge_base_uses_the_compare_endpoint_and_accepts_both_object_id_lengths(self) -> None:
        for base, head, sha in (("a" * 40, "b" * 64, "c" * 40), ("a" * 64, "b" * 40, "c" * 64)):
            with self.subTest(base=base, head=head, sha=sha):
                client, calls = self._client({"merge_base_commit": {"sha": sha}})
                self.assertEqual(sha, client.get_merge_base("Example/One", base, head))
                self.assertEqual([["gh", "api", f"repos/example/one/compare/{base}...{head}?per_page=1"]], calls)
                self.assertEqual(1, client.call_count)

    def test_merge_base_rejects_invalid_commit_ids_before_running_gh(self) -> None:
        for invalid, found in INVALID_COMMITS:
            for base, head, which in ((invalid, "b" * 40, "base"), ("a" * 40, invalid, "head")):
                with self.subTest(base=base, head=head):
                    client, calls = self._client()
                    with self.assertRaises(GitHubInputError) as context:
                        client.get_merge_base("example/one", base, head)
                    self.assertEqual(field_message(f"{which} commit", OBJECT_ID, found), str(context.exception))
                    self.assertEqual("input", context.exception.kind)
                    self.assertEqual([], calls)
                    self.assertEqual(0, client.call_count)

    def test_merge_base_rejects_malformed_compare_responses(self) -> None:
        sha = "compare.merge_base_commit.sha"
        values: tuple[tuple[Any, str], ...] = (
            ([], "compare must be an object, found an array"),
            ({}, "compare.merge_base_commit must be an object, found null"),
            ({"merge_base_commit": []}, "compare.merge_base_commit must be an object, found an array"),
            ({"merge_base_commit": {}}, field_message(sha, OBJECT_ID, "null")),
            ({"merge_base_commit": {"sha": 1}}, field_message(sha, OBJECT_ID, "integer 1")),
            ({"merge_base_commit": {"sha": "a" * 39}}, field_message(sha, OBJECT_ID, "a string of 39 characters")),
            ({"merge_base_commit": {"sha": "g" * 40}}, field_message(sha, OBJECT_ID, NOT_HEX)),
        )
        for value, message in values:
            with self.subTest(value=value):
                client, calls = self._client(value)
                with self.assertRaises(GitHubFieldError) as context:
                    client.get_merge_base("example/one", "a" * 40, "b" * 40)
                self.assertNotIsInstance(context.exception, GitHubInputError)
                self.assertEqual(message, str(context.exception))
                self.assertEqual("malformed", context.exception.kind)
                self.assertFalse(context.exception.retryable)
                self.assertEqual(1, len(calls))

    def test_file_blob_rejects_invalid_commit_ids_before_running_gh(self) -> None:
        for commit, found in INVALID_COMMITS:
            with self.subTest(commit=commit):
                client, calls = self._client()
                with self.assertRaises(GitHubInputError) as context:
                    client.get_file_blob("example/one", commit, "docs/design.docx", maximum=1)
                self.assertEqual(field_message("commit", OBJECT_ID, found), str(context.exception))
                self.assertEqual("input", context.exception.kind)
                self.assertEqual([], calls)
                self.assertEqual(0, client.call_count)

    def test_file_blob_rejects_contents_that_are_not_one_file(self) -> None:
        with self.subTest(value=[]):
            client, calls = self._client([])
            with self.assertRaisesRegex(GitHubFieldError, "^contents must be an object, found an array$") as field:
                client.get_file_blob("example/one", "a" * 40, "docs/design.docx", maximum=1)
            self.assertEqual("malformed", field.exception.kind)
            self.assertEqual(1, len(calls))
        values: tuple[Any, ...] = ({}, {"type": "dir"}, {"type": 1}, {"type": "Symlink"})
        for value in values:
            with self.subTest(value=value):
                client, calls = self._client(value)
                with self.assertRaisesRegex(GitHubError, "^Contents response is not one file$") as context:
                    client.get_file_blob("example/one", "a" * 40, "docs/design.docx", maximum=1)
                self.assertNotIsInstance(context.exception, review_github.NotOneFile)
                self.assertEqual("malformed", context.exception.kind)
                self.assertEqual(1, len(calls))

    def test_file_blob_names_a_symbolic_link_or_a_submodule_and_reads_no_blob(self) -> None:
        # GitHub answers a link whose target is not a file in the repository, and a submodule, with their own type.
        for value, entry in (
            ({"type": "symlink", "target": "../../outside.md"}, "symbolic link"),
            ({"type": "submodule", "submodule_git_url": "https://example.invalid/x.git"}, "submodule"),
        ):
            with self.subTest(entry=entry):
                client, calls = self._client(value)
                with self.assertRaises(review_github.NotOneFile) as context:
                    client.get_file_blob("example/one", "a" * 40, "docs/link.md", maximum=1)
                self.assertEqual(
                    f"Contents response is not one file: docs/link.md is a {entry}", str(context.exception)
                )
                self.assertEqual((entry, "not_file"), (context.exception.entry, context.exception.kind))
                self.assertEqual(1, len(calls))

    def test_file_blob_rejects_contents_with_invalid_blob_ids(self) -> None:
        for fields, found in (
            ({}, "null"),
            ({"sha": 1}, "integer 1"),
            ({"sha": "b" * 39}, "a string of 39 characters"),
            ({"sha": "g" * 40}, NOT_HEX),
            ({"sha": "B" * 64}, NOT_HEX),
        ):
            with self.subTest(fields=fields):
                client, calls = self._client({"type": "file", "size": 0, **fields})
                with self.assertRaises(GitHubFieldError) as context:
                    client.get_file_blob("example/one", "a" * 40, "docs/design.docx", maximum=1)
                self.assertEqual(field_message("contents.sha", OBJECT_ID, found), str(context.exception))
                self.assertEqual("malformed", context.exception.kind)
                self.assertEqual(1, len(calls))

    def test_file_blob_rejects_contents_without_a_nonnegative_integer_size(self) -> None:
        for fields, found in (
            ({}, "null"),
            ({"size": None}, "null"),
            ({"size": "1"}, 'string "1"'),
            ({"size": 1.5}, "number 1.5"),
            ({"size": True}, "boolean true"),
            ({"size": -1}, "integer -1"),
        ):
            with self.subTest(fields=fields):
                client, calls = self._client({"type": "file", "sha": "b" * 40, **fields})
                with self.assertRaises(GitHubFieldError) as context:
                    client.get_file_blob("example/one", "a" * 40, "docs/design.docx", maximum=1)
                self.assertEqual(
                    field_message("contents.size", "an integer of at least 0", found), str(context.exception)
                )
                self.assertEqual("malformed", context.exception.kind)
                self.assertEqual(1, len(calls))

    def test_file_blob_fetches_valid_bytes_below_and_at_the_size_limit_with_quoted_path(self) -> None:
        payload = b"\x00\xff\n"
        for commit, sha, maximum in (("a" * 40, "b" * 64, 4), ("a" * 64, "b" * 40, 3)):
            with self.subTest(commit=commit, sha=sha, maximum=maximum):
                client, calls = self._client(
                    {"type": "file", "sha": sha, "size": len(payload)}, {"encoding": "base64", "content": "AP8K\n"}
                )
                self.assertEqual(
                    (sha, payload),
                    client.get_file_blob("Example/One", commit, "docs/A design #1?+%.docx", maximum=maximum),
                )
                self.assertEqual(
                    [
                        ["gh", "api", f"repos/example/one/contents/docs/A%20design%20%231%3F%2B%25.docx?ref={commit}"],
                        ["gh", "api", f"repos/example/one/git/blobs/{sha}"],
                    ],
                    calls,
                )
                self.assertEqual(2, client.call_count)

    def test_file_blob_fetches_an_empty_file_at_a_zero_size_limit(self) -> None:
        sha = "b" * 40
        client, calls = self._client({"type": "file", "sha": sha, "size": 0}, {"encoding": "base64", "content": ""})
        self.assertEqual((sha, b""), client.get_file_blob("example/one", "a" * 40, "docs/design.docx", maximum=0))
        self.assertEqual(2, len(calls))

    def test_file_blob_returns_the_id_without_fetching_content_one_byte_over_the_limit(self) -> None:
        sha, commit = "b" * 40, "a" * 40
        client, calls = self._client({"type": "file", "sha": sha, "size": 4})
        self.assertEqual((sha, None), client.get_file_blob("example/one", commit, "docs/design.docx", maximum=3))
        self.assertEqual([["gh", "api", f"repos/example/one/contents/docs/design.docx?ref={commit}"]], calls)
        self.assertEqual(1, client.call_count)

    def test_file_blob_rejects_malformed_blob_responses(self) -> None:
        values: tuple[tuple[Any, str], ...] = (
            ([], "blob must be an object, found an array"),
            ({}, "Blob response has an unexpected shape"),
            ({"encoding": "utf-8", "content": "text"}, "Blob response has an unexpected shape"),
            ({"encoding": "base64"}, "blob.content must be a string, found null"),
            ({"encoding": "base64", "content": 1}, "blob.content must be a string, found integer 1"),
        )
        for value, message in values:
            with self.subTest(value=value):
                client, calls = self._client({"type": "file", "sha": "b" * 40, "size": 1}, value)
                with self.assertRaises(GitHubError) as context:
                    client.get_file_blob("example/one", "a" * 40, "docs/design.docx", maximum=1)
                self.assertEqual(message, str(context.exception))
                self.assertEqual(" must be " in message, isinstance(context.exception, GitHubFieldError))
                self.assertEqual("malformed", context.exception.kind)
                self.assertEqual(2, len(calls))

    def test_file_blob_classifies_base64_decode_failures(self) -> None:
        for content in ("a", "\u00e9"):
            with self.subTest(content=content):
                client, calls = self._client(
                    {"type": "file", "sha": "b" * 40, "size": 1}, {"encoding": "base64", "content": content}
                )
                with self.assertRaisesRegex(GitHubError, "^Blob response is not base64$") as context:
                    client.get_file_blob("example/one", "a" * 40, "docs/design.docx", maximum=1)
                self.assertEqual("malformed", context.exception.kind)
                self.assertFalse(context.exception.retryable)
                self.assertIsNotNone(context.exception.__cause__)
                self.assertEqual(2, len(calls))

    def test_document_reads_propagate_gh_and_json_failures_without_fetching_further(self) -> None:
        contents = CommandResult(0, json.dumps({"type": "file", "sha": "b" * 40, "size": 1}), "")
        for failure, kind, message in (
            (CommandResult(1, "", "HTTP 404: Not Found"), "not_found", "^HTTP 404: Not Found$"),
            (CommandResult(0, "not-json", ""), "malformed", "^GitHub returned malformed JSON:"),
        ):
            for stage in ("compare", "contents", "blob"):
                with self.subTest(stage=stage, kind=kind):
                    responses = [contents, failure] if stage == "blob" else [failure]
                    runner = mock.Mock(side_effect=responses)
                    client = GitHubClient(runner)
                    with self.assertRaisesRegex(GitHubError, message) as context:
                        if stage == "compare":
                            client.get_merge_base("example/one", "a" * 40, "b" * 40)
                        else:
                            client.get_file_blob("example/one", "a" * 40, "docs/design.docx", maximum=1)
                    self.assertEqual(kind, context.exception.kind)
                    self.assertFalse(context.exception.retryable)
                    self.assertEqual(1 if kind == "not_found" else None, context.exception.returncode)
                    self.assertEqual(len(responses), runner.call_count)
                    self.assertEqual(len(responses), client.call_count)

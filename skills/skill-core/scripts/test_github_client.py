"""Regression tests for the GitHub CLI client every skill that runs gh shares."""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import unittest
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bounded_process
import github_client
from github_client import (
    Backoff,
    CommandResult,
    GitHubClient,
    GitHubError,
    classify_failure,
    graphql_failure,
    replace_undecodable,
    split_response,
)

SECONDARY_LIMIT = "gh: You have exceeded a secondary rate limit. (HTTP 403)"


def undecodable(data: bytes) -> str:
    """What subprocess_runner returns for these bytes."""
    return data.decode("utf-8", "surrogateescape")


class Recorder:
    """A sleeper that records waits and advances a fake clock instead of sleeping."""

    def __init__(self, now: float = 1000.0) -> None:
        self.waits: list[float] = []
        self.now = now

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


def scripted(*responses: CommandResult) -> tuple[Callable[[Sequence[str]], CommandResult], list[list[str]]]:
    """A runner that answers each call with the next response and records the arguments."""
    queue = list(responses)
    calls: list[list[str]] = []

    def run(arguments: Sequence[str]) -> CommandResult:
        calls.append(list(arguments))
        return queue.pop(0)

    return run, calls


def client_for(run: Callable[[Sequence[str]], CommandResult], now: float = 1000.0) -> tuple[GitHubClient, Recorder]:
    sleeper = Recorder(now)
    return GitHubClient(run, sleeper=sleeper, clock=sleeper.clock), sleeper


def ok(stdout: str = "{}") -> CommandResult:
    return CommandResult(0, stdout, "")


def failed(stderr: str, stdout: str = "") -> CommandResult:
    return CommandResult(1, stdout, stderr)


class ClassifierTests(unittest.TestCase):
    def test_every_failure_class_from_literal_gh_stderr(self) -> None:
        cases = {
            "gh: API rate limit exceeded for user ID 1234. (HTTP 403)": "rate_limit",
            SECONDARY_LIMIT: "rate_limit",
            "gh: HTTP 429: Too Many Requests": "rate_limit",
            "gh: Bad credentials (HTTP 401)": "authentication",
            "gh: Requires authentication (HTTP 401)": "authentication",
            "You are not logged into any GitHub hosts. To log in, run: gh auth login": "authentication",
            "To get started with GitHub CLI, please run:  gh auth login": "authentication",
            "gh: Only the first 1000 search results are available (HTTP 422)": "search_cap",
            "gh: Resource protected by organization SAML enforcement. (HTTP 403)": "forbidden",
            "gh: Not Found (HTTP 404)": "not_found",
            "HTTP 404: Not Found (https://api.github.com/repos/o/r/pulls/9)": "not_found",
            "error connecting to api.github.com\ncheck your internet connection or https://githubstatus.com": "network",
            'Get "https://api.github.com/user": dial tcp: lookup api.github.com: no such host': "network",
            "read tcp 10.0.0.2:51000->140.82.112.6:443: wsarecv: connection reset by peer": "network",
            "dial tcp 140.82.112.6:443: connect: connection refused": "network",
            'Post "https://api.github.com/graphql": net/http: TLS handshake timeout': "network",
            "dial tcp 140.82.112.6:443: i/o timeout": "network",
            "gh: Validation Failed (HTTP 422)": "api",
            "gh: Server Error (HTTP 502)": "api",
            "GraphQL: Could not resolve to a Repository with the name 'o/r'. (repository)": "api",
            "": "api",
        }
        for stderr, kind in cases.items():
            with self.subTest(stderr=stderr):
                self.assertEqual(kind, classify_failure(stderr))

    def test_response_status_and_quota_headers_mark_a_rate_limit(self) -> None:
        self.assertEqual("rate_limit", classify_failure("gh: HTTP 403", 403, {"x-ratelimit-remaining": "0"}))
        self.assertEqual("forbidden", classify_failure("gh: HTTP 403", 403, {"x-ratelimit-remaining": "12"}))
        self.assertEqual("rate_limit", classify_failure("", 429, {}))

    def test_graphql_body_errors_are_classified_by_type(self) -> None:
        cases = (
            ([{"type": "RATE_LIMITED", "message": "slow down"}], "rate_limit", True),
            ([{"type": "NOT_FOUND", "message": "gone"}], "not_found", False),
            ([{"type": "FORBIDDEN", "message": "SSO"}], "forbidden", False),
            ([{"type": "SOMETHING", "message": "odd"}], "api", False),
            ({"message": "a lone error"}, "api", False),
        )
        for errors, kind, retryable in cases:
            with self.subTest(errors=errors):
                error = graphql_failure({"errors": errors})
                if error is None:
                    self.fail("each case is a GraphQL failure")
                self.assertEqual((kind, retryable), (error.kind, error.retryable))
                self.assertTrue(str(error).startswith("GraphQL request failed"), str(error))
        error = graphql_failure({"errors": [{"type": "FORBIDDEN", "message": "SSO"}]})
        self.assertEqual("GraphQL request failed (FORBIDDEN): SSO", str(error))
        self.assertIsNone(graphql_failure({"data": {}}))
        self.assertIsNone(graphql_failure([]))

    def test_only_a_rate_limit_is_retryable(self) -> None:
        for stderr in ("gh: HTTP 429", "gh: HTTP 401", "error connecting to api.github.com", "gh: HTTP 502"):
            client, _ = client_for(scripted(failed(stderr))[0])
            with self.subTest(stderr=stderr), self.assertRaises(GitHubError) as context:
                client.run(["api", "user"], retry=False)
            self.assertEqual(stderr.startswith("gh: HTTP 429"), context.exception.retryable)


class BackoffTests(unittest.TestCase):
    def test_a_rate_limit_backs_off_then_succeeds(self) -> None:
        run, calls = scripted(failed(SECONDARY_LIMIT), ok('{"login": "octo"}'))
        client, sleeper = client_for(run)
        self.assertEqual('{"login": "octo"}', client.run(["api", "user"]).stdout)
        self.assertEqual([5.0], sleeper.waits)
        self.assertEqual([["gh", "api", "user"], ["gh", "api", "user"]], calls)

    def test_call_count_counts_every_command_run_retries_included_across_threads(self) -> None:
        run, _ = scripted(failed(SECONDARY_LIMIT), ok(), failed("HTTP 404: Not Found"))
        client, _ = client_for(run)
        client.run(["api", "user"])
        with self.assertRaises(GitHubError):
            client.run(["api", "repos/o/r"])
        self.assertEqual(3, client.call_count)
        threaded = GitHubClient(lambda arguments: ok(), sleeper=Recorder())
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: threaded.run(["api", "user"]), range(400)))
        self.assertEqual(400, threaded.call_count)

    def test_exhausting_retries_raises_the_rate_limit_after_the_bounded_waits(self) -> None:
        run, calls = scripted(*[failed(SECONDARY_LIMIT)] * 6)
        client, sleeper = client_for(run)
        with self.assertRaises(GitHubError) as context:
            client.run(["api", "user"])
        self.assertEqual("rate_limit", context.exception.kind)
        self.assertEqual(f"{SECONDARY_LIMIT} (gave up after 5 retries)", str(context.exception))
        self.assertEqual([5.0, 10.0, 20.0, 40.0, 80.0], sleeper.waits)
        self.assertEqual(6, len(calls))

    def test_retry_after_header_sets_the_wait(self) -> None:
        head = "HTTP/2.0 403 Forbidden\r\nRetry-After: 42\r\nX-Ratelimit-Remaining: 3\r\n\r\n{}"
        client, sleeper = client_for(scripted(failed(SECONDARY_LIMIT, head), ok("{}"))[0])
        client.run(["api", "search/issues?q=x"], headers=True)
        self.assertEqual([42.0], sleeper.waits)

    def test_exhausted_quota_waits_until_the_reset_time(self) -> None:
        head = "HTTP/2.0 403 Forbidden\r\nX-Ratelimit-Remaining: 0\r\nX-Ratelimit-Reset: 1030\r\n\r\n{}"
        client, sleeper = client_for(scripted(failed("gh: HTTP 403", head), ok("{}"))[0], now=1000.0)
        client.run(["api", "user"], headers=True)
        self.assertEqual([31.0], sleeper.waits)

    def test_a_wait_longer_than_the_limit_fails_instead_of_sleeping(self) -> None:
        head = "HTTP/2.0 403 Forbidden\r\nRetry-After: 3600\r\n\r\n{}"
        client, sleeper = client_for(scripted(failed(SECONDARY_LIMIT, head))[0])
        with self.assertRaises(GitHubError) as context:
            client.run(["api", "user"], headers=True)
        self.assertEqual("rate_limit", context.exception.kind)
        self.assertEqual("GitHub asked to wait 3600s before retrying; rerun later", str(context.exception))
        self.assertEqual([], sleeper.waits)

    def test_the_policy_values(self) -> None:
        policy = Backoff()
        self.assertEqual((5.0, 5, 300.0), (policy.base, policy.retries, policy.longest))
        self.assertEqual([5.0, 10.0, 20.0, 40.0, 80.0], [policy.wait(attempt, {}, 0.0) for attempt in range(5)])

    def test_without_retry_a_rate_limit_fails_at_once(self) -> None:
        run, calls = scripted(failed(SECONDARY_LIMIT))
        client, sleeper = client_for(run)
        with self.assertRaises(GitHubError) as context:
            client.run(["pr", "view"], retry=False)
        self.assertEqual(("rate_limit", SECONDARY_LIMIT), (context.exception.kind, str(context.exception)))
        self.assertEqual(([], 1), (sleeper.waits, len(calls)))

    def test_other_failures_are_not_retried(self) -> None:
        run, calls = scripted(failed("gh: Bad credentials (HTTP 401)"))
        client, sleeper = client_for(run)
        with self.assertRaises(GitHubError) as context:
            client.run(["api", "user"])
        self.assertEqual("authentication", context.exception.kind)
        self.assertEqual(([], 1), (sleeper.waits, len(calls)))

    def test_an_empty_stderr_still_names_the_failure_and_its_exit_code(self) -> None:
        client, _ = client_for(scripted(CommandResult(4, "", ""))[0])
        with self.assertRaisesRegex(GitHubError, "gh api failed with exit code 4") as context:
            client.run(["api", "user"])
        self.assertEqual(4, context.exception.returncode)


class RequestTests(unittest.TestCase):
    def test_parse_may_ask_for_a_retry_or_fail(self) -> None:
        answers = iter([GitHubError("timed out", kind="incomplete", retryable=True), None])

        def parse(result: CommandResult) -> str:
            problem = next(answers)
            if problem is not None:
                raise problem
            return result.stdout

        client, sleeper = client_for(scripted(ok("first"), ok("second"))[0])
        self.assertEqual("second", client.request(["api", "search/issues"], parse))
        self.assertEqual([5.0], sleeper.waits)

        def fatal(result: CommandResult) -> str:
            raise GitHubError("bad shape", kind="malformed")

        client, sleeper = client_for(scripted(ok())[0])
        with self.assertRaises(GitHubError) as context:
            client.request(["api", "user"], fatal)
        self.assertEqual(("malformed", []), (context.exception.kind, sleeper.waits))

    def test_headers_are_requested_and_split_from_the_body(self) -> None:
        stdout = 'HTTP/2.0 200 OK\r\nX-Ratelimit-Remaining: 29\r\n\r\n{"items": []}'
        run, calls = scripted(ok(stdout))
        client, _ = client_for(run)
        self.assertEqual('{"items": []}', client.run(["api", "search/issues?q=x"], headers=True).stdout)
        self.assertEqual(["gh", "api", "-i", "search/issues?q=x"], calls[0])

    def test_sso_partial_results_fail_closed(self) -> None:
        stdout = "HTTP/2.0 200 OK\r\nX-Github-Sso: partial-results; organizations=21955855\r\n\r\n{}"
        client, sleeper = client_for(scripted(ok(stdout))[0])
        with self.assertRaises(GitHubError) as context:
            client.run(["api", "search/issues?q=x"], headers=True)
        self.assertEqual(("sso_partial", []), (context.exception.kind, sleeper.waits))
        self.assertIn("21955855", str(context.exception))

    def test_a_pacer_sees_every_attempt_in_order(self) -> None:
        events: list[str] = []

        class Pacer:
            def before(self) -> None:
                events.append("before")

            def after(self, error: GitHubError | None) -> None:
                events.append("ok" if error is None else error.kind)

        client, _ = client_for(scripted(failed(SECONDARY_LIMIT), ok())[0])
        client.run(["api", "search/issues"], pacer=Pacer())
        self.assertEqual(["before", "rate_limit", "before", "ok"], events)

    def test_split_response_reads_the_status_line_and_headers(self) -> None:
        response = split_response("HTTP/2.0 404 Not Found\r\nRetry-After: 7\r\nX-A: b\r\n\r\nbody\n\nmore")
        self.assertEqual((404, {"retry-after": "7", "x-a": "b"}, "body\n\nmore"), response)
        self.assertEqual((None, {}, '{"plain": true}'), split_response('{"plain": true}'))


class DecodingTests(unittest.TestCase):
    def test_undecodable_bytes_are_replaced_and_counted(self) -> None:
        cases = (
            (b"caf\xe9", "caf\ufffd", 1),
            (b"\xe2\x82 cut short", "\ufffd\ufffd cut short", 2),  # a truncated sequence is two bytes
            ("caf\ufffd already".encode(), "caf\ufffd already", 0),  # a real U+FFFD is not counted
            (b"plain", "plain", 0),
        )
        for data, text, count in cases:
            with self.subTest(data=data):
                self.assertEqual((text, count), replace_undecodable(undecodable(data)))
                client, _ = client_for(scripted(CommandResult(0, undecodable(data), undecodable(b"warn \xff")))[0])
                result = client.run(["api", "user"])
                self.assertEqual((text, count, "warn \ufffd"), (result.stdout, result.replaced, result.stderr))

    def test_a_failure_message_is_valid_unicode(self) -> None:
        client, _ = client_for(scripted(failed(undecodable(b"gh: caf\xe9 Not Found (HTTP 404)")))[0])
        with self.assertRaises(GitHubError) as context:
            client.run(["api", "repos/o/r"])
        self.assertEqual(
            ("not_found", "gh: caf\ufffd Not Found (HTTP 404)"), (context.exception.kind, str(context.exception))
        )
        str(context.exception).encode("utf-8")

    def test_command_result_is_frozen(self) -> None:
        result = CommandResult(0, "out", "err")
        self.assertEqual(0, result.replaced)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.stdout = "changed"  # type: ignore[misc]  # the assignment is what the test proves fails

    def test_runner_decodes_bytes_that_are_not_utf8_without_losing_them(self) -> None:
        program = "import sys; sys.stdout.buffer.write(b'caf\\xe9'); sys.stderr.buffer.write(b'bad \\xff')"
        result = github_client.subprocess_runner([sys.executable, "-c", program])
        self.assertEqual((0, "caf\udce9", "bad \udcff"), (result.returncode, result.stdout, result.stderr))

    def test_missing_cli_is_a_prerequisite_error(self) -> None:
        with (
            mock.patch.object(github_client, "run_bounded", side_effect=FileNotFoundError("gh")),
            self.assertRaises(GitHubError) as context,
        ):
            github_client.subprocess_runner(["gh", "api", "user"])
        self.assertEqual("prerequisite", context.exception.kind)
        self.assertIn("install GitHub CLI", str(context.exception))

    def test_a_cli_that_cannot_start_is_an_execution_error(self) -> None:
        with (
            mock.patch.object(github_client, "run_bounded", side_effect=PermissionError("denied")),
            self.assertRaises(GitHubError) as context,
        ):
            github_client.subprocess_runner(["gh", "api", "user"])
        self.assertEqual("execution", context.exception.kind)


class DownloadTests(unittest.TestCase):
    def test_download_writes_the_exact_bytes(self) -> None:
        data = bytes(range(256)) * 4
        program = f"import sys; sys.stdout.buffer.write({data!r})"
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "archive.tar.gz"
            result = github_client.subprocess_downloader([sys.executable, "-c", program], target)
            self.assertEqual(0, result.returncode)
            self.assertEqual(data, target.read_bytes())

    def test_download_retries_a_rate_limit_and_classifies_failures(self) -> None:
        answers = [failed(SECONDARY_LIMIT), ok(""), failed("gh: Not Found (HTTP 404)")]
        calls: list[tuple[list[str], Path]] = []

        def downloader(arguments: Sequence[str], target: Path) -> CommandResult:
            calls.append((list(arguments), target))
            target.write_bytes(b"partial" if answers[0].returncode else b"whole")
            return answers.pop(0)

        sleeper = Recorder()
        client = GitHubClient(sleeper=sleeper, clock=sleeper.clock, downloader=downloader)
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "source.tar.gz"
            client.download(["api", "repos/o/r/tarball/abc"], target)
            self.assertEqual(b"whole", target.read_bytes())
            self.assertEqual([5.0], sleeper.waits)
            self.assertEqual([["gh", "api", "repos/o/r/tarball/abc"]] * 2, [call[0] for call in calls])
            with self.assertRaises(GitHubError) as context:
                client.download(["api", "repos/o/r/tarball/abc"], target)
            self.assertEqual("not_found", context.exception.kind)

    def test_missing_cli_is_a_prerequisite_error_for_a_download(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(github_client, "run_bounded", side_effect=FileNotFoundError("gh")),
            self.assertRaises(GitHubError) as context,
        ):
            github_client.subprocess_downloader(["gh", "api", "x"], Path(temporary) / "out")
        self.assertEqual("prerequisite", context.exception.kind)


class JsonTests(unittest.TestCase):
    def test_json_parses_the_body_or_fails_as_malformed(self) -> None:
        client, _ = client_for(scripted(ok('{"login": "octo"}'), ok("not-json"))[0])
        payload: Any = client.json(["api", "user"])
        self.assertEqual({"login": "octo"}, payload)
        with self.assertRaises(GitHubError) as context:
            client.json(["api", "user"])
        self.assertEqual("malformed", context.exception.kind)
        self.assertIn("malformed JSON", str(context.exception))

    def test_json_retries_a_graphql_rate_limit_in_the_body(self) -> None:
        limited = json.dumps({"errors": [{"type": "RATE_LIMITED", "message": "slow down"}]})
        client, sleeper = client_for(scripted(ok(limited), ok('{"data": {}}'))[0])
        self.assertEqual({"data": {}}, client.json(["api", "graphql"], graphql=True))
        self.assertEqual([5.0], sleeper.waits)


class SubprocessRunnerContractTests(unittest.TestCase):
    def test_runner_runs_through_the_bounded_layer_and_captures_both_streams(self) -> None:
        finished = bounded_process.Finished(3, b"o", b"e")
        with mock.patch.object(github_client, "run_bounded", return_value=finished) as run:
            result = github_client.subprocess_runner(["gh", "api", "user"])
        self.assertEqual(CommandResult(3, "o", "e"), result)
        self.assertEqual(mock.call(["gh", "api", "user"], 300.0, stdout=None, cwd=None), run.call_args)

    def test_runner_runs_in_the_directory_it_is_given(self) -> None:
        directory = Path(self.directory()) / "a repository"
        directory.mkdir()
        result = github_client.subprocess_runner([sys.executable, "-c", "import os; print(os.getcwd())"], cwd=directory)
        self.assertEqual(directory.resolve(), Path(result.stdout.strip()).resolve())

    def test_a_command_that_runs_too_long_is_a_timeout_and_is_not_retried(self) -> None:
        program = "import time; time.sleep(30)"
        with self.assertRaises(GitHubError) as context:
            github_client.subprocess_runner([sys.executable, "-c", program], timeout=0.5)
        self.assertEqual(("timeout", False), (context.exception.kind, context.exception.retryable))
        self.assertIn("did not finish within 0.5 seconds", str(context.exception))
        with self.assertRaises(GitHubError) as context:
            github_client.subprocess_downloader(
                [sys.executable, "-c", program], Path(self.directory()) / "out", timeout=0.5
            )
        self.assertEqual("timeout", context.exception.kind)

        calls: list[Sequence[str]] = []

        def stalled(arguments: Sequence[str]) -> CommandResult:
            calls.append(arguments)
            raise GitHubError("GitHub CLI did not finish within 1 seconds", kind="timeout")

        client, sleeper = client_for(stalled)
        with self.assertRaises(GitHubError) as context:
            client.run(["api", "user"])
        self.assertEqual(("timeout", 1, []), (context.exception.kind, len(calls), sleeper.waits))

    def test_the_client_timeout_bounds_its_default_runners(self) -> None:
        finished = bounded_process.Finished(0, b"{}", b"")
        with (
            mock.patch.object(github_client, "run_bounded", return_value=finished) as run,
            tempfile.TemporaryDirectory() as temporary,
        ):
            client = GitHubClient(timeout=7.5)
            client.run(["api", "user"])
            client.download(["api", "x"], Path(temporary) / "out")
            GitHubClient().run(["api", "user"])
        self.assertEqual([7.5, 7.5, 300.0], [call.args[1] for call in run.call_args_list])
        self.assertEqual(300.0, github_client.DEFAULT_TIMEOUT_SECONDS)

    def directory(self) -> str:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return temporary.name


if __name__ == "__main__":
    unittest.main()

"""Regression tests for the GitHub CLI client every skill that runs gh shares."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import unittest
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, get_args
from unittest import mock

import pytest

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
USER_NOT_FOUND = json.dumps({"errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a User."}]})


def sanitized_environment(test: unittest.TestCase) -> tuple[Path, Path]:
    """A PATH of one empty temporary folder and a fresh local application data folder for the rest of a test, so the
    only gh the locator finds is one the test puts in the first or in WinGet's Links folder under the second; returns
    the two folders. Off Windows, where no package manager links commands into a folder of its own, the second is the
    folder PATH names after the first, the place the locator looks next."""
    temporary = tempfile.TemporaryDirectory(prefix="gh locator ")
    test.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    path_folder = root / "on path"
    if sys.platform == "win32":
        links = root / "local app data" / "Microsoft" / "WinGet" / "Links"
        environment = {"PATH": str(path_folder), "PATHEXT": ".com;.exe", "LOCALAPPDATA": str(root / "local app data")}
    else:
        links = root / "later on path"
        environment = {"PATH": os.pathsep.join((str(path_folder), str(links)))}
    path_folder.mkdir()
    links.mkdir(parents=True)
    patch = mock.patch.dict(os.environ, environment)
    patch.start()
    test.addCleanup(patch.stop)
    return path_folder, links


def install_gh(folder: Path) -> Path:
    """A GitHub CLI as its installer leaves it: `gh.exe` on Windows, and an executable `gh` elsewhere."""
    program = folder / ("gh.exe" if sys.platform == "win32" else "gh")
    program.write_bytes(b"fixture")
    program.chmod(0o755)
    return program


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
    def test_response_status_and_quota_headers_mark_a_rate_limit(self) -> None:
        self.assertEqual("rate_limit", classify_failure("gh: HTTP 403", 403, {"x-ratelimit-remaining": "0"}))
        self.assertEqual("forbidden", classify_failure("gh: HTTP 403", 403, {"x-ratelimit-remaining": "12"}))
        self.assertEqual("rate_limit", classify_failure("", 429, {}))

    def test_graphql_body_errors_are_classified_by_type(self) -> None:
        error = graphql_failure({"errors": [{"type": "FORBIDDEN", "message": "SSO"}]})
        self.assertEqual("GraphQL request failed (FORBIDDEN): SSO", str(error))
        self.assertIsNone(graphql_failure({"data": {}}))
        self.assertIsNone(graphql_failure([]))


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

    def test_a_refused_api_request_carries_the_reasons_its_body_gives(self) -> None:
        reason = "The listed users cannot be searched either because the users do not exist\nor you cannot view them."
        body = json.dumps(
            {"message": "Validation Failed", "errors": [{"message": reason, "code": "invalid"}, {"code": "missing"}]}
        )
        head = "HTTP/2.0 422 Unprocessable Entity\r\nContent-Type: application/json\r\n\r\n"
        for stdout, headers in ((head + body, True), (body, False)):
            client, sleeper = client_for(scripted(failed("gh: Validation Failed (HTTP 422)\n", stdout))[0])
            with self.subTest(headers=headers), self.assertRaises(GitHubError) as context:
                client.run(["api", "search/issues?q=author:nobody"], headers=headers)
            self.assertEqual(
                "gh: Validation Failed (HTTP 422): The listed users cannot be searched either because the users do "
                "not exist or you cannot view them.",
                str(context.exception),
            )
            self.assertEqual(("api", []), (context.exception.kind, sleeper.waits))

    def test_a_reason_gh_already_printed_or_a_body_without_reasons_adds_nothing(self) -> None:
        client, _ = client_for(scripted(failed("gh: pull request not found\n", USER_NOT_FOUND))[0])
        with self.assertRaises(GitHubError) as context:
            client.run(["pr", "view", "5"])
        self.assertEqual("gh: pull request not found", str(context.exception), "only gh api prints a response body")


BAD_GATEWAY = "gh: Bad Gateway (HTTP 502)"
UNKNOWN_OUTCOME = "GitHub may have applied it, so check before repeating it"


class TransientTests(unittest.TestCase):
    def test_a_transient_read_is_retried_then_succeeds(self) -> None:
        run, calls = scripted(failed(BAD_GATEWAY), ok('{"login": "octo"}'))
        client, sleeper = client_for(run)
        self.assertEqual('{"login": "octo"}', client.run(["api", "user"]).stdout)
        self.assertEqual([2.0], sleeper.waits)
        self.assertEqual([["gh", "api", "user"]] * 2, calls)

    def test_a_transient_read_fails_after_its_retries_in_the_existing_shape(self) -> None:
        run, calls = scripted(*[failed(BAD_GATEWAY)] * 3)
        client, sleeper = client_for(run)
        with self.assertRaises(GitHubError) as context:
            client.json(["pr", "view", "7", "--json", "title"])
        self.assertEqual(f"{BAD_GATEWAY} (gave up after 2 retries)", str(context.exception))
        self.assertEqual(("api", True), (context.exception.kind, context.exception.transient))
        self.assertEqual([2.0, 4.0], sleeper.waits)
        self.assertEqual(3, len(calls))

    def test_a_dropped_connection_on_a_download_is_retried(self) -> None:
        answers = [failed("connection reset by peer"), ok("")]

        def downloader(arguments: Sequence[str], target: Path) -> CommandResult:
            return answers.pop(0)

        sleeper = Recorder()
        client = GitHubClient(sleeper=sleeper, clock=sleeper.clock, downloader=downloader)
        with tempfile.TemporaryDirectory() as temporary:
            client.download(["api", "repos/o/r/tarball/abc"], Path(temporary) / "source.tar.gz")
        self.assertEqual(([2.0], []), (sleeper.waits, answers))

    def test_without_retry_a_transient_read_fails_at_once_unchanged(self) -> None:
        run, calls = scripted(failed(BAD_GATEWAY))
        client, sleeper = client_for(run)
        with self.assertRaises(GitHubError) as context:
            client.run(["api", "user"], retry=False)
        self.assertEqual(("api", BAD_GATEWAY), (context.exception.kind, str(context.exception)))
        self.assertEqual(([], 1), (sleeper.waits, len(calls)))

    def test_a_change_that_ran_out_its_time_limit_is_an_unknown_outcome_and_a_read_stays_a_timeout(self) -> None:
        stalled = "GitHub CLI did not finish within 1 seconds"

        def stall(arguments: Sequence[str]) -> CommandResult:
            raise GitHubError(stalled, kind="timeout")

        client, sleeper = client_for(stall)
        with self.assertRaises(GitHubError) as context:
            client.run(["pr", "merge", "7", "--squash"])
        self.assertEqual("unknown_outcome", context.exception.kind)
        self.assertEqual(
            f"{stalled} (the outcome of this gh pr merge request is unknown: {UNKNOWN_OUTCOME})",
            str(context.exception),
        )
        with self.assertRaises(GitHubError) as context:
            client.run(["pr", "view", "7"])
        self.assertEqual(("timeout", stalled), (context.exception.kind, str(context.exception)))
        self.assertEqual([], sleeper.waits)

    def test_a_change_still_fails_permanently_and_waits_out_a_rate_limit(self) -> None:
        arguments = ["api", "-X", "DELETE", "repos/o/r/git/refs/heads/topic"]
        client, sleeper = client_for(scripted(failed("gh: Not Found (HTTP 404)"))[0])
        with self.assertRaises(GitHubError) as context:
            client.run(arguments)
        self.assertEqual(("not_found", []), (context.exception.kind, sleeper.waits))
        run, calls = scripted(failed(SECONDARY_LIMIT), ok())
        client, sleeper = client_for(run)
        client.run(arguments)
        self.assertEqual(([5.0], 2), (sleeper.waits, len(calls)))

    def test_rate_limits_and_transient_failures_keep_separate_bounded_counts(self) -> None:
        run, calls = scripted(
            failed(SECONDARY_LIMIT), failed(BAD_GATEWAY), failed(SECONDARY_LIMIT), failed(BAD_GATEWAY), ok()
        )
        client, sleeper = client_for(run)
        client.run(["api", "user"])
        self.assertEqual([5.0, 2.0, 10.0, 4.0], sleeper.waits)
        self.assertEqual(5, len(calls))

    def test_the_worst_case_is_a_finite_number_of_bounded_attempts(self) -> None:
        # Every rate-limit retry and every transient retry spent on one read, each attempt failing.
        answers = [failed(SECONDARY_LIMIT)] * 5 + [failed(BAD_GATEWAY)] * 2 + [failed(SECONDARY_LIMIT)]
        run, calls = scripted(*answers)
        client, sleeper = client_for(run)
        with self.assertRaises(GitHubError) as context:
            client.run(["api", "user"])
        self.assertEqual(f"{SECONDARY_LIMIT} (gave up after 5 retries)", str(context.exception))
        self.assertEqual(8, len(calls))
        self.assertEqual(161.0, sum(sleeper.waits))

    def test_the_transient_policy_values(self) -> None:
        policy = Backoff()
        self.assertEqual((2.0, 2), (policy.transient_base, policy.transient_retries))
        self.assertEqual([2.0, 4.0], [policy.transient_wait(attempt) for attempt in range(2)])
        self.assertEqual(frozenset({502, 503, 504}), github_client.TRANSIENT_STATUSES)


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
        sanitized_environment(self)
        with (
            mock.patch.object(github_client, "run_bounded") as run,
            self.assertRaises(GitHubError) as context,
        ):
            github_client.subprocess_runner(["gh", "api", "user"])
        self.assertEqual("prerequisite", context.exception.kind)
        self.assertEqual(github_client.MISSING_CLI, str(context.exception))
        run.assert_not_called()

    def test_a_cli_that_cannot_start_is_an_execution_error(self) -> None:
        install_gh(sanitized_environment(self)[0])
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
        sanitized_environment(self)
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(github_client, "run_bounded") as run,
            self.assertRaises(GitHubError) as context,
        ):
            github_client.subprocess_downloader(["gh", "api", "x"], Path(temporary) / "out")
        self.assertEqual("prerequisite", context.exception.kind)
        run.assert_not_called()


class JsonTests(unittest.TestCase):
    def test_json_parses_the_body_or_fails_as_malformed(self) -> None:
        client, _ = client_for(scripted(ok('{"login": "octo"}'), ok("not-json"))[0])
        payload: Any = client.json(["api", "user"])
        self.assertEqual({"login": "octo"}, payload)
        with self.assertRaises(GitHubError) as context:
            client.json(["api", "user"])
        self.assertEqual("malformed", context.exception.kind)
        self.assertIn("malformed JSON", str(context.exception))

    def test_json_body_is_the_one_parse_a_callers_own_parse_reuses(self) -> None:
        self.assertEqual([1, "two"], github_client.json_body(github_client.CommandResult(0, '[1, "two"]', "")))

    def test_json_retries_a_graphql_rate_limit_in_the_body(self) -> None:
        limited = json.dumps({"errors": [{"type": "RATE_LIMITED", "message": "slow down"}]})
        client, sleeper = client_for(scripted(ok(limited), ok('{"data": {}}'))[0])
        self.assertEqual({"data": {}}, client.json(["api", "graphql"], graphql=True))
        self.assertEqual([5.0], sleeper.waits)


class SubprocessRunnerContractTests(unittest.TestCase):
    def test_runner_runs_through_the_bounded_layer_and_captures_both_streams(self) -> None:
        program = str(install_gh(sanitized_environment(self)[0]))
        finished = bounded_process.Finished(3, b"o", b"e")
        with mock.patch.object(github_client, "run_bounded", return_value=finished) as run:
            result = github_client.subprocess_runner(["gh", "api", "user"])
        self.assertEqual(CommandResult(3, "o", "e"), result)
        self.assertEqual(mock.call([program, "api", "user"], 300.0, stdout=None, cwd=None), run.call_args)

    def test_both_runners_start_the_gh_the_shared_locator_finds_by_its_absolute_path(self) -> None:
        # A GitHub CLI only in WinGet's Links folder, as `deploy.py check` finds it, runs from there, and one on
        # PATH comes first; process creation alone would look on PATH only.
        path_folder, links = sanitized_environment(self)
        linked = install_gh(links)
        finished = bounded_process.Finished(0, b"", b"")
        with (
            mock.patch.object(github_client, "run_bounded", return_value=finished) as run,
            tempfile.TemporaryDirectory() as temporary,
        ):
            github_client.subprocess_runner(["gh", "api", "user"])
            github_client.subprocess_downloader(["gh", "api", "x"], Path(temporary) / "out")
            on_path = install_gh(path_folder)
            github_client.subprocess_runner(["gh", "api", "user"])
        started = [Path(call.args[0][0]) for call in run.call_args_list]
        self.assertEqual([linked, linked, on_path], started)
        self.assertTrue(all(program.is_absolute() for program in started))
        self.assertEqual(
            [["api", "user"], ["api", "x"], ["api", "user"]], [call.args[0][1:] for call in run.call_args_list]
        )

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
        install_gh(sanitized_environment(self)[0])
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


def command_id(value: object) -> str | None:
    """A test ID that reads an argument list as the gh command it runs, leaving every other value's ID to pytest."""
    return " ".join(["gh", *value]) if isinstance(value, list) else None


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        pytest.param("gh: API rate limit exceeded for user ID 1234. (HTTP 403)", "rate_limit", id="primary-limit"),
        pytest.param(SECONDARY_LIMIT, "rate_limit", id="secondary-limit"),
        pytest.param("gh: HTTP 429: Too Many Requests", "rate_limit", id="too-many-requests"),
        pytest.param("gh: Bad credentials (HTTP 401)", "authentication", id="bad-credentials"),
        pytest.param("gh: Requires authentication (HTTP 401)", "authentication", id="requires-authentication"),
        pytest.param(
            "You are not logged into any GitHub hosts. To log in, run: gh auth login",
            "authentication",
            id="not-logged-in",
        ),
        pytest.param("To get started with GitHub CLI, please run:  gh auth login", "authentication", id="get-started"),
        pytest.param("gh: Only the first 1000 search results are available (HTTP 422)", "search_cap", id="search-cap"),
        pytest.param(
            "gh: Resource protected by organization SAML enforcement. (HTTP 403)", "forbidden", id="saml-enforcement"
        ),
        pytest.param("gh: Not Found (HTTP 404)", "not_found", id="not-found"),
        pytest.param(
            "HTTP 404: Not Found (https://api.github.com/repos/o/r/pulls/9)", "not_found", id="not-found-with-a-url"
        ),
        pytest.param(
            "error connecting to api.github.com\ncheck your internet connection or https://githubstatus.com",
            "network",
            id="error-connecting",
        ),
        pytest.param(
            'Get "https://api.github.com/user": dial tcp: lookup api.github.com: no such host',
            "network",
            id="no-such-host",
        ),
        pytest.param(
            "read tcp 10.0.0.2:51000->140.82.112.6:443: wsarecv: connection reset by peer",
            "network",
            id="connection-reset",
        ),
        pytest.param("dial tcp 140.82.112.6:443: connect: connection refused", "network", id="connection-refused"),
        pytest.param(
            'Post "https://api.github.com/graphql": net/http: TLS handshake timeout',
            "network",
            id="tls-handshake-timeout",
        ),
        pytest.param("dial tcp 140.82.112.6:443: i/o timeout", "network", id="i-o-timeout"),
        pytest.param("gh: Validation Failed (HTTP 422)", "api", id="validation-failed"),
        pytest.param("gh: Server Error (HTTP 502)", "api", id="server-error"),
        pytest.param(
            "GraphQL: Could not resolve to a Repository with the name 'o/r'. (repository)",
            "api",
            id="graphql-could-not-resolve",
        ),
        pytest.param("", "api", id="empty"),
    ],
)
def test_every_failure_class_from_literal_gh_stderr(stderr: str, kind: str) -> None:
    assert kind == classify_failure(stderr)


@pytest.mark.parametrize(
    ("errors", "kind", "retryable"),
    [
        pytest.param([{"type": "RATE_LIMITED", "message": "slow down"}], "rate_limit", True, id="RATE_LIMITED"),
        pytest.param([{"type": "NOT_FOUND", "message": "gone"}], "not_found", False, id="NOT_FOUND"),
        pytest.param([{"type": "FORBIDDEN", "message": "SSO"}], "forbidden", False, id="FORBIDDEN"),
        pytest.param([{"type": "SOMETHING", "message": "odd"}], "api", False, id="another-type"),
        pytest.param({"message": "a lone error"}, "api", False, id="a-lone-error"),
    ],
)
def test_graphql_body_errors_are_classified_by_type(errors: object, kind: str, retryable: bool) -> None:
    error = graphql_failure({"errors": errors})
    if error is None:
        raise AssertionError("each case is a GraphQL failure")
    assert (kind, retryable) == (error.kind, error.retryable)
    assert str(error).startswith("GraphQL request failed"), str(error)


@pytest.mark.parametrize(
    "stderr", ["gh: HTTP 429", "gh: HTTP 401", "error connecting to api.github.com", "gh: HTTP 502"]
)
def test_only_a_rate_limit_is_retryable(stderr: str) -> None:
    client, _ = client_for(scripted(failed(stderr))[0])
    with pytest.raises(GitHubError) as context:
        client.run(["api", "user"], retry=False)
    assert stderr.startswith("gh: HTTP 429") == context.value.retryable


@pytest.mark.parametrize(
    "stdout",
    [
        pytest.param(USER_NOT_FOUND, id="a-graphql-body"),
        pytest.param("", id="empty"),
        pytest.param("not json", id="not-json"),
        pytest.param("[]", id="an-empty-array"),
        pytest.param('{"errors": "text"}', id="errors-as-text"),
        pytest.param('{"errors": [{"message": 5}]}', id="a-message-that-is-not-text"),
    ],
)
def test_a_reason_gh_already_printed_or_a_body_without_reasons_adds_nothing(stdout: str) -> None:
    client, _ = client_for(scripted(failed("gh: Could not resolve to a User.\n", stdout))[0])
    with pytest.raises(GitHubError) as context:
        client.run(["api", "graphql"])
    assert str(context.value) == "gh: Could not resolve to a User."


@pytest.mark.parametrize(
    ("stderr", "stdout", "headers", "transient"),
    [
        pytest.param(BAD_GATEWAY, "", False, True, id="bad-gateway"),
        pytest.param(
            "HTTP 503: Service Unavailable (https://api.github.com/graphql)",
            "",
            False,
            True,
            id="service-unavailable",
        ),
        pytest.param("gh: Gateway Timeout (HTTP 504)", "", False, True, id="gateway-timeout"),
        pytest.param(
            "read tcp 10.0.0.2:51000->140.82.112.6:443: wsarecv: connection reset by peer",
            "",
            False,
            True,
            id="connection-reset",
        ),
        pytest.param("dial tcp 140.82.112.6:443: i/o timeout", "", False, True, id="i-o-timeout"),
        pytest.param(
            'Post "https://api.github.com/graphql": net/http: TLS handshake timeout',
            "",
            False,
            True,
            id="tls-handshake-timeout",
        ),
        pytest.param('Get "https://api.github.com/user": unexpected EOF', "", False, True, id="unexpected-eof"),
        pytest.param(
            "http2: server sent GOAWAY and closed the connection; LastStreamID=1999", "", False, True, id="goaway"
        ),
        pytest.param("gh: Server Error (HTTP 500)", "", False, False, id="server-error"),
        pytest.param("gh: Validation Failed (HTTP 422)", "", False, False, id="validation-failed"),
        pytest.param("gh: Not Found (HTTP 404)", "", False, False, id="not-found"),
        pytest.param("gh: Bad credentials (HTTP 401)", "", False, False, id="bad-credentials"),
        pytest.param(SECONDARY_LIMIT, "", False, False, id="secondary-limit"),
        pytest.param(
            "error connecting to api.github.com\ncheck your internet connection or https://githubstatus.com",
            "",
            False,
            False,
            id="error-connecting",
        ),
        pytest.param(
            "dial tcp 140.82.112.6:443: connect: connection refused", "", False, False, id="connection-refused"
        ),
        pytest.param("", "", False, False, id="empty"),
        *(
            pytest.param(
                "gh: request failed", f"HTTP/2.0 {status} Status\r\n\r\n{{}}", True, transient, id=f"status-{status}"
            )
            for status, transient in ((502, True), (503, True), (504, True), (500, False), (404, False))
        ),
    ],
)
def test_transient_failures_from_literal_gh_stderr_and_status(
    stderr: str, stdout: str, headers: bool, transient: bool
) -> None:
    client, _ = client_for(scripted(failed(stderr, stdout))[0])
    with pytest.raises(GitHubError) as context:
        client.run(["api", "user"], retry=False, headers=headers)
    assert transient == context.value.transient


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        ("gh: Server Error (HTTP 500)", "api"),
        ("gh: Not Found (HTTP 404)", "not_found"),
        ("gh: Bad credentials (HTTP 401)", "authentication"),
        ("dial tcp 140.82.112.6:443: connect: connection refused", "network"),
    ],
)
def test_a_permanent_read_failure_is_not_retried(stderr: str, kind: str) -> None:
    run, calls = scripted(failed(stderr))
    client, sleeper = client_for(run)
    with pytest.raises(GitHubError) as context:
        client.run(["api", "repos/o/r"])
    assert (kind, stderr) == (context.value.kind, str(context.value))
    assert (sleeper.waits, len(calls)) == ([], 1)


@pytest.mark.parametrize("retry", [True, False])
@pytest.mark.parametrize(
    ("arguments", "change"),
    [
        (["api", "-X", "PATCH", "repos/o/r/issues/1", "-f", "state=closed"], "PATCH"),
        (["api", "repos/o/r/issues/1/comments", "-f", "body=hello"], "POST"),
        (["api", "graphql", "-f", "query=mutation { addStar(input: {}) { clientMutationId } }"], "POST"),
        (["pr", "comment", "7", "--body", "hello"], "gh pr comment"),
    ],
    ids=command_id,
)
def test_a_transient_failure_on_a_change_is_an_unknown_outcome_never_retried(
    arguments: list[str], change: str, retry: bool
) -> None:
    run, calls = scripted(failed(BAD_GATEWAY))
    client, sleeper = client_for(run)
    with pytest.raises(GitHubError) as context:
        client.run(arguments, retry=retry)
    assert context.value.kind == "unknown_outcome"
    assert f"{BAD_GATEWAY} (the outcome of this {change} request is unknown: {UNKNOWN_OUTCOME})" == str(context.value)
    assert (context.value.retryable, context.value.transient) == (False, True)
    assert (sleeper.waits, len(calls)) == ([], 1)


@pytest.mark.parametrize(
    ("arguments", "change"),
    [
        (["api", "user"], None),
        (["api", "--paginate", "--slurp", "repos/o/r/pulls?state=open"], None),
        (["api", "-H", "Accept: application/vnd.github.diff", "repos/o/r/pulls/7"], None),
        (["api", "--hostname", "github.com", "rate_limit"], None),
        (["api", "-X", "GET", "search/issues", "-f", "q=is:pr"], None),
        (["api", "--method=get", "search/issues", "-F", "per_page=100"], None),
        (["api", "-XHEAD", "repos/o/r"], None),
        (["api", "graphql", "-f", "query=query($after: String) { viewer { login } }", "-F", "n=5"], None),
        (["api", "graphql", "-F", "query={ viewer { login } }"], None),
        (["pr", "view", "7", "--json", "title"], None),
        (["pr", "list", "--repo", "o/r", "--json", "number"], None),
        (["run", "view", "42", "--log"], None),
        (["pr", "diff", "7"], None),
        (["pr", "checks", "7"], None),
        (["auth", "status"], None),
        (["search", "prs", "--author", "octo"], None),
        (["api", "repos/o/r/issues", "-f", "title=x"], "POST"),
        (["api", "repos/o/r/issues", "--raw-field=title=x"], "POST"),
        (["api", "repos/o/r/issues", "--input", "body.json"], "POST"),
        (["api", "-X", "PUT", "repos/o/r/topics"], "PUT"),
        (["api", "--method", "delete", "repos/o/r"], "DELETE"),
        (["api", "graphql", "-f", "query=mutation { addStar(input: {}) { clientMutationId } }"], "POST"),
        (["api", "graphql", "-f", "query=# a note\nmutation Close { closeIssue }"], "POST"),
        (["api", "graphql", "-F", "query=@query.graphql"], "POST"),
        (["api", "graphql", "--input", "query.json"], "POST"),
        (["api", "graphql"], None),  # gh sends a request with no fields as a GET
        (["api", "-H", "-X", "repos/o/r"], None),
        (["pr", "comment", "7", "--body", "x"], "gh pr comment"),
        (["pr", "merge", "7", "--squash"], "gh pr merge"),
        (["issue", "create", "--title", "x"], "gh issue create"),
        (["pr", "edit", "7", "--add-label", "view"], "gh pr edit"),
        (["workflow", "run", "validate.yml"], "gh workflow run"),
        (["browse"], "gh browse"),
        ([], "gh"),
    ],
    ids=command_id,
)
def test_reads_are_recognized_and_everything_else_is_a_change(arguments: list[str], change: str | None) -> None:
    assert change == github_client.change_of(arguments)


@pytest.mark.parametrize(
    ("data", "text", "count"),
    [
        (b"caf\xe9", "caf\ufffd", 1),
        (b"\xe2\x82 cut short", "\ufffd\ufffd cut short", 2),  # a truncated sequence is two bytes
        ("caf\ufffd already".encode(), "caf\ufffd already", 0),  # a real U+FFFD is not counted
        (b"plain", "plain", 0),
    ],
)
def test_undecodable_bytes_are_replaced_and_counted(data: bytes, text: str, count: int) -> None:
    assert (text, count) == replace_undecodable(undecodable(data))
    client, _ = client_for(scripted(CommandResult(0, undecodable(data), undecodable(b"warn \xff")))[0])
    result = client.run(["api", "user"])
    assert (text, count, "warn \ufffd") == (result.stdout, result.replaced, result.stderr)


@pytest.mark.parametrize("stdout", [pytest.param("", id="empty"), "not-json", "{"])
def test_json_body_is_the_one_parse_a_callers_own_parse_reuses(stdout: str) -> None:
    with pytest.raises(GitHubError) as context:
        github_client.json_body(github_client.CommandResult(0, stdout, ""))
    assert (context.value.kind, context.value.retryable) == ("malformed", False)
    assert str(context.value).startswith("GitHub returned malformed JSON: ")


def test_a_github_error_names_the_clients_classes_and_those_callers_raise() -> None:
    assert get_args(github_client.GitHubErrorKind) == (
        "api",
        "prerequisite",
        "execution",
        "timeout",
        "rate_limit",
        "authentication",
        "search_cap",
        "forbidden",
        "not_found",
        "network",
        "malformed",
        "sso_partial",
        "unknown_outcome",
        "not_file",
        "input",
        "ineligible",
        "incomplete",
        "refused",
    )

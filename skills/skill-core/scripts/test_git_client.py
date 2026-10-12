"""Regression tests for the git client every skill that runs git shares."""

from __future__ import annotations

import contextlib
import dataclasses
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bounded_process
import git_client
from git_client import GitClient, GitError, GitResult, classify_failure
from git_test_base import GitTestCase

NOT_A_REPOSITORY = "fatal: not a git repository (or any of the parent directories): .git\n"


def scripted(
    *responses: GitResult,
) -> tuple[Callable[[Sequence[str], float], GitResult], list[tuple[list[str], float]]]:
    """A runner that answers each call with the next response and records the command and its timeout."""
    queue = list(responses)
    calls: list[tuple[list[str], float]] = []

    def run(command: Sequence[str], timeout: float) -> GitResult:
        calls.append((list(command), timeout))
        return queue.pop(0)

    return run, calls


class ClientTests(GitTestCase):
    def test_run_builds_the_command_and_returns_any_exit_status(self) -> None:
        runner, calls = scripted(GitResult(0, "abc\n", ""), GitResult(1, "", ""))
        client = GitClient(runner)
        self.assertEqual(GitResult(0, "abc\n", ""), client.run(["rev-parse", "HEAD"]))
        self.assertEqual(
            1, client.run(["merge-base", "--is-ancestor", "a", "b"], directory=Path("some dir")).returncode
        )
        self.assertEqual(
            [
                (["git", "rev-parse", "HEAD"], 300.0),
                (["git", "-C", str(Path("some dir")), "merge-base", "--is-ancestor", "a", "b"], 300.0),
            ],
            calls,
        )

    def test_the_timeout_is_the_clients_unless_a_call_names_its_own(self) -> None:
        runner, calls = scripted(*[GitResult(0, "", "")] * 3)
        GitClient(runner).run(["status"])
        GitClient(runner, timeout=20).run(["status"])
        GitClient(runner, timeout=20).output(["fetch"], timeout=600)
        self.assertEqual([300.0, 20, 600], [timeout for _command, timeout in calls])
        self.assertEqual(300.0, git_client.DEFAULT_TIMEOUT_SECONDS)

    def test_output_returns_stdout_or_raises_the_classified_failure(self) -> None:
        runner, _calls = scripted(
            GitResult(0, "main\n", ""), GitResult(128, "", NOT_A_REPOSITORY), GitResult(1, "", "error: boom\n")
        )
        client = GitClient(runner)
        self.assertEqual("main\n", client.output(["branch", "--show-current"]))
        with self.assertRaises(GitError) as context:
            client.output(["status"], directory="elsewhere")
        self.assertEqual(("not_repository", 128), (context.exception.kind, context.exception.returncode))
        self.assertEqual(NOT_A_REPOSITORY.strip(), str(context.exception))
        with self.assertRaises(GitError) as context:
            client.output(["fetch"])
        self.assertEqual(
            ("git", 1, "error: boom"), (context.exception.kind, context.exception.returncode, str(context.exception))
        )

    def test_an_empty_stderr_still_names_the_command_and_its_exit_code(self) -> None:
        runner, _calls = scripted(GitResult(5, "", ""))
        with self.assertRaises(GitError) as context:
            GitClient(runner).output(["fetch", "--all"])
        self.assertEqual("git fetch failed with exit code 5", str(context.exception))

    def test_input_goes_to_the_input_runner_with_the_same_command_timeout_and_classifier(self) -> None:
        runner, plain = scripted(GitResult(0, "plain\n", ""))
        given: list[tuple[list[str], float, bytes]] = []
        answers = [GitResult(0, "fed\n", ""), GitResult(128, "", NOT_A_REPOSITORY)]

        def input_runner(command: Sequence[str], timeout: float, *, input_bytes: bytes) -> GitResult:
            given.append((list(command), timeout, input_bytes))
            return answers.pop(0)

        client = GitClient(runner, timeout=20, input_runner=input_runner)
        self.assertEqual("plain\n", client.output(["status"]))
        self.assertEqual(
            "fed\n", client.output(["hash-object", "--stdin-paths"], directory="repo", input_bytes=b"a\nb\n")
        )
        with self.assertRaises(GitError) as context:
            client.output(["update-index", "--index-info"], timeout=600, input_bytes=b"")
        self.assertEqual(("not_repository", 128), (context.exception.kind, context.exception.returncode))
        self.assertEqual([(["git", "status"], 20)], plain)
        self.assertEqual(
            [
                (["git", "-C", "repo", "hash-object", "--stdin-paths"], 20, b"a\nb\n"),
                (["git", "update-index", "--index-info"], 600, b""),
            ],
            given,
        )

    def test_a_client_given_only_a_runner_refuses_input_instead_of_running_real_git(self) -> None:
        runner, calls = scripted()
        with (
            mock.patch.object(git_client, "run_bounded") as run,
            self.assertRaisesRegex(TypeError, "input_runner"),
        ):
            GitClient(runner).output(["hash-object", "--stdin"], input_bytes=b"hello\n")
        self.assertEqual(([], []), (run.call_args_list, calls))

    def test_the_default_runner_named_explicitly_still_takes_input(self) -> None:
        finished = bounded_process.Finished(0, b"id\n", b"")
        for client in (GitClient(), GitClient(git_client.subprocess_runner)):
            with (
                self.subTest(client=client),
                mock.patch.object(git_client, "run_bounded", return_value=finished) as run,
            ):
                self.assertEqual("id\n", client.output(["hash-object", "--stdin"], input_bytes=b"blob"))
            self.assertEqual(mock.call(["git", "hash-object", "--stdin"], 300.0, input_bytes=b"blob"), run.call_args)

    def test_rev_parse_path_names_the_absolute_path_git_prints(self) -> None:
        runner, calls = scripted(GitResult(0, "C:/a repo/.git\n", ""), GitResult(0, "C:/a repo\n", ""))
        client = GitClient(runner)
        self.assertEqual(Path("C:/a repo/.git"), client.rev_parse_path("--git-common-dir", directory=Path("a dir")))
        self.assertEqual(Path("C:/a repo"), client.rev_parse_path("--show-toplevel", directory="b"))
        self.assertEqual(
            [
                ["git", "-C", "a dir", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                ["git", "-C", "b", "rev-parse", "--path-format=absolute", "--show-toplevel"],
            ],
            [command for command, _timeout in calls],
        )

    def test_rev_parse_path_is_none_outside_a_repository_or_when_git_cannot_answer(self) -> None:
        def failing(command: Sequence[str], timeout: float) -> GitResult:
            raise GitError("git did not finish within 300 seconds", kind="timeout")

        runner, _calls = scripted(GitResult(128, "", NOT_A_REPOSITORY), GitResult(0, "\n", ""))
        self.assertIsNone(GitClient(runner).rev_parse_path("--show-toplevel", directory="x"))
        self.assertIsNone(GitClient(runner).rev_parse_path("--show-toplevel", directory="x"))
        self.assertIsNone(GitClient(failing).rev_parse_path("--git-common-dir", directory="x"))

    def test_classifier_reads_literal_git_stderr(self) -> None:
        self.assertEqual("not_repository", classify_failure(NOT_A_REPOSITORY))
        self.assertEqual("not_repository", classify_failure("fatal: Not A Git Repository: 'x'"))
        self.assertEqual("git", classify_failure("fatal: could not read Username for 'https://github.com'"))
        self.assertEqual("git", classify_failure(""))

    def test_result_is_frozen_and_gives_back_the_exact_bytes(self) -> None:
        result = GitResult(0, b"caf\xe9\r\n".decode("utf-8", "surrogateescape"), "")
        self.assertEqual(b"caf\xe9\r\n", result.output_bytes())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.stdout = "changed"  # type: ignore[misc]  # the assignment is what the test proves fails


# Literal stderr of git's transport failures, as git and curl print them.
EARLY_EOF = (
    "error: RPC failed; curl 56 Recv failure: Connection was reset\n"
    "error: 6253 bytes of body are still expected\n"
    "fetch-pack: unexpected disconnect while reading sideband packet\n"
    "fatal: early EOF\n"
    "fatal: fetch-pack: invalid index-pack output\n"
)
UNRESOLVED = "fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host: github.com\n"
HUNG_UP = "fatal: the remote end hung up unexpectedly\n"
# The permanent failures a retry cannot cure.
PERMANENT = {
    "unknown ref": "fatal: couldn't find remote ref refs/heads/no-such-branch\n",
    "refused authentication": "remote: Invalid username or token.\n"
    "fatal: Authentication failed for 'https://github.com/o/r.git/'\n",
    "missing repository": "remote: Repository not found.\n"
    "fatal: repository 'https://github.com/o/missing.git/' not found\n",
    "bad object": "error: Could not read 0123456789abcdef0123456789abcdef01234567\n"
    "fatal: bad object refs/remotes/origin/main\n",
    "forbidden": "fatal: unable to access 'https://github.com/o/r.git/': The requested URL returned error: 403\n",
    "transient words beside a permanent answer": "error: RPC failed; HTTP 404 curl 22 The requested URL returned "
    "error: 404\nfatal: early EOF\n",
}


class Clock:
    """A sleeper that records waits and advances a fake clock instead of sleeping; runners may advance it too."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


def retrying(*responses: GitResult, timeout: float = 300.0) -> tuple[GitClient, Clock, list[tuple[list[str], float]]]:
    runner, calls = scripted(*responses)
    sleeper = Clock()
    return GitClient(runner, timeout=timeout, sleeper=sleeper, clock=sleeper.clock), sleeper, calls


class TransientTests(GitTestCase):
    def test_a_transient_fetch_is_retried_and_the_second_answer_is_returned(self) -> None:
        # The first fetch updated a ref before the connection dropped; fetch is idempotent, so a second run brings
        # the refs to the same values and its own answer is the one the caller sees.
        partial = GitResult(128, "", " * [new ref]  refs/pull/7/head -> pull/7\n" + EARLY_EOF)
        second = GitResult(0, "", " * [new ref]  refs/pull/8/head -> pull/8\n")
        client, sleeper, calls = retrying(partial, second)
        self.assertIs(second, client.run(["fetch", "--no-tags", "--quiet", "origin", "refs/pull/7/head"]))
        self.assertEqual([2.0], sleeper.waits)
        self.assertEqual(2, len(calls))

    def test_every_transport_message_is_transient_for_fetch_and_ls_remote(self) -> None:
        messages = (
            EARLY_EOF,
            UNRESOLVED,
            HUNG_UP,
            "error: RPC failed; HTTP 502 curl 22 The requested URL returned error: 502\n",
            "fatal: unable to access 'https://github.com/o/r.git/': Recv failure: Connection reset by peer\n",
            "fatal: unable to access 'https://github.com/o/r.git/': Failed to connect to github.com port 443 after "
            "21045 ms: Connection timed out\n",
            "fatal: unable to access 'https://github.com/o/r.git/': schannel: failed to receive handshake, SSL/TLS "
            "connection failed\n",
            "fatal: unable to access 'https://github.com/o/r.git/': OpenSSL SSL_connect: SSL_ERROR_SYSCALL in "
            "connection to github.com:443\n",
            "fatal: unable to access 'https://github.com/o/r.git/': gnutls_handshake() failed: The TLS connection "
            "was non-properly terminated.\n",
            "ssh: Could not resolve hostname github.com: Name or service not known\n"
            "fatal: Could not read from remote repository.\n",
            "fatal: unable to access 'https://github.com/o/r.git/': The requested URL returned error: 503\n",
        )
        for arguments in (["fetch", "origin"], ["ls-remote", "--symref", "origin", "HEAD"]):
            for stderr in messages:
                with self.subTest(arguments[0], stderr=stderr):
                    client, sleeper, calls = retrying(GitResult(128, "", stderr), GitResult(0, "answer\n", ""))
                    self.assertEqual(GitResult(0, "answer\n", ""), client.run(arguments))
                    self.assertEqual(([2.0], 2), (sleeper.waits, len(calls)))

    def test_a_transient_failure_that_outlasts_the_retries_keeps_its_shape_naming_them(self) -> None:
        client, sleeper, calls = retrying(*[GitResult(128, "", UNRESOLVED)] * 3)
        result = client.run(["fetch", "origin", "--quiet"], directory="repo")
        self.assertEqual(GitResult(128, "", UNRESOLVED + "(gave up after 2 retries)\n"), result)
        self.assertEqual([2.0, 4.0], sleeper.waits)
        self.assertEqual([["git", "-C", "repo", "fetch", "origin", "--quiet"]] * 3, [command for command, _ in calls])
        client, _sleeper, _calls = retrying(*[GitResult(128, "", HUNG_UP)] * 3)
        with self.assertRaises(GitError) as context:
            client.output(["ls-remote", "origin"])
        self.assertEqual(("git", 128), (context.exception.kind, context.exception.returncode))
        self.assertEqual(
            "fatal: the remote end hung up unexpectedly\n(gave up after 2 retries)", str(context.exception)
        )

    def test_a_permanent_failure_is_not_retried(self) -> None:
        for name, stderr in PERMANENT.items():
            with self.subTest(name):
                client, sleeper, calls = retrying(GitResult(128, "", stderr))
                self.assertEqual(GitResult(128, "", stderr), client.run(["fetch", "origin", "x"]))
                self.assertEqual(([], 1), (sleeper.waits, len(calls)))

    def test_transient_words_the_remote_or_a_hook_printed_do_not_make_a_failure_transient(self) -> None:
        # A commit message quoted by a reference-transaction hook and a line the server sent hold the words; git's
        # own line names a failure no retry cures.
        stderr = (
            "remote: Retry when the mirror reports an early EOF or Connection reset\n"
            "0123456 fix: retry on RPC failed and Could not resolve host\n"
            "error: cannot lock ref 'refs/remotes/origin/main': is at 0123 but expected 4567\n"
        )
        client, sleeper, calls = retrying(GitResult(1, "", stderr))
        self.assertEqual(GitResult(1, "", stderr), client.run(["fetch", "origin"]))
        self.assertEqual(([], 1), (sleeper.waits, len(calls)))

    def test_push_and_every_other_subcommand_are_never_retried(self) -> None:
        for arguments in (
            ["push", "origin", "main"],
            ["--no-optional-locks", "push", "origin"],
            ["pull", "--ff-only"],
            ["clone", "https://github.com/o/r.git", "r"],
            ["remote", "update"],
            ["status"],
            ["-c", "fetch", "push"],
            [],
        ):
            with self.subTest(arguments=arguments):
                client, sleeper, calls = retrying(GitResult(128, "", EARLY_EOF))
                self.assertEqual(GitResult(128, "", EARLY_EOF), client.run(arguments))
                self.assertEqual(([], 1), (sleeper.waits, len(calls)))

    def test_the_subcommand_is_found_after_git_options(self) -> None:
        for arguments in (
            ["--no-optional-locks", "fetch", "--all", "--prune", "--quiet"],
            ["-c", "http.version=HTTP/1.1", "fetch", "origin"],
            ["--git-dir", "push", "ls-remote", "origin"],
        ):
            with self.subTest(arguments=arguments):
                client, sleeper, _calls = retrying(GitResult(128, "", UNRESOLVED), GitResult(0, "", ""))
                self.assertEqual(0, client.run(arguments).returncode)
                self.assertEqual([2.0], sleeper.waits)

    def test_a_zero_exit_is_an_answer_whatever_stderr_says(self) -> None:
        client, sleeper, calls = retrying(GitResult(0, "", "warning: early EOF in a progress line\n"))
        self.assertEqual(0, client.run(["fetch"]).returncode)
        self.assertEqual(([], 1), (sleeper.waits, len(calls)))

    def test_the_whole_call_stays_inside_its_time_limit(self) -> None:
        sleeper = Clock()
        timeouts: list[float] = []

        def slow(command: Sequence[str], timeout: float) -> GitResult:
            timeouts.append(timeout)
            sleeper.now += 10.0  # each attempt runs ten seconds before the connection drops
            return GitResult(128, "", HUNG_UP)

        result = GitClient(slow, timeout=60, sleeper=sleeper, clock=sleeper.clock).run(["fetch"])
        # 10 s, a 2 s wait, then 60 - 12 = 48 s left; 10 s more, a 4 s wait, then 60 - 26 = 34 s left.
        self.assertEqual([60, 48.0, 34.0], timeouts)
        self.assertEqual([2.0, 4.0], sleeper.waits)
        self.assertEqual(36.0, sleeper.now)
        self.assertTrue(result.stderr.endswith("(gave up after 2 retries)\n"))

    def test_a_wait_that_would_not_fit_in_the_time_limit_is_not_made(self) -> None:
        sleeper = Clock()

        def slow(command: Sequence[str], timeout: float) -> GitResult:
            sleeper.now += 9.0
            return GitResult(128, "", HUNG_UP)

        result = GitClient(slow, timeout=15, sleeper=sleeper, clock=sleeper.clock).run(["ls-remote", "origin"])
        # 9 s, a 2 s wait, and 9 s more leave no room for the 4 s wait, so it is never made.
        self.assertEqual([2.0], sleeper.waits)
        self.assertEqual(
            HUNG_UP + "(gave up after 1 retry: its 15-second time limit left no room for another)\n", result.stderr
        )
        client, sleeper, calls = retrying(GitResult(128, "", HUNG_UP), timeout=2)
        self.assertEqual(
            HUNG_UP + "(gave up after 0 retries: its 2-second time limit left no room for another)\n",
            client.run(["fetch"]).stderr,
        )
        self.assertEqual(([], 1), (sleeper.waits, len(calls)))

    def test_a_retry_that_runs_out_the_time_limit_returns_the_failure_before_it(self) -> None:
        answers: list[GitResult | GitError] = [
            GitResult(128, "", UNRESOLVED),
            GitError("git fetch did not finish within 298 seconds", kind="timeout"),
        ]

        def runner(command: Sequence[str], timeout: float) -> GitResult:
            answer = answers.pop(0)
            if isinstance(answer, GitError):
                raise answer
            return answer

        sleeper = Clock()
        result = GitClient(runner, sleeper=sleeper, clock=sleeper.clock).run(["fetch"])
        self.assertEqual(
            GitResult(128, "", UNRESOLVED + "(gave up after 1 retry: it ran out its 300-second time limit)\n"), result
        )

    def test_a_first_attempt_that_runs_out_its_time_limit_still_raises(self) -> None:
        def stalled(command: Sequence[str], timeout: float) -> GitResult:
            raise GitError("git fetch did not finish within 300 seconds", kind="timeout")

        sleeper = Clock()
        with self.assertRaises(GitError) as context:
            GitClient(stalled, sleeper=sleeper, clock=sleeper.clock).run(["fetch"])
        self.assertEqual("timeout", context.exception.kind)
        self.assertEqual([], sleeper.waits)

    def test_input_is_given_again_to_each_attempt(self) -> None:
        given: list[bytes] = []
        answers = [GitResult(128, "", HUNG_UP), GitResult(0, "", "")]

        def input_runner(command: Sequence[str], timeout: float, *, input_bytes: bytes) -> GitResult:
            given.append(input_bytes)
            return answers.pop(0)

        sleeper = Clock()
        client = GitClient(scripted()[0], input_runner=input_runner, sleeper=sleeper, clock=sleeper.clock)
        self.assertEqual(0, client.run(["fetch", "--stdin", "origin"], input_bytes=b"main\n").returncode)
        self.assertEqual([b"main\n", b"main\n"], given)

    def test_the_default_sleeper_and_clock_are_looked_up_per_client(self) -> None:
        runner, _calls = scripted(GitResult(128, "", HUNG_UP), GitResult(0, "", ""))
        with mock.patch("time.sleep") as sleep:
            GitClient(runner).run(["fetch"])
        self.assertEqual([mock.call(2.0)], sleep.call_args_list)

    def test_the_retry_policy_values(self) -> None:
        self.assertEqual(frozenset({"fetch", "ls-remote"}), git_client.RETRIED_SUBCOMMANDS)
        self.assertEqual((2, 2.0), (git_client.TRANSIENT_RETRIES, git_client.TRANSIENT_BASE_SECONDS))
        self.assertEqual([2.0, 4.0], [git_client.transient_wait(retry) for retry in range(2)])
        self.assertEqual(("fatal:", "error:", "fetch-pack:", "ssh:"), git_client.GIT_LINE_PREFIXES)
        self.assertIn("could not resolve host", git_client.TRANSIENT_MARKERS)
        self.assertIn("couldn't find remote ref", git_client.PERMANENT_MARKERS)


class SubprocessRunnerTests(GitTestCase):
    def test_the_runner_goes_through_the_bounded_layer(self) -> None:
        finished = bounded_process.Finished(2, b"out \xff", b"bad \xff")
        with mock.patch.object(git_client, "run_bounded", return_value=finished) as run:
            result = git_client.subprocess_runner(["git", "status"], 12)
        self.assertEqual(mock.call(["git", "status"], 12), run.call_args)
        self.assertEqual(GitResult(2, "out \udcff", "bad �"), result)

    def test_given_input_goes_through_the_bounded_layer(self) -> None:
        finished = bounded_process.Finished(0, b"id\n", b"")
        with mock.patch.object(git_client, "run_bounded", return_value=finished) as run:
            result = git_client.subprocess_runner(["git", "hash-object", "--stdin"], 12, input_bytes=b"blob")
        self.assertEqual(mock.call(["git", "hash-object", "--stdin"], 12, input_bytes=b"blob"), run.call_args)
        self.assertEqual(GitResult(0, "id\n", ""), result)

    def test_missing_git_is_a_prerequisite_error(self) -> None:
        with (
            mock.patch.object(git_client, "run_bounded", side_effect=FileNotFoundError("git")),
            self.assertRaises(GitError) as context,
        ):
            GitClient().run(["status"])
        self.assertEqual("prerequisite", context.exception.kind)
        self.assertIn("install Git", str(context.exception))
        with self.assertRaises(GitError) as context:
            git_client.subprocess_runner(["coding-agent-skills-no-such-git"], 60)
        self.assertEqual("prerequisite", context.exception.kind)

    def test_git_that_cannot_start_is_an_execution_error(self) -> None:
        with (
            mock.patch.object(git_client, "run_bounded", side_effect=PermissionError("denied")),
            self.assertRaises(GitError) as context,
        ):
            GitClient().run(["status"])
        self.assertEqual("execution", context.exception.kind)

    def test_a_command_that_runs_too_long_is_a_timeout(self) -> None:
        with self.assertRaises(GitError) as context:
            git_client.subprocess_runner([sys.executable, "-c", "import time; time.sleep(60)"], 0.5)
        self.assertEqual("timeout", context.exception.kind)
        self.assertIn("did not finish within 0.5 seconds", str(context.exception))
        self.assertIsInstance(context.exception.__cause__, subprocess.TimeoutExpired)

    def test_a_command_given_input_that_runs_too_long_is_a_timeout(self) -> None:
        program = "import sys, time; sys.stdin.buffer.read(); time.sleep(60)"
        with self.assertRaises(GitError) as context:
            git_client.subprocess_runner([sys.executable, "-c", program], 0.5, input_bytes=b"request\n")
        self.assertEqual("timeout", context.exception.kind)
        self.assertIsInstance(context.exception.__cause__, subprocess.TimeoutExpired)

    def test_given_input_reaches_the_command_byte_for_byte_and_then_ends(self) -> None:
        # The stub reports, in hex, every byte it read and what a second read found once the first reached the end.
        program = (
            "import sys; first = sys.stdin.buffer.read(); after = sys.stdin.buffer.read(); "
            "sys.stdout.write(first.hex() + ' ' + after.hex() + '.')"
        )
        given = b"caf\xe9\r\n\x00no newline at the end"
        result = git_client.subprocess_runner([sys.executable, "-c", program], 60, input_bytes=given)
        self.assertEqual(GitResult(0, given.hex() + " .", ""), result)

    def test_a_command_given_no_input_reads_an_empty_stdin(self) -> None:
        program = "import sys; sys.stdout.write(repr(sys.stdin.buffer.read()))"
        for given in (None, b""):
            with self.subTest(given=given):
                result = git_client.subprocess_runner([sys.executable, "-c", program], 60, input_bytes=given)
                self.assertEqual(GitResult(0, "b''", ""), result)

    def test_a_command_that_reads_none_of_its_input_still_finishes(self) -> None:
        # Far more than a pipe holds, so a command that never reads it could block a writer that used a pipe.
        result = git_client.subprocess_runner(
            [sys.executable, "-c", "print('done')"], 60, input_bytes=b"x" * (4 * 1024 * 1024)
        )
        self.assertEqual((0, "done"), (result.returncode, result.stdout.strip()))

    def test_real_git_reads_given_input_as_its_whole_stdin(self) -> None:
        # The blob id of "hello\n", as git has always printed it.
        self.assertEqual(
            "ce013625030ba8dba906f756967f9e9ca394464a\n",
            GitClient().output(["hash-object", "--stdin"], input_bytes=b"hello\n"),
        )

    def test_real_git_outside_a_repository_is_not_a_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "plain folder"
            directory.mkdir()
            for arguments, given in ((["rev-parse", "--show-toplevel"], None), (["update-index", "--index-info"], b"")):
                with (
                    self.subTest(arguments[0]),
                    mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": temporary}),
                    self.assertRaises(GitError) as context,
                ):
                    GitClient().output(arguments, directory=directory, input_bytes=given)
                self.assertEqual("not_repository", context.exception.kind)


class StreamTests(GitTestCase):
    def test_cat_file_batch_answers_with_the_exact_bytes(self) -> None:
        content = b"caf\xe9\r\nno newline at the end"
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "a repository"
            client = GitClient(timeout=60)
            client.output(["init", "--quiet", str(repository)])
            source = repository / "file.bin"
            source.write_bytes(content)
            object_id = client.output(
                ["hash-object", "-w", "--no-filters", "--", "file.bin"], directory=repository
            ).strip()
            with client.stream(["cat-file", "--batch"], directory=repository) as stream:
                stream.stdin.write(f"{object_id}\n".encode("ascii"))
                stream.stdin.close()
                self.assertEqual(object_id.encode("ascii"), stream.readline(len(object_id)))
                self.assertEqual(f" blob {len(content)}\n".encode("ascii"), stream.readline())
                self.assertEqual(content, stream.read(len(content)))
                self.assertEqual(b"\n", stream.read(1))
                self.assertEqual(b"", stream.read(1))
                self.assertEqual(0, stream.wait())
                self.assertEqual("", stream.stderr())

    def test_a_read_that_gets_no_output_is_a_timeout(self) -> None:
        program = "import time; time.sleep(60)"
        with (
            bounded_process.streaming([sys.executable, "-c", program], idle_timeout=0.5, exit_wait=0.5) as running,
            self.assertRaises(GitError) as context,
        ):
            git_client.GitStream(running).readline()
        self.assertEqual("timeout", context.exception.kind)
        self.assertIn("gave no output for 0.5 seconds", str(context.exception))

    def test_missing_git_or_git_that_cannot_start_is_classified(self) -> None:
        for error, kind in ((FileNotFoundError("git"), "prerequisite"), (PermissionError("denied"), "execution")):
            with (
                self.subTest(kind=kind),
                mock.patch.object(git_client, "streaming", side_effect=error),
                self.assertRaises(GitError) as context,
                GitClient().stream(["cat-file", "--batch"]),
            ):
                pass
            self.assertEqual(kind, context.exception.kind)

    def test_the_idle_timeout_is_the_clients_unless_the_stream_names_its_own(self) -> None:
        bounds: list[float] = []

        @contextlib.contextmanager
        def recording(command: Sequence[str], *, idle_timeout: float) -> Iterator[bounded_process.Streaming]:
            bounds.append(idle_timeout)
            self.assertEqual(["git", "-C", "here", "cat-file", "--batch"], list(command))
            with bounded_process.streaming([sys.executable, "-c", "pass"], idle_timeout=idle_timeout) as running:
                running.stdin.close()
                yield running

        with mock.patch.object(git_client, "streaming", recording):
            with GitClient(timeout=20).stream(["cat-file", "--batch"], directory="here"):
                pass
            with GitClient(timeout=20).stream(["cat-file", "--batch"], directory="here", idle_timeout=5):
                pass
        self.assertEqual([20, 5], bounds)


if __name__ == "__main__":
    unittest.main()

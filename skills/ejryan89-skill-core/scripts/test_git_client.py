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
from typing import Any, get_args
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bounded_process
import executable_locator
import git_client
from git_client import GitClient, GitError, GitResult, classify_failure
from job_tree import GroupTree, JobTree, TreeNotEnded
from process_identity import process_status

NOT_A_REPOSITORY = "fatal: not a git repository (or any of the parent directories): .git\n"


def git() -> str:
    """The absolute path the client starts git at."""
    return executable_locator.located_command(["git"])[0]


# Starts a program that runs on, writes its PID to the file named by its first argument once it has started it, and
# then runs on itself, as a git that starts a transport or an alias's program and hangs does.
SPAWNING_PROGRAM = (
    "import os, subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "open(sys.argv[1] + '.partial', 'w').write(str(child.pid))\n"
    "os.replace(sys.argv[1] + '.partial', sys.argv[1])\n"
    "time.sleep(120)\n"
)
# The first time limit a spawning command gets, doubled until the command had started its program within it, so a
# loaded machine's slow start never decides the test.
FIRST_LIMIT_SECONDS = 2.0
LAST_LIMIT_SECONDS = 64.0
NOT_ENDED = "The job's processes may still run: process 7 had not ended 10 seconds on"


def bounded_call(
    command: Sequence[str], timeout: float, **options: object
) -> tuple[tuple[object, ...], dict[str, object]]:
    """A call of the bounded layer as the client makes it, its process tree checked and left out."""
    tree = options.pop("tree", None)
    if not isinstance(tree, (JobTree, GroupTree)) or not tree.hidden_window:
        raise AssertionError(f"git runs in a process tree that hides its window, not {tree!r}")
    return (list(command), timeout), options


def call_shape(call: Any) -> tuple[tuple[object, ...], dict[str, object]]:
    """A recorded call of the bounded layer, its process tree checked and left out."""
    return bounded_call(*call.args, **call.kwargs)


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


class ClientTests(unittest.TestCase):
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
            self.assertEqual(
                (([git(), "hash-object", "--stdin"], 300.0), {"input_bytes": b"blob"}), call_shape(run.call_args)
            )

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

    def test_rev_parse_path_is_none_only_when_git_says_not_a_repository(self) -> None:
        runner, _calls = scripted(GitResult(128, "", NOT_A_REPOSITORY))
        self.assertIsNone(GitClient(runner).rev_parse_path("--show-toplevel", directory="x"))

    def test_rev_parse_path_raises_every_other_failure_with_gits_reason(self) -> None:
        def failing(command: Sequence[str], timeout: float) -> GitResult:
            raise GitError("git did not finish within 300 seconds", kind="timeout")

        dubious = "fatal: detected dubious ownership in repository at 'C:/a repo'\n"
        for name, client, kind, reason in (
            ("dubious ownership", GitClient(scripted(GitResult(128, "", dubious))[0]), "git", "dubious ownership"),
            ("timeout", GitClient(failing), "timeout", "did not finish"),
            ("no path", GitClient(scripted(GitResult(0, "\n", ""))[0]), "git", "not one path for each"),
        ):
            with self.subTest(name), self.assertRaises(GitError) as context:
                client.rev_parse_path("--show-toplevel", directory="x")
            self.assertEqual(kind, context.exception.kind)
            self.assertIn(reason, str(context.exception))

    def test_rev_parse_paths_names_one_path_per_option_or_raises(self) -> None:
        runner, calls = scripted(GitResult(0, "C:/a\nC:/a/.git\n", ""), GitResult(0, "C:/a\n", ""))
        client = GitClient(runner)
        options = ["--show-toplevel", "--git-common-dir"]
        self.assertEqual([Path("C:/a"), Path("C:/a/.git")], client.rev_parse_paths(options, directory="d"))
        self.assertEqual(["git", "-C", "d", "rev-parse", "--path-format=absolute", *options], calls[0][0])
        with self.assertRaises(GitError) as context:
            client.rev_parse_paths(options, directory="d")
        self.assertIn("for --show-toplevel --git-common-dir in d, not one path for each", str(context.exception))

    def test_quiet_output_is_none_only_for_exit_1_with_nothing_on_stderr(self) -> None:
        runner, calls = scripted(
            GitResult(0, "found\n", ""),
            GitResult(1, "", ""),
            GitResult(1, "", "warning: ignoring broken ref refs/heads/side\n"),
            GitResult(128, "", "fatal: bad config line 1 in file .git/config\n"),
            GitResult(128, "", NOT_A_REPOSITORY),
            GitResult(2, "", ""),
        )
        client = GitClient(runner)
        arguments = ["rev-parse", "--verify", "--quiet", "side"]
        self.assertEqual("found\n", client.quiet_output(arguments, directory="d", timeout=5))
        self.assertEqual((["git", "-C", "d", *arguments], 5), calls[0])
        self.assertIsNone(client.quiet_output(arguments))
        for kind, returncode, reason in (
            ("git", 1, "warning: ignoring broken ref refs/heads/side"),
            ("git", 128, "fatal: bad config line 1 in file .git/config"),
            ("not_repository", 128, "fatal: not a git repository"),
            ("git", 2, "git rev-parse failed with exit code 2"),
        ):
            with self.subTest(reason), self.assertRaises(GitError) as context:
                client.quiet_output(arguments)
            self.assertEqual((kind, returncode), (context.exception.kind, context.exception.returncode))
            self.assertIn(reason, str(context.exception))

    def test_a_quiet_failure_with_nothing_on_stderr_names_the_subcommand_and_its_exit_code(self) -> None:
        client = GitClient(scripted(GitResult(2, "", ""))[0])
        with self.assertRaises(GitError) as context:
            client.quiet_output(["rev-parse", "--verify", "--quiet", "side"])
        self.assertEqual("git rev-parse failed with exit code 2", str(context.exception))
        self.assertEqual(("git", 2), (context.exception.kind, context.exception.returncode))

    def test_a_blob_git_cannot_print_names_the_object_gits_reason_its_kind_and_exit_code(self) -> None:
        name = "c" * 40
        runner, calls = scripted(
            GitResult(0, f"{name}\n", ""), GitResult(128, "", "fatal: not a git repository: '.git'\n")
        )
        with self.assertRaises(GitError) as context:
            GitClient(runner).file_at("HEAD", "a file", directory="d")
        self.assertEqual("HEAD:a file: fatal: not a git repository: '.git'", str(context.exception))
        self.assertEqual(("not_repository", 128), (context.exception.kind, context.exception.returncode))
        self.assertEqual(["git", "-C", "d", "cat-file", "blob", name], calls[1][0])

    def test_a_blob_failure_with_nothing_on_stderr_names_cat_file_and_its_exit_code(self) -> None:
        runner, _calls = scripted(GitResult(0, "c" * 40 + "\n", ""), GitResult(3, "", ""))
        with self.assertRaises(GitError) as context:
            GitClient(runner).file_at("HEAD", "a file", directory="d")
        self.assertEqual("HEAD:a file: git cat-file failed with exit code 3", str(context.exception))
        self.assertEqual(("git", 3), (context.exception.kind, context.exception.returncode))

    def test_commit_of_refuses_an_answer_that_is_not_one_object_name(self) -> None:
        runner, calls = scripted(GitResult(0, "a" * 40 + "\n", ""), GitResult(0, "b" * 64 + "\n", ""))
        client = GitClient(runner)
        self.assertEqual("a" * 40, client.commit_of("main", directory="d"))
        self.assertEqual(
            ["git", "-C", "d", "rev-parse", "--verify", "--quiet", "--end-of-options", "main^{commit}"], calls[0][0]
        )
        self.assertEqual("b" * 64, client.commit_of("main", directory="d"))
        for printed in ("a" * 39, "A" * 40, "a" * 40 + "\n" + "a" * 40, ""):
            with self.subTest(printed=printed), self.assertRaises(GitError) as context:
                GitClient(scripted(GitResult(0, printed, ""))[0]).commit_of("main", directory="d")
            self.assertIn("not one object name", str(context.exception))

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


class TransientTests(unittest.TestCase):
    def test_a_transient_fetch_is_retried_and_the_second_answer_is_returned(self) -> None:
        # The first fetch updated a ref before the connection dropped; fetch is idempotent, so a second run brings
        # the refs to the same values and its own answer is the one the caller sees.
        partial = GitResult(128, "", " * [new ref]  refs/pull/7/head -> pull/7\n" + EARLY_EOF)
        second = GitResult(0, "", " * [new ref]  refs/pull/8/head -> pull/8\n")
        client, sleeper, calls = retrying(partial, second)
        self.assertIs(second, client.run(["fetch", "--no-tags", "--quiet", "origin", "refs/pull/7/head"]))
        self.assertEqual([2.0], sleeper.waits)
        self.assertEqual(2, len(calls))

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


class SubprocessRunnerTests(unittest.TestCase):
    def test_the_runner_goes_through_the_bounded_layer(self) -> None:
        finished = bounded_process.Finished(2, b"out \xff", b"bad \xff")
        with mock.patch.object(git_client, "run_bounded", return_value=finished) as run:
            result = git_client.subprocess_runner(["git", "status"], 12)
        self.assertEqual((([git(), "status"], 12), {}), call_shape(run.call_args))
        self.assertEqual(GitResult(2, "out \udcff", "bad �"), result)

    def test_given_input_goes_through_the_bounded_layer(self) -> None:
        finished = bounded_process.Finished(0, b"id\n", b"")
        with mock.patch.object(git_client, "run_bounded", return_value=finished) as run:
            result = git_client.subprocess_runner(["git", "hash-object", "--stdin"], 12, input_bytes=b"blob")
        self.assertEqual((([git(), "hash-object", "--stdin"], 12), {"input_bytes": b"blob"}), call_shape(run.call_args))
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

    def test_a_program_a_timed_out_command_started_is_ended_with_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            started = Path(temporary) / "started pid"
            limit = FIRST_LIMIT_SECONDS
            while True:
                with self.assertRaises(GitError) as context:
                    git_client.subprocess_runner([sys.executable, "-c", SPAWNING_PROGRAM, str(started)], limit)
                self.assertEqual("timeout", context.exception.kind)
                if started.exists() or limit >= LAST_LIMIT_SECONDS:
                    break
                limit *= 2
            child = int(started.read_text(encoding="utf-8"))
        self.assertFalse(process_status(child).alive, "the program the timed-out command started was ended")

    def test_a_program_a_finished_command_left_running_is_ended_with_it(self) -> None:
        program = (
            "import subprocess, sys\n"
            "print(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']).pid)\n"
        )
        result = git_client.subprocess_runner([sys.executable, "-c", program], 60)
        self.assertEqual(0, result.returncode)
        self.assertFalse(process_status(int(result.stdout)).alive, "the program the command left was ended")

    def test_programs_that_could_not_be_ended_are_an_execution_error(self) -> None:
        not_ended = TreeNotEnded(NOT_ENDED)
        with (
            mock.patch.object(git_client, "run_bounded", side_effect=not_ended),
            self.assertRaises(GitError) as context,
        ):
            GitClient().run(["fetch"])
        self.assertEqual("execution", context.exception.kind)
        self.assertEqual(
            f"git fetch left programs it started that could not be ended: {NOT_ENDED}", str(context.exception)
        )
        self.assertIs(not_ended, context.exception.__cause__)

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


class ThreeAnswerTests(unittest.TestCase):
    """Found, absent, and could not tell, from real git in a throwaway repository."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="three answers ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "a repository"
        self.client = GitClient(timeout=60)
        self.client.output(["init", "--quiet", str(self.repository)])
        (self.repository / "a file.txt").write_bytes(b"caf\xe9\r\n")
        (self.repository / "a folder").mkdir()
        (self.repository / "a folder" / "inner").write_text("x\n", encoding="utf-8")
        self.client.output(["add", "--", "a file.txt", "a folder"], directory=self.repository)
        identity = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
        self.client.output([*identity, "commit", "--quiet", "-m", "first"], directory=self.repository)
        self.head = self.client.output(["rev-parse", "HEAD"], directory=self.repository).strip()

    def break_configuration(self) -> None:
        (self.repository / ".git" / "config").write_text("[core\nbogus\n", encoding="utf-8")

    def test_found_and_absent(self) -> None:
        self.assertEqual(self.head, self.client.commit_of("HEAD", directory=self.repository))
        self.assertIsNone(self.client.commit_of("no-such-branch", directory=self.repository))
        self.assertIsNone(self.client.commit_of("-n", directory=self.repository))
        self.assertEqual(b"caf\xe9\r\n", self.client.file_at("HEAD", "a file.txt", directory=self.repository))
        self.assertIsNone(self.client.file_at("HEAD", "no such file", directory=self.repository))
        self.assertIsNone(self.client.file_at("0" * 40, "a file.txt", directory=self.repository))
        top = self.client.rev_parse_path("--show-toplevel", directory=self.repository / "a folder")
        self.assertEqual(self.repository.resolve(), top.resolve() if top else None)
        plain = self.root / "plain folder"
        plain.mkdir()
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.root)}):
            self.assertIsNone(self.client.rev_parse_path("--show-toplevel", directory=plain))

    def test_a_broken_ref_is_named_not_taken_for_absent(self) -> None:
        (self.repository / ".git" / "refs" / "heads" / "broken").write_text("nonsense\n", encoding="utf-8")
        with self.assertRaises(GitError) as context:
            self.client.commit_of("broken", directory=self.repository)
        self.assertIn("ignoring broken ref refs/heads/broken", str(context.exception))

    def test_a_folder_is_not_a_file(self) -> None:
        with self.assertRaises(GitError) as context:
            self.client.file_at("HEAD", "a folder", directory=self.repository)
        self.assertIn("HEAD:a folder", str(context.exception))

    def test_a_repository_git_cannot_read_is_named_by_every_form(self) -> None:
        self.break_configuration()
        for name, call in (
            ("rev_parse_path", lambda: self.client.rev_parse_path("--show-toplevel", directory=self.repository)),
            ("commit_of", lambda: self.client.commit_of("HEAD", directory=self.repository)),
            ("file_at", lambda: self.client.file_at("HEAD", "a file.txt", directory=self.repository)),
        ):
            with self.subTest(name), self.assertRaises(GitError) as context:
                call()
            self.assertEqual("git", context.exception.kind)
            self.assertIn("bad config line 1", str(context.exception))


class StreamTests(unittest.TestCase):
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
        self.assertRegex(str(context.exception), r" gave no output for 0\.5 seconds$")

    def test_a_sized_read_that_gets_no_output_is_a_timeout_that_says_so(self) -> None:
        program = "import time; time.sleep(60)"
        with (
            bounded_process.streaming([sys.executable, "-c", program], idle_timeout=0.5, exit_wait=0.5) as running,
            self.assertRaises(GitError) as context,
        ):
            git_client.GitStream(running).read(1)
        self.assertEqual("timeout", context.exception.kind)
        self.assertRegex(str(context.exception), r" gave no output for 0\.5 seconds$")

    def test_a_wait_for_git_that_has_not_exited_is_a_timeout_that_says_so(self) -> None:
        program = "import time; time.sleep(60)"
        with (
            bounded_process.streaming([sys.executable, "-c", program], idle_timeout=0.5, exit_wait=0.5) as running,
            self.assertRaises(GitError) as context,
        ):
            git_client.GitStream(running).wait()
        self.assertEqual("timeout", context.exception.kind)
        self.assertRegex(str(context.exception), r" had not exited after 0\.5 seconds$")

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
        def recording(
            command: Sequence[str], *, idle_timeout: float, tree: object
        ) -> Iterator[bounded_process.Streaming]:
            bounded_call(command, idle_timeout, tree=tree)
            bounds.append(idle_timeout)
            self.assertEqual([git(), "-C", "here", "cat-file", "--batch"], list(command))
            with bounded_process.streaming([sys.executable, "-c", "pass"], idle_timeout=idle_timeout) as running:
                running.stdin.close()
                yield running

        with mock.patch.object(git_client, "streaming", recording):
            with GitClient(timeout=20).stream(["cat-file", "--batch"], directory="here"):
                pass
            with GitClient(timeout=20).stream(["cat-file", "--batch"], directory="here", idle_timeout=5):
                pass
        self.assertEqual([20, 5], bounds)

    def test_a_program_git_left_running_is_ended_once_the_stream_closes(self) -> None:
        program = (
            "import subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "sys.stdout.write(str(child.pid) + chr(10))\n"
            "sys.stdout.flush()\n"
            "sys.stdin.read()\n"
        )
        with (
            mock.patch.object(git_client, "located_command", return_value=[sys.executable, "-c", program]),
            GitClient(timeout=60).stream(["cat-file", "--batch"]) as stream,
        ):
            child = int(stream.readline())
            self.assertTrue(process_status(child).alive, "the program runs while the stream is open")
            stream.stdin.close()
            self.assertEqual(0, stream.wait())
        self.assertFalse(process_status(child).alive, "the program git left was ended once the stream closed")

    def test_programs_a_stream_could_not_end_are_an_execution_error(self) -> None:
        not_ended = TreeNotEnded(NOT_ENDED)

        @contextlib.contextmanager
        def unending(command: Sequence[str], **_options: object) -> Iterator[bounded_process.Streaming]:
            with bounded_process.streaming([sys.executable, "-c", "pass"], idle_timeout=60) as running:
                running.stdin.close()
                yield running
            raise not_ended

        with (
            mock.patch.object(git_client, "streaming", unending),
            self.assertRaises(GitError) as context,
            GitClient().stream(["cat-file", "--batch"]),
        ):
            pass
        self.assertEqual("execution", context.exception.kind)
        self.assertEqual(
            f"git cat-file --batch left programs it started that could not be ended: {NOT_ENDED}",
            str(context.exception),
        )
        self.assertIs(not_ended, context.exception.__cause__)


class WorkingDirectoryTests(unittest.TestCase):
    """A git in the working directory is never the git the client starts: Windows process creation looks a bare name
    up there before PATH, and a relative PATH entry such as `.` names it on every platform."""

    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="git client ")
        self.addCleanup(temporary.cleanup)
        self.planted = Path(temporary.name)
        # Not a program Windows can start, and a script that answers in git's place elsewhere.
        program = self.planted / ("git.exe" if sys.platform == "win32" else "git")
        program.write_bytes(b"#!/bin/sh\necho planted git\n")
        program.chmod(0o755)
        # Without the variable that stops it, Windows' own search finds the planted git on any Python.
        stop = "NoDefaultCurrentDirectoryInExePath".casefold()
        environment = {key: value for key, value in os.environ.items() if key.casefold() != stop}
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Registered after the folder's cleanup, so it runs first: Windows refuses to remove a working directory.
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.planted)

    def path_values(self) -> dict[str, str]:
        """PATH as it is, and with entries that name the working directory relative to it before it."""
        path = os.environ.get("PATH", "")
        return {"as it is": path, "relative entries first": os.pathsep.join((".", "", path))}

    def test_run_starts_the_git_on_path_and_never_one_in_the_working_directory(self) -> None:
        for label, path in self.path_values().items():
            with self.subTest(path=label), mock.patch.dict(os.environ, {"PATH": path}):
                result = GitClient(timeout=60).run(["--version"])
                self.assertEqual((0, "git version"), (result.returncode, result.stdout[:11]))
                self.assertNotEqual(self.planted, Path(git()).parent)

    def test_stream_starts_the_git_on_path_and_never_one_in_the_working_directory(self) -> None:
        for label, path in self.path_values().items():
            with (
                self.subTest(path=label),
                mock.patch.dict(os.environ, {"PATH": path}),
                GitClient(timeout=60).stream(["--version"]) as stream,
            ):
                stream.stdin.close()
                self.assertEqual(b"git version", stream.read(11))

    def test_git_is_looked_up_once_for_each_search_environment(self) -> None:
        finished = bounded_process.Finished(0, b"", b"")
        program = git()
        with (
            mock.patch.dict(git_client.FOUND, clear=True),
            mock.patch.object(
                executable_locator, "find_executable", wraps=executable_locator.find_executable
            ) as lookup,
            mock.patch.object(git_client, "run_bounded", return_value=finished) as run,
        ):
            GitClient().run(["status"])
            GitClient().run(["log"])
            self.assertEqual(1, lookup.call_count)
            self.assertEqual([[program, "status"], [program, "log"]], [call.args[0] for call in run.call_args_list])
            self.assertTrue(Path(program).is_absolute())
            with mock.patch.dict(os.environ, {"PATH": os.pathsep.join((os.environ["PATH"], str(self.planted)))}):
                GitClient().run(["status"])
            self.assertEqual(2, lookup.call_count, "a changed PATH is looked up again")

    def test_a_git_the_locator_cannot_find_is_a_prerequisite_error(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": str(self.planted / "never a folder")}):
            with self.assertRaises(GitError) as context:
                GitClient().run(["--version"])
            self.assertEqual("prerequisite", context.exception.kind)
            with self.assertRaises(GitError) as context, GitClient().stream(["--version"]):
                pass
            self.assertEqual("prerequisite", context.exception.kind)


# The listing the issue reproduces the defect with, and what each of its two repositories answers.
LISTING = ["for-each-ref", "--format=%(refname)", "refs/heads"]
NAMED_LISTING = b"refs/heads/main\nrefs/heads/only-in-a\n"
OTHER_LISTING = b"refs/heads/main\n"


class RepositoryVariableTests(unittest.TestCase):
    """The issue's two repositories: `a` with branches main and only-in-a, and `b` with main alone."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.client = GitClient(timeout=60)
        self.named, self.other = root / "a named repository", root / "b another repository"
        for repository in (self.named, self.other):
            self.client.output(["init", "--quiet", "-b", "main", str(repository)])
            self.client.output(["commit", "--quiet", "--allow-empty", "-m", "initial"], directory=repository)
        self.client.output(["branch", "only-in-a"], directory=self.named)

    def plain_git(self, arguments: Sequence[str]) -> bytes:
        """What git prints in the named repository given this process's environment as it is, as before the fix."""
        return subprocess.run(
            ["git", "-C", str(self.named), *arguments], capture_output=True, check=True, env=dict(os.environ)
        ).stdout

    def other_git_dir_forms(self) -> list[str]:
        """`b`'s git directory as each platform's shells export it: Git Bash on Windows also writes C:/ forms."""
        native = str(self.other / ".git")
        return [native, (self.other / ".git").as_posix()] if os.name == "nt" else [native]

    def test_a_listing_names_the_repository_it_was_given_with_git_dir_set_to_another(self) -> None:
        forms = self.other_git_dir_forms()
        self.assertTrue(forms)
        for value in forms:
            with self.subTest(value=value), mock.patch.dict(os.environ, {"GIT_DIR": value}):
                self.assertEqual(OTHER_LISTING, self.plain_git(LISTING))
                self.assertEqual(NAMED_LISTING, self.client.run(LISTING, directory=self.named).output_bytes())
                self.assertEqual(NAMED_LISTING.decode(), self.client.output(LISTING, directory=self.named))
                with self.client.stream(LISTING, directory=self.named) as stream:
                    stream.stdin.close()
                    self.assertEqual(NAMED_LISTING, stream.read(len(NAMED_LISTING) + 1))
                    self.assertEqual(0, stream.wait())

    def test_a_status_reads_the_named_repositorys_index_with_git_index_file_set_to_another(self) -> None:
        (self.named / "f.txt").write_text("staged\n", encoding="utf-8")
        self.client.output(["add", "f.txt"], directory=self.named)
        status = ["status", "--porcelain", "-z"]
        empty_index = self.other / "an empty index"
        with mock.patch.dict(os.environ, {"GIT_INDEX_FILE": str(empty_index)}):
            self.assertEqual(b"?? f.txt\0", self.plain_git(status))
            self.assertEqual("A  f.txt\0", self.client.output(status, directory=self.named))

    def test_every_repository_selecting_variable_set_at_once_reaches_no_call(self) -> None:
        (self.named / "f.txt").write_text("staged\n", encoding="utf-8")
        self.client.output(["add", "f.txt"], directory=self.named)
        self.client.output(["config", "fixture.repository", "named"], directory=self.named)
        other_git = self.other / ".git"
        other_config = self.other / "another config"
        other_config.write_text("[fixture]\n\trepository = other\n", encoding="utf-8")
        values = {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other_git / "objects"),
            "GIT_COMMON_DIR": str(other_git),
            "GIT_CONFIG": str(other_config),
            "GIT_DIR": str(other_git),
            "GIT_GRAFT_FILE": str(self.other / "grafts"),
            "GIT_IMPLICIT_WORK_TREE": "0",
            "GIT_INDEX_FILE": str(self.other / "an empty index"),
            "GIT_NAMESPACE": "elsewhere",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OBJECT_DIRECTORY": str(other_git / "objects"),
            "GIT_PREFIX": "a prefix/",
            "GIT_REPLACE_REF_BASE": "refs/elsewhere/",
            "GIT_SHALLOW_FILE": str(self.other / "shallow"),
            "GIT_WORK_TREE": str(self.other),
        }
        self.assertEqual(bounded_process.REPOSITORY_VARIABLES, set(values))
        with mock.patch.dict(os.environ, values):
            self.assertEqual(NAMED_LISTING.decode(), self.client.output(LISTING, directory=self.named))
            self.assertEqual("A  f.txt\0", self.client.output(["status", "--porcelain", "-z"], directory=self.named))
            config = ["config", "--get", "fixture.repository"]
            self.assertEqual("named\n", self.client.output(config, directory=self.named))
            self.assertEqual(b"other\n", self.plain_git(config))

    def test_command_scope_settings_still_reach_git(self) -> None:
        settings = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "fixture.scope", "GIT_CONFIG_VALUE_0": "command"}
        with mock.patch.dict(os.environ, settings):
            self.assertEqual(
                "command\n", self.client.output(["config", "--get", "fixture.scope"], directory=self.named)
            )

    def test_the_variables_are_the_ones_git_clears_but_its_settings_and_with_its_namespace(self) -> None:
        listed = set(self.client.output(["rev-parse", "--local-env-vars"]).split())
        self.assertLessEqual(
            listed - {"GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"}, bounded_process.REPOSITORY_VARIABLES
        )
        self.assertEqual(
            frozenset(
                {
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                    "GIT_COMMON_DIR",
                    "GIT_CONFIG",
                    "GIT_DIR",
                    "GIT_GRAFT_FILE",
                    "GIT_IMPLICIT_WORK_TREE",
                    "GIT_INDEX_FILE",
                    "GIT_NAMESPACE",
                    "GIT_NO_REPLACE_OBJECTS",
                    "GIT_OBJECT_DIRECTORY",
                    "GIT_PREFIX",
                    "GIT_REPLACE_REF_BASE",
                    "GIT_SHALLOW_FILE",
                    "GIT_WORK_TREE",
                }
            ),
            bounded_process.REPOSITORY_VARIABLES,
        )


def command_id(value: object) -> str | None:
    """A test ID that reads an argument list as the git command it runs, leaving every other value's ID to pytest."""
    return " ".join(["git", *value]) if isinstance(value, list) else None


@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param(EARLY_EOF, id="early-eof"),
        pytest.param(UNRESOLVED, id="unresolved-host"),
        pytest.param(HUNG_UP, id="hung-up"),
        pytest.param("error: RPC failed; HTTP 502 curl 22 The requested URL returned error: 502\n", id="http-502"),
        pytest.param(
            "fatal: unable to access 'https://github.com/o/r.git/': Recv failure: Connection reset by peer\n",
            id="connection-reset",
        ),
        pytest.param(
            "fatal: unable to access 'https://github.com/o/r.git/': Failed to connect to github.com port 443 after "
            "21045 ms: Connection timed out\n",
            id="connection-timed-out",
        ),
        pytest.param(
            "fatal: unable to access 'https://github.com/o/r.git/': schannel: failed to receive handshake, SSL/TLS "
            "connection failed\n",
            id="schannel-handshake",
        ),
        pytest.param(
            "fatal: unable to access 'https://github.com/o/r.git/': OpenSSL SSL_connect: SSL_ERROR_SYSCALL in "
            "connection to github.com:443\n",
            id="openssl-syscall",
        ),
        pytest.param(
            "fatal: unable to access 'https://github.com/o/r.git/': gnutls_handshake() failed: The TLS connection "
            "was non-properly terminated.\n",
            id="gnutls-handshake",
        ),
        pytest.param(
            "ssh: Could not resolve hostname github.com: Name or service not known\n"
            "fatal: Could not read from remote repository.\n",
            id="ssh-unresolved-hostname",
        ),
        pytest.param(
            "fatal: unable to access 'https://github.com/o/r.git/': The requested URL returned error: 503\n",
            id="http-503",
        ),
    ],
)
@pytest.mark.parametrize(
    "arguments", [["fetch", "origin"], ["ls-remote", "--symref", "origin", "HEAD"]], ids=command_id
)
def test_every_transport_message_is_transient_for_fetch_and_ls_remote(arguments: list[str], stderr: str) -> None:
    client, sleeper, calls = retrying(GitResult(128, "", stderr), GitResult(0, "answer\n", ""))
    assert GitResult(0, "answer\n", "") == client.run(arguments)
    assert (sleeper.waits, len(calls)) == ([2.0], 2)


@pytest.mark.parametrize("stderr", [pytest.param(stderr, id=name) for name, stderr in PERMANENT.items()])
def test_a_permanent_failure_is_not_retried(stderr: str) -> None:
    client, sleeper, calls = retrying(GitResult(128, "", stderr))
    assert GitResult(128, "", stderr) == client.run(["fetch", "origin", "x"])
    assert (sleeper.waits, len(calls)) == ([], 1)


@pytest.mark.parametrize(
    "arguments",
    [
        ["push", "origin", "main"],
        ["--no-optional-locks", "push", "origin"],
        ["pull", "--ff-only"],
        ["clone", "https://github.com/o/r.git", "r"],
        ["remote", "update"],
        ["status"],
        ["-c", "fetch", "push"],
        [],
    ],
    ids=command_id,
)
def test_push_and_every_other_subcommand_are_never_retried(arguments: list[str]) -> None:
    client, sleeper, calls = retrying(GitResult(128, "", EARLY_EOF))
    assert GitResult(128, "", EARLY_EOF) == client.run(arguments)
    assert (sleeper.waits, len(calls)) == ([], 1)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--no-optional-locks", "fetch", "--all", "--prune", "--quiet"],
        ["-c", "http.version=HTTP/1.1", "fetch", "origin"],
        ["--git-dir", "push", "ls-remote", "origin"],
        ["--config-env", "core.x=HOME", "fetch", "origin"],
        ["--attr-source", "HEAD", "fetch", "origin"],
        ["--super-prefix", "sub/", "fetch", "origin"],
        ["--work-tree", "push", "--namespace", "push", "-C", "push", "fetch"],
    ],
    ids=command_id,
)
def test_the_subcommand_is_found_after_git_options(arguments: list[str]) -> None:
    client, sleeper, _calls = retrying(GitResult(128, "", UNRESOLVED), GitResult(0, "", ""))
    assert client.run(arguments).returncode == 0
    assert sleeper.waits == [2.0]


def test_a_git_error_names_one_of_the_classes_its_documentation_gives() -> None:
    assert get_args(git_client.GitErrorKind) == ("git", "prerequisite", "execution", "timeout", "not_repository")

"""Regression tests for the git client every skill that runs git shares."""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import os
import re
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
from git_client import GitClient, GitError, GitErrorKind, GitResult, classify_failure
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

    def test_an_empty_stderr_names_the_subcommand_behind_git_s_own_options(self) -> None:
        # Each failing call names its subcommand, never git's first option, and a call with none names git alone.
        for arguments, message in (
            (["--no-optional-locks", "merge-base", "a", "b"], "git merge-base failed with exit code 2"),
            (["-C", "d", "--shallow-file", "s", "log"], "git log failed with exit code 2"),
            (["--no-optional-locks"], "git failed with exit code 2"),
        ):
            for call in ("output", "quiet_output"):
                with self.subTest(arguments=arguments, call=call), self.assertRaises(GitError) as context:
                    getattr(GitClient(scripted(GitResult(2, "", ""))[0]), call)(arguments)
                self.assertEqual(message, str(context.exception))

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
                (([git(), "hash-object", "--stdin"], 300.0), {"input_bytes": b"blob", "env": None}),
                call_shape(run.call_args),
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

    def test_output_lines_splits_on_a_newline_alone(self) -> None:
        self.assertEqual([], git_client.output_lines(""))
        self.assertEqual([""], git_client.output_lines("\n"))
        self.assertEqual(["a", "b"], git_client.output_lines("a\nb\n"))
        self.assertEqual(["a", "b"], git_client.output_lines("a\nb"))
        self.assertEqual(["a", "", "b"], git_client.output_lines("a\n\nb\n"))
        # Each other character str.splitlines() breaks on stays inside its line.
        breaks = [chr(code) for code in (0x0B, 0x0C, 0x0D, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029)]
        self.assertEqual(
            [f"a{character}b" for character in breaks],
            git_client.output_lines("".join(f"a{character}b\n" for character in breaks)),
        )
        # The same text splitlines() breaks at each of them.
        self.assertEqual(len(breaks) + 1, len("".join(f"a{character}b" for character in breaks).splitlines()))

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

    def test_a_quiet_command_given_input_reads_it_through_the_input_runner(self) -> None:
        given: list[bytes] = []

        def input_runner(command: Sequence[str], timeout: float, *, input_bytes: bytes) -> GitResult:
            given.append(input_bytes)
            return GitResult(1, "", "")

        client = GitClient(scripted()[0], input_runner=input_runner)
        self.assertIsNone(client.quiet_output(["grep", "-f", "-"], input_bytes=b"pattern\n"))
        self.assertEqual([b"pattern\n"], given)

    def test_a_quiet_failure_with_nothing_on_stderr_names_the_subcommand_and_its_exit_code(self) -> None:
        client = GitClient(scripted(GitResult(2, "", ""))[0])
        with self.assertRaises(GitError) as context:
            client.quiet_output(["rev-parse", "--verify", "--quiet", "side"])
        self.assertEqual("git rev-parse failed with exit code 2", str(context.exception))
        self.assertEqual(("git", 2), (context.exception.kind, context.exception.returncode))

    def test_an_error_is_git_s_answer_unless_git_is_missing_could_not_start_or_ran_out_of_time(self) -> None:
        kinds: tuple[tuple[GitErrorKind, bool], ...] = (
            ("git", True),
            ("not_repository", True),
            ("prerequisite", False),
            ("execution", False),
            ("timeout", False),
        )
        for kind, answered in kinds:
            with self.subTest(kind=kind):
                self.assertEqual(answered, GitError("x", kind=kind).answered)
        self.assertTrue(GitError("git rev-parse answered 3 lines, not two").answered)

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


# A path holding a non-ASCII character and one holding a newline, which git quotes in a listing without -z.
AWKWARD_NAMES = ("line\nbreak.py", "modülé.py")


def plumbing(root: Path, *arguments: str, data: bytes = b"") -> str:
    """What a git plumbing command run in `root` prints, given `data` as its stdin, stripped."""
    identity = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.com"]
    done = subprocess.run(["git", "-C", str(root), *identity, *arguments], input=data, capture_output=True, check=True)
    return done.stdout.decode("utf-8").strip()


def awkward_history(root: Path) -> tuple[str, str]:
    """A repository whose second commit adds AWKWARD_NAMES beside the first's README.md, and the two commits.

    Built from trees, since Git for Windows refuses a newline in a path in the index."""
    plumbing(root, "init", "--quiet")
    blob = plumbing(root, "hash-object", "-w", "--stdin", data=b"text\n")
    commits: list[str] = []
    for names in (("README.md",), ("README.md", *AWKWARD_NAMES)):
        entries = "".join(f"100644 blob {blob}\t{name}\0" for name in names)
        tree = plumbing(root, "mktree", "-z", data=entries.encode("utf-8"))
        parents = [option for commit in commits for option in ("-p", commit)]
        commits.append(plumbing(root, "commit-tree", tree, *parents, "-m", "commit"))
    return commits[0], commits[1]


class NamesTests(unittest.TestCase):
    def test_names_runs_the_listing_with_z_after_its_subcommand_and_splits_on_nul(self) -> None:
        runner, calls = scripted(
            GitResult(0, "M\0a b\0?? c\0", ""), GitResult(0, "", ""), GitResult(0, "d\0", ""), GitResult(0, "", "")
        )
        client = GitClient(runner)
        self.assertEqual(["M", "a b", "?? c"], client.names(["-c", "x=y", "status", "--porcelain"], directory="r"))
        self.assertEqual([], client.names(["ls-files"], timeout=7))
        self.assertEqual(["d"], client.names(["ls-files", "-z", "--others"]))
        # Arguments that name no subcommand take -z last.
        self.assertEqual([], client.names(["--literal-pathspecs"]))
        self.assertEqual(
            [
                (["git", "-C", "r", "-c", "x=y", "status", "-z", "--porcelain"], 300.0),
                (["git", "ls-files", "-z"], 7),
                (["git", "ls-files", "-z", "--others"], 300.0),
                (["git", "--literal-pathspecs", "-z"], 300.0),
            ],
            calls,
        )

    def test_names_keeps_status_porcelain_s_status_and_path_in_one_field(self) -> None:
        # status --porcelain -z prints "XY path", then a rename's original path as a field of its own.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base, _head = awkward_history(root)
            plumbing(root, "reset", "--quiet", "--hard", base)
            plumbing(root, "mv", "README.md", "renamed.md")
            self.assertEqual(
                ["R  renamed.md", "README.md"], GitClient().names(["status", "--porcelain"], directory=root)
            )

    def test_names_raises_a_failed_listing_as_output_does(self) -> None:
        runner, _calls = scripted(GitResult(128, "", NOT_A_REPOSITORY))
        with self.assertRaises(GitError) as context:
            GitClient(runner).names(["ls-files"])
        self.assertEqual(("not_repository", 128), (context.exception.kind, context.exception.returncode))

    def test_names_reads_a_name_holding_a_non_ascii_character_or_a_newline_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base, head = awkward_history(root)
            client = GitClient()
            self.assertEqual(list(AWKWARD_NAMES), client.names(["diff", "--name-only", base, head], directory=root))
            self.assertEqual(
                ["A", AWKWARD_NAMES[0], "A", AWKWARD_NAMES[1]],
                client.names(["diff", "--name-status", base, head], directory=root),
            )
            self.assertEqual(
                ["README.md", *AWKWARD_NAMES], client.names(["ls-tree", "--name-only", head], directory=root)
            )


class SubprocessRunnerTests(unittest.TestCase):
    def test_the_runner_goes_through_the_bounded_layer(self) -> None:
        finished = bounded_process.Finished(2, b"out \xff", b"bad \xff")
        with mock.patch.object(git_client, "run_bounded", return_value=finished) as run:
            result = git_client.subprocess_runner(["git", "status"], 12)
        self.assertEqual((([git(), "status"], 12), {"env": None}), call_shape(run.call_args))
        self.assertEqual(GitResult(2, "out \udcff", "bad �"), result)

    def test_given_input_goes_through_the_bounded_layer(self) -> None:
        finished = bounded_process.Finished(0, b"id\n", b"")
        with mock.patch.object(git_client, "run_bounded", return_value=finished) as run:
            result = git_client.subprocess_runner(["git", "hash-object", "--stdin"], 12, input_bytes=b"blob")
        self.assertEqual(
            (([git(), "hash-object", "--stdin"], 12), {"input_bytes": b"blob", "env": None}), call_shape(run.call_args)
        )
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

    def test_a_path_holding_a_character_splitlines_breaks_on_is_one_path(self) -> None:
        for character in (chr(0x2028), chr(0x85)):
            checkout = self.root / f"a{character}checkout"
            self.client.output(["init", "--quiet", str(checkout)])
            with self.subTest(character=hex(ord(character))):
                paths = self.client.rev_parse_paths(["--show-toplevel", "--git-common-dir"], directory=checkout)
                self.assertEqual(
                    [checkout.resolve(), (checkout / ".git").resolve()], [path.resolve() for path in paths or []]
                )

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
            command: Sequence[str], *, idle_timeout: float, env: object, tree: object
        ) -> Iterator[bounded_process.Streaming]:
            bounded_call(command, idle_timeout, tree=tree)
            self.assertIsNone(env)
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
            mock.patch.object(executable_locator, "find_program", wraps=executable_locator.find_program) as lookup,
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

    def test_no_client_call_takes_an_index_file(self) -> None:
        # run_bounded's index_file is the one way to set GIT_INDEX_FILE; a client takes no keyword that reaches it, so
        # a caller that needs another index builds the client on its own runner, as deploy.py --at does.
        for name in ("__init__", "run", "output", "names", "quiet_output", "stream"):
            with self.subTest(call=name):
                self.assertNotIn("index_file", inspect.signature(getattr(GitClient, name)).parameters)
        runner, calls = scripted(GitResult(0, "", ""))
        with self.assertRaises(TypeError):
            GitClient(runner).run(["status"], index_file=self.other / "index")  # type: ignore[call-arg]  # the keyword it refuses
        self.assertEqual([], calls)

    def test_each_repository_selecting_variable_given_in_an_environment_reaches_no_git(self) -> None:
        # The real runner, given an environment holding one variable that points at the other repository, reads the
        # named one: an environment may come from another process, such as a hook's, so none of them, GIT_INDEX_FILE
        # included, is ever taken from it.
        (self.named / "f.txt").write_text("staged\n", encoding="utf-8")
        self.client.output(["add", "f.txt"], directory=self.named)
        other_git = self.other / ".git"
        values = {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other_git / "objects"),
            "GIT_COMMON_DIR": str(other_git),
            "GIT_CONFIG": str(self.other / "another config"),
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
        base = bounded_process.without_repository_variables(os.environ)
        status = ["git", "-C", str(self.named), "status", "--porcelain", "-z"]
        listing = ["git", "-C", str(self.named), *LISTING]
        for name in sorted(values):
            with self.subTest(variable=name):
                given = {**base, name: values[name]}
                self.assertEqual("A  f.txt\0", git_client.subprocess_runner(status, 60, env=given).stdout)
                self.assertEqual(NAMED_LISTING.decode(), git_client.subprocess_runner(listing, 60, env=given).stdout)

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


# Every name `git --list-cmds=builtins,main` printed on git 2.56.0.windows.2.
GIT_2_56_COMMANDS = frozenset(
    [
        "add",
        "am",
        "annotate",
        "apply",
        "archive",
        "backfill",
        "bisect",
        "blame",
        "branch",
        "bugreport",
        "bundle",
        "cat-file",
        "check-attr",
        "check-ignore",
        "check-mailmap",
        "check-ref-format",
        "checkout",
        "checkout--worker",
        "checkout-index",
        "cherry",
        "cherry-pick",
        "citool",
        "clean",
        "clone",
        "column",
        "commit",
        "commit-graph",
        "commit-tree",
        "config",
        "count-objects",
        "credential",
        "credential-cache",
        "credential-cache--daemon",
        "credential-store",
        "credential-wincred",
        "daemon",
        "describe",
        "diagnose",
        "diff",
        "diff-files",
        "diff-index",
        "diff-pairs",
        "diff-tree",
        "difftool",
        "difftool--helper",
        "fast-export",
        "fast-import",
        "fetch",
        "fetch-pack",
        "filter-branch",
        "fmt-merge-msg",
        "for-each-ref",
        "for-each-repo",
        "format-patch",
        "format-rev",
        "fsck",
        "fsck-objects",
        "fsmonitor--daemon",
        "gc",
        "get-tar-commit-id",
        "grep",
        "gui",
        "gui--askpass",
        "gui--askyesno",
        "gui.tcl",
        "hash-object",
        "help",
        "history",
        "hook",
        "http-backend",
        "http-fetch",
        "http-push",
        "imap-send",
        "index-pack",
        "init",
        "init-db",
        "interpret-trailers",
        "last-modified",
        "log",
        "ls-files",
        "ls-remote",
        "ls-tree",
        "mailinfo",
        "mailsplit",
        "maintenance",
        "merge",
        "merge-base",
        "merge-file",
        "merge-index",
        "merge-octopus",
        "merge-one-file",
        "merge-ours",
        "merge-recursive",
        "merge-recursive-ours",
        "merge-recursive-theirs",
        "merge-resolve",
        "merge-subtree",
        "merge-tree",
        "mergetool",
        "mktag",
        "mktree",
        "multi-pack-index",
        "mv",
        "name-rev",
        "notes",
        "pack-objects",
        "pack-redundant",
        "pack-refs",
        "patch-id",
        "pickaxe",
        "prune",
        "prune-packed",
        "pull",
        "push",
        "quiltimport",
        "range-diff",
        "read-tree",
        "rebase",
        "receive-pack",
        "reflog",
        "refs",
        "remote",
        "remote-ext",
        "remote-fd",
        "remote-ftp",
        "remote-ftps",
        "remote-http",
        "remote-https",
        "repack",
        "replace",
        "replay",
        "repo",
        "request-pull",
        "rerere",
        "reset",
        "restore",
        "rev-list",
        "rev-parse",
        "revert",
        "rm",
        "send-email",
        "send-pack",
        "sh-i18n--envsubst",
        "shortlog",
        "show",
        "show-branch",
        "show-index",
        "show-ref",
        "sparse-checkout",
        "stage",
        "stash",
        "status",
        "stripspace",
        "submodule",
        "submodule--helper",
        "subtree",
        "survey",
        "switch",
        "symbolic-ref",
        "tag",
        "unpack-file",
        "unpack-objects",
        "update",
        "update-index",
        "update-ref",
        "update-server-info",
        "upload-archive",
        "upload-archive--writer",
        "upload-pack",
        "url-parse",
        "var",
        "verify-commit",
        "verify-pack",
        "verify-tag",
        "version",
        "web--browse",
        "whatchanged",
        "worktree",
        "write-tree",
    ]
)
# The commands of that list git added after 2.31, from each release's notes, which an older git may lack, so an alias
# or a `git-<name>` program there could take the name.
COMMANDS_AFTER_2_31 = frozenset(
    {"backfill", "diff-pairs", "history", "last-modified", "refs", "replay", "repo", "survey"}
)
# Every key of git 2.56's `git help --config` whose value is a command, a program, or names one, with a name for each
# placeholder: a `<driver>`, `<tool>`, or `<name>` as `x`, a `<url>` as `https://example.invalid`.
PROGRAM_SETTINGS_2_56 = (
    "browser.x.cmd",
    "browser.x.path",
    "core.alternateRefsCommand",
    "core.askPass",
    "core.editor",
    "core.fsmonitor",
    "core.gitProxy",
    "core.hooksPath",
    "core.pager",
    "core.sshCommand",
    "credential.helper",
    "credential.https://example.invalid.helper",
    "diff.x.command",
    "diff.x.textconv",
    "diff.external",
    "diff.guitool",
    "diff.tool",
    "difftool.x.cmd",
    "difftool.x.path",
    "filter.x.clean",
    "filter.x.smudge",
    "filter.x.process",
    "gc.recentObjectsHook",
    "gpg.program",
    "gpg.x.program",
    "gpg.ssh.defaultKeyCommand",
    "guitool.x.cmd",
    "help.browser",
    "hook.x.command",
    "hook.x.event",
    "imap.tunnel",
    "init.templateDir",
    "instaweb.browser",
    "instaweb.httpd",
    "interactive.diffFilter",
    "man.viewer",
    "man.x.cmd",
    "man.x.path",
    "merge.x.driver",
    "merge.guitool",
    "merge.tool",
    "mergetool.x.cmd",
    "mergetool.x.path",
    "pager.log",
    "protocol.allow",
    "protocol.ext.allow",
    "pull.octopus",
    "pull.twohead",
    "remote.x.receivepack",
    "remote.x.uploadpack",
    "remote.x.vcs",
    "sendemail.ccCmd",
    "sendemail.headerCmd",
    "sendemail.toCmd",
    "sendemail.smtpServer",
    "sequence.editor",
    "submodule.x.update",
    "tar.tgz.command",
    "trailer.x.cmd",
    "trailer.x.command",
    "uploadpack.packObjectsHook",
    "web.browser",
)


class IsolatedClientTests(unittest.TestCase):
    """An isolated client inside a repository whose configuration, the global one, and a command-scope setting each
    set fixture.source, as content a review did not write can sit inside or below a repository."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.repository = root / "a repository"
        self.inner = self.repository / "content it did not write"
        self.inner.mkdir(parents=True)
        GitClient(timeout=60).output(["init", "--quiet", str(self.repository)])
        GitClient(timeout=60).output(["config", "fixture.source", "repository"], directory=self.repository)
        global_file = root / "global config"
        global_file.write_text("[fixture]\n\tglobal = yes\n", encoding="utf-8")
        scope = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "fixture.scope", "GIT_CONFIG_VALUE_0": "command"}
        patcher = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(global_file), **scope})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_isolated_command_names_the_null_device_and_starts_with_the_isolated_environment(self) -> None:
        null = "nul" if os.name == "nt" else "/dev/null"
        finished = bounded_process.Finished(0, b"", b"")
        with mock.patch.object(git_client, "run_bounded", return_value=finished) as run:
            GitClient(isolated=True, timeout=7).run(["grep", "-e", "x"], directory="d")
            GitClient(isolated=True, timeout=7).run(["hash-object", "--stdin"], input_bytes=b"blob")
        self.assertEqual([7, 7], [call_shape(call)[0][1] for call in run.call_args_list], "the client's time limit")
        (command, _), options = call_shape(run.call_args_list[0])
        self.assertEqual([git(), f"--git-dir={null}", "-C", "d", "grep", "-e", "x"], command)
        environment = options["env"]
        self.assertIsInstance(environment, dict)
        assert isinstance(environment, dict)
        self.assertEqual(null, environment["GIT_CONFIG_GLOBAL"])
        self.assertNotIn("GIT_CONFIG_COUNT", environment)
        (command, _), options = call_shape(run.call_args_list[1])
        self.assertEqual([git(), f"--git-dir={null}", "hash-object", "--stdin"], command)
        self.assertEqual((b"blob", null), (options["input_bytes"], options["env"]["GIT_CONFIG_GLOBAL"]))  # type: ignore[index]  # env is the dict asserted above

    def test_an_isolated_client_refuses_a_runner_that_could_not_start_git_isolated(self) -> None:
        runner, _ = scripted(GitResult(0, "", ""))
        for options in ({"runner": runner}, {"input_runner": runner}):
            with (
                self.subTest(options=sorted(options)),
                self.assertRaisesRegex(
                    TypeError,
                    "^an isolated GitClient starts git itself, with the isolated environment, so it takes no runner$",
                ),
            ):
                GitClient(isolated=True, **options)  # type: ignore[arg-type]  # each option is a runner
        GitClient(git_client.subprocess_runner, isolated=True, input_runner=git_client.subprocess_runner)

    def test_a_plain_client_refuses_every_spelling_of_no_index_in_every_position(self) -> None:
        # Of git 2.56's commands, `git <command> -h` names --no-index for these and check-ignore alone.
        self.assertEqual(frozenset({"grep", "diff", "difftool"}), git_client.ISOLATED_SUBCOMMANDS)
        # Written out here rather than read from NO_INDEX_SPELLINGS: each abbreviation from --no-i to the whole option.
        spellings = ["--no-i", "--no-in", "--no-ind", "--no-inde", "--no-index"]
        self.assertEqual(frozenset(spellings), git_client.NO_INDEX_SPELLINGS)
        shapes: dict[str, Callable[[str], list[str]]] = {
            "first": lambda flag: ["grep", flag, "-n", "-e", "needle", "--"],
            "among options": lambda flag: ["grep", "-n", flag, "-e", "needle", "--"],
            "after a pattern": lambda flag: ["grep", "-n", "-e", "needle", flag, "--", "a.txt"],
            "after a revision": lambda flag: ["grep", "-n", "-e", "needle", "HEAD", flag],
            "after --": lambda flag: ["grep", "-n", "-e", "needle", "--", flag],
            "behind git's options": lambda flag: ["-c", "a.b=c", "grep", flag, "-e", "needle"],
            "diff": lambda flag: ["diff", "--stat", flag, "a", "b"],
            "difftool": lambda flag: ["difftool", flag, "-y", "-x", "cat", "a", "b"],
        }
        refusal = (
            r"^git grep, diff, or difftool --no-index reads files no repository holds; "
            r"run it on GitClient\(isolated=True\)$"
        )
        for spelling in spellings:
            for shape, build in shapes.items():
                arguments = build(spelling)
                with self.subTest(spelling=spelling, shape=shape):
                    runner, calls = scripted(GitResult(0, "", ""))
                    with self.assertRaisesRegex(ValueError, refusal):
                        GitClient(runner).run(arguments)
                    with self.assertRaisesRegex(ValueError, refusal):
                        GitClient(runner).stream(arguments).__enter__()
                    self.assertEqual([], calls, "refused before git starts")
        allowed = (
            ["grep", "-n", "--no-ignore-case", "-e", "x"],
            ["diff", "--no-indent-heuristic", "a", "b"],
            ["check-ignore", "-q", "--no-index", "a"],
            ["--no-index", "status"],
            ["grep", "-n", "-e", "x", "--", "a.txt"],
        )
        for arguments in allowed:
            with self.subTest(arguments=arguments):
                runner, calls = scripted(GitResult(0, "", ""))
                GitClient(runner).run(arguments)
                self.assertEqual(["git", *arguments], calls[0][0])

    def test_a_plain_client_refuses_a_command_line_setting_that_can_define_an_alias(self) -> None:
        # Written out here rather than read from ALIAS_SECTIONS: an alias, and the two ways to include a file of them.
        self.assertEqual(frozenset({"alias", "include", "includeif"}), git_client.ALIAS_SECTIONS)
        self.assertEqual(frozenset({"help.autocorrect"}), git_client.SUBCOMMAND_KEYS)
        refused = (
            ["-c", "alias.s=grep", "s", "--no-index", "-e", "x"],
            ["-c", "ALIAS.s=grep", "s", "-e", "x"],
            ["-c", "Alias.s", "status"],
            ["--config-env", "alias.s=GREP", "s", "--no-index"],
            ["--config-env=alias.s=GREP", "s"],
            ["-C", "d", "-c", "core.x=y", "-c", "alias.s=grep", "s"],
            ["-c", "include.path=/x/aliases", "s"],
            ["-c", "includeIf.onbranch:main.path=/x/aliases", "s"],
            ["--config-env", "IncludeIf.gitdir:/x/.path=FILE", "status"],
            ["--exec-path=/x/programs", "s", "--no-index"],
            ["-C", "d", "--exec-path=C:/x", "status"],
            # help.autocorrect runs the command git guesses a misspelled name meant, here grep.
            ["-c", "help.autocorrect=immediate", "gerp", "--no-index", "-l", "-e", "x"],
            ["-c", "HELP.AutoCorrect=1", "s"],
            ["--config-env", "help.autocorrect=WHEN", "s"],
            ["--config-env=Help.autocorrect=WHEN", "s"],
        )
        refusal = r"^an alias, autocorrection, or a folder of git programs set on git's command line can run grep, "
        for arguments in refused:
            with self.subTest(arguments=arguments):
                runner, calls = scripted(GitResult(0, "", ""))
                with self.assertRaisesRegex(ValueError, refusal):
                    GitClient(runner).run(arguments)
                with self.assertRaisesRegex(ValueError, refusal):
                    GitClient(runner).stream(arguments).__enter__()
                self.assertEqual([], calls, "refused before git starts")
        allowed = (
            ["-c", "core.alias=x", "status"],
            ["-c", "aliases.s=grep", "status"],
            ["--config-env=core.quotepath=VALUE", "status"],
            ["grep", "-c", "-e", "alias.s=grep"],
            ["log", "-c", "alias.s=x"],
            ["--git-dir", "alias.s", "status"],
            ["--exec-path"],
            ["log", "--exec-path=x"],
            ["-c", "help.format=html", "status"],
            ["-c", "helper.autocorrect=1", "status"],
            ["-c", "help.autocorrectx=1", "status"],
        )
        for arguments in allowed:
            with self.subTest(arguments=arguments):
                runner, calls = scripted(GitResult(0, "", ""))
                GitClient(runner).run(arguments)
                self.assertEqual(["git", *arguments], calls[0][0])

    def test_a_plain_client_refuses_every_carrier_of_another_command_in_every_spelling(self) -> None:
        # Each carries a command or names a program git starts, whose own git calls the client never sees.
        refused = {
            "git submodule foreach": ["submodule", "foreach", "git grep --no-index -l needle"],
            "git submodule foreach ": ["submodule", "--quiet", "foreach", "--recursive", "true"],
            "git bisect run": ["bisect", "run", "git", "grep", "--no-index", "-l", "needle"],
            "git bisect replay": ["bisect", "replay", "steps.log"],
            "git bisect visualize": ["bisect", "visualize"],
            "git rebase --exec": ["rebase", "--exec", "git grep --no-index -l needle", "HEAD~1"],
            "git rebase --exec=": ["rebase", "--exec=true", "HEAD~1"],
            "git rebase --ex": ["rebase", "--ex", "true", "HEAD~1"],
            "git rebase --e": ["rebase", "--e=true", "HEAD~1"],
            "git rebase -x": ["rebase", "-x", "true", "HEAD~1"],
            "git rebase -xtrue": ["rebase", "-xtrue", "HEAD~1"],
            "git rebase -ix": ["rebase", "-ix", "true", "HEAD~1"],
            "git rebase -qvx": ["rebase", "-qvx", "true"],
            "git grep --open-files-in-pager": ["grep", "--open-files-in-pager=sh", "-e", "x"],
            "git grep --open": ["grep", "--open", "-e", "x"],
            "git grep -O": ["grep", "-O", "-e", "x"],
            "git grep -nOsh": ["grep", "-nOsh", "-e", "x"],
            "git fetch --upload-pack": ["fetch", "--upload-pack", "sh -c x", "origin"],
            "git fetch --upload": ["fetch", "--upload=x", "origin"],
            "git pull --upload-pack=": ["pull", "--upload-pack=x"],
            "git ls-remote --upload-pack=": ["ls-remote", "--upload-pack=x", "origin"],
            "git ls-remote --exec=": ["ls-remote", "--exec=x", "origin"],
            "git fetch-pack --exec=": ["fetch-pack", "--exec=x", "a"],
            "git push --receive-pack=": ["push", "--receive-pack=x", "origin"],
            "git push --exec=": ["push", "--exec=x", "origin"],
            "git send-pack --receive=": ["send-pack", "--receive=x", "a"],
            "git archive --exec": ["archive", "--remote", "r", "--exec", "x", "HEAD"],
            "git clone -u": ["clone", "-u", "x", "url", "dir"],
            "git clone -c": ["clone", "-c", "core.hooksPath=/hooks", "url"],
            "git clone -qc": ["clone", "-qccore.fsmonitor=x", "url"],
            "git clone --config": ["clone", "--config", "filter.a.smudge=x", "url"],
            "git clone --template=": ["clone", "--template=/x", "url"],
            "git init --template": ["init", "--template", "/x", "dir"],
            "git difftool": ["difftool", "-x", "cat", "HEAD"],
            "git mergetool": ["mergetool"],
            "git filter-branch": ["filter-branch", "--tree-filter", "x"],
            "git for-each-repo": ["for-each-repo", "--config=x", "--", "grep", "--no-index"],
            "git merge-index": ["merge-index", "x", "-a"],
            "git hook": ["hook", "run", "pre-commit"],
            "git credential": ["credential", "fill"],
            "git help": ["help", "--web", "log"],
            "git maintenance": ["maintenance", "start"],
            "git shell": ["shell", "-c", "x"],
            "git upload-pack": ["upload-pack", "."],
            "git send-email": ["send-email", "x.patch"],
            "git lfs": ["lfs", "ls-files"],
            "git s": ["s"],
            "git log --help": ["log", "--help"],
            "git --help": ["--help", "log"],
            "git -h": ["-C", "d", "-h", "log"],
            "git --help ": ["--no-pager", "--help"],
            "git log --hel": ["log", "-p", "--hel"],
            "git commit --help": ["commit", "-a", "--help"],
            "git replay": ["replay", "--onto", "main", "topic"],
            "the setting hook.x.command": [
                "-c",
                "hook.x.command=git grep --no-index -l needle",
                "-c",
                "hook.x.event=pre-commit",
                "commit",
                "-m",
                "m",
            ],
            "git Status": ["Status"],
            "the setting core.editor": ["-c", "core.editor=x", "commit"],
            "the setting core.pager": ["-c", "Core.Pager=x", "log"],
            "the setting core.sshcommand": ["-c", "core.sshCommand=x", "fetch"],
            "the setting core.fsmonitor": ["--config-env=core.fsmonitor=V", "status"],
            "the setting core.hookspath": ["-c", "core.hooksPath=/hooks", "commit", "-m", "x"],
            "the setting core.hookspath ": ["-c", "core.hooksPath", "commit"],
            "the setting core.hookspath  ": ["--config-env", f"core.hooksPath={os.devnull}", "commit"],
            "the setting core.hookspath   ": ["-c", f"core.hooksPath={os.devnull}x", "commit"],
            "the setting sequence.editor": ["-c", "sequence.editor=x", "rebase", "-i", "HEAD~1"],
            "the setting diff.external": ["-c", "diff.external=x", "diff"],
            "the setting diff.a.textconv": ["-c", "diff.a.textconv=x", "log", "-p"],
            "the setting diff.a.b.command": ["-c", "diff.a.b.command=x", "diff"],
            "the setting merge.a.driver": ["--config-env", "merge.a.driver=V", "merge", "x"],
            "the setting remote.origin.uploadpack": ["-c", "remote.origin.uploadpack=x", "fetch"],
            "the setting remote.origin.vcs": ["-c", "remote.origin.vcs=x", "fetch"],
            "the setting submodule.a.update": ["-c", "submodule.a.update=!x", "submodule", "update"],
            "the setting protocol.ext.allow": ["-c", "protocol.ext.allow=always", "fetch", "ext::x"],
            "the setting protocol.allow": ["-c", "protocol.allow=always", "fetch"],
            "the setting credential.helper": ["-c", "credential.helper=x", "fetch"],
            "the setting filter.a.smudge": ["-c", "filter.a.smudge=x", "checkout"],
            "the setting gpg.program": ["-c", "gpg.program=x", "verify-commit", "HEAD"],
            "the setting pager.log": ["-c", "pager.log=x", "log"],
            "the setting init.templatedir": ["-c", "init.templateDir=/x", "init"],
            "the setting uploadpack.packobjectshook": ["-c", "uploadpack.packObjectsHook=x", "fetch"],
        }
        for what, arguments in refused.items():
            with self.subTest(arguments=arguments):
                runner, calls = scripted(GitResult(0, "", ""))
                refusal = (
                    rf"^{re.escape(what.strip())}\S* starts a command or a program whose git calls a plain GitClient "
                    r"cannot read, so it refuses it$"
                )
                with self.assertRaisesRegex(ValueError, refusal):
                    GitClient(runner).run(arguments)
                with self.assertRaisesRegex(ValueError, refusal):
                    GitClient(runner).stream(arguments).__enter__()
                self.assertEqual([], calls, "refused before git starts")
        allowed: tuple[list[str], ...] = (
            ["rebase", "HEAD~1"],
            ["rebase", "--onto", "main", "HEAD~1"],
            ["rebase", "--empty=drop", "--no-exec", "-q", "HEAD~1"],
            ["rebase", "-Xours", "-sort", "HEAD~1"],
            ["rebase", "-r", "HEAD~1"],
            ["submodule"],
            ["submodule", "status", "--recursive"],
            ["submodule", "--quiet", "update", "--init"],
            ["bisect", "start", "HEAD", "HEAD~3"],
            ["bisect", "reset"],
            ["bisect"],
            ["grep", "--or", "--only-matching", "-n", "-e", "x"],
            ["grep", "-eO", "-n"],
            ["fetch", "-u", "--update-shallow", "origin"],
            ["push", "--repo=origin", "--no-exec"],
            ["clone", "-b", "main", "-ojc", "url"],
            ["init", "--quiet", "dir"],
            ["archive", "--format=tar", "HEAD"],
            ["log", "--exec=x"],
            ["-c", f"core.hooksPath={os.devnull}", "commit", "-m", "x"],
            ["-c", f"Core.HooksPath={os.devnull}", "init", "--quiet", "dir"],
            ["-c", "protocol.file.allow=always", "submodule", "add", "url"],
            ["-c", "core.quotePath=off", "-c", "diff.a=x", "-c", "remote.textconv=x", "status"],
            ["--version"],
            ["log", "-p", "--help-all"],
            ["ls-remote", "--heads", "origin"],
            ["grep", "-h", "-e", "x"],
            ["--git-dir", "--help", "status"],
            [],
        )
        for arguments in allowed:
            with self.subTest(arguments=arguments):
                runner, calls = scripted(GitResult(0, "", ""))
                GitClient(runner).run(arguments)
                self.assertEqual(["git", *arguments], calls[0][0])

    def test_a_plain_client_refuses_a_strategy_or_a_transport_that_runs_a_program_from_path(self) -> None:
        # A strategy outside git's own runs git-merge-<name>, and a foreign transport git-remote-<name>, from PATH.
        refused = {
            "git merge -s": ["merge", "-s", "x", "topic"],
            "git merge -sx": ["merge", "-sx", "topic"],
            "git merge -ns": ["merge", "-ns", "x", "topic"],
            "git merge --strategy=": ["merge", "--strategy=x", "topic"],
            "git merge --str": ["merge", "--str", "x", "topic"],
            "git merge -s ": ["merge", "--no-ff", "-s"],
            "git rebase --strategy=": ["rebase", "--strategy=x", "main"],
            "git rebase -is": ["rebase", "-is", "x", "main"],
            "git pull -s": ["pull", "-s", "x", "origin"],
            "git pull --strategy": ["pull", "--strategy", "x"],
            "git cherry-pick --strategy=": ["cherry-pick", "--strategy=x", "HEAD"],
            "git cherry-pick --strat": ["cherry-pick", "--strat", "x", "HEAD"],
            "git revert --strategy": ["revert", "--strategy", "Ours", "HEAD"],
            "git fetch x::": ["fetch", "x::anything"],
            "git fetch ext::": ["fetch", "ext::x"],
            "git fetch https::": ["fetch", "https::example.invalid/r"],
            "git fetch x://": ["fetch", "x://host/r"],
            "git fetch HTTPS://": ["fetch", "HTTPS://host/r"],
            "git pull x::": ["pull", "x::a", "main"],
            "git push x::": ["push", "x::a", "main"],
            "git push --repo=": ["push", "--repo=x::a"],
            "git ls-remote x::": ["ls-remote", "x::a"],
            "git clone x::": ["clone", "x::a", "dir"],
            "git remote x::": ["remote", "add", "o", "x::a"],
            "git archive --remote=": ["archive", "--remote=x::a", "HEAD"],
            "git archive x::": ["archive", "--remote", "x::a", "HEAD"],
            "git submodule x::": ["submodule", "add", "x::a"],
            "the setting url.x::a.insteadof": ["-c", "url.x::a.insteadOf=origin", "fetch"],
            "the setting url.a.pushinsteadof": ["-c", "url.a.pushInsteadOf=b", "push"],
            "the setting remote.origin.url": ["-c", "remote.origin.url=x::a", "fetch"],
            "the setting remote.origin.pushurl": ["-c", "remote.origin.pushUrl=x::a", "push"],
            "the setting submodule.a.url": ["-c", "submodule.a.url=x::a", "submodule", "update"],
            "the setting branch.main.mergeoptions": ["-c", "branch.main.mergeOptions=-s x", "pull"],
            "the setting branch.main.remote": ["-c", "branch.main.remote=x::foo", "pull"],
            "the setting branch.main.pushremote": ["-c", "branch.main.pushRemote=1::foo", "push"],
            "the setting remote.pushdefault": ["-c", "remote.pushDefault=x://h/r", "push"],
            "the setting checkout.defaultremote": ["--config-env", "checkout.defaultRemote=V", "checkout", "a"],
        }
        # git's is_urlschemechar takes a digit first, so a digit-first transport runs a helper on every subcommand.
        for subcommand in sorted(git_client.ADDRESS_SUBCOMMANDS):
            for address in ("1::x", "1x://host/r", "9+a.b-c::x"):
                refused[f"git {subcommand} {address}"] = [subcommand, address]
        for what, arguments in refused.items():
            with self.subTest(arguments=arguments):
                runner, calls = scripted(GitResult(0, "", ""))
                refusal = rf"^{re.escape(what.strip())}\S* starts a command or a program whose git calls a plain"
                with self.assertRaisesRegex(ValueError, refusal):
                    GitClient(runner).run(arguments)
                self.assertEqual([], calls, "refused before git starts")
        builtin = [
            [subcommand, option, strategy, "topic"]
            for subcommand in ("merge", "rebase", "pull")
            for option in ("-s", "--strategy", "--strat")
            for strategy in ("ort", "recursive", "resolve", "octopus", "ours", "subtree")
        ]
        allowed: tuple[list[str], ...] = (
            *builtin,
            ["merge", "-sours", "topic"],
            ["merge", "--strategy=ort", "-Xours", "--strategy-option=theirs", "topic"],
            ["merge", "-ms", "topic"],
            ["rebase", "-Xs", "main"],
            ["cherry-pick", "-s", "x"],
            ["revert", "-s", "HEAD"],
            ["log", "--strategy=x", "x::a"],
            ["fetch", "https://example.invalid/r", "main:topic"],
            ["clone", "ssh://git@[::1]/r", "git@host:r", "file:///c:/r", "C:\\r", "dir"],
            ["push", "git+ssh://host/r", "HEAD:refs/heads/a"],
            ["ls-remote", "git://host/r", "ftps://host/r"],
            ["remote", "add", "o", "../r"],
            ["-c", "branch.main.remote=origin", "-c", "remote.pushDefault=https://h/r", "pull"],
        )
        for arguments in allowed:
            with self.subTest(arguments=arguments):
                runner, calls = scripted(GitResult(0, "", ""))
                GitClient(runner).run(arguments)
                self.assertEqual(["git", *arguments], calls[0][0])

    def test_a_plain_client_refuses_a_config_write_of_a_setting_a_later_command_runs_a_program_by(self) -> None:
        # Written to a file, each setting makes a later plain fetch, pull, or commit start a program the client never
        # reads; an edit starts the editor, and a section rename can make one section's settings another's.
        refused = {
            "remote.origin.url": ["config", "remote.origin.url", "x::a"],
            "remote.origin.pushurl": ["config", "--add", "remote.origin.pushurl", "x::a"],
            "url.x::a.insteadOf": ["config", "--global", "url.x::a.insteadOf", "origin"],
            "Url.a.PushInsteadOf": ["config", "--replace-all", "Url.a.PushInsteadOf", "b"],
            "submodule.a.url": ["config", "--file", ".gitmodules", "submodule.a.url", "x::a"],
            "branch.main.mergeOptions": ["config", "set", "branch.main.mergeOptions", "-s x"],
            "pull.twohead": ["config", "--local", "pull.twohead", "x"],
            "core.editor": ["config", "-f", "c", "core.editor", "vim"],
            "core.sshCommand=": ["config", "set", "--value=x", "core.sshCommand=x"],
            "include.path": ["config", "include.path", "/x"],
            "includeIf.gitdir:/a.path": ["config", "includeIf.gitdir:/a.path", "/x"],
            "core.hooksPath": ["config", "--unset", "core.hooksPath"],
            # A read option given as another option's value is no read.
            "core.pager": ["config", "--comment", "--get", "core.pager", "x"],
            "core.askpass": ["config", "--fi", "get", "core.askpass", "x"],
            "--edit": ["config", "--edit"],
            "--ed": ["config", "--global", "--ed"],
            "--e": ["config", "--e"],
            # git takes a remote name no remote has as an address, so a foreign transport there runs its helper.
            "branch.main.remote": ["config", "branch.main.remote", "x::foo"],
            "branch.main.pushRemote": ["config", "set", "branch.main.pushRemote", "1x://h/r"],
            "remote.pushDefault": ["config", "--add", "remote.pushDefault", "x::foo"],
            "checkout.defaultRemote": ["config", "--global", "checkout.defaultRemote", "x::foo"],
            "branch.a.remote": ["config", "--unset", "branch.a.remote"],
            "-e": ["config", "-e"],
            "-le": ["config", "-le"],
            "edit": ["config", "edit"],
            "--rename-section": ["config", "--rename-section", "a", "remote.x"],
            "rename-section": ["config", "rename-section", "a", "url.x::a"],
        }
        for what, arguments in refused.items():
            with self.subTest(arguments=arguments):
                runner, calls = scripted(GitResult(0, "", ""))
                refusal = rf"^git config {re.escape(what)}\S* starts a command or a program whose git calls a plain"
                with self.assertRaisesRegex(ValueError, refusal):
                    GitClient(runner).run(arguments)
                self.assertEqual([], calls, "refused before git starts")
        allowed: tuple[list[str], ...] = (
            ["config", "--get", "remote.origin.url"],
            ["config", "--file", "c", "--get", "core.worktree"],
            ["config", "--local", "--type=bool", "--get", "x.y"],
            ["config", "get", "remote.origin.url"],
            ["config", "--get-regexp", "url\\..*\\.insteadof"],
            ["config", "-l"],
            ["config", "list", "--show-origin"],
            ["config", "user.name", "t"],
            ["config", "--add", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"],
            ["config", "set", "core.quotePath", "off"],
            ["config", "branch.main.remote", "origin"],
            ["config", "remote.pushDefault", "https://example.invalid/r"],
        )
        for arguments in allowed:
            with self.subTest(arguments=arguments):
                runner, calls = scripted(GitResult(0, "", ""))
                GitClient(runner).run(arguments)
                self.assertEqual(["git", *arguments], calls[0][0])

    def test_every_subcommand_a_plain_client_runs_is_one_of_git_s_own_since_2_31(self) -> None:
        # Pinned rather than read from the git that runs the test, which may be older or newer.
        self.assertEqual(set(), set(git_client.PLAIN_SUBCOMMANDS) - GIT_2_56_COMMANDS)
        self.assertEqual(set(), set(git_client.PLAIN_SUBCOMMANDS) & COMMANDS_AFTER_2_31)
        self.assertLessEqual(COMMANDS_AFTER_2_31, GIT_2_56_COMMANDS)
        for name in sorted(COMMANDS_AFTER_2_31):
            with self.subTest(name=name):
                runner, calls = scripted(GitResult(0, "", ""))
                with self.assertRaisesRegex(ValueError, rf"^git {name} starts a command or a program"):
                    GitClient(runner).run([name])
                self.assertEqual([], calls)

    def test_every_setting_git_lists_that_names_a_program_is_refused(self) -> None:
        # Each key of git 2.56's `git help --config` whose value is a command, a program, or a strategy that names
        # one, with a name for each placeholder; and the two left out on purpose.
        for key in PROGRAM_SETTINGS_2_56:
            with self.subTest(key=key):
                self.assertTrue(git_client.names_program(key.casefold()))
                runner, calls = scripted(GitResult(0, "", ""))
                with self.assertRaisesRegex(ValueError, rf"^the setting {re.escape(key.casefold())} starts"):
                    GitClient(runner).run(["-c", f"{key}=x", "status"])
                self.assertEqual([], calls)
        for key in ("receive.procReceiveRefs", "core.fsmonitorHookVersion", "pull.rebase"):
            with self.subTest(key=key):
                self.assertFalse(git_client.names_program(key.casefold()))

    def test_a_carried_command_never_runs_on_a_plain_client(self) -> None:
        # Unrefused, the rebase runs its command after the commit it replays, outside the client.
        marker = self.repository / "ran"
        identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        for message in ("one", "two"):
            GitClient(timeout=60).output(
                [*identity, "commit", "--quiet", "--allow-empty", "-m", message], directory=self.repository
            )
        carried = [
            *identity,
            "rebase",
            "--quiet",
            "--force-rebase",
            "-x",
            "git var GIT_COMMITTER_IDENT > ran",
            "HEAD~1",
        ]
        with self.assertRaisesRegex(ValueError, r"^git rebase -x starts a command or a program"):
            GitClient(timeout=60).run(carried, directory=self.repository)
        self.assertFalse(marker.exists())

    def test_a_command_line_alias_runs_on_an_isolated_client(self) -> None:
        # The alias reaches grep --no-index, which an isolated client runs.
        (self.inner / "f.txt").write_bytes(b"needle\n")
        arguments = ["-c", "alias.s=grep", "s", "--no-index", "-l", "-e", "needle"]
        self.assertEqual("f.txt\n", GitClient(isolated=True).output(arguments, directory=self.inner))

    def test_a_pattern_goes_right_after_the_subcommand_and_any_spelling_of_no_index_is_searched_for(self) -> None:
        for spelling in ("--no-i", "--no-index", "-e", "--"):
            with self.subTest(pattern=spelling):
                runner, calls = scripted(GitResult(0, "", ""))
                GitClient(runner).run(["-c", "a.b=c", "grep", "-n", "HEAD", "--"], directory="d", pattern=spelling)
                self.assertEqual(
                    ["git", "-C", "d", "-c", "a.b=c", "grep", "-e", spelling, "-n", "HEAD", "--"], calls[0][0]
                )
        runner, _ = scripted(GitResult(0, "", ""))
        with self.assertRaisesRegex(ValueError, "^a pattern needs a subcommand to follow$"):
            GitClient(runner).run([], pattern="x")

    def test_a_search_for_the_literal_no_index_runs_in_the_repository(self) -> None:
        (self.inner / "f.txt").write_bytes(b"--no-index\n")
        GitClient(timeout=60).output(["add", "f.txt"], directory=self.inner)
        arguments = ["grep", "--null", "-n", "--full-name", "--cached", "--"]
        client = GitClient(timeout=60)
        found = client.output(arguments, directory=self.inner, pattern="--no-index")
        self.assertEqual("content it did not write/f.txt\x001\x00--no-index\n", found)
        with client.stream(arguments, directory=self.inner, pattern="--no-index") as stream:
            stream.stdin.close()
            self.assertEqual(found.encode(), stream.read(200))

    def test_the_reviewed_forms_never_reach_git_unisolated(self) -> None:
        # Each ran unisolated in a planted parent repository before the refusal read every argument.
        (self.inner / "a.txt").write_bytes(b"needle\n")
        with (self.repository / ".git" / "config").open("a", encoding="utf-8") as config:
            config.write("[grep]\n\tcolumn = true\n")
        for arguments in (
            ["grep", "-n", "-e", "needle", "--no-index", "--", "a.txt"],
            ["grep", "-n", "--no-inde", "-e", "needle", "--"],
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ValueError, r"GitClient\(isolated=True\)"):
                    GitClient(timeout=60).run(arguments, directory=self.inner)
                found = GitClient(timeout=60, isolated=True).output(arguments, directory=self.inner)
                self.assertEqual("a.txt:1:needle\n", found)

    def test_an_isolated_grep_without_an_index_reads_no_repository_around_it(self) -> None:
        (self.inner / "f.txt").write_bytes(b"needle\n")
        with (self.repository / ".git" / "config").open("a", encoding="utf-8") as config:
            config.write("[grep]\n\tcolumn = true\n[unterminated\n")
        arguments = ["grep", "--null", "-n", "--no-index", "-e", "needle", "--"]
        self.assertEqual(128, GitClient(timeout=60).run(["status"], directory=self.inner).returncode)
        isolated = GitClient(timeout=60, isolated=True)
        self.assertEqual("f.txt\x001\x00needle\n", isolated.output(arguments, directory=self.inner))
        with isolated.stream(arguments, directory=self.inner) as stream:
            stream.stdin.close()
            self.assertEqual((b"f.txt\x001\x00needle\n", 0), (stream.read(100), stream.wait()))

    def test_a_plain_client_reads_each_setting_and_an_isolated_one_none_in_run_output_and_stream(self) -> None:
        plain, isolated = GitClient(timeout=60), GitClient(timeout=60, isolated=True)
        for key, value in (("fixture.source", "repository"), ("fixture.global", "yes"), ("fixture.scope", "command")):
            with self.subTest(key=key):
                arguments = ["config", "--get", key]
                self.assertEqual(f"{value}\n", plain.output(arguments, directory=self.inner))
                result = isolated.run(arguments, directory=self.inner)
                self.assertEqual((1, ""), (result.returncode, result.stdout))
                with isolated.stream(arguments, directory=self.inner) as stream:
                    stream.stdin.close()
                    self.assertEqual((b"", 1), (stream.read(1), stream.wait()))

    def test_an_isolated_command_finds_no_repository_in_its_folder_or_above_it(self) -> None:
        listing = GitClient(timeout=60, isolated=True).run(["rev-parse", "--git-dir"], directory=self.inner)
        self.assertNotEqual(0, listing.returncode)
        self.assertIn("not a git repository", listing.stderr)
        found = GitClient(timeout=60).output(["rev-parse", "--absolute-git-dir"], directory=self.inner).strip()
        self.assertEqual(
            str((self.repository / ".git").resolve()),
            str(Path(found).resolve()),
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
        ["--shallow-file", "push", "fetch", "origin"],
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


def test_a_command_is_named_by_its_subcommand_behind_git_s_own_options() -> None:
    assert git_client.command_name(["--no-optional-locks", "-c", "a=b", "status"]) == "git status"
    assert git_client.command_name(["--shallow-file", "s", "log"]) == "git log"
    assert git_client.command_name(["--no-optional-locks"]) == "git"


@pytest.mark.parametrize(
    "arguments", [["-c"], ["--config-env"], ["-C", "d", "-c"], ["--no-optional-locks", "--config-env"]]
)
def test_a_configuration_option_with_no_value_after_it_sets_no_section(arguments: list[str]) -> None:
    # git refuses the command itself; the client reads no section past the end and leaves the refusal to git.
    assert git_client.configured_keys(arguments) == []
    assert not git_client.defines_alias(arguments)
    runner, calls = scripted(GitResult(129, "", "error: option requires a value\n"))
    assert GitClient(runner).run(arguments).returncode == 129
    assert calls[0][0] == ["git", *arguments]


def test_the_configured_keys_are_read_whole_and_case_folded() -> None:
    arguments = ["-c", "Alias.S=grep", "--config-env=Help.AutoCorrect=WHEN", "-C", "d", "status", "-c", "x.y=z"]
    assert git_client.configured_keys(arguments) == ["alias.s", "help.autocorrect"]


def test_output_line_takes_off_only_the_newline_that_ends_a_line() -> None:
    # A ref name or a path may end in a character str.strip() also drops.
    for name in ("topic\u00a0", "topic\u2028", "topic\u0085", " topic", "topic\t", ""):
        assert git_client.output_line(f"{name}\n") == name
        assert git_client.output_line(name) == name
    assert git_client.output_line("a\n\n") == "a\n"


# Literal stderr of a fetch that reached the remote but failed in the local repository, as git prints it.
LOCAL_FAILURES = {
    "stale ref lock": "error: cannot lock ref 'refs/remotes/origin/main': Unable to create "
    "'/home/u/clone/.git/refs/remotes/origin/main.lock': File exists.\n\nAnother git process seems to be running in "
    "this repository, or the lock file may be stale\n",
    "unwritable objects": "error: insufficient permission for adding an object to repository database .git/objects\n"
    "fatal: failed to write object\nfatal: unpack-objects failed\n",
    "full disk": "fatal: write error: No space left on device\nfatal: index-pack failed\n",
    "broken config": "fatal: bad config line 3 in file .git/config\n",
    "dubious ownership": "fatal: detected dubious ownership in repository at '/srv/clone'\n",
}
# The remote's failures, which name nothing of the local repository, among them a remote's own lock on its line.
REMOTE_FAILURES = {
    **PERMANENT,
    "early eof": EARLY_EOF,
    "unresolved": UNRESOLVED,
    "hung up": HUNG_UP,
    "missing origin": "fatal: '/srv/missing.git' does not appear to be a git repository\n"
    "fatal: Could not read from remote repository.\n",
    "refused connection": "fatal: unable to access 'https://127.0.0.1:9/x.git/': Failed to connect to 127.0.0.1 port "
    "9 after 0 ms: Couldn't connect to server\n",
    "remote's lock": "remote: error: cannot lock ref 'refs/heads/main': Unable to create 'main.lock': File exists.\n"
    "fatal: the remote end hung up unexpectedly\n",
}


@pytest.mark.parametrize("stderr", [pytest.param(stderr, id=name) for name, stderr in LOCAL_FAILURES.items()])
def test_a_fetch_that_failed_in_the_local_repository_is_not_the_remote_s_failure(stderr: str) -> None:
    assert git_client.local_failure(stderr)
    assert not GitError(stderr.strip(), kind=classify_failure(stderr)).origin_failed


@pytest.mark.parametrize("stderr", [pytest.param(stderr, id=name) for name, stderr in REMOTE_FAILURES.items()])
def test_a_fetch_whose_failure_names_nothing_local_is_the_remote_s_failure(stderr: str) -> None:
    assert not git_client.local_failure(stderr)
    assert GitError(stderr.strip(), kind=classify_failure(stderr)).origin_failed


def test_only_a_timeout_among_the_failures_git_never_answered_is_the_remote_s() -> None:
    answered = {kind: GitError("git said nothing", kind=kind).origin_failed for kind in get_args(GitErrorKind)}
    assert answered == {
        "git": True,
        "timeout": True,
        "prerequisite": False,
        "execution": False,
        "not_repository": False,
    }

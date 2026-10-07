"""Regression tests for the git client every skill that runs git shares."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bounded_process
import git_client
from git_client import GitClient, GitError, GitResult, classify_failure

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


class SubprocessRunnerTests(unittest.TestCase):
    def test_the_runner_goes_through_the_bounded_layer(self) -> None:
        finished = bounded_process.Finished(2, b"out \xff", b"bad \xff")
        with mock.patch.object(git_client, "run_bounded", return_value=finished) as run:
            result = git_client.subprocess_runner(["git", "status"], 12)
        self.assertEqual(mock.call(["git", "status"], 12), run.call_args)
        self.assertEqual(GitResult(2, "out \udcff", "bad �"), result)

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

    def test_real_git_outside_a_repository_is_not_a_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "plain folder"
            directory.mkdir()
            with (
                mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": temporary}),
                self.assertRaises(GitError) as context,
            ):
                GitClient().output(["rev-parse", "--show-toplevel"], directory=directory)
        self.assertEqual("not_repository", context.exception.kind)


if __name__ == "__main__":
    unittest.main()

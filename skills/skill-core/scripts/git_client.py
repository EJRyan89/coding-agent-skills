"""The git client every skill script that runs git shares.

One runner, bounded and non-interactive (see `bounded_process`), and one failure classifier, so a failure means the
same thing in every skill and no git command waits on a credential prompt. Tests inject the runner.

A git exit status is often an answer, such as `merge-base --is-ancestor` saying no, so `run` returns every result
that git produced and raises only when git could not run or finish; `output` raises on a nonzero exit as well.
stdout is decoded with surrogateescape, so `stdout.encode("utf-8", "surrogateescape")` is exactly what git printed;
stderr only feeds messages, so a byte that is not UTF-8 becomes U+FFFD there.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from bounded_process import run_bounded

DEFAULT_TIMEOUT_SECONDS = 300.0
NOT_REPOSITORY_MARKERS = ("not a git repository",)
MISSING_GIT = "Git executable 'git' was not found; install Git first"


class GitError(RuntimeError):
    """A git command that could not run, did not finish, or failed, with its class in `kind`.

    `prerequisite` (git is not installed), `execution` (it could not start), `timeout`, `not_repository`, or `git`
    for any other failure.
    """

    def __init__(self, message: str, *, kind: str = "git", returncode: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.returncode = returncode  # git's exit code, when git ran and failed


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str

    def output_bytes(self) -> bytes:
        """The exact bytes git printed on stdout."""
        return self.stdout.encode("utf-8", "surrogateescape")


Runner = Callable[[Sequence[str], float], GitResult]


def subprocess_runner(command: Sequence[str], timeout: float) -> GitResult:
    """Run a command through the bounded, non-interactive layer, classifying why it could not run or finish."""
    try:
        finished = run_bounded(command, timeout)
    except FileNotFoundError as exc:
        raise GitError(MISSING_GIT, kind="prerequisite") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"{' '.join(command)} did not finish within {timeout:g} seconds", kind="timeout") from exc
    except OSError as exc:
        raise GitError(f"Git could not be started: {exc}", kind="execution") from exc
    return GitResult(
        finished.returncode,
        finished.stdout.decode("utf-8", "surrogateescape"),
        finished.stderr.decode("utf-8", "replace"),
    )


def classify_failure(stderr: str) -> str:
    """The class of a git command that exited nonzero, from its stderr."""
    lowered = stderr.casefold()
    if any(marker in lowered for marker in NOT_REPOSITORY_MARKERS):
        return "not_repository"
    return "git"


class GitClient:
    """Runs git within a time limit, with no stdin and no prompt, and classifies its failures."""

    def __init__(self, runner: Runner = subprocess_runner, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.runner = runner
        self.timeout = timeout

    def run(
        self, arguments: Sequence[str], *, directory: str | Path | None = None, timeout: float | None = None
    ) -> GitResult:
        """Run `git <arguments>`, in `directory` when given (as `git -C`), and return its result whatever its exit.

        Raises GitError when git is missing, cannot start, or does not finish within `timeout` (the client's default
        when None).
        """
        command = ["git", *(["-C", str(directory)] if directory is not None else []), *arguments]
        return self.runner(command, self.timeout if timeout is None else timeout)

    def output(
        self, arguments: Sequence[str], *, directory: str | Path | None = None, timeout: float | None = None
    ) -> str:
        """Run `git <arguments>` as `run` does and return its stdout; a nonzero exit raises a classified GitError."""
        result = self.run(arguments, directory=directory, timeout=timeout)
        if result.returncode != 0:
            message = (
                result.stderr.strip() or f"git {' '.join(arguments[:1])} failed with exit code {result.returncode}"
            )
            raise GitError(message, kind=classify_failure(result.stderr), returncode=result.returncode)
        return result.stdout

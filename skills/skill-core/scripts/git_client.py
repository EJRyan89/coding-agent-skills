"""The git client every skill script that runs git shares.

One runner, bounded and non-interactive (see `bounded_process`), and one failure classifier, so a failure means the
same thing in every skill and no git command waits on a credential prompt. Tests inject the runner.

A git exit status is often an answer, such as `merge-base --is-ancestor` saying no, so `run` returns every result
that git produced and raises only when git could not run or finish; `output` raises on a nonzero exit as well.
stdout is decoded with surrogateescape, so `stdout.encode("utf-8", "surrogateescape")` is exactly what git printed;
stderr only feeds messages, so a byte that is not UTF-8 becomes U+FFFD there.

`input_bytes` gives a command such as `fast-import` its whole stdin up front; it then sees stdin closed,
and the time limit and the failure classification apply as without it. Tests inject `input_runner` for that form.

`stream` runs a git command that answers requests while it runs, such as `cat-file --batch`, with the same
environment; there the time limit bounds each wait for output rather than the whole command.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol, TypeVar

from bounded_process import Streaming, run_bounded, streaming

T = TypeVar("T")

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


class InputRunner(Protocol):
    """A runner that gives the command `input_bytes` as its whole stdin."""

    def __call__(self, command: Sequence[str], timeout: float, *, input_bytes: bytes) -> GitResult: ...


def subprocess_runner(command: Sequence[str], timeout: float, *, input_bytes: bytes | None = None) -> GitResult:
    """Run a command through the bounded, non-interactive layer, classifying why it could not run or finish.

    With `input_bytes`, the command's stdin is exactly those bytes and then closed.
    """
    try:
        if input_bytes is None:
            finished = run_bounded(command, timeout)
        else:
            finished = run_bounded(command, timeout, input_bytes=input_bytes)
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


def _command(arguments: Sequence[str], directory: str | Path | None) -> list[str]:
    return ["git", *(["-C", str(directory)] if directory is not None else []), *arguments]


class GitStream:
    """A running git command's pipes: requests go to `stdin`, and each read waits at most the idle timeout."""

    def __init__(self, running: Streaming) -> None:
        self._running = running
        self.stdin: IO[bytes] = running.stdin

    def read(self, size: int) -> bytes:
        """The next `size` bytes of output, fewer only at its end."""
        return self._bounded(lambda: self._running.read(size))

    def readline(self) -> bytes:
        """The next line of output with its newline, or what is left at its end."""
        return self._bounded(self._running.readline)

    def wait(self) -> int:
        """The exit status once git has exited."""
        return self._bounded(self._running.wait)

    def stderr(self) -> str:
        """What git has written to stderr so far, with each byte that is not UTF-8 as U+FFFD."""
        return self._running.errors().decode("utf-8", "replace")

    def _bounded(self, call: Callable[[], T]) -> T:
        try:
            return call()
        except subprocess.TimeoutExpired as exc:
            command = " ".join(str(part) for part in exc.cmd) if isinstance(exc.cmd, list) else str(exc.cmd)
            raise GitError(f"{command} gave no output for {exc.timeout:g} seconds", kind="timeout") from exc


class GitClient:
    """Runs git within a time limit, with no stdin but input given up front and no prompt, and classifies its
    failures."""

    def __init__(
        self,
        runner: Runner = subprocess_runner,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        input_runner: InputRunner = subprocess_runner,
    ) -> None:
        self.runner = runner
        self.input_runner = input_runner
        self.timeout = timeout

    def run(
        self,
        arguments: Sequence[str],
        *,
        directory: str | Path | None = None,
        timeout: float | None = None,
        input_bytes: bytes | None = None,
    ) -> GitResult:
        """Run `git <arguments>`, in `directory` when given (as `git -C`), and return its result whatever its exit.

        With `input_bytes`, git's stdin is exactly those bytes and then closed, through `input_runner`.
        Raises GitError when git is missing, cannot start, or does not finish within `timeout` (the client's default
        when None).
        """
        command = _command(arguments, directory)
        limit = self.timeout if timeout is None else timeout
        if input_bytes is None:
            return self.runner(command, limit)
        return self.input_runner(command, limit, input_bytes=input_bytes)

    def output(
        self,
        arguments: Sequence[str],
        *,
        directory: str | Path | None = None,
        timeout: float | None = None,
        input_bytes: bytes | None = None,
    ) -> str:
        """Run `git <arguments>` as `run` does and return its stdout; a nonzero exit raises a classified GitError."""
        result = self.run(arguments, directory=directory, timeout=timeout, input_bytes=input_bytes)
        if result.returncode != 0:
            message = (
                result.stderr.strip() or f"git {' '.join(arguments[:1])} failed with exit code {result.returncode}"
            )
            raise GitError(message, kind=classify_failure(result.stderr), returncode=result.returncode)
        return result.stdout

    @contextmanager
    def stream(
        self, arguments: Sequence[str], *, directory: str | Path | None = None, idle_timeout: float | None = None
    ) -> Iterator[GitStream]:
        """Start `git <arguments>` with a pipe for requests, and stop it on leaving the block.

        Each read fails as a `timeout` when no output arrives for `idle_timeout` seconds (the client's default when
        None), so a long answer that keeps arriving never times out. Leaving the block closes git's output, so git
        exits by itself; see `bounded_process.Streaming`.
        """
        command = _command(arguments, directory)
        with ExitStack() as stack:
            try:
                running = stack.enter_context(
                    streaming(command, idle_timeout=self.timeout if idle_timeout is None else idle_timeout)
                )
            except FileNotFoundError as exc:
                raise GitError(MISSING_GIT, kind="prerequisite") from exc
            except OSError as exc:
                raise GitError(f"Git could not be started: {exc}", kind="execution") from exc
            yield GitStream(running)

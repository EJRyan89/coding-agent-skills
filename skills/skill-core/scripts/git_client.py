"""The git client every skill script that runs git shares.

One runner, bounded and non-interactive (see `bounded_process`), and one failure classifier, so a failure means the
same thing in every skill and no git command waits on a credential prompt. The runner and `stream` start git without
its repository-selecting variables (`bounded_process.REPOSITORY_VARIABLES`), so a call reads the repository its
`directory` names, or the one it runs in, whatever `GIT_DIR` or `GIT_INDEX_FILE` the shell or a git hook exported. Tests
inject the runner.

A git exit status is often an answer, such as `merge-base --is-ancestor` saying no, so `run` returns every result
that git produced and raises only when git could not run or finish; `output` raises on a nonzero exit as well.
stdout is decoded with surrogateescape, so `stdout.encode("utf-8", "surrogateescape")` is exactly what git printed;
stderr only feeds messages, so a byte that is not UTF-8 becomes U+FFFD there.

`input_bytes` gives a command such as `fast-import` its whole stdin up front; it then sees stdin closed,
and the time limit and the failure classification apply as without it. Tests inject `input_runner` for that form: a
client given a `runner` alone refuses a call with input rather than run real git under a test.

`stream` runs a git command that answers requests while it runs, such as `cat-file --batch`, with the same
environment; there the time limit bounds each wait for output rather than the whole command.

Both hold git in a process tree (`job_tree`), because git starts programs of its own: Git for Windows' launcher starts
the real git, a fetch starts its transport and pack programs, and a command may run a credential helper, a hook, or an
alias. So a call that times out, and a stream once closed, leave none of them running, and a tree that cannot be
ended fails the call as an `execution` error saying they may still run.

Both start git at the absolute path skill-core's `executable_locator` finds for it, never by its bare name, which
process creation on Windows looks up in the working directory before PATH, so a `git.exe` in the folder a skill runs
from is never the git it runs. The path is looked up once for each value of the variables the locator reads.

A transient failure of a command that only fetches from a remote, a connection dropped or a host briefly not found, is
retried a few times with short waits inside the call's own time limit, and `run` returns the last answer. Every other
command runs once, and `push` is never repeated. Tests inject the sleeper and the clock.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol, TypeVar

from bounded_process import Streaming, run_bounded, streaming
from executable_locator import Found, located_command
from job_tree import TreeNotEnded, process_tree

T = TypeVar("T")

DEFAULT_TIMEOUT_SECONDS = 300.0
NOT_REPOSITORY_MARKERS = ("not a git repository",)
MISSING_GIT = "Git executable 'git' was not found; install Git first"

# The subcommands retried after a transient failure: each only reads from the remote, and a fetch cut off after it
# updated some refs leaves them where a second fetch of the same refspecs puts them, so a repeat is safe. `push` may
# have landed before its answer was lost, and `clone` runs through no skill's client, so neither is here.
RETRIED_SUBCOMMANDS = frozenset({"fetch", "ls-remote"})
# git's options that take their value as the next argument, so the subcommand is never read from a value.
GLOBAL_VALUE_OPTIONS = frozenset({"-c", "-C", "--git-dir", "--work-tree", "--namespace"})
# A dropped connection, a failed handshake, a host briefly not found, or a gateway briefly down clears in seconds; a
# longer outage should fail fast rather than hold a skill for minutes, leaving the rerun to the user.
TRANSIENT_RETRIES = 2
TRANSIENT_BASE_SECONDS = 2.0
# The lines git and its transports print themselves. A transient marker counts only on one of them, so the words in
# a line the remote sent (`remote:`) or a hook printed, such as a quoted commit message, never make a failure
# transient.
GIT_LINE_PREFIXES = ("fatal:", "error:", "fetch-pack:", "ssh:")
# Lowered markers of a transport failure that may clear on its own. Unlike gh's policy, a host git could not resolve
# is here: git looks the host up afresh on every fetch, and a lookup that fails while a network or VPN connection
# comes back is the commonest brief fault on a workstation; offline, the retries cost seconds.
TRANSIENT_MARKERS = (
    "rpc failed",
    "early eof",
    "unexpected disconnect",
    "remote end hung up",
    "could not resolve host",
    "connection reset",
    "connection was reset",
    "connection timed out",
    "failed to receive handshake",
    "gnutls_handshake() failed",
    "ssl_error_syscall",
    "returned error: 502",
    "returned error: 503",
    "returned error: 504",
)
# Lowered markers of an answer no retry changes: an unknown ref, a refused authentication, a missing repository, a
# bad object, a certificate problem, or any 4xx status. Read anywhere in stderr, the remote's lines included, and
# checked first, so a failure that names one is never retried whatever transport message comes with it.
PERMANENT_MARKERS = (
    "couldn't find remote ref",
    "authentication failed",
    "could not read username",
    "could not read password",
    "invalid username or password",
    "permission denied",
    "not found",
    "does not appear to be a git repository",
    "bad object",
    "not our ref",
    "ssl certificate problem",
    "returned error: 4",
)


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


# The functions that start a command whose program tests/run_validation.py cannot read here, with the reason.
COMMANDS_ALLOWED = {
    "subprocess_runner": "GitClient's default runner, which runs the git commands GitClient builds; the policy "
    "reads every GitClient as running git",
}


# The git the runner and `stream` found, kept so a run of many git commands looks it up once.
FOUND: Found = {}


def subprocess_runner(command: Sequence[str], timeout: float, *, input_bytes: bytes | None = None) -> GitResult:
    """Run a command through the bounded, non-interactive layer, its program at the path the locator finds,
    classifying why it could not run or finish.

    With `input_bytes`, the command's stdin is exactly those bytes and then closed. Every program the command
    started is ended with it, once it has finished or been killed.
    """
    tree = process_tree(hidden_window=True)
    try:
        if input_bytes is None:
            finished = run_bounded(located_command(command, kept=FOUND), timeout, tree=tree)
        else:
            finished = run_bounded(located_command(command, kept=FOUND), timeout, input_bytes=input_bytes, tree=tree)
    except FileNotFoundError as exc:
        raise GitError(MISSING_GIT, kind="prerequisite") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"{' '.join(command)} did not finish within {timeout:g} seconds", kind="timeout") from exc
    except TreeNotEnded as exc:
        raise GitError(_not_ended(command, exc), kind="execution") from exc
    except OSError as exc:
        raise GitError(f"Git could not be started: {exc}", kind="execution") from exc
    return GitResult(
        finished.returncode,
        finished.stdout.decode("utf-8", "surrogateescape"),
        finished.stderr.decode("utf-8", "replace"),
    )


def _not_ended(command: Sequence[str], error: TreeNotEnded) -> str:
    """The message of a git command whose programs could not be ended."""
    return f"{' '.join(str(part) for part in command)} left programs it started that could not be ended: {error}"


def classify_failure(stderr: str) -> str:
    """The class of a git command that exited nonzero, from its stderr."""
    lowered = stderr.casefold()
    if any(marker in lowered for marker in NOT_REPOSITORY_MARKERS):
        return "not_repository"
    return "git"


def subcommand(arguments: Sequence[str]) -> str | None:
    """The git subcommand `arguments` run, after any of git's own options, or None when they name none."""
    parts = iter(arguments)
    for part in parts:
        if part in GLOBAL_VALUE_OPTIONS:
            next(parts, None)
        elif not part.startswith("-"):
            return part
    return None


def transient_failure(result: GitResult) -> bool:
    """Whether a git command's result is a transport failure that may clear on its own.

    A zero exit is an answer. A failure is transient when one of git's own lines names a transient fault and no line
    names a permanent one.
    """
    if result.returncode == 0:
        return False
    lowered = result.stderr.casefold()
    if any(marker in lowered for marker in PERMANENT_MARKERS):
        return False
    own = [line for line in lowered.splitlines() if line.lstrip().startswith(GIT_LINE_PREFIXES)]
    return any(marker in line for line in own for marker in TRANSIENT_MARKERS)


def transient_wait(retry: int) -> float:
    """The wait before transient retry `retry`, counted from 0, doubling from `TRANSIENT_BASE_SECONDS`."""
    return TRANSIENT_BASE_SECONDS * 2**retry


def _gave_up(result: GitResult, retries: int, why: str = "") -> GitResult:
    """`result` with the retries spent on it named on a line after git's own stderr."""
    stderr = result.stderr if result.stderr.endswith("\n") or not result.stderr else result.stderr + "\n"
    note = f"gave up after {retries} {'retry' if retries == 1 else 'retries'}{why}"
    return GitResult(result.returncode, result.stdout, f"{stderr}({note})\n")


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

    def readline(self, limit: int | None = None) -> bytes:
        """The next line of output with its newline, or what is left at its end; with `limit`, at most that many
        bytes of it, leaving the rest of a longer line to the next read."""
        return self._bounded(lambda: self._running.readline(limit))

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


def _refuse_input(command: Sequence[str], timeout: float, *, input_bytes: bytes) -> GitResult:
    raise TypeError("this GitClient was given a runner but no input_runner, so it takes no input_bytes")


class GitClient:
    """Runs git within a time limit, with no stdin but input given up front and no prompt, and classifies its
    failures.

    `input_runner` runs the calls given `input_bytes`. It defaults to the real runner only while `runner` is the real
    runner too, so a client built on a test's runner never reaches real git through a call with input. `sleeper`
    makes the waits between retries, and `clock` measures what is left of a call's time limit.
    """

    def __init__(
        self,
        runner: Runner = subprocess_runner,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        input_runner: InputRunner | None = None,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.runner = runner
        if input_runner is None:
            input_runner = subprocess_runner if runner is subprocess_runner else _refuse_input
        self.input_runner = input_runner
        self.timeout = timeout
        # Looked up per client, so a test that patches time.sleep reaches a client built deep inside a call.
        self.sleeper = sleeper or time.sleep
        self.clock = clock or time.monotonic

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
        when None). A subcommand in `RETRIED_SUBCOMMANDS` that fails transiently runs again within that limit: the
        result is the last attempt's, and one that outlasts the retries names them on its stderr.
        """
        command = _command(arguments, directory)
        limit = self.timeout if timeout is None else timeout

        def attempt(seconds: float) -> GitResult:
            if input_bytes is None:
                return self.runner(command, seconds)
            return self.input_runner(command, seconds, input_bytes=input_bytes)

        if subcommand(arguments) not in RETRIED_SUBCOMMANDS:
            return attempt(limit)
        return self._retried(attempt, limit)

    def _retried(self, attempt: Callable[[float], GitResult], limit: float) -> GitResult:
        """Run `attempt` with the whole limit, then retry a transient failure with what is left of it.

        A wait that would leave no time is not made, and a retry that runs out the time left gives back the failure
        before it, so the call never runs longer than `limit`. A first attempt that runs out the limit still raises.
        """
        started = self.clock()
        result = attempt(limit)
        retries = 0
        while transient_failure(result):
            if retries == TRANSIENT_RETRIES:
                return _gave_up(result, retries)
            wait = transient_wait(retries)
            if limit - (self.clock() - started) <= wait:
                return _gave_up(result, retries, f": its {limit:g}-second time limit left no room for another")
            self.sleeper(wait)
            retries += 1
            try:
                result = attempt(limit - (self.clock() - started))
            except GitError as exc:
                if exc.kind != "timeout":
                    raise
                return _gave_up(result, retries, f": it ran out its {limit:g}-second time limit")
        return result

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

    def rev_parse_path(self, option: str, *, directory: str | Path) -> Path | None:
        """The absolute path `git rev-parse <option>` names for `directory`, such as `--show-toplevel` (the root of
        its checkout or worktree) or `--git-common-dir` (the Git directory every worktree of it shares).

        None outside a repository, and when git cannot run, does not finish, fails, or prints nothing.
        """
        try:
            result = self.run(["rev-parse", "--path-format=absolute", option], directory=directory)
        except GitError:
            return None
        value = result.stdout.rstrip("\r\n")
        return Path(value) if result.returncode == 0 and value else None

    @contextmanager
    def stream(
        self, arguments: Sequence[str], *, directory: str | Path | None = None, idle_timeout: float | None = None
    ) -> Iterator[GitStream]:
        """Start `git <arguments>` with a pipe for requests, and stop it on leaving the block.

        Each read fails as a `timeout` when no output arrives for `idle_timeout` seconds (the client's default when
        None), so a long answer that keeps arriving never times out. Leaving the block closes git's output, so git
        exits by itself; see `bounded_process.Streaming`. Every program git started is then ended with it, and a
        program that could not be ended fails the block as an `execution` GitError.
        """
        command = _command(arguments, directory)
        try:
            with ExitStack() as stack:
                try:
                    running = stack.enter_context(
                        streaming(
                            located_command(command, kept=FOUND),
                            idle_timeout=self.timeout if idle_timeout is None else idle_timeout,
                            tree=process_tree(hidden_window=True),
                        )
                    )
                except FileNotFoundError as exc:
                    raise GitError(MISSING_GIT, kind="prerequisite") from exc
                except TreeNotEnded:
                    raise
                except OSError as exc:
                    raise GitError(f"Git could not be started: {exc}", kind="execution") from exc
                yield GitStream(running)
        except TreeNotEnded as exc:
            raise GitError(_not_ended(command, exc), kind="execution") from exc

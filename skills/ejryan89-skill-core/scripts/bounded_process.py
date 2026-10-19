"""Run a command within a time limit and never wait on a prompt: the one layer under the git and gh clients.

The command reads no stdin, or only the input its caller gives in full up front, and its environment turns off every
prompt git, Git Credential Manager, and gh would otherwise show, so an expired credential fails at once instead of
waiting for an answer nobody gives. Its environment, that default or one its caller gives, also leaves out git's
repository-selecting variables, such as `GIT_DIR` and `GIT_INDEX_FILE`, which a git hook exports and a shell may pass
on: a command names its repository by the directory it runs in or names, as `git -C` and gh in its working directory
do, and reads that repository, its index, and its objects, never one a variable points at. A program that is not git,
such as Copilot CLI or an MCP server, may run git or read a repository's files itself, so no command keeps them.
`without_repository_variables` is the one builder of such an environment, which a start outside this module uses too.

Output goes to temporary files, not pipes. A command that times out is killed, but a process it started, such as
`git-remote-https` or a credential helper, can outlive it and hold a pipe open, and reading a pipe to its end would
then wait for that process. A file needs no reader, so the call returns when the time is up. Given input is a file
too: the command reads it to its end and then sees stdin closed, and one that reads none of it cannot block. An output
limit bounds what is read back from each file, and names what it cut. A caller whose command may start programs of its
own gives a `ProcessTree`, which ends every one of them still running once the command is done or killed; ending them
is operating-system-specific, so this module only calls it.

`Streaming` runs a command that answers requests while it runs, such as `git cat-file --batch`, with the same
environment unless its caller gives another, which loses the same variables; its stdin is the caller's pipe of
requests, never the terminal. It takes a `ProcessTree` as `run_bounded` does, ended once the command is closed.
"""

from __future__ import annotations

import os
import queue
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol

# Seconds to wait for a killed command to be reported gone; job_tree imports it as its wait for a refused command
# and for an ended tree's processes.
KILL_WAIT_SECONDS = 10.0
# Seconds a stopped streaming command gets to exit by itself before it is killed.
EXIT_WAIT_SECONDS = 60.0
# Seconds a closing streaming session waits, once its command is gone, for the reader to see stdout end.
READER_WAIT_SECONDS = 1.0
CHUNK_BYTES = 64 * 1024
QUEUED_CHUNKS = 16  # how far a streaming command's reader runs ahead of its caller
POLL_SECONDS = 0.1
NON_INTERACTIVE = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GH_PROMPT_DISABLED": "1"}
# git's repository-selecting variables: each points a git command at a repository, work tree, index, object store,
# configuration file, or set of refs other than those of the directory it runs in. They are the list git clears itself
# before it works in another repository (`git rev-parse --local-env-vars`) and GIT_NAMESPACE, but not the command-scope
# settings in that list, GIT_CONFIG_PARAMETERS and GIT_CONFIG_COUNT, which configure git as a user's settings do rather
# than choose a repository.
REPOSITORY_VARIABLES = frozenset(
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
)
# Ends a stream cut at an output limit, so whoever reads it learns that more was written than it holds.
CUT_NOTE = "\n[output cut: {cut} bytes past the first {limit} not kept]\n"


@dataclass(frozen=True)
class Finished:
    returncode: int
    stdout: bytes
    stderr: bytes


class ProcessTree(Protocol):
    """A command and every process it starts, kept together so that ending the tree ends them all."""

    def start_options(self) -> dict[str, Any]:
        """Keyword arguments for subprocess.Popen that start the command as the tree needs."""
        ...

    def hold(self, process: subprocess.Popen[Any]) -> AbstractContextManager[object]:
        """Entered once the command has started; leaving it ends every process of the tree still running."""
        ...


def without_repository_variables(environment: Mapping[str, str]) -> dict[str, str]:
    """`environment` without git's repository-selecting variables, each matched in any case, as Windows reads a name.

    Every command this module starts gets its environment through it, and so does every process a skill's Python, the
    deployer, or a repository tool starts some other way, so none reads a repository a variable points at.
    """
    return {name: value for name, value in environment.items() if name.upper() not in REPOSITORY_VARIABLES}


def non_interactive_environment() -> dict[str, str]:
    """This process's environment with every prompt turned off and git's repository-selecting variables removed."""
    return {**without_repository_variables(os.environ), **NON_INTERACTIVE}


def run_bounded(
    command: Sequence[str],
    timeout: float,
    *,
    stdout: IO[bytes] | None = None,
    stderr: IO[bytes] | None = None,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
    env: Mapping[str, str] | None = None,
    output_limit: int | None = None,
    tree: ProcessTree | None = None,
) -> Finished:
    """Run `command` without a shell, and with no stdin but `input_bytes`, and return its exit status and output.

    With `stdout`, the command writes there and the result's stdout is empty, and `stderr` likewise; given the same
    file, the two streams arrive in it in the order the command wrote them. With `cwd`, it runs in that directory.
    With `input_bytes`, its stdin is exactly those bytes and then closed; the time limit covers reading them. With
    `env`, that is the command's whole environment but git's repository-selecting variables, and no prompt is turned
    off for it; otherwise it is `non_interactive_environment()`. With `output_limit`, at most that many bytes of each
    stream are read back, and a stream that held more ends with CUT_NOTE. With `tree`, the command starts as the tree
    says, and every process it started that stays in the tree is ended once it has finished or been killed.
    Raises OSError when the command cannot start (FileNotFoundError when it does not exist), and
    subprocess.TimeoutExpired, after killing it, when it does not finish within `timeout` seconds; the exception
    carries the output written by then, read back as a result's would be.
    """
    with tempfile.TemporaryFile() as errors, tempfile.TemporaryFile() as output, _stdin(input_bytes) as stdin:
        process = subprocess.Popen(
            list(command),
            stdin=stdin,
            stdout=output if stdout is None else stdout,
            stderr=errors if stderr is None else stderr,
            env=non_interactive_environment() if env is None else without_repository_variables(env),
            cwd=cwd,
            **({} if tree is None else tree.start_options()),
        )
        expired: subprocess.TimeoutExpired | None = None
        with nullcontext() if tree is None else tree.hold(process):
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait(timeout=KILL_WAIT_SECONDS)
                expired = exc
        kept_output, kept_errors = _read_back(output, output_limit), _read_back(errors, output_limit)
        if expired is not None:
            raise subprocess.TimeoutExpired(list(command), timeout, kept_output, kept_errors) from expired
        return Finished(returncode, kept_output, kept_errors)


def _read_back(stream: IO[bytes], limit: int | None) -> bytes:
    """A stream's file from its start, or its first `limit` bytes and CUT_NOTE when it holds more."""
    stream.seek(0)
    if limit is None:
        return stream.read()
    kept = stream.read(limit)
    cut = stream.seek(0, os.SEEK_END) - len(kept)
    return kept + CUT_NOTE.format(cut=cut, limit=limit).encode() if cut > 0 else kept


@contextmanager
def _stdin(input_bytes: bytes | None) -> Iterator[int | IO[bytes]]:
    """No stdin, or a file of `input_bytes` read from its start, which ends where they do."""
    if input_bytes is None:
        yield subprocess.DEVNULL
        return
    with tempfile.TemporaryFile() as given:
        given.write(input_bytes)
        given.seek(0)
        yield given


@contextmanager
def streaming(
    command: Sequence[str],
    *,
    idle_timeout: float,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    exit_wait: float = EXIT_WAIT_SECONDS,
    merge_stderr: bool = False,
    tree: ProcessTree | None = None,
) -> Iterator[Streaming]:
    """Start a command that takes requests on stdin and answers on stdout while it runs, and close it on leaving.

    With `timeout`, the whole session, from starting the command to closing it, lasts at most that many seconds, as
    `Streaming` describes. With `env`, that is the command's whole environment but git's repository-selecting
    variables, and no prompt is turned off for it; otherwise it is `non_interactive_environment()`. Raises OSError
    when the command cannot start (FileNotFoundError when it does not exist). With `merge_stderr`, both output
    streams go through the bounded stdout reader, rather than keeping diagnostics in a file. With `tree`, the command
    starts as the tree says, and every process it started that stays in the tree is ended once it has been closed.
    """
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else errors,
            env=non_interactive_environment() if env is None else without_repository_variables(env),
            cwd=cwd,
            **({} if tree is None else tree.start_options()),
        )
        with _held(process, tree):
            running = Streaming(process, errors, idle_timeout=idle_timeout, exit_wait=exit_wait, timeout=timeout)
            try:
                yield running
            finally:
                running.close()


@contextmanager
def _held(process: subprocess.Popen[bytes], tree: ProcessTree | None) -> Iterator[None]:
    """Hold a streaming command in `tree`, closing its pipes when the tree refuses it, since no `Streaming` will."""
    if tree is None:
        yield
        return
    with ExitStack() as stack:
        try:
            stack.enter_context(tree.hold(process))
        except BaseException:
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    with suppress(OSError):
                        pipe.close()
            raise
        yield


class Streaming:
    """A running command that answers requests on stdout, such as `git cat-file --batch`; see `streaming`.

    A thread reads its stdout ahead of the caller into a short queue, so every wait for output is bounded: a read that
    receives no byte for `idle_timeout` seconds raises subprocess.TimeoutExpired, while output that keeps arriving
    never times out, however large. Killing the command instead would not be enough, because a launcher such as Git
    for Windows' git.exe can be killed while the program it started keeps the pipe open.

    The caller ends its requests by closing `stdin`, from whichever thread writes them. Closing ends them too, for a
    caller that left by an exception before it could, and stops the reader, which closes the command's stdout, so a
    command waiting for requests sees them end and a command still writing fails its next write, and either exits by
    itself; it is killed only if it has not exited `exit_wait` seconds later. A killed launcher's program
    can hold its working directory a moment longer, so ending it this way matters.

    Once the command is gone, its stdout ends at once unless a program it started still holds it, for as long as that
    program runs. Closing therefore waits at most READER_WAIT_SECONDS for the reader, and a reader still waiting then
    closes the pipe itself when that program lets go. Waiting for a killed command to be reported gone is a separate
    allowance, KILL_WAIT_SECONDS, which ends as soon as the kill has taken effect.

    A session `timeout` bounds all of it. Every read and `wait` also raises subprocess.TimeoutExpired once that many
    seconds have passed since the command started, however steadily output arrives. Closing then waits for nothing
    past that moment but a killed command to be reported gone: the command gets `exit_wait` seconds to exit or what is
    left of the session, whichever is less, and the reader is not waited for once the session is over.
    """

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        errors: IO[bytes],
        *,
        idle_timeout: float,
        exit_wait: float,
        timeout: float | None = None,
    ) -> None:
        if process.stdin is None or process.stdout is None:
            process.kill()
            process.wait(timeout=KILL_WAIT_SECONDS)
            raise OSError("the command started without its pipes")
        self.command = process.args
        self.idle_timeout = idle_timeout
        self.exit_wait = exit_wait
        self.timeout = timeout
        self._deadline = None if timeout is None else time.monotonic() + timeout
        self.stdin: IO[bytes] = process.stdin
        self._process = process
        self._stdout: IO[bytes] = process.stdout
        self._errors = errors
        self._chunks: queue.Queue[bytes] = queue.Queue(QUEUED_CHUNKS)
        self._ended = threading.Event()
        self._stopping = threading.Event()
        self._closed = False
        self._buffer = bytearray()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        """Move stdout into the queue until it ends or the caller stops, then close it."""
        try:
            while not self._stopping.is_set():
                # The descriptor, not the buffered pipe, so a read returns what has arrived instead of a full buffer.
                chunk = os.read(self._stdout.fileno(), CHUNK_BYTES)
                if not chunk:
                    break
                while not self._stopping.is_set():
                    try:
                        self._chunks.put(chunk, timeout=POLL_SECONDS)
                        break
                    except queue.Full:
                        continue
        except (OSError, ValueError):
            pass  # the pipe broke or was closed; the caller learns from the exit status
        finally:
            self._ended.set()
            with suppress(OSError):
                self._stdout.close()

    def _within(self, seconds: float) -> float:
        """`seconds`, or what is left of the session when that is less, and never below zero."""
        if self._deadline is None:
            return seconds
        return max(0.0, min(seconds, self._deadline - time.monotonic()))

    def _fill(self) -> bool:
        """Add the next chunk of output to the buffer, or return False at its end."""
        idle_until = time.monotonic() + self.idle_timeout
        while True:
            if self.timeout is not None and self._within(self.timeout) <= 0:
                raise subprocess.TimeoutExpired(self.command, self.timeout)
            try:
                self._buffer += self._chunks.get(timeout=POLL_SECONDS)
                return True
            except queue.Empty:
                if self._ended.is_set() and self._chunks.empty():
                    return False
                if time.monotonic() >= idle_until:
                    raise subprocess.TimeoutExpired(self.command, self.idle_timeout) from None

    def read(self, size: int) -> bytes:
        """The next `size` bytes of output, fewer only at its end."""
        while len(self._buffer) < size and self._fill():
            pass
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def readline(self, limit: int | None = None) -> bytes:
        """The next line of output with its newline, or what is left at its end.

        With `limit`, at most that many bytes of it, leaving the rest of a longer line to the next read, so a line
        with no end in sight is never held whole. Each byte is searched for the newline once, however many reads a
        long line takes to arrive.
        """
        searched = 0
        while (newline := self._buffer.find(b"\n", searched)) < 0 and (limit is None or len(self._buffer) < limit):
            searched = len(self._buffer)
            if not self._fill():
                break
        end = len(self._buffer) if newline < 0 else newline + 1
        if limit is not None:
            end = min(end, limit)
        data = bytes(self._buffer[:end])
        del self._buffer[:end]
        return data

    def wait(self) -> int:
        """The exit status, raising subprocess.TimeoutExpired when the command has not exited within `idle_timeout`
        or by the end of the session."""
        return self._process.wait(timeout=self._within(self.idle_timeout))

    def errors(self) -> bytes:
        """What the command has written to stderr so far."""
        self._errors.seek(0)
        return self._errors.read()

    def close(self) -> None:
        """Stop reading, let the command exit, kill it if it has not exited `exit_wait` seconds later or by the end of
        the session, and give the reader READER_WAIT_SECONDS to finish; closing again does nothing."""
        if self._closed:
            return
        self._closed = True
        self._stopping.set()
        with suppress(OSError, ValueError):
            self.stdin.close()  # a command that has stopped reading may fail the flush of what is still buffered
        try:
            self._process.wait(timeout=self._within(self.exit_wait))
        except subprocess.TimeoutExpired:
            self._process.kill()
            with suppress(subprocess.TimeoutExpired):
                self._process.wait(timeout=KILL_WAIT_SECONDS)
        # Short, and not past the session's end: a program the command started may still hold its stdout, and then the
        # reader closes it when that program lets go.
        self._reader.join(self._within(READER_WAIT_SECONDS))

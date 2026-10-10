"""Run a command within a time limit and never wait on a prompt: the one layer under the git and gh clients.

The command reads no stdin, or only the input its caller gives in full up front, and its environment turns off every
prompt git, Git Credential Manager, and gh would otherwise show, so an expired credential fails at once instead of
waiting for an answer nobody gives.

Output goes to temporary files, not pipes. A command that times out is killed, but a process it started, such as
`git-remote-https` or a credential helper, can outlive it and hold a pipe open, and reading a pipe to its end would
then wait for that process. A file needs no reader, so the call returns when the time is up. Given input is a file
too: the command reads it to its end and then sees stdin closed, and one that reads none of it cannot block.

`Streaming` runs a command that answers requests while it runs, such as `git cat-file --batch`, with the same
environment unless its caller gives another; its stdin is the caller's pipe of requests, never the terminal.
"""

from __future__ import annotations

import os
import queue
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO

# Seconds to wait for a killed command to be reported gone.
KILL_WAIT_SECONDS = 10.0
# Seconds a stopped streaming command gets to exit by itself before it is killed.
EXIT_WAIT_SECONDS = 60.0
CHUNK_BYTES = 64 * 1024
QUEUED_CHUNKS = 16  # how far a streaming command's reader runs ahead of its caller
POLL_SECONDS = 0.1
NON_INTERACTIVE = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GH_PROMPT_DISABLED": "1"}


@dataclass(frozen=True)
class Finished:
    returncode: int
    stdout: bytes
    stderr: bytes


def non_interactive_environment() -> dict[str, str]:
    """This process's environment with every prompt turned off."""
    return {**os.environ, **NON_INTERACTIVE}


def run_bounded(
    command: Sequence[str],
    timeout: float,
    *,
    stdout: IO[bytes] | None = None,
    stderr: IO[bytes] | None = None,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
) -> Finished:
    """Run `command` without a shell, and with no stdin but `input_bytes`, and return its exit status and output.

    With `stdout`, the command writes there and the result's stdout is empty, and `stderr` likewise; given the same
    file, the two streams arrive in it in the order the command wrote them. With `cwd`, it runs in that directory.
    With `input_bytes`, its stdin is exactly those bytes and then closed; the time limit covers reading them.
    Raises OSError when the command cannot start (FileNotFoundError when it does not exist), and
    subprocess.TimeoutExpired, after killing it, when it does not finish within `timeout` seconds.
    """
    with tempfile.TemporaryFile() as errors, tempfile.TemporaryFile() as output, _stdin(input_bytes) as stdin:
        process = subprocess.Popen(
            list(command),
            stdin=stdin,
            stdout=output if stdout is None else stdout,
            stderr=errors if stderr is None else stderr,
            env=non_interactive_environment(),
            cwd=cwd,
        )
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=KILL_WAIT_SECONDS)
            raise
        output.seek(0)
        errors.seek(0)
        return Finished(returncode, output.read(), errors.read())


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
) -> Iterator[Streaming]:
    """Start a command that takes requests on stdin and answers on stdout while it runs, and close it on leaving.

    With `timeout`, the whole session, from starting the command to closing it, lasts at most that many seconds, as
    `Streaming` describes. With `env`, that is the command's whole environment and no prompt is turned off for it;
    otherwise it is this process's environment with every prompt turned off. Raises OSError when the command cannot
    start (FileNotFoundError when it does not exist).
    """
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            env=non_interactive_environment() if env is None else dict(env),
            cwd=cwd,
        )
        running = Streaming(process, errors, idle_timeout=idle_timeout, exit_wait=exit_wait, timeout=timeout)
        try:
            yield running
        finally:
            running.close()


class Streaming:
    """A running command that answers requests on stdout, such as `git cat-file --batch`; see `streaming`.

    A thread reads its stdout ahead of the caller into a short queue, so every wait for output is bounded: a read that
    receives no byte for `idle_timeout` seconds raises subprocess.TimeoutExpired, while output that keeps arriving
    never times out, however large. Killing the command instead would not be enough, because a launcher such as Git
    for Windows' git.exe can be killed while the program it started keeps the pipe open.

    The caller ends its requests by closing `stdin`, from whichever thread writes them. Closing stops the reader,
    which closes the command's stdout, so a command still writing fails its next write and exits by itself; it is
    killed only if it has not exited `exit_wait` seconds later. A killed launcher's program
    can hold its working directory a moment longer, so ending it this way matters.

    A session `timeout` bounds all of it. Every read and `wait` also raises subprocess.TimeoutExpired once that many
    seconds have passed since the command started, however steadily output arrives. Closing then waits for nothing
    past that moment but a killed command to be reported gone, for at most KILL_WAIT_SECONDS: the command gets
    `exit_wait` seconds to exit or what is left of the session, whichever is less, and the reader is not waited for
    once the session is over, since a program the command started can hold its stdout open for as long as it runs.
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
        """Stop reading, let the command exit, and kill it if it has not exited `exit_wait` seconds later or by the end
        of the session."""
        if self._closed:
            return
        self._closed = True
        self._stopping.set()
        try:
            self._process.wait(timeout=self._within(self.exit_wait))
        except subprocess.TimeoutExpired:
            self._process.kill()
            with suppress(subprocess.TimeoutExpired):
                self._process.wait(timeout=KILL_WAIT_SECONDS)
        # Bounded, and not past the session's end: a program the command started may still hold its stdout.
        self._reader.join(self._within(KILL_WAIT_SECONDS))

"""Run a command within a time limit and never wait on a prompt: the one layer under the git and gh clients.

The command reads no stdin, and its environment turns off every prompt git, Git Credential Manager, and gh would
otherwise show, so an expired credential fails at once instead of waiting for an answer nobody gives.

Output goes to temporary files, not pipes. A command that times out is killed, but a process it started, such as
`git-remote-https` or a credential helper, can outlive it and hold a pipe open, and reading a pipe to its end would
then wait for that process. A file needs no reader, so the call returns when the time is up.

`Streaming` runs a command that answers requests while it runs, such as `git cat-file --batch`, with the same
environment; its stdin is the caller's pipe of requests, never the terminal.
"""

from __future__ import annotations

import os
import queue
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
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
    command: Sequence[str], timeout: float, *, stdout: IO[bytes] | None = None, cwd: Path | None = None
) -> Finished:
    """Run `command` without a shell or stdin and return its exit status and output.

    With `stdout`, the command writes there and the result's stdout is empty. With `cwd`, it runs in that directory.
    Raises OSError when the command cannot start (FileNotFoundError when it does not exist), and
    subprocess.TimeoutExpired, after killing it, when it does not finish within `timeout` seconds.
    """
    with tempfile.TemporaryFile() as errors, tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=output if stdout is None else stdout,
            stderr=errors,
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
def streaming(
    command: Sequence[str], *, idle_timeout: float, cwd: Path | None = None, exit_wait: float = EXIT_WAIT_SECONDS
) -> Iterator[Streaming]:
    """Start a command that takes requests on stdin and answers on stdout while it runs, and close it on leaving.

    Raises OSError when the command cannot start (FileNotFoundError when it does not exist).
    """
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            env=non_interactive_environment(),
            cwd=cwd,
        )
        running = Streaming(process, errors, idle_timeout=idle_timeout, exit_wait=exit_wait)
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
    """

    def __init__(
        self, process: subprocess.Popen[bytes], errors: IO[bytes], *, idle_timeout: float, exit_wait: float
    ) -> None:
        if process.stdin is None or process.stdout is None:
            raise OSError("the command started without its pipes")
        self.command = process.args
        self.idle_timeout = idle_timeout
        self.exit_wait = exit_wait
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

    def _fill(self) -> bool:
        """Add the next chunk of output to the buffer, or return False at its end."""
        deadline = time.monotonic() + self.idle_timeout
        while True:
            try:
                self._buffer += self._chunks.get(timeout=POLL_SECONDS)
                return True
            except queue.Empty:
                if self._ended.is_set() and self._chunks.empty():
                    return False
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(self.command, self.idle_timeout) from None

    def read(self, size: int) -> bytes:
        """The next `size` bytes of output, fewer only at its end."""
        while len(self._buffer) < size and self._fill():
            pass
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def readline(self) -> bytes:
        """The next line of output with its newline, or what is left at its end."""
        while b"\n" not in self._buffer and self._fill():
            pass
        end = self._buffer.find(b"\n") + 1 or len(self._buffer)
        data = bytes(self._buffer[:end])
        del self._buffer[:end]
        return data

    def wait(self) -> int:
        """The exit status, raising subprocess.TimeoutExpired when the command has not exited within `idle_timeout`."""
        return self._process.wait(timeout=self.idle_timeout)

    def errors(self) -> bytes:
        """What the command has written to stderr so far."""
        self._errors.seek(0)
        return self._errors.read()

    def close(self) -> None:
        """Stop reading, let the command exit, and kill it if it has not exited `exit_wait` seconds later."""
        if self._closed:
            return
        self._closed = True
        self._stopping.set()
        try:
            self._process.wait(timeout=self.exit_wait)
        except subprocess.TimeoutExpired:
            self._process.kill()
            with suppress(subprocess.TimeoutExpired):
                self._process.wait(timeout=KILL_WAIT_SECONDS)
        self._reader.join(KILL_WAIT_SECONDS)  # bounded: a program the command started may still hold its stdout

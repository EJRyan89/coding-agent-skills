"""Run a command within a time limit and never wait on a prompt: the one layer under the git and gh clients.

The command reads no stdin, and its environment turns off every prompt git, Git Credential Manager, and gh would
otherwise show, so an expired credential fails at once instead of waiting for an answer nobody gives.

Output goes to temporary files, not pipes. A command that times out is killed, but a process it started, such as
`git-remote-https` or a credential helper, can outlive it and hold a pipe open, and reading a pipe to its end would
then wait for that process. A file needs no reader, so the call returns when the time is up.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

# Seconds to wait for a killed command to be reported gone.
KILL_WAIT_SECONDS = 10.0
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

"""Run one entry point as its own program under one environment fault, for test_faults.py.

The suite starts this file with Python and a run file: a JSON object naming the entry point's path, its arguments,
the fault, the path the fault is placed at when it is a file's, the folders the run may write in, the folders it may
not write in even where those hold them, and the record file.
It runs the entry point's `__main__` block as `python <entry point>` would, with the entry point's folder first on
sys.path and its arguments in sys.argv, after it has:

- replaced git's runner, for a git fault: every git command exits 128 with a reason git gives for a broken checkout,
  or does not finish in time, as GitClient's runner reports each, and so does every git the deployer starts through
  its own tool runner when the entry point is deploy.py;
- replaced gh's runner, for a gh fault: every gh command fails with an answer GitHub gives for a request it refuses;
- made every stat of the fault's path raise, and every presence check of it answer False, as Python's own do, for
  that fault;
- installed an audit hook that stops the run, past any handler the entry point has, when it starts a program other
  than git, opens a network connection, or writes, makes, moves, or removes anything outside the folders the run may
  write in or inside one it may not, so a run never reaches the real home, the source checkout, a model, or the
  network;
- noted each time the run opens, lists, or stats the fault's path, or calls the replaced runner, so the suite knows
  the fault was reached.

It writes the record, `{"exit": <code>, "reached": <bool>, "stopped": <reason or null>}`, and exits with the entry
point's code, or STOPPED when the hook stopped the run. The other faults, a file that cannot be read, a directory in
a file's place, and a missing file, are the suite's to place before it starts this file.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import shlex
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# The exit this runner gives a run its hook stopped, which no entry point's exit table names.
STOPPED = 97
GIT_FAILURE = b"fatal: bad config line 1 in file .git/config\n"
GH_FAILURE = b"HTTP 422: Validation Failed (https://api.github.com/graphql)\n"
GIT_EXIT = 128
# The audit events that start a program, and those that write, make, move, or remove a path, by the argument that
# names it.
STARTS = frozenset({"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.startfile"})
WRITES = {
    "os.mkdir": (0,),
    "os.rename": (0, 1),
    "os.remove": (0,),
    "os.rmdir": (0,),
    "os.truncate": (0,),
    "os.symlink": (0, 1),
    "os.link": (0, 1),
    "os.chmod": (0,),
    "os.utime": (0,),
    "shutil.rmtree": (0,),
    "shutil.copyfile": (1,),
    "shutil.copytree": (1,),
    "shutil.move": (1,),
}
READS = frozenset({"open", "os.listdir", "os.scandir"})
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
GIT_NAMES = frozenset({"git", "git.exe"})
# The presence checks of os.path, which answer False where a stat raises.
PRESENCE_CHECKS = ("exists", "lexists", "isfile", "isdir", "islink")


class Stopped(BaseException):
    """A run the hook stopped: a BaseException, so no handler an entry point has for Exception catches it."""


def key(path: Any) -> str | None:
    """A path as the hook compares paths: absolute, its case and separators folded, or None for no path."""
    if isinstance(path, int) or path is None:
        return None
    try:
        text = os.fsdecode(os.fspath(path))
    except TypeError:
        return None
    return os.path.normcase(os.path.normpath(Path(text).absolute()))


def program_of(program: Any, command: Any) -> str:
    """The program a start names: its executable when given, else its command's first word, read from the command
    line string Windows' Popen builds before the audit event as well as from a list."""
    if program:
        return os.fsdecode(program)
    if isinstance(command, (str, bytes)):
        words = shlex.split(os.fsdecode(command), posix=False)
        return words[0].strip('"') if words else ""
    return os.fsdecode(command[0]) if command else ""


def forms(path: Any) -> list[str]:
    """A path's keys as the hook compares them: as given, and resolved, since a Windows temporary folder may be named
    by its short name, which a script that resolves a path names by its long one."""
    given = key(path)
    if given is None:
        return []
    resolved = key(os.path.realpath(given))
    return [given] if resolved in {None, given} else [given, resolved]


def within(path: str, folders: list[str]) -> bool:
    return any(path == folder or path.startswith(folder.rstrip(os.sep) + os.sep) for folder in folders)


class Run:
    """One run's fault, the folders it may write in, and what it has seen."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.fault: str = spec["fault"]
        self.target = key(spec.get("target"))
        self.targets = forms(spec.get("target"))
        # The run's folders, and the null device, which a program it starts takes as its stdin or stdout.
        self.writable = [found for folder in spec["writable"] for found in forms(folder)]
        # The folders no run writes in even where a writable folder holds them: the source checkout, which lies under
        # the temporary folder when the mutation gate runs the suite in a throwaway worktree.
        self.protected = [found for folder in spec["protected"] for found in forms(folder)]
        self.null = key(os.devnull)
        self.allow_git = not self.fault.startswith("git_")
        self.reached = False
        self.stopped: str | None = None

    def stop(self, reason: str) -> None:
        if self.stopped is None:
            self.stopped = reason
        raise Stopped(reason)

    def may_write(self, found: str) -> bool:
        """Whether the run may write at a path as the hook compares it: inside its folders and outside every protected
        one."""
        return within(found, self.writable) and not within(found, self.protected)

    def touches(self, path: object) -> None:
        """Note a run that opens, lists, or stats the fault's path; a stat fault is reached only by a stat, which
        raise_on_stat notes."""
        if self.fault == "stat_raises":
            return
        found = key(path)
        if found is not None and within(found, self.targets):
            self.reached = True

    def hook(self, event: str, arguments: tuple[Any, ...]) -> None:
        if self.stopped is not None and event != "open":
            return
        if event in STARTS:
            program = arguments[0] if event == "subprocess.Popen" else None
            command = arguments[1] if event == "subprocess.Popen" else arguments
            name = Path(program_of(program, command)).name.casefold()
            if not (self.allow_git and name in GIT_NAMES):
                self.stop(f"started a program: {name or event}")
        elif event == "socket.connect":
            self.stop("opened a network connection")
        elif event == "open":
            path, mode, flags = arguments
            self.touches(path)
            writes = (isinstance(mode, str) and any(letter in mode for letter in "wax+")) or (
                mode is None and isinstance(flags, int) and flags & WRITE_FLAGS
            )
            found = key(path)
            if writes and found is not None and found != self.null and not self.may_write(found):
                self.stop(f"wrote outside the run's folders: {found}")
        elif event in READS:
            self.touches(arguments[0] if arguments else None)
            # A missing file is met by listing the folder that would hold it, as a glob or a walk finds files.
            if event != "open" and self.fault == "missing" and self.target is not None:
                listed = key(arguments[0] if arguments and arguments[0] is not None else ".")
                self.reached = self.reached or any(listed == key(Path(each).parent) for each in self.targets)
        elif event in WRITES:
            for index in WRITES[event]:
                found = key(arguments[index]) if index < len(arguments) else None
                if found is not None and not self.may_write(found):
                    self.stop(f"changed a path outside the run's folders ({event}): {found}")


class FaultedStream:
    """A git command that answers requests, as bounded_process.streaming runs one, under a git fault: one that exits
    128 at once with git's reason, giving no output, or one whose every read and wait runs past its time limit."""

    def __init__(self, command: object, timeout: float, times_out: bool) -> None:
        self.command = command
        self.timeout = timeout
        self.times_out = times_out
        self.stdin = io.BytesIO()

    def _end(self) -> None:
        if self.times_out:
            raise subprocess.TimeoutExpired(self.command, self.timeout)  # type: ignore[arg-type]  # as Popen's args

    def read(self, size: int) -> bytes:
        self._end()
        return b""

    def readline(self, limit: int | None = None) -> bytes:
        self._end()
        return b""

    def wait(self) -> int:
        self._end()
        return GIT_EXIT

    def errors(self) -> bytes:
        return b"" if self.times_out else GIT_FAILURE


def replace_git(run: Run, repository: Path) -> None:
    import bounded_process
    import git_client

    def runner(command: object, timeout: float, **_: object) -> bounded_process.Finished:
        run.reached = True
        if run.fault == "git_timeout":
            raise subprocess.TimeoutExpired(str(command), timeout)
        return bounded_process.Finished(GIT_EXIT, b"", GIT_FAILURE)

    @contextlib.contextmanager
    def streaming(command: object, *_: object, **keywords: Any) -> Iterator[FaultedStream]:
        run.reached = True
        yield FaultedStream(command, float(keywords.get("idle_timeout") or 1), run.fault == "git_timeout")

    git_client.run_bounded = runner  # type: ignore[attr-defined]  # the fault's runner, in the name the client calls
    git_client.streaming = streaming  # type: ignore[attr-defined,assignment]  # the fault's stream, which answers as Streaming does
    replace_deployer_git(run, repository)


def replace_deployer_git(run: Run, repository: Path) -> None:
    """Replace the deployer's tool runner for git, which deploy.py and a tool that reads a source through the deployer,
    such as plugin_release, start through it rather than GitClient: the deployer of the repository the entry point
    lies in, imported from that repository's root, as the entry point imports it."""
    if not (repository / "deployer" / "platform_support.py").is_file():
        return
    sys.path.insert(1, str(repository))
    from deployer import platform_support

    real = platform_support.run_tool

    def run_tool(arguments: list[str], *positional: Any, **keywords: Any) -> platform_support.ToolResult:
        if Path(arguments[0]).name.casefold() not in GIT_NAMES:
            return real(arguments, *positional, **keywords)
        run.reached = True
        if run.fault == "git_timeout":
            raise platform_support.ToolTimedOut(arguments[0], keywords.get("timeout") or 1)
        reason = GIT_FAILURE.decode()
        if keywords.get("separate_errors"):
            return platform_support.ToolResult(GIT_EXIT, "", reason)
        return platform_support.ToolResult(GIT_EXIT, reason)

    platform_support.run_tool = run_tool


def replace_gh(run: Run) -> None:
    import bounded_process
    import github_client

    def runner(command: object, timeout: float, **_: object) -> bounded_process.Finished:
        run.reached = True
        return bounded_process.Finished(1, b"", GH_FAILURE)

    github_client.run_bounded = runner  # type: ignore[attr-defined]  # the fault's runner, in the name the client calls


def raise_on_stat(run: Run) -> None:
    real_stat, real_lstat = os.stat, os.lstat

    def faulted(real: Any) -> Any:
        def stat(path: Any, *arguments: Any, **keywords: Any) -> Any:
            if key(path) in run.targets:
                run.reached = True
                raise PermissionError(13, "Permission denied (injected)", os.fsdecode(path))
            return real(path, *arguments, **keywords)

        return stat

    os.stat = faulted(real_stat)
    os.lstat = faulted(real_lstat)
    # The presence checks answer False for a path whose stat raises, as Python's own do, which Path.exists and its
    # siblings call on Python 3.12 and later, so a script that reads presence through them meets the fault as absence.
    for name in PRESENCE_CHECKS:
        setattr(os.path, name, absent(run, getattr(os.path, name)))


def absent(run: Run, real: Any) -> Any:
    def check(path: Any) -> bool:
        if key(path) in run.targets:
            run.reached = True
            return False
        return bool(real(path))

    return check


def note_stats(run: Run) -> None:
    """Note a stat of the fault's path, which no audit event reports, as a reach of the fault."""
    real_stat, real_lstat = os.stat, os.lstat

    def noting(real: Any) -> Any:
        def stat(path: Any, *arguments: Any, **keywords: Any) -> Any:
            run.touches(path)
            return real(path, *arguments, **keywords)

        return stat

    os.stat = noting(real_stat)
    os.lstat = noting(real_lstat)
    # Python 3.12 and later answer these without os.stat on Windows.
    for name in PRESENCE_CHECKS:
        setattr(os.path, name, noting(getattr(os.path, name)))


def main() -> int:
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    run = Run(spec)
    entry = Path(spec["entry"])
    sys.path[:0] = [str(entry.parent), *spec["path"]]
    if run.fault.startswith("git_"):
        replace_git(run, Path(spec["repository"]))
    elif run.fault.startswith("gh_"):
        replace_gh(run)
    elif run.fault == "stat_raises":
        raise_on_stat(run)
    else:
        note_stats(run)
    # A run waits on nothing: a retry's backoff returns at once.
    time.sleep = lambda seconds: None
    socket.setdefaulttimeout(1)
    sys.argv = [str(entry), *spec["arguments"]]
    sys.addaudithook(run.hook)
    code: object = 0
    try:
        runpy.run_path(str(entry), run_name="__main__")
    except SystemExit as exit_:
        code = exit_.code
    except Stopped:
        code = STOPPED
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
    number = code if isinstance(code, int) else (0 if code is None else 1)
    record = {"exit": number, "reached": run.reached, "stopped": run.stopped}
    run.writable.append(key(spec["record"]) or "")
    Path(spec["record"]).write_text(json.dumps(record), encoding="utf-8")
    return number


if __name__ == "__main__":
    sys.exit(main())

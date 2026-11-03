"""The git client every skill script that runs git shares.

One runner, bounded and non-interactive (see `bounded_process`), and one failure classifier, so a failure means the
same thing in every skill and no git command waits on a credential prompt. The runner and `stream` start git without
its repository-selecting variables (`bounded_process.REPOSITORY_VARIABLES`), so a call reads the repository its
`directory` names, or the one it runs in, whatever `GIT_DIR` or `GIT_INDEX_FILE` the shell or a git hook exported. Tests
inject the runner.

A git exit status is often an answer, such as `merge-base --is-ancestor` saying no, so `run` returns every result
that git produced and raises only when git could not run or finish; `output` raises on a nonzero exit as well.
Where absence is a normal answer, a path query (`rev_parse_paths`), a ref lookup (`commit_of`), a file at a commit
(`file_at`), and any `--quiet` command (`quiet_output`) give three: what git found, None for what git reports as
absent, and a GitError with git's reason for every other failure, so a checkout or a ref git cannot read is never
taken for one that is not there.
stdout is decoded with surrogateescape, so `stdout.encode("utf-8", "surrogateescape")` is exactly what git printed;
stderr only feeds messages, so a byte that is not UTF-8 becomes U+FFFD there.

`input_bytes` gives a command such as `fast-import` its whole stdin up front; it then sees stdin closed,
and the time limit and the failure classification apply as without it. Tests inject `input_runner` for that form: a
client given a `runner` alone refuses a call with input rather than run real git under a test.

`stream` runs a git command that answers requests while it runs, such as `cat-file --batch`, with the same
environment; there the time limit bounds each wait for output rather than the whole command.

An `isolated` client is for git run against content it did not write, such as `grep --no-index` over a review's
snapshot. Each of its commands names the null device as its repository (`NO_REPOSITORY`), so git discovers none, in the
folder or above it, and reads no repository's configuration, and runs with `bounded_process.isolated_git_environment`,
so no system, global, or command-scope setting from the environment shapes what it prints. Only its arguments and the
files it reads decide its output. Its caller asks for it; a client that is not isolated refuses a `grep`, `diff`, or
`difftool` any of whose arguments spells `--no-index` or an abbreviation git accepts for it (`reads_no_repository`),
wherever it stands, since that form reads files rather than a repository, so a caller that forgets to ask fails instead
of running git unisolated. The refusal reads no option grammar: a search pattern is never among the arguments but the
client's own `pattern`, which it gives git as `-e <pattern>` right after the subcommand, so a pattern that reads
`--no-index` is searched for, and a caller that needs one inside its own arguments uses an isolated client. The
subcommand is read by name, so a plain client also refuses a `-c` or `--config-env` among git's own options that sets
anything in a section that can define an alias (`ALIAS_SECTIONS`): an alias, or a file included from the command line,
could make another name run `grep` or `diff`. For the same reason it refuses an `--exec-path=<folder>`, which makes git
run a `git-<name>` program from that folder under a name the client cannot read. It cannot see an alias that git's
configuration files or the environment's `GIT_CONFIG_PARAMETERS` and `GIT_CONFIG_COUNT` define, nor tell a folder of
content it did not write from a repository, so a command that reads the surrounding repository by design, such as
`ls-files --others`, `grep --untracked`, or `check-ignore --no-index`, runs on a plain client, and so does a `diff` or
`difftool` run outside any repository, which git reads as `--no-index` without the option; a caller over such content
asks for an isolated client. Of git's commands, `grep`, `diff`, `difftool`, and `check-ignore` take `--no-index`. An
isolated client refuses a test's runner, which could not start git with the isolated environment.

Both hold git in a process tree (`job_tree`), because git starts programs of its own: Git for Windows' launcher starts
the real git, a fetch starts its transport and pack programs, and a command may run a credential helper, a hook, or an
alias. So a call that times out, and a stream once closed, leave none of them running, and a tree that cannot be
ended fails the call as an `execution` error saying they may still run.

Both start git at the absolute path ejryan89-skill-core's `executable_locator` finds for it, never by its bare name,
which process creation on Windows looks up in the working directory before PATH, so a `git.exe` in the folder a skill
runs from is never the git it runs. The path is looked up once for each value of the variables the locator reads.

A transient failure of a command that only fetches from a remote, a connection dropped or a host briefly not found, is
retried a few times with short waits inside the call's own time limit, and `run` returns the last answer. Every other
command runs once, and `push` is never repeated. Tests inject the sleeper and the clock.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal, Protocol, TypeVar

from bounded_process import Streaming, isolated_git_environment, run_bounded, streaming
from executable_locator import Found, located_command
from flat_text import flat_text
from job_tree import TreeNotEnded, process_tree

T = TypeVar("T")

# Each class of failure a GitError names, as `GitError` describes them.
GitErrorKind = Literal["git", "prerequisite", "execution", "timeout", "not_repository"]

DEFAULT_TIMEOUT_SECONDS = 300.0
NOT_REPOSITORY_MARKERS = ("not a git repository",)
MISSING_GIT = "Git executable 'git' was not found; install Git first"
# The repository an isolated client's commands name: the null device is never a git directory, so git takes the
# command as run outside any repository and neither discovers one nor reads a repository's configuration.
NO_REPOSITORY = f"--git-dir={os.devnull}"
# The subcommands that, given `--no-index`, read the files they are pointed at rather than a repository, which only an
# isolated client runs. `check-ignore --no-index` still reads its repository's ignore rules, so it is not one.
ISOLATED_SUBCOMMANDS = frozenset({"grep", "diff", "difftool"})
# git's option that runs its `git-<name>` programs from another folder, given as `--exec-path=<folder>`; alone, it
# prints the folder and runs nothing.
EXEC_PATH_OPTION = "--exec-path="

# Every spelling git may take for --no-index: the option and each abbreviation of it from `--no-i` upward. git refuses
# the shorter ones it finds ambiguous, such as `--no-i` beside grep's `--no-ignore-case`, so refusing them too costs
# nothing, and git takes no abbreviation shorter than `--no-`. A plain client refuses each wherever it stands.
NO_INDEX_SPELLINGS = frozenset("--no-index"[:length] for length in range(len("--no-i"), len("--no-index") + 1))
# git's options that set a configuration value for one command, each spelled `<name>=<value>` or
# `<name>=<variable>`, and the sections, case-folded as git reads them, whose settings can define an alias: the alias
# itself, or a file `include.path` or an `includeIf.<condition>.path` includes, whose own settings may hold one.
CONFIGURATION_OPTIONS = frozenset({"-c", "--config-env"})
ALIAS_SECTIONS = frozenset({"alias", "include", "includeif"})
# A full object name, SHA-1 or SHA-256, as `rev-parse --verify` prints it.
OBJECT_NAME = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# The subcommands retried after a transient failure: each only reads from the remote, and a fetch cut off after it
# updated some refs leaves them where a second fetch of the same refspecs puts them, so a repeat is safe. `push` may
# have landed before its answer was lost, and `clone` runs through no skill's client, so neither is here.
RETRIED_SUBCOMMANDS = frozenset({"fetch", "ls-remote"})
# git's options that take their value as the next argument, so the subcommand is never read from a value: every one
# git.c reads that way.
GLOBAL_VALUE_OPTIONS = frozenset(
    {
        "-c",
        "-C",
        "--attr-source",
        "--config-env",
        "--git-dir",
        "--namespace",
        "--shallow-file",
        "--super-prefix",
        "--work-tree",
    }
)
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

    def __init__(self, message: str, *, kind: GitErrorKind = "git", returncode: int | None = None) -> None:
        super().__init__(message)
        self.kind: GitErrorKind = kind
        self.returncode = returncode  # git's exit code, when git ran and failed

    @property
    def answered(self) -> bool:
        """Whether git ran and finished, so that the error is its answer about what it was asked, not that git is
        missing, could not start, or did not finish in time, which says nothing of it."""
        return self.kind not in {"prerequisite", "execution", "timeout"}


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
    "isolated_runner": "the isolated GitClient's real runner, which passes the git commands GitClient builds to "
    "subprocess_runner with the isolated environment; the policy reads every GitClient as running git",
}


# The git the runner and `stream` found, kept so a run of many git commands looks it up once.
FOUND: Found = {}


def subprocess_runner(
    command: Sequence[str], timeout: float, *, input_bytes: bytes | None = None, env: Mapping[str, str] | None = None
) -> GitResult:
    """Run a command through the bounded, non-interactive layer, its program at the path the locator finds,
    classifying why it could not run or finish.

    With `input_bytes`, the command's stdin is exactly those bytes and then closed. With `env`, that is its whole
    environment, as `bounded_process.run_bounded` takes it. Every program the command started is ended with it, once
    it has finished or been killed.
    """
    tree = process_tree(hidden_window=True)
    try:
        if input_bytes is None:
            finished = run_bounded(located_command(command, kept=FOUND), timeout, env=env, tree=tree)
        else:
            finished = run_bounded(
                located_command(command, kept=FOUND), timeout, input_bytes=input_bytes, env=env, tree=tree
            )
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


def isolated_runner(command: Sequence[str], timeout: float, *, input_bytes: bytes | None = None) -> GitResult:
    """`subprocess_runner` with `bounded_process.isolated_git_environment`, the real runner of an isolated client."""
    return subprocess_runner(command, timeout, input_bytes=input_bytes, env=isolated_git_environment())


def _not_ended(command: Sequence[str], error: TreeNotEnded) -> str:
    """The message of a git command whose programs could not be ended."""
    return f"{' '.join(str(part) for part in command)} left programs it started that could not be ended: {error}"


def classify_failure(stderr: str) -> GitErrorKind:
    """The class of a git command that exited nonzero, from its stderr."""
    lowered = stderr.casefold()
    if any(marker in lowered for marker in NOT_REPOSITORY_MARKERS):
        return "not_repository"
    return "git"


def subcommand_index(arguments: Sequence[str]) -> int | None:
    """Where the git subcommand `arguments` run stands in them, after any of git's own options, or None when they
    name none."""
    index = 0
    while index < len(arguments):
        if arguments[index] in GLOBAL_VALUE_OPTIONS:
            index += 2
        elif not arguments[index].startswith("-"):
            return index
        else:
            index += 1
    return None


def subcommand(arguments: Sequence[str]) -> str | None:
    """The git subcommand `arguments` run, after any of git's own options, or None when they name none."""
    index = subcommand_index(arguments)
    return None if index is None else arguments[index]


def output_lines(output: str) -> list[str]:
    """The lines git wrote, split on "\\n" alone, the one byte that ends a line of its output: never on the other
    characters str.splitlines() breaks on, such as U+0085 and U+2028, which a ref name or a path can hold. A last
    "\\n" ends the last line rather than starting an empty one."""
    return output.removesuffix("\n").split("\n") if output else []


def command_name(arguments: Sequence[str]) -> str:
    """`git` and the subcommand `arguments` run, past git's own options, the name a failure is labelled with; `git`
    alone when they name none."""
    return " ".join(["git", *filter(None, [subcommand(arguments)])])


def failure_message(arguments: Sequence[str], result: GitResult) -> str:
    """What a failed `git <arguments>` says: git's stderr, or, when it printed none, the subcommand and the exit
    code."""
    return result.stderr.strip() or f"{command_name(arguments)} failed with exit code {result.returncode}"


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
    return TRANSIENT_BASE_SECONDS * 2.0**retry


def _gave_up(result: GitResult, retries: int, why: str = "") -> GitResult:
    """`result` with the retries spent on it named on a line after git's own stderr."""
    stderr = result.stderr if result.stderr.endswith("\n") or not result.stderr else result.stderr + "\n"
    note = f"gave up after {retries} {'retry' if retries == 1 else 'retries'}{why}"
    return GitResult(result.returncode, result.stdout, f"{stderr}({note})\n")


def reads_no_repository(arguments: Sequence[str]) -> bool:
    """Whether `arguments` run a subcommand in `ISOLATED_SUBCOMMANDS` with any later argument in `NO_INDEX_SPELLINGS`,
    wherever it stands: before or after a pattern, a revision, or `--`. No option grammar is read, so no spelling git
    takes for the option is passed over."""
    index = subcommand_index(arguments)
    return (
        index is not None
        and arguments[index] in ISOLATED_SUBCOMMANDS
        and any(argument in NO_INDEX_SPELLINGS for argument in arguments[index + 1 :])
    )


def configured_sections(arguments: Sequence[str]) -> list[str]:
    """The section, case-folded, of each setting a `-c` or `--config-env` among git's own options in `arguments` makes,
    in either spelling: the value as the next word or attached after `=`."""
    end = subcommand_index(arguments)
    index, sections = 0, []
    while index < (len(arguments) if end is None else end):
        argument = arguments[index]
        option, attached, value = argument.partition("=")
        if argument in CONFIGURATION_OPTIONS and index + 1 < len(arguments):
            sections.append(re.split(r"[.=]", arguments[index + 1], maxsplit=1)[0].casefold())
        elif attached and option == "--config-env":
            sections.append(re.split(r"[.=]", value, maxsplit=1)[0].casefold())
        index += 2 if argument in GLOBAL_VALUE_OPTIONS else 1
    return sections


def defines_alias(arguments: Sequence[str]) -> bool:
    """Whether `arguments` set, among git's own options, anything in a section that can define an alias
    (`ALIAS_SECTIONS`), or name another folder of git programs with `--exec-path=`."""
    end = subcommand_index(arguments)
    own = arguments if end is None else arguments[:end]
    return any(section in ALIAS_SECTIONS for section in configured_sections(arguments)) or any(
        argument.startswith(EXEC_PATH_OPTION) for argument in own
    )


def _command(arguments: Sequence[str], directory: str | Path | None, *, isolated: bool) -> list[str]:
    return [
        "git",
        *([NO_REPOSITORY] if isolated else []),
        *(["-C", str(directory)] if directory is not None else []),
        *arguments,
    ]


class GitStream:
    """A running git command's pipes: requests go to `stdin`, and each read waits at most the idle timeout."""

    def __init__(self, running: Streaming) -> None:
        self._running = running
        self.stdin: IO[bytes] = running.stdin

    def read(self, size: int) -> bytes:
        """The next `size` bytes of output, fewer only at its end."""
        return self._bounded(lambda: self._running.read(size), "gave no output for")

    def readline(self, limit: int | None = None) -> bytes:
        """The next line of output with its newline, or what is left at its end; with `limit`, at most that many
        bytes of it, leaving the rest of a longer line to the next read."""
        return self._bounded(lambda: self._running.readline(limit), "gave no output for")

    def wait(self) -> int:
        """The exit status once git has exited, or a `timeout` GitError when it has not exited within the idle
        timeout."""
        return self._bounded(self._running.wait, "had not exited after")

    def stderr(self) -> str:
        """What git has written to stderr so far, with each byte that is not UTF-8 as U+FFFD."""
        return self._running.errors().decode("utf-8", "replace")

    def _bounded(self, call: Callable[[], T], waited: str) -> T:
        """`call`'s answer, or a `timeout` GitError naming what git `waited` through the time limit doing."""
        try:
            return call()
        except subprocess.TimeoutExpired as exc:
            command = " ".join(str(part) for part in exc.cmd) if isinstance(exc.cmd, list) else str(exc.cmd)
            raise GitError(f"{command} {waited} {exc.timeout:g} seconds", kind="timeout") from exc


def _refuse_input(command: Sequence[str], timeout: float, *, input_bytes: bytes) -> GitResult:
    raise TypeError("this GitClient was given a runner but no input_runner, so it takes no input_bytes")


class GitClient:
    """Runs git within a time limit, with no stdin but input given up front and no prompt, and classifies its
    failures.

    `input_runner` runs the calls given `input_bytes`. It defaults to the real runner only while `runner` is the real
    runner too, so a client built on a test's runner never reaches real git through a call with input. `sleeper`
    makes the waits between retries, and `clock` measures what is left of a call's time limit.

    With `isolated`, every command names `NO_REPOSITORY`, and the real runners and `stream` start it with
    `bounded_process.isolated_git_environment`, as the module's docstring describes; such a client takes only the real
    runners, and refuses another with a TypeError. Without it, a command `reads_no_repository` names is refused with a
    ValueError before git starts, and so is one `defines_alias` names. `run`, `output`, and `stream` take a search's
    `pattern` apart from its arguments and give it to git as `-e <pattern>` right after the subcommand, where the
    refusal never reads it.
    """

    def __init__(
        self,
        runner: Runner = subprocess_runner,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        input_runner: InputRunner | None = None,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        isolated: bool = False,
    ) -> None:
        if isolated and (runner is not subprocess_runner or input_runner not in {None, subprocess_runner}):
            raise TypeError(
                "an isolated GitClient starts git itself, with the isolated environment, so it takes no runner"
            )
        self.isolated = isolated
        self.runner: Runner = isolated_runner if isolated else runner
        if input_runner is None:
            input_runner = subprocess_runner if runner is subprocess_runner else _refuse_input
        self.input_runner: InputRunner = isolated_runner if isolated else input_runner
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
        pattern: str | None = None,
    ) -> GitResult:
        """Run `git <arguments>`, in `directory` when given (as `git -C`), and return its result whatever its exit.

        With `input_bytes`, git's stdin is exactly those bytes and then closed, through `input_runner`.
        Raises GitError when git is missing, cannot start, or does not finish within `timeout` (the client's default
        when None). A subcommand in `RETRIED_SUBCOMMANDS` that fails transiently runs again within that limit: the
        result is the last attempt's, and one that outlasts the retries names them on its stderr.
        """
        command = self._command(arguments, directory, pattern)
        limit = self.timeout if timeout is None else timeout

        def attempt(seconds: float) -> GitResult:
            if input_bytes is None:
                return self.runner(command, seconds)
            return self.input_runner(command, seconds, input_bytes=input_bytes)

        if subcommand(arguments) not in RETRIED_SUBCOMMANDS:
            return attempt(limit)
        return self._retried(attempt, limit)

    def _command(self, arguments: Sequence[str], directory: str | Path | None, pattern: str | None) -> list[str]:
        """The command line of `git <arguments>` in `directory` on this client, with `-e <pattern>` right after the
        subcommand when given, or a ValueError for a command only an isolated client runs or a pattern with no
        subcommand to follow."""
        if not self.isolated and reads_no_repository(arguments):
            raise ValueError(
                "git grep or diff --no-index reads files no repository holds; run it on GitClient(isolated=True)"
            )
        if not self.isolated and defines_alias(arguments):
            raise ValueError(
                "an alias or a folder of git programs set on git's command line can run grep or diff --no-index "
                "unseen; "
                "run it on GitClient(isolated=True)"
            )
        given = list(arguments)
        if pattern is not None:
            index = subcommand_index(given)
            if index is None:
                raise ValueError("a pattern needs a subcommand to follow")
            given[index + 1 : index + 1] = ["-e", pattern]
        return _command(given, directory, isolated=self.isolated)

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
        pattern: str | None = None,
    ) -> str:
        """Run `git <arguments>` as `run` does and return its stdout; a nonzero exit raises a classified GitError."""
        result = self.run(arguments, directory=directory, timeout=timeout, input_bytes=input_bytes, pattern=pattern)
        if result.returncode != 0:
            raise GitError(
                failure_message(arguments, result), kind=classify_failure(result.stderr), returncode=result.returncode
            )
        return result.stdout

    def names(
        self, arguments: Sequence[str], *, directory: str | Path | None = None, timeout: float | None = None
    ) -> list[str]:
        """The fields a name-listing command prints, such as `diff --name-only`, `diff --name-status`, `ls-files`,
        `ls-tree`, or `status --porcelain`, each exactly as git holds it.

        Runs the command as `output` does with `-z` after its subcommand, so git neither quotes a name holding a
        non-ASCII byte or a newline nor ends a field on a byte a name can hold, and splits on NUL, the one byte git
        refuses in a name. Fields come in the order git prints them, in the command's own shape: `diff --name-status`
        gives a status as a field of its own, before its path or a rename's two, while `status --porcelain` gives its
        two-letter status, a space, and the path in one field, followed by a rename's original path in another.
        """
        index = subcommand_index(arguments)
        position = len(arguments) if index is None else index + 1
        command = list(arguments) if "-z" in arguments else [*arguments[:position], "-z", *arguments[position:]]
        listed = self.output(command, directory=directory, timeout=timeout)
        return listed.removesuffix("\0").split("\0") if listed else []

    def rev_parse_paths(self, options: Sequence[str], *, directory: str | Path) -> list[Path] | None:
        """The absolute path `git rev-parse` names for `directory` for each of `options`, in order, such as
        `--show-toplevel` (the root of its checkout or worktree) or `--git-common-dir` (the Git directory every
        worktree of it shares).

        None only when git answers that `directory` lies in no repository. Every other failure, such as a checkout
        whose ownership git doubts or whose configuration it cannot parse, raises GitError with git's reason, and so
        does an answer that is not one path for each option.
        """
        try:
            output = self.output(["rev-parse", "--path-format=absolute", *options], directory=directory)
        except GitError as exc:
            if exc.kind == "not_repository":
                return None
            raise
        paths = output_lines(output)
        if len(paths) != len(options) or not all(paths):
            raise GitError(
                f"git rev-parse answered {flat_text(output)!r} for {' '.join(options)} in {directory}, "
                "not one path for each"
            )
        return [Path(path) for path in paths]

    def rev_parse_path(self, option: str, *, directory: str | Path) -> Path | None:
        """The absolute path `git rev-parse <option>` names for `directory`; see `rev_parse_paths`."""
        paths = self.rev_parse_paths([option], directory=directory)
        return None if paths is None else paths[0]

    def quiet_output(
        self,
        arguments: Sequence[str],
        *,
        directory: str | Path | None = None,
        timeout: float | None = None,
        input_bytes: bytes | None = None,
    ) -> str | None:
        """The stdout of a git command run with `--quiet`, such as `rev-parse --verify --quiet`, or None when it exits
        1 and prints nothing on stderr, which such a command, `grep` and `merge-base --is-ancestor` among them, does
        only when what it reads is not there. `input_bytes` is its stdin, as for `run`.

        Any other failure is git's, such as a broken ref it warns about or a configuration it cannot parse, and raises
        a classified GitError with git's reason.
        """
        result = self.run(arguments, directory=directory, timeout=timeout, input_bytes=input_bytes)
        if result.returncode == 0:
            return result.stdout
        if result.returncode == 1 and not result.stderr.strip():
            return None
        raise GitError(
            failure_message(arguments, result), kind=classify_failure(result.stderr), returncode=result.returncode
        )

    def commit_of(self, reference: str, *, directory: str | Path) -> str | None:
        """The full name of the commit `reference` names in `directory`'s repository, or None when git answers that it
        names none; see `quiet_output`. An answer that is not one object name raises GitError."""
        return self._object_of(f"{reference}^{{commit}}", directory)

    def file_at(self, commit: str, path: str, *, directory: str | Path) -> bytes | None:
        """The bytes of the file at `path` in `commit`, or None when git answers that `commit` holds nothing there or
        is no commit it knows; see `quiet_output`. A path that names a folder, and any other failure, raise
        GitError."""
        name = self._object_of(f"{commit}:{path}", directory)
        if name is None:
            return None
        arguments = ["cat-file", "blob", name]
        result = self.run(arguments, directory=directory)
        if result.returncode != 0:
            message = f"{commit}:{path}: {failure_message(arguments, result)}"
            raise GitError(message, kind=classify_failure(result.stderr), returncode=result.returncode)
        return result.output_bytes()

    def _object_of(self, name: str, directory: str | Path) -> str | None:
        """The object name `git rev-parse --verify --quiet` gives `name`, or None when it names nothing."""
        output = self.quiet_output(["rev-parse", "--verify", "--quiet", "--end-of-options", name], directory=directory)
        if output is None:
            return None
        found = output.strip()
        if not OBJECT_NAME.fullmatch(found):
            raise GitError(f"git rev-parse --verify answered {flat_text(found)!r} for {name}, not one object name")
        return found

    @contextmanager
    def stream(
        self,
        arguments: Sequence[str],
        *,
        directory: str | Path | None = None,
        idle_timeout: float | None = None,
        pattern: str | None = None,
    ) -> Iterator[GitStream]:
        """Start `git <arguments>` with a pipe for requests, and stop it on leaving the block; `pattern` as `run`
        takes it.

        Each read fails as a `timeout` when no output arrives for `idle_timeout` seconds (the client's default when
        None), so a long answer that keeps arriving never times out. Leaving the block closes git's output, so git
        exits by itself; see `bounded_process.Streaming`. Every program git started is then ended with it, and a
        program that could not be ended fails the block as an `execution` GitError.
        """
        command = self._command(arguments, directory, pattern)
        try:
            with ExitStack() as stack:
                try:
                    running = stack.enter_context(
                        streaming(
                            located_command(command, kept=FOUND),
                            idle_timeout=self.timeout if idle_timeout is None else idle_timeout,
                            env=isolated_git_environment() if self.isolated else None,
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

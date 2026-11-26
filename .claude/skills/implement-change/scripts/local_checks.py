"""Run, before a push, what continuous integration's Validation check would most likely fail on for the change.

    python -B .claude/skills/implement-change/scripts/local_checks.py --base origin/<branch>
        [--title-file <file>] [--body-file <file>]

It fetches the base, then runs the validation runner, `tests/run_validation.py`, from the top of the repository:

    pre-push       `--pre-push` against the base, which the runner reads as `origin/<GITHUB_BASE_REF>`. For a narrow
                   change it is the `--changed` selection: the policy checks, the static checks of each kind of file
                   changed (for Python the format, lint, and every type check), and each suite the change reaches
                   whole, the suites `CHANGE_REACHES` in `tests/validation/job_selection.py` adds among them. For a
                   wide one, which the runner names and says why, it is the policy checks, those static checks, and
                   the suites that read a changed file directly, on capped workers, and continuous integration runs
                   the rest. With --title-file and --body-file, the title and body checks read those files
    windows-types  the type-check jobs that read a Python file changed since the base, committed, uncommitted, or
                   untracked, named as `pull_request.py type-checks` names them, or every type check for a change that
                   reaches them all, with ejryan89-skill-core's `type_check_roots.PLATFORM_VARIABLE` set to win32, so
                   mypy checks the code as Windows' legs do, which a check on macOS or Linux otherwise never does

Both run whatever the other's result, so one run names every failure. Each run writes to this process's standard output
as it goes, between its RUN line and the line of its result, and starts in a process tree of its own, which ends
whatever it started once it is done. The runner selects what runs and decides when a change is wide; this script reads
nothing of its selection.

Every line of its own is one fact:

    BASE <remote>/<branch> <sha>    the base's tip after the fetch, which the changed files are read from
    RUN <name> <command>            a run starting, and its command from the top of the repository, as Bash reads it
    SKIPPED <name> <why>            a run not owed: no changed file is one a type check reads
    PASSED <name>                   the run passed
    NOT_PASSED <name> <status>      the run failed with that exit status, or `timeout` when it ran past RUN_SECONDS
    READY                           every run passed
    NOT_READY <count>               how many runs did not pass
    FAILED <reason>                 the script could not finish

Exit status:
  0  every run passed: READY
  1  a run did not pass, NOT_READY, or a last line FAILED <reason>
  2  a usage error, such as a base on a remote other than origin
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))

from bounded_process import non_interactive_environment, run_bounded
from console import run_main
from flat_text import flat_text, single_line
from git_client import GitClient
from job_tree import process_tree
from pull_request import (
    VALIDATION_COMMAND,
    Base,
    PullRequestError,
    base_argument,
    changed_files,
    configured_paths,
    fetched_base,
    run_command,
    top_of,
)
from type_check_roots import (
    JOB_PREFIX,
    MYPY_PATH_PREFIX,
    PLATFORM_VARIABLE,
    every_job_reasons,
    file_type_check_jobs,
    imported_folders,
    strict_type_check_areas,
)

# The one remote the runner compares with, as `origin/<GITHUB_BASE_REF>`.
RUNNER_REMOTE = "origin"
# The variable that names the base branch to the runner, as continuous integration sets it on a pull request.
BASE_VARIABLE = "GITHUB_BASE_REF"
# The platform whose type checks only Windows' legs run, as mypy's --platform and sys.platform spell it.
WINDOWS = "win32"
# Seconds a run may take before it is ended: longer than a whole local run of the runner takes.
RUN_SECONDS = 4 * 60 * 60.0


@dataclass(frozen=True)
class Check:
    """One run of the runner: its name on the lines, its arguments after the runner's command, and the variables it is
    given beyond this process's environment."""

    name: str
    arguments: tuple[str, ...]
    variables: tuple[tuple[str, str], ...] = ()


def origin_base(value: str) -> Base:
    """--base: a branch of origin, which is the remote the runner reads the base from."""
    base = base_argument(value)
    if base.remote != RUNNER_REMOTE:
        raise argparse.ArgumentTypeError(
            f"{value!r} is on {base.remote}; the validation runner compares with {RUNNER_REMOTE}, so give "
            f"{RUNNER_REMOTE}/<branch>"
        )
    return base


def pre_push_check(title_file: Path | None, body_file: Path | None) -> Check:
    """The runner's --pre-push selection, given the title and body files when there are any, each from the folder this
    script was started in, since the runner starts from the top of the repository."""
    files = [
        *(["--pr-title", str(title_file.absolute())] if title_file else []),
        *(["--pr-body", str(body_file.absolute())] if body_file else []),
    ]
    return Check("pre-push", ("--pre-push", *files))


def windows_check(top: Path, changed: Sequence[str]) -> Check | None:
    """The type-check jobs the changed files reach, as `pull_request.py type-checks` names them, run as on Windows, or
    None when no changed file is one a type check reads."""
    imported = configured_paths(
        top, imported_folders, f"[tool.mypy] mypy_path is not a list of paths starting {MYPY_PATH_PREFIX}"
    )
    if every_job_reasons(changed, imported):
        jobs = [JOB_PREFIX]
    else:
        strict = configured_paths(top, strict_type_check_areas, "[tool.validation] mypy-strict is not a list of paths")
        jobs = file_type_check_jobs(top, changed, strict)
        if not jobs:
            return None
        # Refuses a job name no single line or single quotes can hold, as the printed command would.
        run_command(jobs)
    selection = tuple(argument for job in jobs for argument in ("-k", job))
    return Check("windows-types", selection, ((PLATFORM_VARIABLE, WINDOWS),))


def shown(check: Check) -> str:
    """The run's command as Bash reads it from the top of the repository, its variables before it."""
    variables = [f"{name}={shlex.quote(value)}" for name, value in check.variables]
    arguments = [argument if argument.startswith("-") else shell_word(argument) for argument in check.arguments]
    return " ".join([*variables, VALIDATION_COMMAND, *arguments])


def shell_word(argument: str) -> str:
    """A job name in single quotes, as `pull_request.py type-checks` prints one, and a path in double quotes."""
    return f"'{argument}'" if argument.startswith(JOB_PREFIX) else f'"{argument}"'


def output_stream() -> IO[bytes]:
    """Where each run writes, its output and its errors together, in the order it wrote them."""
    return sys.stdout.buffer


def run_check(check: Check, top: Path, branch: str) -> str:
    """Run `check` from `top`, its output on this process's, and return its result line."""
    environment = {name: value for name, value in non_interactive_environment().items() if name != PLATFORM_VARIABLE}
    environment.update({BASE_VARIABLE: branch, **dict(check.variables)})
    print(single_line(f"RUN {check.name} {shown(check)}"), flush=True)
    stream = output_stream()
    try:
        finished = run_bounded(
            [sys.executable, *VALIDATION_COMMAND.split()[1:], *check.arguments],
            RUN_SECONDS,
            stdout=stream,
            stderr=stream,
            cwd=top,
            env=environment,
            tree=process_tree(),
        )
    except subprocess.TimeoutExpired:
        return f"NOT_PASSED {check.name} timeout"
    except OSError as exc:
        raise PullRequestError(f"cannot start the validation runner: {flat_text(str(exc))}") from exc
    finally:
        stream.flush()
    return f"PASSED {check.name}" if finished.returncode == 0 else f"NOT_PASSED {check.name} {finished.returncode}"


def local_checks(git: GitClient, repository: Path, options: argparse.Namespace) -> int:
    """Fetch the base, run each check, print a line for each, and return the exit status."""
    base: Base = options.base
    top = top_of(git, repository)
    commit = fetched_base(git, repository, base)
    print(single_line(f"BASE {base} {commit}"), flush=True)
    changed = changed_files(git, top, commit)
    windows = windows_check(top, changed)
    checks = [pre_push_check(options.title_file, options.body_file), *([windows] if windows else [])]
    results = []
    for check in checks:
        results.append(run_check(check, top, base.branch))
        print(single_line(results[-1]), flush=True)
        if check.name == "pre-push" and windows is None:
            print("SKIPPED windows-types no changed file is one a type check reads", flush=True)
    failed = [result for result in results if not result.startswith("PASSED ")]
    print(single_line(f"NOT_READY {len(failed)}" if failed else "READY"), flush=True)
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run what continuous integration's Validation check would fail on for the change, before a push."
    )
    parser.add_argument("--repository", type=Path, default=Path(), help="the checkout (default: here)")
    parser.add_argument("--base", type=origin_base, required=True, help="a branch of origin, such as origin/main")
    parser.add_argument("--title-file", type=Path, help="the pull request's title file, for the title check")
    parser.add_argument("--body-file", type=Path, help="the pull request's body file, for the body checks")
    return parser


def main(arguments: Sequence[str] | None = None, git: GitClient | None = None) -> int:
    options = build_parser().parse_args(arguments)
    try:
        return local_checks(git or GitClient(), options.repository, options)
    except PullRequestError as exc:
        print(f"FAILED {flat_text(str(exc))}", flush=True)
        return 1


if __name__ == "__main__":
    run_main(main)

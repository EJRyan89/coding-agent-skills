"""The job pool: a job, the process it runs, the pool that runs every job beside the policy checks, and the step
summary."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, TypeVar

# ejryan89-skill-core's bounded runner is imported before validation_support, which puts ejryan89-skill-core on sys.path
# too, so this module states the path itself.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from bounded_process import run_bounded
from validation_support import REPOSITORY_ROOT

from deployer import platform_support, tools

COMMAND_TIMEOUT_SECONDS = 20 * 60

# Held for each line or block written while the pool runs, so a suite's line never lands inside the policy report.
OUTPUT_LOCK = threading.Lock()
# What the foreground work beside the pool returns.
T = TypeVar("T")


# Bash and PowerShell suites cannot be split, so they start before any shard.
UNSPLIT_SUITE_WEIGHT = 1_000


def _text_block(text: str) -> list[str]:
    """A text fence under a summary's list item."""
    return ["", "  ```text", *(f"  {line}" for line in text.rstrip("\n").split("\n")), "  ```", ""]


def step_summary(
    python: tuple[int, ...],
    versions: dict[str, tuple[int, ...] | None],
    mode: str,
    policies: int,
    jobs: int,
    seconds: float,
    failed: list[str],
    details: Mapping[str, str] | None = None,
    shard: str | None = None,
    retried: Mapping[str, str] | None = None,
) -> str:
    """The Markdown GitHub Actions shows on the run's summary page: the report of each job that raised or whose
    program was ended before its first line of output, each job started once more after such an ending with how that
    ended, and the shard, as `<index>/<count>`, of a run split across runners."""
    found = [f"Python {tools.format_version(python)}"] + [
        f"{label} {tools.format_version(version) if version else 'unknown'}" for label, version in versions.items()
    ]
    result = "**validation FAILED**" if failed else "**validation passed**"
    lines = [
        f"## Repository validation, shard {shard}" if shard else "## Repository validation",
        "",
        f"- {', '.join(found)}",
        f"- {mode}",
        f"- {policies} policy checks and {jobs} suite jobs in {seconds:.0f}s: {result}",
    ]
    for label in failed:
        lines.append(f"- Failed: `{label}`")
        detail = (details or {}).get(label)
        if detail:
            lines += _text_block(detail)
    for label, note in (retried or {}).items():
        lines += [f"- Retried once: `{label}`", *_text_block(note)]
    return "\n".join(lines) + "\n"


def append_step_summary(environment: Mapping[str, str], text: str) -> None:
    path = environment.get("GITHUB_STEP_SUMMARY")
    if path:
        with Path(path).open("a", encoding="utf-8") as summary:
            summary.write(text)


class Terminated(Exception):
    """A program a job started was ended with a termination status before its first line of output: an exception or
    another process ended it, so nothing it runs reached a verdict, and no check failed."""

    def __init__(self, status: str, arguments: Sequence[str]) -> None:
        self.status = status
        self.command = " ".join(arguments)
        super().__init__(
            f"{Path(arguments[0]).name} was ended with status {status} before its first line of output: {self.command}"
        )


def refuse_termination(arguments: Sequence[str], returncode: int, output: str | bytes) -> None:
    """Raise Terminated when a program was ended with a termination status before it wrote anything."""
    status = platform_support.termination_status(returncode)
    if status is not None and not output:
        raise Terminated(status, arguments)


class TimedOut(Exception):
    """A program a job started did not finish within COMMAND_TIMEOUT_SECONDS, so it was ended with every process it
    started: no check failed, and its report names the whole command and what it wrote before it was ended."""


def run_process(arguments: list[str], environment: dict[str, str] | None = None, cwd: Path = REPOSITORY_ROOT) -> None:
    """Run a job's program and raise AssertionError, with its output, when it fails, Terminated when it was ended
    before its first line of output, and TimedOut when it runs past COMMAND_TIMEOUT_SECONDS.

    It runs through ejryan89-skill-core's bounded runner in this platform's process tree, so the time limit, and the end
    of a run that passes or fails, ends every process the program started that stays in its tree, such as a git,
    PowerShell, or Python a suite left holding its temporary home, and its output goes to real files, since MSYS tools
    can reject inherited anonymous pipes with a spurious "failed to set file descriptor text/binary mode" error.
    """
    merged = dict(os.environ)
    if environment:
        merged.update(environment)
    command = " ".join(arguments)
    try:
        finished = run_bounded(
            arguments, COMMAND_TIMEOUT_SECONDS, cwd=cwd, env=merged, tree=platform_support.process_tree()
        )
    except subprocess.TimeoutExpired as exc:
        raise TimedOut(
            f"Command timed out after {COMMAND_TIMEOUT_SECONDS}s and was ended with every process it started: "
            f"{command}\n{_text(exc.stdout)}\n{_text(exc.stderr)}"
        ) from None
    if finished.returncode != 0:
        refuse_termination(arguments, finished.returncode, finished.stdout + finished.stderr)
        status = platform_support.termination_status(finished.returncode)
        ended = f" (status {status}, ended after its output began)" if status else ""
        raise AssertionError(
            f"Command failed with exit code {finished.returncode}{ended}: {command}\n"
            f"{_text(finished.stdout)}\n{_text(finished.stderr)}"
        )


def _text(written: bytes | str | None) -> str:
    """What a program wrote, as text: UTF-8, with a byte it is not written as its replacement character."""
    return written.decode("utf-8", "replace") if isinstance(written, bytes) else written or ""


# The most workers a run starts unless VALIDATION_JOBS says otherwise. Most jobs wait on Git and other processes, so
# on a 24-CPU machine 24 workers took a full run from a median of 105 s at 16 to 96 s (2026-10-08, three runs each).
# Past one per CPU it depends on the platform: on CI's 4-vCPU runners 6 workers cut the Linux legs' summed seconds
# from a median of 449 s to 357 s and left Windows' unchanged (2026-10-23, six runs at 4 and three at 6), so only
# the Linux legs set VALIDATION_JOBS.
MAXIMUM_WORKERS = 24


def worker_count(environment: Mapping[str, str] = os.environ, cpus: int | None = None) -> int:
    """The workers a run starts: VALIDATION_JOBS, at least one, or else one per CPU up to MAXIMUM_WORKERS. Raises
    ValueError, naming the variable, when VALIDATION_JOBS is set to anything but a whole number."""
    configured = environment.get("VALIDATION_JOBS")
    if configured:
        try:
            return max(1, int(configured))
        except ValueError:
            raise ValueError(f"VALIDATION_JOBS must be a whole number of workers, not {configured!r}") from None
    return max(1, min(MAXIMUM_WORKERS, cpus or os.cpu_count() or 1))


@dataclass(frozen=True)
class Job:
    label: str
    name: str  # what -k matches: the suite's path, without the shard
    weight: float  # a rough cost: the pool starts the heaviest jobs first
    run: Callable[[], None]


@dataclass(frozen=True)
class Failure:
    """A job that failed: a check's message, the traceback of anything else it raised, the command and output of a
    program that ran past its time limit, or, when every attempt's program was ended before its first line of output,
    each of those endings."""

    job: Job
    report: str
    raised: bool = False
    terminated: bool = False
    timed_out: bool = False


@dataclass(frozen=True)
class Retry:
    """A job started once more because its program was ended before its first line of output, and how that ended."""

    job: Job
    ending: str
    result: str  # "passed", "failed", or "was ended again"


class PoolResult(NamedTuple):
    """The failed jobs and the retried ones, each in label order."""

    failures: list[Failure]
    retries: list[Retry]


# The times a job is started again when its program was ended with a termination status before its first line of
# output: an exception or another process ended it, not a check, and on the hosted runners a rerun of such a leg passed.
# One, so a job ended again is reported as failed with both endings rather than hidden behind further attempts. A job
# that wrote anything, or that ran and failed, is never started again.
TERMINATION_RETRIES = 1


def _attempt(job: Job) -> Failure | Terminated | None:
    """Run a job once: None when it passes, the ending when its program was ended before any output, else a failure."""
    try:
        job.run()
    except Terminated as exc:
        return exc
    except TimedOut as exc:
        return Failure(job, str(exc), timed_out=True)
    except AssertionError as exc:
        return Failure(job, str(exc))
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - any other exception, a job's sys.exit included, fails only the job
        return Failure(job, "".join(traceback.format_exception(exc)), raised=True)
    return None


def _settle(job: Job, outcome: Failure | Terminated | None, endings: list[Terminated]) -> Failure | None:
    """The failure, if any, a job's last attempt leaves, naming the endings that started it again."""
    if isinstance(outcome, Terminated):
        attempts = [*endings, outcome]
        return Failure(
            job,
            f"Ended before its first line of output on each of {len(attempts)} attempts, a termination rather than a "
            "failed check, so nothing in it reached a verdict:\n" + "\n".join(f"  {ending}" for ending in attempts),
            terminated=True,
        )
    if outcome is not None and endings:
        return Failure(
            job, f"Started once more after: {endings[0]}\nThe retry failed:\n{outcome.report}", outcome.raised
        )
    return outcome


def _run_job(job: Job, verbose: bool) -> tuple[Failure | None, Retry | None]:
    """Run a job, starting it again up to TERMINATION_RETRIES times while its program is ended before any output."""
    started = time.perf_counter()
    endings: list[Terminated] = []
    outcome = _attempt(job)
    while isinstance(outcome, Terminated) and len(endings) < TERMINATION_RETRIES:
        endings.append(outcome)
        with OUTPUT_LOCK:
            print(
                f"RETRY {time.perf_counter() - started:6.1f}s {job.label}: {outcome}; a termination, not a failed "
                "check, so it starts once more",
                flush=True,
            )
        outcome = _attempt(job)
    failure = _settle(job, outcome, endings)
    retry = None
    if endings:
        result = "passed" if failure is None else "was ended again" if failure.terminated else "failed"
        retry = Retry(job, str(endings[0]), result)
    if verbose or failure is not None or endings:
        note = f" (retried after status {endings[0].status})" if endings else ""
        with OUTPUT_LOCK:
            print(f"{'FAIL' if failure else 'ok'} {time.perf_counter() - started:6.1f}s {job.label}{note}", flush=True)
    return failure, retry


def run_jobs(jobs: list[Job], verbose: bool, workers: int) -> PoolResult:
    """Run every job in one pool, heaviest first, and return the failures and the retries in label order.

    A job fails by raising: an AssertionError is a check that failed and reports its message, and any other exception,
    such as an OSError, reports its traceback, so one broken job never stops the pool or the summary. A job that raises
    Terminated is started once more, and fails only when it is ended again or its retry fails.
    """
    failures: list[Failure] = []
    retries: list[Retry] = []

    def run(job: Job) -> None:
        failure, retry = _run_job(job, verbose)
        with OUTPUT_LOCK:
            if failure is not None:
                failures.append(failure)
            if retry is not None:
                retries.append(retry)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(run, sorted(jobs, key=lambda job: -job.weight)))
    return PoolResult(
        sorted(failures, key=lambda failure: failure.job.label), sorted(retries, key=lambda retry: retry.job.label)
    )


def run_beside(foreground: Callable[[], T], jobs: list[Job], verbose: bool, workers: int) -> tuple[T, PoolResult]:
    """Start the pool on its own thread, run `foreground` in this one while the pool works, and return both results.

    The pool is always waited for: an exception from `foreground` propagates only once every job has finished.
    """
    with ThreadPoolExecutor(max_workers=1) as background:
        pool = background.submit(run_jobs, jobs, verbose, workers)
        result = foreground()
        return result, pool.result()

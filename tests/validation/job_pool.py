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
from types import MappingProxyType
from typing import NamedTuple

# ejryan89-skill-core's bounded runner is imported before validation_support, which puts ejryan89-skill-core on sys.path
# too, so this module states the path itself.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from bounded_process import run_bounded
from credentials import without_credentials
from text_lines import program_lines
from validation_notes import NOTE_MARKER
from validation_support import REPOSITORY_ROOT

from deployer import platform_support, tools

COMMAND_TIMEOUT_SECONDS = 20 * 60

# Held for each line or block written while the pool runs, so a suite's line never lands inside the policy report.
OUTPUT_LOCK = threading.Lock()
# What the foreground work beside the pool returns.


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
    notes: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """The Markdown GitHub Actions shows on the run's summary page: the report of each job that raised or whose
    program was ended before its first line of output, each job started once more after such an ending with how that
    ended, each note a job's program kept, and the shard, as `<index>/<count>`, of a run split across runners."""
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
    for label, kept in (notes or {}).items():
        lines += [f"- Noted: `{label}`", *_text_block("\n".join(kept))]
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
    before its first line of output, and TimedOut when it runs past COMMAND_TIMEOUT_SECONDS. The program's environment
    is this process's with `environment` over it, less every credential variable, so no program a job runs is
    given a token.

    It runs through ejryan89-skill-core's bounded runner in this platform's process tree, so the time limit, and the end
    of a run that passes or fails, ends every process the program started that stays in its tree, such as a git,
    PowerShell, or Python a suite left holding its temporary home, and its output goes to real files, since MSYS tools
    can reject inherited anonymous pipes with a spurious "failed to set file descriptor text/binary mode" error.

    What a program that passes writes after NOTE_MARKER on a line, on either stream, is kept as a note of the job the
    pool runs on this thread; the rest of its output is dropped.
    """
    merged = without_credentials({**os.environ, **(environment or {})})
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
    if finished.returncode == 0:
        _keep_notes(_text(finished.stdout), _text(finished.stderr))
        return
    refuse_termination(arguments, finished.returncode, finished.stdout + finished.stderr)
    status = platform_support.termination_status(finished.returncode)
    ended = f" (status {status}, ended after its output began)" if status else ""
    raise AssertionError(
        f"Command failed with exit code {finished.returncode}{ended}: {command}\n"
        f"{_text(finished.stdout)}\n{_text(finished.stderr)}"
    )


# The notes of the job each pool thread runs: _run_job starts the list, and run_process, which a job calls on the
# thread that runs it, adds each note its passing program wrote.
_JOB_NOTES = threading.local()


def program_notes(*outputs: str) -> list[str]:
    """What follows NOTE_MARKER on each line of `outputs` that holds it, to the line's end and without trailing
    whitespace, in order, leaving out a marker with nothing after it. The marker need not start its line: pytest writes
    a test's progress before what the test prints, on the same line."""
    notes = (line.partition(NOTE_MARKER)[2].rstrip() for output in outputs for line in program_lines(output))
    return [note for note in notes if note]


def _keep_notes(*outputs: str) -> None:
    """Add each note in a passing program's `outputs` to the notes of the job this thread runs, if it runs one."""
    kept: list[str] | None = getattr(_JOB_NOTES, "kept", None)
    if kept is not None:
        kept.extend(program_notes(*outputs))


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
    """The failed jobs and the retried ones, each in label order, and the notes each passing job's programs kept, by
    the job's label in label order."""

    failures: list[Failure]
    retries: list[Retry]
    notes: Mapping[str, list[str]] = MappingProxyType({})


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
            job,
            f"Started once more after: {endings[0]}\nThe retry failed:\n{outcome.report}",
            raised=outcome.raised,
            timed_out=outcome.timed_out,
        )
    return outcome


def _attempts(job: Job, started: float) -> tuple[Failure | Terminated | None, list[Terminated]]:
    """Run a job, starting it again up to TERMINATION_RETRIES times while its program is ended before any output, and
    return its last attempt's outcome and the endings that started it again."""
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
    return outcome, endings


def _run_job(job: Job, verbose: bool) -> tuple[Failure | None, Retry | None, list[str]]:
    """Run a job as _attempts does and return its failure, its retry, and, when it passes, the notes its programs kept.
    A passing job with notes prints its line, as a verbose run prints every job's, and a NOTE line for each note."""
    started = time.perf_counter()
    _JOB_NOTES.kept = []
    try:
        outcome, endings = _attempts(job, started)
        notes: list[str] = _JOB_NOTES.kept
    finally:
        del _JOB_NOTES.kept
    failure = _settle(job, outcome, endings)
    if failure is not None:
        notes = []
    retry = None
    if endings:
        result = "passed" if failure is None else "was ended again" if failure.terminated else "failed"
        retry = Retry(job, str(endings[0]), result)
    if verbose or failure is not None or endings or notes:
        note = f" (retried after status {endings[0].status})" if endings else ""
        with OUTPUT_LOCK:
            print(f"{'FAIL' if failure else 'ok'} {time.perf_counter() - started:6.1f}s {job.label}{note}", flush=True)
            for kept in notes:
                print(f"NOTE {job.label}: {kept}", flush=True)
    return failure, retry, notes


def run_jobs(jobs: list[Job], verbose: bool, workers: int) -> PoolResult:
    """Run every job in one pool, heaviest first, and return the failures, the retries, and the passing jobs' notes in
    label order.

    A job fails by raising: an AssertionError is a check that failed and reports its message, and any other exception,
    such as an OSError, reports its traceback, so one broken job never stops the pool or the summary. A job that raises
    Terminated is started once more, and fails only when it is ended again or its retry fails. A failed job keeps no
    notes: its report holds its programs' whole output.
    """
    failures: list[Failure] = []
    retries: list[Retry] = []
    notes: dict[str, list[str]] = {}

    def run(job: Job) -> None:
        failure, retry, kept = _run_job(job, verbose)
        with OUTPUT_LOCK:
            if failure is not None:
                failures.append(failure)
            if retry is not None:
                retries.append(retry)
            if kept:
                notes[job.label] = kept

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(run, sorted(jobs, key=lambda job: -job.weight)))
    return PoolResult(
        sorted(failures, key=lambda failure: failure.job.label),
        sorted(retries, key=lambda retry: retry.job.label),
        dict(sorted(notes.items())),
    )


def run_beside[T](foreground: Callable[[], T], jobs: list[Job], verbose: bool, workers: int) -> tuple[T, PoolResult]:
    """Start the pool on its own thread, run `foreground` in this one while the pool works, and return both results.

    The pool is always waited for: an exception from `foreground` propagates only once every job has finished.
    """
    with ThreadPoolExecutor(max_workers=1) as background:
        pool = background.submit(run_jobs, jobs, verbose, workers)
        result = foreground()
        return result, pool.result()

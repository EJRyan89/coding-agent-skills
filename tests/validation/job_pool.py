"""The job pool: a job, the process it runs, the pool that runs every job, and the step summary."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from validation_support import REPOSITORY_ROOT

from deployer import tools

COMMAND_TIMEOUT_SECONDS = 20 * 60


# Bash and PowerShell suites cannot be split, so they start before any shard.
UNSPLIT_SUITE_WEIGHT = 1_000


def step_summary(
    python: tuple[int, ...],
    versions: dict[str, tuple[int, ...] | None],
    mode: str,
    policies: int,
    jobs: int,
    seconds: float,
    failed: list[str],
) -> str:
    """The Markdown GitHub Actions shows on the run's summary page."""
    found = [f"Python {tools.format_version(python)}"] + [
        f"{label} {tools.format_version(version) if version else 'unknown'}" for label, version in versions.items()
    ]
    result = "**validation FAILED**" if failed else "**validation passed**"
    lines = [
        "## Repository validation",
        "",
        f"- {', '.join(found)}",
        f"- {mode}",
        f"- {policies} policy checks and {jobs} suite jobs in {seconds:.0f}s: {result}",
        *(f"- Failed: `{label}`" for label in failed),
    ]
    return "\n".join(lines) + "\n"


def append_step_summary(environment: Mapping[str, str], text: str) -> None:
    path = environment.get("GITHUB_STEP_SUMMARY")
    if path:
        with Path(path).open("a", encoding="utf-8") as summary:
            summary.write(text)


def run_process(arguments: list[str], environment: dict[str, str] | None = None, cwd: Path = REPOSITORY_ROOT) -> None:
    merged = dict(os.environ)
    if environment:
        merged.update(environment)
    # Redirect to real files: MSYS tools can reject inherited anonymous pipes with a
    # spurious "failed to set file descriptor text/binary mode" error.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            completed = subprocess.run(
                arguments,
                cwd=cwd,
                env=merged,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(f"Command timed out after {COMMAND_TIMEOUT_SECONDS}s: {arguments[0]}") from exc
        if completed.returncode != 0:
            stdout.seek(0)
            stderr.seek(0)
            output = stdout.read().decode("utf-8", "replace")
            error = stderr.read().decode("utf-8", "replace")
            raise AssertionError(
                f"Command failed with exit code {completed.returncode}: {' '.join(arguments)}\n{output}\n{error}"
            )


def worker_count() -> int:
    configured = os.environ.get("VALIDATION_JOBS")
    if configured:
        return max(1, int(configured))
    return max(1, min(16, os.cpu_count() or 1))


@dataclass(frozen=True)
class Job:
    label: str
    name: str  # what -k matches: the suite's path, without the shard
    weight: float  # a rough cost: the pool starts the heaviest jobs first
    run: Callable[[], None]


def run_jobs(jobs: list[Job], verbose: bool, workers: int) -> list[tuple[Job, AssertionError]]:
    """Run every job in one pool, heaviest first, and return the failures in label order."""
    failures: list[tuple[Job, AssertionError]] = []
    lock = threading.Lock()

    def attempt(job: Job) -> None:
        started = time.perf_counter()
        try:
            job.run()
            error = None
        except AssertionError as exc:
            error = exc
        with lock:
            if error is not None:
                failures.append((job, error))
            if verbose or error is not None:
                print(f"{'FAIL' if error else 'ok'} {time.perf_counter() - started:6.1f}s {job.label}", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(attempt, sorted(jobs, key=lambda job: -job.weight)))
    return sorted(failures, key=lambda failure: failure[0].label)

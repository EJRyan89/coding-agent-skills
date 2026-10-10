"""Which jobs a run selects: every job, or on the documentation path the fence checks and the suites that name a
changed file, and -k's name patterns; and which of them one leg of a run split across CI runners takes.
"""

from __future__ import annotations

import functools
import json
import subprocess
import unittest
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from job_pool import UNSPLIT_SUITE_WEIGHT, Job
from python_checks import static_format_check, static_lint_check, type_check_jobs
from shell_targets import static_markdown_shell_check, static_powershell_check, static_shell_check
from suite_discovery import regression_suites, suite_jobs
from validation_support import REPOSITORY_ROOT

from tests.run_shard import deal, flatten

POWERSHELL_JOB = "static PowerShell checks (PSScriptAnalyzer on .ps1 files and PowerShell fences)"
MARKDOWN_SHELL_JOB = "static Markdown shell checks (ShellCheck on Bash fences outside skills/ and tests/)"
# What each job took on CI's runners, by job name (a suite's path, summed over its shards, or a static check's label),
# from one completed validate run's logs. tools/record_job_seconds.py writes it, and the release procedure refreshes it,
# so the legs are dealt by measured seconds that change only when the file does, never by a run's live timings.
JOB_SECONDS = Path(__file__).with_name("job_seconds.json")
# A job the table does not name yet, such as a new suite, is dealt by its count instead: its typical tests, or for an
# unsplit job UNSPLIT_JOB_COST of them, at TYPICAL_TEST_SECONDS each. An unsplit job's weight only starts it before
# any shard in the pool, so dealt at that weight it would fill a leg alone. On a four-core runner the static checks
# took 2 to 10 seconds each, and the Python suites 0.495 seconds a counted test (2026-10-10).
UNSPLIT_JOB_COST = 10
TYPICAL_TEST_SECONDS = 0.5


def markdown_jobs(root: Path) -> list[Job]:
    """The static jobs that read fences in Markdown, which a documentation-only change can reach."""
    return [
        Job(POWERSHELL_JOB, POWERSHELL_JOB, UNSPLIT_SUITE_WEIGHT, functools.partial(static_powershell_check, root)),
        Job(
            MARKDOWN_SHELL_JOB,
            MARKDOWN_SHELL_JOB,
            UNSPLIT_SUITE_WEIGHT,
            functools.partial(static_markdown_shell_check, root),
        ),
    ]


def documentation_jobs(root: Path, paths: list[str], suites: list[Path]) -> list[Job]:
    """What a documentation-only change runs: the fence checks when Markdown changed, and the suites naming a file."""
    fences = markdown_jobs(root) if any(path.casefold().endswith(".md") for path in paths) else []
    return [*fences, *suite_jobs(suites_naming(paths, suites))]


def all_jobs() -> list[Job]:
    shell = "static shell checks (bash -n and ShellCheck on skill scripts)"
    python_format = "static format check (ruff format --check)"
    python_lint = "static lint check (ruff check)"
    return [
        Job(shell, shell, UNSPLIT_SUITE_WEIGHT, static_shell_check),
        *markdown_jobs(REPOSITORY_ROOT),
        Job(python_format, python_format, UNSPLIT_SUITE_WEIGHT, static_format_check),
        Job(python_lint, python_lint, UNSPLIT_SUITE_WEIGHT, static_lint_check),
        *type_check_jobs(),
        *suite_jobs(regression_suites()),
    ]


def changed_paths(root: Path, base: str) -> list[str] | None:
    """Files changed since the merge base with `base`, plus uncommitted and untracked ones; None when Git cannot tell.

    Renames count as a deletion and an addition, so moving a file out of skills/ is still a change to skills/.
    """
    paths: set[str] = set()
    for command in (
        ["diff", "--name-only", "--no-renames", f"{base}...HEAD"],
        ["diff", "--name-only", "--no-renames", "HEAD"],
        ["ls-files", "--others", "--exclude-standard"],
    ):
        result = subprocess.run(["git", "-C", str(root), *command], capture_output=True, text=True, encoding="utf-8")
        if result.returncode != 0:
            return None
        paths.update(line for line in result.stdout.splitlines() if line)
    return sorted(paths)


def suites_naming(paths: list[str], suites: list[Path]) -> list[Path]:
    """The suites whose source names a changed file, by path or by file name."""
    names = {name for path in paths for name in (path, path.rsplit("/", 1)[-1])}
    return [suite for suite in suites if any(name in suite.read_text(encoding="utf-8") for name in names)]


def name_patterns(patterns: list[str]) -> list[str]:
    """unittest's -k rule: a pattern without a wildcard matches as a substring."""
    return [pattern if any(character in pattern for character in "*?[") else f"*{pattern}*" for pattern in patterns]


def job_seconds(path: Path = JOB_SECONDS) -> dict[str, float]:
    """The measured seconds by job name: a JSON object of the run they came from and the seconds, each above zero."""
    try:
        table = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{path.name} cannot be read as JSON: {error}") from error
    if not (isinstance(table, dict) and set(table) == {"run", "seconds"} and isinstance(table["seconds"], dict)):
        raise ValueError(f'{path.name} must be an object of "run" and "seconds", the seconds an object by job name')
    seconds = table["seconds"]
    for name, value in seconds.items():
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise ValueError(f"{path.name} records {name} at {value!r}; a job's seconds are a number above zero")
    return {name: float(value) for name, value in seconds.items()}


@functools.cache
def recorded_job_seconds() -> dict[str, float]:
    return job_seconds()


def leg_cost(job: Job, shards: int, seconds: Mapping[str, float]) -> float:
    """What a job counts for, dealt to a leg, in seconds: its suite's measured seconds shared among the suite's
    `shards`, or without a measurement its count at TYPICAL_TEST_SECONDS a test."""
    if job.name in seconds:
        return seconds[job.name] / shards
    return (UNSPLIT_JOB_COST if job.weight == UNSPLIT_SUITE_WEIGHT else job.weight) * TYPICAL_TEST_SECONDS


def leg_jobs(jobs: list[Job], index: int, count: int, seconds: Mapping[str, float] | None = None) -> list[Job]:
    """The jobs leg `index` (from 0) of `count` runs, dealt as run_shard.py deals a suite's tests: the heaviest first
    onto the leg with the least so far, by label on a tie, each weighed by leg_cost from `seconds` (JOB_SECONDS when
    None). The legs run every job exactly once between them, and each leg's share depends only on the jobs and the
    table, never on their order, so legs that select alike deal alike."""
    measured = recorded_job_seconds() if seconds is None else seconds
    shards = Counter(job.name for job in jobs)
    costs = {job.label: leg_cost(job, shards[job.name], measured) for job in jobs}
    legs = deal([job.label for job in jobs], count, costs)
    return [job for job in jobs if legs[job.label] == index]


def leg_policies(policies: unittest.TestSuite, index: int, count: int) -> unittest.TestSuite:
    """The policy checks leg `index` (from 0) of `count` runs: one each to the legs in turn, by test ID. They run
    beside the pool rather than in it, so they are dealt apart from the jobs."""
    checks = list(flatten(policies))
    legs = deal([check.id() for check in checks], count, {})
    return unittest.TestSuite(check for check in checks if legs[check.id()] == index)


def stale_job_seconds_problems(seconds: Mapping[str, float], names: set[str]) -> list[str]:
    """Report a measured entry that names no job a full run selects, such as a suite since renamed or removed."""
    return [
        f"{JOB_SECONDS.name} records {name}, which no validation job runs; rerun tools/record_job_seconds.py on a "
        "completed main run, or remove the entry"
        for name in sorted(set(seconds) - names)
    ]


class JobSelectionPolicies(unittest.TestCase):
    def test_measured_job_seconds_name_jobs_that_run(self) -> None:
        self.assertEqual([], stale_job_seconds_problems(job_seconds(), {job.name for job in all_jobs()}))

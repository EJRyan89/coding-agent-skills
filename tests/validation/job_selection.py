"""Which jobs a run selects: every job, or on the documentation path the fence checks and the suites that name a
changed file, and -k's name patterns; and which of them one leg of a run split across CI runners takes.
"""

from __future__ import annotations

import functools
import subprocess
import unittest
from pathlib import Path

from job_pool import UNSPLIT_SUITE_WEIGHT, Job
from python_checks import static_format_check, static_lint_check, type_check_jobs
from shell_targets import static_markdown_shell_check, static_powershell_check, static_shell_check
from suite_discovery import regression_suites, suite_jobs
from validation_support import REPOSITORY_ROOT

from tests.run_shard import deal, flatten

POWERSHELL_JOB = "static PowerShell checks (PSScriptAnalyzer on .ps1 files and PowerShell fences)"
MARKDOWN_SHELL_JOB = "static Markdown shell checks (ShellCheck on Bash fences outside skills/ and tests/)"
# An unsplit job's weight only starts it before any shard in the pool, so dealt to a leg it counts as this many typical
# tests instead. On a four-core runner the static checks took 2 to 10 seconds each and a typical test about half a
# second (2026-10-10); dealt at their weight, the jobs left one leg a quarter of another's work.
UNSPLIT_JOB_COST = 10


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


def leg_cost(job: Job) -> float:
    """What a job counts for, dealt to a leg: a suite or shard's cost in typical tests, or UNSPLIT_JOB_COST."""
    return UNSPLIT_JOB_COST if job.weight == UNSPLIT_SUITE_WEIGHT else job.weight


def leg_jobs(jobs: list[Job], index: int, count: int) -> list[Job]:
    """The jobs leg `index` (from 0) of `count` runs, dealt as run_shard.py deals a suite's tests: the heaviest first
    onto the leg with the least so far, by label on a tie. The legs run every job exactly once between them, and each
    leg's share depends only on the jobs, never on their order, so legs that select alike deal alike."""
    legs = deal([job.label for job in jobs], count, {job.label: leg_cost(job) for job in jobs})
    return [job for job in jobs if legs[job.label] == index]


def leg_policies(policies: unittest.TestSuite, index: int, count: int) -> unittest.TestSuite:
    """The policy checks leg `index` (from 0) of `count` runs: one each to the legs in turn, by test ID. They run
    beside the pool rather than in it, so they are dealt apart from the jobs."""
    checks = list(flatten(policies))
    legs = deal([check.id() for check in checks], count, {})
    return unittest.TestSuite(check for check in checks if legs[check.id()] == index)

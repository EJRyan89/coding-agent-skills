"""Which jobs a run selects: every job, or on the documentation path the fence checks and the suites that name a
changed file, and -k's name patterns.
"""

from __future__ import annotations

import functools
import subprocess
from pathlib import Path

from job_pool import UNSPLIT_SUITE_WEIGHT, Job
from python_checks import static_format_check, static_lint_check, type_check_jobs
from shell_targets import static_markdown_shell_check, static_powershell_check, static_shell_check
from suite_discovery import regression_suites, suite_jobs
from validation_support import REPOSITORY_ROOT

POWERSHELL_JOB = "static PowerShell checks (PSScriptAnalyzer on .ps1 files and PowerShell fences)"
MARKDOWN_SHELL_JOB = "static Markdown shell checks (ShellCheck on Bash fences outside skills/ and tests/)"


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

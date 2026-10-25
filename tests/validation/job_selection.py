"""Which jobs a run selects: every job, or on the documentation path the fence checks and the suites that name a
changed file, with --changed the static checks and suites a change reaches, and -k's name patterns; and which of them
one leg of a run split across CI runners takes.
"""

from __future__ import annotations

import ast
import fnmatch
import functools
import math
import unittest
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath

import json_reader
from dealing import deal
from job_pool import UNSPLIT_SUITE_WEIGHT, Job
from path_containment import posix_within
from python_checks import static_format_check, static_lint_check, type_check_jobs
from shell_targets import static_markdown_shell_check, static_powershell_check, static_shell_check
from suite_discovery import FIXTURE_ROOT, regression_suites, suite_jobs
from validation_support import REPOSITORY_ROOT, UnreadablePython, parse_python, repository_files

from deployer import platform_support
from tools.release_notes import git_output

POWERSHELL_JOB = "static PowerShell checks (PSScriptAnalyzer on .ps1 files and PowerShell fences)"
MARKDOWN_SHELL_JOB = "static Markdown shell checks (ShellCheck on Bash fences outside skills/ and tests/)"
SHELL_JOB = "static shell checks (bash -n and ShellCheck on skill scripts)"
FORMAT_JOB = "static format check (ruff format --check)"
LINT_JOB = "static lint check (ruff check)"
# The development dependencies' pins and their hash lock, which pin the ruff, mypy, and pytest every Python check and
# suite runs on, so a bump reaches the format, lint, and type checks and every Python suite.
DEVELOPMENT_DEPENDENCIES = ("requirements-dev.in", "requirements-dev.txt")
# The files besides Python's own that set how the format, lint, and type checks read it.
PYTHON_CHECK_FILES = ("pyproject.toml", *DEVELOPMENT_DEPENDENCIES)
# What a change reaches beyond the suites that name, import, or sit in the skill of a changed file, as pairs of
# changed-path patterns and the suite patterns they add, both fnmatch patterns over repository-relative paths, where *
# crosses folders. A suite pattern is matched against the suites alone, so it names a folder or a suite and never
# restates the suite-name rule. The deployer renders and deploys every shipped skill's files in its rendering suite; it
# reads each skill's metadata, its dependencies, agents, tools, runtime support, and selection, in the rendering suite
# and the bundles-and-dependencies suite; the deployer's suites run deploy.py and every module behind it; and the files
# that set a tool floor are read by the toolchain, upgrade-note, and check suites; and the root conftest.py sets up
# every Python suite, whose git it isolates and whose shards it deals, and the development dependencies pin the pytest
# that runs every Python suite. --changed adds them, so no contributor has to remember them.
CHANGE_REACHES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("skills/*",), ("tests/deployer/test_rendering_locking.py",)),
    (
        ("deploy-meta/*",),
        ("tests/deployer/test_rendering_locking.py", "tests/deployer/test_bundles_dependencies.py"),
    ),
    (("deployer/*", "deploy.py"), ("tests/deployer/*",)),
    (
        (
            "deployer/tools.py",
            "skills/ejryan89-skill-core/scripts/pdf_tool.py",
            "tests/validation/toolchain.py",
            "requirements-dev.in",
            "requirements-dev.txt",
        ),
        (
            "tests/validation/test_toolchain.py",
            "tests/validation/test_upgrade_notes.py",
            "tests/deployer/test_check.py",
        ),
    ),
    (("conftest.py", *DEVELOPMENT_DEPENDENCIES), ("*.py",)),
)
# The folders skills sit in: a change anywhere in a skill reaches the suites in its scripts/.
SKILL_PARENTS = ("skills", ".claude/skills")
# What each job took on CI's runners, by platform and then by job name (a suite's path, summed over its shards, or a
# static check's label), the median across several completed validate runs' logs. A job's cost differs between the
# platforms by up to an order of magnitude, so each platform's legs are dealt by that platform's seconds.
# tools/record_job_seconds.py writes it, and the release procedure refreshes it, so the legs are dealt by measured
# seconds that change only when the file does, never by a run's live timings.
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


def static_jobs() -> list[Job]:
    """The static checks: the shell, fence, format, lint, and type checks."""
    return [
        Job(SHELL_JOB, SHELL_JOB, UNSPLIT_SUITE_WEIGHT, static_shell_check),
        *markdown_jobs(REPOSITORY_ROOT),
        Job(FORMAT_JOB, FORMAT_JOB, UNSPLIT_SUITE_WEIGHT, static_format_check),
        Job(LINT_JOB, LINT_JOB, UNSPLIT_SUITE_WEIGHT, static_lint_check),
        *type_check_jobs(),
    ]


def all_jobs() -> list[Job]:
    return [*static_jobs(), *suite_jobs(regression_suites())]


def changed_static_jobs(paths: list[str], jobs: list[Job], type_checks: set[str]) -> list[Job]:
    """The static checks that read a changed file's kind: the fence checks for Markdown, the PowerShell analyzer for a
    PowerShell script, the shell check for a Bash script, and for Python, its configuration, or the development
    dependencies that pin its checkers, the format and lint
    checks and every type check in type_checks, since a module's callers sit in other type-check roots."""
    suffixes = {PurePosixPath(path).suffix.casefold() for path in paths}
    labels = set()
    if ".md" in suffixes:
        labels |= {POWERSHELL_JOB, MARKDOWN_SHELL_JOB}
    if ".ps1" in suffixes:
        labels.add(POWERSHELL_JOB)
    if suffixes & {".sh", ".bash"}:
        labels.add(SHELL_JOB)
    if ".py" in suffixes or any(path in PYTHON_CHECK_FILES for path in paths):
        labels |= {FORMAT_JOB, LINT_JOB, *type_checks}
    return [job for job in jobs if job.label in labels]


def module_name(path: str) -> str:
    """The name a Python file is imported by: its stem, or its folder's name for a package's __init__.py."""
    pure = PurePosixPath(path)
    return pure.parent.name if pure.name == "__init__.py" else pure.stem


def imported_names(tree: ast.Module) -> set[str]:
    """Every name a module imports by, each part of a dotted name counted, so `from deployer import tools` imports
    deployer and tools; an import_module or __import__ of a string literal counts as an import."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(part for alias in node.names for part in alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            names.update((node.module or "").split("."))
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            function = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if function in ("import_module", "__import__") and isinstance(node.args[0].value, str):
                names.update(node.args[0].value.split("."))
    return names


def python_imports(root: Path, names: Iterable[str]) -> dict[str, set[str]]:
    """What each Python file outside the fixture trees imports, by its repository-relative name. A file no policy can
    read imports nothing here; the policies that read it report it."""
    imports: dict[str, set[str]] = {}
    for name in names:
        if not name.endswith(".py") or posix_within(name, FIXTURE_ROOT.as_posix(), strict=True):
            continue
        try:
            imports[name] = imported_names(parse_python(root / name))
        except UnreadablePython:
            imports[name] = set()
    return imports


def importers(changed: Iterable[str], imports: Mapping[str, set[str]]) -> set[str]:
    """The changed Python files and every Python file that imports one, directly or through others, matched by module
    name: a name two files share reaches the importers of both, which only runs more."""
    reached = {path for path in changed if path.endswith(".py")}
    frontier = set(reached)
    while frontier:
        names = {module_name(path) for path in frontier}
        frontier = {path for path, imported in imports.items() if path not in reached and imported & names}
        reached |= frontier
    return reached


def skill_scripts_prefix(path: str) -> str | None:
    """The scripts/ folder of the skill a path sits in, as a prefix, or None outside a skill."""
    for parent in SKILL_PARENTS:
        rest = path.removeprefix(f"{parent}/")
        if rest != path and "/" in rest:
            return f"{parent}/{rest.split('/', 1)[0]}/scripts/"
    return None


def reached_suite_patterns(paths: list[str]) -> set[str]:
    """The suite patterns CHANGE_REACHES adds for the changed paths."""
    return {
        suite
        for changed, suites in CHANGE_REACHES
        if any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in changed)
        for suite in suites
    }


def changed_suites(root: Path, paths: list[str], suites: list[Path], files: list[str]) -> list[Path]:
    """The suites a change reaches: a changed suite; a suite that imports a changed module, directly or through
    others; a suite that names a changed file or one of those importers, by path or by a file name no other file in
    files has; a suite in the scripts/ of a skill one of them sits in; and the suites CHANGE_REACHES adds."""
    affected = set(paths) | importers(paths, python_imports(root, files))
    counts = Counter(name.rsplit("/", 1)[-1] for name in files)
    named = suites_naming(sorted(affected), suites, frozenset(name for name, count in counts.items() if count > 1))
    skills = tuple({prefix for path in affected if (prefix := skill_scripts_prefix(path))})
    reached = reached_suite_patterns(paths)
    selected = []
    for suite in suites:
        label = suite.relative_to(root).as_posix()
        if (
            label in affected
            or suite in named
            or label.startswith(skills)
            or any(fnmatch.fnmatchcase(label, pattern) for pattern in reached)
        ):
            selected.append(suite)
    return selected


def reached_suites(root: Path, paths: list[str]) -> list[Path]:
    """The suites the changed paths reach, as --changed selects them; tools/mutation_gate.py runs those that reach a
    changed module against its mutants."""
    files = [path.relative_to(root).as_posix() for path in repository_files(root)]
    return changed_suites(root, paths, regression_suites(root), files)


def changed_jobs(root: Path, paths: list[str]) -> list[Job]:
    """What --changed runs beside the policy checks: the static checks and the suites the changed paths reach."""
    type_checks = {job.label for job in type_check_jobs()}
    return [*changed_static_jobs(paths, static_jobs(), type_checks), *suite_jobs(reached_suites(root, paths))]


def change_reach_problems(files: list[str], suites: list[str]) -> list[str]:
    """Report a CHANGE_REACHES pattern that matches no repository file or no regression suite, such as one a rename
    left behind, which would quietly add nothing."""
    problems = []
    for changed, reached in CHANGE_REACHES:
        problems += [
            f"CHANGE_REACHES names {pattern}, which matches no repository file"
            for pattern in changed
            if not any(fnmatch.fnmatchcase(name, pattern) for name in files)
        ]
        problems += [
            f"CHANGE_REACHES adds {pattern}, which matches no regression suite"
            for pattern in reached
            if not any(fnmatch.fnmatchcase(name, pattern) for name in suites)
        ]
    return problems


def changed_paths(root: Path, base: str) -> list[str]:
    """Files changed since the merge base with `base`, plus uncommitted and untracked ones, each as git names it, read
    NUL-separated so a name git would quote is read as itself.

    Renames count as a deletion and an addition, so moving a file out of skills/ is still a change to skills/.
    Raises ReleaseNotesError naming why when git cannot run or cannot tell.
    """
    paths: set[str] = set()
    for command in (
        ["diff", "-z", "--name-only", "--no-renames", f"{base}...HEAD"],
        ["diff", "-z", "--name-only", "--no-renames", "HEAD"],
        ["ls-files", "-z", "--others", "--exclude-standard"],
    ):
        paths.update(name for name in git_output(root, *command).split("\0") if name)
    return sorted(paths)


def suites_naming(paths: list[str], suites: list[Path], shared: frozenset[str] = frozenset()) -> list[Path]:
    """The suites whose source names a changed file, by path or by file name, unless shared holds the file name, as
    for a name several files have, such as SKILL.md, which then matches by path alone."""
    names = {name for path in paths for name in (path, path.rsplit("/", 1)[-1]) if name == path or name not in shared}
    return [suite for suite in suites if any(name in suite.read_text(encoding="utf-8") for name in names)]


def name_patterns(patterns: list[str]) -> list[str]:
    """unittest's -k rule: a pattern without a wildcard matches as a substring."""
    return [pattern if any(character in pattern for character in "*?[") else f"*{pattern}*" for pattern in patterns]


def job_seconds(path: Path = JOB_SECONDS) -> dict[str, dict[str, float]]:
    """The measured seconds by platform and job name: a JSON object of the runs they came from and the seconds, an
    object for each platform the deployer supports, each value a finite number above zero. A file that cannot be read,
    is not UTF-8 JSON, nests past the parser's depth, repeats a key, or holds NaN or Infinity is refused by name, as
    ValueError.

    A table from before the runs were several, which records one run under "run", or from before each platform had its
    own seconds, whose seconds are by job name alone, is refused rather than read: the release step that writes it
    rewrites it whole, so a reader of both would only keep the old shape alive.
    """
    try:
        table = json_reader.loads(path.read_text(encoding="utf-8"))
    # ValueError covers the decode errors, UnicodeError, an over-long integer, and what json_reader refuses: a repeated
    # key, NaN or Infinity, and nesting too deep
    except (OSError, ValueError) as error:
        raise ValueError(f"{path.name} cannot be read as JSON: {error}") from error
    if isinstance(table, dict) and "run" in table and "runs" not in table:
        raise ValueError(
            f'{path.name} records one run under "run"; rerun tools/record_job_seconds.py to record the "runs"'
        )
    if not (isinstance(table, dict) and set(table) == {"runs", "seconds"} and isinstance(table["seconds"], dict)):
        raise ValueError(f'{path.name} must be an object of "runs" and "seconds", the seconds an object by job name')
    runs = table["runs"]
    if not (
        isinstance(runs, list)
        and runs
        and all(isinstance(run, int) and not isinstance(run, bool) and run > 0 for run in runs)
        # Distinct only once each is an integer: a list or an object among them cannot be put in a set.
        and len(set(runs)) == len(runs)
    ):
        raise ValueError(f'{path.name} records "runs" as {runs!r}; they are a list of distinct run IDs, at least one')
    platforms = sorted(platform_support.SUPPORTED_PLATFORMS.values())
    seconds = table["seconds"]
    if any(not isinstance(value, dict) for value in seconds.values()):
        raise ValueError(
            f"{path.name} records seconds by job name alone; rerun tools/record_job_seconds.py to record each "
            f"platform's, under {', '.join(platforms)}"
        )
    if sorted(seconds) != platforms:
        raise ValueError(f"{path.name} records seconds for {sorted(seconds)}; it records each of {platforms}")
    for platform, jobs in seconds.items():
        for name, value in jobs.items():
            # A float past the largest one, such as 1e999, parses to infinity without naming a constant.
            if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value < math.inf:
                raise ValueError(
                    f"{path.name} records {name} on {platform} at {value!r}; a job's seconds are a finite number "
                    "above zero"
                )
    return {platform: {name: float(value) for name, value in jobs.items()} for platform, jobs in seconds.items()}


@functools.cache
def recorded_job_seconds() -> dict[str, dict[str, float]]:
    return job_seconds()


def host_job_seconds() -> dict[str, float]:
    """The recorded seconds of the platform this process runs on, by which its legs are dealt."""
    return recorded_job_seconds()[platform_support.PLATFORM_NAME]


def leg_cost(job: Job, shards: int, seconds: Mapping[str, float]) -> float:
    """What a job counts for, dealt to a leg, in seconds: its suite's measured seconds shared among the suite's
    `shards`, or without a measurement its count at TYPICAL_TEST_SECONDS a test."""
    if job.name in seconds:
        return seconds[job.name] / shards
    return (UNSPLIT_JOB_COST if job.weight == UNSPLIT_SUITE_WEIGHT else job.weight) * TYPICAL_TEST_SECONDS


def flatten(suite: unittest.TestSuite) -> Iterable[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def leg_jobs(jobs: list[Job], index: int, count: int, seconds: Mapping[str, float] | None = None) -> list[Job]:
    """The jobs leg `index` (from 0) of `count` runs, dealt as conftest.py deals a suite's tests: the heaviest first
    onto the leg with the least so far, by label on a tie, each weighed by leg_cost from `seconds` (when None, the
    host platform's table in JOB_SECONDS). The legs run every job exactly once between them, and each leg's share
    depends only on the jobs and the table, never on their order, so legs that select alike deal alike."""
    measured = host_job_seconds() if seconds is None else seconds
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


def stale_job_seconds_problems(seconds: Mapping[str, Mapping[str, float]], names: set[str]) -> list[str]:
    """Report a measured entry on any platform that names no job a full run selects, such as a suite since renamed or
    removed."""
    return [
        f"{JOB_SECONDS.name} records {name} on {platform}, which no validation job runs; rerun "
        "tools/record_job_seconds.py on a completed main run, or remove the entry"
        for platform, jobs in sorted(seconds.items())
        for name in sorted(set(jobs) - names)
    ]


class JobSelectionPolicies(unittest.TestCase):
    def test_measured_job_seconds_name_jobs_that_run(self) -> None:
        self.assertEqual([], stale_job_seconds_problems(job_seconds(), {job.name for job in all_jobs()}))

    def test_each_change_reach_names_files_and_suites_that_exist(self) -> None:
        files = [path.relative_to(REPOSITORY_ROOT).as_posix() for path in repository_files(REPOSITORY_ROOT)]
        suites = [suite.relative_to(REPOSITORY_ROOT).as_posix() for suite in regression_suites()]
        self.assertEqual([], change_reach_problems(files, suites))

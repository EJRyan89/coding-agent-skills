"""Run the complete repository validation suite.

Usage: python -B tests/run_validation.py [-k PATTERN ... | --changed | --full] [-v] [--pr-body FILE]
                                        [--pr-title FILE] [--shard INDEX/COUNT]

It runs every regression suite under tests/ and each skill's scripts/ in one pool of worker processes, largest first,
and the policy checks in this process while the pool works, printing their report when they finish. The policies live
in tests/validation/, one module per family, each with its fixture tests beside it as
tests/validation/test_<module>.py, which run in the pool as regression suites. Each Python suite runs as
`python -B -m pytest <suite>` from the repository root, where pytest loads the root conftest.py, and a large one is
split into shards, each its own pytest process that conftest.py's --suite-shard deals the suite's tests to, so no single
suite sets the length of the run.
-k selects the policy checks whose name, and the suites whose path, matches a pattern, and a pattern that matches
none of either is a usage error, exit 2, rather than a run that passes empty. VALIDATION_JOBS sets the number of
workers.

When every changed file is documentation, it runs the policy checks and only the suites that name a changed
file, and, when a changed file is Markdown, the PowerShell analyzer and the ShellCheck check of Markdown Bash fences;
anything else, or a change it cannot determine, runs everything. --full always runs everything.

--changed, the local check of a change, runs the policy checks, the static checks that read a changed file's kind,
and the suites the change reaches: a changed suite, a suite that imports a changed module directly or through others,
one that names a changed file or such an importer, one in the skill a changed file sits in, and the suites
job_selection.CHANGE_REACHES adds, such as the deployer's rendering suite for a shipped skill's files and the floor
suites for a tool floor. A change it cannot determine runs everything.

--pr-body names a file holding the pull request's body, whose `## Upgrade note` the upgrade-notes check reads beside
the notes merged into origin/main since the last tag; CI passes the body this way. Without it, the check reads the
notes in the commit messages since that tag on HEAD, the branch's own included. --pr-title names a file holding the
pull request's title, which the title check holds to the typed prefix; CI passes it the same way, and without it the
check passes.

--shard 2/4 runs the second of four legs, as CI's runners do: once -k or the documentation path has selected the
policy checks and suite jobs, tests/validation/job_selection.py deals them across the legs the same way on every leg,
so the legs together run exactly what one run would, each once.

The policy checks run on the main thread while the pool's thread starts jobs from os.environ and prints each result
to sys.stdout, so no policy check swaps a standard stream or changes the environment, and
suite_discovery.pool_shared_state_problems holds the rule. main takes its streams and environment as parameters, which
its suite, tests/test_run_validation.py, passes, and it constructs each check that judges the pull request with the
--pr-body and --pr-title files it was given rather than setting them anywhere a check reads.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import os
import sys
import time
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple, NoReturn, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "validation"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "skill-core" / "scripts"))

import duplication
import fsops_platform
import job_selection
import kinds_table
import markdown_links
import pull_request_title
import python_checks
import repository_hygiene
import shell_targets
import silent_reverts
import skill_grants
import skill_layout
import skill_scripts
import suite_discovery
import toolchain
import upgrade_notes
import workarounds
import workflows
from console import run_main
from dealing import shard_of
from job_pool import (
    OUTPUT_LOCK,
    Job,
    PoolResult,
    append_step_summary,
    run_beside,
    step_summary,
    worker_count,
)
from job_selection import (
    MARKDOWN_SHELL_JOB,
    POWERSHELL_JOB,
    all_jobs,
    changed_jobs,
    changed_paths,
    documentation_jobs,
    leg_jobs,
    leg_policies,
    name_patterns,
)
from suite_discovery import regression_suites
from toolchain import report_prerequisite_problems, tool_versions
from validation_support import REPOSITORY_ROOT, PullRequest, PullRequestChecks

from deployer import tools

# The modules whose policy checks run in this process while the pool runs the suites. Each policy module's fixture
# tests run in the pool as the regression suite beside it.
POLICY_MODULES = (
    duplication,
    fsops_platform,
    job_selection,
    kinds_table,
    markdown_links,
    pull_request_title,
    python_checks,
    repository_hygiene,
    shell_targets,
    silent_reverts,
    skill_grants,
    skill_layout,
    skill_scripts,
    suite_discovery,
    toolchain,
    upgrade_notes,
    workarounds,
    workflows,
)

# Documentation no regression suite executes. A suite that names one of these files still runs when it changes.
# Markdown under skills/, agents/, or .claude/ is skill and agent behavior, not documentation.
DOCUMENTATION_DIRECTORIES = ("docs/", ".github/ISSUE_TEMPLATE/")
DOCUMENTATION_FILES = {
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CLAUDE.md",
    "AGENTS.md",
    ".github/pull_request_template.md",
}


def is_documentation(path: str) -> bool:
    return path in DOCUMENTATION_FILES or path.startswith(DOCUMENTATION_DIRECTORIES)


def documentation_only(paths: list[str] | None) -> tuple[bool, str]:
    """Whether every changed file is documentation, and why. Anything uncertain is not."""
    if paths is None:
        return False, "the changed files could not be determined"
    if not paths:
        return False, "no changed files were found"
    others = [path for path in paths if not is_documentation(path)]
    if others:
        return False, f"{len(others)} changed files are not documentation, such as {others[0]}"
    return True, f"all {len(paths)} changed files are documentation"


def shard_option(text: str) -> tuple[int, int]:
    """--shard's `<index>/<count>`, the index counted from 1."""
    try:
        return shard_of(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected {error}") from error


def deal_to_leg(
    policies: unittest.TestSuite, jobs: list[Job], shard: tuple[int, int]
) -> tuple[unittest.TestSuite, list[Job], str]:
    """This leg's policy checks and jobs, and the line that says what share of the selection it runs."""
    index, count = shard
    leg_checks, leg_suites = leg_policies(policies, index - 1, count), leg_jobs(jobs, index - 1, count)
    line = (
        f"Shard {index}/{count}: {leg_checks.countTestCases()} of {policies.countTestCases()} policy checks and "
        f"{len(leg_suites)} of {len(jobs)} suite jobs."
    )
    return leg_checks, leg_suites, line


class PolicyLoader(unittest.TestLoader):
    """unittest's loader, but for constructing each check that judges the pull request with the one the run names."""

    def __init__(self, pull_request: PullRequest) -> None:
        super().__init__()
        self.pull_request = pull_request

    def loadTestsFromTestCase(self, testCaseClass: type[unittest.TestCase]) -> unittest.TestSuite:
        if not issubclass(testCaseClass, PullRequestChecks):
            return super().loadTestsFromTestCase(testCaseClass)
        names = self.getTestCaseNames(testCaseClass)
        return self.suiteClass([testCaseClass(name, self.pull_request) for name in names])


def run_policies(policies: unittest.TestSuite, verbose: bool, out: TextIO | io.TextIOBase) -> unittest.TestResult:
    """Run the policy checks into a buffer and print their report in one block once they finish."""
    report = io.StringIO()
    result = unittest.TextTestRunner(stream=report, verbosity=2 if verbose else 1).run(policies)
    with OUTPUT_LOCK:
        print(f"Policy checks:\n{report.getvalue()}", end="", file=out, flush=True)
    return result


class Encodable(io.TextIOBase):
    """A text stream that writes what the stream it wraps cannot encode as backslash escapes, so a report quoting what
    a suite printed, a replacement character included, reaches its closing line on a legacy code page."""

    def __init__(self, stream: TextIO | io.TextIOBase) -> None:
        super().__init__()
        self.stream = stream

    def write(self, text: str) -> int:
        encoding = getattr(self.stream, "encoding", None) or "utf-8"
        self.stream.write(text.encode(encoding, "backslashreplace").decode(encoding))
        return len(text)

    def flush(self) -> None:
        # A stream that is gone, as a test's is once the test ends, has nothing left to flush.
        if not self.stream.closed:
            self.stream.flush()


class UsageParser(argparse.ArgumentParser):
    """An argument parser that writes a usage error to the stream it is given, so the runner's own checks read the
    error without swapping sys.stderr beside the pool."""

    def __init__(self, *, errors: TextIO | None = None, **options: Any) -> None:
        super().__init__(**options)
        self.errors = errors

    def error(self, message: str) -> NoReturn:
        stream = sys.stderr if self.errors is None else self.errors
        self.print_usage(stream)
        stream.write(f"{self.prog}: error: {message}\n")
        raise SystemExit(2)


def parse_arguments(argv: list[str] | None, errors: TextIO | None = None) -> argparse.Namespace:
    parser = UsageParser(errors=errors, description="Run the repository's policy checks and regression suites.")
    parser.add_argument(
        "-k",
        dest="patterns",
        action="append",
        default=[],
        metavar="PATTERN",
        help="run only the policy checks whose name, and the suites whose path, match; repeatable",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="list each policy check and suite as it finishes")
    reach = parser.add_mutually_exclusive_group()
    reach.add_argument("--full", action="store_true", help="run every suite even when only documentation changed")
    reach.add_argument(
        "--changed",
        action="store_true",
        help="run the policy checks and the static checks and suites the files changed since the base reach",
    )
    parser.add_argument(
        "--pr-body",
        type=Path,
        metavar="FILE",
        help="a file holding the pull request body, whose ## Upgrade note the upgrade-notes check reads",
    )
    parser.add_argument(
        "--pr-title",
        type=Path,
        metavar="FILE",
        help="a file holding the pull request title, which the title check holds to the typed prefix",
    )
    parser.add_argument(
        "--shard",
        type=shard_option,
        metavar="INDEX/COUNT",
        help="run leg INDEX of COUNT: a share of the selected policy checks and suites, dealt alike on every leg",
    )
    arguments = parser.parse_args(argv)
    if arguments.changed and arguments.patterns:
        parser.error("argument --changed: not allowed with argument -k, which selects by name instead")
    if arguments.pr_body is not None and not arguments.pr_body.is_file():
        parser.error(f"--pr-body is not a file: {arguments.pr_body}")
    if arguments.pr_title is not None and not arguments.pr_title.is_file():
        parser.error(f"--pr-title is not a file: {arguments.pr_title}")
    return arguments


def select_jobs(arguments: argparse.Namespace, base: str) -> tuple[list[Job], str]:
    """The jobs a run without -k selects and the line that says why: every job with --full; with --changed the static
    checks and suites the change since base reaches; otherwise, when every changed file is documentation, the fence
    checks and the suites that name a changed file; and every job when the changed files cannot be determined."""
    paths = None if arguments.full else changed_paths(REPOSITORY_ROOT, base)
    if arguments.changed:
        if paths is None:
            return all_jobs(), f"Full validation: the files changed since {base} could not be determined."
        jobs = changed_jobs(REPOSITORY_ROOT, paths)
        return jobs, (
            f"Changed files: {len(paths)} files changed since {base}. Running the policy checks and the "
            f"{len({job.name for job in jobs})} static checks and suites they reach; pass --full to run every suite."
        )
    only, reason = (False, "--full was given") if arguments.full else documentation_only(paths)
    if not only or paths is None:
        return all_jobs(), f"Full validation: {reason}."
    jobs = documentation_jobs(REPOSITORY_ROOT, paths, regression_suites())
    fences = [job.label for job in jobs if job.label in (POWERSHELL_JOB, MARKDOWN_SHELL_JOB)]
    suites = sorted({job.name for job in jobs if job.label not in fences})
    return jobs, (
        f"Documentation only: {reason} since {base}. Running the policy checks, "
        + (f"the {len(fences)} Markdown fence checks, " if fences else "")
        + f"and the {len(suites)} suites that name a changed file; pass --full to run every suite."
    )


class Run(NamedTuple):
    """What a run's report says besides its results: how its jobs were chosen, its leg, its tools, and its seconds."""

    mode: str
    leg: str | None
    versions: dict[str, tuple[int, ...] | None]
    seconds: float


def report_run(
    policy_result: unittest.TestResult,
    pool: PoolResult,
    jobs: int,
    run: Run,
    out: TextIO | io.TextIOBase,
    environment: Mapping[str, str],
) -> bool:
    """Print each retried and failed job and the closing line to out, append the step summary to the file environment
    names, and return whether the run passed."""
    failures, retries = pool
    retried = {retry.job.label: f"{retry.ending}\nStarted once more, it {retry.result}." for retry in retries}
    for label, note in retried.items():
        print(f"\n{'=' * 70}\nRETRIED {label}\n{'-' * 70}\n{note}", file=out, flush=True)
    for failure in failures:
        print(f"\n{'=' * 70}\nFAILED {failure.job.label}\n{'-' * 70}\n{failure.report}", file=out, flush=True)
    passed = policy_result.wasSuccessful() and not failures
    print(
        f"\n{f'Shard {run.leg}: ' if run.leg else ''}{policy_result.testsRun} policy checks and {jobs} suite jobs in "
        f"{run.seconds:.0f}s: "
        + ("validation passed." if passed else f"validation FAILED ({len(failures)} suite jobs failed)."),
        file=out,
    )
    failed = [f"policy {test.id().rsplit('.', 1)[-1]}" for test, _ in policy_result.failures + policy_result.errors]
    failed += [failure.job.label for failure in failures]
    details = {
        failure.job.label: failure.report
        for failure in failures
        if failure.raised or failure.terminated or failure.timed_out
    }
    summary = step_summary(
        tools.python_version(),
        run.versions,
        run.mode,
        policy_result.testsRun,
        jobs,
        run.seconds,
        failed,
        details,
        run.leg,
        retried,
    )
    append_step_summary(environment, summary)
    return passed


def main(
    argv: list[str] | None = None,
    *,
    out: TextIO | io.TextIOBase | None = None,
    errors: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Run the validation. Its own output goes to out, a usage error or an empty selection to errors, and it reads the
    base branch, the worker count, and the step summary's path from environment: sys.stdout, sys.stderr, and
    os.environ unless given."""
    out = Encodable(sys.stdout if out is None else out)
    errors = sys.stderr if errors is None else errors
    environment = os.environ if environment is None else environment
    arguments = parse_arguments(argv, errors)
    try:
        workers = worker_count(environment)
    except ValueError as error:
        print(f"run_validation.py: error: {error}", file=errors)
        return 2
    versions = tool_versions()
    if report_prerequisite_problems(versions):
        return 2
    started = time.perf_counter()
    patterns = name_patterns(arguments.patterns)
    loader = PolicyLoader(PullRequest(arguments.pr_body, arguments.pr_title))
    if patterns:
        loader.testNamePatterns = patterns
    policies = unittest.TestSuite(loader.loadTestsFromModule(module) for module in POLICY_MODULES)
    if patterns:
        jobs = [job for job in all_jobs() if any(fnmatch.fnmatchcase(job.name, pattern) for pattern in patterns)]
        mode = f"Selected by -k {' '.join(arguments.patterns)}."
    else:
        jobs, mode = select_jobs(arguments, f"origin/{environment.get('GITHUB_BASE_REF') or 'main'}")
        print(mode, file=out, flush=True)
    if not policies.countTestCases() and not jobs:
        print(f"No policy check or suite matches {' '.join(arguments.patterns)}.", file=errors)
        return 2
    leg = f"{arguments.shard[0]}/{arguments.shard[1]}" if arguments.shard else None
    if arguments.shard:
        try:
            policies, jobs, share = deal_to_leg(policies, jobs, arguments.shard)
        except ValueError as error:
            print(f"run_validation.py: error: the legs cannot be dealt: {error}", file=errors)
            return 2
        mode = f"{mode} {share}"
        print(share, file=out, flush=True)

    print(
        f"Running {policies.countTestCases()} policy checks beside {len(jobs)} suite jobs on {workers} workers.",
        file=out,
        flush=True,
    )
    policy_result, pool = run_beside(
        lambda: run_policies(policies, arguments.verbose, out), jobs, arguments.verbose, workers
    )
    passed = report_run(
        policy_result,
        pool,
        len(jobs),
        Run(mode, leg, versions, time.perf_counter() - started),
        out,
        environment,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    # skill-core's runner writes UTF-8, so a report quoting what a suite printed, its replacement characters included,
    # ends with its closing line and its step summary on any console, and a crash of the runner itself ends in a FAILED
    # line after its traceback.
    run_main(main, errors="backslashreplace", traceback_requested=lambda: True)

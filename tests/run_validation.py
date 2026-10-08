"""Run the complete repository validation suite.

Usage: python -B tests/run_validation.py [-k PATTERN ...] [-v] [--full]

It runs every regression suite under tests/ and each skill's scripts/ in one pool of worker processes, largest first,
and the policy checks in this process while the pool works, printing their report when they finish. The policies live
in tests/validation/, one module per family, each with its fixture tests beside it as
tests/validation/test_<module>.py, which run in the pool as regression suites. A large Python suite is split into
shards, each run by tests/run_shard.py in its own process, so no single suite sets the length of the run.
-k selects the policy checks whose name, and the suites whose path, matches a pattern. VALIDATION_JOBS sets the
number of workers.

When every changed file is documentation, it runs the policy checks and only the suites that name a changed
file; anything else, or a change it cannot determine, runs everything. --full always runs everything.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "validation"))

import duplication
import fsops_platform
import markdown_links
import python_checks
import repository_hygiene
import shell_targets
import skill_grants
import skill_layout
import skill_scripts
import suite_discovery
import toolchain
import upgrade_notes
import workflows
from job_pool import OUTPUT_LOCK, append_step_summary, run_beside, step_summary, worker_count
from job_selection import (
    MARKDOWN_SHELL_JOB,
    POWERSHELL_JOB,
    all_jobs,
    changed_paths,
    documentation_jobs,
    name_patterns,
)
from suite_discovery import regression_suites
from toolchain import report_prerequisite_problems, tool_versions
from validation_support import REPOSITORY_ROOT

from deployer import tools

# The modules whose policy checks run in this process while the pool runs the suites. Each policy module's fixture
# tests run in the pool as the regression suite beside it.
POLICY_MODULES = (
    duplication,
    fsops_platform,
    markdown_links,
    python_checks,
    repository_hygiene,
    shell_targets,
    skill_grants,
    skill_layout,
    skill_scripts,
    suite_discovery,
    toolchain,
    upgrade_notes,
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


class DocumentationDecision(unittest.TestCase):
    def test_documentation_paths_are_classified(self) -> None:
        for path in (
            "docs/skills.md",
            "docs/new/guide.md",
            "README.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "CLAUDE.md",
            "AGENTS.md",
            ".github/ISSUE_TEMPLATE/bug.md",
            ".github/pull_request_template.md",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_documentation(path))
        for path in (
            "skills/repo-cleanup/SKILL.md",
            "skills/README.md",
            "agents/code-review-reviewer.md",
            ".claude/skills/change-skill/SKILL.md",
            ".agents/skills/change-skill/SKILL.md",
            ".github/workflows/validate.yml",
            "tests/run_validation.py",
            "deployer/source.py",
            "source.json",
            "docs",
            "LICENSE",
            "readme.md",
        ):
            with self.subTest(path=path):
                self.assertFalse(is_documentation(path))

    def test_only_a_change_that_is_all_documentation_skips_the_suites(self) -> None:
        self.assertEqual(
            (True, "all 2 changed files are documentation"), documentation_only(["README.md", "docs/skills.md"])
        )
        for paths, reason in (
            (None, "could not be determined"),
            ([], "no changed files"),
            (
                ["docs/skills.md", "skills/repo-cleanup/SKILL.md"],
                "1 changed files are not documentation, such as skills/",
            ),
        ):
            with self.subTest(paths=paths):
                only, why = documentation_only(paths)
                self.assertFalse(only)
                self.assertIn(reason, why)


def run_policies(policies: unittest.TestSuite, verbose: bool) -> unittest.TestResult:
    """Run the policy checks into a buffer and print their report in one block once they finish."""
    report = io.StringIO()
    result = unittest.TextTestRunner(stream=report, verbosity=2 if verbose else 1).run(policies)
    with OUTPUT_LOCK:
        print(f"Policy checks:\n{report.getvalue()}", end="", flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the repository's policy checks and regression suites.")
    parser.add_argument(
        "-k",
        dest="patterns",
        action="append",
        default=[],
        metavar="PATTERN",
        help="run only the policy checks whose name, and the suites whose path, match; repeatable",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="list each policy check and suite as it finishes")
    parser.add_argument("--full", action="store_true", help="run every suite even when only documentation changed")
    arguments = parser.parse_args(argv)
    versions = tool_versions()
    if report_prerequisite_problems(versions):
        return 2
    started = time.perf_counter()
    patterns = name_patterns(arguments.patterns)
    loader = unittest.TestLoader()
    if patterns:
        loader.testNamePatterns = patterns
    policies = unittest.TestSuite(
        loader.loadTestsFromModule(module) for module in (sys.modules[__name__], *POLICY_MODULES)
    )
    if patterns:
        jobs = [job for job in all_jobs() if any(fnmatch.fnmatchcase(job.name, pattern) for pattern in patterns)]
        mode = f"Selected by -k {' '.join(arguments.patterns)}."
    else:
        base = f"origin/{os.environ.get('GITHUB_BASE_REF') or 'main'}"
        paths = None if arguments.full else changed_paths(REPOSITORY_ROOT, base)
        only, reason = (False, "--full was given") if arguments.full else documentation_only(paths)
        if only and paths is not None:
            jobs = documentation_jobs(REPOSITORY_ROOT, paths, regression_suites())
            fences = [job.label for job in jobs if job.label in (POWERSHELL_JOB, MARKDOWN_SHELL_JOB)]
            suites = sorted({job.name for job in jobs if job.label not in fences})
            mode = (
                f"Documentation only: {reason} since {base}. Running the policy checks, "
                + (f"the {len(fences)} Markdown fence checks, " if fences else "")
                + f"and the {len(suites)} suites that name a changed file; pass --full to run every suite."
            )
        else:
            jobs = all_jobs()
            mode = f"Full validation: {reason}."
        print(mode, flush=True)
    if not policies.countTestCases() and not jobs:
        print(f"No policy check or suite matches {' '.join(arguments.patterns)}.", file=sys.stderr)
        return 2

    workers = worker_count()
    print(
        f"Running {policies.countTestCases()} policy checks beside {len(jobs)} suite jobs on {workers} workers.",
        flush=True,
    )
    policy_result, failures = run_beside(
        lambda: run_policies(policies, arguments.verbose), jobs, arguments.verbose, workers
    )
    for failure in failures:
        print(f"\n{'=' * 70}\nFAILED {failure.job.label}\n{'-' * 70}\n{failure.report}", flush=True)
    passed = policy_result.wasSuccessful() and not failures
    seconds = time.perf_counter() - started
    print(
        f"\n{policy_result.testsRun} policy checks and {len(jobs)} suite jobs in {seconds:.0f}s: "
        + ("validation passed." if passed else f"validation FAILED ({len(failures)} suite jobs failed).")
    )
    failed = [f"policy {test.id().rsplit('.', 1)[-1]}" for test, _ in policy_result.failures + policy_result.errors]
    failed += [failure.job.label for failure in failures]
    tracebacks = {failure.job.label: failure.report for failure in failures if failure.raised}
    summary = step_summary(
        tools.python_version(), versions, mode, policy_result.testsRun, len(jobs), seconds, failed, tracebacks
    )
    append_step_summary(os.environ, summary)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

"""Regression tests for tests/run_validation.py: what its options select, the usage errors it refuses before any
check runs, the pull request it gives the checks that judge one, and how it reports a retried, a terminated, or a
failed job. Each reads main's output through the stream and the environment it is given."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import os
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "validation"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import executable_locator
from git_client import MISSING_GIT
from job_pool import Failure, Job, PoolResult, Retry
from job_selection import MARKDOWN_SHELL_JOB, flatten
from suite_names import is_suite_name
from validation_support import REPOSITORY_ROOT, PullRequest, PullRequestChecks

from tests import run_validation
from tests.run_validation import documentation_only, is_documentation, main, parse_arguments, shard_option


@contextlib.contextmanager
def git_not_on_path() -> Iterator[None]:
    """PATH names a folder that does not exist and no link folder is searched, so the real locator finds no git and
    the real client raises its own failure."""
    nowhere = str(Path(__file__).resolve().parent / "no git here")
    with mock.patch.dict(os.environ, {"PATH": nowhere}), mock.patch.object(executable_locator, "link_folders", list):
        yield


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


class PullRequestBodyOption(unittest.TestCase):
    def test_a_body_that_is_not_a_file_is_a_usage_error_before_any_check_runs(self) -> None:
        missing = REPOSITORY_ROOT / "tests" / "no such body.md"
        error = io.StringIO()
        with (
            mock.patch.object(run_validation, "tool_versions") as versions,
            self.assertRaises(SystemExit) as raised,
        ):
            main(["--pr-body", str(missing)], errors=error)
        self.assertEqual(2, raised.exception.code)
        self.assertIn(f"--pr-body is not a file: {missing}", error.getvalue())
        versions.assert_not_called()

    def test_a_title_that_is_not_a_file_is_a_usage_error_before_any_check_runs(self) -> None:
        missing = REPOSITORY_ROOT / "tests" / "no such title.txt"
        error = io.StringIO()
        with (
            mock.patch.object(run_validation, "tool_versions") as versions,
            self.assertRaises(SystemExit) as raised,
        ):
            main(["--pr-title", str(missing)], errors=error)
        self.assertEqual(2, raised.exception.code)
        self.assertIn(f"--pr-title is not a file: {missing}", error.getvalue())
        versions.assert_not_called()

    def test_the_body_and_the_title_reach_each_check_that_judges_the_pull_request(self) -> None:
        body, title = REPOSITORY_ROOT / "tests" / "run_validation.py", REPOSITORY_ROOT / "conftest.py"
        loaded: list[unittest.TestSuite] = []

        def policies_run(policies: unittest.TestSuite, *_: object) -> unittest.TestResult:
            loaded.append(policies)
            return unittest.TestResult()

        with (
            mock.patch.object(run_validation, "tool_versions", return_value={}),
            mock.patch.object(run_validation, "report_prerequisite_problems", return_value=False),
            mock.patch.object(run_validation, "all_jobs", return_value=[]),
            mock.patch.object(run_validation, "run_policies", side_effect=policies_run),
            mock.patch.object(run_validation, "run_beside", side_effect=lambda run, *_: (run(), PoolResult([], []))),
        ):
            code = main(
                [
                    *(
                        "-k",
                        "*PullRequestTitlePolicies.*",
                        "-k",
                        "*UpgradeNotesPolicies.*",
                        "-k",
                        "*SilentRevertPolicies.*",
                    ),
                    *("--pr-body", str(body), "--pr-title", str(title)),
                ],
                out=io.StringIO(),
                environment={},
            )
        self.assertEqual(0, code)
        checks = [check for check in flatten(loaded[0]) if isinstance(check, PullRequestChecks)]
        self.assertEqual(
            {"PullRequestTitlePolicies", "SilentRevertPolicies", "UpgradeNotesPolicies"},
            {type(check).__name__ for check in checks},
        )
        for check in checks:
            with self.subTest(check=check.id()):
                self.assertEqual(PullRequest(body, title), check.pull_request)

    def test_a_check_constructed_without_a_pull_request_judges_no_body_and_no_title(self) -> None:
        check = PullRequestChecks()
        self.assertEqual(PullRequest(), check.pull_request)
        self.assertIsNone(check.pull_request.body_text())
        self.assertIsNone(check.pull_request.title_text())

    def test_a_title_and_body_saved_with_a_byte_order_mark_are_judged_as_typed(self) -> None:
        # A Windows editor may save the mark before the text, which a utf-8 read keeps at the head of the title.
        with tempfile.TemporaryDirectory() as temporary:
            title, body = Path(temporary) / "title.txt", Path(temporary) / "body.md"
            title.write_bytes(b"\xef\xbb\xbffix(deployer): keep a thing\n")
            body.write_bytes(b"\xef\xbb\xbf## Summary\n")
            pull_request = PullRequest(body, title)
            self.assertEqual("fix(deployer): keep a thing\n", pull_request.title_text())
            self.assertEqual("## Summary\n", pull_request.body_text())


class ShardOption(unittest.TestCase):
    def test_a_shard_is_an_index_from_one_up_to_its_count(self) -> None:
        self.assertEqual((1, 1), shard_option("1/1"))
        self.assertEqual((2, 4), shard_option("2/4"))
        self.assertEqual((4, 4), shard_option("4/4"))
        for text in ("0/4", "5/4", "2/0", "2", "2/4/8", "-1/4", " 2/4", "a/b", "02/4"):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                shard_option(text)

    def test_a_shard_outside_its_count_is_a_usage_error_before_any_check_runs(self) -> None:
        error = io.StringIO()
        with mock.patch.object(run_validation, "tool_versions") as versions, self.assertRaises(SystemExit) as raised:
            main(["--shard", "5/4"], errors=error)
        self.assertEqual(2, raised.exception.code)
        self.assertIn("argument --shard: expected <index>/<count>", error.getvalue())
        versions.assert_not_called()


class SelectionOptions(unittest.TestCase):
    """What -k and --changed select, read through the stream and the environment main is given."""

    def run_main(self, argv: list[str], **patches: Any) -> tuple[int, str, str, list[list[Job]]]:
        """main's exit code, output, error output, and the jobs it gave the pool, with the tools and the files the
        checks read held still and the named functions of this module replaced."""
        started: list[list[Job]] = []

        def pool_runs(_policies: object, jobs: list[Job], *_: object) -> tuple[unittest.TestResult, PoolResult]:
            started.append(jobs)
            return unittest.TestResult(), PoolResult([], [])

        printed, error = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(run_validation, "tool_versions", return_value={}))
            stack.enter_context(mock.patch.object(run_validation, "report_prerequisite_problems", return_value=False))
            stack.enter_context(mock.patch.object(run_validation, "run_beside", side_effect=pool_runs))
            for name, replacement in patches.items():
                stack.enter_context(mock.patch.object(run_validation, name, replacement))
            code = main(argv, out=printed, errors=error, environment={})
        return code, printed.getvalue(), error.getvalue(), started

    def test_a_pattern_matching_no_policy_check_and_no_suite_is_a_usage_error_before_any_job_starts(self) -> None:
        unmatched = Job("tests/test_other.py", "tests/test_other.py", 1, lambda: None)
        code, _, error, started = self.run_main(
            ["-k", "zz names nothing"], all_jobs=mock.Mock(return_value=[unmatched])
        )
        self.assertEqual(2, code)
        self.assertEqual("No policy check or suite matches zz names nothing.\n", error)
        self.assertEqual([], started)

    def test_a_pattern_matching_nothing_beside_one_that_matches_is_a_usage_error_naming_it(self) -> None:
        suite = Job("tests/test_other.py", "tests/test_other.py", 1, lambda: None)
        for argv in (
            ["-k", "test_other", "-k", "zz names nothing"],
            ["-k", "PullRequestTitlePolicies", "-k", "zz names nothing", "-k", "yy?names*nothing"],
        ):
            with self.subTest(argv=argv):
                code, _, error, started = self.run_main(argv, all_jobs=mock.Mock(return_value=[suite]))
                self.assertEqual(2, code)
                unmatched = argv[3::2]
                self.assertEqual("".join(f"No policy check or suite matches {name}.\n" for name in unmatched), error)
                self.assertEqual([], started)

    def test_a_run_without_a_pattern_that_selects_nothing_is_a_usage_error_naming_how_it_selected(self) -> None:
        # The policy modules always hold checks, so only a run with none of them reaches the refusal without -k.
        code, _, error, started = self.run_main(
            ["--changed"],
            POLICY_MODULES=(),
            changed_paths=mock.Mock(return_value=["LICENSE"]),
            changed_jobs=mock.Mock(return_value=[]),
        )
        self.assertEqual(2, code)
        self.assertEqual(
            "No policy check or suite job is selected: Changed files: 1 files changed since origin/main. Running the "
            "policy checks and the 0 static checks and suites they reach; pass --full to run every suite.\n",
            error,
        )
        self.assertEqual([], started)

    def test_every_pattern_matching_a_policy_check_or_a_suite_runs(self) -> None:
        suite = Job("tests/test_other.py", "tests/test_other.py", 1, lambda: None)
        code, printed, error, started = self.run_main(
            ["-k", "test_other", "-k", "PullRequestTitlePolicies"], all_jobs=mock.Mock(return_value=[suite])
        )
        self.assertEqual((0, ""), (code, error))
        self.assertEqual([[suite]], started)
        self.assertRegex(printed, r"\ARunning [1-9][0-9]* policy checks beside 1 suite jobs ")

    def test_a_documentation_only_change_runs_the_jobs_it_reaches_and_says_why(self) -> None:
        fence = Job(MARKDOWN_SHELL_JOB, MARKDOWN_SHELL_JOB, 1, print)
        naming = Job("tests/test_names.py", "tests/test_names.py", 1, print)
        selection = mock.Mock(return_value=[fence, naming])
        every = mock.Mock()
        code, printed, _, started = self.run_main(
            [],
            changed_paths=mock.Mock(return_value=["README.md", "docs/skills.md"]),
            documentation_jobs=selection,
            all_jobs=every,
            regression_suites=mock.Mock(return_value=[]),
        )
        self.assertEqual(0, code)
        selection.assert_called_once_with(REPOSITORY_ROOT, ["README.md", "docs/skills.md"], [])
        every.assert_not_called()
        self.assertEqual([[fence, naming]], started)
        self.assertIn(
            "Documentation only: all 2 changed files are documentation since origin/main. Running the policy checks, "
            "the 1 Markdown fence checks, and the 1 suites that name a changed file; pass --full to run every suite.",
            printed,
        )

    def test_full_runs_every_job_without_reading_the_change(self) -> None:
        every = [Job("tests/test_a.py", "tests/test_a.py", 1, print)]
        paths, selection = mock.Mock(return_value=["README.md"]), mock.Mock()
        code, printed, _, started = self.run_main(
            ["--full"], changed_paths=paths, documentation_jobs=selection, all_jobs=mock.Mock(return_value=every)
        )
        self.assertEqual(0, code)
        paths.assert_not_called()
        selection.assert_not_called()
        self.assertEqual([every], started)
        self.assertIn("Full validation: --full was given.", printed)

    def test_changed_runs_the_jobs_the_changed_files_reach(self) -> None:
        reached = Job("tests/deployer/test_rendering_locking.py", "tests/deployer/test_rendering_locking.py", 1, print)
        paths = ["skills/a/scripts/tool.py"]
        selection = mock.Mock(return_value=[reached])
        code, printed, _, started = self.run_main(
            ["--changed"], changed_paths=mock.Mock(return_value=paths), changed_jobs=selection
        )
        self.assertEqual(0, code)
        selection.assert_called_once_with(REPOSITORY_ROOT, paths)
        self.assertEqual([[reached]], started)
        self.assertIn("Changed files: 1 files changed since origin/main.", printed)

    def test_changed_runs_everything_when_the_changed_files_cannot_be_determined(self) -> None:
        # The real git, missing from PATH and the link folders, cannot tell; the run says why and runs every job.
        every = [Job("tests/test_a.py", "tests/test_a.py", 1, print)]
        for argv, line in (
            (["--changed"], "the files changed since origin/main could not be determined"),
            ([], "the changed files could not be determined"),
        ):
            selection = mock.Mock()
            with self.subTest(argv=argv), git_not_on_path():
                code, printed, _, started = self.run_main(
                    argv, changed_jobs=selection, documentation_jobs=selection, all_jobs=mock.Mock(return_value=every)
                )
            self.assertEqual(0, code)
            selection.assert_not_called()
            self.assertEqual([every], started)
            self.assertIn(f"Full validation: {line} ({MISSING_GIT}).", printed)

    def test_changed_with_a_pattern_or_full_is_a_usage_error(self) -> None:
        for argv, message in (
            (["--changed", "-k", "deployer"], "argument --changed: not allowed with argument -k"),
            (["--changed", "--full"], "argument --full: not allowed with argument --changed"),
            (["--full", "-k", "deployer"], "argument --full: not allowed with argument -k"),
        ):
            with self.subTest(argv=argv):
                error = io.StringIO()
                with self.assertRaises(SystemExit) as raised:
                    parse_arguments(argv, error)
                self.assertEqual(2, raised.exception.code)
                self.assertIn(message, error.getvalue())


class PullRequestOnlySelection(unittest.TestCase):
    """--pr-title or --pr-body alone runs the checks that judge the pull request and no suite; with -k, --changed,
    --full, or --shard the run selects as it would without them, as CI's legs, which always pass --shard, do."""

    PULL_REQUEST_CHECKS = frozenset(
        {
            "KnownFailurePullRequestPolicies",
            "PullRequestTitlePolicies",
            "SilentRevertPolicies",
            "UpgradeNotesPolicies",
        }
    )
    LIMITED = (
        "Pull request checks only: KnownFailurePullRequestPolicies, PullRequestTitlePolicies, SilentRevertPolicies, "
        "UpgradeNotesPolicies, since"
    )
    EVERY = (Job("tests/test_a.py", "tests/test_a.py", 1, print),)

    def run_main(self, argv: list[str], **patches: Any) -> tuple[set[str], list[list[Job]], str]:
        """The policy check classes main loaded, the jobs it gave the pool, and its output, with the tools held still,
        every job one suite, and the named functions of this module replaced."""
        loaded: list[unittest.TestSuite] = []
        started: list[list[Job]] = []

        def pool_runs(run: Any, jobs: list[Job], *_: object) -> tuple[unittest.TestResult, PoolResult]:
            started.append(jobs)
            return run(), PoolResult([], [])

        def policies_run(policies: unittest.TestSuite, *_: object) -> unittest.TestResult:
            loaded.append(policies)
            return unittest.TestResult()

        printed = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(run_validation, "tool_versions", return_value={}))
            stack.enter_context(mock.patch.object(run_validation, "report_prerequisite_problems", return_value=False))
            stack.enter_context(mock.patch.object(run_validation, "all_jobs", return_value=list(self.EVERY)))
            stack.enter_context(mock.patch.object(run_validation, "run_policies", side_effect=policies_run))
            stack.enter_context(mock.patch.object(run_validation, "run_beside", side_effect=pool_runs))
            for name, replacement in patches.items():
                stack.enter_context(mock.patch.object(run_validation, name, replacement))
            code = main(argv, out=printed, errors=io.StringIO(), environment={})
        self.assertEqual(0, code)
        return {type(check).__name__ for check in flatten(loaded[0])}, started, printed.getvalue()

    def pull_request_files(self) -> list[list[str]]:
        body, title = REPOSITORY_ROOT / "tests" / "run_validation.py", REPOSITORY_ROOT / "conftest.py"
        return [
            ["--pr-title", str(title)],
            ["--pr-body", str(body)],
            ["--pr-title", str(title), "--pr-body", str(body)],
        ]

    def test_a_title_or_a_body_alone_runs_only_the_pull_request_checks_and_says_so(self) -> None:
        for files in self.pull_request_files():
            with self.subTest(files=files[::2]):
                checks, started, printed = self.run_main(files)
                self.assertEqual(self.PULL_REQUEST_CHECKS, checks)
                self.assertEqual([[]], started)
                self.assertIn(self.LIMITED, printed)

    def test_a_pattern_keeps_its_own_selection(self) -> None:
        for files in self.pull_request_files():
            with self.subTest(files=files[::2]):
                checks, started, printed = self.run_main(["-k", "*", *files])
                self.assertLess(self.PULL_REQUEST_CHECKS, checks)
                self.assertEqual(1, len(started[0]))
                self.assertNotIn("Pull request checks only", printed)

    def test_changed_full_and_a_shard_keep_their_own_selection(self) -> None:
        reached = [Job("tests/test_b.py", "tests/test_b.py", 1, print)]
        for option, patches, jobs in (
            (["--changed"], {"changed_jobs": mock.Mock(return_value=reached)}, reached),
            (["--full"], {}, self.EVERY),
            (["--shard", "1/1"], {"deal_to_leg": lambda policies, jobs, _: (policies, jobs, "Shard 1/1.")}, self.EVERY),
        ):
            for files in self.pull_request_files():
                with self.subTest(option=option, files=files[::2]):
                    checks, started, printed = self.run_main(
                        [*option, *files], changed_paths=mock.Mock(return_value=["deployer/tools.py"]), **patches
                    )
                    self.assertLess(self.PULL_REQUEST_CHECKS, checks)
                    self.assertEqual(list(jobs), started[0])
                    self.assertNotIn("Pull request checks only", printed)


class TerminationReport(unittest.TestCase):
    """main's report of a retried job and a terminated one, read through the stream and the environment main is
    given. This check runs beside the pool, which starts each job from os.environ and prints to sys.stdout, so it
    changes neither."""

    def test_a_retry_and_a_termination_reach_the_output_and_the_step_summary(self) -> None:
        ended = "bash.exe was ended with status 0x80000004 before its first line of output: bash.exe -c 'x'"
        retried, terminated = (Job(label, label, 1, lambda: None) for label in ("suite b.sh", "suite a.sh"))
        pool = PoolResult(
            [
                Failure(
                    terminated, f"Ended before its first line of output on each of 2 attempts:\n  {ended}", False, True
                )
            ],
            [Retry(retried, ended, "passed")],
        )
        stdout, environment = sys.stdout, dict(os.environ)
        seen: list[tuple[bool, bool]] = []

        def pool_runs(*_: object) -> tuple[unittest.TestResult, PoolResult]:
            # What a pool job would start from and print to, at the moment the pool runs.
            seen.append((sys.stdout is stdout, dict(os.environ) == environment))
            return unittest.TestResult(), pool

        printed = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            summary = Path(temporary) / "step summary.md"
            with (
                mock.patch.object(run_validation, "tool_versions", return_value={}),
                mock.patch.object(run_validation, "report_prerequisite_problems", return_value=False),
                mock.patch.object(run_validation, "all_jobs", return_value=[retried, terminated]),
                mock.patch.object(run_validation, "run_beside", side_effect=pool_runs),
            ):
                code = main(
                    ["-k", "suite ?.sh"], out=printed, environment={**environment, "GITHUB_STEP_SUMMARY": str(summary)}
                )
            text = summary.read_text(encoding="utf-8")
        self.assertEqual(1, code)
        self.assertEqual([(True, True)], seen)
        self.assertIs(stdout, sys.stdout)
        self.assertEqual(environment, dict(os.environ))
        self.assertIn(f"\nRETRIED suite b.sh\n{'-' * 70}\n{ended}\nStarted once more, it passed.\n", printed.getvalue())
        self.assertIn(
            f"\nFAILED suite a.sh\n{'-' * 70}\n"
            f"Ended before its first line of output on each of 2 attempts:\n  {ended}\n",
            printed.getvalue(),
        )
        self.assertTrue(
            text.endswith(
                "**validation FAILED**\n- Failed: `suite a.sh`\n\n  ```text\n"
                f"  Ended before its first line of output on each of 2 attempts:\n    {ended}\n  ```\n\n"
                f"- Retried once: `suite b.sh`\n\n  ```text\n  {ended}\n  Started once more, it passed.\n  ```\n\n"
            ),
            text,
        )


class PolicyModules(unittest.TestCase):
    def test_the_policy_modules_are_every_module_beside_the_policies_that_defines_a_check(self) -> None:
        # Each module in tests/validation/ that is not a suite and defines a test case with a check of its own runs in
        # this process; a policy module missing from the list would never run, and a list entry with no check is dead.
        loader = unittest.TestLoader()
        defining = set()
        for path in sorted((REPOSITORY_ROOT / "tests" / "validation").glob("*.py")):
            if is_suite_name(path.name):
                continue
            module = importlib.import_module(path.stem)
            checks = [
                value
                for value in vars(module).values()
                if isinstance(value, type)
                and issubclass(value, unittest.TestCase)
                and value.__module__ == module.__name__
                and loader.getTestCaseNames(value)
            ]
            if checks:
                defining.add(module.__name__)
        self.assertEqual(sorted(defining), sorted(module.__name__ for module in run_validation.POLICY_MODULES))


class RunnerFailures(unittest.TestCase):
    """What ends a run other than its checks: a report a console cannot encode, and a setting the runner cannot use.
    Each reads through the stream and the environment main is given, so neither changes what the pool reads."""

    def test_a_report_the_console_cannot_encode_still_ends_with_its_closing_line_and_step_summary(self) -> None:
        job = Job("suite x.py", "suite x.py", 1, lambda: None)
        report = "Command failed with exit code 1: python x.py\nAssertionError: '\ufffd' != 'e'\n"
        pool = PoolResult([Failure(job, report)], [])
        written = io.BytesIO()
        console = io.TextIOWrapper(written, encoding="cp1252", errors="strict", newline="\n")
        with tempfile.TemporaryDirectory() as temporary:
            summary = Path(temporary) / "step summary.md"
            with (
                mock.patch.object(run_validation, "tool_versions", return_value={}),
                mock.patch.object(run_validation, "report_prerequisite_problems", return_value=False),
                mock.patch.object(run_validation, "all_jobs", return_value=[job]),
                mock.patch.object(run_validation, "run_beside", return_value=(unittest.TestResult(), pool)),
            ):
                code = main(["-k", "suite x.py"], out=console, environment={"GITHUB_STEP_SUMMARY": str(summary)})
            console.flush()
            self.assertEqual(1, code)
            self.assertIn("- Failed: `suite x.py`", summary.read_text(encoding="utf-8"))
        printed = written.getvalue().decode("cp1252")
        self.assertIn("AssertionError: '\\ufffd' != 'e'", printed)
        self.assertIn("validation FAILED (0 policy checks and 1 suite jobs failed).", printed)

    def test_the_closing_line_counts_the_failed_policy_checks_beside_the_failed_suite_jobs(self) -> None:
        class Failing(unittest.TestCase):
            def test_fails(self) -> None:
                self.fail("boom")

            def test_raises(self) -> None:
                raise OSError("denied")

        policies = unittest.TestResult()
        unittest.TestSuite([Failing("test_fails"), Failing("test_raises")]).run(policies)
        job = Job("suite x.py", "suite x.py", 1, lambda: None)
        printed = io.StringIO()
        with (
            mock.patch.object(run_validation, "tool_versions", return_value={}),
            mock.patch.object(run_validation, "report_prerequisite_problems", return_value=False),
            mock.patch.object(run_validation, "all_jobs", return_value=[job]),
            mock.patch.object(run_validation, "run_beside", return_value=(policies, PoolResult([], []))),
        ):
            code = main(["-k", "suite x.py"], out=printed, environment={})
        self.assertEqual(1, code)
        self.assertIn("validation FAILED (2 policy checks and 0 suite jobs failed).", printed.getvalue())

    def test_a_worker_count_that_is_not_a_whole_number_is_a_usage_error_before_any_check_runs(self) -> None:
        error = io.StringIO()
        with mock.patch.object(run_validation, "tool_versions") as versions:
            self.assertEqual(2, main([], errors=error, environment={"VALIDATION_JOBS": "four"}))
        self.assertIn("VALIDATION_JOBS must be a whole number of workers, not 'four'", error.getvalue())
        versions.assert_not_called()

    def test_a_table_the_legs_cannot_be_dealt_from_is_a_usage_error(self) -> None:
        error = io.StringIO()
        with (
            mock.patch.object(run_validation, "tool_versions", return_value={}),
            mock.patch.object(run_validation, "report_prerequisite_problems", return_value=False),
            mock.patch.object(run_validation, "all_jobs", return_value=[Job("suite x.py", "suite x.py", 1, print)]),
            mock.patch.object(
                run_validation, "deal_to_leg", side_effect=ValueError("job_seconds.json cannot be read as JSON")
            ),
        ):
            code = main(["-k", "suite x.py", "--shard", "1/2"], out=io.StringIO(), errors=error)
        self.assertEqual(2, code)
        self.assertIn("the legs cannot be dealt: job_seconds.json cannot be read as JSON", error.getvalue())

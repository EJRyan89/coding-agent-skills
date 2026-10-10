"""Fixture tests for tests/validation/job_selection.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from job_pool import UNSPLIT_SUITE_WEIGHT, Job
from job_selection import (
    JOB_SECONDS,
    MARKDOWN_SHELL_JOB,
    POWERSHELL_JOB,
    changed_paths,
    documentation_jobs,
    job_seconds,
    leg_jobs,
    leg_policies,
    stale_job_seconds_problems,
    suites_naming,
)
from validation_support import REPOSITORY_ROOT


def fixture_job(label: str, weight: float) -> Job:
    return Job(label, label.split(" [")[0], weight, lambda: None)


# Measured seconds for some of LegFixtures.JOBS, by name: a sharded suite's are the sum over its shards.
MEASURED = {"skills/a/scripts/test_a.sh": 40.0, "tests/test_big.py": 6.0, "tests/test_one.py": 3.5}


class LegFixtures(unittest.TestCase):
    JOBS = (
        fixture_job("static shell checks", UNSPLIT_SUITE_WEIGHT),
        fixture_job("skills/a/scripts/test_a.sh", UNSPLIT_SUITE_WEIGHT),
        fixture_job("tests/test_big.py [shard 1/3]", 9),
        fixture_job("tests/test_big.py [shard 2/3]", 9),
        fixture_job("tests/test_big.py [shard 3/3]", 9),
        fixture_job("tests/test_small.py", 4),
        fixture_job("tests/test_one.py", 1),
        fixture_job("tests/test_two.py", 2),
    )

    def test_the_legs_together_run_every_job_once_whatever_the_order_given(self) -> None:
        labels = sorted(job.label for job in self.JOBS)
        for count in range(1, len(self.JOBS) + 2):
            with self.subTest(count=count):
                for seconds in ({}, MEASURED):
                    legs = [
                        sorted(job.label for job in leg_jobs(list(self.JOBS), index, count, seconds))
                        for index in range(count)
                    ]
                    self.assertEqual(labels, sorted(label for leg in legs for label in leg))
                    # The same leg gets the same jobs from any order, so every leg of a run agrees on the dealing.
                    shuffled = [*self.JOBS[3:], *reversed(self.JOBS[:3])]
                    self.assertEqual(
                        legs,
                        [
                            sorted(job.label for job in leg_jobs(shuffled, index, count, seconds))
                            for index in range(count)
                        ],
                    )

    def test_the_heaviest_job_goes_onto_the_lightest_leg_and_an_unsplit_job_counts_as_a_few_tests(self) -> None:
        # An unsplit job's weight of 1,000 only starts it first in the pool; dealt at that weight it would fill a leg
        # alone. At its leg cost the 12-test job goes first, the unsplit job onto the other leg, then the 6-test jobs
        # each onto the lighter leg, the lower index on a tie.
        jobs = [
            fixture_job("static lint check", UNSPLIT_SUITE_WEIGHT),
            fixture_job("tests/test_twelve.py", 12),
            fixture_job("tests/test_six.py [shard 1/2]", 6),
            fixture_job("tests/test_six.py [shard 2/2]", 6),
        ]
        self.assertEqual(
            [
                ["tests/test_twelve.py", "tests/test_six.py [shard 2/2]"],
                ["static lint check", "tests/test_six.py [shard 1/2]"],
            ],
            [[job.label for job in leg_jobs(jobs, index, 2, {})] for index in range(2)],
        )

    def test_a_measured_cost_moves_a_job_to_a_lighter_leg(self) -> None:
        jobs = [
            fixture_job("skills/a/scripts/test_a.sh", UNSPLIT_SUITE_WEIGHT),
            fixture_job("tests/test_twelve.py", 12),
            fixture_job("tests/test_six.py [shard 1/2]", 6),
            fixture_job("tests/test_six.py [shard 2/2]", 6),
        ]

        def legs(seconds: dict[str, float]) -> list[list[str]]:
            return [[job.label for job in leg_jobs(jobs, index, 2, seconds)] for index in range(2)]

        # Counted, the Bash suite is ten typical tests and shares a leg with a shard of the six-test suite.
        self.assertEqual(
            [
                ["tests/test_twelve.py", "tests/test_six.py [shard 2/2]"],
                ["skills/a/scripts/test_a.sh", "tests/test_six.py [shard 1/2]"],
            ],
            legs({}),
        )
        # Measured at 30 seconds it outweighs everything else, so it goes first onto a leg of its own; the suites with
        # no entry keep their counted cost of half a second a test. The six-test suite's measured 4 seconds are 2 a
        # shard, still lighter than the twelve-test suite's counted 6, so both shards join it on the other leg.
        self.assertEqual(
            [
                ["skills/a/scripts/test_a.sh"],
                ["tests/test_twelve.py", "tests/test_six.py [shard 1/2]", "tests/test_six.py [shard 2/2]"],
            ],
            legs({"skills/a/scripts/test_a.sh": 30.0, "tests/test_six.py": 4.0}),
        )

    def test_policy_checks_are_dealt_one_each_in_turn_by_id(self) -> None:
        checks = {f"test_{name}": lambda self: None for name in "edcba"}
        # Nested as the runner loads them, a suite per module holding a suite per class.
        policies = unittest.TestSuite(
            [unittest.defaultTestLoader.loadTestsFromTestCase(type("Policies", (unittest.TestCase,), checks))]
        )
        legs = [
            [
                test.id().rsplit(".", 1)[-1]
                for test in leg_policies(policies, index, 2)
                if isinstance(test, unittest.TestCase)
            ]
            for index in range(2)
        ]
        self.assertEqual([["test_a", "test_c", "test_e"], ["test_b", "test_d"]], legs)
        self.assertEqual([3, 2], [leg_policies(policies, index, 2).countTestCases() for index in range(2)])
        self.assertEqual([], list(leg_policies(policies, 5, 6)))


class JobSecondsFixtures(unittest.TestCase):
    def write_table(self, text: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "job_seconds.json"
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_table_reads_seconds_by_job_name(self) -> None:
        path = self.write_table('{"run": 7, "seconds": {"static lint check": 2.5, "tests/test_a.py": 12}}\n')
        self.assertEqual({"static lint check": 2.5, "tests/test_a.py": 12.0}, job_seconds(path))
        self.assertTrue(JOB_SECONDS.is_file())
        self.assertTrue(job_seconds(JOB_SECONDS))

    def test_a_malformed_table_is_refused(self) -> None:
        for text in (
            "not json",
            "[]",
            '{"run": 7}',
            '{"run": 7, "seconds": []}',
            '{"run": 7, "seconds": {"tests/test_a.py": "12"}}',
            '{"run": 7, "seconds": {"tests/test_a.py": 0}}',
            '{"run": 7, "seconds": {"tests/test_a.py": true}}',
            '{"run": 7, "seconds": {}, "extra": 1}',
        ):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "job_seconds.json"):
                job_seconds(self.write_table(text))

    def test_an_entry_for_a_job_that_no_longer_runs_is_stale(self) -> None:
        seconds = {"tests/test_kept.py": 3.0, "tests/test_gone.py": 4.0, "static lint check": 1.0}
        problems = stale_job_seconds_problems(seconds, {"tests/test_kept.py", "static lint check", "tests/test_new.py"})
        self.assertEqual(1, len(problems))
        self.assertIn("tests/test_gone.py", problems[0])
        self.assertIn("tools/record_job_seconds.py", problems[0])
        self.assertEqual([], stale_job_seconds_problems({"tests/test_kept.py": 3.0}, {"tests/test_kept.py"}))


class JobSelectionFixtures(unittest.TestCase):
    def test_suites_that_name_a_changed_document_still_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            by_path, by_name, unrelated = root / "test_path.py", root / "test_name.py", root / "test_other.py"
            by_path.write_text("SOURCE = 'docs/skills.md'\n", encoding="utf-8")
            by_name.write_text("open('README.md')\n", encoding="utf-8")
            unrelated.write_text("pass\n", encoding="utf-8")
            self.assertEqual(
                [by_path, by_name], suites_naming(["docs/skills.md", "README.md"], [by_path, by_name, unrelated])
            )
            self.assertEqual([], suites_naming(["docs/other.md"], [by_path, by_name, unrelated]))

    def test_changed_files_include_commits_uncommitted_untracked_and_both_sides_of_a_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository with spaces"
            root.mkdir()
            environment = {
                **os.environ,
                "GIT_CONFIG_GLOBAL": str(Path(temporary) / "empty"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            }
            (Path(temporary) / "empty").write_text("", encoding="utf-8")

            def git(*arguments: str) -> None:
                subprocess.run(["git", "-C", str(root), *arguments], env=environment, check=True, capture_output=True)

            for name in ("skills/a/tool.py", "docs/kept.md", "docs/edited.md"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(f"{name}\n", encoding="utf-8")
            git("init", "-q", "-b", "main")
            git("add", ".")
            git("commit", "-q", "-m", "base")
            git("switch", "-q", "-c", "work")
            git("mv", "skills/a/tool.py", "docs/tool.md")
            git("commit", "-q", "-m", "move")
            (root / "docs" / "edited.md").write_text("changed\n", encoding="utf-8")
            (root / "docs" / "new.md").write_text("new\n", encoding="utf-8")
            with mock.patch.dict(os.environ, environment):
                self.assertEqual(
                    ["docs/edited.md", "docs/new.md", "docs/tool.md", "skills/a/tool.py"], changed_paths(root, "main")
                )
                self.assertIsNone(changed_paths(root, "no-such-base"))
                self.assertIsNone(changed_paths(Path(temporary), "main"))

    @staticmethod
    def documentation_failures(files: dict[str, str], changed: list[str]) -> dict[str, str]:
        """Run the jobs a documentation-only change to `changed` selects in a fixture repository; failures by label."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository with spaces"
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            failures: dict[str, str] = {}
            for job in documentation_jobs(root, changed, []):
                try:
                    job.run()
                except AssertionError as error:
                    failures[job.label] = str(error)
            return failures

    def test_a_documentation_only_change_with_a_violating_powershell_fence_fails(self) -> None:
        unused = "function Get-Answer {\n    $unused = 1\n    'answer'\n}\n"
        failures = self.documentation_failures(
            {"docs/install.md": f"# Install\n\n```powershell\n{unused}```\n\n```bash\necho ready\n```\n"},
            ["docs/install.md"],
        )
        self.assertEqual([POWERSHELL_JOB], list(failures))
        # The fence opens on line 3, so the fragment's second line is the file's fifth.
        self.assertIn("docs/install.md:5: PSUseDeclaredVarsMoreThanAssignments (Warning)", failures[POWERSHELL_JOB])

    def test_a_documentation_only_change_with_a_violating_bash_fence_fails(self) -> None:
        failures = self.documentation_failures(
            {"README.md": "# Read me\n\n```powershell\nGet-Date\n```\n\nThen:\n\n```bash\ncd 'some folder'\nls\n```\n"},
            ["README.md"],
        )
        self.assertEqual([MARKDOWN_SHELL_JOB], list(failures))
        self.assertIn("README.md:10:1: warning:", failures[MARKDOWN_SHELL_JOB])
        self.assertIn("[SC2164]", failures[MARKDOWN_SHELL_JOB])
        self.assertIn("never suppress", failures[MARKDOWN_SHELL_JOB])

    def test_a_documentation_only_change_runs_the_fence_checks_only_when_markdown_changed(self) -> None:
        def labels(paths: list[str], suites: list[Path]) -> list[str]:
            # By name, so a suite split into shards appears once.
            return list(dict.fromkeys(job.name for job in documentation_jobs(REPOSITORY_ROOT, paths, suites)))

        # This suite names docs/releasing.md and not the issue template configuration, so only a change to the
        # first runs it.
        naming = REPOSITORY_ROOT / "tests" / "validation" / "test_upgrade_notes.py"
        # No Markdown changed, so no fence can have changed; the Python, type, and skill-script checks never run,
        # since a documentation-only change cannot reach Python or skills/.
        self.assertEqual([], labels([".github/ISSUE_TEMPLATE/config.yml"], [naming]))
        self.assertEqual(
            [POWERSHELL_JOB, MARKDOWN_SHELL_JOB, "tests/validation/test_upgrade_notes.py"],
            labels(["docs/releasing.md", ".github/ISSUE_TEMPLATE/config.yml"], [naming]),
        )
        self.assertEqual([POWERSHELL_JOB, MARKDOWN_SHELL_JOB], labels(["docs/GUIDE.MD"], [naming]))


if __name__ == "__main__":
    unittest.main()

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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

import job_selection
from job_pool import UNSPLIT_SUITE_WEIGHT, Job
from job_selection import (
    FORMAT_JOB,
    JOB_SECONDS,
    LINT_JOB,
    MARKDOWN_SHELL_JOB,
    POWERSHELL_JOB,
    SHELL_JOB,
    change_reach_problems,
    changed_jobs,
    changed_paths,
    changed_static_jobs,
    changed_suites,
    documentation_jobs,
    job_seconds,
    leg_jobs,
    leg_policies,
    stale_job_seconds_problems,
    suites_naming,
)
from suite_discovery import regression_suites
from validation_support import REPOSITORY_ROOT, relative

# Runs no test of its own; the test functions below check through its assertions, as the classes do.
CHECK = unittest.TestCase()


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
        path = self.write_table('{"runs": [7, 9], "seconds": {"static lint check": 2.5, "tests/test_a.py": 12}}\n')
        self.assertEqual({"static lint check": 2.5, "tests/test_a.py": 12.0}, job_seconds(path))
        self.assertTrue(JOB_SECONDS.is_file())
        self.assertTrue(job_seconds(JOB_SECONDS))

    def test_a_table_from_one_run_is_refused_with_the_step_that_rewrites_it(self) -> None:
        path = self.write_table('{"run": 7, "seconds": {"tests/test_a.py": 1}}\n')
        with self.assertRaisesRegex(ValueError, r'one run under "run"; rerun tools/record_job_seconds\.py'):
            job_seconds(path)

    def test_an_entry_for_a_job_that_no_longer_runs_is_stale(self) -> None:
        seconds = {"tests/test_kept.py": 3.0, "tests/test_gone.py": 4.0, "static lint check": 1.0}
        problems = stale_job_seconds_problems(seconds, {"tests/test_kept.py", "static lint check", "tests/test_new.py"})
        self.assertEqual(1, len(problems))
        self.assertIn("tests/test_gone.py", problems[0])
        self.assertIn("tools/record_job_seconds.py", problems[0])
        self.assertEqual([], stale_job_seconds_problems({"tests/test_kept.py": 3.0}, {"tests/test_kept.py"}))


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"runs": [7]}',
        '{"runs": [7], "seconds": []}',
        '{"runs": [7], "seconds": {"tests/test_a.py": "12"}}',
        '{"runs": [7], "seconds": {"tests/test_a.py": 0}}',
        '{"runs": [7], "seconds": {"tests/test_a.py": true}}',
        '{"runs": [7], "seconds": {}, "extra": 1}',
        '{"seconds": {"tests/test_a.py": 1}}',
        '{"runs": [], "seconds": {"tests/test_a.py": 1}}',
        '{"runs": 7, "seconds": {"tests/test_a.py": 1}}',
        '{"runs": [7, 7], "seconds": {"tests/test_a.py": 1}}',
        '{"runs": ["7"], "seconds": {"tests/test_a.py": 1}}',
        '{"runs": [true], "seconds": {"tests/test_a.py": 1}}',
        '{"runs": [0], "seconds": {"tests/test_a.py": 1}}',
        '{"runs": [[7]], "seconds": {"tests/test_a.py": 1}}',
        '{"runs": [7, {"id": 9}], "seconds": {"tests/test_a.py": 1}}',
    ],
)
def test_a_malformed_table_is_refused(tmp_path: Path, text: str) -> None:
    path = tmp_path / "job_seconds.json"
    path.write_text(text, encoding="utf-8")
    with CHECK.assertRaisesRegex(ValueError, "job_seconds.json"):
        job_seconds(path)


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

            def git(*arguments: str) -> None:
                subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)

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
            self.assertEqual(
                ["docs/edited.md", "docs/new.md", "docs/tool.md", "skills/a/tool.py"], changed_paths(root, "main")
            )
            self.assertIsNone(changed_paths(root, "no-such-base"))
            self.assertIsNone(changed_paths(Path(temporary), "main"))

    def test_changed_files_are_the_checkouts_whatever_repository_git_dir_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkout, other = Path(temporary) / "checkout", Path(temporary) / "another repository"
            for root, name in ((checkout, "docs/mine.md"), (other, "skills/a/theirs.py")):
                root.mkdir()

                def git(*arguments: str, root: Path = root) -> None:
                    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)

                git("init", "-q", "-b", "main")
                git("commit", "-q", "--allow-empty", "-m", "base")
                git("switch", "-q", "-c", "work")
                (root / name).parent.mkdir(parents=True)
                (root / name).write_text(f"{name}\n", encoding="utf-8")
                git("add", ".")
                git("commit", "-q", "-m", "change")
            with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
                self.assertEqual(["docs/mine.md"], changed_paths(checkout, "main"))
            # A git that cannot run at all cannot tell either.
            with mock.patch.object(job_selection, "git", side_effect=ValueError("git was not found")):
                self.assertIsNone(changed_paths(checkout, "main"))

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


# A repository tree for ChangedSelectionFixtures, by path.
CHANGED_TREE = {
    "skills/a/SKILL.md": "# a\n",
    "skills/a/scripts/tool.py": "import helper\n",
    "skills/a/scripts/helper.py": "VALUE = 1\n",
    "skills/a/scripts/test_tool.sh": "bash tool.sh\n",
    "skills/b/SKILL.md": "# b\n",
    "skills/b/scripts/test_b.py": "from importlib import import_module\nimport_module('tool')\n",
    "deploy-meta/a.json": "{}\n",
    "tests/deployer/test_rendering_locking.py": "pass\n",
    "tests/deployer/test_bundles_dependencies.py": "pass\n",
    "tests/deployer/test_plan.py": "pass\n",
    "tests/validation/test_toolchain.py": "pass\n",
    "tests/validation/test_upgrade_notes.py": "pass\n",
    "tests/deployer/test_check.py": "pass\n",
    "tests/tools/test_names_the_skill_file.py": "open('skills/a/SKILL.md')\n",
    "tests/tools/test_names_any_skill_file.py": "open('SKILL.md')\n",
    "tests/tools/test_unrelated.py": "import os\n",
    "tests/fixtures/repo/test_fixture.py": "import helper\n",
}


class ChangedSelectionFixtures(unittest.TestCase):
    """What --changed selects: the static checks a changed file's kind reaches, and the suites that name, import, or
    share a skill with a changed file, with those CHANGE_REACHES adds."""

    def selected(self, paths: list[str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository with spaces"
            for name, text in CHANGED_TREE.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, encoding="utf-8")
            suites = [
                root / name
                for name in sorted(CHANGED_TREE)
                if "/test_" in name and not name.startswith("tests/fixtures/")
            ]
            return [
                suite.relative_to(root).as_posix() for suite in changed_suites(root, paths, suites, list(CHANGED_TREE))
            ]

    def test_a_skill_script_reaches_its_importers_its_skill_and_the_rendering_suite(self) -> None:
        # helper.py is imported by tool.py, which test_b.py imports by import_module; test_tool.sh shares their skill.
        # The fixture tree's suite is a review input, never run.
        self.assertEqual(
            [
                "skills/a/scripts/test_tool.sh",
                "skills/b/scripts/test_b.py",
                "tests/deployer/test_rendering_locking.py",
            ],
            self.selected(["skills/a/scripts/helper.py"]),
        )

    def test_a_shared_file_name_matches_by_path_alone(self) -> None:
        # Two files are named SKILL.md, so a suite that names only the bare name does not run for either.
        self.assertEqual(
            [
                "skills/a/scripts/test_tool.sh",
                "tests/deployer/test_rendering_locking.py",
                "tests/tools/test_names_the_skill_file.py",
            ],
            self.selected(["skills/a/SKILL.md"]),
        )

    def test_a_changed_suite_runs_and_a_floor_reaches_the_floor_suites(self) -> None:
        self.assertEqual(["tests/tools/test_unrelated.py"], self.selected(["tests/tools/test_unrelated.py"]))
        self.assertEqual(
            [
                "tests/deployer/test_check.py",
                "tests/validation/test_toolchain.py",
                "tests/validation/test_upgrade_notes.py",
            ],
            self.selected(["requirements-dev.txt"]),
        )
        self.assertEqual(
            [
                "tests/deployer/test_bundles_dependencies.py",
                "tests/deployer/test_check.py",
                "tests/deployer/test_plan.py",
                "tests/deployer/test_rendering_locking.py",
            ],
            self.selected(["deploy.py"]),
        )
        self.assertEqual([], self.selected(["LICENSE"]))

    def test_a_skill_metadata_change_reaches_the_suites_that_deploy_it(self) -> None:
        # No suite names the metadata file or imports it; the deployer reads it when it renders and resolves skills.
        self.assertEqual(
            ["tests/deployer/test_bundles_dependencies.py", "tests/deployer/test_rendering_locking.py"],
            self.selected(["deploy-meta/a.json"]),
        )

    def test_the_static_checks_follow_the_kind_of_file_changed(self) -> None:
        jobs = [
            fixture_job(label, UNSPLIT_SUITE_WEIGHT)
            for label in (SHELL_JOB, POWERSHELL_JOB, MARKDOWN_SHELL_JOB, FORMAT_JOB, LINT_JOB, "type a", "type b")
        ]

        def labels(paths: list[str]) -> list[str]:
            return [job.label for job in changed_static_jobs(paths, jobs, {"type a", "type b"})]

        self.assertEqual([], labels(["LICENSE", "source.json"]))
        self.assertEqual([POWERSHELL_JOB, MARKDOWN_SHELL_JOB], labels(["docs/GUIDE.MD"]))
        self.assertEqual([SHELL_JOB, POWERSHELL_JOB], labels(["skills/a/scripts/run.sh", "tools/x.ps1"]))
        self.assertEqual([FORMAT_JOB, LINT_JOB, "type a", "type b"], labels(["skills/a/scripts/tool.py"]))
        self.assertEqual([FORMAT_JOB, LINT_JOB, "type a", "type b"], labels(["pyproject.toml"]))

    def test_a_reach_that_matches_nothing_is_reported(self) -> None:
        files = [
            "skills/a/scripts/tool.py",
            "deploy-meta/a.json",
            "deployer/tools.py",
            "deploy.py",
            "skills/skill-core/scripts/pdf_tool.py",
            "tests/validation/toolchain.py",
            "conftest.py",
            "requirements-dev.in",
            "requirements-dev.txt",
        ]
        suites = [
            "tests/deployer/test_rendering_locking.py",
            "tests/deployer/test_bundles_dependencies.py",
            "tests/deployer/test_check.py",
            "tests/validation/test_toolchain.py",
            "tests/validation/test_upgrade_notes.py",
        ]
        self.assertEqual([], change_reach_problems(files, suites))
        self.assertEqual(
            [
                "CHANGE_REACHES names requirements-dev.txt, which matches no repository file",
                "CHANGE_REACHES adds tests/validation/test_toolchain.py, which matches no regression suite",
            ],
            change_reach_problems(files[:-1], [suite for suite in suites if "toolchain" not in suite]),
        )

    def test_a_change_to_a_shipped_skill_script_selects_the_rendering_suite_in_this_repository(self) -> None:
        names = {job.name for job in changed_jobs(REPOSITORY_ROOT, ["skills/repo-cleanup/scripts/repo_cleanup.py"])}
        self.assertLessEqual(
            {
                "tests/deployer/test_rendering_locking.py",
                "skills/repo-cleanup/scripts/test_repo_cleanup.py",
                FORMAT_JOB,
                LINT_JOB,
            },
            names,
        )
        self.assertNotIn("tests/deployer/test_plan.py", names)

    def test_a_change_to_the_root_conftest_selects_every_python_suite_and_no_shell_suite(self) -> None:
        names = {job.name for job in changed_jobs(REPOSITORY_ROOT, ["conftest.py"])}
        python_suites = {relative(suite) for suite in regression_suites() if suite.suffix == ".py"}
        self.assertLessEqual(python_suites, names)
        self.assertNotIn("skills/update-coding-agent-skills/scripts/test_update.sh", names)

    def test_a_change_to_a_skills_metadata_selects_the_deployer_suites_that_read_it_in_this_repository(self) -> None:
        names = {job.name for job in changed_jobs(REPOSITORY_ROOT, ["deploy-meta/review-prs.json"])}
        self.assertLessEqual(
            {"tests/deployer/test_rendering_locking.py", "tests/deployer/test_bundles_dependencies.py"}, names
        )
        self.assertNotIn("tests/deployer/test_plan.py", names)

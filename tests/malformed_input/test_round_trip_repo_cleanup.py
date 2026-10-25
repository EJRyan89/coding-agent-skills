"""repo-cleanup's plan, sweep record, and seals: every one its sweep saves passes the reader of the command after it.

A round trip as test_round_trip.py describes, apart from it because it runs git: each branch name in edge_values.py
that git accepts is made a branch of the suite's fixture clone, and the plan build_plan builds for it is saved with its
seal and read back by load_plan unchanged, as the confirmation commands read it. A sweep record holding every
repository state, with edge text in its paths and lines, reads back as write_record saved it.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from edge_values import NAMES, TEXT, sized
from reader_table import REPOSITORY

sys.path.insert(0, str(REPOSITORY / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("repo-cleanup")))

import repo_cleanup as rc
import test_repo_cleanup as cleanup_suite

# The settings conftest.py adds to the global configuration of every home this suite's git runs in.
git_config = cleanup_suite.GIT_CONFIG


def setUpModule() -> None:
    """Build the repositories every test starts from, with Git isolated as each test isolates it."""
    cleanup_suite.build_template()


class PlanRoundTripTests(cleanup_suite.Fixture):
    def made_branch(self, name: str) -> bool:
        """Whether git made a branch of the clone named name, as it does for each name it takes that the platform's
        file names can hold."""
        made = subprocess.run(["git", "-C", str(self.clone), "branch", name, "main"], capture_output=True)
        return made.returncode == 0

    def test_the_plan_a_sweep_saves_for_each_branch_name_git_takes_reads_back_unchanged(self) -> None:
        names = [name for name in NAMES if self.made_branch(name)]
        self.assertIn("\u00a0", names, "git takes a branch named only by a no-break space")
        plan = rc.build_plan(str(self.clone), str(self.repos), rc.Services(gh=self.github))
        self.assertEqual(sorted(names), sorted(branch["name"] for branch in plan["branches"]))
        path = self.root / "plan.json"
        rc.save_plan(path, plan)
        self.assertEqual(plan, rc.load_plan(path))


class SweepRecordRoundTripTests(cleanup_suite.CleanupTestCase):
    def test_a_sweep_record_reads_back_every_repository_and_policy_line_written(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        results = [
            {
                "root": f"/repos/{name}",
                "state": state,
                "plan": None if index % 2 else f"/plans/{name}.json",
                "lines": [f"REPO\t/repos/{name}\t{state}", *TEXT],
            }
            for index, (name, state) in enumerate(zip(sized(NAMES, len(rc.SWEEP_STATES)), rc.SWEEP_STATES, strict=True))
        ]
        entries: list[dict[str, object]] = []
        policy = [f"POLICY\t{text}" for text in TEXT]
        rc.write_record(directory, entries, results, policy)
        self.assertEqual((entries, policy), rc.read_record(directory))
        rc.write_record(directory, entries, [], [])
        self.assertEqual((entries, []), rc.read_record(directory))

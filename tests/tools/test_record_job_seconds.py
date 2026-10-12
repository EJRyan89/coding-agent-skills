"""Regression suite for tools/record_job_seconds.py, over fixture logs with gh replaced by a stub runner."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

from github_client import CommandResult, GitHubClient

from tools import record_job_seconds

LEG_1 = "Shard 1/4 on Python 3.11\tRun repository validation\t2026-10-10T06:17:04.4226827Z "
LEG_2 = "Shard 2/4 on Python 3.11\tUNKNOWN STEP\t2026-10-10T06:17:05.0000000Z "
LEG_3 = "Shard 3/4 on Python 3.11\tRun repository validation\t2026-10-10T06:17:07.0000000Z "
LEG_4 = "Shard 4/4 on Python 3.11\tRun repository validation\t2026-10-10T06:17:08.0000000Z "
LATEST = "Shard 1/4 on Python 3.x\tUNKNOWN STEP\t2026-10-10T06:17:06.0000000Z "
# Lines as gh prints them: the leg, the step, then the timestamp before each line the runner wrote.
LOG = "\n".join(
    [
        f"{LEG_1}Shard 1/4: 16 of 64 policy checks and 2 of 5 suite jobs.",
        f"{LEG_1}ok    0.4s static format check (ruff format --check)",
        f"{LEG_1}ok   55.7s skills/update-coding-agent-skills/scripts/test_update.sh",
        f"{LEG_1}ok   12.0s tests/deployer/test_render.py [shard 1/3]",
        f"{LEG_2}ok    9.5s tests/deployer/test_render.py [shard 2/3]",
        f"{LEG_2}ok    8.0s tests/deployer/test_render.py [shard 3/3]",
        f"{LEG_2}ok    3.2s static type check (mypy skills/skill-core/scripts)",
        # The weekly run's second interpreter reports the same job again, so it counts at its mean.
        f"{LATEST}ok    4.2s static type check (mypy skills/skill-core/scripts)",
        f"{LEG_2}Ran 12 tests in 0.402s",
        f"{LEG_2}OK",
        f"{LEG_2}test_ok (Tests.test_ok) ... ok",
        f"{LEG_2}    ok 3.0s a line a suite printed, indented",
        f"{LEG_2}Shard 2/4: 16 policy checks and 3 suite jobs in 82s: validation passed.",
        f"{LEG_3}Shard 3/4: 16 policy checks and 0 suite jobs in 1s: validation passed.",
        f"{LEG_4}Shard 4/4: 16 policy checks and 0 suite jobs in 1s: validation passed.",
    ]
)
LEGS = (LEG_1, LEG_2, LEG_3, LEG_4)


def log_of(seconds: dict[str, float], legs: Sequence[str] = LEGS) -> str:
    """A log reporting each job on the legs in turn, and a closing line from every leg."""
    lines = [f"{legs[index % len(legs)]}ok {value:5.1f}s {name}" for index, (name, value) in enumerate(seconds.items())]
    return "\n".join([*lines, *(f"{leg}validation passed." for leg in legs)])


class Stub:
    """gh's answers by command, recording each command it is given."""

    def __init__(self, answers: dict[str, str | int]) -> None:
        self.answers = answers
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> CommandResult:
        self.commands.append(list(command))
        joined = " ".join(command)
        for prefix, answer in self.answers.items():
            if joined.startswith(prefix):
                if isinstance(answer, int):
                    return CommandResult(answer, "", "HTTP 404: Not Found")
                return CommandResult(0, answer, "")
        raise AssertionError(f"unexpected command: {joined}")


SUCCESS = json.dumps({"workflowName": "Validation", "status": "completed", "conclusion": "success"})


class ParseTests(unittest.TestCase):
    def test_a_suites_shards_are_summed_and_a_job_reported_twice_counts_at_its_mean(self) -> None:
        self.assertEqual(
            {
                "static format check (ruff format --check)": 0.4,
                "skills/update-coding-agent-skills/scripts/test_update.sh": 55.7,
                "tests/deployer/test_render.py": 29.5,
                "static type check (mypy skills/skill-core/scripts)": 3.7,
            },
            record_job_seconds.job_seconds_from_log(LOG),
        )

    def test_the_table_is_written_in_name_order_whatever_the_order_found(self) -> None:
        text = record_job_seconds.render_table([9, 7], {"tests/b.py": 2.0, "static lint check": 1.5, "tests/a.py": 3.0})
        self.assertEqual(
            '{\n  "runs": [\n    7,\n    9\n  ],\n  "seconds": {\n    "static lint check": 1.5,\n'
            '    "tests/a.py": 3.0,\n    "tests/b.py": 2.0\n  }\n}\n',
            text,
        )

    def test_a_job_takes_its_median_across_the_runs_that_report_it(self) -> None:
        per_run = {
            1: {"tests/a.py": 10.0, "tests/b.py": 5.0},
            2: {"tests/a.py": 80.0, "tests/b.py": 6.0},
            3: {"tests/a.py": 12.0, "tests/b.py": 7.0},
        }
        self.assertEqual({"tests/a.py": 12.0, "tests/b.py": 6.0}, record_job_seconds.combine(per_run))
        # An even count takes the middle pair's mean, rounded to the table's tenth.
        for second, expected in ((12.0, 11.0), (10.4, 10.2)):
            self.assertEqual(
                {"tests/a.py": expected},
                record_job_seconds.combine({1: {"tests/a.py": 10.0}, 2: {"tests/a.py": second}}),
            )

    def test_a_job_seen_in_some_runs_takes_the_median_of_the_runs_that_have_it(self) -> None:
        per_run = {
            1: {"tests/a.py": 10.0},
            2: {"tests/a.py": 20.0, "tests/new.py": 40.0},
            3: {"tests/a.py": 30.0, "tests/new.py": 60.0},
        }
        self.assertEqual({"tests/a.py": 20.0, "tests/new.py": 50.0}, record_job_seconds.combine(per_run))

    def test_a_job_the_newest_run_does_not_report_is_left_out(self) -> None:
        # A suite renamed after run 2, or removed: the old name is in the older runs only.
        per_run = {
            1: {"tests/old.py": 10.0, "tests/kept.py": 1.0},
            2: {"tests/old.py": 12.0, "tests/kept.py": 2.0},
            3: {"tests/renamed.py": 11.0, "tests/kept.py": 3.0},
        }
        self.assertEqual({"tests/renamed.py": 11.0, "tests/kept.py": 2.0}, record_job_seconds.combine(per_run))

    def test_the_newest_run_is_the_highest_id_whatever_the_order_given(self) -> None:
        per_run = {9: {"tests/new.py": 1.0}, 3: {"tests/old.py": 1.0}, 5: {"tests/old.py": 1.0}}
        self.assertEqual({"tests/new.py": 1.0}, record_job_seconds.combine(per_run))

    def test_a_log_is_complete_only_when_it_reports_every_leg_of_the_matrix(self) -> None:
        self.assertEqual("", record_job_seconds.leg_problem(LOG))
        self.assertEqual("", record_job_seconds.leg_problem(log_of({"tests/a.py": 1.0})))
        self.assertEqual("lacks leg 3/4, 4/4", record_job_seconds.leg_problem(log_of({"tests/a.py": 1.0}, LEGS[:2])))
        gap = log_of({"tests/a.py": 1.0}, (LEG_1, *LEGS[2:]))
        self.assertEqual("lacks leg 2/4", record_job_seconds.leg_problem(gap))
        self.assertEqual("names no one set of legs", record_job_seconds.leg_problem("no leg\tstep\ttext"))


class MainTests(unittest.TestCase):
    def run_main(self, arguments: list[str], stub: Stub) -> tuple[int, str, str, Path]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        table = Path(directory.name) / "job_seconds.json"
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = record_job_seconds.main(arguments, client=GitHubClient(runner=stub), table=table)
        return code, output.getvalue(), errors.getvalue(), table

    def runs_stub(self, logs: dict[int, str]) -> Stub:
        answers: dict[str, str | int] = {}
        for run, log in logs.items():
            answers[f"gh run view {run} --json"] = SUCCESS
            answers[f"gh run view {run} --log"] = log
        return Stub(answers)

    def test_a_named_run_rewrites_the_table_from_its_logs(self) -> None:
        stub = Stub({"gh run view 38 --json": SUCCESS, "gh run view 38 --log": LOG})
        code, output, _, table = self.run_main(["38"], stub)
        self.assertEqual(0, code)
        self.assertEqual(
            {
                "runs": [38],
                "seconds": {
                    "skills/update-coding-agent-skills/scripts/test_update.sh": 55.7,
                    "static format check (ruff format --check)": 0.4,
                    "static type check (mypy skills/skill-core/scripts)": 3.7,
                    "tests/deployer/test_render.py": 29.5,
                },
            },
            json.loads(table.read_text(encoding="utf-8")),
        )
        self.assertIn("Recorded 4 jobs, 89.3 job-seconds, as the median of 1 run (38) in", output)
        # Line feeds on every platform, so a rerun on another machine changes only the seconds.
        self.assertNotIn(b"\r", table.read_bytes())

    def test_named_runs_are_combined_by_median_and_recorded_by_id(self) -> None:
        stub = self.runs_stub(
            {
                30: log_of({"tests/a.py": 10.0, "tests/b.py": 4.0}),
                20: log_of({"tests/a.py": 90.0, "tests/b.py": 6.0}),
                10: log_of({"tests/a.py": 14.0, "tests/b.py": 5.0}),
            }
        )
        code, output, _, table = self.run_main(["30", "20", "10", "20"], stub)
        self.assertEqual(0, code)
        self.assertEqual(
            {"runs": [10, 20, 30], "seconds": {"tests/a.py": 14.0, "tests/b.py": 5.0}},
            json.loads(table.read_text(encoding="utf-8")),
        )
        self.assertIn("Recorded 2 jobs, 19.0 job-seconds, as the median of 3 runs (10, 20, 30) in", output)
        self.assertFalse(any(command[:3] == ["gh", "run", "list"] for command in stub.commands))

    def test_a_job_some_runs_lack_takes_the_median_of_the_runs_that_have_it(self) -> None:
        stub = self.runs_stub(
            {
                1: log_of({"tests/a.py": 10.0}),
                2: log_of({"tests/a.py": 20.0, "tests/new.py": 8.0}),
                3: log_of({"tests/a.py": 30.0, "tests/new.py": 12.0}),
            }
        )
        code, _, _, table = self.run_main(["1", "2", "3"], stub)
        self.assertEqual(0, code)
        seconds = json.loads(table.read_text(encoding="utf-8"))["seconds"]
        self.assertEqual({"tests/a.py": 20.0, "tests/new.py": 10.0}, seconds)

    def test_a_job_renamed_or_removed_after_the_older_runs_is_not_recorded(self) -> None:
        stub = self.runs_stub(
            {
                1: log_of({"tests/old.py": 10.0, "tests/gone.py": 3.0, "tests/kept.py": 5.0}),
                2: log_of({"tests/renamed.py": 11.0, "tests/kept.py": 7.0}),
            }
        )
        code, _, _, table = self.run_main(["1", "2"], stub)
        self.assertEqual(0, code)
        seconds = json.loads(table.read_text(encoding="utf-8"))["seconds"]
        self.assertEqual({"tests/renamed.py": 11.0, "tests/kept.py": 6.0}, seconds)

    def test_without_runs_the_latest_successful_main_push_runs_are_read(self) -> None:
        stub = self.runs_stub({43: LOG, 42: LOG, 41: LOG})
        stub.answers["gh run list"] = json.dumps([{"databaseId": 43}, {"databaseId": 42}, {"databaseId": 41}])
        code, output, _, table = self.run_main([], stub)
        self.assertEqual(0, code)
        self.assertEqual([41, 42, 43], json.loads(table.read_text(encoding="utf-8"))["runs"])
        self.assertIn("as the median of 3 runs (41, 42, 43)", output)
        listing = stub.commands[0]
        # The default count is pinned here against the literal, not the constant under test.
        for option, value in (
            ("--workflow", "validate.yml"),
            ("--branch", "main"),
            ("--event", "push"),
            ("--status", "success"),
            ("--limit", "5"),
        ):
            self.assertEqual(value, listing[listing.index(option) + 1])

    def test_count_sets_how_many_of_the_latest_runs_are_listed(self) -> None:
        stub = self.runs_stub({50: LOG})
        stub.answers["gh run list"] = json.dumps([{"databaseId": 50}])
        code, _, _, _ = self.run_main(["--count", "1"], stub)
        self.assertEqual(0, code)
        self.assertEqual("1", stub.commands[0][stub.commands[0].index("--limit") + 1])
        for arguments in (["--count", "0"], ["--count", "-2"]):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                self.run_main(arguments, Stub({}))

    def test_a_run_that_did_not_pass_or_is_another_workflow_is_refused_and_the_table_is_left_alone(self) -> None:
        for state in (
            {"workflowName": "Validation", "status": "completed", "conclusion": "failure"},
            # A cancelled leg ends the run as cancelled.
            {"workflowName": "Validation", "status": "completed", "conclusion": "cancelled"},
            {"workflowName": "Validation", "status": "in_progress", "conclusion": ""},
            {"workflowName": "Deployment Smoke Test", "status": "completed", "conclusion": "success"},
            # A run from before the workflow was renamed carries its old name.
            {"workflowName": "Validate", "status": "completed", "conclusion": "success"},
        ):
            with self.subTest(state=state):
                stub = Stub({"gh run view 9 --json": json.dumps(state)})
                code, _, errors, table = self.run_main(["9"], stub)
                self.assertEqual(2, code)
                self.assertIn("record from completed, successful Validation runs", errors)
                self.assertFalse(table.exists())
                self.assertFalse(any("--log" in command for command in stub.commands))

    def test_one_refused_run_among_good_ones_refuses_the_record(self) -> None:
        stub = self.runs_stub({1: LOG, 3: LOG})
        stub.answers["gh run view 2 --json"] = json.dumps(
            {"workflowName": "Validation", "status": "completed", "conclusion": "failure"}
        )
        code, _, errors, table = self.run_main(["1", "2", "3"], stub)
        self.assertEqual((2, False), (code, table.exists()))
        self.assertIn("run 2 is 'Validation', completed, failure", errors)

    def test_a_successful_run_whose_log_lacks_a_leg_is_refused(self) -> None:
        # A rerun of one failed leg ends successful but may log only the legs of its last attempt.
        stub = self.runs_stub({7: log_of({"tests/a.py": 1.0, "tests/b.py": 2.0}, LEGS[:3])})
        code, _, errors, table = self.run_main(["7"], stub)
        self.assertEqual((2, False), (code, table.exists()))
        self.assertIn("run 7's log lacks leg 4/4", errors)

    def test_a_run_whose_log_gh_no_longer_holds_refuses_the_record_and_names_the_run(self) -> None:
        stub = self.runs_stub({1: LOG})
        stub.answers["gh run view 2 --json"] = SUCCESS
        stub.answers["gh run view 2 --log"] = 1
        code, _, errors, table = self.run_main(["1", "2"], stub)
        self.assertEqual((2, False), (code, table.exists()))
        self.assertIn("Cannot read the validate runs", errors)
        self.assertIn("Not Found", errors)

    def test_a_run_gh_cannot_read_or_with_no_passing_jobs_writes_nothing(self) -> None:
        code, _, errors, table = self.run_main(["5"], Stub({"gh run view 5": 1}))
        self.assertEqual((2, False), (code, table.exists()))
        self.assertIn("Cannot read the validate runs", errors)
        code, _, errors, table = self.run_main([], Stub({"gh run list": "[]"}))
        self.assertEqual((2, False), (code, table.exists()))
        self.assertIn("no successful push run of validate.yml on main", errors)
        quiet = Stub({"gh run view 6 --json": SUCCESS, "gh run view 6 --log": f"{LEG_1}Shard 1/4: nothing printed"})
        code, _, errors, table = self.run_main(["6"], quiet)
        self.assertEqual((2, False), (code, table.exists()))
        self.assertIn("run 6 reported no passing jobs", errors)

    def test_the_table_it_writes_is_the_one_validation_reads(self) -> None:
        table = record_job_seconds.TABLE.relative_to(record_job_seconds.REPOSITORY_ROOT)
        self.assertEqual(Path("tests/validation/job_seconds.json"), table)
        self.assertTrue(record_job_seconds.TABLE.is_file())

    def test_the_release_procedure_refreshes_the_table_before_tagging(self) -> None:
        text = (record_job_seconds.REPOSITORY_ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
        before_tagging = text.split("## Before tagging", 1)[1].split("\n## ", 1)[0]
        self.assertIn("python -B tools/record_job_seconds.py", before_tagging)


if __name__ == "__main__":
    unittest.main()

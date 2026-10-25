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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from github_client import CommandResult, GitHubClient
from json_writer import JsonWriteRefused

from tools import record_job_seconds

LEG_1 = "Windows shard 1/4, Python 3.11\tRun repository validation\t2026-10-10T06:17:04.4226827Z "
LEG_2 = "Windows shard 2/4, Python 3.11\tUNKNOWN STEP\t2026-10-10T06:17:05.0000000Z "
LEG_3 = "Windows shard 3/4, Python 3.11\tRun repository validation\t2026-10-10T06:17:07.0000000Z "
LEG_4 = "Windows shard 4/4, Python 3.11\tRun repository validation\t2026-10-10T06:17:08.0000000Z "
LATEST = "Windows shard 1/4, Python 3.x\tUNKNOWN STEP\t2026-10-10T06:17:06.0000000Z "
LINUX_1 = "Linux shard 1/2, Python 3.11\tRun repository validation\t2026-10-10T06:17:09.0000000Z "
LINUX_2 = "Linux shard 2/2, Python 3.11\tUNKNOWN STEP\t2026-10-10T06:17:09.5000000Z "
# The job that ran every job on Linux unsharded before Linux was dealt: one leg of one.
UNSHARDED = "Validation on Linux\tRun repository validation\t2026-10-09T06:17:09.0000000Z "
# The job beside the legs, which runs none.
AGGREGATE = "Validation\tRequire every leg to pass\t2026-10-10T06:17:10.0000000Z "
# Lines as gh prints them: the leg, the step, then the timestamp before each line the runner wrote.
LOG = "\n".join(
    [
        f"{LEG_1}Shard 1/4: 16 of 64 policy checks and 2 of 5 suite jobs.",
        f"{LEG_1}ok    0.4s static format check (ruff format --check)",
        f"{LEG_1}ok   55.7s skills/update-coding-agent-skills/scripts/test_update.sh",
        f"{LEG_1}ok   12.0s tests/deployer/test_render.py [shard 1/3]",
        f"{LEG_2}ok    9.5s tests/deployer/test_render.py [shard 2/3]",
        f"{LEG_2}ok    8.0s tests/deployer/test_render.py [shard 3/3]",
        f"{LEG_2}ok    3.2s static type check (mypy skills/ejryan89-skill-core/scripts)",
        # The weekly run's second interpreter reports the same job again, so it counts at its mean.
        f"{LATEST}ok    4.2s static type check (mypy skills/ejryan89-skill-core/scripts)",
        f"{LEG_2}Ran 12 tests in 0.402s",
        f"{LEG_2}OK",
        f"{LEG_2}test_ok (Tests.test_ok) ... ok",
        f"{LEG_2}    ok 3.0s a line a suite printed, indented",
        f"{LEG_2}Shard 2/4: 16 policy checks and 3 suite jobs in 82s: validation passed.",
        f"{LEG_3}Shard 3/4: 16 policy checks and 0 suite jobs in 1s: validation passed.",
        f"{LEG_4}Shard 4/4: 16 policy checks and 0 suite jobs in 1s: validation passed.",
        # The Linux legs' seconds are Linux's, never added to a Windows leg's.
        f"{LINUX_1}ok    2.0s tests/deployer/test_render.py [shard 1/3]",
        f"{LINUX_2}ok    1.5s tests/deployer/test_render.py [shard 2/3]",
        f"{LINUX_2}ok    1.0s tests/deployer/test_render.py [shard 3/3]",
        f"{LINUX_1}ok    9.0s static type check (mypy skills/ejryan89-skill-core/scripts)",
        f"{LINUX_2}ok    7.0s skills/update-coding-agent-skills/scripts/test_update.sh",
        f"{AGGREGATE}ok    1.0s a line the aggregate job printed",
    ]
)
LEGS = (LEG_1, LEG_2, LEG_3, LEG_4)
LINUX_LEGS = (LINUX_1, LINUX_2)
WINDOWS_SECONDS = {
    "static format check (ruff format --check)": 0.4,
    "skills/update-coding-agent-skills/scripts/test_update.sh": 55.7,
    "tests/deployer/test_render.py": 29.5,
    "static type check (mypy skills/ejryan89-skill-core/scripts)": 3.7,
}
LINUX_SECONDS = {
    "tests/deployer/test_render.py": 4.5,
    "static type check (mypy skills/ejryan89-skill-core/scripts)": 9.0,
    "skills/update-coding-agent-skills/scripts/test_update.sh": 7.0,
}
# The legs as the workflow named them before the Linux job joined the required check.
OLD_LEGS = tuple(
    f"Shard {index}/4 on Python 3.11\tRun repository validation\t2026-10-09T06:17:0{index}.0000000Z "
    for index in range(1, 5)
)
# The Linux legs as the workflow named them before they were named as the Windows legs are.
OLD_LINUX_LEGS = tuple(
    f"Validation on Linux, shard {index}/2\tRun repository validation\t2026-10-09T06:17:0{index}.0000000Z "
    for index in range(1, 3)
)


def log_of(
    seconds: dict[str, float],
    legs: Sequence[str] = LEGS,
    linux_legs: Sequence[str] = LINUX_LEGS,
    linux: dict[str, float] | None = None,
) -> str:
    """A log reporting each job on the Windows legs in turn and, at `linux`'s seconds (a tenth of the Windows seconds
    when None), on the Linux legs in turn, and a closing line from every leg."""
    linux = {name: value / 10 for name, value in seconds.items()} if linux is None else linux
    lines = [
        f"{platform_legs[index % len(platform_legs)]}ok {value:5.1f}s {name}"
        for platform_legs, jobs in ((legs, seconds), (linux_legs, linux))
        if platform_legs
        for index, (name, value) in enumerate(jobs.items())
    ]
    return "\n".join([*lines, *(f"{leg}validation passed." for leg in (*legs, *linux_legs))])


def both(seconds: dict[str, float]) -> dict[str, dict[str, float]]:
    """The table's seconds for a log_of log: the Windows seconds, and a tenth of them on Linux."""
    return {"Linux": {name: round(value / 10, 1) for name, value in seconds.items()}, "Windows": seconds}


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


def run_record(arguments: list[str], stub: Stub, directory: Path) -> tuple[int, str, str, Path]:
    """Run the command with its table in directory, returning its exit code, its output, its errors, and the table."""
    table = directory / "job_seconds.json"
    output, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        code = record_job_seconds.main(arguments, client=GitHubClient(runner=stub), table=table)
    return code, output.getvalue(), errors.getvalue(), table


def runs_stub(logs: dict[int, str]) -> Stub:
    answers: dict[str, str | int] = {}
    for run, log in logs.items():
        answers[f"gh run view {run} --json"] = SUCCESS
        answers[f"gh run view {run} --log"] = log
    return Stub(answers)


class ParseTests(unittest.TestCase):
    def test_a_suites_shards_are_summed_and_a_job_reported_twice_counts_at_its_mean_on_each_platform(self) -> None:
        self.assertEqual(
            {"Windows": WINDOWS_SECONDS, "Linux": LINUX_SECONDS}, record_job_seconds.job_seconds_from_log(LOG)
        )

    def test_a_job_the_pool_retried_is_recorded_under_its_own_name(self) -> None:
        # The note job_pool.py puts after a job it started once more after a termination is no part of the job's name,
        # so the retried shard's seconds join its suite's and the entry names a job validation runs.
        retried = "\n".join(
            [
                f"{LEG_1}ok   12.0s tests/deployer/test_render.py [shard 1/3] (retried after status 0x80000004)",
                f"{LEG_2}ok    9.5s tests/deployer/test_render.py [shard 2/3]",
                f"{LEG_3}ok    5.0s skills/update-coding-agent-skills/scripts/test_update.sh "
                "(retried after status 0xC0000142)",
                f"{LINUX_1}ok    2.0s tests/deployer/test_render.py (retried after status SIGKILL)",
            ]
        )
        self.assertEqual(
            {
                "Windows": {
                    "tests/deployer/test_render.py": 21.5,
                    "skills/update-coding-agent-skills/scripts/test_update.sh": 5.0,
                },
                "Linux": {"tests/deployer/test_render.py": 2.0},
            },
            record_job_seconds.job_seconds_from_log(retried),
        )
        # A label that only resembles the note keeps it.
        self.assertEqual("tests/a.py (retried)", record_job_seconds.job_name("tests/a.py (retried)"))

    def test_the_table_is_written_in_name_order_whatever_the_order_found(self) -> None:
        text = record_job_seconds.render_table(
            [9, 7],
            {"Windows": {"tests/b.py": 2.0, "static lint check": 1.5, "tests/a.py": 3.0}, "Linux": {"tests/a.py": 0.5}},
        )
        self.assertEqual(
            '{\n  "runs": [\n    7,\n    9\n  ],\n  "seconds": {\n    "Linux": {\n      "tests/a.py": 0.5\n    },\n'
            '    "Windows": {\n      "static lint check": 1.5,\n      "tests/a.py": 3.0,\n      "tests/b.py": 2.0\n'
            "    }\n  }\n}\n",
            text,
        )

    def test_a_time_json_cannot_hold_is_refused_at_the_table_by_name(self) -> None:
        for seconds in (float("nan"), float("inf")):
            with (
                self.subTest(seconds=seconds),
                self.assertRaisesRegex(JsonWriteRefused, "^the value holds NaN or an infinity"),
            ):
                record_job_seconds.render_table([7], {"Linux": {"tests/a.py": seconds}})

    def test_only_a_leg_s_three_field_ok_line_counts(self) -> None:
        # A line with the leg and the text but no step, and a line with a step but no leg, are no leg's result.
        log = "\n".join(
            [
                "Windows shard 1/4, Python 3.11\t2026-10-10T06:17:04Z ok   5.0s tests/a.py",
                "Another job\tRun repository validation\t2026-10-10T06:17:04Z ok   6.0s tests/a.py",
                f"{LEG_1}ok   1.0s tests/b.py",
            ]
        )
        self.assertEqual({"Windows": {"tests/b.py": 1.0}}, record_job_seconds.job_seconds_from_log(log))

    def test_seconds_keep_one_decimal_and_a_job_that_rounds_to_zero_is_dropped(self) -> None:
        log = "\n".join(
            [
                # Reported twice, the job counts at its mean, 1.25, which the table keeps to one decimal.
                f"{LEG_1}ok   1.0s tests/a.py",
                f"{LATEST}ok   1.5s tests/a.py",
                # Its mean, a thirtieth of a second, rounds to zero: a job's seconds are above zero or not recorded.
                f"{LEG_2}ok   0.1s tests/b.py",
                f"{LEG_3}ok   0.0s tests/b.py",
                f"{LEG_4}ok   0.0s tests/b.py",
                # Every job of a platform rounds to zero, so the platform has no seconds.
                f"{LINUX_1}ok   0.0s tests/b.py",
            ]
        )
        self.assertEqual({"Windows": {"tests/a.py": 1.2}}, record_job_seconds.job_seconds_from_log(log))

    def test_the_platforms_recorded_are_those_the_deal_reads(self) -> None:
        # Pinned against the literals: the platforms the deployer supports, by the names platform_support gives them.
        self.assertEqual(("Linux", "Windows"), record_job_seconds.PLATFORMS)

    def test_a_job_takes_its_median_across_the_runs_that_report_it_on_its_platform(self) -> None:
        per_run = {
            1: {"Windows": {"tests/a.py": 10.0, "tests/b.py": 5.0}, "Linux": {"tests/a.py": 1.0}},
            2: {"Windows": {"tests/a.py": 80.0, "tests/b.py": 6.0}, "Linux": {"tests/a.py": 3.0}},
            3: {"Windows": {"tests/a.py": 12.0, "tests/b.py": 7.0}, "Linux": {"tests/a.py": 2.0}},
        }
        self.assertEqual(
            {"Windows": {"tests/a.py": 12.0, "tests/b.py": 6.0}, "Linux": {"tests/a.py": 2.0}},
            record_job_seconds.combine(per_run),
        )
        # An even count takes the middle pair's mean, rounded to the table's tenth.
        for second, expected in ((12.0, 11.0), (10.4, 10.2)):
            self.assertEqual(
                {"Linux": {"tests/a.py": expected}},
                record_job_seconds.combine({1: {"Linux": {"tests/a.py": 10.0}}, 2: {"Linux": {"tests/a.py": second}}}),
            )

    def test_a_job_seen_in_some_runs_takes_the_median_of_the_runs_that_have_it(self) -> None:
        per_run = {
            1: {"Windows": {"tests/a.py": 10.0}, "Linux": {"tests/new.py": 9.0}},
            2: {"Windows": {"tests/a.py": 20.0, "tests/new.py": 40.0}, "Linux": {}},
            3: {"Windows": {"tests/a.py": 30.0, "tests/new.py": 60.0}, "Linux": {"tests/new.py": 5.0}},
        }
        self.assertEqual(
            {"Windows": {"tests/a.py": 20.0, "tests/new.py": 50.0}, "Linux": {"tests/new.py": 7.0}},
            record_job_seconds.combine(per_run),
        )

    def test_a_job_the_newest_run_does_not_report_is_left_out(self) -> None:
        # A suite renamed after run 2, or removed: the old name is in the older runs only.
        per_run = {
            1: {"Windows": {"tests/old.py": 10.0, "tests/kept.py": 1.0}},
            2: {"Windows": {"tests/old.py": 12.0, "tests/kept.py": 2.0}},
            3: {"Windows": {"tests/renamed.py": 11.0, "tests/kept.py": 3.0}},
        }
        self.assertEqual(
            {"Windows": {"tests/renamed.py": 11.0, "tests/kept.py": 2.0}}, record_job_seconds.combine(per_run)
        )

    def test_a_median_keeps_one_decimal_and_a_run_without_the_platform_is_passed_over(self) -> None:
        per_run: dict[int, dict[str, dict[str, float]]] = {
            1: {"Windows": {"tests/a.py": 9.0}},
            2: {"Windows": {"tests/a.py": 1.0}, "Linux": {"tests/a.py": 1.0}},
            3: {"Windows": {"tests/a.py": 1.5}, "Linux": {"tests/a.py": 1.5}},
        }
        # Linux's median, 1.25, is of the two runs that report Linux, kept to one decimal.
        self.assertEqual(
            {"Windows": {"tests/a.py": 1.5}, "Linux": {"tests/a.py": 1.2}}, record_job_seconds.combine(per_run)
        )

    def test_the_newest_run_is_the_highest_id_whatever_the_order_given(self) -> None:
        per_run: dict[int, dict[str, dict[str, float]]] = {
            9: {"Linux": {"tests/new.py": 1.0}},
            3: {"Linux": {"tests/old.py": 1.0}},
            5: {"Linux": {}},
        }
        self.assertEqual({"Linux": {"tests/new.py": 1.0}}, record_job_seconds.combine(per_run))

    def test_a_log_is_complete_only_when_it_reports_every_leg_of_each_platform_s_matrix(self) -> None:
        self.assertEqual("", record_job_seconds.leg_problem(LOG))
        self.assertEqual("", record_job_seconds.leg_problem(log_of({"tests/a.py": 1.0})))
        self.assertEqual(
            "lacks Windows leg 3/4, 4/4", record_job_seconds.leg_problem(log_of({"tests/a.py": 1.0}, LEGS[:2]))
        )
        gap = log_of({"tests/a.py": 1.0}, (LEG_1, *LEGS[2:]))
        self.assertEqual("lacks Windows leg 2/4", record_job_seconds.leg_problem(gap))
        self.assertEqual("lacks Windows leg 1/4", record_job_seconds.leg_problem(log_of({"tests/a.py": 1.0}, LEGS[1:])))
        self.assertEqual(
            "lacks Linux leg 1/2",
            record_job_seconds.leg_problem(log_of({"tests/a.py": 1.0}, linux_legs=LINUX_LEGS[1:])),
        )
        self.assertEqual(
            "lacks Linux leg 2/2",
            record_job_seconds.leg_problem(log_of({"tests/a.py": 1.0}, linux_legs=LINUX_LEGS[:1])),
        )
        self.assertEqual(
            "names no one set of Linux legs", record_job_seconds.leg_problem(log_of({"tests/a.py": 1.0}, linux_legs=()))
        )
        self.assertEqual(
            "names no one set of Linux legs; names no one set of Windows legs",
            record_job_seconds.leg_problem("no leg\tstep\ttext"),
        )
        # The unsharded job and the Linux legs in one log are two sets of Linux legs.
        self.assertEqual(
            "names no one set of Linux legs",
            record_job_seconds.leg_problem(f"{log_of({'tests/a.py': 1.0})}\n{UNSHARDED}ok 1.0s tests/a.py"),
        )
        # The aggregate job is no leg.
        self.assertEqual(set(), record_job_seconds.legs_seen(f"{AGGREGATE}ok 1.0s x"))

    def test_a_leg_under_the_name_it_had_before_the_rename_is_a_leg(self) -> None:
        old = log_of({"tests/a.py": 4.0, "tests/b.py": 2.5}, OLD_LEGS)
        windows = {leg for leg in record_job_seconds.legs_seen(old) if leg[0] == "Windows"}
        self.assertEqual({("Windows", index, 4) for index in range(1, 5)}, windows)
        self.assertEqual("", record_job_seconds.leg_problem(old))
        self.assertEqual(both({"tests/a.py": 4.0, "tests/b.py": 2.5}), record_job_seconds.job_seconds_from_log(old))
        self.assertEqual(
            "lacks Windows leg 4/4", record_job_seconds.leg_problem(log_of({"tests/a.py": 1.0}, OLD_LEGS[:3]))
        )
        self.assertEqual(("Windows", 2, 4), record_job_seconds.leg_of("Windows shard 2/4, Python 3.x"))
        self.assertEqual(("Windows", 3, 4), record_job_seconds.leg_of("Shard 3/4 on Python 3.11"))
        self.assertEqual(("Linux", 2, 2), record_job_seconds.leg_of("Linux shard 2/2, Python 3.11"))
        self.assertEqual(("Linux", 2, 2), record_job_seconds.leg_of("Validation on Linux, shard 2/2"))

    def test_a_linux_leg_under_the_name_it_had_before_its_rename_is_a_leg(self) -> None:
        old = log_of({"tests/a.py": 40.0}, linux_legs=OLD_LINUX_LEGS)
        linux = {leg for leg in record_job_seconds.legs_seen(old) if leg[0] == "Linux"}
        self.assertEqual({("Linux", 1, 2), ("Linux", 2, 2)}, linux)
        self.assertEqual("", record_job_seconds.leg_problem(old))
        self.assertEqual(both({"tests/a.py": 40.0}), record_job_seconds.job_seconds_from_log(old))
        self.assertEqual(
            "lacks Linux leg 2/2",
            record_job_seconds.leg_problem(log_of({"tests/a.py": 1.0}, linux_legs=OLD_LINUX_LEGS[:1])),
        )

    def test_the_unsharded_linux_job_before_linux_was_dealt_is_its_one_leg(self) -> None:
        old = log_of({"tests/a.py": 40.0}, linux_legs=(UNSHARDED,), linux={"tests/a.py": 4.0})
        self.assertEqual(("Linux", 1, 1), record_job_seconds.leg_of("Validation on Linux"))
        self.assertIn(("Linux", 1, 1), record_job_seconds.legs_seen(old))
        self.assertEqual("", record_job_seconds.leg_problem(old))
        self.assertEqual(
            {"Windows": {"tests/a.py": 40.0}, "Linux": {"tests/a.py": 4.0}},
            record_job_seconds.job_seconds_from_log(old),
        )


class MainTests(unittest.TestCase):
    def run_main(self, arguments: list[str], stub: Stub) -> tuple[int, str, str, Path]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return run_record(arguments, stub, Path(directory.name))

    def test_a_named_run_rewrites_the_table_from_its_logs(self) -> None:
        stub = Stub({"gh run view 38 --json": SUCCESS, "gh run view 38 --log": LOG})
        code, output, _, table = self.run_main(["38"], stub)
        self.assertEqual(0, code)
        self.assertEqual(
            {"runs": [38], "seconds": {"Linux": LINUX_SECONDS, "Windows": WINDOWS_SECONDS}},
            json.loads(table.read_text(encoding="utf-8")),
        )
        self.assertIn(
            "Recorded Linux 3 jobs, 20.5 job-seconds; Windows 4 jobs, 89.3 job-seconds, as the median of 1 run (38) in",
            output,
        )
        # Line feeds on every platform, so a rerun on another machine changes only the seconds.
        self.assertNotIn(b"\r", table.read_bytes())

    def test_named_runs_are_combined_by_median_and_recorded_by_id(self) -> None:
        stub = runs_stub(
            {
                30: log_of({"tests/a.py": 10.0, "tests/b.py": 4.0}),
                20: log_of({"tests/a.py": 90.0, "tests/b.py": 6.0}),
                10: log_of({"tests/a.py": 14.0, "tests/b.py": 5.0}),
            }
        )
        code, output, _, table = self.run_main(["30", "20", "10", "20"], stub)
        self.assertEqual(0, code)
        self.assertEqual(
            {"runs": [10, 20, 30], "seconds": both({"tests/a.py": 14.0, "tests/b.py": 5.0})},
            json.loads(table.read_text(encoding="utf-8")),
        )
        self.assertIn(
            "Recorded Linux 2 jobs, 1.9 job-seconds; Windows 2 jobs, 19.0 job-seconds, as the median of 3 runs "
            "(10, 20, 30) in",
            output,
        )
        self.assertFalse(any(command[:3] == ["gh", "run", "list"] for command in stub.commands))

    def test_a_job_some_runs_lack_takes_the_median_of_the_runs_that_have_it(self) -> None:
        stub = runs_stub(
            {
                1: log_of({"tests/a.py": 10.0}),
                2: log_of({"tests/a.py": 20.0, "tests/new.py": 8.0}),
                3: log_of({"tests/a.py": 30.0, "tests/new.py": 12.0}),
            }
        )
        code, _, _, table = self.run_main(["1", "2", "3"], stub)
        self.assertEqual(0, code)
        seconds = json.loads(table.read_text(encoding="utf-8"))["seconds"]
        self.assertEqual(both({"tests/a.py": 20.0, "tests/new.py": 10.0}), seconds)

    def test_runs_from_both_sides_of_the_legs_rename_are_combined(self) -> None:
        stub = runs_stub(
            {
                1: log_of({"tests/a.py": 10.0, "tests/b.py": 3.0}, OLD_LEGS),
                2: log_of({"tests/a.py": 30.0, "tests/b.py": 5.0}, OLD_LEGS),
                3: log_of({"tests/a.py": 20.0, "tests/b.py": 4.0}),
                4: log_of({"tests/a.py": 50.0, "tests/b.py": 9.0}),
                5: log_of({"tests/a.py": 40.0, "tests/b.py": 6.0}),
            }
        )
        code, output, errors, table = self.run_main(["1", "2", "3", "4", "5"], stub)
        self.assertEqual((0, ""), (code, errors))
        self.assertEqual(
            {"runs": [1, 2, 3, 4, 5], "seconds": both({"tests/a.py": 30.0, "tests/b.py": 5.0})},
            json.loads(table.read_text(encoding="utf-8")),
        )
        self.assertIn("as the median of 5 runs (1, 2, 3, 4, 5)", output)

    def test_runs_from_both_sides_of_the_linux_legs_split_are_combined(self) -> None:
        unsharded = (UNSHARDED,)
        stub = runs_stub(
            {
                1: log_of({"tests/a.py": 10.0}, linux_legs=unsharded, linux={"tests/a.py": 1.0, "tests/gone.py": 1.0}),
                2: log_of({"tests/a.py": 10.0}, linux_legs=unsharded, linux={"tests/a.py": 3.0}),
                3: log_of({"tests/a.py": 10.0}, linux={"tests/a.py": 2.0}),
            }
        )
        code, _, errors, table = self.run_main(["1", "2", "3"], stub)
        self.assertEqual((0, ""), (code, errors))
        self.assertEqual(
            {"Linux": {"tests/a.py": 2.0}, "Windows": {"tests/a.py": 10.0}},
            json.loads(table.read_text(encoding="utf-8"))["seconds"],
        )

    def test_runs_from_both_sides_of_the_linux_legs_rename_are_combined(self) -> None:
        stub = runs_stub(
            {
                1: log_of({"tests/a.py": 10.0}, linux_legs=OLD_LINUX_LEGS, linux={"tests/a.py": 1.0}),
                2: log_of({"tests/a.py": 10.0}, linux_legs=OLD_LINUX_LEGS, linux={"tests/a.py": 3.0}),
                3: log_of({"tests/a.py": 10.0}, linux={"tests/a.py": 2.0}),
            }
        )
        code, _, errors, table = self.run_main(["1", "2", "3"], stub)
        self.assertEqual((0, ""), (code, errors))
        self.assertEqual(
            {"Linux": {"tests/a.py": 2.0}, "Windows": {"tests/a.py": 10.0}},
            json.loads(table.read_text(encoding="utf-8"))["seconds"],
        )

    def test_a_job_renamed_or_removed_after_the_older_runs_is_not_recorded(self) -> None:
        stub = runs_stub(
            {
                1: log_of({"tests/old.py": 10.0, "tests/gone.py": 3.0, "tests/kept.py": 5.0}),
                2: log_of({"tests/renamed.py": 11.0, "tests/kept.py": 7.0}),
            }
        )
        code, _, _, table = self.run_main(["1", "2"], stub)
        self.assertEqual(0, code)
        seconds = json.loads(table.read_text(encoding="utf-8"))["seconds"]
        self.assertEqual(both({"tests/renamed.py": 11.0, "tests/kept.py": 6.0}), seconds)

    def test_without_runs_the_latest_successful_main_push_runs_are_read(self) -> None:
        stub = runs_stub({43: LOG, 42: LOG, 41: LOG})
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
            ("--limit", "6"),  # one more than the count, so a cut shows
        ):
            self.assertEqual(value, listing[listing.index(option) + 1])

    def test_a_listing_with_more_runs_than_the_count_keeps_the_newest(self) -> None:
        listed = json.dumps([{"databaseId": number} for number in (9, 8, 7)])
        client = GitHubClient(lambda arguments: CommandResult(0, listed, ""))
        self.assertEqual([9, 8], record_job_seconds.latest_runs(client, 2))

    def test_count_sets_how_many_of_the_latest_runs_are_listed(self) -> None:
        stub = runs_stub({50: LOG})
        stub.answers["gh run list"] = json.dumps([{"databaseId": 50}])
        code, _, _, _ = self.run_main(["--count", "1"], stub)
        self.assertEqual(0, code)
        self.assertEqual("2", stub.commands[0][stub.commands[0].index("--limit") + 1])
        for arguments in (["--count", "0"], ["--count", "-2"]):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                self.run_main(arguments, Stub({}))

    def test_one_refused_run_among_good_ones_refuses_the_record(self) -> None:
        stub = runs_stub({1: LOG, 3: LOG})
        stub.answers["gh run view 2 --json"] = json.dumps(
            {"workflowName": "Validation", "status": "completed", "conclusion": "failure"}
        )
        code, _, errors, table = self.run_main(["1", "2", "3"], stub)
        self.assertEqual((2, False), (code, table.exists()))
        self.assertIn("run 2 is 'Validation', completed, failure", errors)

    def test_a_successful_run_whose_log_lacks_a_leg_is_refused(self) -> None:
        # A rerun of one failed leg ends successful but may log only the legs of its last attempt.
        stub = runs_stub({7: log_of({"tests/a.py": 1.0, "tests/b.py": 2.0}, LEGS[:3])})
        code, _, errors, table = self.run_main(["7"], stub)
        self.assertEqual((2, False), (code, table.exists()))
        self.assertIn("run 7's log lacks Windows leg 4/4", errors)

    def test_a_successful_run_whose_log_lacks_a_platform_is_refused(self) -> None:
        stub = runs_stub({7: log_of({"tests/a.py": 1.0}, linux_legs=())})
        code, _, errors, table = self.run_main(["7"], stub)
        self.assertEqual((2, False), (code, table.exists()))
        self.assertIn("run 7's log names no one set of Linux legs", errors)

    def test_a_run_whose_log_gh_no_longer_holds_refuses_the_record_and_names_the_run(self) -> None:
        stub = runs_stub({1: LOG})
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
        # Every leg ran, and none printed a job's seconds, as a run without -v does.
        nothing = "\n".join(f"{leg}nothing printed" for leg in (*LEGS, *LINUX_LEGS))
        quiet = Stub({"gh run view 6 --json": SUCCESS, "gh run view 6 --log": nothing})
        code, _, errors, table = self.run_main(["6"], quiet)
        self.assertEqual((2, False), (code, table.exists()))
        self.assertIn("run 6's Linux and Windows legs reported no passing jobs; run validation with -v", errors)
        # One platform's legs printed theirs and the other's did not.
        half = log_of({"tests/a.py": 1.0}, linux={})
        code, _, errors, table = self.run_main(
            ["4"], Stub({"gh run view 4 --json": SUCCESS, "gh run view 4 --log": half})
        )
        self.assertEqual((2, False), (code, table.exists()))
        self.assertIn("run 4's Linux legs reported no passing jobs", errors)

    def test_the_table_it_writes_is_the_one_validation_reads(self) -> None:
        table = record_job_seconds.TABLE.relative_to(record_job_seconds.REPOSITORY_ROOT)
        self.assertEqual(Path("tests/validation/job_seconds.json"), table)
        self.assertTrue(record_job_seconds.TABLE.is_file())

    def test_the_release_procedure_refreshes_the_table_before_tagging(self) -> None:
        text = (record_job_seconds.REPOSITORY_ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
        before_tagging = text.split("## Before tagging", 1)[1].split("\n## ", 1)[0]
        self.assertIn("python -B tools/record_job_seconds.py", before_tagging)


@pytest.mark.parametrize(
    "job",
    [
        "Shard 1/4, Python 3.11",
        "Windows shard 1/4 on Python 3.11",
        "windows shard 1/4, Python 3.11",
        "linux shard 1/2, Python 3.11",
        "Linux shard 1/2",
        "Linux shard 1/2, Python",
        "Linux shard 1/2 Python 3.11",
        "Validation on Linux shard 1/2",
        "validation on Linux, shard 1/2",
        "Linux shard 1/2 on Python 3.11",
        "Validation on Linux, shard 1/2, Python 3.11",
        "Validation on Windows",
        "Validation",
        "Shard 1/4 on 3.11",
    ],
)
def test_a_near_miss_of_a_shape_the_workflow_gave_its_legs_is_no_leg(job: str) -> None:
    # Only the shapes the workflow gave its legs: a near miss is no leg.
    assert record_job_seconds.leg_of(job) is None


@pytest.mark.parametrize(
    "log",
    [
        pytest.param(f"{AGGREGATE}ok    1.0s tests/a.py", id="the aggregate job"),
        pytest.param(
            "Shard 1/4, Python 3.11\tRun repository validation\t2026-10-10T06:17:04Z ok 1.0s tests/a.py",
            id="a near miss of a leg",
        ),
        pytest.param("", id="empty"),
    ],
)
def test_a_run_whose_log_names_no_leg_by_any_name_is_refused_for_that_cause(log: str, tmp_path: Path) -> None:
    code, _, errors, table = run_record(["8"], runs_stub({8: log}), tmp_path)
    assert (code, table.exists()) == (2, False)
    assert "run 8's log names no job as a leg" in errors
    # The message names the shapes it looked for, and blames no missing -v.
    assert "`Windows shard <n>/<count>, Python <version>`" in errors
    assert "`Shard <n>/<count> on Python <version>`" in errors
    assert "`Linux shard <n>/<count>, Python <version>`" in errors
    assert "`Validation on Linux, shard <n>/<count>`" in errors
    assert "`Validation on Linux`" in errors
    assert "-v" not in errors


@pytest.mark.parametrize(
    "state",
    [
        pytest.param({"workflowName": "Validation", "status": "completed", "conclusion": "failure"}, id="failure"),
        # A cancelled leg ends the run as cancelled.
        pytest.param({"workflowName": "Validation", "status": "completed", "conclusion": "cancelled"}, id="cancelled"),
        pytest.param({"workflowName": "Validation", "status": "in_progress", "conclusion": ""}, id="in progress"),
        pytest.param(
            {"workflowName": "Deployment Smoke Test", "status": "completed", "conclusion": "success"},
            id="another workflow",
        ),
        # A run from before the workflow was renamed carries its old name.
        pytest.param({"workflowName": "Validate", "status": "completed", "conclusion": "success"}, id="the old name"),
    ],
)
def test_a_run_that_did_not_pass_or_is_another_workflow_is_refused_and_the_table_is_left_alone(
    state: dict[str, str], tmp_path: Path
) -> None:
    stub = Stub({"gh run view 9 --json": json.dumps(state)})
    code, _, errors, table = run_record(["9"], stub, tmp_path)
    assert code == 2
    assert "record from completed, successful Validation runs" in errors
    assert not table.exists()
    assert not any("--log" in command for command in stub.commands)

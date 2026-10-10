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

LEG_1 = "Validate (Python 3.11, shard 1/4)\tRun repository validation\t2026-10-10T06:17:04.4226827Z "
LEG_2 = "Validate (Python 3.11, shard 2/4)\tUNKNOWN STEP\t2026-10-10T06:17:05.0000000Z "
LATEST = "Validate (Python 3.x, shard 1/4)\tUNKNOWN STEP\t2026-10-10T06:17:06.0000000Z "
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
    ]
)


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


SUCCESS = json.dumps({"workflowName": "Validate", "status": "completed", "conclusion": "success"})


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
        text = record_job_seconds.render_table(7, {"tests/b.py": 2.0, "static lint check": 1.5, "tests/a.py": 3.0})
        self.assertEqual(
            '{\n  "run": 7,\n  "seconds": {\n    "static lint check": 1.5,\n    "tests/a.py": 3.0,\n'
            '    "tests/b.py": 2.0\n  }\n}\n',
            text,
        )


class MainTests(unittest.TestCase):
    def run_main(self, arguments: list[str], stub: Stub) -> tuple[int, str, str, Path]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        table = Path(directory.name) / "job_seconds.json"
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = record_job_seconds.main(arguments, client=GitHubClient(runner=stub), table=table)
        return code, output.getvalue(), errors.getvalue(), table

    def test_a_named_run_rewrites_the_table_from_its_logs(self) -> None:
        stub = Stub({"gh run view 38 --json": SUCCESS, "gh run view 38 --log": LOG})
        code, output, _, table = self.run_main(["38"], stub)
        self.assertEqual(0, code)
        self.assertEqual(
            {
                "run": 38,
                "seconds": {
                    "skills/update-coding-agent-skills/scripts/test_update.sh": 55.7,
                    "static format check (ruff format --check)": 0.4,
                    "static type check (mypy skills/skill-core/scripts)": 3.7,
                    "tests/deployer/test_render.py": 29.5,
                },
            },
            json.loads(table.read_text(encoding="utf-8")),
        )
        self.assertIn("Recorded 4 jobs, 89.3 job-seconds, from run 38", output)
        # Line feeds on every platform, so a rerun on another machine changes only the seconds.
        self.assertNotIn(b"\r", table.read_bytes())

    def test_without_a_run_the_latest_successful_main_push_run_is_read(self) -> None:
        stub = Stub(
            {
                "gh run list": json.dumps([{"databaseId": 41}]),
                "gh run view 41 --json": SUCCESS,
                "gh run view 41 --log": LOG,
            }
        )
        code, _, _, table = self.run_main([], stub)
        self.assertEqual(0, code)
        self.assertEqual(41, json.loads(table.read_text(encoding="utf-8"))["run"])
        listing = stub.commands[0]
        for option, value in (
            ("--workflow", "validate.yml"),
            ("--branch", "main"),
            ("--event", "push"),
            ("--status", "success"),
        ):
            self.assertEqual(value, listing[listing.index(option) + 1])

    def test_a_run_that_did_not_pass_or_is_another_workflow_is_refused_and_the_table_is_left_alone(self) -> None:
        for state in (
            {"workflowName": "Validate", "status": "completed", "conclusion": "failure"},
            {"workflowName": "Validate", "status": "in_progress", "conclusion": ""},
            {"workflowName": "Deployable", "status": "completed", "conclusion": "success"},
        ):
            with self.subTest(state=state):
                stub = Stub({"gh run view 9 --json": json.dumps(state)})
                code, _, errors, table = self.run_main(["9"], stub)
                self.assertEqual(2, code)
                self.assertIn("record from a completed, successful Validate run", errors)
                self.assertFalse(table.exists())
                self.assertFalse(any("--log" in command for command in stub.commands))

    def test_a_run_gh_cannot_read_or_with_no_passing_jobs_writes_nothing(self) -> None:
        code, _, errors, table = self.run_main(["5"], Stub({"gh run view 5": 1}))
        self.assertEqual((2, False), (code, table.exists()))
        self.assertIn("Cannot read the validate run", errors)
        code, _, errors, table = self.run_main([], Stub({"gh run list": "[]"}))
        self.assertEqual((2, False), (code, table.exists()))
        self.assertIn("no successful push run of validate.yml on main", errors)
        quiet = Stub({"gh run view 6 --json": SUCCESS, "gh run view 6 --log": f"{LEG_1}Shard 1/4: nothing printed"})
        code, _, errors, table = self.run_main(["6"], quiet)
        self.assertEqual((2, False), (code, table.exists()))
        self.assertIn("reported no passing jobs", errors)

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

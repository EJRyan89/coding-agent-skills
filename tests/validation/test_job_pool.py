"""Fixture tests for tests/validation/job_pool.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from job_pool import OUTPUT_LOCK, Job, append_step_summary, run_beside, run_jobs, step_summary, worker_count


class JobPoolFixtures(unittest.TestCase):
    def test_step_summary_reports_the_result_and_each_failure(self) -> None:
        text = step_summary(
            (3, 11, 9),
            {"Git Bash": (5, 2, 37), "ShellCheck": (0, 9, 0), "PowerShell 7 (pwsh)": None},
            "Full validation: no changed files were found.",
            140,
            52,
            61.4,
            ["policy test_one", "suite tests/x.py"],
        )
        self.assertEqual(
            "## Repository validation\n\n"
            "- Python 3.11.9, Git Bash 5.2.37, ShellCheck 0.9.0, PowerShell 7 (pwsh) unknown\n"
            "- Full validation: no changed files were found.\n"
            "- 140 policy checks and 52 suite jobs in 61s: **validation FAILED**\n"
            "- Failed: `policy test_one`\n"
            "- Failed: `suite tests/x.py`\n",
            text,
        )
        self.assertIn("**validation passed**", step_summary((3, 14, 7), {}, "Mode.", 1, 1, 1.0, []))
        # A leg of a run split across runners names its shard, since each leg writes its own summary.
        self.assertEqual(
            "## Repository validation, shard 2/4\n\n- Python 3.11.9\n- Mode.\n"
            "- 1 policy checks and 1 suite jobs in 1s: **validation passed**\n",
            step_summary((3, 11, 9), {}, "Mode.", 1, 1, 1.0, [], shard="2/4"),
        )

    def test_any_exception_fails_only_its_job_and_reports_its_traceback(self) -> None:
        def check_fails() -> None:
            raise AssertionError("2 suite failures")

        def cannot_read() -> None:
            raise OSError(5, "Access is denied")

        def exits() -> None:
            sys.exit(3)

        ran: list[str] = []
        jobs = [
            Job("passes", "passes", 1, lambda: ran.append("passes")),
            Job("check fails", "check fails", 4, check_fails),
            Job("cannot read", "cannot read", 3, cannot_read),
            Job("exits", "exits", 2, exits),
            Job("also passes", "also passes", 0, lambda: ran.append("also passes")),
        ]
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            failures = run_jobs(jobs, verbose=False, workers=1)
        self.assertEqual(["passes", "also passes"], ran, "a raising job stops neither the pool nor the jobs after it")
        self.assertEqual(["cannot read", "check fails", "exits"], [failure.job.label for failure in failures])
        failed = {failure.job.label: failure for failure in failures}
        # A failed check reports its message; anything else reports the traceback that names where it was raised.
        self.assertEqual(("2 suite failures", False), (failed["check fails"].report, failed["check fails"].raised))
        self.assertTrue(failed["cannot read"].raised)
        self.assertIn("Traceback (most recent call last):", failed["cannot read"].report)
        self.assertIn("in cannot_read", failed["cannot read"].report)
        self.assertTrue(failed["cannot read"].report.rstrip().endswith("OSError: [Errno 5] Access is denied"))
        self.assertTrue(failed["exits"].report.rstrip().endswith("SystemExit: 3"))
        self.assertEqual(3, printed.getvalue().count("FAIL "))

    def test_the_pool_runs_beside_the_foreground_and_both_results_come_back(self) -> None:
        # Each side waits for the other to start, so this passes only when the suites and the policy checks overlap.
        job_started, foreground_started = threading.Event(), threading.Event()

        def job() -> None:
            job_started.set()
            if not foreground_started.wait(30):
                raise AssertionError("the foreground never ran while this job did")

        def foreground() -> str:
            foreground_started.set()
            if not job_started.wait(30):
                raise AssertionError("no job ran while the foreground did")
            return "policies passed"

        jobs = [Job("waits", "waits", 1, job), Job("fails", "fails", 2, lambda: self.fail("2 suite failures"))]
        with contextlib.redirect_stdout(io.StringIO()):
            result, failures = run_beside(foreground, jobs, verbose=False, workers=2)
        self.assertEqual("policies passed", result)
        self.assertEqual(["fails"], [failure.job.label for failure in failures])

    def test_a_pool_line_waits_while_the_foreground_holds_the_output(self) -> None:
        # The policy report prints under OUTPUT_LOCK, so no suite's line lands inside it.
        finished = threading.Event()

        def job() -> None:
            finished.set()
            raise AssertionError("failed")

        def foreground() -> str:
            with OUTPUT_LOCK:
                if not finished.wait(30):
                    raise AssertionError("the job never ran")
                time.sleep(0.2)
                return printed.getvalue()

        with contextlib.redirect_stdout(io.StringIO()) as printed:
            during, failures = run_beside(foreground, [Job("fails", "fails", 1, job)], verbose=False, workers=1)
        self.assertEqual("", during)
        self.assertEqual(["fails"], [failure.job.label for failure in failures])
        self.assertRegex(printed.getvalue(), r"\AFAIL +[0-9.]+s fails\n\Z")

    def test_the_pool_finishes_before_an_exception_in_the_foreground_propagates(self) -> None:
        ran = threading.Event()

        def job() -> None:
            time.sleep(0.2)
            ran.set()

        def foreground() -> None:
            raise RuntimeError("policy loader broke")

        with self.assertRaisesRegex(RuntimeError, "policy loader broke"):
            run_beside(foreground, [Job("slow", "slow", 1, job)], verbose=False, workers=1)
        self.assertTrue(ran.is_set(), "the pool is waited for, never left running")

    def test_one_worker_per_cpu_up_to_the_maximum_unless_validation_jobs_says(self) -> None:
        self.assertEqual(4, worker_count({}, cpus=4))
        self.assertEqual(24, worker_count({}, cpus=24))
        self.assertEqual(24, worker_count({}, cpus=64))
        self.assertEqual(32, worker_count({"VALIDATION_JOBS": "32"}, cpus=4))
        self.assertEqual(1, worker_count({"VALIDATION_JOBS": "0"}, cpus=24))

    def test_step_summary_carries_the_traceback_of_a_job_that_raised(self) -> None:
        traceback = 'Traceback (most recent call last):\n  File "x.py", line 1\nOSError: denied\n'
        text = step_summary((3, 11, 9), {}, "Mode.", 1, 2, 3.0, ["suite a.py", "suite b.py"], {"suite b.py": traceback})
        self.assertEqual(
            "## Repository validation\n\n- Python 3.11.9\n- Mode.\n- 1 policy checks and 2 suite jobs in 3s: "
            "**validation FAILED**\n- Failed: `suite a.py`\n- Failed: `suite b.py`\n\n  ```text\n"
            '  Traceback (most recent call last):\n    File "x.py", line 1\n  OSError: denied\n  ```\n\n',
            text,
        )

    def test_step_summary_is_appended_only_when_github_names_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "summary with spaces.md"
            path.write_text("earlier\n", encoding="utf-8")
            append_step_summary({"GITHUB_STEP_SUMMARY": str(path)}, "## Repository validation\n")
            self.assertEqual("earlier\n## Repository validation\n", path.read_text(encoding="utf-8"))
            append_step_summary({}, "ignored\n")
            self.assertEqual(["summary with spaces.md"], [child.name for child in Path(temporary).iterdir()])


if __name__ == "__main__":
    unittest.main()

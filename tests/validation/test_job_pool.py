"""Fixture tests for tests/validation/job_pool.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from job_pool import Job, append_step_summary, run_jobs, step_summary


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

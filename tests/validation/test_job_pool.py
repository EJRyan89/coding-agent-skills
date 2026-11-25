"""Fixture tests for tests/validation/job_pool.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import job_pool
import validation_notes
from job_pool import (
    OUTPUT_LOCK,
    Job,
    PoolResult,
    Retry,
    Terminated,
    TimedOut,
    append_step_summary,
    run_beside,
    run_jobs,
    run_process,
    step_summary,
    worker_count,
)
from job_selection import stale_job_seconds_problems
from process_identity import process_status
from suite_discovery import PYTEST_ENVIRONMENT, pytest_base, pytest_command
from validation_support import REPOSITORY_ROOT

from deployer import platform_support
from tests import run_validation
from tools import record_job_seconds

# Stand-ins for the program a job starts, Bash among them. Windows reports a process that exits with -2147483644 as
# 0x80000004, the status Git Bash was ended with on the hosted runners before it printed anything; elsewhere a process
# a signal ends, here SIGKILL, is the one ended rather than exiting, and Python reports it as -9.
if platform_support.WINDOWS:
    ENDING, STATUS, CODE = "os._exit(-2147483644)", "0x80000004", "2147483652"
else:
    ENDING, STATUS, CODE = "os.kill(os.getpid(), 9)", "SIGKILL", "-9"
ENDED = [sys.executable, "-c", f"import os; {ENDING}"]
ENDED_AFTER_OUTPUT = [sys.executable, "-c", f"import os; print('ran', flush=True); {ENDING}"]
FAILS_AFTER_OUTPUT = [sys.executable, "-c", "import sys; print('1 test failed', flush=True); sys.exit(1)"]
PASSES = [sys.executable, "-c", "pass"]
# A suite that prints a line, starts a grandchild that outlives it, records the grandchild's process ID in the file its
# first argument names, and hangs.
HANGS = (
    "import subprocess, sys, time\n"
    "print('started its grandchild', flush=True)\n"
    "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "open(sys.argv[1], 'w').write(str(grandchild.pid))\n"
    "time.sleep(60)\n"
)


def job_running(label: str, *commands: list[str]) -> tuple[Job, list[list[str]]]:
    """A job that starts the next of `commands` on each attempt, the last one again once they run out."""
    started: list[list[str]] = []

    def run() -> None:
        command = commands[min(len(started), len(commands) - 1)]
        started.append(command)
        run_process(command)

    return Job(label, label, 1, run), started


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
            failures = run_jobs(jobs, verbose=False, workers=1).failures
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
            result, (failures, _, _) = run_beside(foreground, jobs, verbose=False, workers=2)
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
            during, (failures, _, _) = run_beside(foreground, [Job("fails", "fails", 1, job)], verbose=False, workers=1)
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
        with self.assertRaisesRegex(ValueError, "VALIDATION_JOBS must be a whole number of workers, not 'four'"):
            worker_count({"VALIDATION_JOBS": "four"}, cpus=4)

    def test_a_job_child_receives_no_token_from_the_runner_or_its_caller(self) -> None:
        # The child fails, naming what it holds, when any token variable reached it, in any case.
        names = ["GH_ENTERPRISE_TOKEN", "GH_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GITHUB_TOKEN"]
        holds_token = (
            "import os, sys\n"
            f"held = sorted(name for name in os.environ if name.upper() in {names!r})\n"
            "sys.exit(f'holds {held}' if held else 0)\n"
        )
        tokens = {"GH_TOKEN": "marker", "GITHUB_TOKEN": "marker", "Gh_Enterprise_Token": "marker"}
        with mock.patch.dict(os.environ, tokens):
            run_process([sys.executable, "-c", holds_token], environment={"GITHUB_ENTERPRISE_TOKEN": "marker"})
        # The control: the same child, given a token another way, fails, so the run above shows the removal.
        with self.assertRaisesRegex(AssertionError, "holds"):
            run_process([sys.executable, "-c", holds_token.replace("os.environ", "{'GH_TOKEN': 1}")])

    def test_a_program_past_its_time_limit_is_ended_with_every_process_it_started_and_reported_whole(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            recorded = Path(temporary) / "grandchild.txt"
            command = [sys.executable, "-c", HANGS, str(recorded)]
            with mock.patch.object(job_pool, "COMMAND_TIMEOUT_SECONDS", 4), self.assertRaises(TimedOut) as raised:
                run_process(command)
            # The report names the whole command, not the interpreter alone, and what it printed before it hung.
            report = str(raised.exception)
            self.assertIn(
                f"Command timed out after 4s and was ended with every process it started: {command[0]} -c", report
            )
            self.assertIn(str(recorded), report)
            self.assertIn("started its grandchild", report)
            # Nothing the suite started is left running.
            self.assertFalse(process_status(int(recorded.read_text(encoding="utf-8"))).alive)

        # A timed-out job fails as a timeout, which the step summary details, rather than as a failed check.
        def hangs() -> None:
            raise TimedOut("Command timed out after 4s: x")

        failures = run_jobs([Job("hangs", "hangs", 1, hangs)], verbose=False, workers=1).failures
        self.assertEqual([(True, False, False)], [(f.timed_out, f.raised, f.terminated) for f in failures])

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


class TerminationFixtures(unittest.TestCase):
    """A program ended with a termination status before its first line is the program's ending, not a failed check."""

    def test_a_job_is_started_again_once(self) -> None:
        self.assertEqual(1, job_pool.TERMINATION_RETRIES)

    def test_a_program_ended_before_any_output_is_a_termination_naming_its_status_and_command(self) -> None:
        with self.assertRaises(Terminated) as raised:
            run_process(ENDED)
        self.assertEqual(STATUS, raised.exception.status)
        self.assertEqual(" ".join(ENDED), raised.exception.command)
        self.assertEqual(
            f"{Path(sys.executable).name} was ended with status {STATUS} before its first line of output: "
            f"{' '.join(ENDED)}",
            str(raised.exception),
        )
        self.assertNotIsInstance(raised.exception, AssertionError, "a termination is not a failed check")

    def test_a_program_ended_after_its_output_began_is_a_failed_check_naming_the_status(self) -> None:
        with self.assertRaises(AssertionError) as raised:
            run_process(ENDED_AFTER_OUTPUT)
        self.assertIn(f"exit code {CODE} (status {STATUS}, ended after its output began)", str(raised.exception))
        self.assertIn("ran", str(raised.exception))

    def test_a_job_ended_before_output_is_started_once_more_and_reported_when_it_passes(self) -> None:
        job, started = job_running("bash suite", ENDED, PASSES)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            failures, retries, _ = run_jobs([job], verbose=False, workers=1)
        self.assertEqual([ENDED, PASSES], started)
        self.assertEqual([], failures)
        ending = f"{Path(sys.executable).name} was ended with status {STATUS} before its first line of output: "
        self.assertEqual([Retry(job, f"{ending}{' '.join(ENDED)}", "passed")], retries)
        self.assertRegex(
            printed.getvalue(),
            rf"\ARETRY +[0-9.]+s bash suite: .+ was ended with status {STATUS} before its first line of output: .+; "
            r"a termination, not a failed check, so it starts once more\n"
            rf"ok +[0-9.]+s bash suite \(retried after status {STATUS}\)\n\Z",
        )

    def test_the_line_a_retried_job_passes_with_is_recorded_under_the_job_s_name(self) -> None:
        # The seconds recorder reads this line from a leg's log: the retry note stays out of the name it records, so
        # the refreshed table names a job validation runs.
        # The retry takes long enough that its seconds round above zero, which a recorded job's always are.
        job, _ = job_running(
            "tests/test_a.py [shard 1/2]", ENDED, [sys.executable, "-c", "import time; time.sleep(0.3)"]
        )
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            run_jobs([job], verbose=True, workers=1)
        leg = "Linux shard 1/2, Python 3.11\tRun repository validation\t2026-10-23T06:17:04.0000000Z "
        log = "\n".join(f"{leg}{line}" for line in printed.getvalue().splitlines())
        self.assertIn("(retried after status", log)
        recorded = record_job_seconds.job_seconds_from_log(log)
        self.assertEqual(["tests/test_a.py"], list(recorded["Linux"]))
        self.assertEqual([], stale_job_seconds_problems(recorded, {"tests/test_a.py"}))

    def test_a_second_ending_is_a_failed_job_that_names_each_status(self) -> None:
        job, started = job_running("bash suite", ENDED)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            failures, retries, _ = run_jobs([job], verbose=False, workers=1)
        self.assertEqual([ENDED, ENDED], started, "started once more, and no more")
        self.assertEqual(["bash suite"], [failure.job.label for failure in failures])
        failure = failures[0]
        self.assertEqual((True, False), (failure.terminated, failure.raised))
        self.assertTrue(
            failure.report.startswith(
                "Ended before its first line of output on each of 2 attempts, a termination rather than a failed "
                "check, so nothing in it reached a verdict:\n  "
            )
        )
        self.assertEqual(2, failure.report.count(f"with status {STATUS} before its first line of output: {ENDED[0]}"))
        self.assertEqual(["was ended again"], [retry.result for retry in retries])
        self.assertRegex(printed.getvalue(), rf"\nFAIL +[0-9.]+s bash suite \(retried after status {STATUS}\)\n\Z")

    def test_a_job_that_wrote_output_is_never_started_again(self) -> None:
        failing, failing_started = job_running("fails", FAILS_AFTER_OUTPUT, PASSES)
        ended, ended_started = job_running("ended after output", ENDED_AFTER_OUTPUT, PASSES)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            failures, retries, _ = run_jobs([failing, ended], verbose=False, workers=1)
        self.assertEqual(([FAILS_AFTER_OUTPUT], [ENDED_AFTER_OUTPUT]), (failing_started, ended_started))
        self.assertEqual(["ended after output", "fails"], [failure.job.label for failure in failures])
        self.assertEqual([(False, False), (False, False)], [(f.terminated, f.raised) for f in failures])
        self.assertIn("1 test failed", failures[1].report)
        self.assertEqual([], retries)
        self.assertNotIn("RETRY", printed.getvalue())

    def test_a_retry_that_fails_reports_the_ending_and_the_retry_s_result(self) -> None:
        job, started = job_running("bash suite", ENDED, FAILS_AFTER_OUTPUT)
        with contextlib.redirect_stdout(io.StringIO()):
            failures, retries, _ = run_jobs([job], verbose=False, workers=1)
        self.assertEqual([ENDED, FAILS_AFTER_OUTPUT], started)
        self.assertEqual(["failed"], [retry.result for retry in retries])
        report = failures[0].report
        self.assertTrue(report.startswith("Started once more after: "), report)
        self.assertIn(f"with status {STATUS} before its first line of output", report)
        self.assertIn("\nThe retry failed:\nCommand failed with exit code 1: ", report)
        self.assertIn("1 test failed", report)

    def test_a_retry_that_times_out_stays_a_timeout_or_a_raise_whose_report_the_summary_details(self) -> None:
        ending = Terminated("0x80000004", ["bash.exe", "-c", "x"])
        for raised, flags in (
            (TimedOut("Command timed out after 4s: x"), (True, False, False)),
            (OSError("denied"), (False, True, False)),
        ):
            attempts: list[BaseException] = [ending, raised]

            def run(attempts: list[BaseException] = attempts) -> None:
                raise attempts.pop(0)

            with self.subTest(raised=type(raised).__name__), contextlib.redirect_stdout(io.StringIO()):
                failures, retries, _ = run_jobs([Job("bash suite", "bash suite", 1, run)], verbose=False, workers=1)
                self.assertEqual(["failed"], [retry.result for retry in retries])
                self.assertEqual([flags], [(f.timed_out, f.raised, f.terminated) for f in failures])

    def test_step_summary_names_a_termination_and_each_retry(self) -> None:
        ended = "bash.exe was ended with status 0x80000004 before its first line of output: bash.exe -c 'x'"
        text = step_summary(
            (3, 11, 9),
            {},
            "Mode.",
            1,
            3,
            4.0,
            ["suite a.sh"],
            {"suite a.sh": f"Ended before its first line of output on each of 2 attempts:\n  {ended}\n  {ended}"},
            "1/4",
            {"suite a.sh": f"{ended}\nStarted once more, it was ended again.", "suite b.sh": f"{ended}\nIt passed."},
        )
        self.assertEqual(
            "## Repository validation, shard 1/4\n\n- Python 3.11.9\n- Mode.\n"
            "- 1 policy checks and 3 suite jobs in 4s: **validation FAILED**\n"
            "- Failed: `suite a.sh`\n\n  ```text\n"
            "  Ended before its first line of output on each of 2 attempts:\n"
            f"    {ended}\n    {ended}\n  ```\n\n"
            "- Retried once: `suite a.sh`\n\n  ```text\n"
            f"  {ended}\n  Started once more, it was ended again.\n  ```\n\n"
            "- Retried once: `suite b.sh`\n\n  ```text\n"
            f"  {ended}\n  It passed.\n  ```\n\n",
            text,
        )


MARK = validation_notes.NOTE_MARKER
# A program that passes after writing a note on each stream, one after other output on its line, as pytest's progress
# comes before what a test prints, and a line of its own.
NOTES = (
    "import sys\n"
    f"print({MARK + 'the volume stays attached  '!r}, flush=True)\n"
    "print('a line with no note', flush=True)\n"
    f"print({'test_a.py ' + MARK + 'after the progress'!r}, flush=True)\n"
    f"print({MARK + 'from the error stream'!r}, file=sys.stderr, flush=True)\n"
)
# A suite that passes and reports a test volume left attached through test_atomic_file.py's own report.
LEFT_ATTACHED = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, {scripts!r})\n"
    "from test_atomic_file import report_volume_left\n"
    "def test_passes_with_a_volume_left_attached():\n"
    "    report_volume_left('MS-DOS', Path('mount'), 'hdiutil detach exited with status 16: busy')\n"
)


class NoteFixtures(unittest.TestCase):
    """A passing job's notes: the lines its programs write starting with NOTE_MARKER, which the runner keeps."""

    def test_the_pool_reads_the_line_suites_write(self) -> None:
        # One constant, which test_validation_notes.py pins and test_atomic_file.py's teardown writes through.
        self.assertEqual(
            ["a volume stays attached"], job_pool.program_notes(validation_notes.note_line("a volume stays attached"))
        )

    def test_what_follows_the_marker_on_a_line_is_a_note(self) -> None:
        self.assertEqual(
            ["one", "after progress", f"two {MARK}kept whole", "three"],
            job_pool.program_notes(
                f"{MARK}one  \nno note\ntest_a.py .{MARK}after progress\n",
                f"{MARK}two {MARK}kept whole\r\n",
                f"{MARK}three",
            ),
        )
        self.assertEqual(
            [],
            job_pool.program_notes("", f"{MARK.rstrip()}\n {MARK.lower()}x\n{MARK.rstrip()}x\n{MARK}   \n"),
        )

    def test_a_passing_job_keeps_its_notes_prints_them_and_a_failing_one_keeps_none(self) -> None:
        noted, _ = job_running("noted suite", [sys.executable, "-c", NOTES])
        quiet, _ = job_running("quiet suite", PASSES)
        failing, _ = job_running("failing suite", [sys.executable, "-c", NOTES + "sys.exit(1)\n"])
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            result = run_jobs([quiet, failing, noted], verbose=False, workers=2)
        self.assertEqual(["failing suite"], [failure.job.label for failure in result.failures])
        self.assertIn(f"{MARK}the volume stays attached", result.failures[0].report, "its output is whole")
        self.assertEqual(
            {"noted suite": ["the volume stays attached", "after the progress", "from the error stream"]}, result.notes
        )
        lines = printed.getvalue().splitlines()
        self.assertRegex(
            lines[lines.index("NOTE noted suite: the volume stays attached") - 1], r"^ok +[0-9.]+s noted suite$"
        )
        self.assertIn("NOTE noted suite: from the error stream", lines)
        self.assertFalse([line for line in lines if "quiet suite" in line], "a quiet job prints nothing unless verbose")
        self.assertFalse([line for line in lines if line.startswith("NOTE failing suite")])

    def test_a_retried_job_keeps_only_its_last_attempt_s_notes_and_prints_each_once(self) -> None:
        # The first attempt passes one program with a note, then a program is ended before any output, so the job
        # starts once more and passes both: the note is its retry's, once.
        noting = [sys.executable, "-c", f"print({MARK + 'kept once'!r})"]
        attempts: list[int] = []

        def run() -> None:
            attempts.append(1)
            run_process(noting)
            run_process(ENDED if len(attempts) == 1 else PASSES)

        with contextlib.redirect_stdout(io.StringIO()) as printed:
            result = run_jobs([Job("retried suite", "retried suite", 1, run)], verbose=False, workers=1)
        self.assertEqual(2, len(attempts))
        self.assertEqual(([], ["passed"]), (result.failures, [retry.result for retry in result.retries]))
        self.assertEqual({"retried suite": ["kept once"]}, result.notes)
        self.assertEqual(1, printed.getvalue().count("NOTE retried suite: kept once\n"), printed.getvalue())

    def test_a_job_that_fails_after_a_program_kept_a_note_keeps_none(self) -> None:
        noting = [sys.executable, "-c", f"print({MARK + 'from a program that passed'!r})"]

        def run() -> None:
            run_process(noting)
            run_process(FAILS_AFTER_OUTPUT)

        with contextlib.redirect_stdout(io.StringIO()) as printed:
            result = run_jobs([Job("failing suite", "failing suite", 1, run)], verbose=False, workers=1)
        self.assertEqual(["failing suite"], [failure.job.label for failure in result.failures])
        self.assertEqual({}, result.notes, "a failed job's report is its own; it keeps no notes")
        self.assertNotIn("NOTE failing suite", printed.getvalue())

    def test_a_verbose_run_prints_every_job_s_line_and_a_quiet_one_only_a_noted_job_s(self) -> None:
        for verbose in (True, False):
            quiet, _ = job_running("quiet suite", PASSES)
            with self.subTest(verbose=verbose), contextlib.redirect_stdout(io.StringIO()) as printed:
                run_jobs([quiet], verbose=verbose, workers=1)
            if verbose:
                self.assertRegex(printed.getvalue(), r"\Aok +[0-9.]+s quiet suite\n\Z")
            else:
                self.assertEqual("", printed.getvalue())

    def test_the_retries_come_back_in_label_order_whatever_order_the_jobs_ran_in(self) -> None:
        # The heaviest job starts first, so "b" runs before "a" on one worker.
        light, _ = job_running("a retried", ENDED, PASSES)
        heavy, _ = job_running("b retried", ENDED, PASSES)
        heavy = Job(heavy.label, heavy.name, 2, heavy.run)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            result = run_jobs([light, heavy], verbose=False, workers=1)
        self.assertLess(printed.getvalue().index("b retried"), printed.getvalue().index("a retried"), "b ran first")
        self.assertEqual(["a retried", "b retried"], [retry.job.label for retry in result.retries])

    def test_a_job_s_notes_stay_its_own_and_none_outlive_it(self) -> None:
        jobs = [
            job_running(f"suite {index}", [sys.executable, "-c", f"print({MARK + str(index)!r})"])[0]
            for index in range(6)
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_jobs(jobs, verbose=False, workers=3)
        self.assertEqual({f"suite {index}": [str(index)] for index in range(6)}, result.notes)
        with contextlib.redirect_stdout(io.StringIO()):
            run_process([sys.executable, "-c", NOTES])
        self.assertFalse(hasattr(job_pool._JOB_NOTES, "kept"), "a program outside a job keeps no note")

    def test_a_note_line_leaves_the_seconds_recorder_reading_the_job_s_own_line(self) -> None:
        job, _ = job_running(
            "tests/test_a.py [shard 1/2]", [sys.executable, "-c", "import time; time.sleep(0.3)\n" + NOTES]
        )
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            run_jobs([job], verbose=False, workers=1)
        leg = "Linux shard 1/2, Python 3.11\tRun repository validation\t2026-10-23T06:17:04.0000000Z "
        log = "\n".join(f"{leg}{line}" for line in printed.getvalue().splitlines())
        self.assertEqual(["tests/test_a.py"], list(record_job_seconds.job_seconds_from_log(log)["Linux"]))

    def test_a_suite_that_passes_keeps_the_report_of_a_test_volume_left_attached(self) -> None:
        # test_atomic_file.py's teardown report, from a passing pytest run under the repository's pytest configuration,
        # as the runner starts a suite: its report reaches the runner's output and the step summary.
        scripts = REPOSITORY_ROOT / "skills" / "ejryan89-skill-core" / "scripts"
        with tempfile.TemporaryDirectory() as temporary, pytest_base() as basetemp:
            suite = Path(temporary) / "test_left_attached.py"
            suite.write_text(LEFT_ATTACHED.format(scripts=str(scripts)), encoding="utf-8")
            # The repository's configuration, read where the suite sits outside the repository, would have pytest
            # walk every folder above the suite up to the drive, the shared temporary folder included, whose entries
            # other suites remove while it lists them; --confcutdir keeps the walk inside the suite's own folder.
            command = pytest_command(
                str(suite), "-c", str(REPOSITORY_ROOT / "pyproject.toml"), "--confcutdir", temporary, basetemp=basetemp
            )
            job = Job("left attached", "left attached", 1, lambda: run_process(command, PYTEST_ENVIRONMENT))
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                result = run_jobs([job], verbose=False, workers=1)
        # A failed run ends its message with the end of the suite's own output, where pytest says why, so a reader
        # that quotes only the end of this suite's output, as the mutation gate's reach run does, still shows it.
        self.assertEqual(
            [],
            [failure.job.label for failure in result.failures],
            "".join(failure.report[-1500:] for failure in result.failures),
        )
        report = f"the MS-DOS test volume at {Path('mount')} stays attached: hdiutil detach exited with status 16: busy"
        self.assertEqual({"left attached": [report]}, result.notes)
        self.assertIn(f"NOTE left attached: {report}\n", printed.getvalue())
        summary = step_summary((3, 12, 0), {}, "Mode.", 1, 1, 1.0, [], notes=result.notes)
        self.assertTrue(summary.endswith(f"- Noted: `left attached`\n\n  ```text\n  {report}\n  ```\n\n"), summary)


class NotedReportFixtures(unittest.TestCase):
    """The runner's report of a run whose jobs kept notes: report_run's output and its step summary."""

    def report(self, pool: PoolResult) -> tuple[bool, str, str]:
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            summary = Path(temporary) / "summary.md"
            passed = run_validation.report_run(
                unittest.TestResult(),
                pool,
                2,
                run_validation.Run("Mode.", None, {}, 1.0),
                out,
                {"GITHUB_STEP_SUMMARY": str(summary)},
            )
            return passed, out.getvalue(), summary.read_text(encoding="utf-8")

    def test_a_passing_job_s_notes_are_reported_indented_and_leave_the_run_passing(self) -> None:
        notes = {"tests/a.py": ["ERROR: a volume stays attached", "a second note"]}
        passed, out, summary = self.report(PoolResult([], [], notes))
        self.assertTrue(passed, "a note is no failure")
        self.assertIn(
            f"\n{'=' * 70}\nNOTED tests/a.py\n{'-' * 70}\n  ERROR: a volume stays attached\n  a second note\n", out
        )
        self.assertTrue(out.rstrip().endswith("validation passed."), out)
        self.assertIn(
            "- Noted: `tests/a.py`\n\n  ```text\n  ERROR: a volume stays attached\n  a second note\n  ```\n", summary
        )
        self.assertIn("**validation passed**", summary)

    def test_a_failing_job_s_output_is_reported_whole_as_before_and_never_as_notes(self) -> None:
        failing, _ = job_running(
            "failing suite", [sys.executable, "-c", NOTES + "print('1 test failed')\nsys.exit(1)\n"]
        )
        with contextlib.redirect_stdout(io.StringIO()):
            pool = run_jobs([failing], verbose=False, workers=1)
        self.assertEqual({}, pool.notes)
        passed, out, summary = self.report(pool)
        self.assertFalse(passed)
        self.assertNotIn("NOTED", out)
        self.assertNotIn("Noted", summary)
        block = out[out.index(f"{'=' * 70}\nFAILED failing suite\n{'-' * 70}\nCommand failed with exit code 1: ") :]
        for line in (f"{MARK}the volume stays attached", "a line with no note", "1 test failed"):
            self.assertIn(f"\n{line}", block)
        self.assertIn(f"{MARK}from the error stream", block)


class RunnerDefaultEnvironment(unittest.TestCase):
    """The runner's main as CI starts it, with no environment argument: it reads the step summary's path, the base
    branch, and the worker count from os.environ. Its own policy checks pass an environment, and a policy check may not
    change os.environ beside the pool, so this suite, which runs in its own process, holds the default."""

    def test_main_reads_the_step_summary_the_base_branch_and_the_workers_from_os_environ(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            summary = Path(temporary) / "step summary.md"
            variables = {"GITHUB_STEP_SUMMARY": str(summary), "GITHUB_BASE_REF": "release line", "VALIDATION_JOBS": "3"}
            with (
                mock.patch.dict(os.environ, variables),
                mock.patch.object(run_validation, "tool_versions", return_value={}),
                mock.patch.object(run_validation, "report_prerequisite_problems", return_value=False),
                mock.patch.object(run_validation, "changed_paths", return_value=None) as changed,
                mock.patch.object(run_validation, "all_jobs", return_value=[]),
                mock.patch.object(
                    run_validation, "run_beside", return_value=(unittest.TestResult(), PoolResult([], []))
                ) as beside,
                contextlib.redirect_stdout(output),
            ):
                code = run_validation.main([])
            self.assertTrue(summary.is_file(), "the step summary is written to the file os.environ names")
            text = summary.read_text(encoding="utf-8")
        self.assertEqual(0, code, output.getvalue())
        changed.assert_called_once_with(REPOSITORY_ROOT, "origin/release line")
        self.assertEqual(3, beside.call_args.args[3])
        self.assertIn("suite jobs on 3 workers.", output.getvalue())
        self.assertIn("## Repository validation\n", text)
        self.assertIn("**validation passed**", text)

"""Regression suite for tools/python_diagnostics.py and tools/unraisable_record.py.

The run is started as the workflow starts it, with PYTHONDEVMODE=1 and PYTHONWARNINGS=error in its environment, on
small fixture suites this suite writes to a temporary folder: one that is clean, and one for each diagnostic the run
must report as a failure, a warning raised in the suite's own process or in a child, and an unraisable exception left in
either, the last the one a child's exit status never shows.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "validation"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from flat_text import flat_text
from suite_discovery import PYTEST_ENVIRONMENT, pytest_base, pytest_command, regression_suites

from tools import python_diagnostics, unraisable_record

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPOSITORY_ROOT / "tools" / "python_diagnostics.py"
RUN_SECONDS = 300
DIAGNOSTIC_ENVIRONMENT = {"PYTHONDEVMODE": "1", "PYTHONWARNINGS": "error"}

# Each fixture suite by its file name: what it does and, for each but the clean one, the diagnostic it leaves.
FIXTURES = {
    "test_clean.py": """
        import subprocess, sys

        def test_a_clean_child_and_a_closed_file() -> None:
            with open(__file__, encoding="utf-8") as source:
                source.read()
            assert subprocess.run([sys.executable, "-c", "pass"], check=False).returncode == 0
    """,
    "test_warning.py": """
        import warnings

        def test_a_deprecated_call() -> None:
            warnings.warn("this call goes away", DeprecationWarning, stacklevel=1)
    """,
    "test_unraisable.py": """
        def test_a_file_left_open() -> None:
            open(__file__, encoding="utf-8")
    """,
    "test_child_warning.py": """
        import subprocess, sys

        def test_a_child_that_warns() -> None:
            program = "import warnings; warnings.warn('this call goes away', DeprecationWarning)"
            assert subprocess.run([sys.executable, "-c", program], check=False).returncode == 0
    """,
    "test_child_unraisable.py": """
        import subprocess, sys

        def test_a_child_that_leaves_a_file_open_and_exits_0() -> None:
            program = "open(__import__('os').devnull, encoding='utf-8')"
            assert subprocess.run([sys.executable, "-c", program], check=False).returncode == 0
    """,
}


def run_tool(*arguments: str, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(TOOL), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, **DIAGNOSTIC_ENVIRONMENT} if environment is None else environment,
        cwd=REPOSITORY_ROOT,
        timeout=RUN_SECONDS,
        check=False,
    )


def suite_folder() -> Path:
    """A new folder under the temporary directory for fixture suites, which the caller removes, holding a pytest.ini so
    pytest roots each suite there and collects no folder above it. Without one, pytest roots a suite at the working
    directory, the repository, whenever the two share no path, as on Windows, where the repository and the temporary
    directory lie on different drives. It then collects every folder above the suite, the temporary directory that
    every parallel suite writes in among them, and on Windows matches each entry there against the suite's 8.3 short
    path by a stat that fails, stopping pytest with exit 2, on an entry another suite removes meanwhile."""
    folder = Path(tempfile.mkdtemp(prefix="python-diagnostics-test-"))
    (folder / "pytest.ini").write_bytes(b"[pytest]\n")
    return folder


def facts(output: str, key: str) -> list[list[str]]:
    """The fields of each fact line output prints under key."""
    return [line.split("\t")[1:] for line in output.splitlines() if line.split("\t")[0] == key]


class FixtureRunTests(unittest.TestCase):
    """One run over every fixture suite, read by each test."""

    completed: subprocess.CompletedProcess[str]
    summary: str
    suites: dict[str, str]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        folder = suite_folder()
        cls.addClassCleanup(shutil.rmtree, folder)
        cls.suites = {}
        for name, source in FIXTURES.items():
            (folder / name).write_bytes(textwrap.dedent(source).encode("utf-8"))
            cls.suites[name] = str(folder / name)
        summary = folder / "summary.md"
        cls.completed = run_tool("--summary", str(summary), *cls.suites.values())
        cls.summary = summary.read_text(encoding="utf-8")

    def verdicts(self, key: str) -> dict[str, list[str]]:
        return {fields[0]: fields[1:] for fields in facts(self.completed.stdout, key)}

    def test_the_run_names_its_interpreter_and_settings_and_that_a_child_took_them(self) -> None:
        # The version as a fact line prints it, its runs of spaces made one, as a Windows build's date has.
        self.assertEqual([[sys.executable, flat_text(sys.version)]], facts(self.completed.stdout, "INTERPRETER"))
        self.assertEqual([["PYTHONDEVMODE=1", "PYTHONWARNINGS=error"]], facts(self.completed.stdout, "SETTINGS"))
        [child] = facts(self.completed.stdout, "CHILD")
        self.assertEqual("True", child[0])
        self.assertIn("error", child[1].split(","))
        for line in ("INTERPRETER", "SETTINGS\tPYTHONDEVMODE=1\tPYTHONWARNINGS=error", "CHILD\tTrue"):
            self.assertIn(line.replace("\t", " "), self.summary)

    def test_a_clean_suite_passes(self) -> None:
        self.assertIn(self.suites["test_clean.py"], self.verdicts("PASSED"))
        self.assertNotIn(self.suites["test_clean.py"], self.verdicts("FAILED"))
        self.assertNotIn(self.suites["test_clean.py"], self.verdicts("UNRAISABLE"))

    def test_a_warning_in_the_suites_own_process_fails_it(self) -> None:
        self.assertEqual(["pytest exited 1"], self.verdicts("FAILED")[self.suites["test_warning.py"]])
        self.assertIn("DeprecationWarning: this call goes away", self.completed.stdout)

    def test_an_unraisable_exception_in_the_suites_own_process_fails_it(self) -> None:
        self.assertEqual(["pytest exited 1"], self.verdicts("FAILED")[self.suites["test_unraisable.py"]])
        self.assertIn("PytestUnraisableExceptionWarning", self.completed.stdout)

    def test_a_warning_a_child_raises_ends_the_child_and_fails_the_suite(self) -> None:
        self.assertEqual(["pytest exited 1"], self.verdicts("FAILED")[self.suites["test_child_warning.py"]])

    def test_an_unraisable_exception_in_a_child_that_exits_0_fails_the_run_through_its_record(self) -> None:
        suite = self.suites["test_child_unraisable.py"]
        self.assertNotIn(suite, self.verdicts("FAILED"), "the suite's own test passes: the child exited 0")
        self.assertNotIn(suite, self.verdicts("PASSED"))
        [record] = [fields for fields in facts(self.completed.stdout, "UNRAISABLE") if fields[0] == suite]
        self.assertIn("ResourceWarning: unclosed file", record[1])
        self.assertIn(f"| `{suite}` | 1 unraisable recorded |", self.summary)

    def test_the_run_exits_1_when_any_suite_failed(self) -> None:
        self.assertEqual(1, self.completed.returncode, self.completed.stderr)


class FixtureFolderTests(unittest.TestCase):
    def test_a_fixture_suite_is_collected_from_its_own_folder_alone(self) -> None:
        folder = suite_folder()
        self.addCleanup(shutil.rmtree, folder)
        suite = folder / "test_clean.py"
        suite.write_bytes(textwrap.dedent(FIXTURES["test_clean.py"]).encode("utf-8"))
        with pytest_base() as basetemp:
            completed = subprocess.run(
                [*pytest_command(str(suite), basetemp=basetemp), "--collect-only"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, **PYTEST_ENVIRONMENT},
                cwd=REPOSITORY_ROOT,
                timeout=RUN_SECONDS,
                check=False,
            )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        lines = [line.strip() for line in completed.stdout.splitlines()]
        self.assertIn("configfile: pytest.ini", lines, completed.stdout)
        [root] = [line.removeprefix("rootdir: ") for line in lines if line.startswith("rootdir: ")]
        self.assertTrue(Path(root).samefile(folder), root)
        tree = [line for line in lines if line.startswith("<")]
        self.assertEqual(f"<Dir {folder.name}>", tree[0], "no folder above the suite's own is collected")


class CleanRunTests(unittest.TestCase):
    def test_a_run_whose_every_suite_passes_exits_0(self) -> None:
        folder = suite_folder()
        self.addCleanup(shutil.rmtree, folder)
        suite = folder / "test_clean.py"
        suite.write_bytes(textwrap.dedent(FIXTURES["test_clean.py"]).encode("utf-8"))
        completed = run_tool(str(suite))
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual([[str(suite)]], [fields[:1] for fields in facts(completed.stdout, "PASSED")])


class ProbeFailureRunTests(unittest.TestCase):
    def test_a_run_whose_probe_fails_runs_no_suite_and_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            silent = Path(temporary) / "silent_recorder.py"
            silent.write_bytes(b"# Records nothing.\n")
            summary = Path(temporary) / "summary.md"
            output = io.StringIO()
            with (
                mock.patch.object(python_diagnostics, "RECORDER", silent),
                contextlib.redirect_stdout(output),
            ):
                status = python_diagnostics.run(
                    [str(REPOSITORY_ROOT / python_diagnostics.DIAGNOSTIC_SUITES[0])],
                    {**os.environ, **DIAGNOSTIC_ENVIRONMENT},
                    summary,
                )
            written = summary.read_text(encoding="utf-8")
        self.assertEqual(1, status)
        self.assertEqual(
            [["the probe recorded no unraisable exception, so the recorder did not reach it"]],
            facts(output.getvalue(), "CHILD_UNSET"),
        )
        self.assertEqual([], facts(output.getvalue(), "PASSED") + facts(output.getvalue(), "FAILED"))
        self.assertIn("No suite ran: the probe recorded no unraisable exception", written)


class SuiteTimeTests(unittest.TestCase):
    def test_a_suite_that_runs_past_its_time_is_ended_and_fails(self) -> None:
        folder = suite_folder()
        self.addCleanup(shutil.rmtree, folder)
        suite = folder / "test_sleeping.py"
        suite.write_bytes(b"import time\n\n\ndef test_sleeps() -> None:\n    time.sleep(60)\n")
        record = folder / "record"
        record.mkdir()
        environment = python_diagnostics.child_environment(dict(os.environ), folder, record)
        with mock.patch.object(python_diagnostics, "SUITE_SECONDS", 1.0):
            outcome = python_diagnostics.run_suite(str(suite), environment, record)
        self.assertEqual("it ran past 1 seconds", outcome.failure)
        self.assertFalse(outcome.passed)
        self.assertLess(outcome.seconds, 30)


class BaseFolderTests(unittest.TestCase):
    def test_a_suite_keeps_its_temporary_folders_in_a_base_folder_of_its_own(self) -> None:
        # pytest makes the base every pytest process shares, pytest-of-<user>, in the folder PYTEST_DEBUG_TEMPROOT
        # names, so a run that used it would leave it in `shared`.
        folder = suite_folder()
        self.addCleanup(shutil.rmtree, folder)
        suite = folder / "test_temporary.py"
        suite.write_bytes(b"def test_makes_a_folder(tmp_path):\n    (tmp_path / 'made').write_bytes(b'')\n")
        record, shared = folder / "record", folder / "shared"
        record.mkdir()
        shared.mkdir()
        inherited = {**os.environ, "PYTEST_DEBUG_TEMPROOT": str(shared)}
        environment = python_diagnostics.child_environment(inherited, folder, record)
        outcome = python_diagnostics.run_suite(str(suite), environment, record)
        self.assertIsNone(outcome.failure, outcome.output)
        self.assertEqual([], list(shared.iterdir()))


# Fixture suites whose child meets an unraisable exception the recorder cannot write as it would, each exiting 0 as a
# careless test expects, run apart from FIXTURES so the lines that start pytest above keep their place.
UNRECORDED_FIXTURES = {
    "test_child_unrecorded.py": """
        import os, subprocess, sys

        def test_a_child_that_cannot_write_its_record_leaves_a_file_open_and_is_read_as_exiting_0() -> None:
            program = "open(__import__('os').devnull, encoding='utf-8')"
            environment = {**os.environ, "PYTHON_DIAGNOSTICS_RECORD": os.path.join(os.devnull, "missing")}
            assert subprocess.run([sys.executable, "-c", program], env=environment, check=False).returncode == 0
    """,
    "test_child_torn_exception.py": """
        import subprocess, sys

        TORN = (
            "class Torn(Exception):\\n"
            "    def __str__(self):\\n"
            "        raise RuntimeError('torn')\\n"
            "class Holder:\\n"
            "    def __del__(self):\\n"
            "        raise Torn()\\n"
            "Holder()\\n"
        )

        def test_a_child_whose_unraisable_exception_cannot_be_shown_exits_0() -> None:
            assert subprocess.run([sys.executable, "-c", TORN], check=False).returncode == 0
    """,
}


class UnrecordedFixtureRunTests(unittest.TestCase):
    """One run over the fixture suites whose child's unraisable exception the recorder cannot write as it would."""

    completed: subprocess.CompletedProcess[str]
    suites: dict[str, str]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        folder = suite_folder()
        cls.addClassCleanup(shutil.rmtree, folder)
        cls.suites = {}
        for name, source in UNRECORDED_FIXTURES.items():
            (folder / name).write_bytes(textwrap.dedent(source).encode("utf-8"))
            cls.suites[name] = str(folder / name)
        cls.completed = run_tool(*cls.suites.values())

    def verdicts(self, key: str) -> dict[str, list[str]]:
        return {fields[0]: fields[1:] for fields in facts(self.completed.stdout, key)}

    def test_a_child_that_cannot_write_its_record_ends_with_its_own_status_and_fails_the_suite(self) -> None:
        suite = self.suites["test_child_unrecorded.py"]
        self.assertEqual(["pytest exited 1"], self.verdicts("FAILED")[suite])
        self.assertIn(f"{unraisable_record.UNRECORDED_STATUS} == 0", self.completed.stdout)
        self.assertIn("could not record an unraisable exception", self.completed.stdout)

    def test_an_unraisable_exception_whose_text_raises_is_still_recorded(self) -> None:
        suite = self.suites["test_child_torn_exception.py"]
        self.assertNotIn(suite, self.verdicts("FAILED"), "the suite's own test passes: the child exited 0")
        self.assertNotIn(suite, self.verdicts("PASSED"))
        [record] = [fields for fields in facts(self.completed.stdout, "UNRAISABLE") if fields[0] == suite]
        self.assertIn("Torn: <exception str() failed>", record[1])

    def test_the_run_exits_1(self) -> None:
        self.assertEqual(1, self.completed.returncode, self.completed.stderr)


class UsageTests(unittest.TestCase):
    def test_a_run_whose_environment_lacks_either_setting_is_refused(self) -> None:
        for missing in DIAGNOSTIC_ENVIRONMENT:
            with self.subTest(missing=missing):
                environment = {**os.environ, **DIAGNOSTIC_ENVIRONMENT}
                del environment[missing]
                completed = run_tool(environment=environment)
                self.assertEqual(2, completed.returncode)
                self.assertIn(f"set {missing}=", completed.stderr)
                self.assertEqual("", completed.stdout)

    def test_a_run_whose_interpreter_ignored_the_settings_is_refused(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-B", "-E", str(TOOL)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, **DIAGNOSTIC_ENVIRONMENT},
            cwd=REPOSITORY_ROOT,
            timeout=RUN_SECONDS,
            check=False,
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("start it without -I or -E", completed.stderr)

    def test_a_suite_that_is_missing_or_not_a_file_is_refused_with_its_reason(self) -> None:
        completed = run_tool("tests/no_such_suite.py", "tests")
        self.assertEqual(2, completed.returncode)
        self.assertIn("suite tests/no_such_suite.py is missing; suite tests is not a file", completed.stderr)
        self.assertEqual("", completed.stdout)


class ProbeTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.site, self.record = Path(temporary.name) / "site", Path(temporary.name) / "record"
        self.site.mkdir()
        self.record.mkdir()
        (self.site / "sitecustomize.py").write_bytes(python_diagnostics.RECORDER.read_bytes())

    def environment(self, **settings: str) -> dict[str, str]:
        inherited = {name: value for name, value in os.environ.items() if name not in DIAGNOSTIC_ENVIRONMENT}
        return python_diagnostics.child_environment({**inherited, **settings}, self.site, self.record)

    def test_a_child_that_takes_the_settings_and_the_recorder_passes(self) -> None:
        fields, problem = python_diagnostics.probe_problem(self.environment(**DIAGNOSTIC_ENVIRONMENT), self.record)
        self.assertIsNone(problem)
        self.assertTrue(fields.startswith("True\t"), fields)
        self.assertEqual([], list(self.record.iterdir()), "the probe's record is taken before the suites run")

    def test_a_child_outside_development_mode_is_named(self) -> None:
        _, problem = python_diagnostics.probe_problem(self.environment(PYTHONWARNINGS="error"), self.record)
        self.assertEqual("the probe did not run in development mode with warnings as errors", problem)

    def test_a_probe_that_exits_non_zero_is_named_with_its_status(self) -> None:
        # The probe runs to its end, then exits 3 as it shuts down.
        (self.site / "sitecustomize.py").write_bytes(b"import atexit, os\natexit.register(os._exit, 3)\n")
        _, problem = python_diagnostics.probe_problem(self.environment(**DIAGNOSTIC_ENVIRONMENT), self.record)
        self.assertIsNotNone(problem)
        self.assertTrue(str(problem).startswith("the probe exited 3: "), problem)

    def test_a_probe_that_prints_no_json_is_named(self) -> None:
        (self.site / "sitecustomize.py").write_bytes(b"print('not the settings')\n")
        _, problem = python_diagnostics.probe_problem(self.environment(**DIAGNOSTIC_ENVIRONMENT), self.record)
        self.assertIsNotNone(problem)
        self.assertTrue(str(problem).startswith("the probe printed no settings: "), problem)

    def test_a_child_the_recorder_does_not_reach_is_named(self) -> None:
        environment = self.environment(**DIAGNOSTIC_ENVIRONMENT)
        environment["PYTHONPATH"] = os.pathsep.join(environment["PYTHONPATH"].split(os.pathsep)[1:])
        _, problem = python_diagnostics.probe_problem(environment, self.record)
        self.assertEqual("the probe recorded no unraisable exception, so the recorder did not reach it", problem)


class RecordReadingTests(unittest.TestCase):
    def test_an_empty_record_is_read_as_an_unraisable_exception_left_unrecorded_and_every_record_is_taken(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary)
            (record / "11.txt").write_bytes(b"")
            (record / "12.txt").write_bytes(b"['program'] Exception ignored in: None: ResourceWarning: unclosed\n")
            (record / "13.txt").write_bytes(b"\n \n")
            taken = python_diagnostics.taken_records(record)
            self.assertEqual([], list(record.iterdir()))
        self.assertEqual(
            (
                f"11.txt {python_diagnostics.EMPTY_RECORD}",
                "['program'] Exception ignored in: None: ResourceWarning: unclosed",
                f"13.txt {python_diagnostics.EMPTY_RECORD}",
            ),
            taken,
        )


class MissingRecordFolderTests(unittest.TestCase):
    def test_a_suite_after_which_the_record_folder_is_gone_fails_never_reads_as_recording_nothing(self) -> None:
        folder = suite_folder()
        self.addCleanup(shutil.rmtree, folder)
        suite = folder / "test_clean.py"
        suite.write_bytes(b"def test_passes() -> None:\n    pass\n")
        record = folder / "record"
        environment = python_diagnostics.child_environment(dict(os.environ), folder / "site", record)
        outcome = python_diagnostics.run_suite(str(suite), environment, record)
        self.assertEqual(
            f"its record folder {record} is gone, so nothing its processes met was recorded", outcome.failure
        )
        self.assertFalse(outcome.passed)


class SettingsTests(unittest.TestCase):
    def test_each_setting_is_required_in_the_environment_and_taken_by_the_interpreter(self) -> None:
        problem = python_diagnostics.settings_problem
        self.assertIsNone(problem(DIAGNOSTIC_ENVIRONMENT, True, ["default", "error"]))
        self.assertEqual(
            "set PYTHONDEVMODE=1 and PYTHONWARNINGS=error in the environment that starts the run",
            problem({}, True, ["error"]),
        )
        self.assertEqual(
            "set PYTHONWARNINGS=error in the environment that starts the run",
            problem({"PYTHONDEVMODE": "1", "PYTHONWARNINGS": "default"}, True, ["error"]),
        )
        ignored = "the interpreter ignored PYTHONDEVMODE or PYTHONWARNINGS; start it without -I or -E"
        self.assertEqual(ignored, problem(DIAGNOSTIC_ENVIRONMENT, False, ["error"]))
        self.assertEqual(ignored, problem(DIAGNOSTIC_ENVIRONMENT, True, ["default"]))

    def test_the_childrens_environment_puts_the_recorder_first_and_keeps_the_search_path(self) -> None:
        site, record = Path("site"), Path("record")
        environment = python_diagnostics.child_environment({"PYTHONPATH": "kept", "PYTEST_ADDOPTS": "-x"}, site, record)
        self.assertEqual(os.pathsep.join([str(site), "kept"]), environment["PYTHONPATH"])
        self.assertEqual(str(record), environment[unraisable_record.RECORD_VARIABLE])
        self.assertEqual("", environment["PYTEST_ADDOPTS"])
        self.assertEqual(str(site), python_diagnostics.child_environment({}, site, record)["PYTHONPATH"])

    def test_every_chosen_suite_is_a_regression_suite_the_runner_runs(self) -> None:
        runner = {path.relative_to(REPOSITORY_ROOT).as_posix() for path in regression_suites()}
        self.assertEqual([], [suite for suite in python_diagnostics.DIAGNOSTIC_SUITES if suite not in runner])
        self.assertEqual(len(set(python_diagnostics.DIAGNOSTIC_SUITES)), len(python_diagnostics.DIAGNOSTIC_SUITES))


class RecorderTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        previous = sys.unraisablehook
        self.addCleanup(setattr, sys, "unraisablehook", previous)

    def unraisable(self) -> sys.UnraisableHookArgs:
        error = ResourceWarning("unclosed file\nsecond line")
        arguments = SimpleNamespace(
            exc_type=ResourceWarning, exc_value=error, exc_traceback=None, err_msg="Exception ignored while", object=1
        )
        return cast("sys.UnraisableHookArgs", arguments)

    def test_without_a_record_folder_nothing_is_installed(self) -> None:
        hook = sys.unraisablehook
        self.assertFalse(unraisable_record.install({}))
        self.assertIs(hook, sys.unraisablehook)

    def test_each_unraisable_exception_is_appended_on_one_line_and_handed_on(self) -> None:
        handed: list[Any] = []
        sys.unraisablehook = handed.append
        self.assertTrue(unraisable_record.install({unraisable_record.RECORD_VARIABLE: str(self.folder)}))
        sys.unraisablehook(self.unraisable())
        sys.unraisablehook(self.unraisable())
        [record] = list(self.folder.iterdir())
        self.assertEqual(f"{os.getpid()}.txt", record.name)
        lines = record.read_text(encoding="utf-8").splitlines()
        self.assertEqual(2, len(lines))
        self.assertTrue(
            lines[0].endswith("Exception ignored while: 1: ResourceWarning: unclosed file second line"), lines[0]
        )
        self.assertEqual(2, len(handed), "the hook it replaced still prints each one")

    def test_a_record_folder_that_cannot_be_written_hands_the_exception_on_and_ends_the_process_by_its_status(
        self,
    ) -> None:
        handed: list[Any] = []
        sys.unraisablehook = handed.append
        missing = self.folder / "missing"
        unraisable = self.unraisable()
        said = self.ending_hook(missing, unraisable)
        self.assertEqual([unraisable], handed, "the hook it replaced still prints the exception")
        self.assertTrue(said.startswith(f"unraisable_record: could not record an unraisable exception in {missing}: "))
        self.assertNotIn(unraisable_record.UNFINISHED, said, "an OSError names its own cause")
        self.assertEqual([], list(self.folder.iterdir()))

    def test_a_line_stopped_by_anything_but_an_os_error_also_ends_the_process_by_its_status(self) -> None:
        class Interrupting:
            @override
            def __repr__(self) -> str:
                raise KeyboardInterrupt

        handed: list[Any] = []
        sys.unraisablehook = handed.append
        arguments = SimpleNamespace(
            exc_type=ResourceWarning, exc_value=None, exc_traceback=None, err_msg=None, object=Interrupting()
        )
        unraisable = cast("sys.UnraisableHookArgs", arguments)
        said = self.ending_hook(self.folder, unraisable)
        self.assertEqual([unraisable], handed, "the hook it replaced still prints the exception")
        self.assertEqual(
            f"unraisable_record: could not record an unraisable exception in {self.folder}: "
            f"{unraisable_record.UNFINISHED}\n",
            said,
        )
        self.assertEqual([], list(self.folder.iterdir()))

    def ending_hook(self, folder: Path, unraisable: sys.UnraisableHookArgs) -> str:
        """Install the hook recording in folder and run it on unraisable, which must end the process with
        UNRECORDED_STATUS, and return what it wrote to the error output. os._exit is replaced before the hook is
        installed, since the hook takes it then."""
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        saved = os.dup(2)
        self.addCleanup(os.close, saved)
        os.dup2(write, 2)
        os.close(write)
        try:
            with mock.patch.object(os, "_exit", side_effect=SystemExit) as ended:
                self.assertTrue(unraisable_record.install({unraisable_record.RECORD_VARIABLE: str(folder)}))
                with self.assertRaises(SystemExit):
                    sys.unraisablehook(unraisable)
        finally:
            os.dup2(saved, 2)
        ended.assert_called_once_with(unraisable_record.UNRECORDED_STATUS)
        return os.read(read, 65536).decode("utf-8")

    def run_child(self, program: str, record: Path) -> subprocess.CompletedProcess[str]:
        """Run program in a Python child that takes the run's settings and the recorder, recording in record."""
        site = self.folder / "site"
        site.mkdir(exist_ok=True)
        (site / "sitecustomize.py").write_bytes(python_diagnostics.RECORDER.read_bytes())
        environment = python_diagnostics.child_environment({**os.environ, **DIAGNOSTIC_ENVIRONMENT}, site, record)
        return subprocess.run(
            [sys.executable, "-B", "-c", program],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=RUN_SECONDS,
            check=False,
        )

    def test_a_child_that_cannot_write_its_record_exits_with_the_unrecorded_status(self) -> None:
        missing = self.folder / "missing"
        completed = self.run_child("open(__import__('os').devnull, encoding='utf-8')", missing)
        self.assertEqual(unraisable_record.UNRECORDED_STATUS, completed.returncode, completed.stderr)
        self.assertIn("ResourceWarning: unclosed file", completed.stderr, "the exception is still printed")
        self.assertIn(f"could not record an unraisable exception in {missing}: ", completed.stderr)

    def test_a_child_that_meets_one_after_shutdown_cleared_the_modules_exits_with_the_unrecorded_status(self) -> None:
        # A file sys still holds under an underscore name is released after the interpreter has cleared every other
        # module's globals, the recorder's, os's, and pathlib's among them, so its line can no longer be built, but
        # before it clears sys.unraisablehook, which sys's names without an underscore go with.
        record = self.folder / "record"
        record.mkdir()
        completed = self.run_child("import os, sys\nsys._kept = open(os.devnull, encoding='utf-8')\n", record)
        self.assertEqual(unraisable_record.UNRECORDED_STATUS, completed.returncode, completed.stderr)
        self.assertIn(
            f"could not record an unraisable exception in {record}: {unraisable_record.UNFINISHED}", completed.stderr
        )

    def test_a_child_ended_unrecorded_keeps_the_output_it_had_buffered(self) -> None:
        program = "print('result before')\nopen(__import__('os').devnull, encoding='utf-8')\nprint('result after')\n"
        completed = self.run_child(program, self.folder / "missing")
        self.assertEqual(unraisable_record.UNRECORDED_STATUS, completed.returncode, completed.stderr)
        self.assertEqual("result before\n", completed.stdout)

    def test_an_exception_with_no_value_is_shown_by_its_type_name(self) -> None:
        handed: list[Any] = []
        sys.unraisablehook = handed.append
        self.assertTrue(unraisable_record.install({unraisable_record.RECORD_VARIABLE: str(self.folder)}))
        arguments = SimpleNamespace(
            exc_type=ResourceWarning, exc_value=None, exc_traceback=None, err_msg=None, object=None
        )
        sys.unraisablehook(cast("sys.UnraisableHookArgs", arguments))
        [record] = list(self.folder.iterdir())
        line = record.read_text(encoding="utf-8")
        self.assertTrue(line.endswith("Exception ignored in: None: ResourceWarning\n"), line)
        self.assertEqual(1, len(handed))

    def test_an_object_whose_repr_raises_and_an_exception_whose_str_raises_are_still_recorded(self) -> None:
        class Torn(Exception):
            @override
            def __str__(self) -> str:
                raise RuntimeError("torn")

        class Unshown:
            @override
            def __repr__(self) -> str:
                raise RuntimeError("torn")

        handed: list[Any] = []
        sys.unraisablehook = handed.append
        self.assertTrue(unraisable_record.install({unraisable_record.RECORD_VARIABLE: str(self.folder)}))
        arguments = SimpleNamespace(exc_type=Torn, exc_value=Torn(), exc_traceback=None, err_msg=None, object=Unshown())
        sys.unraisablehook(cast("sys.UnraisableHookArgs", arguments))
        [record] = list(self.folder.iterdir())
        line = record.read_text(encoding="utf-8")
        self.assertRegex(line, r"Exception ignored in: <Unshown instance at 0x[0-9a-f]+>: ")
        self.assertTrue(line.endswith("Torn: <exception str() failed>\n"), line)
        self.assertEqual(1, len(handed))

    def test_text_the_record_cannot_encode_is_written_escaped(self) -> None:
        handed: list[Any] = []
        sys.unraisablehook = handed.append
        self.assertTrue(unraisable_record.install({unraisable_record.RECORD_VARIABLE: str(self.folder)}))
        arguments = SimpleNamespace(
            exc_type=ResourceWarning,
            exc_value=ResourceWarning("name \udcff"),
            exc_traceback=None,
            err_msg=None,
            object=None,
        )
        sys.unraisablehook(cast("sys.UnraisableHookArgs", arguments))
        [record] = list(self.folder.iterdir())
        self.assertTrue(record.read_text(encoding="utf-8").endswith("ResourceWarning: name \\udcff\n"))

    def test_the_process_is_named_by_its_arguments_when_shutdown_has_cleared_them(self) -> None:
        handed: list[Any] = []
        sys.unraisablehook = handed.append
        arguments = ["program.py", "an argument"]
        with mock.patch.object(sys, "argv", arguments):
            self.assertTrue(unraisable_record.install({unraisable_record.RECORD_VARIABLE: str(self.folder)}))
            arguments[0] = "/path/to/module.py"
            with mock.patch.object(sys, "argv", None):
                sys.unraisablehook(self.unraisable())
        [record] = list(self.folder.iterdir())
        line = record.read_text(encoding="utf-8")
        self.assertTrue(line.startswith("['/path/to/module.py', 'an argument'] Exception ignored while"), line)

    def test_the_sitecustomize_it_hides_still_runs_and_its_own_folder_is_passed_over(self) -> None:
        own, other = self.folder / "own", self.folder / "other"
        own.mkdir()
        other.mkdir()
        (own / "sitecustomize.py").write_bytes(b"raise AssertionError('its own copy runs again')\n")
        self.assertFalse(unraisable_record.run_shadowed(own, [str(own)]))
        (other / "sitecustomize.py").write_bytes(b"import sys\nsys.shadowed_ran = True\n")
        self.addCleanup(lambda: sys.__dict__.pop("shadowed_ran", None))
        self.assertTrue(unraisable_record.run_shadowed(own, [str(own), str(other)]))
        self.assertIs(True, sys.__dict__.get("shadowed_ran"))

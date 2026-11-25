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

    def test_a_record_folder_that_cannot_be_written_raises_and_still_hands_the_exception_on(self) -> None:
        handed: list[Any] = []
        sys.unraisablehook = handed.append
        self.assertTrue(unraisable_record.install({unraisable_record.RECORD_VARIABLE: str(self.folder / "missing")}))
        unraisable = self.unraisable()
        with self.assertRaises(FileNotFoundError):
            sys.unraisablehook(unraisable)
        self.assertEqual([unraisable], handed, "the hook it replaced still prints the exception")

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

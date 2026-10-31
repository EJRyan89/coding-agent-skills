"""The mutation gate: which modules it mutates, how it orders and describes the mutants, which suites a mutant runs
against and what each outcome means, the time cap, the summary, and a whole run over a fixture repository's real
suites with a stub generator and reach, since mutmut and coverage.py are installed only on the gate's runner."""

from __future__ import annotations

import ast
import contextlib
import importlib
import io
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "validation"))

from fsops_platform import PLATFORM_TOKENS
from git_client import GitClient

from deployer import platform_support
from tools import mutation_gate
from tools.mutation_gate import (
    ERRORED,
    KILLED,
    LINUX_LEG,
    NOT_REACHED,
    NOT_RUN,
    PASSED,
    SURVIVED,
    TIMED_OUT,
    WINDOWS_LEG,
    Gate,
    LegRun,
    ModuleReport,
    Mutant,
    Outcome,
    Reach,
    ReachRunner,
    RecordError,
    RecordFieldError,
    Report,
    Selected,
    SelectedModule,
    SuiteRunner,
    Workers,
    changed_lines,
    changed_modules,
    changed_tables,
    code,
    context_tests,
    counts,
    coverage_settings,
    describe,
    executed_lines,
    executed_tests,
    generator_problem,
    is_gate_module,
    joined,
    kill_ratio,
    leg_modules,
    leg_record,
    module_mutants,
    named_in,
    needed_legs,
    no_module_line,
    policy_tables,
    queue_order,
    read_leg,
    read_selection,
    run_suite,
    runs_on_windows,
    sanction_table,
    selection_record,
    suite_plan,
    suite_selection,
    summary,
    try_mutant,
    try_mutants,
    unfinished_line,
    verdict,
)


@pytest.mark.parametrize(
    ("path", "mutated"),
    [
        ("deployer/names.py", True),
        ("deployer/__init__.py", True),
        ("tools/worktrees.py", True),
        ("skills/ejryan89-skill-core/scripts/console.py", True),
        ("skills/ejryan89-code-review-core/scripts/hosts/copilot.py", True),
        (".claude/skills/implement-change/scripts/pull_request.py", True),
        ("skills/ejryan89-skill-core/scripts/test_console.py", False),
        ("tools/conftest.py", False),
        ("tests/validation/job_selection.py", False),
        ("tests/fixtures/repo/tools/calc.py", False),
        ("skills/ejryan89-skill-core/SKILL.md", False),
        ("skills/ejryan89-skill-core/helper.py", False),
        ("deploy.py", False),
        ("conftest.py", False),
        ("tools/worktrees-powershell.ps1", False),
    ],
)
def test_the_gate_mutates_modules_under_its_roots_and_never_a_suite(path: str, mutated: bool) -> None:
    assert mutated == is_gate_module(path)


def plumbing(root: Path, *arguments: str, data: bytes = b"") -> str:
    """What a git plumbing command run in `root` prints, given `data` as its stdin, stripped."""
    identity = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.com"]
    done = subprocess.run(["git", "-C", str(root), *identity, *arguments], input=data, capture_output=True, check=True)
    return done.stdout.decode("utf-8").strip()


def test_the_gate_reads_a_changed_module_whose_path_holds_a_non_ascii_character_or_a_newline() -> None:
    # Built from trees, since Git for Windows refuses a newline in a path in the index; git quotes both names in a
    # listing without -z.
    awkward = ["line\nbreak.py", "modülé.py"]
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        plumbing(root, "init", "--quiet", "--initial-branch=main")
        blob = plumbing(root, "hash-object", "-w", "--stdin", data=b"VALUE = 1\n")
        commits: list[str] = []
        for names in (["calc.py"], ["calc.py", *awkward]):
            tools = plumbing(
                root, "mktree", "-z", data="".join(f"100644 blob {blob}\t{name}\0" for name in names).encode()
            )
            tree = plumbing(root, "mktree", "-z", data=f"040000 tree {tools}\ttools\0".encode())
            parents = [option for commit in commits for option in ("-p", commit)]
            commits.append(plumbing(root, "commit-tree", tree, *parents, "-m", "commit"))
        plumbing(root, "update-ref", "refs/heads/main", commits[0])
        plumbing(root, "update-ref", "refs/heads/feature", commits[1])
        plumbing(root, "symbolic-ref", "HEAD", "refs/heads/feature")
        assert [f"tools/{name}" for name in awkward] == changed_modules(root, "main", GitClient())


def mutant(line: int, module: str = "tools/calc.py") -> Mutant:
    return Mutant(module, line, f"line {line}", f"mutated {line}", f"source {line}")


def line_generator(module: str, source: str, lines: frozenset[int]) -> list[str]:
    """A mutant of each given line that holds a digit, its first digit made 9, as mutmut's line filter would give."""
    made = []
    for number, text in enumerate(source.splitlines(keepends=True), start=1):
        if number in lines and re.search(r"[0-8]", text):
            rest = source.splitlines(keepends=True)
            rest[number - 1] = re.sub(r"[0-8]", "9", text, count=1)
            made.append("".join(rest))
    return made


MULTILINE_CALC = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "\n"
    "def scaled(a):\n"
    "    return add(\n"
    "        a,\n"
    "        a * 2,\n"
    "    )\n"
    "\n"
    "\n"
    "def unused(a):\n"
    "    return add(\n"
    "        a,\n"
    "        a - 1,\n"
    "    )\n"
)
# What coverage.py 7.16.2's `coverage json` wrote for MULTILINE_CALC after a run that called scaled() alone, its other
# fields left out: each statement by its first line, so the multi-line calls' continuation lines are in no list.
MULTILINE_REPORT = {
    "files": {"tools/calc.py": {"executed_lines": [1, 2, 5, 6, 12], "missing_lines": [13], "excluded_lines": []}}
}


# A module and its suite, as tests/tools/test_calc.py of a fixture repository, whose lines coverage.py records with the
# test functions that ran them: add() runs in a set-up too, Base's test_negate() is inherited, so it runs as both
# Base's and CalcTests' test, and square() runs only in a process a test starts.
CONTEXT_CALC = """import sys


def add(a, b):
    return a + b


def double(a):
    return add(
        a,
        a,
    )


def negate(a):
    return -a


def square(a):
    return a * a


if __name__ == "__main__":
    print(square(int(sys.argv[1])))
"""
CONTEXT_SUITE = """import subprocess
import sys
import unittest
from pathlib import Path

CALC = Path(__file__).resolve().parents[2] / "tools" / "calc.py"
sys.path.insert(0, str(CALC.parent))

import calc


class Base(unittest.TestCase):
    def test_negate(self) -> None:
        self.assertEqual(-2, calc.negate(2))


class CalcTests(Base):
    def setUp(self) -> None:
        self.four = calc.add(2, 2)

    def test_double(self) -> None:
        self.assertEqual(4, calc.double(2))

    def test_square_in_a_process(self) -> None:
        done = subprocess.run([sys.executable, str(CALC), "3"], capture_output=True, text=True, check=True)
        self.assertEqual("9", done.stdout.strip())


def test_add() -> None:
    assert calc.add(1, 2) == 3
"""
CONTEXT_SUITE_PATH = "tests/tools/test_calc.py"
# What coverage.py 7.16.2's `coverage json --show-contexts` wrote for CONTEXT_CALC after pytest ran CONTEXT_SUITE with
# COVERAGE_PROCESS_START naming coverage_settings()'s file, its other fields left out. The empty context is code that
# ran outside every test function: at import and in setUp(). The inherited test_negate() is named by Base, the class
# that defines it, and the process test_square_in_a_process() started by that test's node id and phase.
SQUARE_CALL = "tests/tools/test_calc.py::CalcTests::test_square_in_a_process (call)"
CONTEXT_REPORT = {
    "files": {
        "tools/calc.py": {
            "executed_lines": [1, 4, 5, 8, 9, 15, 16, 19, 20, 23, 24],
            "missing_lines": [],
            "excluded_lines": [],
            "contexts": {
                "1": ["", SQUARE_CALL],
                "4": ["", SQUARE_CALL],
                "5": ["", "test_calc.CalcTests.test_double", "test_calc.test_add"],
                "8": ["", SQUARE_CALL],
                "15": ["", SQUARE_CALL],
                "19": ["", SQUARE_CALL],
                "20": [SQUARE_CALL],
                "23": ["", SQUARE_CALL],
                "24": [SQUARE_CALL],
                "16": ["test_calc.Base.test_negate"],
                "9": ["test_calc.CalcTests.test_double"],
                "10": ["test_calc.CalcTests.test_double"],
                "11": ["test_calc.CalcTests.test_double"],
            },
        }
    }
}
# What `pytest tests/tools/test_calc.py --collect-only -q` listed for CONTEXT_SUITE.
CONTEXT_COLLECTED = [
    "tests/tools/test_calc.py::Base::test_negate",
    "tests/tools/test_calc.py::CalcTests::test_double",
    "tests/tools/test_calc.py::CalcTests::test_negate",
    "tests/tools/test_calc.py::CalcTests::test_square_in_a_process",
    "tests/tools/test_calc.py::test_add",
]
# double()'s statement, lines 9 to 14, only a test function ran, and square()'s, lines 20 to 22, and the call on line
# 24 only the process one test's call started: every other line runs the whole suite.
SQUARE_TEST = SQUARE_CALL.removesuffix(" (call)")
CONTEXT_TESTS = {
    "tools/calc.py": {
        **dict.fromkeys(range(9, 15), frozenset({"tests/tools/test_calc.py::CalcTests::test_double"})),
        **dict.fromkeys([20, 21, 22, 24], frozenset({SQUARE_TEST})),
    }
}


def context_tree(root: Path) -> None:
    """CONTEXT_CALC and CONTEXT_SUITE in a tree, as tools/calc.py and tests/tools/test_calc.py."""
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "tools").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "calc.py").write_text(CONTEXT_CALC, encoding="utf-8")
    (root / CONTEXT_SUITE_PATH).write_text(CONTEXT_SUITE, encoding="utf-8")


class MutantFixtures(unittest.TestCase):
    def test_a_zero_context_diff_names_each_added_or_changed_line(self) -> None:
        diff = "@@ -3 +3 @@\n-a\n+b\n@@ -10,0 +11,2 @@\n+c\n+d\n@@ -20,2 +23,0 @@\n-e\n-f\n"
        self.assertEqual({3, 11, 12}, changed_lines(diff))

    def test_a_mutant_is_described_by_its_first_changed_line_on_one_line(self) -> None:
        original = "def add(a):\n    return a + 1\n"
        self.assertEqual((2, "return a + 1", "return a - 1"), describe(original, "def add(a):\n    return a - 1\n"))
        with self.assertRaises(ValueError):
            describe(original, original)

    def test_only_mutants_on_changed_lines_are_kept_in_line_order_and_duplicates_and_the_module_are_dropped(
        self,
    ) -> None:
        source = "a = 1\nb = 2\nc = 3\nd = 4\n"
        made = ["a = 1\nb = 2\nc = 3\nd = 5\n", "a = 0\nb = 2\nc = 3\nd = 4\n", source, "a = 1\nb = 9\nc = 3\nd = 4\n"]
        made += [source, "a = 1\nb = 9\nc = 3\nd = 4\n"]
        asked: list[frozenset[int]] = []

        def generate(module: str, text: str, lines: frozenset[int]) -> list[str]:
            asked.append(lines)
            return made

        # A generator that ignores the lines it is given still has its mutants on unchanged lines 1 and 4 dropped.
        found = module_mutants("tools/calc.py", source, frozenset({2, 3}), generate)
        self.assertEqual([frozenset({2, 3})], asked)
        self.assertEqual([(2, "b = 2", "b = 9")], [(item.line, item.before, item.after) for item in found])

    def test_a_module_with_no_changed_line_makes_no_mutant_and_asks_the_generator_for_none(self) -> None:
        def refuse(module: str, text: str, lines: frozenset[int]) -> list[str]:
            raise AssertionError("no mutant is made of a module with no changed line")

        self.assertEqual([], module_mutants("tools/calc.py", "a = 1\n", frozenset(), refuse))

    def test_mutmut_is_asked_for_the_changed_lines_alone(self) -> None:
        create = mock.Mock(return_value=(None, [], set(), set()))
        fake = types.SimpleNamespace(create_mutations=create)
        with mock.patch.object(importlib, "import_module", return_value=fake) as found:
            self.assertEqual([], mutation_gate.mutmut_mutants("tools/calc.py", "a = 1\n", frozenset({6})))
        found.assert_called_once_with("mutmut.mutation.file_mutation")
        create.assert_called_once_with("tools/calc.py", "a = 1\n", covered_lines={6})

    def test_a_generator_that_no_longer_makes_the_canary_mutant_or_ignores_the_lines_is_refused(self) -> None:
        self.assertIsNone(generator_problem(lambda module, text, lines: [text.replace("+", "-")] if 2 in lines else []))
        problem = generator_problem(lambda module, text, lines: [text.replace("1", "2")])
        self.assertIn("no longer turns", problem or "")
        problem = generator_problem(lambda module, text, lines: [text.replace("+", "-")])
        self.assertIn("no longer keeps to the changed lines", problem or "")

    def test_a_code_span_survives_backticks_in_the_source(self) -> None:
        self.assertEqual("`a + 1`", code("a + 1"))
        self.assertEqual("``print(`x`)``", code("print(`x`)"))
        self.assertEqual("`` `x` ``", code("`x`"))
        self.assertEqual("`` `x ``", code("`x"))
        self.assertEqual("`` x` ``", code("x`"))


class ReachFixtures(unittest.TestCase):
    def test_coverage_measures_the_suites_subprocesses_and_only_the_changed_modules(self) -> None:
        settings = coverage_settings(Path("data folder"), [Path("tree/tools/calc.py"), Path("tree/deployer/a.py")])
        # `patch = subprocess` would hand each child the parent's configuration, its context already expanded.
        self.assertNotIn("patch", settings)
        self.assertIn("\ncontext = ${PYTEST_CURRENT_TEST-}\n", settings)
        self.assertIn("\ndynamic_context = test_function\n", settings)
        self.assertIn("parallel = true", settings)
        self.assertIn(f"    {Path('tree/tools/calc.py')}\n    {Path('tree/deployer/a.py')}\n", settings)
        self.assertIn("disable_warnings = no-data-collected,", settings)

    def test_executed_lines_are_read_by_module_whatever_path_names_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary) / "tree with spaces"
            (tree / "tools").mkdir(parents=True)
            (tree / "deployer").mkdir()
            (tree / "tools" / "calc.py").write_text("a\n" * 6, encoding="utf-8")
            (tree / "deployer" / "a.py").write_text("a\n" * 3, encoding="utf-8")
            (tree / "tools" / "unchanged.py").write_text("a\n", encoding="utf-8")
            report = {
                "files": {
                    str(tree / "tools" / "calc.py"): {"executed_lines": [1, 2, 5], "missing_lines": [3, 6]},
                    "deployer/a.py": {"executed_lines": [3], "missing_lines": [1]},
                    "tools/unchanged.py": {"executed_lines": [1]},
                    "tools/unrun.py": ["not", "an", "entry"],
                    str(Path(temporary) / "elsewhere.py"): {"executed_lines": [9]},
                }
            }
            lines = executed_lines(report, tree, ["tools/calc.py", "deployer/a.py", "tools/unrun.py"])
        self.assertEqual(
            {"tools/calc.py": {1, 2, 5}, "deployer/a.py": {3}, "tools/unrun.py": set()},
            {module: set(found) for module, found in lines.items()},
        )

    def test_a_continuation_line_is_reached_when_coverage_recorded_its_statement_by_its_first_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            (tree / "tools").mkdir()
            (tree / "tools" / "calc.py").write_text(MULTILINE_CALC, encoding="utf-8")
            lines = executed_lines(MULTILINE_REPORT, tree, ["tools/calc.py"])["tools/calc.py"]
        # scaled()'s call opens on line 6 and runs to line 9; unused()'s opens on line 13 and never ran.
        self.assertEqual({1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12}, set(lines))

    def test_an_excluded_statement_ends_the_one_before_it_and_the_last_statement_runs_to_the_modules_end(self) -> None:
        entry = {"executed_lines": [1, 4], "missing_lines": [], "excluded_lines": [2]}
        self.assertEqual({1, 4, 5, 6}, set(mutation_gate.statement_lines(entry, 6)))
        self.assertEqual(frozenset(), mutation_gate.statement_lines({"executed_lines": []}, 6))


class MeasurementCanaryFixtures(unittest.TestCase):
    """The canary suite every reach pass measures first, with a reach runner standing in for coverage.py."""

    CANARY_TEST = frozenset({"test_canary.py::test_canary"})

    def canary(self, tests: dict[int, frozenset[str]] | str | None) -> str | None:
        """The canary's verdict on a reach runner that runs the canary suite with the real pytest, unmeasured, and
        reports `tests` as the lines' tests, `tests` itself as why it has no reach, or, for None, a reach holding no
        entry for the canary module."""
        asked: list[tuple[str, list[str], float]] = []

        def reach(tree: Path, suite: str, modules: Sequence[str], timeout: float) -> Reach | str:
            asked.append((suite, list(modules), timeout))
            done = subprocess.run(
                [sys.executable, "-B", "-m", "pytest", suite, "-q", "-p", "no:cacheprovider"],
                cwd=tree,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(0, done.returncode, done.stdout + done.stderr)
            if isinstance(tests, str):
                return tests
            if tests is None:
                return Reach(1.0, {}, {})
            return Reach(1.0, {"canary.py": frozenset(tests)}, {"canary.py": tests})

        problem = mutation_gate.measurement_problem(reach)
        self.assertEqual([("test_canary.py", ["canary.py"], mutation_gate.MINIMUM_SUITE_SECONDS)], asked)
        return problem

    def test_a_reach_that_names_the_suites_and_its_childs_lines_with_the_test_passes(self) -> None:
        self.assertIsNone(self.canary({5: self.CANARY_TEST, 9: self.CANARY_TEST}))

    def test_a_reach_that_measured_nothing_or_no_process_or_the_wrong_test_fails(self) -> None:
        other = frozenset({"test_canary.py::test_other"})
        own = "the reach run no longer names the line the suite's own process ran with the canary suite's test"
        child = "the reach run no longer names the line a process its test started ran with the canary suite's test"
        cases: list[tuple[dict[int, frozenset[str]] | str | None, str]] = [
            # No .pth file starts measuring, so `coverage combine` has no data.
            (
                "failed in `coverage combine` with exit code 1: `No data to combine`",
                "the reach run's canary suite failed in `coverage combine` with exit code 1: `No data to combine`, so "
                "no suite's reach can be measured",
            ),
            (None, own),
            ({}, own),
            # `patch = subprocess`, or a child started with its context already expanded, names no test.
            ({5: self.CANARY_TEST}, child),
            ({9: self.CANARY_TEST}, own),
            ({5: self.CANARY_TEST, 9: other}, child),
        ]
        for tests, expected in cases:
            with self.subTest(tests=tests):
                self.assertEqual(expected, self.canary(tests))

    def test_the_canary_texts_are_ascii_so_their_bytes_hold_in_every_locale(self) -> None:
        self.assertTrue(mutation_gate.MEASUREMENT_SOURCE.isascii())
        self.assertTrue(mutation_gate.MEASUREMENT_TESTS.isascii())


class ContextFixtures(unittest.TestCase):
    """The tests a mutant's line runs, read from coverage.py's recorded contexts and pytest's listing of the suite."""

    def test_only_a_statement_its_own_test_functions_alone_ran_names_tests_and_every_other_runs_the_whole_suite(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary) / "tree with spaces"
            context_tree(tree)
            tests = executed_tests(CONTEXT_REPORT, tree, ["tools/calc.py"], CONTEXT_SUITE_PATH, CONTEXT_COLLECTED)
            lines = executed_lines(CONTEXT_REPORT, tree, ["tools/calc.py"])["tools/calc.py"]
        self.assertEqual(CONTEXT_TESTS, tests)
        # add() also ran in setUp(), negate() in an inherited test, and the module's import lines in the suite's own
        # process too: each line is reached, and runs the whole suite.
        for line in (1, 5, 16, 19):
            with self.subTest(line=line):
                self.assertIn(line, lines)
                self.assertNotIn(line, tests["tools/calc.py"])

    def test_a_process_a_tests_call_started_names_that_test_and_any_other_phase_runs_the_whole_suite(self) -> None:
        suite = "tests/tools/test_calc.py"
        collected = [f"{suite}::CalcTests::test_double", f"{suite}::test_add[a b]", f"{suite}::test_add[c]"]
        self.assertEqual(frozenset({collected[1]}), context_tests([f"{suite}::test_add[a b] (call)"], suite, collected))
        # A process started by a test's call and the test's own function name it together.
        self.assertEqual(
            frozenset(collected),
            context_tests(
                [f"{suite}::CalcTests::test_double (call)", "test_calc.CalcTests.test_double", "test_calc.test_add"],
                suite,
                collected,
            ),
        )
        for contexts in (
            # A set-up or a tear-down may be a fixture's that other tests share.
            [f"{suite}::CalcTests::test_double (setup)"],
            [f"{suite}::CalcTests::test_double (teardown)"],
            [f"{suite}::CalcTests::test_double (call)", ""],
            # A node id of no collected test, or a call phase with what a nested pytest's test function added.
            [f"{suite}::test_gone (call)"],
            ["tests/tools/test_other.py::test_add[c] (call)"],
            [f"{suite}::test_add[c] (call)|test_inner.test_x"],
            [f"{suite}::test_add[c]"],
        ):
            with self.subTest(contexts=contexts):
                self.assertIsNone(context_tests(contexts, suite, collected))

    def test_a_context_names_a_test_by_its_modules_import_name_and_every_parametrized_case_runs(self) -> None:
        suite = "tests/tools/test_calc.py"
        collected = [f"{suite}::test_add[1]", f"{suite}::test_add[2]", f"{suite}::CalcTests::test_double"]
        self.assertEqual(
            frozenset(collected[:2]), context_tests(["test_calc.test_add"], suite, collected), "the pytest module name"
        )
        self.assertEqual(
            frozenset(collected),
            context_tests(["tests.tools.test_calc.test_add", "test_calc.CalcTests.test_double"], suite, collected),
        )

    def test_a_class_named_as_its_module_and_a_parameter_holding_brackets_still_name_their_tests(self) -> None:
        suite = "tests/tools/test_calc.py"
        nested = f"{suite}::test_calc::test_x"
        bracketed = [f"{suite}::test_add[a[1]]", f"{suite}::test_add[b]"]
        self.assertEqual(frozenset({nested}), context_tests(["test_calc.test_calc.test_x"], suite, [nested]))
        self.assertEqual(frozenset(bracketed), context_tests(["test_calc.test_add"], suite, bracketed))

    def test_a_report_without_contexts_or_with_none_for_a_statement_names_no_test(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            context_tree(tree)
            entry = CONTEXT_REPORT["files"]["tools/calc.py"]
            plain = {key: value for key, value in entry.items() if key != "contexts"}
            for report in (
                {"files": {"tools/calc.py": plain}},
                {"files": {"tools/calc.py": {**plain, "contexts": {}}}},
            ):
                with self.subTest(report=report):
                    tests = executed_tests(report, tree, ["tools/calc.py"], CONTEXT_SUITE_PATH, CONTEXT_COLLECTED)
                    self.assertEqual({"tools/calc.py": {}}, tests)

    def test_a_context_that_names_no_collected_test_of_the_suite_alone_runs_the_whole_suite(self) -> None:
        suite = "tests/tools/test_calc.py"
        collected = [f"{suite}::CalcTests::test_double", f"{suite}::test_add"]
        for contexts in (
            [],
            [""],
            ["test_calc.test_add", ""],
            ["test_other.CalcTests.test_double"],
            ["not_test_calc.test_add"],
            ["test_calc.Helper.test_double"],
            ["test_calc.test_gone"],
        ):
            with self.subTest(contexts=contexts):
                self.assertIsNone(context_tests(contexts, suite, collected))

    def test_a_suite_pytest_cannot_list_names_no_test_and_its_listing_keeps_only_its_node_ids(self) -> None:
        self.assertIsNone(context_tests(["test_calc.test_add"], "tests/tools/test_calc.py", []))
        listing = "tests/a.py::T::test_x\ntests/a.py::test_y[1]\n\n2 tests collected in 0.01s\n"
        self.assertEqual(
            ["tests/a.py::T::test_x", "tests/a.py::test_y[1]"], mutation_gate.collected_tests(listing, "tests/a.py")
        )


class ProcessArgumentFixtures(unittest.TestCase):
    """What each process the gate starts for a suite reaches the bounded runner with."""

    def setUp(self) -> None:
        self.calls: list[tuple[list[str], float, dict[str, object], list[str]]] = []
        self.tree_sentinel = object()
        patcher = mock.patch("deployer.platform_support.process_tree", return_value=self.tree_sentinel)
        patcher.start()
        self.addCleanup(patcher.stop)

    def bounded(self, listing: bytes = b"", returncode: int = 0) -> Any:
        def run(command: Sequence[str], timeout: float, **options: object) -> types.SimpleNamespace:
            listed = [part for part in command if part.startswith("@")]
            read = Path(listed[0][1:]).read_text(encoding="utf-8").splitlines() if listed else []
            self.calls.append((list(command), timeout, options, read))
            output = options.get("stdout")
            if output is not None:
                output.write(listing)  # type: ignore[attr-defined]  # the gate passes a binary file
            return types.SimpleNamespace(returncode=returncode, stdout=b"", stderr=b"")

        return mock.patch.object(mutation_gate, "run_bounded", side_effect=run)

    def assert_suite_options(self, options: dict[str, object]) -> None:
        self.assertEqual(mutation_gate.suite_environment(), options["env"])
        self.assertEqual(mutation_gate.OUTPUT_LIMIT, options["output_limit"])
        self.assertIs(self.tree_sentinel, options["tree"])
        self.assertEqual(Path("tree"), options["cwd"])

    def test_a_suite_run_stops_at_its_first_failure_within_its_time_and_names_every_selected_test(self) -> None:
        tests = ["tests/x.py::A::test_a", "tests/x.py::test_b[1]"]
        with self.bounded():
            self.assertEqual(PASSED, run_suite(Path("tree"), "tests/x.py", 42.0))
            self.assertEqual(PASSED, run_suite(Path("tree"), "tests/x.py", 42.0, tests))
        (whole, timeout, options, read), (selected, _, selected_options, listed) = self.calls
        self.assertEqual([sys.executable, "-B", "-m", "pytest", "tests/x.py", "-x", "-q"], whole)
        self.assertEqual((42.0, []), (timeout, read))
        self.assert_suite_options(options)
        self.assertEqual([sys.executable, "-B", "-m", "pytest", selected[4], "-x", "-q"], selected)
        self.assertTrue(selected[4].startswith("@"), selected)
        self.assertEqual(tests, listed)
        self.assert_suite_options(selected_options)

    def test_a_suites_listing_is_read_whole_from_its_file_and_a_byte_it_cannot_decode_is_replaced(self) -> None:
        listing = b"tests/x.py::test_a\n\xff\ntests/x.py::test_b\n\n2 tests collected\n"
        with tempfile.TemporaryDirectory() as temporary, self.bounded(listing):
            found = mutation_gate.suite_tests(Path("tree"), "tests/x.py", Path(temporary) / "listing.txt")
        self.assertEqual(["tests/x.py::test_a", "tests/x.py::test_b"], found)
        ((command, timeout, options, _),) = self.calls
        self.assertEqual([sys.executable, "-B", "-m", "pytest", "tests/x.py", "--collect-only", "-q"], command)
        self.assertEqual(mutation_gate.MINIMUM_SUITE_SECONDS, timeout)
        self.assert_suite_options(options)
        self.assertIn("stdout", options)

    def test_a_suite_pytest_fails_to_list_names_no_test(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, self.bounded(b"tests/x.py::test_a\n", returncode=4):
            self.assertEqual([], mutation_gate.suite_tests(Path("tree"), "tests/x.py", Path(temporary) / "listing"))

    def test_a_reach_runs_pytest_measured_from_its_file_and_its_coverage_steps_unmeasured(self) -> None:
        # pytest runs this test with PYTEST_CURRENT_TEST set, which the suite's own process must not inherit.
        self.assertIn("PYTEST_CURRENT_TEST", os.environ)
        settings: list[str] = []
        environments: list[dict[str, str]] = []

        def run(command: Sequence[str], timeout: float, **options: Any) -> types.SimpleNamespace:
            self.calls.append((list(command), timeout, options, []))
            environments.append(options["env"])
            start = options["env"].get("COVERAGE_PROCESS_START")
            if start:
                settings.append(Path(start).read_text(encoding="utf-8"))
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with mock.patch.object(mutation_gate, "run_bounded", side_effect=run):
            result = mutation_gate.coverage_reach(Path("tree"), "tests/x.py", ["tools/calc.py"], 42.0)
        self.assertEqual("failed in `coverage json`, which wrote no report", result)
        (suite, timeout, options, _), *steps = self.calls
        self.assertEqual(([sys.executable, "-B", "-m", "pytest", "tests/x.py", "-q"], 42.0), (suite, timeout))
        environment = environments[0]
        self.assertNotIn("PYTEST_CURRENT_TEST", environment)
        self.assertEqual("1", environment["PYTHONDONTWRITEBYTECODE"])
        self.assertEqual("coveragerc", Path(environment["COVERAGE_PROCESS_START"]).name)
        self.assertEqual(
            [coverage_settings(Path(environment["COVERAGE_PROCESS_START"]).parent, [Path("tree/tools/calc.py")])],
            settings,
        )
        self.assertIs(self.tree_sentinel, options["tree"])
        self.assertEqual(["combine", "json"], [command[4] for command, _, _, _ in steps])
        for _, _, step_options, _ in steps:
            self.assertEqual(mutation_gate.suite_environment(), step_options["env"])


# What each of pytest's exits reads as, with and without a collection error in its output.
EXIT_STATUSES = {
    pytest.ExitCode.OK: (PASSED, PASSED),
    pytest.ExitCode.TESTS_FAILED: (KILLED, KILLED),
    pytest.ExitCode.INTERRUPTED: (ERRORED, KILLED),
    pytest.ExitCode.INTERNAL_ERROR: (ERRORED, ERRORED),
    pytest.ExitCode.USAGE_ERROR: (ERRORED, ERRORED),
    pytest.ExitCode.NO_TESTS_COLLECTED: (ERRORED, ERRORED),
    pytest.ExitCode.MAX_WARNINGS_ERROR: (ERRORED, ERRORED),
}


def test_every_pytest_exit_code_has_a_reading() -> None:
    """A member pytest adds later fails here until its reading is decided."""
    assert set(EXIT_STATUSES) == set(pytest.ExitCode)


@pytest.mark.parametrize(("code", "statuses"), [*EXIT_STATUSES.items(), (9, (ERRORED, ERRORED))])
def test_only_a_test_failure_or_a_failed_collection_kills_a_mutant(code: int, statuses: tuple[str, str]) -> None:
    collection = b"==== ERRORS ====\n____ ERROR collecting tests/x.py ____\nImportError\n"
    found = []
    for output in (b"!!! KeyboardInterrupt !!!\n", collection):
        finished = types.SimpleNamespace(returncode=int(code), stdout=output, stderr=b"")
        with (
            mock.patch.object(mutation_gate, "run_bounded", return_value=finished),
            mock.patch("deployer.platform_support.process_tree"),
        ):
            found.append(run_suite(Path("tree"), "tests/x.py", 42.0))
    assert tuple(found) == statuses


class RunSuiteFixtures(unittest.TestCase):
    """The real suite runner, over a suite with a test that fails and one that passes."""

    def test_named_tests_run_alone_and_without_names_the_whole_suite_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary) / "tree with spaces"
            (tree / "tests").mkdir(parents=True)
            (tree / "tests" / "test_x.py").write_text(
                "def test_passes():\n    pass\n\n\ndef test_too():\n    pass\n\n\n"
                "def test_fails():\n    raise AssertionError\n",
                encoding="utf-8",
            )
            alone = run_suite(tree, "tests/test_x.py", 60.0, ["tests/test_x.py::test_passes"])
            both = run_suite(
                tree, "tests/test_x.py", 60.0, ["tests/test_x.py::test_passes", "tests/test_x.py::test_too"]
            )
            whole = run_suite(tree, "tests/test_x.py", 60.0)
            failing = run_suite(tree, "tests/test_x.py", 60.0, ["tests/test_x.py::test_fails"])
        self.assertEqual((PASSED, PASSED, KILLED, KILLED), (alone, both, whole, failing))

    def run_tree(self, files: dict[str, str], tests: Sequence[str] = ()) -> str:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary) / "tree with spaces"
            for name, text in files.items():
                (tree / name).parent.mkdir(parents=True, exist_ok=True)
                (tree / name).write_text(text, encoding="utf-8")
            return run_suite(tree, "tests/test_x.py", 60.0, tests)

    def test_a_test_module_pytest_fails_to_collect_kills_the_mutant(self) -> None:
        # pytest exits 2, interrupted, having failed to collect the module, as a mutant that breaks an import does.
        self.assertEqual(KILLED, self.run_tree({"tests/test_x.py": "import not_a_module_anywhere\n"}))
        self.assertEqual(KILLED, self.run_tree({"tests/test_x.py": "def test_x(:\n    pass\n"}))

    def test_an_exit_that_is_neither_a_pass_nor_a_test_failure_errors_and_judges_nothing(self) -> None:
        cases = {
            # Exit 2 with every module collected: a test interrupted the run.
            "interrupted": ({"tests/test_x.py": "def test_x():\n    raise KeyboardInterrupt\n"}, ()),
            # Exit 3: a hook raised, an internal error.
            "internal error": (
                {
                    "conftest.py": "def pytest_collection_finish(session):\n    raise RuntimeError('hook')\n",
                    "tests/test_x.py": "def test_x():\n    pass\n",
                },
                (),
            ),
            # Exit 4: a node id pytest cannot resolve, which only a mutant's subset names.
            "usage error": (
                {"tests/test_x.py": "def test_x():\n    pass\n"},
                ("tests/test_x.py::test_gone",),
            ),
            # Exit 5: no test collected.
            "no tests collected": ({"tests/test_x.py": "X = 1\n"}, ()),
        }
        for name, (files, tests) in cases.items():
            with self.subTest(name):
                self.assertEqual(ERRORED, self.run_tree(files, tests))


class CoverageReachFixtures(unittest.TestCase):
    """The reach of a suite run by the real pytest, its coverage data combined and reported by a stub coverage.py in
    the tree, which `python -m coverage` finds first there."""

    # A process that inherits coverage.py's subprocess setting starts the real coverage.py from its .pth file before
    # `-m coverage` is resolved, which then runs instead of this stub, so the stub refuses to run measured.
    STUB = (
        "import json, os, pathlib, sys, time\n"
        "if any(name.startswith('COVERAGE_PROCESS_') for name in os.environ):\n"
        "    print('started measured', file=sys.stderr)\n"
        "    sys.exit(9)\n"
        "step = sys.argv[1]\n"
        "action = json.loads(pathlib.Path('stub-actions.json').read_text()).get(step, 'ok')\n"
        "if action == 'sleep':\n"
        "    time.sleep(60)\n"
        "if action.startswith('fail '):\n"
        "    _, code, message = action.split(' ', 2)\n"
        "    print(message, file=sys.stderr)\n"
        "    sys.exit(int(code))\n"
        "rest = sys.argv[3:]\n"
        "expected = {'combine': rest == ['-q'],\n"
        "            'json': rest[:3] == ['-q', '--show-contexts', '-o'] and len(rest) == 4}\n"
        "if not sys.argv[2].startswith('--rcfile=') or not expected.get(step):\n"
        "    print('unexpected arguments', sys.argv[1:], file=sys.stderr)\n"
        "    sys.exit(7)\n"
        "if step == 'json' and action == 'ok':\n"
        "    report = pathlib.Path('stub-report.json')\n"
        "    text = report.read_text() if report.is_file() else json.dumps({'files': {}})\n"
        "    pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text(text)\n"
    )

    def reach(
        self,
        actions: dict[str, str],
        recorded: bool = False,
        suite: str = "tests/test_calc.py",
        report: object = None,
        passes: bool = True,
    ) -> Reach | str:
        """The reach of `suite` in a tree whose stub coverage.py reports nothing, or, when `recorded`, CONTEXT_REPORT
        for the CONTEXT_SUITE the tree holds, or else `report` when one is given. Unless `recorded`, the suite is one
        test, which passes or fails as `passes` says."""
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary) / "tree with spaces"
            (tree / "tests").mkdir(parents=True)
            (tree / "tests" / "test_calc.py").write_text(f"def test_it():\n    assert {passes}\n", encoding="utf-8")
            (tree / "coverage.py").write_text(self.STUB, encoding="utf-8")
            (tree / "stub-actions.json").write_text(json.dumps(actions), encoding="utf-8")
            if recorded:
                context_tree(tree)
                (tree / "stub-report.json").write_text(json.dumps(CONTEXT_REPORT), encoding="utf-8")
            elif report is not None:
                (tree / "stub-report.json").write_text(json.dumps(report), encoding="utf-8")
            with mock.patch.object(mutation_gate, "MINIMUM_SUITE_SECONDS", 30.0 if recorded else 2.0):
                return mutation_gate.coverage_reach(tree, suite, ["tools/calc.py"], 60.0)

    def test_a_suite_whose_coverage_data_is_combined_and_reported_is_measured(self) -> None:
        result = self.reach({})
        if not isinstance(result, Reach):
            self.fail(f"the reach was not measured: {result}")
        self.assertEqual({"tools/calc.py": frozenset()}, dict(result.lines))
        self.assertIsInstance(result.seconds, float)
        self.assertGreater(result.seconds, 0.0)

    def test_a_failed_or_timed_out_coverage_step_names_the_step_and_its_output(self) -> None:
        for actions, expected in (
            (
                {"combine": "fail 3 Can't combine: the data file is damaged"},
                "failed in `coverage combine` with exit code 3: `Can't combine: the data file is damaged`",
            ),
            ({"combine": "sleep"}, "timed out in `coverage combine`"),
            ({"json": "sleep"}, "timed out in `coverage json`"),
            ({"json": "write nothing"}, "failed in `coverage json`, which wrote no report"),
        ):
            with self.subTest(actions=actions):
                self.assertEqual(expected, self.reach(actions))

    def test_a_suite_that_ran_no_line_of_the_modules_is_measured_and_reached_nothing(self) -> None:
        # coverage.py 7.16.2 prints this on stdout and exits 1 when nothing in the include list ran.
        result = self.reach({"json": "fail 1 No data to report."})
        if not isinstance(result, Reach):
            self.fail(f"the reach was not measured: {result}")
        self.assertEqual({"tools/calc.py": frozenset()}, dict(result.lines))
        self.assertEqual({}, dict(result.tests))
        self.assertGreater(result.seconds, 0.0)

    def test_no_data_is_read_as_reaching_nothing_only_from_coverage_json_exiting_1_with_that_message_alone(
        self,
    ) -> None:
        for actions, expected in (
            # No measured process started, so the measurement failed rather than the suite reaching nothing.
            (
                {"combine": "fail 1 No data to combine"},
                "failed in `coverage combine` with exit code 1: `No data to combine`",
            ),
            (
                {"combine": "fail 1 No data to report."},
                "failed in `coverage combine` with exit code 1: `No data to report.`",
            ),
            ({"json": "fail 2 No data to report."}, "failed in `coverage json` with exit code 2: `No data to report.`"),
            (
                {"json": "fail 1 Couldn't read data: No data to report."},
                "failed in `coverage json` with exit code 1: `Couldn't read data: No data to report.`",
            ),
            (
                {"json": "fail 1 No data to report. Traceback follows"},
                "failed in `coverage json` with exit code 1: `No data to report. Traceback follows`",
            ),
            ({"json": "fail 1 No data to report"}, "failed in `coverage json` with exit code 1: `No data to report`"),
        ):
            with self.subTest(actions=actions):
                self.assertEqual(expected, self.reach(actions))

    def test_a_report_with_no_files_object_is_a_failed_step_not_a_suite_that_reached_nothing(self) -> None:
        reports: list[object] = [{}, {"meta": {"version": "7"}}, {"files": []}, []]
        for report in reports:
            with self.subTest(report=report):
                self.assertEqual(
                    "failed in `coverage json`, whose report holds no files object", self.reach({}, report=report)
                )

    def test_the_report_names_each_line_the_tests_that_ran_it_from_pytests_listing_of_the_suite(self) -> None:
        result = self.reach({}, recorded=True, suite=CONTEXT_SUITE_PATH)
        if not isinstance(result, Reach):
            self.fail(f"the reach was not measured: {result}")
        self.assertEqual(CONTEXT_TESTS, {module: dict(lines) for module, lines in result.tests.items()})
        self.assertIn(20, result.lines["tools/calc.py"])

    def test_a_suite_pytest_cannot_list_is_measured_and_runs_whole_against_every_mutant(self) -> None:
        # The stub reports CONTEXT_REPORT, but pytest lists no test of the suite.
        with mock.patch.object(mutation_gate, "suite_tests", return_value=[]):
            result = self.reach({}, recorded=True, suite=CONTEXT_SUITE_PATH)
        if not isinstance(result, Reach):
            self.fail(f"the reach was not measured: {result}")
        self.assertEqual({}, dict(result.tests))
        self.assertIn(9, result.lines["tools/calc.py"])

    def test_a_suite_failed_unmutated_is_still_reported_by_its_exit_code(self) -> None:
        self.assertEqual("failed unmutated with exit code 1", self.reach({}, passes=False))
        self.assertEqual("failed unmutated with exit code 4", self.reach({}, suite="tests/test_gone.py"))

    def test_a_gate_measured_by_coverage_starts_every_coverage_step_unmeasured(self) -> None:
        # The gate's own suite runs under the reach pass, which leaves this variable set, or, from coverage.py's
        # `patch = subprocess`, this one.
        with mock.patch.dict(os.environ, {"COVERAGE_PROCESS_START": "rc", "COVERAGE_PROCESS_CONFIG": "{}"}):
            result = self.reach({})
        self.assertIsInstance(result, Reach, result)


class TryMutantFixtures(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tree = Path(temporary.name) / "tree with spaces"
        (self.tree / "tools").mkdir(parents=True)
        self.original = b"def add(a, b):\r\n    return a + b\r\n"
        (self.tree / "tools" / "calc.py").write_bytes(self.original)
        self.mutant = Mutant("tools/calc.py", 2, "return a + b", "return a - b", "def add(a, b):\n    return a - b\n")
        self.reach = {
            "tests/slow.py": Reach(30.0, {"tools/calc.py": frozenset({1, 2})}),
            "tests/fast.py": Reach(2.0, {"tools/calc.py": frozenset({2})}),
            "tests/other.py": Reach(1.0, {"tools/calc.py": frozenset({1})}),
        }
        self.calls: list[tuple[str, float, str]] = []
        self.tests: list[tuple[str, ...]] = []

    def runner(self, results: dict[str, str]) -> SuiteRunner:
        def run(tree: Path, suite: str, timeout: float, tests: Sequence[str]) -> str:
            self.calls.append((suite, timeout, (tree / "tools" / "calc.py").read_text(encoding="utf-8")))
            self.tests.append(tuple(tests))
            return results.get(suite, PASSED)

        return run

    def test_the_first_failing_suite_kills_the_mutant_fastest_first_and_the_module_is_put_back(self) -> None:
        outcome = try_mutant(
            self.tree, self.mutant, self.reach, 100.0, self.runner({"tests/slow.py": KILLED}), lambda: 0.0
        )
        self.assertEqual(Outcome(self.mutant, KILLED, ("tests/fast.py", "tests/slow.py"), (0, 0)), outcome)
        # Each suite ran against the mutant, with three times its unmutated seconds and never under the floor.
        self.assertEqual(
            [("tests/fast.py", 60.0, self.mutant.source), ("tests/slow.py", 90.0, self.mutant.source)], self.calls
        )
        self.assertEqual(self.original, (self.tree / "tools" / "calc.py").read_bytes())

    def test_a_mutant_every_reaching_suite_passes_survived_and_a_timeout_counts_as_killed(self) -> None:
        survived = try_mutant(self.tree, self.mutant, self.reach, 100.0, self.runner({}), lambda: 0.0)
        self.assertEqual((SURVIVED, ("tests/fast.py", "tests/slow.py")), (survived.status, survived.suites))
        timed = try_mutant(
            self.tree, self.mutant, self.reach, 100.0, self.runner({"tests/fast.py": TIMED_OUT}), lambda: 0
        )
        self.assertEqual((TIMED_OUT, ("tests/fast.py",)), (timed.status, timed.suites))
        self.assertEqual(self.original, (self.tree / "tools" / "calc.py").read_bytes())

    def test_a_suite_that_errored_judges_nothing_so_the_next_suite_runs_and_the_mutant_errors_unless_it_kills(
        self,
    ) -> None:
        killed = try_mutant(
            self.tree,
            self.mutant,
            self.reach,
            100.0,
            self.runner({"tests/fast.py": ERRORED, "tests/slow.py": KILLED}),
            lambda: 0.0,
        )
        self.assertEqual(Outcome(self.mutant, KILLED, ("tests/fast.py", "tests/slow.py"), (0, 0)), killed)
        errored = try_mutant(
            self.tree, self.mutant, self.reach, 100.0, self.runner({"tests/fast.py": ERRORED}), lambda: 0.0
        )
        self.assertEqual(Outcome(self.mutant, ERRORED, ("tests/fast.py", "tests/slow.py"), (0, 0)), errored)
        last = try_mutant(
            self.tree, self.mutant, self.reach, 100.0, self.runner({"tests/slow.py": ERRORED}), lambda: 0.0
        )
        self.assertEqual(ERRORED, last.status)
        self.assertEqual(self.original, (self.tree / "tools" / "calc.py").read_bytes())

    def test_a_suite_runs_only_the_tests_that_ran_the_line_when_it_names_them_and_whole_otherwise(self) -> None:
        reach = {
            "tests/fast.py": Reach(
                2.0, {"tools/calc.py": frozenset({2})}, {"tools/calc.py": {2: frozenset({"b", "a"})}}
            ),
            "tests/slow.py": Reach(
                30.0, {"tools/calc.py": frozenset({1, 2})}, {"tools/calc.py": {1: frozenset({"c"})}}
            ),
        }
        outcome = try_mutant(self.tree, self.mutant, reach, 100.0, self.runner({}), lambda: 0.0)
        self.assertEqual(Outcome(self.mutant, SURVIVED, ("tests/fast.py", "tests/slow.py"), (2, 0)), outcome)
        # tests/slow.py names tests only for line 1, so line 2's mutant runs the whole of it.
        self.assertEqual([("a", "b"), ()], self.tests)

    def test_a_table_mutant_runs_only_the_tests_that_read_the_table_and_skips_a_suite_that_only_imported_it(
        self,
    ) -> None:
        table = Mutant("tools/calc.py", 1, "x", "y", "TABLE = 9\n", True, frozenset({2}))
        reach = {
            "tests/imports.py": Reach(1.0, {"tools/calc.py": frozenset({1})}),
            "tests/reads.py": Reach(
                30.0, {"tools/calc.py": frozenset({1, 2})}, {"tools/calc.py": {2: frozenset({"r"})}}
            ),
        }
        outcome = try_mutant(self.tree, table, reach, 100.0, self.runner({}), lambda: 0.0)
        self.assertEqual(Outcome(table, SURVIVED, ("tests/reads.py",), (1,)), outcome)
        self.assertEqual([("r",)], self.tests)

    def test_a_mutant_no_suite_executed_is_not_reached_and_runs_nothing(self) -> None:
        unreached = Mutant("tools/calc.py", 7, "x", "y", "z")
        self.assertEqual(
            Outcome(unreached, NOT_REACHED),
            try_mutant(self.tree, unreached, self.reach, 100.0, self.runner({}), lambda: 0.0),
        )
        self.assertEqual([], self.calls)

    def test_the_log_names_each_mutant_a_suite_ran_against_and_each_no_suite_reached(self) -> None:
        unreached = Mutant("tools/calc.py", 7, "x = 1", "x = 2", "z")
        left = Mutant("tools/calc.py", 9, "y = 1", "y = 2", "w")
        output = FlushRecorder()
        with contextlib.redirect_stdout(output):
            outcomes = try_mutants(
                Workers([self.tree], lambda: 0.0),
                [self.mutant, unreached],
                self.reach,
                100.0,
                self.runner({"tests/fast.py": KILLED}),
            )
            try_mutants(Workers([self.tree], lambda: 200.0), [left], self.reach, 100.0, self.runner({}))
        self.assertEqual([KILLED, NOT_REACHED], [outcome.status for outcome in outcomes])
        # A mutant the deadline left untested ran against nothing and reached nothing to log.
        killed = "killed      tools/calc.py:2 return a + b -> return a - b\n"
        unreached_line = "not reached tools/calc.py:7 x = 1 -> x = 2\n"
        self.assertEqual(killed + unreached_line, output.getvalue())
        # Each line reaches the live log as its mutant finishes, not when the run ends.
        self.assertEqual([killed, killed + unreached_line], output.flushed)

    def test_the_deadline_leaves_a_mutant_not_run_and_the_module_put_back_even_when_a_suite_raises(self) -> None:
        times = iter([0.0, 200.0])
        outcome = try_mutant(self.tree, self.mutant, self.reach, 100.0, self.runner({}), lambda: next(times))
        self.assertEqual(Outcome(self.mutant, NOT_RUN, ("tests/fast.py",), (0,)), outcome)

        def broken(tree: Path, suite: str, timeout: float, tests: Sequence[str]) -> str:
            raise OSError("the suite could not start")

        with self.assertRaises(OSError):
            try_mutant(self.tree, self.mutant, self.reach, 100.0, broken, lambda: 0.0)
        self.assertEqual(self.original, (self.tree / "tools" / "calc.py").read_bytes())


TABLE_MODULE = '''"""A table only a function reads."""

import re

TABLE = {"a": 1, "b": 2}
PATTERN = re.compile(r"x{1,3}")
BOUND = (1, 2)


def lookup(key):
    if key in TABLE:
        return TABLE[key]
    return None


def matches(text):
    return PATTERN.fullmatch(text) is not None


def bounded(value, limits=BOUND):
    return limits[0] <= value


def scale(a):
    return a * 2
'''


NESTED_MODULE = """import functools

TABLE: dict[str, int] = {
    "a": 1,
    "b": 2,
}


def outer():
    def inner():
        return 0

    return TABLE["a"] + inner()


def keyed():
    return sorted([0], key=lambda v: TABLE["b"])


@functools.lru_cache(maxsize=3)
def cached():
    return 4
"""


def table_mutants(source: str, lines: frozenset[int], elsewhere: bool = False) -> list[Mutant]:
    return module_mutants("tools/table.py", source, lines, line_generator, lambda name: elsewhere)


class ImportTimeFixtures(unittest.TestCase):
    """A mutant of a module-level assignment: which lines read what it binds, and when it runs every reaching suite
    whole, as it did before."""

    def test_a_table_only_functions_read_names_its_reading_lines_and_a_function_line_runs_no_reader_analysis(
        self,
    ) -> None:
        mutants = {mutant.line: mutant for mutant in table_mutants(TABLE_MODULE, frozenset({5, 6, 25}))}
        self.assertEqual((True, frozenset({11, 12})), (mutants[5].at_import, mutants[5].readers))
        self.assertEqual((True, frozenset({17})), (mutants[6].at_import, mutants[6].readers))
        self.assertEqual((False, None), (mutants[25].at_import, mutants[25].readers))

    def test_a_name_read_at_import_or_named_in_another_file_or_by_default_runs_the_whole_suite(self) -> None:
        # BOUND is read by a default, which runs at import.
        self.assertIsNone(table_mutants(TABLE_MODULE, frozenset({7}))[0].readers)
        self.assertIsNone(table_mutants(TABLE_MODULE, frozenset({5}), elsewhere=True)[0].readers)
        default = module_mutants("tools/table.py", TABLE_MODULE, frozenset({5}), line_generator)
        self.assertEqual((True, None), (default[0].at_import, default[0].readers))
        read_at_import = TABLE_MODULE + "\nKEYS = sorted(TABLE)\n"
        self.assertIsNone(table_mutants(read_at_import, frozenset({5}))[0].readers)

    def test_a_name_reached_as_an_attribute_a_global_a_parameter_or_a_string_runs_the_whole_suite(self) -> None:
        for extra in (
            "\n\ndef reflect():\n    return globals()['TABLE']\n",
            "\n\ndef attribute(module):\n    return module.TABLE\n",
            "\n\ndef rebind():\n    global TABLE\n    TABLE = {}\n",
            "\n\ndef shadow(TABLE):\n    return 1\n",
        ):
            with self.subTest(extra=extra):
                self.assertIsNone(table_mutants(TABLE_MODULE + extra, frozenset({5}))[0].readers)
        # The module's docstring naming the table is no reader.
        documented = TABLE_MODULE.replace("A table only", "TABLE is a table only")
        self.assertEqual(frozenset({11, 12}), table_mutants(documented, frozenset({5}))[0].readers)

    def test_an_assignment_that_stores_into_an_object_or_a_statement_that_binds_nothing_runs_the_whole_suite(
        self,
    ) -> None:
        source = "import os\n\nos.environ['X'] = '1'\nprint(2)\nA, B = 3, 4\n\n\ndef f():\n    return A + B\n"
        mutants = {mutant.line: mutant for mutant in table_mutants(source, frozenset({3, 4, 5}))}
        self.assertEqual((True, None), (mutants[3].at_import, mutants[3].readers))
        self.assertEqual((True, None), (mutants[4].at_import, mutants[4].readers))
        self.assertEqual(frozenset({9}), mutants[5].readers)

    def test_a_functions_body_runs_from_its_first_statement_to_the_end_of_its_last_however_many_lines_it_takes(
        self,
    ) -> None:
        source = "def f():\n    a = 1\n    b = 2\n    return (\n        a + b\n    )\n\n\nX = 1\n"
        self.assertEqual(frozenset(range(2, 7)), mutation_gate.function_body_lines(ast.parse(source)))

    def test_a_multi_line_annotated_table_read_after_a_nested_function_and_in_a_lambda_names_its_readers(self) -> None:
        mutants = {mutant.line: mutant for mutant in table_mutants(NESTED_MODULE, frozenset({4, 5, 20, 22}))}
        # Lines 4 and 5 continue the annotated assignment on line 3.
        self.assertEqual((True, frozenset({13, 17})), (mutants[4].at_import, mutants[4].readers))
        self.assertEqual(frozenset({13, 17}), mutants[5].readers)
        # A decorator's line lies between the module's statements, and a function's body runs when it is called.
        self.assertEqual((True, None), (mutants[20].at_import, mutants[20].readers))
        self.assertEqual((False, None), (mutants[22].at_import, mutants[22].readers))

    def test_a_name_read_in_a_decorator_or_a_return_annotation_is_read_at_import(self) -> None:
        for reader in ("@functools.lru_cache(maxsize=len(TABLE))\ndef f():\n", "def f() -> type(TABLE):\n"):
            with self.subTest(reader=reader):
                source = f'import functools\n\nTABLE = {{"a": 1}}\n\n\n{reader}    return 2\n'
                self.assertIsNone(table_mutants(source, frozenset({3}))[0].readers)

    def test_any_of_the_names_an_assignment_binds_named_in_a_string_runs_the_whole_suite(self) -> None:
        source = 'A, B = 1, 2\n\n\ndef f():\n    return globals()["A"] + B\n'
        self.assertIsNone(table_mutants(source, frozenset({1}))[0].readers)

    def test_other_sources_are_every_python_file_but_the_module_in_path_order_each_byte_decoded_or_replaced(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            for name, content in (
                ("tools/m.py", b"NAME\n"),
                ("tools/a.py", b"X\n"),
                ("tests/b.py", "Y é\n".encode()),
                ("tools/c.py", b"\xff\n"),
                ("README.md", b"Z\n"),
            ):
                (tree / name).parent.mkdir(parents=True, exist_ok=True)
                (tree / name).write_bytes(content)
            self.assertEqual("Y é\n\nX\n\n�\n", mutation_gate.other_sources(tree, "tools/m.py"))

    def test_selection_reads_the_other_files_of_the_worktree_for_the_names_a_table_binds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            (tree / "tools").mkdir()
            (tree / "tools" / "table.py").write_text(TABLE_MODULE, encoding="utf-8")
            (tree / "tools" / "other.py").write_text("UNRELATED = 1\n", encoding="utf-8")
            git = mock.create_autospec(GitClient, instance=True)
            git.output.return_value = "@@ -0,0 +5 @@\n"
            gate = Gate(tree, "main", 1, 1, git, line_generator)
            self.assertEqual(frozenset({11, 12}), gate.select(tree, ["tools/table.py"])["tools/table.py"][1][0].readers)
            (tree / "tools" / "other.py").write_text("from table import TABLE\n", encoding="utf-8")
            self.assertIsNone(gate.select(tree, ["tools/table.py"])["tools/table.py"][1][0].readers)

    def test_a_name_is_named_in_a_text_only_as_a_whole_word(self) -> None:
        self.assertTrue(named_in("from table import TABLE\n", "TABLE"))
        self.assertFalse(named_in("TABLES = 1\nMY_TABLE = 2\n", "TABLE"))

    def test_a_mutant_with_readers_runs_the_tests_that_read_the_name_and_leaves_out_a_suite_that_read_none(
        self,
    ) -> None:
        table = Mutant("tools/table.py", 5, "x", "y", "z", True, frozenset({12, 13}))
        reads = Reach(
            30.0,
            {"tools/table.py": frozenset({5, 12, 13})},
            {"tools/table.py": {12: frozenset({"t::a"}), 13: frozenset({"t::b"})}},
        )
        imports = Reach(1.0, {"tools/table.py": frozenset({5})})
        self.assertEqual(["t::a", "t::b"], suite_selection(table, reads))
        self.assertIsNone(suite_selection(table, imports))
        self.assertEqual(
            [("tests/reads.py", ["t::a", "t::b"])],
            suite_plan(table, {"tests/reads.py": reads, "tests/imports.py": imports}),
        )

    def test_a_reader_run_outside_a_test_function_or_no_reader_run_at_all_runs_every_suite_whole(self) -> None:
        table = Mutant("tools/table.py", 5, "x", "y", "z", True, frozenset({12, 13}))
        in_a_process = Reach(
            30.0, {"tools/table.py": frozenset({5, 12, 13})}, {"tools/table.py": {12: frozenset({"t::a"})}}
        )
        self.assertEqual([], suite_selection(table, in_a_process))
        imports = Reach(1.0, {"tools/table.py": frozenset({5})})
        self.assertEqual(
            [("tests/fast.py", []), ("tests/slow.py", [])],
            suite_plan(table, {"tests/slow.py": Reach(9.0, imports.lines), "tests/fast.py": imports}),
        )
        # A mutant without readers keeps the selection by its own line.
        plain = Mutant("tools/table.py", 12, "x", "y", "z")
        self.assertEqual(["t::a"], suite_selection(plain, in_a_process))
        self.assertEqual([], suite_selection(plain, imports))


REPOSITORY = Path(__file__).resolve().parents[2]
SANCTIONED_MODULE = """\"\"\"A module with a sanction table.\"\"\"

FSOPS_ALLOWED = {
    "write_1": "a throwaway file",
}
LIMIT = 3


def scale(a):
    return a * 2
"""
POLICY = """FSOPS_ALLOWANCE = "FSOPS_ALLOWED"
OTHER = "UNREAD_ALLOWED"
"""


def policy_tree(root: Path) -> None:
    """A tree holding SANCTIONED_MODULE, a policy that reads FSOPS_ALLOWED, and a suite and a name that read nothing."""
    (root / "tools").mkdir()
    (root / "tools" / "table.py").write_text(SANCTIONED_MODULE, encoding="utf-8")
    (root / "tests" / "validation").mkdir(parents=True)
    (root / "tests" / "validation" / "policy.py").write_text(POLICY, encoding="utf-8")
    (root / "tests" / "validation" / "a_test.py").write_text('SUITE_ALLOWANCE = "SUITE_ALLOWED"\n', encoding="utf-8")


class SanctionTableFixtures(unittest.TestCase):
    """A sanction table is held by the validation policy that reads it, never by the module's suites, so its lines make
    no mutant, and every other changed line keeps its mutants."""

    def test_a_table_line_makes_no_mutant_and_an_ordinary_line_beside_it_still_does(self) -> None:
        found = module_mutants("tools/table.py", SANCTIONED_MODULE, frozenset({4, 6, 10}), line_generator)
        self.assertEqual([(6, "LIMIT = 9"), (10, "return a * 9")], [(item.line, item.after) for item in found])
        # Under any other name the same table is an ordinary assignment, and is mutated.
        plain = SANCTIONED_MODULE.replace("FSOPS_ALLOWED", "FSOPS_TABLE")
        mutated = module_mutants("tools/table.py", plain, frozenset({4, 6, 10}), line_generator)
        self.assertEqual([4, 6, 10], [item.line for item in mutated])

    def test_a_sanction_table_is_one_name_ending_in_allowed_bound_at_module_level(self) -> None:
        source = "A_ALLOWED = {}\nB_ALLOWED: dict[str, str] = {}\nC_ALLOWED, D = {}, 1\nALLOWED = 1\nimport os\n"
        names = [sanction_table(statement) for statement in ast.parse(source).body]
        self.assertEqual(["A_ALLOWED", "B_ALLOWED", None, None, None], names)
        self.assertIsNone(sanction_table(None))
        tree = ast.parse(SANCTIONED_MODULE)
        self.assertEqual(["FSOPS_ALLOWED"], changed_tables(tree, frozenset({5, 10})))
        # A table's first and last lines are its own.
        self.assertEqual(["FSOPS_ALLOWED"], changed_tables(tree, frozenset({3})))
        self.assertEqual(["FSOPS_ALLOWED"], changed_tables(tree, frozenset({5})))
        self.assertEqual([], changed_tables(tree, frozenset({2, 6, 10})))

    def test_a_policy_reads_the_table_it_names_under_a_name_ending_in_allowance_and_a_suite_reads_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            policy_tree(Path(temporary))
            self.assertEqual({"FSOPS_ALLOWED": "tests/validation/policy.py"}, policy_tables(Path(temporary)))

    def test_each_unread_table_is_named_and_a_read_one_is_no_problem(self) -> None:
        policies = {"FSOPS_ALLOWED": "tests/validation/policy.py"}
        self.assertIsNone(mutation_gate.unread_tables("tools/t.py", ["FSOPS_ALLOWED"], policies))
        problem = mutation_gate.unread_tables("tools/t.py", ["A_ALLOWED", "FSOPS_ALLOWED", "B_ALLOWED"], policies)
        self.assertEqual(
            "tools/t.py changes A_ALLOWED, B_ALLOWED, which no validation policy under tests/validation reads, so the "
            "gate would leave it unchecked: add the policy that reads it, or rename the table so its name does not "
            "end in _ALLOWED",
            problem,
        )

    def test_the_repositorys_policies_read_the_tables_their_rules_name(self) -> None:
        found = policy_tables(REPOSITORY)
        self.assertEqual("tests/validation/fsops_platform.py", found["FSOPS_ALLOWED"])
        self.assertEqual("tests/validation/fsops_platform.py", found["PLATFORM_ALLOWED"])
        self.assertEqual("tests/validation/skill_scripts.py", found["COMMANDS_ALLOWED"])
        self.assertEqual("tests/validation/duplication.py", found["DUPLICATION_ALLOWED"])
        self.assertEqual("tests/validation/kinds_table.py", found["KINDS_ALLOWED"])

    def test_every_sanction_table_of_a_module_the_gate_mutates_has_a_policy_that_reads_it(self) -> None:
        policies = policy_tables(REPOSITORY)
        unread = []
        for folder in ("deployer", "tools", "skills", ".claude/skills"):
            for path in sorted((REPOSITORY / folder).rglob("*.py")):
                module = path.relative_to(REPOSITORY).as_posix()
                if not is_gate_module(module):
                    continue
                for statement in ast.parse(path.read_bytes().decode("utf-8")).body:
                    name = sanction_table(statement)
                    if name and name not in policies:
                        unread.append(f"{module}: {name}")
        self.assertEqual([], unread)

    def test_selection_names_each_changed_table_with_its_policy_and_refuses_one_no_policy_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            policy_tree(tree)
            git = mock.create_autospec(GitClient, instance=True)
            git.output.return_value = "@@ -0,0 +4 @@\n@@ -0,0 +10 @@\n"
            gate = Gate(tree, "main", 1, 1, git, line_generator)
            lines, found, tables = gate.select(tree, ["tools/table.py"])["tools/table.py"]
            self.assertEqual((2, [10]), (lines, [item.line for item in found]))
            self.assertEqual((("FSOPS_ALLOWED", "tests/validation/policy.py"),), tables)
            unread = SANCTIONED_MODULE.replace("FSOPS_ALLOWED", "UNREAD_ALLOWED")
            (tree / "tools" / "table.py").write_text(unread, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "tools/table.py changes UNREAD_ALLOWED, which no validation"):
                gate.select(tree, ["tools/table.py"])


def test_the_cap_gives_the_reach_pass_half_of_the_run() -> None:
    assert mutation_gate.run_deadline(100.0, 40) == 100.0 + 40 * 60
    assert mutation_gate.reach_deadline(100.0, 100.0 + 40 * 60) == 100.0 + 20 * 60


def test_the_queue_takes_one_mutant_from_each_module_in_turn() -> None:
    first = [mutant(1, "tools/a.py"), mutant(2, "tools/a.py"), mutant(3, "tools/a.py")]
    second = [mutant(7, "tools/b.py")]
    assert [first[0], second[0], first[1], first[2]] == queue_order([first, [], second])


def linux_module(module: str, outcomes: list[Outcome], lines: int) -> ModuleReport:
    """A module the Linux leg alone ran, or, with no outcomes, one no leg ran."""
    return ModuleReport(module, lines, (), {LINUX_LEG: outcomes} if outcomes else {})


class SummaryFixtures(unittest.TestCase):
    def report(self, outcomes: list[Outcome], lines: int = 12) -> Report:
        leg = LegRun(
            LINUX_LEG,
            40,
            600.0,
            4,
            {"tools/calc.py": outcomes},
            {"tests/broken.py": "failed unmutated with exit code 1"},
            ["tests/late.py"],
        )
        return Report("origin/main", "3.8.0", [linux_module("tools/calc.py", outcomes, lines)], [leg])

    def test_the_kill_ratio_counts_timeouts_as_killed_and_leaves_out_what_was_not_tested(self) -> None:
        found = {KILLED: 6, TIMED_OUT: 1, SURVIVED: 3, NOT_REACHED: 4, NOT_RUN: 2}
        self.assertEqual("70% (7 of 10)", kill_ratio(found))
        self.assertEqual("none tested", kill_ratio(dict.fromkeys(found, 0) | {NOT_REACHED: 3}))

    def test_the_summary_tables_each_module_and_names_each_survivor_by_line_mutation_and_suites(self) -> None:
        survivor = Outcome(mutant(4), SURVIVED, ("tests/fast.py", "tests/slow.py"))
        outcomes = [Outcome(mutant(1), KILLED, ("tests/fast.py",)), survivor, Outcome(mutant(9), NOT_REACHED)]
        report = self.report(outcomes)
        report.legs[0].unmeasured_suites.append("tests/later.py")
        text = summary(report)
        self.assertIn("| `tools/calc.py` | Linux | 12 | 3 | 1 | 0 | 1 | 1 | 0 | 0 | 50% (1 of 2) |", text)
        self.assertIn("### `tools/calc.py`: 1 surviving mutant\n", text)
        self.assertIn("- Line 4: `line 4` became `mutated 4`; passed `tests/fast.py`, `tests/slow.py` on Linux\n", text)
        self.assertIn("mutants made by mutmut 3.8.0 on the lines the pull request changed", text)
        self.assertNotIn("mutmut made no mutant", text)
        self.assertIn(
            "\n- On Linux, 4 workers ran for 10 of its 40 minutes; a mutant ran against the suites that executed its "
            "statement unmutated, fastest first, until one failed.\n"
            "- The Linux leg runs every module's mutants, and the Windows leg those of each module that is or imports "
            "`deployer/platform_support.py`, `device_names`, `executable_locator`, `job_tree`, `link_probe`, "
            "`path_keys`, `pdf_tool`, or `process_identity`, holds a `PLATFORM_ALLOWED` table, or reads a platform "
            "name in any expression, such as `sys.platform`, `msvcrt`, `creationflags`, or `os.altsep`; a mutant "
            "either leg killed counts as killed.\n\n"
            "| Module | Legs | Changed lines | Mutants | Killed | Timed out | Survived | Not reached | Not run "
            "| Errored | Kill ratio |\n|---|---|---|---|---|---|---|---|---|---|---|\n",
            text,
        )
        self.assertIn(
            "\n\n`tests/broken.py` failed unmutated with exit code 1 on Linux, so no mutant ran against it there.\n",
            text,
        )
        self.assertIn(
            "\n\nThe cap left these suites' reach unmeasured on Linux, so no mutant ran against them there: "
            "`tests/late.py`, `tests/later.py`.\n",
            text,
        )
        self.assertNotIn("killed on one leg alone", text)
        self.assertNotIn("cut", text.split("Kill ratio")[1].split("###")[0].replace("unmutated", ""))

    def test_the_summary_names_each_mutant_not_reached_by_line_and_mutation_under_its_module(self) -> None:
        outcomes = [
            Outcome(mutant(1), KILLED, ("t",)),
            Outcome(mutant(3), SURVIVED, ("t",)),
            Outcome(Mutant("tools/calc.py", 5, "`a`", "b", "s"), NOT_REACHED),
            Outcome(mutant(9), NOT_REACHED),
        ]
        report = self.report(outcomes)
        report.modules.append(linux_module("tools/other.py", [Outcome(mutant(2, "tools/other.py"), NOT_REACHED)], 1))
        text = summary(report)
        self.assertTrue(
            text.endswith(
                "### `tools/calc.py`: 1 surviving mutant\n\n"
                "- Line 3: `line 3` became `mutated 3`; passed `t` on Linux\n\n"
                "### `tools/calc.py`: 2 mutants not reached\n\n"
                "- Line 5: `` `a` `` became `b`; no suite executed its statement\n"
                "- Line 9: `line 9` became `mutated 9`; no suite executed its statement\n\n"
                "### `tools/other.py`: 1 mutant not reached\n\n"
                "- Line 2: `line 2` became `mutated 2`; no suite executed its statement\n"
            ),
            text,
        )
        self.assertNotIn("not reached\n", summary(self.report([Outcome(mutant(1), KILLED, ("t",))])))

    def test_a_survivor_names_how_many_of_a_suites_tests_it_ran_against_when_not_all_of_them(self) -> None:
        survivor = Outcome(mutant(4), SURVIVED, ("tests/fast.py", "tests/slow.py", "tests/one.py"), (3, 0, 1))
        text = summary(self.report([survivor]))
        self.assertIn(
            "- Line 4: `line 4` became `mutated 4`; passed `tests/fast.py` (3 tests that ran the line), "
            "`tests/slow.py`, `tests/one.py` (1 test that ran the line) on Linux",
            text,
        )

    def test_every_outcome_is_counted_and_survivors_are_counted_and_marked_one_by_one(self) -> None:
        outcomes = [
            Outcome(mutant(1), KILLED, ("t",)),
            Outcome(mutant(2), KILLED, ("t",)),
            Outcome(mutant(3), SURVIVED, ("t",)),
            Outcome(mutant(5), SURVIVED, ("t",)),
        ]
        self.assertEqual(
            {KILLED: 2, TIMED_OUT: 0, SURVIVED: 2, NOT_REACHED: 0, NOT_RUN: 0, ERRORED: 0}, counts(outcomes)
        )
        text = summary(self.report(outcomes))
        self.assertIn("### `tools/calc.py`: 2 surviving mutants", text)
        self.assertIn("- Line 3: `line 3` became `mutated 3`; passed `t` on Linux", text)
        self.assertIn("- Line 5: `line 5` became `mutated 5`; passed `t` on Linux", text)

    def test_a_changed_module_with_no_mutant_is_named_and_never_left_silent(self) -> None:
        text = summary(self.report([], lines=3))
        self.assertIn("| `tools/calc.py` | none | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | none tested |", text)
        self.assertIn(
            "\n`tools/calc.py`: mutmut made no mutant on the 3 lines this pull request changed, so none ran.\n", text
        )
        one = summary(self.report([], lines=1))
        self.assertIn("mutmut made no mutant on the 1 line this pull request changed, so none ran.", one)
        deleted = summary(self.report([], lines=0))
        self.assertIn(
            "`tools/calc.py` keeps no line this pull request added or changed, so it has nothing to mutate.", deleted
        )

    def test_a_skipped_sanction_table_is_named_with_its_policy_in_place_of_an_empty_selection(self) -> None:
        report = self.report([])
        report.modules[0].tables = (
            ("FSOPS_ALLOWED", "tests/validation/fsops_platform.py"),
            ("KINDS_ALLOWED", "tests/validation/kinds_table.py"),
        )
        text = summary(report)
        # The table's line is added after the module table, never in place of the summary.
        self.assertTrue(text.startswith("## Mutation gate\n"), text)
        self.assertIn(
            "|\n\n`tools/calc.py`: the changed lines of its sanction tables made no mutant, since the validation "
            "policy that reads each holds it, not the module's suites: `FSOPS_ALLOWED` "
            "(`tests/validation/fsops_platform.py`), `KINDS_ALLOWED` (`tests/validation/kinds_table.py`).\n",
            text,
        )
        self.assertNotIn("mutmut made no mutant", text)
        # A module report built without tables holds an empty tuple of them, never None.
        self.assertEqual((), ModuleReport("tools/calc.py", 0).tables)
        # ...and no leg, so no outcome.
        self.assertEqual([], ModuleReport("tools/calc.py", 0).outcomes)

    def test_the_summary_says_when_the_cap_cut_a_module_short(self) -> None:
        text = summary(self.report([Outcome(mutant(1), KILLED, ("t",)), Outcome(mutant(2), NOT_RUN)]))
        self.assertIn("\n\nThe cap cut `tools/calc.py` short on Linux: 1 mutants did not run.\n", text)

    def test_a_run_the_cap_cut_short_says_so_on_its_first_line_naming_each_module_and_its_counts(self) -> None:
        table = Mutant("tools/calc.py", 3, "a", "b", "c", True)
        outcomes = [Outcome(mutant(1), KILLED, ("t",)), Outcome(mutant(2), NOT_RUN), Outcome(table, NOT_RUN)]
        report = self.report(outcomes)
        report.modules.append(linux_module("tools/other.py", [Outcome(mutant(4, "tools/other.py"), NOT_RUN)], 1))
        first = summary(report).splitlines()[0]
        self.assertEqual(
            "**Unfinished, so not a verdict:** a leg's cap cut the run short: 2 of the 3 mutants of `tools/calc.py` "
            "did not run on Linux (1 on lines that run at import); 1 of the 1 mutants of `tools/other.py` did not run "
            "on Linux. Split the pull request, or move its module-level tables into the functions that use them, "
            "until it finishes.",
            first,
        )
        # The suite whose reach was not measured leaves no verdict open beside a mutant killed or not run.
        self.assertIn("`tests/late.py`. No mutant it ran survived or went unreached", summary(report))
        self.assertEqual(["", "## Mutation gate"], summary(report).splitlines()[1:3])

    def test_a_reach_the_cap_left_unmeasured_leaves_the_run_unfinished_while_a_mutant_it_ran_is_open(self) -> None:
        for status in (SURVIVED, NOT_REACHED):
            with self.subTest(status=status):
                report = self.report([Outcome(mutant(1), KILLED, ("t",)), Outcome(mutant(2), status)])
                report.legs[0].unmeasured_suites.append("tests/later.py")
                self.assertEqual(
                    "**Unfinished, so not a verdict:** a leg's cap cut the run short: the reach of 2 suites was not "
                    "measured on Linux, which leaves 1 mutant that survived or no suite reached open there. Split the "
                    "pull request, or move its module-level tables into the functions that use them, until it "
                    "finishes.",
                    unfinished_line(report),
                )
                self.assertNotIn("leave no verdict open", summary(report))

    def test_a_reach_the_cap_left_unmeasured_beside_only_killed_mutants_leaves_the_run_finished(self) -> None:
        report = self.report([Outcome(mutant(1), KILLED, ("t",)), Outcome(mutant(2), TIMED_OUT, ("t",))])
        report.legs[0].unmeasured_suites.append("tests/later.py")
        self.assertIsNone(unfinished_line(report))
        text = summary(report)
        self.assertEqual("## Mutation gate", text.splitlines()[0])
        self.assertIn(
            "\n\nThe cap left these suites' reach unmeasured on Linux, so no mutant ran against them there: "
            "`tests/late.py`, `tests/later.py`. No mutant it ran survived or went unreached, so they leave no verdict "
            "open.\n",
            text,
        )

    def test_a_widely_imported_modules_windows_reach_cut_short_after_both_legs_killed_it_is_a_verdict(self) -> None:
        # Every mutant killed on both legs, while the Windows leg's cap left most suites' reach unmeasured.
        report = two_legs([Outcome(TEXTUAL, KILLED, ("t",))], [Outcome(TEXTUAL, KILLED, ("t",))])
        report.legs[1].unmeasured_suites.extend(["tests/a.py", "tests/b.py"])
        self.assertIsNone(unfinished_line(report))
        # A mutant Windows killed, which survived on Linux, is a verdict too; one that survived both legs is open.
        killed = two_legs([Outcome(TEXTUAL, SURVIVED, ("t",))], [Outcome(TEXTUAL, KILLED, ("t",))])
        killed.legs[0].unmeasured_suites.append("tests/a.py")
        self.assertIsNone(unfinished_line(killed))
        survived = two_legs([Outcome(TEXTUAL, SURVIVED, ("t",))], [Outcome(TEXTUAL, SURVIVED, ("t",))])
        survived.legs[1].unmeasured_suites.append("tests/a.py")
        self.assertIn(
            "the reach of 1 suite was not measured on Windows, which leaves 1 mutant that survived or no suite "
            "reached open there",
            str(unfinished_line(survived)),
        )
        unreached = Outcome(mutant(5, PATH_MODULE), NOT_REACHED)
        two = two_legs([Outcome(TEXTUAL, SURVIVED, ("t",)), unreached], [Outcome(TEXTUAL, SURVIVED, ("t",)), unreached])
        two.legs[1].unmeasured_suites.append("tests/a.py")
        self.assertIn(
            "on Windows, which leaves 2 mutants that survived or no suite reached open there",
            str(unfinished_line(two)),
        )
        # A leg's unmeasured suites leave open only the mutants of modules that leg ran.
        other = two_legs([Outcome(TEXTUAL, KILLED, ("t",))], [Outcome(TEXTUAL, KILLED, ("t",))])
        other.modules.append(linux_module("tools/calc.py", [Outcome(mutant(1), SURVIVED, ("t",))], 1))
        other.legs[1].unmeasured_suites.append("tests/a.py")
        self.assertIsNone(unfinished_line(other))

    def test_an_errored_mutant_leaves_the_run_unfinished_and_is_listed_with_its_suites(self) -> None:
        outcomes = [Outcome(mutant(1), KILLED, ("t",)), Outcome(mutant(4), ERRORED, ("tests/x.py",), (2,))]
        report = self.report(outcomes)
        report.legs[0].unmeasured_suites.clear()
        self.assertEqual(
            "**Unfinished, so not a verdict:** A suite ended with neither a pass nor a test failure, so it judged "
            "nothing: 1 of the 2 mutants of `tools/calc.py` errored on Linux. Rerun the gate, and fix what its log "
            "shows the suite stopped on.",
            unfinished_line(report),
        )
        text = summary(report)
        self.assertIn("| `tools/calc.py` | Linux | 12 | 2 | 1 | 0 | 0 | 0 | 0 | 1 | 100% (1 of 1) |", text)
        self.assertIn(
            "### `tools/calc.py`: 1 errored mutant, which no suite judged\n\n"
            "- Line 4: `line 4` became `mutated 4`; ran `tests/x.py` (2 tests that ran the line) on Linux\n",
            text,
        )

    def test_a_finished_run_opens_on_its_heading_with_no_unfinished_line(self) -> None:
        report = self.report([Outcome(mutant(1), KILLED, ("t",)), Outcome(mutant(2), NOT_REACHED)])
        report.legs[0].unmeasured_suites.clear()
        self.assertIsNone(unfinished_line(report))
        self.assertEqual("## Mutation gate", summary(report).splitlines()[0])

    def test_a_survivor_with_readers_names_the_tests_that_ran_its_line_or_read_what_it_binds(self) -> None:
        table = Mutant("tools/calc.py", 4, "line 4", "mutated 4", "s", True, frozenset({9}))
        text = summary(self.report([Outcome(table, SURVIVED, ("tests/fast.py",), (2,))]))
        self.assertIn("passed `tests/fast.py` (2 tests that ran the line or read a name it binds)", text)

    def test_a_change_with_no_module_to_mutate_is_one_line(self) -> None:
        line = no_module_line("origin/main")
        self.assertEqual(1, line.count("\n"))
        self.assertTrue(line.endswith("since `origin/main`, so no mutant ran.\n"))


PATH_MODULE = "tools/paths.py"
# A mutant like those only Windows tells apart: a path compared by its text, where Windows' own parsing drops a name's
# trailing dots and spaces.
TEXTUAL = Mutant(PATH_MODULE, 4, "path_key(name, textual=True)", "path_key(name, textual=False)", "source")


def two_legs(linux: list[Outcome], windows: list[Outcome], lines: int = 5) -> Report:
    """A run of one module both legs ran, the Windows leg handed in first, as the join may read it."""
    selected = Selected(
        "origin/main", "3.8.0", (SelectedModule(PATH_MODULE, lines, tuple(o.mutant for o in linux), (), True),)
    )
    legs = [
        LegRun(WINDOWS_LEG, 40, 2400.0, 4, {PATH_MODULE: windows}, {}, []),
        LegRun(LINUX_LEG, 40, 120.0, 4, {PATH_MODULE: linux}, {}, []),
    ]
    return joined(selected, legs)


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((SURVIVED, KILLED), KILLED),
        ((NOT_REACHED, TIMED_OUT), TIMED_OUT),
        ((TIMED_OUT, KILLED), KILLED),
        ((SURVIVED, NOT_RUN), NOT_RUN),
        ((SURVIVED, ERRORED), ERRORED),
        ((ERRORED, NOT_REACHED), ERRORED),
        ((ERRORED, KILLED), KILLED),
        ((ERRORED, TIMED_OUT), TIMED_OUT),
        ((NOT_RUN, ERRORED), NOT_RUN),
        ((KILLED, NOT_RUN), KILLED),
        ((NOT_REACHED, SURVIVED), SURVIVED),
        ((NOT_REACHED, NOT_REACHED), NOT_REACHED),
        ((SURVIVED,), SURVIVED),
    ],
)
def test_a_mutant_is_killed_when_any_leg_killed_it_and_not_run_when_a_leg_that_might_have_did_not(
    statuses: tuple[str, ...], expected: str
) -> None:
    assert verdict([Outcome(TEXTUAL, status) for status in statuses]).status == expected


def test_a_mutant_no_leg_ran_has_no_verdict() -> None:
    with pytest.raises(ValueError, match=r"^no leg ran the mutant$"):
        verdict([])


@pytest.mark.parametrize(
    ("module", "source", "windows"),
    [
        ("deployer/platform_support.py", "x = 1\n", True),
        ("skills/ejryan89-skill-core/scripts/path_keys.py", "x = 1\n", True),
        ("tools/runner.py", 'PLATFORM_ALLOWED = {"win32": "why"}\n', True),
        ("deployer/plan.py", "from . import platform_support\n", True),
        ("deployer/fsops.py", "from .platform_support import WINDOWS\n", True),
        ("tools/gate.py", "from deployer import platform_support\n", True),
        ("tools/report.py", "from path_keys import path_key\n", True),
        ("tools/report.py", "def probe():\n    import link_probe\n", True),
        ("tools/report.py", 'import importlib\nimportlib.import_module("device_names")\n', True),
        ("tools/report.py", "import os.path\nFSOPS_ALLOWED = {}\n", False),
        ("tools/report.py", '# path_keys\nNAME = "link_probe"\n', False),
        ("tools/path_keys_report.py", "x = 1\n", False),
        # A module whose own code compares a platform name, with no seam import and no table.
        ("tools/report.py", 'import sys\nif sys.platform == "win32":\n    x = 1\n', True),
        ("skills/x/scripts/x.py", 'import sys as s\nX = "a" if "win32" != s.platform else "b"\n', True),
        (
            "tools/report.py",
            'from sys import platform\nWINDOWS = platform.startswith("win") or platform == "cygwin"\n',
            True,
        ),
        ("tools/report.py", 'import os\ndef nt():\n    return os.name in ("nt",)\n', True),
        ("tools/report.py", 'import platform\nif platform.system() == "Windows":\n    pass\n', True),
        # A platform name read in any expression, not only a comparison.
        ("tools/report.py", 'import sys\nif sys.platform.startswith("win"):\n    pass\n', True),
        ("tools/report.py", 'import sys\nmatch sys.platform:\n    case "win32":\n        pass\n', True),
        ("tools/report.py", 'import platform\nWINDOWS = platform.system().lower() == "windows"\n', True),
        ("tools/report.py", 'import os\nNT = os.name.casefold() == "nt"\n', True),
        ("tools/report.py", 'import sys\ncurrent = sys.platform\nif current == "win32":\n    pass\n', True),
        ("tools/report.py", 'import sys\nSEP = {"win32": ";", "linux": ":"}[sys.platform]\n', True),
        ("tools/report.py", "import pathlib\ndef native(p):\n    return isinstance(p, pathlib.WindowsPath)\n", True),
        (
            "tools/report.py",
            "from pathlib import WindowsPath as W\ndef native(p):\n    return isinstance(p, W)\n",
            True,
        ),
        ("tools/report.py", "import sys\nprint(sys.platform)\n", True),
        ("tools/report.py", "import os\ndef stop(pid):\n    os.killpg(pid, 9)\n", True),
        ("tools/report.py", "import subprocess\nFLAGS = subprocess.CREATE_NO_WINDOW\n", True),
        ("tools/report.py", "import platform\nprint(platform.python_version())\n", False),
        # A test of whether a name exists decides on the platform too.
        ("tools/report.py", 'import os\nif hasattr(os, "fork"):\n    pass\n', True),
        ("tools/report.py", 'import sys\nNAME = getattr(sys, "platform")\n', True),
        ("tools/report.py", 'import os\nprint(getattr(os, "home"))\n', False),
        ("tools/report.py", 'import tool\nif tool.platform == "win32":\n    pass\n', False),
        ("tools/report.py", '# if sys.platform == "win32"\nx = 1\n', False),
    ],
)
def test_the_windows_leg_runs_the_platform_seam_each_module_that_imports_it_or_holds_its_table_and_each_that_branches(
    module: str, source: str, windows: bool
) -> None:
    assert runs_on_windows(module, source) is windows


def claude_md_seam_modules() -> list[str]:
    """The ejryan89-skill-core modules CLAUDE.md names as the platform seam's exceptions, in the paragraph that opens
    by keeping operating-system-specific behavior in deployer/platform_support.py."""
    text = (mutation_gate.REPOSITORY_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    paragraph = next(block for block in text.split("\n\n") if "Keep every operating-system-specific behavior" in block)
    return sorted(set(re.findall(r"`([a-z_]+\.py)`", paragraph.split("The exceptions are", 1)[1])))


@pytest.mark.parametrize(
    "module",
    [
        *(f"skills/ejryan89-skill-core/scripts/{name}" for name in claude_md_seam_modules()),
        "skills/ejryan89-code-review-core/scripts/review_config.py",
        "skills/ejryan89-code-review-core/scripts/review_io.py",
        "skills/ejryan89-code-review-core/scripts/review_process.py",
    ],
)
def test_the_windows_leg_runs_each_seam_module_claude_md_names_and_each_module_that_branches_on_the_platform(
    module: str,
) -> None:
    source = (mutation_gate.REPOSITORY_ROOT / module).read_text(encoding="utf-8")
    assert runs_on_windows(module, source)


# A module that names one token of each kind the platform catalogue holds, as code that reads it would.
CATALOGUE_SOURCES: dict[str, Callable[[str], str]] = {
    "qualified": lambda token: f"import {token.rsplit('.', 1)[0]}\nVALUE = {token}\n",
    "qualified_prefix": lambda token: f"import {token.split('.')[0]}\nVALUE = {token}NAME\n",
    "attribute": lambda token: f"def change(path):\n    return path.{token}\n",
    "keyword": lambda token: f"import subprocess\nsubprocess.run(['x'], {token}=None)\n",
    "module": lambda token: f"import {token}\n",
    "variable": lambda token: f"import os\nVALUE = os.environ.get({token!r})\n",
    "command": lambda token: f"import subprocess\nsubprocess.run([{token!r}, 'x'])\n",
    "path": lambda token: f"VALUE = open({token + '/1/stat'!r})\n",
    "extension": lambda token: f"def program(name):\n    return name + {token!r}\n",
}


def test_every_kind_of_the_platform_catalogue_has_a_source_that_reads_it() -> None:
    """A kind the catalogue adds later fails here until a row reads it."""
    assert set(CATALOGUE_SOURCES) == set(PLATFORM_TOKENS)


@pytest.mark.parametrize(
    ("kind", "token"), [(kind, token) for kind, tokens in PLATFORM_TOKENS.items() for token in tokens]
)
def test_the_windows_leg_runs_a_module_that_names_any_token_of_the_platform_catalogue(kind: str, token: str) -> None:
    assert runs_on_windows("tools/report.py", CATALOGUE_SOURCES[kind](token))


@pytest.mark.parametrize(
    "value",
    [
        "os.sep",
        "os.altsep",
        "os.pathsep",
        "os.linesep",
        "os.devnull",
        "os.path.sep",
        "os.path.altsep",
        "os.path.pathsep",
    ],
)
def test_the_windows_leg_runs_a_module_that_reads_a_separator_or_the_null_device(value: str) -> None:
    owner, name = value.rsplit(".", 1)
    for source in (
        f"import {owner}\nVALUE = {value}\n",
        f"import {owner}\nVALUE = getattr({owner}, {name!r})\n",
        f"from {owner} import {name}\n",
        f"from {owner} import {name} as found\nVALUE = found\n",
    ):
        assert runs_on_windows("tools/report.py", source), source


def test_a_module_that_reads_no_platform_value_stays_on_linux() -> None:
    source = "import os\nVALUE = os.path.join(os.curdir, os.extsep)\nNAME = getattr(os, 'getcwd')\n"
    assert not runs_on_windows("tools/report.py", source)


@pytest.mark.parametrize("seam", ["platform_support", *(Path(name).stem for name in claude_md_seam_modules())])
def test_the_windows_leg_runs_an_importer_of_each_seam_module_claude_md_names(seam: str) -> None:
    for source in (
        f"import {seam}\n",
        f"from {seam} import name\n",
        f"def late():\n    import {seam}\n",
        f"import importlib\nimportlib.import_module({seam!r})\n",
    ):
        assert runs_on_windows("tools/report.py", source), source


@pytest.mark.parametrize(
    "module",
    ["skills/audit-ai-config/scripts/mcp_handshake.py", "skills/ejryan89-code-review-core/scripts/review_hosts.py"],
)
def test_the_windows_leg_runs_a_module_that_reads_a_separator_or_imports_a_seam_module(module: str) -> None:
    assert runs_on_windows(module, (mutation_gate.REPOSITORY_ROOT / module).read_text(encoding="utf-8"))


def test_the_summary_names_the_windows_rule_as_the_selector_holds_it() -> None:
    line = mutation_gate.windows_rule_line()
    for seam in ["platform_support", *(Path(name).stem for name in claude_md_seam_modules())]:
        assert f"`{seam}`" in line or f"`deployer/{seam}.py`" in line, seam
    assert "reads a platform name in any expression" in line
    assert "compares" not in line
    docstring = " ".join((mutation_gate.__doc__ or "").split())
    assert "reads the platform in any expression" in docstring
    assert "compares a platform name" not in docstring


def test_claude_md_names_the_seam_modules_the_windows_leg_is_held_to() -> None:
    assert {"job_tree.py", "process_identity.py", "pdf_tool.py", "path_keys.py"} <= set(claude_md_seam_modules())


class LegFixtures(unittest.TestCase):
    def test_a_mutant_that_survives_on_linux_and_is_killed_on_windows_reads_as_killed_with_its_leg_named(self) -> None:
        report = two_legs(
            [Outcome(TEXTUAL, SURVIVED, ("tests/test_paths.py",))],
            [Outcome(TEXTUAL, KILLED, ("tests/test_paths.py",))],
        )
        text = summary(report)
        self.assertIsNone(unfinished_line(report))
        self.assertIn("| `tools/paths.py` | Linux, Windows | 5 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 100% (1 of 1) |", text)
        self.assertIn(
            "|\n\n### `tools/paths.py`: 1 mutant killed on one leg alone, which counts as killed\n\n"
            "- Line 4: `path_key(name, textual=True)` became `path_key(name, textual=False)`; killed on Windows by "
            "`tests/test_paths.py`, survived on Linux\n",
            text,
        )
        self.assertNotIn("surviving", text)
        self.assertIn("- Modules changed since `origin/main`: 1; mutants made by mutmut 3.8.0 on", text)
        self.assertIn(
            "- On Linux, 4 workers ran for 2 of its 40 minutes; on Windows, 4 workers ran for 40 of its 40 minutes;",
            text,
        )

    def test_each_mutant_one_leg_alone_killed_is_counted_and_listed(self) -> None:
        other = Mutant(PATH_MODULE, 6, "a", "b", "source")
        text = summary(
            two_legs(
                [Outcome(TEXTUAL, SURVIVED, ("t",)), Outcome(other, KILLED, ("t",))],
                [Outcome(TEXTUAL, KILLED, ("w",)), Outcome(other, NOT_REACHED)],
            )
        )
        self.assertIn("### `tools/paths.py`: 2 mutants killed on one leg alone, which counts as killed\n", text)
        self.assertIn("- Line 6: `a` became `b`; killed on Linux by `t`, not reached on Windows\n", text)

    def test_mutants_both_legs_left_not_run_name_both_legs(self) -> None:
        report = two_legs([Outcome(TEXTUAL, NOT_RUN)], [Outcome(TEXTUAL, NOT_RUN)])
        self.assertIn(
            "1 of the 1 mutants of `tools/paths.py` did not run on Linux and Windows.", str(unfinished_line(report))
        )

    def test_a_leg_of_a_platform_the_gate_does_not_name_comes_after_the_named_legs(self) -> None:
        chosen = SelectedModule(PATH_MODULE, 1, (TEXTUAL,), (), True)
        legs = [
            LegRun("Plan 9", 40, 1.0, 1, {PATH_MODULE: [Outcome(TEXTUAL, KILLED, ("p",))]}, {}, []),
            LegRun(LINUX_LEG, 40, 1.0, 1, {PATH_MODULE: [Outcome(TEXTUAL, SURVIVED, ("t",))]}, {}, []),
        ]
        report = joined(Selected("origin/main", "3.8.0", (chosen,)), legs)
        self.assertEqual([LINUX_LEG, "Plan 9"], [leg.name for leg in report.legs])
        self.assertEqual([LINUX_LEG, "Plan 9"], list(report.modules[0].legs))

    def test_a_run_no_leg_ran_says_so_in_place_of_the_legs_times(self) -> None:
        empty = SelectedModule("tools/calc.py", 2, (), (), False)
        text = summary(joined(Selected("origin/main", "3.8.0", (empty,)), []))
        self.assertIn("\n- No leg ran, since no changed line made a mutant.\n", text)
        self.assertNotIn("- On ", text)

    def test_a_mutant_no_linux_suite_reached_that_windows_kills_reads_as_killed(self) -> None:
        folded = Mutant(PATH_MODULE, 7, "name_key(name, folder)", "name_key(name, None)", "source")
        text = summary(
            two_legs([Outcome(folded, NOT_REACHED)], [Outcome(folded, TIMED_OUT, ("tests/a.py", "tests/b.py"))])
        )
        self.assertIn("| 1 | 0 | 1 | 0 | 0 | 0 | 0 | 100% (1 of 1) |", text)
        self.assertIn("timed out on Windows by `tests/b.py`, not reached on Linux\n", text)
        self.assertNotIn("mutant not reached", text)

    def test_a_survivor_of_both_legs_names_each_legs_suites_and_a_mutant_both_killed_is_not_listed(self) -> None:
        both = Mutant(PATH_MODULE, 2, "a", "b", "source")
        text = summary(
            two_legs(
                [Outcome(both, SURVIVED, ("tests/a.py",), (2,)), Outcome(TEXTUAL, KILLED, ("tests/a.py",))],
                [Outcome(both, SURVIVED, ("tests/w.py",)), Outcome(TEXTUAL, KILLED, ("tests/w.py",))],
            )
        )
        self.assertIn(
            "- Line 2: `a` became `b`; passed `tests/a.py` (2 tests that ran the line) on Linux; `tests/w.py` on "
            "Windows\n",
            text,
        )
        self.assertNotIn("killed on one leg alone", text)

    def test_a_linux_survivor_the_windows_leg_left_not_run_leaves_the_run_unfinished_naming_windows(self) -> None:
        report = two_legs([Outcome(TEXTUAL, SURVIVED, ("t",))], [Outcome(TEXTUAL, NOT_RUN)])
        self.assertEqual(
            "**Unfinished, so not a verdict:** a leg's cap cut the run short: 1 of the 1 mutants of `tools/paths.py` "
            "did not run on Windows. Split the pull request, or move its module-level tables into the functions that "
            "use them, until it finishes.",
            unfinished_line(report),
        )
        self.assertIn("The cap cut `tools/paths.py` short on Windows: 1 mutants did not run.", summary(report))
        # A mutant the Linux leg killed has its verdict, whatever the Windows leg left.
        self.assertIsNone(unfinished_line(two_legs([Outcome(TEXTUAL, KILLED, ("t",))], [Outcome(TEXTUAL, NOT_RUN)])))

    def test_the_windows_leg_runs_only_the_modules_its_rule_selects_and_a_leg_with_none_is_not_needed(self) -> None:
        platform = SelectedModule("tools/paths.py", 1, (TEXTUAL,), (), True)
        plain = SelectedModule("tools/calc.py", 1, (mutant(1),), (), False)
        empty = SelectedModule("tools/empty.py", 1, (), (), True)
        selected = Selected("origin/main", "3.8.0", (platform, plain, empty))
        self.assertEqual([platform, plain], leg_modules(LINUX_LEG, selected))
        self.assertEqual([platform], leg_modules(WINDOWS_LEG, selected))
        self.assertEqual([LINUX_LEG, WINDOWS_LEG], needed_legs(selected))
        self.assertEqual([LINUX_LEG], needed_legs(Selected("origin/main", "3.8.0", (plain, empty))))
        self.assertEqual([], needed_legs(Selected("origin/main", "3.8.0", (empty,))))
        # A module no leg ran is named with no leg.
        report = joined(
            selected, [LegRun(LINUX_LEG, 40, 1.0, 1, {"tools/calc.py": [Outcome(mutant(1), KILLED)]}, {}, [])]
        )
        self.assertEqual(
            [{}, {LINUX_LEG: [Outcome(mutant(1), KILLED)]}, {}], [module.legs for module in report.modules]
        )

    def test_a_selection_and_a_legs_run_read_back_as_they_were_written(self) -> None:
        table = Mutant(PATH_MODULE, 9, "T = 1", "T = 2", "T = 2\n", True, frozenset({12, 14}))
        chosen = SelectedModule(PATH_MODULE, 3, (TEXTUAL, table), (("X_ALLOWED", "tests/validation/x.py"),), True)
        selected = Selected("origin/main", "3.8.0", (chosen, SelectedModule("tools/none.py", 0, (), (), False)))
        leg = LegRun(
            WINDOWS_LEG,
            40,
            12.5,
            4,
            {PATH_MODULE: [Outcome(TEXTUAL, KILLED, ("tests/a.py",), (2,)), Outcome(table, NOT_REACHED)]},
            {"tests/b.py": "failed unmutated with exit code 1"},
            ["tests/c.py"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            mutants, outcomes = Path(temporary) / "mutants.json", Path(temporary) / "outcomes.json"
            mutants.write_text(json.dumps(selection_record(selected)), encoding="utf-8")
            outcomes.write_text(json.dumps(leg_record(leg)), encoding="utf-8")
            self.assertEqual(selected, read_selection(mutants))
            self.assertEqual(leg, read_leg(outcomes, selected, WINDOWS_LEG))
            # A leg's outcomes that do not match the selection's mutants one for one stop the join.
            record = leg_record(leg)
            record["outcomes"][PATH_MODULE].pop()
            outcomes.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaisesRegex(RecordError, "holds 1 outcomes of tools/paths.py, not one for each"):
                read_leg(outcomes, selected, WINDOWS_LEG)

    def test_a_leg_record_that_names_another_leg_or_leaves_out_or_adds_a_module_stops_the_join(self) -> None:
        platform = SelectedModule(PATH_MODULE, 1, (TEXTUAL,), (), True)
        plain = SelectedModule("tools/calc.py", 1, (mutant(1),), (), False)
        selected = Selected("origin/main", "3.8.0", (platform, plain))
        both = {
            PATH_MODULE: [Outcome(TEXTUAL, SURVIVED, ("t",))],
            "tools/calc.py": [Outcome(mutant(1), KILLED, ("t",))],
        }
        windows = {PATH_MODULE: [Outcome(TEXTUAL, KILLED, ("t",))]}
        records = [
            (LINUX_LEG, LegRun(LINUX_LEG, 40, 1.0, 1, {}, {}, []), "holds no outcomes of tools/paths.py, tools/calc"),
            (WINDOWS_LEG, LegRun(WINDOWS_LEG, 40, 1.0, 1, {}, {}, []), "holds no outcomes of tools/paths.py, so"),
            (WINDOWS_LEG, LegRun(LINUX_LEG, 40, 1.0, 1, windows, {}, []), "holds the Linux leg's run, not the Windows"),
            (WINDOWS_LEG, LegRun(WINDOWS_LEG, 40, 1.0, 1, both, {}, []), "tools/calc.py, whose mutants the Windows"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "mutants.json").write_text(json.dumps(selection_record(selected)), encoding="utf-8")
            linux = LegRun(LINUX_LEG, 40, 1.0, 1, both, {}, [])
            good = LegRun(WINDOWS_LEG, 40, 1.0, 1, windows, {}, [])
            for leg, record, message in records:
                with self.subTest(leg=leg, message=message):
                    written = {LINUX_LEG: linux, WINDOWS_LEG: good, leg: record}
                    for name, run in written.items():
                        path = folder / f"outcomes-{name.casefold()}.json"
                        path.write_text(json.dumps(leg_record(run)), encoding="utf-8")
                    with self.assertRaisesRegex(RecordError, message):
                        mutation_gate.join(folder)
            # Both legs' own records join.
            for name, run in ((LINUX_LEG, linux), (WINDOWS_LEG, good)):
                (folder / f"outcomes-{name.casefold()}.json").write_text(json.dumps(leg_record(run)), encoding="utf-8")
            self.assertTrue(mutation_gate.join(folder).finished)

    def test_a_record_field_of_another_kind_stops_the_join_by_name(self) -> None:
        selected = Selected("origin/main", "3.8.0", (SelectedModule(PATH_MODULE, 1, (TEXTUAL,), (), True),))
        leg = LegRun(WINDOWS_LEG, 40, 1.0, 1, {PATH_MODULE: [Outcome(TEXTUAL, KILLED, ("t",), (1,))]}, {}, [])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "record.json"
            selection_changes: list[tuple[str, Callable[[dict[str, Any]], object]]] = [
                ("record.json must be an object with field modules", lambda record: record.pop("modules")),
                (
                    r"record.json.modules\[0\].tables\[0\] must be an array of a table's name",
                    lambda record: record["modules"][0].update(tables=[["X_ALLOWED"]]),
                ),
                (
                    r"record.json.modules\[0\].mutants\[0\].line must be an integer of at least 1",
                    lambda record: record["modules"][0]["mutants"][0].update(line="4"),
                ),
            ]
            for message, change in selection_changes:
                with self.subTest(message=message):
                    record = selection_record(selected)
                    change(record)
                    path.write_text(json.dumps(record), encoding="utf-8")
                    with self.assertRaisesRegex(RecordFieldError, message):
                        read_selection(path)
            leg_changes: list[tuple[str, Callable[[dict[str, Any]], object]]] = [
                (
                    r"record.json.outcomes.tools/paths.py\[0\].status must be one of",
                    lambda record: record["outcomes"][PATH_MODULE][0].update(status="won"),
                ),
                (
                    "record.json.failed_suites.t must be a non-empty string",
                    lambda record: record["failed_suites"].update(t=1),
                ),
            ]
            for message, change in leg_changes:
                with self.subTest(message=message):
                    record = leg_record(leg)
                    change(record)
                    path.write_text(json.dumps(record), encoding="utf-8")
                    with self.assertRaisesRegex(RecordFieldError, message):
                        read_leg(path, selected, WINDOWS_LEG)
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(RecordError, "record.json cannot be read"):
                read_selection(path)


# A selection of one module with one mutant that both legs run, and the Windows leg's run of it, as the steps write.
READ_MUTANT = Mutant(PATH_MODULE, 4, "a", "b", "b\n", False, frozenset({5}))
READ_SELECTED = Selected(
    "origin/main", "3.8.0", (SelectedModule(PATH_MODULE, 1, (READ_MUTANT,), (("X_ALLOWED", "tests/x.py"),), True),)
)
READ_LEG = LegRun(WINDOWS_LEG, 40, 1.5, 2, {PATH_MODULE: [Outcome(READ_MUTANT, KILLED, ("t",), (1,))]}, {"u": "v"}, [])


def set_field(record: dict[str, Any], path: Sequence[str | int], value: object) -> dict[str, Any]:
    """The record with the field at `path`, a key or an index at each step, set to `value`."""
    node: Any = record
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value
    return record


MUTANT_AT = ("modules", 0, "mutants", 0)
OUTCOME_AT = ("outcomes", PATH_MODULE, 0)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("base",), 1, "record.json.base must be a non-empty string, found integer 1"),
        (("mutmut",), 1, "record.json.mutmut must be a non-empty string, found integer 1"),
        (("modules", 0), 1, "record.json.modules[0] must be an object, found integer 1"),
        (("modules", 0, "module"), 1, "record.json.modules[0].module must be a non-empty string, found integer 1"),
        (
            ("modules", 0, "lines"),
            -1,
            "record.json.modules[0].lines must be an integer of at least 0, found integer -1",
        ),
        (("modules", 0, "windows"), 1, "record.json.modules[0].windows must be a boolean, found integer 1"),
        (("modules", 0, "tables", 0), 1, "record.json.modules[0].tables[0] must be an array, found integer 1"),
        (
            ("modules", 0, "tables", 0),
            ["X_ALLOWED"],
            "record.json.modules[0].tables[0] must be an array of a table's name and its policy, found an array of 1 "
            "items",
        ),
        (MUTANT_AT, 1, "record.json.modules[0].mutants[0] must be an object, found integer 1"),
        (
            (*MUTANT_AT, "readers"),
            1,
            "record.json.modules[0].mutants[0].readers must be an array or null, found integer 1",
        ),
        (
            (*MUTANT_AT, "readers"),
            [0],
            "record.json.modules[0].mutants[0].readers[0] must be an integer of at least 1, found integer 0",
        ),
        ((*MUTANT_AT, "before"), 1, "record.json.modules[0].mutants[0].before must be a string, found integer 1"),
        ((*MUTANT_AT, "after"), 1, "record.json.modules[0].mutants[0].after must be a string, found integer 1"),
        ((*MUTANT_AT, "source"), 1, "record.json.modules[0].mutants[0].source must be a string, found integer 1"),
        (
            (*MUTANT_AT, "at_import"),
            1,
            "record.json.modules[0].mutants[0].at_import must be a boolean, found integer 1",
        ),
    ],
)
def test_a_selection_field_of_another_kind_is_refused_by_its_path(
    path: Sequence[str | int], value: object, message: str
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        record = Path(temporary) / "record.json"
        record.write_text(json.dumps(set_field(selection_record(READ_SELECTED), path, value)), encoding="utf-8")
        with pytest.raises(RecordFieldError) as refused:
            read_selection(record)
    assert str(refused.value) == message


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("leg",), "Mac", 'record.json.leg must be one of "Linux", "Windows", found string "Mac"'),
        (("minutes",), 0, "record.json.minutes must be an integer of at least 1, found integer 0"),
        (("seconds",), "1", 'record.json.seconds must be a finite number, found string "1"'),
        (("workers",), -1, "record.json.workers must be an integer of at least 0, found integer -1"),
        (("failed_suites",), 1, "record.json.failed_suites must be an object, found integer 1"),
        (("unmeasured_suites",), 1, "record.json.unmeasured_suites must be an array, found integer 1"),
        (("outcomes",), 1, "record.json.outcomes must be an object, found integer 1"),
        (("outcomes", PATH_MODULE), 1, "record.json.outcomes.tools/paths.py must be an array, found integer 1"),
        (OUTCOME_AT, 1, "record.json.outcomes.tools/paths.py[0] must be an object, found integer 1"),
        ((*OUTCOME_AT, "suites"), 1, "record.json.outcomes.tools/paths.py[0].suites must be an array, found integer 1"),
        (
            (*OUTCOME_AT, "tests"),
            [-1],
            "record.json.outcomes.tools/paths.py[0].tests[0] must be an integer of at least 0, found integer -1",
        ),
    ],
)
def test_a_leg_field_of_another_kind_is_refused_by_its_path(
    path: Sequence[str | int], value: object, message: str
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        record = Path(temporary) / "record.json"
        record.write_text(json.dumps(set_field(leg_record(READ_LEG), path, value)), encoding="utf-8")
        with pytest.raises(RecordFieldError) as refused:
            read_leg(record, READ_SELECTED, WINDOWS_LEG)
    assert str(refused.value) == message


def test_each_records_least_values_read_back() -> None:
    # An empty mutation text, a module of no changed line, a first reader line, a leg with no worker under a one-minute
    # cap, and a suite of which no test is counted are each a value the steps can write.
    mutant = Mutant(PATH_MODULE, 1, "", "", "", True, frozenset({1}))
    selected = Selected("origin/main", "3.8.0", (SelectedModule(PATH_MODULE, 0, (mutant,), (), True),))
    leg = LegRun(WINDOWS_LEG, 1, 0.0, 0, {PATH_MODULE: [Outcome(mutant, KILLED, ("t",), (0,))]}, {}, [])
    with tempfile.TemporaryDirectory() as temporary:
        mutants, outcomes = Path(temporary) / "mutants.json", Path(temporary) / "outcomes.json"
        mutants.write_text(json.dumps(selection_record(selected)), encoding="utf-8")
        outcomes.write_text(json.dumps(leg_record(leg)), encoding="utf-8")
        assert read_selection(mutants) == selected
        assert read_leg(outcomes, selected, WINDOWS_LEG) == leg


@pytest.mark.parametrize(
    ("status", "suites", "tests", "message"),
    [
        (KILLED, [], [], "suites must be the suites a killed mutant ran, at least one, found an empty array"),
        (TIMED_OUT, [], [], "suites must be the suites a timed out mutant ran, at least one, found an empty array"),
        (SURVIVED, [], [], "suites must be the suites a survived mutant ran, at least one, found an empty array"),
        (ERRORED, [], [], "suites must be the suites an errored mutant ran, at least one, found an empty array"),
        (NOT_REACHED, ["t"], [1], "suites must be empty for a mutant no suite reached, found an array of 1 items"),
        (KILLED, ["t"], [1, 2], "tests must be an array of at most 1 items, one for each suite, found 2 items"),
        (NOT_RUN, [], [1], "tests must be an array of at most 0 items, one for each suite, found 1 items"),
    ],
)
def test_an_outcome_whose_suites_do_not_fit_its_status_is_refused_by_its_path(
    status: str, suites: list[str], tests: list[int], message: str
) -> None:
    record = leg_record(READ_LEG)
    record["outcomes"][PATH_MODULE][0] = {"status": status, "suites": suites, "tests": tests}
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "record.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        with pytest.raises(RecordFieldError) as refused:
            read_leg(path, READ_SELECTED, WINDOWS_LEG)
    assert str(refused.value) == f"record.json.outcomes.tools/paths.py[0].{message}"


@pytest.mark.parametrize(
    "outcome",
    [
        Outcome(READ_MUTANT, NOT_REACHED),
        Outcome(READ_MUTANT, NOT_RUN),
        Outcome(READ_MUTANT, NOT_RUN, ("t",), (1,)),
        Outcome(READ_MUTANT, TIMED_OUT, ("t",), (1,)),
        Outcome(READ_MUTANT, SURVIVED, ("t", "u"), (1, 0)),
        Outcome(READ_MUTANT, KILLED, ("t", "u")),
        Outcome(READ_MUTANT, ERRORED, ("t",), (1,)),
    ],
)
def test_each_outcome_shape_a_leg_writes_reads_back(outcome: Outcome) -> None:
    leg = LegRun(WINDOWS_LEG, 40, 1.5, 2, {PATH_MODULE: [outcome]}, {}, [])
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "record.json"
        path.write_text(json.dumps(leg_record(leg)), encoding="utf-8")
        assert read_leg(path, READ_SELECTED, WINDOWS_LEG) == leg


def write_join(folder: Path, selection: dict[str, Any], legs: dict[str, dict[str, Any]]) -> None:
    """The selection and each leg's record as the steps hand them to the join."""
    (folder / "mutants.json").write_text(json.dumps(selection), encoding="utf-8")
    for name, record in legs.items():
        (folder / f"outcomes-{name.casefold()}.json").write_text(json.dumps(record), encoding="utf-8")


def test_the_join_refuses_a_kill_no_suite_made_on_both_legs_or_beside_a_survivor() -> None:
    selected = Selected("origin/main", "3.8.0", (SelectedModule(PATH_MODULE, 1, (READ_MUTANT,), (), True),))
    suiteless = {"status": KILLED, "suites": [], "tests": []}
    survivor = LegRun(LINUX_LEG, 40, 1.0, 1, {PATH_MODULE: [Outcome(READ_MUTANT, SURVIVED, ("t",), (1,))]}, {}, [])
    killed = LegRun(WINDOWS_LEG, 40, 1.0, 1, {PATH_MODULE: [Outcome(READ_MUTANT, KILLED, ("t",), (1,))]}, {}, [])
    # Both legs claim a kill no suite made, or Windows does beside a Linux survivor, whose summary names the killer.
    for emptied in ((LINUX_LEG, WINDOWS_LEG), (WINDOWS_LEG,)):
        records = {LINUX_LEG: leg_record(survivor), WINDOWS_LEG: leg_record(killed)}
        for leg in emptied:
            records[leg]["outcomes"][PATH_MODULE][0] = dict(suiteless)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            write_join(folder, selection_record(selected), records)
            with pytest.raises(RecordError, match=r"^outcomes-\w+\.json\.outcomes\.tools/paths\.py\[0\]\.suites must"):
                mutation_gate.join(folder)
    # The same legs with each kill naming its suite join.
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        write_join(
            folder, selection_record(selected), {LINUX_LEG: leg_record(survivor), WINDOWS_LEG: leg_record(killed)}
        )
        assert mutation_gate.join(folder).finished


@pytest.mark.parametrize("status", [KILLED, TIMED_OUT, SURVIVED, ERRORED, NOT_RUN])
@pytest.mark.parametrize("listed", ["failed_suites", "unmeasured_suites"])
def test_the_join_refuses_an_outcome_naming_a_suite_its_leg_lists_as_failed_or_unmeasured(
    status: str, listed: str
) -> None:
    """A suite that failed unmutated or whose reach the cap left unmeasured ran against no mutant, so an outcome naming
    one, killed above all, is not as try_mutant writes it, and the join stops naming the file and the field."""
    selected = Selected("origin/main", "3.8.0", (SelectedModule(PATH_MODULE, 1, (READ_MUTANT,), (), True),))
    outcome = Outcome(READ_MUTANT, status, ("tests/ran.py", "tests/judged-nothing.py"), (1, 0))
    leg = LegRun(WINDOWS_LEG, 40, 1.0, 1, {PATH_MODULE: [outcome]}, {}, [])
    linux = LegRun(LINUX_LEG, 40, 1.0, 1, {PATH_MODULE: [Outcome(READ_MUTANT, SURVIVED, ("t",), (1,))]}, {}, [])
    record = leg_record(leg)
    record[listed] = (
        {"tests/judged-nothing.py": "failed unmutated"} if listed == "failed_suites" else ["tests/judged-nothing.py"]
    )
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        write_join(folder, selection_record(selected), {LINUX_LEG: leg_record(linux), WINDOWS_LEG: record})
        with pytest.raises(RecordFieldError) as refused:
            mutation_gate.join(folder)
        assert str(refused.value) == (
            f"outcomes-windows.json.outcomes.tools/paths.py[0].suites must be suites the leg ran, none of them in its "
            f"{listed}, found tests/judged-nothing.py, which outcomes-windows.json.{listed} lists"
        )
        # The same record with the suite listed apart from the outcome's joins.
        record[listed] = {"tests/other.py": "failed unmutated"} if listed == "failed_suites" else ["tests/other.py"]
        write_join(folder, selection_record(selected), {LINUX_LEG: leg_record(linux), WINDOWS_LEG: record})
        mutation_gate.join(folder)


def test_the_join_refuses_a_selection_that_lists_a_module_twice() -> None:
    first, second = mutant(1), mutant(2)
    twice = Selected(
        "origin/main",
        "3.8.0",
        (
            SelectedModule("tools/calc.py", 1, (first,), (), False),
            SelectedModule("tools/calc.py", 1, (second,), (), False),
        ),
    )
    linux = LegRun(LINUX_LEG, 40, 1.0, 1, {"tools/calc.py": [Outcome(second, KILLED, ("t",), (1,))]}, {}, [])
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        write_join(folder, selection_record(twice), {LINUX_LEG: leg_record(linux)})
        with pytest.raises(RecordFieldError) as refused:
            mutation_gate.join(folder)
        assert str(refused.value) == (
            "mutants.json.modules[1].module must be a module no earlier entry names, found tools/calc.py"
        )
        # Each module once joins.
        once = Selected("origin/main", "3.8.0", (SelectedModule("tools/calc.py", 1, (second,), (), False),))
        write_join(folder, selection_record(once), {LINUX_LEG: leg_record(linux)})
        assert mutation_gate.join(folder).finished


CALC = "def add(a, b):\n    return a + b\n\n\ndef double(a):\n    return a * 2\n"
# The leg every whole run in one process names: this platform's.
HOST = platform_support.PLATFORM_NAME
SUITE = """import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import calc


class CalcTests(unittest.TestCase):
    def test_add(self) -> None:
        self.assertEqual(5, calc.add(2, 3))
"""


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def fixture_repository(root: Path) -> None:
    (root / "tools").mkdir(parents=True)
    (root / "tests" / "tools").mkdir(parents=True)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    git(root, "init", "--quiet", "--initial-branch=main")
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "base")
    git(root, "switch", "--quiet", "-c", "feature")
    (root / "tools" / "calc.py").write_text(CALC, encoding="utf-8")
    (root / "tests" / "tools" / "test_calc.py").write_text(SUITE, encoding="utf-8")
    (root / "docs.md").write_text("not a module\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "change")


def stub_generator(module: str, source: str, lines: frozenset[int]) -> list[str]:
    """The fixture module's two mutants, and the canary's, which every run checks first, each made only when its line
    is one asked for, as mutmut's line filter makes them."""
    made = []
    for before, after in (("a + b", "a - b"), ("a * 2", "a * 3"), ("a + 1", "a - 1")):
        if before in source and source[: source.index(before)].count("\n") + 1 in lines:
            made.append(source.replace(before, after))
    return made


def measured(reach: object) -> None:
    """A measurement canary that passes, for a run whose reach is stubbed: coverage.py is not a development
    dependency."""


def stub_reach(tree: Path, suite: str, modules: object, timeout: float) -> Reach:
    lines = frozenset(range(1, CALC.count("\n") + 1))
    return Reach(1.0, {"tools/calc.py": lines})


def refuse_reach(tree: Path, suite: str, modules: object, timeout: float) -> Reach:
    raise AssertionError("no suite's reach is measured when no changed line yields a mutant")


def change_calc(root: Path, text: str) -> None:
    """Change the fixture's calc.py to `text` in a new commit."""
    (root / "tools" / "calc.py").write_text(text, encoding="utf-8")
    git(root, "commit", "--quiet", "-am", "later change")


class WholeRunFixtures(unittest.TestCase):
    """A run over a fixture repository: real worktrees and the real suite runner, with the generator and the reach
    stubbed, since mutmut and coverage.py are not development dependencies."""

    def test_a_pull_requests_module_is_mutated_in_worktrees_and_the_survivor_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository with spaces"
            fixture_repository(root)
            gate = Gate(root, "main", 5, 2, GitClient(), stub_generator, stub_reach, measure=measured)
            result = gate.report()
            text = result.text
            worktrees = git(root, "worktree", "list", "--porcelain")
            status = git(root, "status", "--porcelain")
        # A finished run is a verdict: its summary opens on its heading and says nothing of a cut.
        self.assertTrue(result.finished)
        self.assertEqual("## Mutation gate", text.splitlines()[0])
        self.assertNotIn("Unfinished", text)
        self.assertNotIn("cap cut", text)
        self.assertIn(f"| `tools/calc.py` | {HOST} | 6 | 2 | 1 | 0 | 1 | 0 | 0 | 0 | 50% (1 of 2) |", text)
        self.assertIn(f"- On {HOST}, 2 workers ran for 0 of its 5 minutes; a mutant ran", text)
        self.assertIn(
            "- Line 6: `return a * 2` became `return a * 3`; passed `tests/tools/test_calc.py`",
            text,
        )
        # The worktrees are gone and the checkout untouched.
        self.assertEqual(1, worktrees.count("worktree "))
        self.assertEqual("", status)

    def test_a_run_the_cap_cuts_short_is_reported_unfinished(self) -> None:
        times = itertools.chain([0.0], itertools.repeat(10_000.0))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            fixture_repository(root)
            result = Gate(
                root, "main", 5, 2, GitClient(), stub_generator, stub_reach, clock=lambda: next(times), measure=measured
            ).report()
        self.assertFalse(result.finished)
        self.assertTrue(result.text.startswith("**Unfinished, so not a verdict:** a leg's cap cut the run short: 2"))
        # Every mutant was left not run, so the unmeasured suite opens none beyond them, and is named apart.
        self.assertNotIn("was not measured", result.text.splitlines()[0])
        self.assertIn(f"The cap left these suites' reach unmeasured on {HOST}", result.text)

    def test_a_changed_sanction_table_is_named_with_its_policy_and_its_modules_other_lines_still_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            fixture_repository(root)
            (root / "tests" / "validation").mkdir()
            (root / "tests" / "validation" / "policy.py").write_text(POLICY, encoding="utf-8")
            git(root, "add", ".")
            git(root, "commit", "--quiet", "-m", "policy")
            git(root, "switch", "--quiet", "-c", "table", "feature")
            change_calc(root, CALC.replace("return a * 2", "return a * 2 + 0") + 'FSOPS_ALLOWED = {"add": "a + b"}\n')

            def generate(module: str, source: str, lines: frozenset[int]) -> list[str]:
                """Each line's mutant, the table's entry among them, as mutmut would make it."""
                if module == "canary.py":
                    return stub_generator(module, source, lines)
                return [source.replace("a * 2 + 0", "a * 3 + 0"), source.replace('"a + b"', '"a - b"')]

            text = Gate(root, "feature", 5, 2, GitClient(), generate, stub_reach, measure=measured).report().text
        self.assertIn(f"| `tools/calc.py` | {HOST} | 2 | 1 | 0 | 0 | 1 | 0 | 0 | 0 | 0% (0 of 1) |", text)
        self.assertIn("- Line 6: `return a * 2 + 0` became `return a * 3 + 0`", text)
        self.assertIn("its sanction tables made no mutant", text)
        self.assertIn("`FSOPS_ALLOWED` (`tests/validation/policy.py`)", text)

    def test_a_mutant_on_a_line_the_pull_request_did_not_change_is_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            fixture_repository(root)
            git(root, "switch", "--quiet", "-c", "only-double", "feature")
            change_calc(root, CALC.replace("return a * 2", "return a * 2 + 0"))
            asked: dict[str, frozenset[int]] = {}

            def generate(module: str, source: str, lines: frozenset[int]) -> list[str]:
                """Both of the module's mutants whatever lines are asked for, and the canary's as mutmut makes it."""
                if module == "canary.py":
                    return stub_generator(module, source, lines)
                asked[module] = lines
                return [source.replace("a + b", "a - b"), source.replace("a * 2", "a * 3")]

            text = Gate(root, "feature", 5, 2, GitClient(), generate, stub_reach, measure=measured).report().text
        self.assertEqual({"tools/calc.py": frozenset({6})}, asked)
        # The suite kills line 2's mutant, so it would count as killed had it run: only line 6's ran, and survived.
        self.assertIn(f"| `tools/calc.py` | {HOST} | 1 | 1 | 0 | 0 | 1 | 0 | 0 | 0 | 0% (0 of 1) |", text)
        self.assertIn("- Line 6: `return a * 2 + 0` became `return a * 3 + 0`", text)
        self.assertNotIn("Line 2", text)

    def test_a_mutant_on_a_continuation_line_runs_against_the_suite_that_executed_its_statement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            fixture_repository(root)
            change_calc(root, MULTILINE_CALC)
            (root / "tests" / "tools" / "test_calc.py").write_text(
                SUITE.replace("calc.add(2, 3)", "calc.scaled(1)").replace("5,", "3,"), encoding="utf-8"
            )
            git(root, "commit", "--quiet", "-am", "test scaled")
            git(root, "switch", "--quiet", "-c", "continuation")
            change_calc(root, MULTILINE_CALC.replace("a * 2,", "a * 2 + 0,"))

            def generate(module: str, source: str, lines: frozenset[int]) -> list[str]:
                if module == "canary.py":
                    return stub_generator(module, source, lines)
                return [source.replace("a * 2 + 0", "a * 3 + 0")] if 8 in lines else []

            def recorded_reach(tree: Path, suite: str, modules: Sequence[str], timeout: float) -> Reach:
                return Reach(1.0, executed_lines(MULTILINE_REPORT, tree, modules))

            text = Gate(root, "feature", 5, 2, GitClient(), generate, recorded_reach, measure=measured).report().text
        # Line 8 is a continuation of the call on line 6, the line coverage.py recorded, so the suite ran and killed it.
        self.assertIn(f"| `tools/calc.py` | {HOST} | 1 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 100% (1 of 1) |", text)

    def test_a_mutant_runs_the_tests_that_ran_its_line_or_whose_call_started_the_process_that_did(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            fixture_repository(root)
            context_tree(root)
            git(root, "add", ".")
            git(root, "commit", "--quiet", "-m", "contexts")
            git(root, "switch", "--quiet", "-c", "per-test")
            change_calc(
                root, CONTEXT_CALC.replace("        a,\n    )", "        a + 0,\n    )").replace("a * a", "a * a + 0")
            )
            calls: list[tuple[str, tuple[str, ...], str]] = []

            def generate(module: str, source: str, lines: frozenset[int]) -> list[str]:
                if module == "canary.py":
                    return stub_generator(module, source, lines)
                return [source.replace("a + 0,", "a + 1,"), source.replace("a * a + 0", "a * a + 1")]

            def recorded_reach(tree: Path, suite: str, modules: Sequence[str], timeout: float) -> Reach:
                lines = executed_lines(CONTEXT_REPORT, tree, modules)
                return Reach(1.0, lines, executed_tests(CONTEXT_REPORT, tree, modules, suite, CONTEXT_COLLECTED))

            def run(tree: Path, suite: str, timeout: float, tests: Sequence[str]) -> str:
                result = run_suite(tree, suite, timeout, tests)
                calls.append((suite, tuple(tests), result))
                return result

            gate = Gate(root, "feature", 5, 1, GitClient(), generate, recorded_reach, run, measure=measured)
            with mock.patch.object(
                mutation_gate, "reached_suites", return_value=[root / "tests" / "tools" / "test_calc.py"]
            ):
                text = gate.report().text
        # Line 11's mutant ran test_double() alone, which killed it; line 20's ran only in the process
        # test_square_in_a_process() starts, so it ran that test alone, which killed it there.
        self.assertEqual(
            [
                (CONTEXT_SUITE_PATH, ("tests/tools/test_calc.py::CalcTests::test_double",), KILLED),
                (CONTEXT_SUITE_PATH, (SQUARE_TEST,), KILLED),
            ],
            calls,
        )
        self.assertIn(f"| `tools/calc.py` | {HOST} | 2 | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 100% (2 of 2) |", text)

    def test_a_changed_module_with_no_mutant_is_named_and_no_suite_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            fixture_repository(root)
            git(root, "switch", "--quiet", "-c", "comment", "feature")
            change_calc(root, CALC + "# A comment, which mutmut does not mutate.\n")
            text = Gate(root, "feature", 5, 2, GitClient(), stub_generator, refuse_reach).report().text
        self.assertIn("| `tools/calc.py` | none | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | none tested |", text)
        self.assertIn(
            "`tools/calc.py`: mutmut made no mutant on the 1 line this pull request changed, so none ran.", text
        )

    def test_a_change_with_no_module_writes_one_line_and_makes_no_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            fixture_repository(root)
            git(root, "switch", "--quiet", "-c", "documentation", "main")
            (root / "docs.md").write_text("only documentation\n", encoding="utf-8")
            git(root, "add", ".")
            git(root, "commit", "--quiet", "-m", "docs")

            def refuse(module: str, source: str, lines: frozenset[int]) -> list[str]:
                raise AssertionError("no mutant is made when no module changed")

            text = Gate(root, "main", 5, 2, GitClient(), refuse, stub_reach).report().text
            worktrees = git(root, "worktree", "list", "--porcelain")
        self.assertEqual(no_module_line("main"), text)
        self.assertEqual(1, worktrees.count("worktree "))

    def test_a_generator_that_fails_its_canary_stops_the_run_before_any_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            fixture_repository(root)
            gate = Gate(root, "main", 5, 2, GitClient(), lambda module, source, lines: [], stub_reach)
            with self.assertRaisesRegex(RuntimeError, "no longer turns"):
                gate.report()
            self.assertEqual(1, git(root, "worktree", "list", "--porcelain").count("worktree "))

    @staticmethod
    def canary_aware(failing: bool, asked: list[str]) -> ReachRunner:
        """A reach runner that stubs every suite's reach and answers the measurement canary's suite as coverage.py
        would with no .pth file when `failing`, and as it does with one otherwise."""
        canary = frozenset({"test_canary.py::test_canary"})

        def reach(tree: Path, suite: str, modules: Sequence[str], timeout: float) -> Reach | str:
            asked.append(suite)
            if suite != "test_canary.py":
                return stub_reach(tree, suite, modules, timeout)
            if failing:
                return "failed in `coverage combine` with exit code 1: `No data to combine`"
            return Reach(1.0, {"canary.py": frozenset({5, 9})}, {"canary.py": {5: canary, 9: canary}})

        return reach

    def test_a_default_gate_measures_its_reach_canary_first_and_stops_when_it_fails(self) -> None:
        for failing in (True, False):
            asked: list[str] = []
            with self.subTest(failing=failing), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "repository"
                fixture_repository(root)
                # No `measure` is given: the gate's own canary runs.
                gate = Gate(root, "main", 5, 2, GitClient(), stub_generator, self.canary_aware(failing, asked))
                if failing:
                    with self.assertRaises(RuntimeError) as raised:
                        gate.report()
                    self.assertEqual(
                        "the reach run's canary suite failed in `coverage combine` with exit code 1: `No data to "
                        "combine`, so no suite's reach can be measured",
                        str(raised.exception),
                    )
                    self.assertEqual(["test_canary.py"], asked)
                else:
                    self.assertTrue(gate.report().finished)
                    self.assertEqual(["test_canary.py", "tests/tools/test_calc.py"], asked)

    def test_a_reach_that_fails_its_measurement_canary_stops_the_run_before_any_suite(self) -> None:
        checked: list[object] = []

        def unmeasured(reach: object) -> str:
            checked.append(reach)
            return "the reach run no longer names the line a process its test started ran with the canary suite's test"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            fixture_repository(root)
            gate = Gate(root, "main", 5, 2, GitClient(), stub_generator, refuse_reach, measure=unmeasured)
            with self.assertRaisesRegex(RuntimeError, "a process its test started"):
                gate.report()
            self.assertEqual(1, git(root, "worktree", "list", "--porcelain").count("worktree "))
        # The canary checks the run's own reach runner, which measures no suite once it fails.
        self.assertEqual([refuse_reach], checked)


class SplitRunFixtures(unittest.TestCase):
    """A run split across jobs as the workflow splits it: the select step, a Linux leg and a Windows leg each in its
    own run of the gate, and the join, over a fixture repository whose module imports a platform module."""

    def legs(self, root: Path, folder: Path, windows: SuiteRunner) -> None:
        """Run each leg as its own job would: the Linux leg with the real suite runner, the Windows leg with
        `windows`."""
        for leg, run in ((LINUX_LEG, run_suite), (WINDOWS_LEG, windows)):
            gate = Gate(root, "main", 5, 2, GitClient(), stub_generator, stub_reach, run, measure=measured)
            with mock.patch.object(platform_support, "PLATFORM_NAME", leg):
                found = gate.leg(read_selection(folder / "mutants.json"))
            (folder / f"outcomes-{leg.casefold()}.json").write_text(json.dumps(leg_record(found)), encoding="utf-8")

    def test_a_mutant_that_survives_the_linux_leg_and_the_windows_leg_kills_reads_as_killed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            fixture_repository(root)
            change_calc(root, CALC + "\n\ndef probe():\n    import device_names\n")
            # A folder whose parent does not exist yet, as the workflow's does not.
            folder = Path(temporary) / "handed" / "on"
            outputs = Path(temporary) / "outputs.txt"
            gate = Gate(root, "main", 5, 2, GitClient(), stub_generator, refuse_reach)
            with mock.patch("sys.stdout", io.StringIO()) as printed:
                mutation_gate.select_step(gate, folder, {"GITHUB_OUTPUT": str(outputs)})
            self.assertEqual("tools/calc.py: 2 mutants, run on Linux, Windows\n", printed.getvalue())
            asked: list[str] = []

            def windows(tree: Path, suite: str, timeout: float, tests: Sequence[str]) -> str:
                asked.append(suite)
                return KILLED

            self.legs(root, folder, windows)
            result = mutation_gate.join(folder)
            step_outputs = outputs.read_text(encoding="utf-8")
        self.assertEqual("linux=true\nwindows=true\n", step_outputs)
        self.assertTrue(result.finished)
        self.assertIn(
            "| `tools/calc.py` | Linux, Windows | 10 | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 100% (2 of 2) |", result.text
        )
        self.assertIn("- Modules changed since `main`: 1; mutants made by mutmut (a test's generator) on", result.text)
        self.assertIn(
            "- Line 6: `return a * 2` became `return a * 3`; killed on Windows by `tests/tools/test_calc.py`, "
            "survived on Linux\n",
            result.text,
        )
        self.assertNotIn("surviving", result.text)
        self.assertEqual(["tests/tools/test_calc.py"] * 2, asked)

    def test_a_module_off_the_windows_rule_runs_on_linux_alone_and_needs_no_windows_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            fixture_repository(root)
            folder = Path(temporary) / "handed on"
            outputs = Path(temporary) / "outputs.txt"
            gate = Gate(root, "main", 5, 2, GitClient(), stub_generator, refuse_reach)
            # A rerun of the step writes into the folder it already made.
            for _ in range(2):
                outputs.unlink(missing_ok=True)
                mutation_gate.select_step(gate, folder, {"GITHUB_OUTPUT": str(outputs)})
            linux = Gate(root, "main", 5, 2, GitClient(), stub_generator, stub_reach, measure=measured)
            with mock.patch.object(platform_support, "PLATFORM_NAME", LINUX_LEG):
                found = linux.leg(read_selection(folder / "mutants.json"))
            with mock.patch.object(platform_support, "PLATFORM_NAME", WINDOWS_LEG):
                nothing = linux.leg(read_selection(folder / "mutants.json"))
            (folder / "outcomes-linux.json").write_text(json.dumps(leg_record(found)), encoding="utf-8")
            text = mutation_gate.join(folder).text
            step_outputs = outputs.read_text(encoding="utf-8")
        self.assertEqual("linux=true\nwindows=false\n", step_outputs)
        self.assertEqual(LegRun(WINDOWS_LEG, 5, 0.0, 0, {}, {}, []), nothing)
        self.assertIn("| `tools/calc.py` | Linux | 6 | 2 | 1 | 0 | 1 | 0 | 0 | 0 | 50% (1 of 2) |", text)

    def test_the_select_step_selects_nothing_without_a_module_and_stops_on_a_failed_generator_canary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            fixture_repository(root)
            with self.assertRaisesRegex(RuntimeError, "no longer turns"):
                Gate(root, "main", 5, 2, GitClient(), lambda module, source, lines: [], refuse_reach).selection()
            git(root, "switch", "--quiet", "-c", "documentation", "main")
            (root / "docs.md").write_text("only documentation\n", encoding="utf-8")
            git(root, "add", ".")
            git(root, "commit", "--quiet", "-m", "docs")
            selected = Gate(root, "main", 5, 2, GitClient(), stub_generator, refuse_reach).selection()
        self.assertEqual(Selected("main", "(a test's generator)", ()), selected)

    def test_the_mutmut_release_is_named_only_when_mutmut_makes_the_mutants(self) -> None:
        git_client = mock.create_autospec(GitClient, instance=True)
        with mock.patch("importlib.metadata.version", return_value="9.9.9") as version:
            self.assertEqual("9.9.9", Gate(Path(), "main", 1, 1, git_client).version())
            self.assertEqual("(a test's generator)", Gate(Path(), "main", 1, 1, git_client, line_generator).version())
        version.assert_called_once_with("mutmut")

    def test_a_missing_selection_or_a_needed_legs_missing_outcomes_stops_the_join(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with self.assertRaisesRegex(RuntimeError, "the select job wrote no mutants.json"):
                mutation_gate.join(folder)
            platform = SelectedModule(PATH_MODULE, 1, (TEXTUAL,), (), True)
            (folder / "mutants.json").write_text(
                json.dumps(selection_record(Selected("origin/main", "3.8.0", (platform,)))), encoding="utf-8"
            )
            linux = LegRun(LINUX_LEG, 40, 1.0, 1, {PATH_MODULE: [Outcome(TEXTUAL, SURVIVED, ("t",))]}, {}, [])
            (folder / "outcomes-linux.json").write_text(json.dumps(leg_record(linux)), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "the Windows leg wrote no outcomes-windows.json"):
                mutation_gate.join(folder)
            (folder / "mutants.json").write_text(
                json.dumps(selection_record(Selected("origin/main", "3.8.0", ()))), encoding="utf-8"
            )
            self.assertEqual(no_module_line("origin/main"), mutation_gate.join(folder).text)

    def test_the_steps_write_their_files_and_the_join_fails_an_unfinished_run(self) -> None:
        platform = SelectedModule(PATH_MODULE, 1, (TEXTUAL,), (), True)
        plain = SelectedModule("tools/calc.py", 1, (mutant(1),), (), False)
        empty = SelectedModule("tools/none.py", 1, (), (), False)
        selected = Selected("origin/main", "3.8.0", (platform, plain, empty))
        unfinished = LegRun(WINDOWS_LEG, 40, 1.0, 1, {PATH_MODULE: [Outcome(TEXTUAL, NOT_RUN)]}, {}, [])
        log = FlushRecorder()
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "folder"
            with mock.patch.object(Gate, "selection", return_value=selected), mock.patch("sys.stdout", log):
                self.assertEqual(0, mutation_gate.main(["select", str(folder)], {}))
            # Each module's line names only the legs that run it, and reaches the log as it is written.
            lines = [
                "tools/paths.py: 1 mutants, run on Linux, Windows\n",
                "tools/calc.py: 1 mutants, run on Linux\n",
                "tools/none.py: 0 mutants, run on no leg\n",
            ]
            self.assertEqual(["".join(lines[: index + 1]) for index in range(len(lines))], log.flushed)
            self.assertEqual(selected, read_selection(folder / "mutants.json"))
            with (
                mock.patch.object(Gate, "leg", return_value=unfinished) as leg,
                mock.patch("sys.stdout", io.StringIO()),
            ):
                self.assertEqual(0, mutation_gate.main(["leg", str(folder)], {}))
            leg.assert_called_once_with(selected)
            self.assertEqual(unfinished, read_leg(folder / "outcomes-windows.json", selected, WINDOWS_LEG))
            linux_outcomes = {
                PATH_MODULE: [Outcome(TEXTUAL, SURVIVED, ("t",))],
                "tools/calc.py": [Outcome(mutant(1), KILLED, ("t",))],
            }
            linux = LegRun(LINUX_LEG, 40, 1.0, 1, linux_outcomes, {}, [])
            (folder / "outcomes-linux.json").write_text(json.dumps(leg_record(linux)), encoding="utf-8")
            summary_file = Path(temporary) / "summary.md"
            with mock.patch("sys.stdout", io.StringIO()) as printed:
                code = mutation_gate.main(["join", str(folder)], {"GITHUB_STEP_SUMMARY": str(summary_file)})
            written = summary_file.read_text(encoding="utf-8")
        self.assertEqual(1, code)
        self.assertTrue(written.startswith("**Unfinished, so not a verdict:**"))
        self.assertIn("::error title=Mutation gate unfinished::", printed.getvalue())


class QueueFixtures(unittest.TestCase):
    """Two changed modules sharing one queue, with a suite run that takes the whole cap."""

    def test_a_later_module_is_not_starved_when_an_earlier_modules_mutants_take_the_cap(self) -> None:
        source = "x = 1\ny = 2\nz = 3\n"
        modules = ["tools/a.py", "tools/b.py"]
        now = [0.0]
        both_started = threading.Barrier(2, timeout=30)

        def run(tree: Path, suite: str, timeout: float, tests: Sequence[str]) -> str:
            # Both workers' first mutants start before either finishes, and the first to finish ends the cap.
            both_started.wait()
            now[0] = 10_000.0
            return PASSED

        def reach(tree: Path, suite: str, measured: object, timeout: float) -> Reach:
            return Reach(1.0, dict.fromkeys(modules, frozenset({1, 2, 3})))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trees = [root / "tree-1", root / "tree-2"]
            for tree in trees:
                (tree / "tools").mkdir(parents=True)
                for module in modules:
                    (tree / module).write_text(source, encoding="utf-8")
            git = mock.create_autospec(GitClient, instance=True)
            git.output.return_value = "@@ -0,0 +1,3 @@\n+x = 1\n+y = 2\n+z = 3\n"
            gate = Gate(root, "main", 1, 2, git, line_generator, reach, run, lambda: now[0], measured)
            with mock.patch.object(mutation_gate, "reached_suites", return_value=[root / "tests" / "test_x.py"]):
                report = gate.mutate(trees, modules, 0.0)
        tested = {module.module: counts(module.outcomes)[SURVIVED] for module in report.modules}
        self.assertEqual({"tools/a.py": 1, "tools/b.py": 1}, tested)
        self.assertTrue(all(module.cut_short for module in report.modules))
        self.assertTrue(summary(report).startswith("**Unfinished, so not a verdict:**"))


class FlushRecorder(io.StringIO):
    """A standard output that records what was written when each flush came."""

    def __init__(self) -> None:
        super().__init__()
        self.flushed: list[str] = []

    def flush(self) -> None:
        self.flushed.append(self.getvalue())


@pytest.mark.parametrize(
    ("result", "code", "annotation"),
    [
        (mutation_gate.Result("## Mutation gate\n\nall ran\n"), 0, None),
        (
            mutation_gate.Result("**Unfinished, so not a verdict:** cut\n\n## Mutation gate\n", False),
            1,
            "::error title=Mutation gate unfinished::**Unfinished, so not a verdict:** cut\n",
        ),
    ],
)
def test_an_unfinished_run_fails_the_job_with_an_annotation_after_writing_its_summary(
    result: mutation_gate.Result, code: int, annotation: str | None, tmp_path: Path
) -> None:
    summary_file = tmp_path / "summary.md"
    output = FlushRecorder()
    with mock.patch.object(mutation_gate.Gate, "report", return_value=result), mock.patch("sys.stdout", output):
        assert code == mutation_gate.main([], {"GITHUB_STEP_SUMMARY": str(summary_file), "VALIDATION_JOBS": "1"})
    printed = output.getvalue()
    assert printed.startswith(result.text)
    assert printed.removeprefix(result.text) == (annotation or "")
    # The summary, and then the annotation, each reach the log before anything printed after them.
    assert output.flushed == [result.text, *([printed] if annotation else [])]
    assert result.text in summary_file.read_text(encoding="utf-8")


def test_the_base_defaults_to_the_pull_requests_base_branch() -> None:
    assert mutation_gate.parse_arguments([], {"GITHUB_BASE_REF": "release"}).base == "origin/release"
    assert mutation_gate.parse_arguments([], {}).base == "origin/main"
    assert mutation_gate.parse_arguments([], {}).minutes == 40
    with pytest.raises(SystemExit):
        mutation_gate.parse_arguments(["--minutes", "0"], {})

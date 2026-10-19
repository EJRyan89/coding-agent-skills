"""The mutation gate: which modules it mutates, how it orders and describes the mutants, which suites a mutant runs
against and what each outcome means, the time cap, the summary, and a whole run over a fixture repository's real
suites with a stub generator and reach, since mutmut and coverage.py are installed only on the gate's runner."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from git_client import GitClient

from tools import mutation_gate
from tools.mutation_gate import (
    KILLED,
    NOT_REACHED,
    NOT_RUN,
    PASSED,
    SURVIVED,
    TIMED_OUT,
    Gate,
    ModuleReport,
    Mutant,
    Outcome,
    Reach,
    Report,
    SuiteRunner,
    changed_lines,
    code,
    counts,
    coverage_settings,
    describe,
    executed_lines,
    generator_problem,
    is_gate_module,
    kill_ratio,
    module_mutants,
    no_module_line,
    summary,
    try_mutant,
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


def mutant(line: int, module: str = "tools/calc.py", changed: bool = False) -> Mutant:
    return Mutant(module, line, f"line {line}", f"mutated {line}", f"source {line}", changed)


class MutantFixtures(unittest.TestCase):
    def test_a_zero_context_diff_names_each_added_or_changed_line(self) -> None:
        diff = "@@ -3 +3 @@\n-a\n+b\n@@ -10,0 +11,2 @@\n+c\n+d\n@@ -20,2 +23,0 @@\n-e\n-f\n"
        self.assertEqual({3, 11, 12}, changed_lines(diff))

    def test_a_mutant_is_described_by_its_first_changed_line_on_one_line(self) -> None:
        original = "def add(a):\n    return a + 1\n"
        self.assertEqual((2, "return a + 1", "return a - 1"), describe(original, "def add(a):\n    return a - 1\n"))
        with self.assertRaises(ValueError):
            describe(original, original)

    def test_mutants_on_changed_lines_come_first_and_duplicates_and_the_unchanged_module_are_dropped(self) -> None:
        source = "a = 1\nb = 2\nc = 3\n"
        made = ["a = 1\nb = 2\nc = 4\n", "a = 0\nb = 2\nc = 3\n", source, "a = 1\nb = 9\nc = 3\n", source]
        made.append("a = 1\nb = 9\nc = 3\n")
        found = module_mutants("tools/calc.py", source, {3}, lambda module, text: made)
        self.assertEqual([(3, True), (1, False), (2, False)], [(item.line, item.changed) for item in found])
        self.assertEqual(("c = 3", "c = 4"), (found[0].before, found[0].after))

    def test_a_generator_that_no_longer_makes_the_canary_mutant_is_refused(self) -> None:
        self.assertIsNone(generator_problem(lambda module, text: [text.replace("+", "-"), text.replace("1", "2")]))
        problem = generator_problem(lambda module, text: [text.replace("1", "2")])
        self.assertIn("no longer turns", problem or "")

    def test_a_code_span_survives_backticks_in_the_source(self) -> None:
        self.assertEqual("`a + 1`", code("a + 1"))
        self.assertEqual("``print(`x`)``", code("print(`x`)"))
        self.assertEqual("`` `x` ``", code("`x`"))
        self.assertEqual("`` `x ``", code("`x"))
        self.assertEqual("`` x` ``", code("x`"))


class ReachFixtures(unittest.TestCase):
    def test_coverage_measures_the_suites_subprocesses_and_only_the_changed_modules(self) -> None:
        settings = coverage_settings(Path("data folder"), [Path("tree/tools/calc.py"), Path("tree/deployer/a.py")])
        self.assertIn("patch = subprocess", settings)
        self.assertIn("parallel = true", settings)
        self.assertIn(f"    {Path('tree/tools/calc.py')}\n    {Path('tree/deployer/a.py')}\n", settings)
        self.assertIn("disable_warnings = no-data-collected,", settings)

    def test_executed_lines_are_read_by_module_whatever_path_names_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary) / "tree with spaces"
            (tree / "tools").mkdir(parents=True)
            report = {
                "files": {
                    str(tree / "tools" / "calc.py"): {"executed_lines": [1, 2, 5]},
                    "deployer/a.py": {"executed_lines": [3]},
                    str(Path(temporary) / "elsewhere.py"): {"executed_lines": [9]},
                }
            }
            lines = executed_lines(report, tree, ["tools/calc.py", "deployer/a.py", "tools/unrun.py"])
        self.assertEqual(
            {"tools/calc.py": {1, 2, 5}, "deployer/a.py": {3}, "tools/unrun.py": set()},
            {module: set(found) for module, found in lines.items()},
        )


class CoverageReachFixtures(unittest.TestCase):
    """The reach of a suite through a stub coverage.py in the tree, which `python -m coverage` finds first there."""

    STUB = (
        "import json, pathlib, sys, time\n"
        "step = sys.argv[1]\n"
        "action = json.loads(pathlib.Path('stub-actions.json').read_text()).get(step, 'ok')\n"
        "if action == 'sleep':\n"
        "    time.sleep(60)\n"
        "if action.startswith('fail '):\n"
        "    _, code, message = action.split(' ', 2)\n"
        "    print(message, file=sys.stderr)\n"
        "    sys.exit(int(code))\n"
        "if step == 'json' and action == 'ok':\n"
        "    pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text(json.dumps({'files': {}}))\n"
    )

    def reach(self, actions: dict[str, str]) -> Reach | str:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary) / "tree with spaces"
            tree.mkdir()
            (tree / "coverage.py").write_text(self.STUB, encoding="utf-8")
            (tree / "stub-actions.json").write_text(json.dumps(actions), encoding="utf-8")
            with mock.patch.object(mutation_gate, "MINIMUM_SUITE_SECONDS", 2.0):
                return mutation_gate.coverage_reach(tree, "tests/test_calc.py", ["tools/calc.py"], 60.0)

    def test_a_suite_whose_coverage_data_is_combined_and_reported_is_measured(self) -> None:
        result = self.reach({})
        if not isinstance(result, Reach):
            self.fail(f"the reach was not measured: {result}")
        self.assertEqual({"tools/calc.py": frozenset()}, dict(result.lines))

    def test_a_failed_or_timed_out_coverage_step_names_the_step_and_its_output(self) -> None:
        for actions, expected in (
            (
                {"combine": "fail 3 Can't combine: the data file is damaged"},
                "failed in `coverage combine` with exit code 3: `Can't combine: the data file is damaged`",
            ),
            ({"json": "fail 1 No data to report."}, "failed in `coverage json` with exit code 1: `No data to report.`"),
            ({"combine": "sleep"}, "timed out in `coverage combine`"),
            ({"json": "sleep"}, "timed out in `coverage json`"),
            ({"json": "write nothing"}, "failed in `coverage json`, which wrote no report"),
        ):
            with self.subTest(actions=actions):
                self.assertEqual(expected, self.reach(actions))

    def test_a_suite_failed_unmutated_is_still_reported_by_its_exit_code(self) -> None:
        self.assertEqual("failed unmutated with exit code 4", self.reach({"run": "fail 4 a test failed"}))


class TryMutantFixtures(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tree = Path(temporary.name) / "tree with spaces"
        (self.tree / "tools").mkdir(parents=True)
        self.original = b"def add(a, b):\r\n    return a + b\r\n"
        (self.tree / "tools" / "calc.py").write_bytes(self.original)
        self.mutant = Mutant(
            "tools/calc.py", 2, "return a + b", "return a - b", "def add(a, b):\n    return a - b\n", True
        )
        self.reach = {
            "tests/slow.py": Reach(30.0, {"tools/calc.py": frozenset({1, 2})}),
            "tests/fast.py": Reach(2.0, {"tools/calc.py": frozenset({2})}),
            "tests/other.py": Reach(1.0, {"tools/calc.py": frozenset({1})}),
        }
        self.calls: list[tuple[str, float, str]] = []

    def runner(self, results: dict[str, str]) -> SuiteRunner:
        def run(tree: Path, suite: str, timeout: float) -> str:
            self.calls.append((suite, timeout, (tree / "tools" / "calc.py").read_text(encoding="utf-8")))
            return results.get(suite, PASSED)

        return run

    def test_the_first_failing_suite_kills_the_mutant_fastest_first_and_the_module_is_put_back(self) -> None:
        outcome = try_mutant(
            self.tree, self.mutant, self.reach, 100.0, self.runner({"tests/slow.py": KILLED}), lambda: 0.0
        )
        self.assertEqual(Outcome(self.mutant, KILLED, ("tests/fast.py", "tests/slow.py")), outcome)
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

    def test_a_mutant_no_suite_executed_is_not_reached_and_runs_nothing(self) -> None:
        unreached = Mutant("tools/calc.py", 7, "x", "y", "z", False)
        self.assertEqual(
            Outcome(unreached, NOT_REACHED),
            try_mutant(self.tree, unreached, self.reach, 100.0, self.runner({}), lambda: 0.0),
        )
        self.assertEqual([], self.calls)

    def test_the_deadline_leaves_a_mutant_not_run_and_the_module_put_back_even_when_a_suite_raises(self) -> None:
        times = iter([0.0, 200.0])
        outcome = try_mutant(self.tree, self.mutant, self.reach, 100.0, self.runner({}), lambda: next(times))
        self.assertEqual((NOT_RUN, ("tests/fast.py",)), (outcome.status, outcome.suites))

        def broken(tree: Path, suite: str, timeout: float) -> str:
            raise OSError("the suite could not start")

        with self.assertRaises(OSError):
            try_mutant(self.tree, self.mutant, self.reach, 100.0, broken, lambda: 0.0)
        self.assertEqual(self.original, (self.tree / "tools" / "calc.py").read_bytes())


def test_the_cap_gives_the_reach_pass_half_and_each_module_an_equal_share_of_what_is_left() -> None:
    assert mutation_gate.run_deadline(100.0, 40) == 100.0 + 40 * 60
    assert mutation_gate.reach_deadline(100.0, 100.0 + 40 * 60) == 100.0 + 20 * 60
    # Three modules left with 900 seconds to go: the first gets 300 of them, and the last whatever remains.
    assert mutation_gate.module_deadline(1000.0, 1900.0, 3) == 1300.0
    assert mutation_gate.module_deadline(1000.0, 1900.0, 1) == 1900.0


class SummaryFixtures(unittest.TestCase):
    def report(self, outcomes: list[Outcome], cut_short: bool = False) -> Report:
        return Report(
            "origin/main",
            40,
            600.0,
            4,
            "3.8.0",
            [ModuleReport("tools/calc.py", outcomes, cut_short)],
            {"tests/broken.py": "failed unmutated with exit code 1"},
            ["tests/late.py"],
        )

    def test_the_kill_ratio_counts_timeouts_as_killed_and_leaves_out_what_was_not_tested(self) -> None:
        found = {KILLED: 6, TIMED_OUT: 1, SURVIVED: 3, NOT_REACHED: 4, NOT_RUN: 2}
        self.assertEqual("70% (7 of 10)", kill_ratio(found))
        self.assertEqual("none tested", kill_ratio(dict.fromkeys(found, 0) | {NOT_REACHED: 3}))

    def test_the_summary_tables_each_module_and_names_each_survivor_by_line_mutation_and_suites(self) -> None:
        survivor = Outcome(mutant(4, changed=True), SURVIVED, ("tests/fast.py", "tests/slow.py"))
        outcomes = [Outcome(mutant(1), KILLED, ("tests/fast.py",)), survivor, Outcome(mutant(9), NOT_REACHED)]
        text = summary(self.report(outcomes))
        self.assertIn("| `tools/calc.py` | 3 | 1 | 0 | 1 | 1 | 0 | 50% (1 of 2) |", text)
        self.assertIn("### `tools/calc.py`: 1 surviving mutant\n", text)
        self.assertIn(
            "- Line 4 (a line this pull request changed): `line 4` became `mutated 4`; passed `tests/fast.py`, "
            "`tests/slow.py`",
            text,
        )
        self.assertIn("mutants made by mutmut 3.8.0", text)
        self.assertIn("4 workers ran for 10 of the 40 minutes", text)
        self.assertIn("`tests/broken.py` failed unmutated with exit code 1, so no mutant ran against it.", text)
        self.assertIn("left these suites' reach unmeasured, so no mutant ran against them: `tests/late.py`.", text)
        self.assertNotIn("cut", text.split("Kill ratio")[1].split("###")[0].replace("unmutated", ""))

    def test_every_outcome_is_counted_and_survivors_are_counted_and_marked_one_by_one(self) -> None:
        outcomes = [
            Outcome(mutant(1), KILLED, ("t",)),
            Outcome(mutant(2), KILLED, ("t",)),
            Outcome(mutant(3, changed=True), SURVIVED, ("t",)),
            Outcome(mutant(5), SURVIVED, ("t",)),
        ]
        self.assertEqual({KILLED: 2, TIMED_OUT: 0, SURVIVED: 2, NOT_REACHED: 0, NOT_RUN: 0}, counts(outcomes))
        text = summary(self.report(outcomes))
        self.assertIn("### `tools/calc.py`: 2 surviving mutants", text)
        self.assertIn("- Line 3 (a line this pull request changed): `line 3`", text)
        self.assertIn("- Line 5: `line 5` became `mutated 5`; passed `t`", text)

    def test_the_summary_says_when_the_cap_cut_a_module_short(self) -> None:
        text = summary(self.report([Outcome(mutant(1), KILLED, ("t",)), Outcome(mutant(2), NOT_RUN)], cut_short=True))
        self.assertIn("The 40-minute cap cut `tools/calc.py` short: 1 mutants did not run.", text)

    def test_a_change_with_no_module_to_mutate_is_one_line(self) -> None:
        line = no_module_line("origin/main")
        self.assertEqual(1, line.count("\n"))
        self.assertTrue(line.endswith("since `origin/main`, so no mutant ran.\n"))


CALC = "def add(a, b):\n    return a + b\n\n\ndef double(a):\n    return a * 2\n"
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


def stub_generator(module: str, source: str) -> list[str]:
    """The fixture module's two mutants, and the canary's, which every run checks first."""
    return [source.replace("a + b", "a - b"), source.replace("a * 2", "a * 3"), source.replace("a + 1", "a - 1")]


def stub_reach(tree: Path, suite: str, modules: object, timeout: float) -> Reach:
    lines = frozenset(range(1, CALC.count("\n") + 1))
    return Reach(1.0, {"tools/calc.py": lines})


class WholeRunFixtures(unittest.TestCase):
    """A run over a fixture repository: real worktrees and the real suite runner, with the generator and the reach
    stubbed, since mutmut and coverage.py are not development dependencies."""

    def test_a_pull_requests_module_is_mutated_in_worktrees_and_the_survivor_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository with spaces"
            fixture_repository(root)
            gate = Gate(root, "main", 5, 2, GitClient(), stub_generator, stub_reach)
            text = gate.report()
            worktrees = git(root, "worktree", "list", "--porcelain")
            status = git(root, "status", "--porcelain")
        self.assertIn("| `tools/calc.py` | 2 | 1 | 0 | 1 | 0 | 0 | 50% (1 of 2) |", text)
        self.assertIn(
            "- Line 6 (a line this pull request changed): `return a * 2` became `return a * 3`; passed "
            "`tests/tools/test_calc.py`",
            text,
        )
        # The worktrees are gone and the checkout untouched.
        self.assertEqual(1, worktrees.count("worktree "))
        self.assertEqual("", status)

    def test_a_change_with_no_module_writes_one_line_and_makes_no_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            fixture_repository(root)
            git(root, "switch", "--quiet", "-c", "documentation", "main")
            (root / "docs.md").write_text("only documentation\n", encoding="utf-8")
            git(root, "add", ".")
            git(root, "commit", "--quiet", "-m", "docs")

            def refuse(module: str, source: str) -> list[str]:
                raise AssertionError("no mutant is made when no module changed")

            text = Gate(root, "main", 5, 2, GitClient(), refuse, stub_reach).report()
            worktrees = git(root, "worktree", "list", "--porcelain")
        self.assertEqual(no_module_line("main"), text)
        self.assertEqual(1, worktrees.count("worktree "))

    def test_a_generator_that_fails_its_canary_stops_the_run_before_any_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            fixture_repository(root)
            gate = Gate(root, "main", 5, 2, GitClient(), lambda module, source: [], stub_reach)
            with self.assertRaisesRegex(RuntimeError, "no longer turns"):
                gate.report()
            self.assertEqual(1, git(root, "worktree", "list", "--porcelain").count("worktree "))


def test_the_base_defaults_to_the_pull_requests_base_branch() -> None:
    assert mutation_gate.parse_arguments([], {"GITHUB_BASE_REF": "release"}).base == "origin/release"
    assert mutation_gate.parse_arguments([], {}).base == "origin/main"
    assert mutation_gate.parse_arguments([], {}).minutes == 40
    with pytest.raises(SystemExit):
        mutation_gate.parse_arguments(["--minutes", "0"], {})

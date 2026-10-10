"""Regression suites: how they are found, sharded, and run, the rules the skill guide states for them, and that
every module is named by a test.
"""

from __future__ import annotations

import ast
import fnmatch
import functools
import re
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path

from job_pool import UNSPLIT_SUITE_WEIGHT, Job, run_process
from shell_targets import run_git_bash, shell_quote
from toolchain import find_powershell
from validation_support import (
    PYTHON_ENTRY_POINT,
    REPOSITORY_ROOT,
    SKILL_GUIDE,
    TEST_NAME_PATTERNS,
    TEST_SCRIPT_EXTENSIONS,
    _markdown_section,
    import_aliases,
    is_executable_script,
    is_test_script,
    module_imports,
    qualified_name,
    relative,
    repository_files,
    skill_script_directories,
)

SHARD_RUNNER = REPOSITORY_ROOT / "tests" / "run_shard.py"
# A Python suite is split into one shard per this many tests, up to MAXIMUM_SHARDS. Smaller shards spread a long
# suite further, at the cost of one more process start and suite import each. A recorded test counts as its seconds.
TESTS_PER_SHARD = 6
MAXIMUM_SHARDS = 8
# The seconds a test takes run alone, recorded for a test that costs many times a typical one, by suite and then by
# Class.test; any other test counts as one. The suite gets shards for its total, and run_shard.py packs the heaviest
# test first onto the lightest shard, so a recorded test runs on a shard of its own instead of lengthening a shared
# one. Measured on 2026-10-08 on a 24-CPU Windows machine, each test run alone.
RECORDED_TEST_SECONDS: dict[str, dict[str, float]] = {
    "skills/code-review-core/scripts/test_adversarial_inputs.py": {
        # Writes and reads back a path at this machine's limit, which with long paths enabled is 32,507 units.
        (
            "PathLengthTests.test_a_path_at_this_machines_limit_is_written_and_one_unit_longer_is_an_unsafe_path_"
            "exclusion"
        ): 13,
    },
    "skills/repo-cleanup/scripts/test_repo_cleanup.py": {
        # Pushes nine branches to a local remote, deletes them there, and plans and applies a cleanup of each with git.
        "PlanApplyTests.test_gone_branches_are_deleted_only_when_their_pull_request_proves_them_stale": 13,
    },
}
TEST_DEFINITION = re.compile(r"^[ \t]+def test_\w+", re.MULTILINE)


def _needs_a_test(name: str, scripts: list[str]) -> bool:
    """Whether a repository path is a module under a skill's scripts/, deployer/, or tools/ that a test must import."""
    parts = name.split("/")
    in_scope = parts[0] in {"deployer", "tools"} or any(name.startswith(f"{directory}/") for directory in scripts)
    return in_scope and name.endswith(".py") and parts[-1] != "__init__.py" and not is_test_script(Path(name))


def _import_name(name: str) -> str:
    """The name a test imports a module by: deployer.x and tools.x as packages, a skill's script by its bare name."""
    parts = name.removesuffix(".py").split("/")
    return ".".join(parts) if parts[0] in {"deployer", "tools"} else parts[-1]


def imported_modules(source: str) -> set[str]:
    """The modules a test imports: every `import a.b`, `from a import b`, and importlib.import_module("a.b"), as a.b
    and each package above it, and every file spec_from_file_location loads, as `*` and the path it names without
    .py, such as *tools/b for "tools/b.py" or *b for a path built from "b.py"."""
    tree = ast.parse(source)
    imported = [[found.module, *(target for _, target in found.bindings)] for found in module_imports(tree)]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
            function = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            strings = [
                part.value for part in ast.walk(node) if isinstance(part, ast.Constant) and isinstance(part.value, str)
            ]
            if function == "import_module":
                names = strings[:1]
            elif function == "spec_from_file_location":
                names = [f"*{value.removesuffix('.py')}" for value in strings if value.endswith(".py")]
            else:
                continue
            imported.append(names)
    found: set[str] = set()
    for names in imported:
        for name in names:
            parts = name.split(".")
            found |= {".".join(parts[: count + 1]) for count in range(len(parts))}
    return found


def untested_module_problems(root: Path) -> list[str]:
    """Report each module under skills/*/scripts/, deployer/, or tools/ that no test_*.py imports.

    A test imports the module itself, by an import statement or through importlib; naming, mentioning, or running it
    is not enough, and neither is a test file named after it. A package's __init__.py needs no test.
    """
    files = repository_files(root)
    imported: set[str] = set()
    for path in files:
        if fnmatch.fnmatchcase(path.name, "test_*.py"):
            imported |= imported_modules(path.read_text(encoding="utf-8"))
    by_path = {name.removeprefix("*") for name in imported if name.startswith("*")}
    scripts = [directory.relative_to(root).as_posix() for directory in skill_script_directories(root)]
    problems: list[str] = []
    for path in sorted(files, key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        if not _needs_a_test(name, scripts):
            continue
        loaded = any(f"/{name.removesuffix('.py')}".endswith(f"/{stem}") for stem in by_path)
        if _import_name(name) not in imported and not loaded:
            problems.append(f"{name} is imported by no test_*.py; add a test that imports it")
    return problems


def suite_discovery_documentation_problems(root: Path) -> list[str]:
    """Report a rule that decides whether a regression suite runs and that "Validation" in the skill guide omits."""
    section = _markdown_section((root / SKILL_GUIDE).read_text(encoding="utf-8"), "## Validation")
    if section is None:
        return [f'{SKILL_GUIDE} has no "Validation" section']
    rules = [*TEST_NAME_PATTERNS, *sorted(TEST_SCRIPT_EXTENSIONS), PYTHON_ENTRY_POINT]
    return [f'{SKILL_GUIDE} "Validation" does not name `{rule}`' for rule in rules if f"`{rule}`" not in section]


def run_test_script(path: Path) -> None:
    target = relative(path)
    suffix = path.suffix.casefold()
    if suffix == ".py":
        run_process([sys.executable, "-B", target])
    elif suffix == ".sh":
        run_git_bash(f"bash {shell_quote(target)}")
    elif suffix == ".ps1":
        run_process([find_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", target])
    else:
        raise AssertionError(f"Unsupported skill test type '{path.suffix}': {target}")


def regression_suites(root: Path = REPOSITORY_ROOT) -> list[Path]:
    """Every regression suite: the test scripts under tests/ and under each shipped or repository skill's scripts/."""
    roots = [root / "tests", *skill_script_directories(root)]
    found = (
        path for top in roots if top.is_dir() for path in top.rglob("*") if path.is_file() and is_test_script(path)
    )
    return sorted(found, key=lambda path: path.relative_to(root).as_posix().casefold())


def suite_cost(suite: Path, recorded: Mapping[str, float]) -> float:
    """A Python suite's cost in typical tests: one for each test, or its recorded seconds."""
    tests = len(TEST_DEFINITION.findall(suite.read_text(encoding="utf-8")))
    return tests + sum(seconds - 1 for seconds in recorded.values())


def shard_count(suite: Path, recorded: Mapping[str, float] | None = None) -> int:
    if suite.suffix.casefold() != ".py":
        return 1
    return max(1, min(MAXIMUM_SHARDS, int(suite_cost(suite, recorded or {})) // TESTS_PER_SHARD))


def run_shard(suite: Path, index: int, count: int, recorded: Mapping[str, float]) -> None:
    costs = [f"{test}={seconds}" for test, seconds in sorted(recorded.items())]
    run_process([sys.executable, "-B", relative(SHARD_RUNNER), relative(suite), str(index), str(count), *costs])


def suite_jobs(
    suites: list[Path],
    recorded: Mapping[str, Mapping[str, float]] = RECORDED_TEST_SECONDS,
    root: Path = REPOSITORY_ROOT,
) -> list[Job]:
    jobs = []
    for suite in suites:
        label = suite.relative_to(root).as_posix()
        if suite.suffix.casefold() != ".py":
            jobs.append(Job(label, label, UNSPLIT_SUITE_WEIGHT, functools.partial(run_test_script, suite)))
            continue
        seconds = recorded.get(label, {})
        count = shard_count(suite, seconds)
        cost = suite_cost(suite, seconds)
        if count == 1:
            jobs.append(Job(label, label, cost, functools.partial(run_test_script, suite)))
            continue
        for index in range(count):
            jobs.append(
                Job(
                    f"{label} [shard {index + 1}/{count}]",
                    label,
                    cost / count,
                    functools.partial(run_shard, suite, index, count, seconds),
                )
            )
    return jobs


def _defined_tests(source: str) -> set[str]:
    """Each Class.test the source defines directly in a class body."""
    return {
        f"{node.name}.{item.name}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ClassDef)
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def recorded_cost_problems(
    root: Path, recorded: Mapping[str, Mapping[str, float]] = RECORDED_TEST_SECONDS
) -> list[str]:
    """Report a recorded test cost that names no Python regression suite, a test the suite does not define, or a cost
    no heavier than a typical test."""
    problems: list[str] = []
    for name, tests in recorded.items():
        path = root / name
        if not (path.is_file() and is_test_script(path) and path.suffix == ".py"):
            problems.append(f"{name} records test costs but is not a regression suite")
            continue
        defined = _defined_tests(path.read_text(encoding="utf-8"))
        for test, seconds in tests.items():
            if test not in defined:
                problems.append(f"{name} records {test}, which it does not define")
            elif seconds <= 1:
                problems.append(f"{name} records {test} at {seconds} seconds; record only tests that cost more than 1")
    return problems


def _calls_unittest_main(statement: ast.stmt, aliases: dict[str, str]) -> bool:
    """Whether a statement calls unittest.main, resolved through the module's imports, or exits with its result."""
    if not isinstance(statement, ast.Expr | ast.Raise):
        return False
    call = statement.value if isinstance(statement, ast.Expr) else statement.exc
    if isinstance(call, ast.Call) and qualified_name(call.func, aliases) in {"sys.exit", "SystemExit"} and call.args:
        call = call.args[0]
    return isinstance(call, ast.Call) and qualified_name(call.func, aliases) == "unittest.main"


def suite_entry_point_problems(root: Path) -> list[str]:
    """Report a Python regression suite whose last top-level statement is not an `if __name__ == "__main__":` block
    that calls unittest.main().

    Every suite runs as `python <file>`, so one without that block defines its tests, runs none, and still exits 0;
    the block comes last, after every test it runs is defined.
    """
    problems: list[str] = []
    for path in regression_suites(root):
        if path.suffix.casefold() != ".py":
            continue
        name = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        last = tree.body[-1] if tree.body else None
        if not (isinstance(last, ast.If) and ast.unparse(last.test) == "__name__ == '__main__'"):
            problems.append(f"{name} does not end with an `{PYTHON_ENTRY_POINT}` block, so it runs no tests")
        elif not any(_calls_unittest_main(statement, import_aliases(tree)) for statement in last.body):
            problems.append(f"{name}:{last.lineno} its `{PYTHON_ENTRY_POINT}` block does not call unittest.main()")
    return problems


def unsuited_script_problems(root: Path) -> list[str]:
    """Report a shipped or repository skill whose scripts/ holds executable files but no regression suite."""
    problems: list[str] = []
    for scripts in skill_script_directories(root):
        files = sorted(path for path in scripts.rglob("*") if path.is_file())
        if any(is_executable_script(path) and not is_test_script(path) for path in files) and not any(
            is_test_script(path) for path in files
        ):
            problems.append(
                f"{scripts.relative_to(root).as_posix()} holds scripts but no regression suite; add a test_*, test-*, "
                "*_test, *-test, or *.test.* Python, Bash, or PowerShell script beside them"
            )
    return problems


class SuiteDiscoveryPolicies(unittest.TestCase):
    def test_scripted_skills_have_regression_suites(self) -> None:
        self.assertEqual([], unsuited_script_problems(REPOSITORY_ROOT))

    def test_every_regression_suite_is_found(self) -> None:
        suites = {relative(path) for path in regression_suites()}
        for expected in (
            "tests/deployer/test_frontmatter.py",
            "tests/tools/test_worktrees.py",
            "tests/ai-config/test_cross_skill_contracts.py",
            "skills/update-coding-agent-skills/scripts/test_update.sh",
            "skills/code-review-core/scripts/test_review_pipeline.py",
        ):
            self.assertIn(expected, suites)
        self.assertNotIn("tests/deployer/harness.py", suites)
        self.assertNotIn("tests/run_shard.py", suites)

    def test_python_suites_run_their_tests_when_executed(self) -> None:
        self.assertTrue([path for path in regression_suites() if path.suffix.casefold() == ".py"])
        self.assertEqual([], suite_entry_point_problems(REPOSITORY_ROOT))

    def test_suite_discovery_rules_are_documented(self) -> None:
        self.assertEqual([], suite_discovery_documentation_problems(REPOSITORY_ROOT))

    def test_recorded_test_costs_name_tests_that_exist(self) -> None:
        self.assertEqual([], recorded_cost_problems(REPOSITORY_ROOT))

    def test_every_module_is_named_by_a_test(self) -> None:
        self.assertEqual([], untested_module_problems(REPOSITORY_ROOT))

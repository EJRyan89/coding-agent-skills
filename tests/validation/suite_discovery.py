"""Regression suites: how they are found, sharded, and run, the rules the skill guide states for them, and that
every module is named by a test.
"""

from __future__ import annotations

import ast
import fnmatch
import functools
import re
import sys
import tomllib
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path

from job_pool import UNSPLIT_SUITE_WEIGHT, Job, run_process
from shell_targets import run_git_bash, shell_quote
from skill_scripts import command_reader
from toolchain import find_powershell
from validation_support import (
    PYTHON_ENTRY_POINT,
    REPOSITORY_ROOT,
    SKILL_GUIDE,
    TEST_NAME_PATTERNS,
    TEST_SCRIPT_EXTENSIONS,
    import_aliases,
    is_executable_script,
    is_test_script,
    markdown_section,
    module_imports,
    qualified_name,
    relative,
    repository_files,
    skill_script_directories,
)

from tests import run_shard as shard_runner

SHARD_RUNNER = REPOSITORY_ROOT / "tests" / "run_shard.py"
# A Python suite is split into one shard per this many tests, up to MAXIMUM_SHARDS. Smaller shards spread a long
# suite further, at the cost of one more process start and suite import each. A recorded test counts as its seconds.
TESTS_PER_SHARD = 6
MAXIMUM_SHARDS = 8
# The seconds a test takes run alone, recorded for a test that costs many times a typical one, by suite and then by
# Class.test; any other test counts as one. The suite gets shards for its total, and run_shard.py packs the heaviest
# test first onto the lightest shard, so a recorded test runs on a shard of its own instead of lengthening a shared
# one, as long as the suite's other tests fit on its other shards; recorded_shard_problems holds that, which can fail
# once a suite outgrows MAXIMUM_SHARDS. Measured on 2026-10-08 on a 24-CPU Windows machine, each test run alone.
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
    "tests/validation/test_skill_scripts.py": {
        # Starts a Python per entry point, which imports the entry point and runs its __main__ block twice.
        "EntryPointRunnerTests.test_every_entry_point_turns_a_failure_into_a_failed_line_and_ctrl_c_into_exit_130": 10,
    },
}


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
    section = markdown_section((root / SKILL_GUIDE).read_text(encoding="utf-8"), "## Validation")
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


# Review and canary inputs: each tree there is another repository, whose suites are what a reviewer reads, not tests of
# this one.
FIXTURE_ROOT = Path("tests") / "fixtures"


def regression_suites(root: Path = REPOSITORY_ROOT) -> list[Path]:
    """Every regression suite: the test scripts under tests/, outside tests/fixtures/, and under each shipped or
    repository skill's scripts/."""
    roots = [root / "tests", *skill_script_directories(root)]
    found = (
        path
        for top in roots
        if top.is_dir()
        for path in top.rglob("*")
        if path.is_file() and is_test_script(path) and not path.is_relative_to(root / FIXTURE_ROOT)
    )
    return sorted(found, key=lambda path: path.relative_to(root).as_posix().casefold())


def suite_cost(suite: Path, recorded: Mapping[str, float]) -> float:
    """A Python suite's cost in typical tests: one for each test, or its recorded seconds."""
    tests = len(defined_tests(suite.read_text(encoding="utf-8")))
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


def defined_tests(source: str) -> list[str]:
    """Each Class.test the source defines directly in a class body, named test* as unittest's loader finds them: the
    one reader of a suite's tests, for its cost, its shards, and the tests its recorded seconds name."""
    return sorted(
        f"{node.name}.{item.name}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ClassDef)
        for item in node.body
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and item.name.startswith("test")
    )


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
        defined = defined_tests(path.read_text(encoding="utf-8"))
        for test, seconds in tests.items():
            if test not in defined:
                problems.append(f"{name} records {test}, which it does not define")
            elif seconds <= 1:
                problems.append(f"{name} records {test} at {seconds} seconds; record only tests that cost more than 1")
    return problems


def recorded_shard_problems(
    root: Path, recorded: Mapping[str, Mapping[str, float]] = RECORDED_TEST_SECONDS
) -> list[str]:
    """Report a recorded test that run_shard.py would deal onto a shard beside another test.

    The heaviest test goes first onto an empty shard, and another test joins it only once every other shard carries as
    much. Below MAXIMUM_SHARDS a suite's shard count grows with its tests, so the others fit around a recorded test; at
    the cap a growing suite fills them, and the recorded test would lengthen a shared shard again.
    """
    problems: list[str] = []
    for name, seconds in recorded.items():
        path = root / name
        if not (path.is_file() and path.suffix == ".py"):
            continue
        shards = shard_runner.deal(defined_tests(path.read_text(encoding="utf-8")), shard_count(path, seconds), seconds)
        for test in sorted(set(seconds) & set(shards)):
            if list(shards.values()).count(shards[test]) > 1:
                problems.append(
                    f"{name} deals {test} onto a shard beside other tests, since the suite fills its "
                    f"{MAXIMUM_SHARDS} shards; raise MAXIMUM_SHARDS or split the suite"
                )
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


# The base every test class in a suite that runs git inherits, and the call a setUpModule of such a suite starts with.
GIT_TEST_BASE = "git_test_base.GitTestCase"
GIT_MODULE_ISOLATION = "git_test_base.isolate_module"
TEST_CASE_BASES = frozenset({"unittest.TestCase", "unittest.IsolatedAsyncioTestCase"})
MYPY_PATH_PREFIX = "$MYPY_CONFIG_FILE_DIR/"
# What a module that runs git holds: a string that starts with the word git, or the git client.
GIT_MARKER = re.compile(r"""["']git(?![\w-])|GitClient""")


def _starts_git(node: ast.AST) -> bool:
    """Whether a node is a list or tuple whose first item is the string git."""
    return (
        isinstance(node, (ast.List, ast.Tuple))
        and bool(node.elts)
        and isinstance(node.elts[0], ast.Constant)
        and node.elts[0].value == "git"
    )


class RepositoryModules:
    """The repository modules a Python file imports, resolved as they are at run time, and whether each runs git.

    A dotted import resolves beside the file, then from the repository root, as deployer.x, tools.x, and tests.x
    do; a relative import from the file's package; and a name each candidate resolves to in the mypy_path
    directories, which validation keeps equal to the directories code puts on sys.path. A module runs git as
    skill_scripts.CommandReader reads it: a list or tuple that starts with git, a command runner or a lookup given
    git, or a GitClient.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        pyproject = root / "pyproject.toml"
        configuration = tomllib.loads(pyproject.read_text(encoding="utf-8")) if pyproject.is_file() else {}
        entries = configuration.get("tool", {}).get("mypy", {}).get("mypy_path", [])
        self.path_directories = [root / entry.removeprefix(MYPY_PATH_PREFIX) for entry in entries]
        self._trees: dict[Path, ast.Module] = {}
        self._aliases: dict[Path, dict[str, str]] = {}
        self._imports: dict[Path, list[Path]] = {}
        self._resolved: dict[tuple[Path, str, int], list[Path]] = {}
        self._runs_git: dict[Path, bool] = {}

    def tree(self, path: Path) -> ast.Module:
        if path not in self._trees:
            self._trees[path] = ast.parse(path.read_text(encoding="utf-8"))
        return self._trees[path]

    def aliases(self, path: Path) -> dict[str, str]:
        if path not in self._aliases:
            self._aliases[path] = import_aliases(self.tree(path))
        return self._aliases[path]

    @staticmethod
    def _module_file(base: Path, parts: list[str]) -> Path | None:
        target = base.joinpath(*parts)
        for candidate in (target.with_name(f"{target.name}.py"), target / "__init__.py"):
            if candidate.is_file():
                return candidate.resolve()
        return None

    def resolve(self, path: Path, module: str, level: int = 0) -> list[Path]:
        """The files an import of module from path may load, each package's __init__.py included."""
        key = (path.parent, module, level)
        if key not in self._resolved:
            self._resolved[key] = self._resolve(path, module, level)
        return self._resolved[key]

    def _resolve(self, path: Path, module: str, level: int) -> list[Path]:
        parts = module.split(".") if module else []
        if level:
            base = path.parent
            for _ in range(level - 1):
                base = base.parent
            bases = [base]
        else:
            bases = [path.parent, self.root, *self.path_directories]
        found: list[Path] = []
        for base in bases:
            for count in range(1, len(parts) + 1) if parts else [0]:
                candidate = self._module_file(base, parts[:count]) if count else base / "__init__.py"
                if candidate is not None and candidate.is_file() and candidate.resolve() not in found:
                    found.append(candidate.resolve())
            if found and not level:
                break
        return [file for file in found if file.is_relative_to(self.root.resolve())]

    def imports(self, path: Path) -> list[Path]:
        """The repository files path's imports may load, in the order they appear."""
        if path in self._imports:
            return self._imports[path]
        found: list[Path] = []
        for node in ast.walk(self.tree(path)):
            if isinstance(node, ast.Import):
                names = [(alias.name, 0) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [(module, node.level)]
                names += [(f"{module}.{alias.name}" if module else alias.name, node.level) for alias in node.names]
            else:
                continue
            found += [file for name, level in names for file in self.resolve(path, name, level)]
        self._imports[path] = list(dict.fromkeys(found))
        return self._imports[path]

    def runs_git_itself(self, path: Path) -> bool:
        if path not in self._runs_git:
            # The reader finds git only in a string that starts with it or in GitClient, so a module without either
            # is not read, and a command list that starts with git answers before the reader resolves every name.
            source = path.read_text(encoding="utf-8")
            if not GIT_MARKER.search(source):
                self._runs_git[path] = False
            elif any(_starts_git(node) for node in ast.walk(self.tree(path))):
                self._runs_git[path] = True
            else:
                self._runs_git[path] = "git" in command_reader(path).commands({"git"})
        return self._runs_git[path]

    def git_runner(self, path: Path) -> Path | None:
        """The first module, path itself first and then what it imports breadth first, that runs git."""
        queue, seen = [path.resolve()], {path.resolve()}
        while queue:
            current = queue.pop(0)
            if self.runs_git_itself(current):
                return current
            for imported in self.imports(current):
                if imported not in seen:
                    seen.add(imported)
                    queue.append(imported)
        return None

    def class_bases(self, path: Path, name: str, seen: frozenset[tuple[Path, str]] = frozenset()) -> set[str]:
        """The test bases a class of path's derives from, through its own classes and the repository's: each of
        TEST_CASE_BASES and GIT_TEST_BASE it reaches."""
        tree = self.tree(path)
        definition = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name), None)
        if definition is None or (path, name) in seen:
            return set()
        aliases = self.aliases(path)
        found: set[str] = set()
        for base in definition.bases:
            qualified = qualified_name(base, aliases)
            if qualified in TEST_CASE_BASES or qualified == GIT_TEST_BASE:
                found.add(qualified)
            elif "." not in qualified:
                found |= self.class_bases(path, qualified, seen | {(path, name)})
            else:
                module, _, imported = qualified.rpartition(".")
                for file in self.resolve(path, module):
                    found |= self.class_bases(file, imported, seen | {(path, name)})
        return found


def _starts_with_call(function: ast.FunctionDef | ast.AsyncFunctionDef, matches: Callable[[ast.expr], bool]) -> bool:
    """Whether the function's first statement after its docstring is a call whose callable `matches` accepts."""
    body = function.body[1:] if function.body and ast.get_docstring(function) is not None else function.body
    first = body[0] if body else None
    return isinstance(first, ast.Expr) and isinstance(first.value, ast.Call) and matches(first.value.func)


def _is_parent_set_up_class(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "setUpClass"
        and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func) == "super"
    )


def _suite_git_problems(modules: RepositoryModules, path: Path, name: str, runner: str) -> list[str]:
    tree = modules.tree(path)
    aliases = modules.aliases(path)
    reason = f"runs git ({'itself' if runner == name else f'through {runner}'})"
    problems: list[str] = []
    for node in tree.body:
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "setUpModule"
            and not _starts_with_call(node, lambda call: qualified_name(call, aliases) == GIT_MODULE_ISOLATION)
        ):
            problems.append(f"{name}:{node.lineno} {reason}, so its setUpModule starts with {GIT_MODULE_ISOLATION}()")
        if not isinstance(node, ast.ClassDef):
            continue
        bases = modules.class_bases(path, node.name)
        if not bases:
            continue
        if GIT_TEST_BASE not in bases:
            problems.append(
                f"{name}:{node.lineno} {node.name} does not inherit {GIT_TEST_BASE}, and the suite {reason}"
            )
        for item in node.body:
            if (
                isinstance(item, ast.FunctionDef)
                and item.name == "setUpClass"
                and not _starts_with_call(item, _is_parent_set_up_class)
            ):
                problems.append(
                    f"{name}:{item.lineno} {node.name}.setUpClass does not start with super().setUpClass(), which "
                    f"isolates its git, and the suite {reason}"
                )
    return problems


def git_isolation_problems(root: Path) -> list[str]:
    """Report a Python regression suite that runs git, itself or through a repository module it imports, with a test
    class that does not inherit git_test_base.GitTestCase, a setUpClass that does not start by calling its parent's,
    or a setUpModule that does not start with git_test_base.isolate_module().

    The shell that starts a suite reaches its git otherwise: a command-scope setting outranks every file, a global
    hook runs on a fixture's commit, and a hook's GIT_DIR points every command at another repository.
    """
    modules = RepositoryModules(root)
    problems: list[str] = []
    for path in regression_suites(root):
        if path.suffix.casefold() != ".py":
            continue
        runner = modules.git_runner(path)
        if runner is None:
            continue
        name = path.relative_to(root).as_posix()
        problems += _suite_git_problems(modules, path.resolve(), name, runner.relative_to(root.resolve()).as_posix())
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

    def test_each_recorded_test_runs_on_a_shard_of_its_own(self) -> None:
        self.assertEqual([], recorded_shard_problems(REPOSITORY_ROOT))

    def test_every_module_is_named_by_a_test(self) -> None:
        self.assertEqual([], untested_module_problems(REPOSITORY_ROOT))

    def test_every_suite_that_runs_git_isolates_it(self) -> None:
        self.assertEqual([], git_isolation_problems(REPOSITORY_ROOT))

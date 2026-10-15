"""Regression suites: how they are found, sharded, and run, the rules the skill guide states for them, and that
every module is named by a test.
"""

from __future__ import annotations

import ast
import fnmatch
import functools
import itertools
import re
import sys
import tomllib
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path

from job_pool import UNSPLIT_SUITE_WEIGHT, Job, run_process
from shell_targets import run_git_bash, shell_quote
from skill_scripts import STANDARD_STREAMS, command_reader
from toolchain import find_powershell
from validation_support import (
    PYTHON_ENTRY_POINT,
    REPOSITORY_ROOT,
    SKILL_GUIDE,
    TEST_NAME_PATTERNS,
    TEST_SCRIPT_EXTENSIONS,
    CallableResolver,
    UnreadablePython,
    call_argument,
    import_aliases,
    is_executable_script,
    is_test_script,
    markdown_section,
    module_statements,
    name_values,
    parse_python,
    parse_source,
    path_parts,
    python_source,
    python_trees,
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


def untested_module_problems(root: Path) -> list[str]:
    """Report each module under skills/*/scripts/, deployer/, or tools/ that no test_*.py imports.

    A test imports the module itself: an import statement, an importlib.import_module or __import__ of a literal name,
    or a load of its file by path, each resolved to the file it loads as RepositoryModules resolves it, so another
    skill's module of the same name, or a standard-library module, is not this one. Naming, mentioning, or running it
    is not enough, and neither is a test file named after it. A package's __init__.py needs no test.
    """
    files = repository_files(root)
    modules = RepositoryModules(root)
    tested: set[Path] = set()
    for path in files:
        if fnmatch.fnmatchcase(path.name, "test_*.py"):
            tested |= set(modules.imports(path, unique_loads=True))
    scripts = [directory.relative_to(root).as_posix() for directory in skill_script_directories(root)]
    problems: list[str] = []
    for path in sorted(files, key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        if _needs_a_test(name, scripts) and path.resolve() not in tested:
            problems.append(f"{name} is imported by no test_*.py; add a test that imports it")
    return problems + modules.unreadable_problems()


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
    """A Python suite's cost in typical tests: one for each test, or its recorded seconds. A suite no policy can read
    costs one test; the policies that read it report it."""
    try:
        tests = len(defined_tests(parse_python(suite)))
    except UnreadablePython:
        tests = 1
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


def _class_tests(tree: ast.Module) -> list[tuple[str, bool]]:
    """Each Class.test a class body defines, named test* as unittest's loader finds them, and whether a block of the
    class body holds it, such as an `if sys.platform == "win32":`, so the loader finds it on some platforms only."""
    return [
        (f"{node.name}.{item.name}", item not in node.body)
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        for item in module_statements(node.body)
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and item.name.startswith("test")
    ]


def defined_tests(tree: ast.Module) -> list[str]:
    """Each Class.test the module defines in a class body, named test* as unittest's loader finds them, one in an if,
    try, or with block of the body included: the one reader of a suite's tests, for its cost, its shards, and the tests
    its recorded seconds name."""
    return sorted({test for test, _ in _class_tests(tree)})


def recorded_cost_problems(
    root: Path, recorded: Mapping[str, Mapping[str, float]] = RECORDED_TEST_SECONDS
) -> list[str]:
    """Report a recorded test cost that names no Python regression suite, a test the suite does not define, a test it
    defines on some platforms only, which tests/run_shard.py refuses where the loader does not find it, or a cost no
    heavier than a typical test."""
    problems: list[str] = []
    for name, tests in recorded.items():
        path = root / name
        if not (path.is_file() and is_test_script(path) and path.suffix == ".py"):
            problems.append(f"{name} records test costs but is not a regression suite")
            continue
        try:
            found = _class_tests(parse_python(path))
        except UnreadablePython as error:
            problems.append(f"{name} {error}")
            continue
        defined = {test for test, _ in found}
        conditional = {test for test, held in found if held}
        for test, seconds in tests.items():
            if test not in defined:
                problems.append(f"{name} records {test}, which it does not define")
            elif test in conditional:
                problems.append(
                    f"{name} records {test}, which a block of its class body holds, so the loader finds it on some "
                    "platforms only; record only a test every platform runs"
                )
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
        try:
            defined = defined_tests(parse_python(path))
        except UnreadablePython:
            continue
        shards = shard_runner.deal(defined, shard_count(path, seconds), seconds)
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
    suites = [path for path in regression_suites(root) if path.suffix.casefold() == ".py"]
    for _, name, tree in python_trees(suites, root, problems):
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


# The calls that load a module from a file by its path, by their last name, and the position and keyword of the path.
PATH_LOADERS = {
    "spec_from_file_location": (1, "location"),
    "SourceFileLoader": (1, "path"),
    "run_path": (0, "path_name"),
}
# The calls that import a module by its name, by their last name.
NAME_LOADERS = frozenset({"import_module", "__import__"})


class RepositoryModules:
    """The repository modules a Python file imports, resolved as they are at run time, and whether each runs git.

    A dotted import resolves beside the file, then from the repository root, as deployer.x, tools.x, and tests.x
    do; a relative import from the file's package; and a name each candidate resolves to in the mypy_path
    directories, which validation keeps equal to the directories code puts on sys.path. A standard-library name
    resolves beside the file only, since the interpreter finds its own module before any other directory's. A
    module named by a literal to importlib.import_module or __import__ is imported as a statement imports it, and a
    file loaded by its path, by spec_from_file_location, a SourceFileLoader, or runpy.run_path, is found by the
    literal end of the path: beside the file, at the root, or every repository file whose path ends so. A module
    runs git as skill_scripts.CommandReader reads it: a list or tuple that starts with git, a command runner or a
    lookup given git, or a GitClient. A file no policy can read imports nothing and runs nothing here, and
    unreadable_problems names it.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        pyproject = root / "pyproject.toml"
        configuration = tomllib.loads(pyproject.read_text(encoding="utf-8")) if pyproject.is_file() else {}
        entries = configuration.get("tool", {}).get("mypy", {}).get("mypy_path", [])
        self.path_directories = [root / entry.removeprefix(MYPY_PATH_PREFIX) for entry in entries]
        self._sources: dict[Path, str] = {}
        self._trees: dict[Path, ast.Module] = {}
        self._aliases: dict[Path, dict[str, str]] = {}
        self._resolvers: dict[Path, CallableResolver] = {}
        self._imports: dict[tuple[Path, bool], list[Path]] = {}
        self._resolved: dict[tuple[Path, str, int], list[Path]] = {}
        self._runs_git: dict[Path, bool] = {}
        self._unreadable: dict[Path, str] = {}

    def source(self, path: Path) -> str:
        if path not in self._sources:
            try:
                self._sources[path] = python_source(path)
            except UnreadablePython as error:
                self._unreadable[path] = str(error)
                self._sources[path] = ""
        return self._sources[path]

    def tree(self, path: Path) -> ast.Module:
        if path not in self._trees:
            try:
                self._trees[path] = parse_source(self.source(path), path.name)
            except UnreadablePython as error:
                self._unreadable[path] = str(error)
                self._trees[path] = ast.Module(body=[], type_ignores=[])
        return self._trees[path]

    def unreadable_problems(self) -> list[str]:
        """Each file read that no policy can read, by its name relative to the root."""
        return sorted(
            f"{path.resolve().relative_to(self.root.resolve()).as_posix()} {reason}"
            for path, reason in self._unreadable.items()
        )

    def aliases(self, path: Path) -> dict[str, str]:
        if path not in self._aliases:
            self._aliases[path] = import_aliases(self.tree(path))
        return self._aliases[path]

    def resolver(self, path: Path) -> CallableResolver:
        if path not in self._resolvers:
            self._resolvers[path] = CallableResolver(self.tree(path))
        return self._resolvers[path]

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
        elif parts and parts[0] in sys.stdlib_module_names:
            bases = [path.parent]
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

    def loaded_files(self, path: Path, unique: bool) -> list[Path]:
        """The repository files path loads by their path, each found by the literal end of the path it names: beside
        path, then at the root, then every repository file whose path ends so, or, when unique, only the one file."""
        tree = self.tree(path)
        names = name_values(tree)
        found: list[Path] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            loader = PATH_LOADERS.get(ast.unparse(node.func).rsplit(".", 1)[-1])
            if loader is None:
                continue
            parts = path_parts(call_argument(node, *loader), names)
            tail = list(itertools.takewhile(lambda part: part is not None, reversed(parts)))[::-1]
            if not tail or not str(tail[-1]).endswith(".py"):
                continue
            ending = "/".join(str(part) for part in tail)
            candidates = [base / ending for base in (path.parent, self.root) if (base / ending).is_file()][:1]
            if not candidates:
                candidates = [
                    file
                    for file in self.root.rglob(str(tail[-1]))
                    if f"/{file.relative_to(self.root).as_posix()}".endswith(f"/{ending}")
                ]
            if not unique or len(candidates) == 1:
                found += [candidate.resolve() for candidate in candidates]
        return found

    def imports(self, path: Path, unique_loads: bool = False) -> list[Path]:
        """The repository files path's imports may load, in the order they appear: import statements, literal names
        given importlib.import_module or __import__, and files loaded by path, every file a load may name, or, with
        unique_loads, only a load whose path names one file."""
        key = (path, unique_loads)
        if key in self._imports:
            return self._imports[key]
        found: list[Path] = []
        for node in ast.walk(self.tree(path)):
            if isinstance(node, ast.Import):
                names = [(alias.name, 0) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [(module, node.level)]
                names += [(f"{module}.{alias.name}" if module else alias.name, node.level) for alias in node.names]
            elif (
                isinstance(node, ast.Call)
                and ast.unparse(node.func).rsplit(".", 1)[-1] in NAME_LOADERS
                and isinstance(argument := call_argument(node, 0, "name"), ast.Constant)
                and isinstance(argument.value, str)
            ):
                names = [(argument.value, 0)]
            else:
                continue
            found += [file for name, level in names for file in self.resolve(path, name, level)]
        found += self.loaded_files(path, unique_loads)
        self._imports[key] = list(dict.fromkeys(found))
        return self._imports[key]

    def runs_git_itself(self, path: Path) -> bool:
        if path not in self._runs_git:
            # The reader finds git only in a string that starts with it or in GitClient, so a module without either
            # is not read, and a command list that starts with git answers before the reader resolves every name.
            source = self.source(path)
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
        TEST_CASE_BASES and GIT_TEST_BASE it reaches. A class defined in a module-level if, try, or with block is a
        module's class as a top-level one is."""
        tree = self.tree(path)
        definition = next(
            (node for node in module_statements(tree.body) if isinstance(node, ast.ClassDef) and node.name == name),
            None,
        )
        if definition is None or (path, name) in seen:
            return set()
        aliases = self.aliases(path)
        resolver = self.resolver(path)
        found: set[str] = set()
        for base in definition.bases:
            # A base is read through the names the module binds to one by assignment, as `Base = unittest.TestCase`,
            # and otherwise through its imports, as a class a module-level block defines twice is.
            qualified = resolver.name(base).removeprefix("<local>.") or qualified_name(base, aliases)
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
    for node in module_statements(tree.body):
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
    or a setUpModule that does not start with git_test_base.isolate_module(). A class or setUpModule defined in a
    module-level if, try, or with block is read as a top-level one is, since unittest finds it the same way.

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
    return problems + modules.unreadable_problems()


# What the validation pool's thread reads while the policy checks run on the main thread: it starts each job from a
# copy of os.environ and prints each result to sys.stdout, so a policy check that changes either reaches the pool.
RUNNER = "tests/run_validation.py"
SHARED_STATE_CALLS = frozenset(
    {
        "contextlib.redirect_stdout",
        "contextlib.redirect_stderr",
        "contextlib.chdir",
        "os.putenv",
        "os.unsetenv",
        "os.chdir",
        "os.fchdir",
    }
)
ENVIRONMENT_CHANGES = frozenset(
    {"update", "pop", "popitem", "clear", "setdefault", "__setitem__", "__delitem__", "__ior__"}
)
PATCHED_TARGETS = frozenset({*STANDARD_STREAMS, "os.environ"})
MOCK_PATCHES = ("unittest.mock.patch", "mock.patch")
PATCH_CALLS = frozenset(MOCK_PATCHES)
PATCH_DICT_CALLS = frozenset(f"{patch}.dict" for patch in MOCK_PATCHES)
PATCH_OBJECT_CALLS = frozenset({*(f"{patch}.object" for patch in MOCK_PATCHES), "setattr", "builtins.setattr"})
PATCH_MULTIPLE_CALLS = frozenset(f"{patch}.multiple" for patch in MOCK_PATCHES)
# The keyword arguments patch.multiple takes for itself; any other keyword names an attribute it replaces.
PATCH_MULTIPLE_OPTIONS = frozenset({"target", "spec", "create", "spec_set", "autospec", "new_callable"})


def _constant(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _name(node: ast.expr | None, resolver: CallableResolver) -> str:
    """The dotted name an expression stands for through the module's imports and the names it binds to one by a simple
    assignment, such as os.environ for `environment` after `environment = os.environ`, or for an assignment
    expression `(environment := os.environ)`; "" when it stands for none."""
    if isinstance(node, ast.NamedExpr):
        return _name(node.value, resolver)
    if isinstance(node, ast.Attribute):
        base = _name(node.value, resolver)
        return f"{base}.{node.attr}" if base else ""
    return resolver.name(node) if isinstance(node, ast.Name) else ""


def _stored(node: ast.expr) -> list[ast.expr]:
    """Each name, attribute, or subscript an assignment or deletion target stores to, through any unpacking."""
    if isinstance(node, (ast.Tuple, ast.List)):
        return [part for item in node.elts for part in _stored(item)]
    return _stored(node.value) if isinstance(node, ast.Starred) else [node]


def _changed_target(node: ast.expr, resolver: CallableResolver, imports: dict[str, str] | None) -> bool:
    """Whether an assignment or deletion target is a standard stream, os.environ, or an entry of os.environ.

    A name an assignment or deletion binds changes only the name, never what it stood for, so a bare name counts only
    for an augmented assignment, given its imports, which changes os.environ in place through `environ |= ...`.
    """
    if isinstance(node, ast.Subscript):
        return _name(node.value, resolver) == "os.environ"
    if isinstance(node, ast.Name):
        return imports is not None and qualified_name(node, imports) in PATCHED_TARGETS
    return _name(node, resolver) in PATCHED_TARGETS


def _replaced_attributes(call: ast.Call) -> list[str]:
    """The attributes a patch.multiple call replaces: its keywords, and the string keys of a `**` dictionary."""
    names = [found.arg for found in call.keywords if found.arg is not None]
    for found in call.keywords:
        if found.arg is None and isinstance(found.value, ast.Dict):
            names += [key for key in map(_constant, found.value.keys) if key]
    return [name for name in names if name not in PATCH_MULTIPLE_OPTIONS]


def _patched(call: ast.Call, called: str, resolver: CallableResolver) -> list[str]:
    """The dotted names a patch, patch.dict, patch.object, patch.multiple, or setattr call replaces, each target and
    attribute read by position or by keyword. A patch target is a string import path; a patch.dict or patch.multiple
    target is a string or an object; a patch.object or setattr target is an object."""
    if called in PATCH_CALLS:
        return [_constant(call_argument(call, 0, "target")) or ""]
    if called in PATCH_DICT_CALLS:
        dictionary = call_argument(call, 0, "in_dict")
        return [_constant(dictionary) or _name(dictionary, resolver)]
    if called in PATCH_OBJECT_CALLS:
        owner = _name(call_argument(call, 0, "target"), resolver)
        attribute = _constant(call_argument(call, 1, "attribute"))
        return [f"{owner}.{attribute}"] if owner and attribute else []
    if called in PATCH_MULTIPLE_CALLS:
        target = call_argument(call, 0, "target")
        base = _constant(target) or _name(target, resolver)
        return [f"{base}.{attribute}" for attribute in _replaced_attributes(call)] if base else []
    return []


def _shares_state(node: ast.AST, resolver: CallableResolver, imports: dict[str, str]) -> str:
    """What a node changes of the state the pool reads, or an empty string."""
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
        targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
        augmented = imports if isinstance(node, ast.AugAssign) else None
        changed = [
            ast.unparse(stored)
            for target in targets
            for stored in _stored(target)
            if _changed_target(stored, resolver, augmented)
        ]
        return f"changes {changed[0]}" if changed else ""
    if not isinstance(node, ast.Call):
        return ""
    called = _name(node.func, resolver)
    owner, _, method = called.rpartition(".")
    if called in SHARED_STATE_CALLS or (owner == "os.environ" and method in ENVIRONMENT_CHANGES):
        return f"calls {called}"
    patched = [name for name in _patched(node, called, resolver) if name in PATCHED_TARGETS]
    return f"patches {patched[0]}" if patched else ""


def shared_state_lines(tree: ast.Module) -> list[tuple[int, str]]:
    """Each line on which a module swaps a standard stream or changes os.environ or the working directory, and how,
    resolved through its imports and the names it binds to one by a simple assignment, as CallableResolver reads
    them."""
    resolver = CallableResolver(tree)
    imports = import_aliases(tree)
    found = {
        (node.lineno, change)
        for node in ast.walk(tree)
        if isinstance(node, (ast.stmt, ast.expr)) and (change := _shares_state(node, resolver, imports))
    }
    return sorted(found)


def policy_module_files(root: Path) -> list[Path]:
    """The runner and every module whose policy checks it runs in its own process, from POLICY_MODULES in the
    runner."""
    runner = root / RUNNER
    for node in parse_python(runner).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "POLICY_MODULES" for target in node.targets
        ):
            names = [item.id for item in getattr(node.value, "elts", []) if isinstance(item, ast.Name)]
            return [runner, *(root / "tests" / "validation" / f"{name}.py" for name in names)]
    raise AssertionError(f"{RUNNER} names no POLICY_MODULES")


def pool_shared_state_problems(root: Path) -> list[str]:
    """Report a policy check that swaps a standard stream, changes os.environ, or moves the working directory.

    The runner runs the policy checks on its main thread while the pool's thread starts jobs from os.environ and
    prints their results to sys.stdout, so such a change reaches a job that starts, or a line printed, meanwhile: a
    job gets a partial environment, or a result goes missing from the output a CI log keeps. Take the stream or the
    environment as a parameter instead, or move the check into a regression suite, which runs in its own process.
    """
    problems: list[str] = []
    for path in policy_module_files(root):
        if not path.is_file():
            problems.append(f"{path.relative_to(root).as_posix()}: a policy module {RUNNER} runs is missing")
            continue
        for _, name, tree in python_trees([path], root, problems):
            problems += [
                f"{name}:{line} {change}, which the validation pool reads while the policy checks run; take it as a "
                "parameter, or move the check into a regression suite"
                for line, change in shared_state_lines(tree)
            ]
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

    def test_no_policy_check_changes_what_the_pool_reads(self) -> None:
        self.assertEqual([], pool_shared_state_problems(REPOSITORY_ROOT))

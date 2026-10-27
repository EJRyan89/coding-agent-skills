"""Mutate the lines a pull request changes in its Python modules and report the mutants its suites leave alive.

Usage:
  python -B tools/mutation_gate.py [--base REF] [--minutes N]

The `Mutation` workflow runs it on every pull request, on Linux, where requirements-mutation.txt installs mutmut and
coverage.py; it is not a required check, and it calls no model and reads no secret. It mutates the lines the pull
request's commits add or change since the base (`origin/<GITHUB_BASE_REF>`, or origin/main, unless --base names
another), as `git diff -U0` names them, in each module under deployer/, tools/, a skill's scripts/, or a repository
skill's scripts/, leaving out suites, conftest.py, and deleted files. mutmut's operators make the mutants, string
literals included, each one mutation of the whole module, and mutmut's own line filter (the `covered_lines` of its
`create_mutations`) makes them only of a node that starts on a changed line. The gate keeps a mutant only when the
first line it changes is a changed line too, so a mutation anchored on an expression that starts on an unchanged line,
such as removing an argument from a call that opens above the change, is left out. A changed module whose changed lines
yield no mutant is named in the summary, never left out of it.

mutmut's own runner is not used: it records which tests reach a function only inside its pytest process and finds a
mutant by a module name it derives from the file's path, while the suites here reach most modules through the
subprocesses they start (deploy.py, a skill's script) and import a skill's or a tool's module by its bare name. So the
gate runs the suites as the validation runner does, `python -B -m pytest <suite>` from the root of a throwaway git
worktree at HEAD, one worktree per worker, with the mutant written into the worktree's copy of the module, where every
process the suite starts imports it.

Which suites run against a mutant is measured, not guessed. The suites that reach the changed modules, as
`job_selection.reached_suites` selects them for `--changed`, each run once unmutated under coverage.py, which records
the statements of the changed modules each suite executes, in its own process or in one it started with the
environment passed on, each process starting its measurement from the reach run's configuration file, as
COVERAGE_PROCESS_START names it; the gate's own coverage steps and suite runs are never measured.
coverage.py records a statement by its first line, and the lines its report names executed, missing, or excluded are the
first lines of all the module's statements, so each line up to the next of them belongs to the statement above it, and a
mutant on a continuation line of a multi-line statement is reached when that statement ran. coverage.py's
`test_function` dynamic context records, too, which test functions ran each statement, and a process the suite
started records, as its static context, the PYTEST_CURRENT_TEST pytest gave it: the node id of the test running when it
started, and the phase. `coverage json --show-contexts` reports them, which the gate matches to the node ids `pytest
--collect-only` lists for the suite. A mutant then runs the suites that executed its statement, fastest first, until
one fails: of each suite, only the tests that ran the statement, when its own test functions, or processes they started
while they ran, alone did, and the whole suite when anything else ran it too, since a set-up, a fixture, an import, or a
process started outside a test's call phase records no test of its own; a context naming no collected test of the
suite, or a test function name more than one collected class holds, which an inherited test method would be, and a
suite pytest cannot list run whole as well. The mutant is killed, or timed out when a suite
runs past three times its unmutated time. A mutant every one of those suites passes survived. A mutant in a statement no
suite executed is not reached, and a suite that fails unmutated, or whose coverage data `coverage combine` or `coverage
json` fails or times out on, or whose report holds no files object, is left out and named with the step and its
output, never read as a suite that reached nothing. The Bash and PowerShell suites run no Python module and are not
used.

A line outside every function's body runs when the module is imported, which records no test function, so it would run
every reaching suite whole. A mutant of a module-level assignment, such as a table or a compiled pattern, changes only
the value its names are bound to, so when only the module's functions read those names (no read at import, in a
decorator, a default, or an annotation, none as an attribute, a parameter, a `global`, or a string, and no other Python
file of the checkout naming one), it runs, of each suite, the tests that ran a line reading one, when only the suite's
own tests ran each such line it ran, and the whole suite otherwise. A suite that ran no reading line cannot
tell the mutant apart and is left out, unless no suite ran one, when every reaching suite runs whole, which still kills
a mutant that breaks the import.

A sanction table, a module-level assignment to one name ending in _ALLOWED such as FSOPS_ALLOWED, makes no mutant: no
suite of its module reads it, and the validation policy that owns it, which the gate does not run, fails an entry that
does not match what the module holds, so its mutants would all survive and spend the run. The policy is the module
under tests/validation, not a suite, that binds the table's name as a string to a module-level name ending in
ALLOWANCE, as FSOPS_ALLOWANCE = "FSOPS_ALLOWED"; the summary names each table skipped and its policy, and a changed
table no policy reads stops the gate, since nothing would check it.

When no changed line yields a mutant, no suite runs. Otherwise the run is bounded by --minutes (DEFAULT_MINUTES): the
reach pass, over the suites that reach a module with a mutant, takes at most half of it, and the mutants then run from
one queue that takes one mutant from each module in turn, each module's in line order, so a module late in the list
starts as soon as the first. A mutant the cap leaves untested is counted as not run, and a suite whose reach the cap
left unmeasured is named. Either leaves the run unfinished, which is no verdict: the summary's first line says so,
naming each module's mutants not run and how many of them lie on lines that run at import, and the count of unmeasured
suites. Each module's kill ratio is its killed and timed-out mutants over every mutant it tested.

While the mutants run, the log gets a line for each one a suite ran against and each one no suite reached, with its
outcome, module, line, and mutation. The report, each module's changed lines, counts, and kill ratio, each changed
module with no mutant, each surviving mutant by its line, the mutation, and the suites it ran against, each with the
count of its tests that ran when not all of them did, and each mutant not reached by its line and the mutation, goes to
stdout and to the step summary GITHUB_STEP_SUMMARY names. A pull request that changes no such module writes one line
saying so. It exits 0 once the gate has run to its end, whatever survived, and 1 when the run was unfinished, after its
summary and an error annotation repeating its first line, or when it cannot run: mutmut no longer makes the mutant its
canary expects on the canary's line alone, the reach run no longer measures its canary suite's own process and the
process the suite's test starts, each line named with that test, which a missing coverage.py .pth file would cause, a
changed sanction table is one no policy reads, or a git command fails.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import difflib
import functools
import importlib
import importlib.metadata
import io
import itertools
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "validation"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import json_reader
from bounded_process import run_bounded
from console import run_main
from duplication import SANCTION_SUFFIX
from flat_text import flat_text
from git_client import GitClient
from job_pool import append_step_summary, worker_count
from job_selection import host_job_seconds, reached_suites
from suite_discovery import pytest_command
from suite_names import is_suite_name

from deployer import platform_support

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MINUTES = 40
# The share of the run the reach pass may take, so the mutants always get the rest.
REACH_SHARE = 0.5
# A mutant's suite times out at this many times its unmutated seconds, and never sooner than the floor, so a mutant
# that makes a loop endless is killed rather than waited on.
SUITE_TIME_FACTOR = 3
MINIMUM_SUITE_SECONDS = 60.0
# How much of a suite's output is read back: the gate reports a suite's result, never its output.
OUTPUT_LIMIT = 4096
# How much of a failed coverage step's output its reason quotes: the end of it, where coverage.py names its error.
STEP_OUTPUT_CHARACTERS = 300

KILLED = "killed"
TIMED_OUT = "timed out"
SURVIVED = "survived"
NOT_REACHED = "not reached"
NOT_RUN = "not run"
PASSED = "passed"

# A mutant mutmut's operators must make, checked before every run, so a release that stops making it fails the gate
# rather than reporting fewer mutants.
CANARY_SOURCE = "def add(a):\n    return a + 1\n"
CANARY_MUTANT = "def add(a):\n    return a - 1\n"
# The canary's mutant must be made when its line is asked for and not when only another line is, so a release that
# stops honoring the line filter fails the gate rather than mutating whole modules again.
CANARY_LINE = frozenset({2})
CANARY_OTHER_LINE = frozenset({1})
# A suite whose reach every reach pass checks first: its test runs parent() in the suite's own process and child() in a
# process it starts, so a coverage.py release, an install layout, or an interpreter that no longer starts measuring
# from COVERAGE_PROCESS_START, or no longer names a child's lines with the test that started it, fails the gate rather
# than leaving every mutant not reached.
MEASUREMENT_MODULE = "canary.py"
MEASUREMENT_SOURCE = (
    "import sys\n\n\ndef parent():\n    return 1\n\n\ndef child():\n    return 2\n\n\n"
    'if __name__ == "__main__":\n    print(child())\n'
)
MEASUREMENT_SUITE = "test_canary.py"
MEASUREMENT_TESTS = (
    "import subprocess\nimport sys\nfrom pathlib import Path\n\nimport canary\n\n\n"
    "def test_canary():\n    assert canary.parent() == 1\n"
    '    subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("canary.py"))], check=True)\n'
)
# coverage.py's warnings name what a measured process did not import; a suite that checks a child's stderr would read
# them as its child's output, so none is printed.
COVERAGE_WARNINGS = (
    "no-data-collected",
    "module-not-imported",
    "module-not-measured",
    "module-not-python",
    "include-ignored",
    "already-imported",
    "couldnt-parse",
    "no-ctracer",
    "no-sysmon",
)
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
# The folder of the validation policies, each of which names the sanction table it reads as a module-level string
# bound to a name ending in POLICY_TABLE_SUFFIX, such as FSOPS_ALLOWANCE = "FSOPS_ALLOWED".
POLICY_FOLDER = "tests/validation"
POLICY_TABLE_SUFFIX = "ALLOWANCE"


@dataclass(frozen=True)
class Mutant:
    module: str
    line: int
    before: str
    after: str
    source: str
    # Whether the mutant's line runs when the module is imported, outside every function's body.
    at_import: bool = False
    # For a mutant of a module-level assignment whose names only the module's functions read, the lines that read
    # them; None when what the mutant changes is not bounded by such lines.
    readers: frozenset[int] | None = None


@dataclass(frozen=True)
class Reach:
    """A suite's unmutated run: its seconds, the lines of each changed module it executed, and, for each line only
    the suite's own tests executed, those tests' node ids; a line it executed that `tests` leaves out runs the
    whole suite."""

    seconds: float
    lines: Mapping[str, frozenset[int]]
    tests: Mapping[str, Mapping[int, frozenset[str]]] = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    mutant: Mutant
    status: str
    suites: tuple[str, ...] = ()
    # How many of each suite's tests ran, in the order of `suites`, 0 for a whole suite; a suite past the end ran whole.
    tests: tuple[int, ...] = ()


@dataclass
class ModuleReport:
    module: str
    outcomes: list[Outcome]
    cut_short: bool
    # How many lines the pull request adds or changes in the module.
    lines: int
    # Each sanction table holding a changed line, which made no mutant, with the policy that reads it.
    tables: tuple[tuple[str, str], ...] = ()


@dataclass
class Report:
    base: str
    minutes: int
    seconds: float
    workers: int
    mutmut: str
    modules: list[ModuleReport]
    failed_suites: dict[str, str]
    unmeasured_suites: list[str]


# Makes a module's mutants, each as the module's whole text, of the nodes that start on the given lines.
Generator = Callable[[str, str, frozenset[int]], list[str]]
# Runs a suite in a worktree within a time limit, only the tests whose node ids it is given when it is given any, and
# says whether they passed, failed (KILLED), or timed out.
SuiteRunner = Callable[[Path, str, float, Sequence[str]], str]
# Runs a suite unmutated in a worktree, measuring the changed modules, and gives its Reach or why it has none.
ReachRunner = Callable[[Path, str, Sequence[str], float], "Reach | str"]
Clock = Callable[[], float]
T = TypeVar("T")
# A module's count of changed lines, the mutants of those lines, and each changed sanction table with its policy.
Selection = tuple[int, list[Mutant], tuple[tuple[str, str], ...]]


def is_gate_module(path: str) -> bool:
    """Whether the gate mutates a changed file: a Python module under deployer/ or tools/, or in a skill's or a
    repository skill's scripts/, that is not a suite or a conftest.py."""
    pure = PurePosixPath(path)
    if pure.suffix != ".py" or is_suite_name(pure.name) or pure.name == "conftest.py":
        return False
    parts = pure.parts
    return (
        parts[0] in ("deployer", "tools")
        or (parts[0] == "skills" and len(parts) > 3 and parts[2] == "scripts")
        or (parts[:2] == (".claude", "skills") and len(parts) > 4 and parts[3] == "scripts")
    )


def changed_modules(root: Path, base: str, git: GitClient) -> list[str]:
    """The modules the gate mutates among the files the commits since the merge base with `base` change, deleted
    files left out."""
    names = git.output(["diff", "--name-only", "--no-renames", "--diff-filter=d", f"{base}...HEAD"], directory=root)
    return sorted(name for name in names.splitlines() if name and is_gate_module(name))


INT_PARSE_ALLOWED = {
    "changed_lines": "git writes a hunk header's numbers, and a diff prefixes each line of a file's text",
}


def changed_lines(diff: str) -> set[int]:
    """The new side's line numbers each hunk of a zero-context diff adds or changes."""
    lines: set[int] = set()
    for match in HUNK.finditer(diff):
        start, count = int(match.group(1)), int(match.group(2) or "1")
        lines.update(range(start, start + count))
    return lines


def describe(original: str, mutated: str) -> tuple[int, str, str]:
    """The first line a mutant changes, and that line's text before and after, each flattened to one line."""
    old, new = original.splitlines(), mutated.splitlines()
    for tag, first, last, new_first, new_last in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag != "equal":
            return first + 1, flat_text(" ".join(old[first:last])), flat_text(" ".join(new[new_first:new_last]))
    raise ValueError("the mutant is the module unchanged")


def function_body_lines(tree: ast.Module) -> frozenset[int]:
    """The lines of every function's body, which run when the function is called rather than at import."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            lines.update(range(node.body[0].lineno, (node.body[-1].end_lineno or node.body[-1].lineno) + 1))
    return frozenset(lines)


def top_statement(tree: ast.Module, line: int) -> ast.stmt | None:
    """The statement of the module's body that holds a line, or None for a line between them, such as a decorator's,
    which no assignment holds."""
    for statement in tree.body:
        if statement.lineno <= line <= (statement.end_lineno or statement.lineno):
            return statement
    return None


def bound_names(statement: ast.stmt) -> frozenset[str]:
    """The names a module-level assignment binds, or none when it is not one or also stores into an attribute or an
    item, which changes an object some other name holds."""
    if isinstance(statement, ast.Assign):
        targets = statement.targets
    elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
        targets = [statement.target]
    else:
        return frozenset()
    names = set()
    for node in (node for target in targets for node in ast.walk(target)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif not isinstance(node, ast.Tuple | ast.List | ast.Starred | ast.expr_context):
            return frozenset()
    return frozenset(names)


def sanction_table(statement: ast.stmt | None) -> str | None:
    """The name a module-level sanction table binds, an assignment to one name ending in SANCTION_SUFFIX, such as
    FSOPS_ALLOWED; None for any other statement."""
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target = statement.targets[0]
    elif isinstance(statement, ast.AnnAssign):
        target = statement.target
    else:
        return None
    return target.id if isinstance(target, ast.Name) and target.id.endswith(SANCTION_SUFFIX) else None


def changed_tables(tree: ast.Module, changed: frozenset[int]) -> list[str]:
    """The sanction tables of a module's body that hold a changed line, in line order."""
    return [
        name
        for statement in tree.body
        if (name := sanction_table(statement))
        and any(statement.lineno <= line <= (statement.end_lineno or statement.lineno) for line in changed)
    ]


def policy_tables(tree: Path) -> dict[str, str]:
    """Each sanction table name a validation policy reads, with the policy's path: a module under POLICY_FOLDER, not a
    suite, that binds the name as a string at module level to a name ending in POLICY_TABLE_SUFFIX."""
    found: dict[str, str] = {}
    for path in sorted((tree / POLICY_FOLDER).glob("*.py")):
        if is_suite_name(path.name):
            continue
        for statement in ast.parse(path.read_bytes().decode("utf-8"), str(path)).body:
            if (
                isinstance(statement, ast.Assign)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
                and statement.value.value.endswith(SANCTION_SUFFIX)
                and any(
                    isinstance(target, ast.Name) and target.id.endswith(POLICY_TABLE_SUFFIX)
                    for target in statement.targets
                )
            ):
                found.setdefault(statement.value.value, f"{POLICY_FOLDER}/{path.name}")
    return found


def unread_tables(module: str, tables: Iterable[str], policies: Mapping[str, str]) -> str | None:
    """Why the gate cannot run when a changed sanction table is one no policy reads, which no mutant of it would be
    checked by; None when a policy reads each."""
    unread = [name for name in tables if name not in policies]
    if not unread:
        return None
    names = ", ".join(unread)
    return (
        f"{module} changes {names}, which no validation policy under {POLICY_FOLDER} reads, so the gate would leave "
        f"it unchecked: add the policy that reads it, or rename the table so its name does not end in {SANCTION_SUFFIX}"
    )


class NameReaders(ast.NodeVisitor):
    """The lines that load any of some names, and whether any load runs at import: outside a function's body, in a
    decorator, a default, or an annotation."""

    def __init__(self, names: frozenset[str]) -> None:
        self.names = names
        self.lines: set[int] = set()
        # The lines of the loads that run at import.
        self.at_import: set[int] = set()
        self.depth = 0

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self.names and isinstance(node.ctx, ast.Load):
            (self.lines if self.depth else self.at_import).add(node.lineno)

    def visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        for part in [*getattr(node, "decorator_list", ()), node.args, getattr(node, "returns", None)]:
            if part is not None:
                self.visit(part)
        self.depth += 1
        for statement in node.body if isinstance(node.body, list) else [node.body]:
            self.visit(statement)
        self.depth -= 1

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = visit_function


def docstrings(tree: ast.Module) -> set[int]:
    """The ids of the module's, its classes', and its functions' docstring nodes, which no code reads by name."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def reached_otherwise(tree: ast.Module, source: str, names: frozenset[str]) -> bool:
    """Whether the module may reach a name other than by loading it as a variable: as an attribute, a parameter, a
    keyword, an import alias, a `global` statement, or a string, such as `globals()` or `getattr` would take."""
    loaded = {(node.lineno, node.col_offset) for node in ast.walk(tree) if isinstance(node, ast.Name)}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.NAME and token.string in names and token.start not in loaded:
            return True
    words = re.compile(rf"\b(?:{'|'.join(map(re.escape, sorted(names)))})\b")
    skipped = docstrings(tree)
    return any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skipped
        and words.search(node.value)
        for node in ast.walk(tree)
    )


def name_readers(
    tree: ast.Module, source: str, statement: ast.stmt, mentioned_elsewhere: Callable[[str], bool]
) -> frozenset[int] | None:
    """For a statement of the module's body that assigns, the lines that read a name it binds, when only the module's
    functions read them: none at import, nothing reached any other way, and no other file of the repository naming
    one, which an import of it or a test of the table would. None otherwise.

    The mutant changes only the value the names are bound to, so only a line that reads one can tell it apart, and the
    tests that ran those lines are the tests that can kill it."""
    names = bound_names(statement)
    if not names or any(mentioned_elsewhere(name) for name in names) or reached_otherwise(tree, source, names):
        return None
    readers = NameReaders(names)
    readers.visit(tree)
    return None if readers.at_import else frozenset(readers.lines)


def module_mutants(
    module: str,
    source: str,
    changed: frozenset[int],
    generate: Generator,
    mentioned_elsewhere: Callable[[str], bool] = lambda name: True,
) -> list[Mutant]:
    """Every distinct mutant of a module whose first changed line is one the pull request changed, in line order, each
    marked when its line runs at import, with the lines that read what a module-level assignment binds.

    A line of a sanction table makes none: the module's suites never read the table, and the validation policy that
    owns it, which the gate does not run, fails an entry that does not match what the module holds."""
    if not changed:
        return []
    tree = ast.parse(source)
    bodies = function_body_lines(tree)
    # Each statement's readers, by its first line, since every mutant of a table shares them.
    readers: dict[int, frozenset[int] | None] = {}
    mutants = []
    for text in dict.fromkeys(generate(module, source, changed)):
        if text == source:
            continue
        line, before, after = describe(source, text)
        if line in changed:
            # A line in a function's body lies in a top statement that defines a function or a class, which binds
            # no name an assignment would, so it gets no readers.
            statement = top_statement(tree, line)
            if sanction_table(statement):
                continue
            if statement and statement.lineno not in readers:
                readers[statement.lineno] = name_readers(tree, source, statement, mentioned_elsewhere)
            found = readers[statement.lineno] if statement else None
            mutants.append(Mutant(module, line, before, after, text, line not in bodies, found))
    return sorted(mutants, key=lambda mutant: mutant.line)


def mutmut_mutants(module: str, source: str, lines: frozenset[int]) -> list[str]:
    """Each mutant mutmut's operators make of a module's nodes that start on `lines`, as the module's whole text with
    that one mutation.

    mutmut reads its configuration, [tool.mutmut] in pyproject.toml, from the working directory, so the mutants are
    made from the repository's root."""
    file_mutation = importlib.import_module("mutmut.mutation.file_mutation")
    with contextlib.chdir(REPOSITORY_ROOT):
        tree, mutations, _, _ = file_mutation.create_mutations(module, source, covered_lines=set(lines))
    return [tree.deep_replace(mutation.original_node, mutation.mutated_node).code for mutation in mutations]


def generator_problem(generate: Generator) -> str | None:
    """Why the generator cannot be trusted, or None when it makes the canary's mutant on its line and only there."""
    if CANARY_MUTANT not in generate("canary.py", CANARY_SOURCE, CANARY_LINE):
        return f"the mutant generator no longer turns {CANARY_SOURCE.strip()!r} into {CANARY_MUTANT.strip()!r}"
    if CANARY_MUTANT in generate("canary.py", CANARY_SOURCE, CANARY_OTHER_LINE):
        return "the mutant generator mutated a line it was not given, so it no longer keeps to the changed lines"
    return None


def measurement_problem(reach: ReachRunner) -> str | None:
    """Why the reach run cannot be trusted, or None when it measured the canary suite's own process and the process its
    test started, and named each one's line with that test."""
    expected = frozenset({f"{MEASUREMENT_SUITE}::test_canary"})
    with tempfile.TemporaryDirectory(prefix="mutation-canary-") as temporary:
        tree = Path(temporary)
        # The texts are ASCII, so their bytes are the same in every encoding a locale could choose.
        (tree / MEASUREMENT_MODULE).write_bytes(MEASUREMENT_SOURCE.encode())
        (tree / MEASUREMENT_SUITE).write_bytes(MEASUREMENT_TESTS.encode())
        result = reach(tree, MEASUREMENT_SUITE, [MEASUREMENT_MODULE], MINIMUM_SUITE_SECONDS)
    if isinstance(result, str):
        return f"the reach run's canary suite {result}, so no suite's reach can be measured"
    tests = result.tests.get(MEASUREMENT_MODULE, {})
    # parent()'s and child()'s return lines.
    for line, where in ((5, "the suite's own process"), (9, "a process its test started")):
        if tests.get(line) != expected:
            return f"the reach run no longer names the line {where} ran with the canary suite's test"
    return None


def suite_environment() -> dict[str, str]:
    """This process's environment for a suite or a coverage step: no bytecode written, so a mutant the same size as the
    module and written within the same second is never read from a stale cache, and no coverage measurement, which a
    process inheriting coverage.py's subprocess setting starts from coverage.py's .pth file, importing the installed
    coverage.py before `-m coverage` is resolved."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("COVERAGE_PROCESS_")}
    return {**environment, "PYTHONDONTWRITEBYTECODE": "1"}


def run_suite(tree: Path, suite: str, timeout: float, tests: Sequence[str] = ()) -> str:
    """Run one suite in a worktree, or only its tests named by node id, stopping at the first failure, with every
    process it starts ended after it. The node ids go to pytest in an argument file, which no command line's length
    limits."""
    with tempfile.TemporaryDirectory(prefix="mutation-tests-") as temporary:
        target = suite
        if tests:
            listed = Path(temporary) / "tests.txt"
            listed.write_text("".join(f"{test}\n" for test in tests), encoding="utf-8")
            target = f"@{listed}"
        try:
            finished = run_bounded(
                pytest_command(target, "-x", "-q"),
                timeout,
                cwd=tree,
                env=suite_environment(),
                output_limit=OUTPUT_LIMIT,
                tree=platform_support.process_tree(),
            )
        except subprocess.TimeoutExpired:
            return TIMED_OUT
    return PASSED if finished.returncode == 0 else KILLED


def coverage_settings(folder: Path, files: Iterable[Path]) -> str:
    """coverage.py's configuration for one reach run: data in its own folder, one file per measured process, and each
    line recorded with the test function that ran it, or, in a process the suite started, with the test pytest was
    running when it started.

    Every process, the suite's own included, reads this file when it starts, as COVERAGE_PROCESS_START names it, so
    each expands PYTEST_CURRENT_TEST from the environment it was started with: unset in the suite's own process, and
    the node id and phase of the running test in a child. coverage.py's `patch = subprocess` is not used, since it
    hands each child the parent's configuration already expanded, which takes precedence over the file."""
    return "\n".join(
        [
            "[run]",
            f"data_file = {folder / 'data'}",
            "parallel = true",
            "dynamic_context = test_function",
            "context = ${PYTEST_CURRENT_TEST-}",
            "include =",
            *(f"    {file}" for file in files),
            f"disable_warnings = {', '.join(COVERAGE_WARNINGS)}",
            "",
        ]
    )


def executed_statements(entry: Mapping[str, Iterable[int]], last: int) -> Iterator[tuple[int, range]]:
    """Each statement a coverage.py JSON report's entry for a module says ran, as its first line and all its lines, up
    to the module's last.

    coverage.py records a statement by its first line alone, and the lines it reports executed, missing, or excluded
    are the first lines of all its statements, so each line from an executed statement's first line up to the next
    statement's belongs to it: a continuation line of a multi-line call, argument list, or literal among them."""
    executed = {int(line) for line in entry.get("executed_lines", ())}
    starts = executed | {int(line) for key in ("missing_lines", "excluded_lines") for line in entry.get(key, ())}
    for start, end in itertools.pairwise([*sorted(starts), max([last, *starts]) + 1]):
        if start in executed:
            yield start, range(start, end)


def statement_lines(entry: Mapping[str, Iterable[int]], last: int) -> frozenset[int]:
    """Every line of each statement a coverage.py JSON report's entry for a module says ran, up to the module's last."""
    return frozenset(line for _, lines in executed_statements(entry, last) for line in lines)


def module_entries(
    report: Mapping[str, object], tree: Path, modules: Sequence[str]
) -> Iterator[tuple[str, Mapping[str, Any], int]]:
    """Each changed module's entry in a coverage.py JSON report and the module's line count, its file named by any
    path into the worktree."""
    files = report.get("files")
    for name, entry in files.items() if isinstance(files, dict) else ():
        path = Path(name) if Path(name).is_absolute() else tree / name
        try:
            module = path.resolve().relative_to(tree.resolve()).as_posix()
        except ValueError:
            continue
        if module in modules and isinstance(entry, dict):
            yield module, entry, len((tree / module).read_bytes().splitlines())


def executed_lines(report: Mapping[str, object], tree: Path, modules: Sequence[str]) -> dict[str, frozenset[int]]:
    """The lines of each module a coverage.py JSON report says ran, every line of a multi-line statement included."""
    lines = {module: frozenset[int]() for module in modules}
    for module, entry, last in module_entries(report, tree, modules):
        lines[module] = statement_lines(entry, last)
    return lines


def collected_tests(listing: str, suite: str) -> list[str]:
    """The node ids of a suite's tests that `pytest <suite> --collect-only -q` lists."""
    return [line for line in listing.splitlines() if line.startswith(f"{suite}::")]


def context_tests(contexts: Iterable[str], suite: str, collected: Sequence[str]) -> frozenset[str] | None:
    """The node ids of the suite's tests that coverage.py's `test_function` contexts for a statement name, or None
    when the statement must run the whole suite.

    A context names a test function by its module's import name and its qualified name. A process the suite started
    names, instead, the node id of the test pytest was running when it started and the phase, and only the call phase
    is that test's alone: a set-up or a tear-down may be a fixture's that other tests share. The empty context is code
    that ran outside every test function: at import, in a set-up or a fixture, or in a process started outside a test,
    and only the whole suite repeats it. A test method a class inherits is named by the class that defines it, so a
    context whose function name more than one collected class holds, or one that names no collected test of this
    suite, runs the whole suite too."""
    stem = PurePosixPath(suite).stem
    listed = set(collected)
    named: dict[str, list[str]] = {}
    for test in collected:
        named.setdefault(test.removeprefix(f"{suite}::").split("[", 1)[0].replace("::", "."), []).append(test)
    functions = Counter(name.rpartition(".")[2] for name in named)
    found: set[str] = set()
    for context in contexts:
        started = context.removesuffix(" (call)")
        if started != context and started in listed:
            found.add(started)
            continue
        module, _, name = context.partition(f"{stem}.")
        if module and not module.endswith("."):
            return None
        if name not in named or functions[name.rpartition(".")[2]] != 1:
            return None
        found.update(named[name])
    return frozenset(found) or None


def executed_tests(
    report: Mapping[str, object], tree: Path, modules: Sequence[str], suite: str, collected: Sequence[str]
) -> dict[str, dict[int, frozenset[str]]]:
    """For each line of each module whose statement a `coverage json --show-contexts` report says only the suite's own
    tests ran, in their functions or in processes their calls started, those tests' node ids."""
    tests: dict[str, dict[int, frozenset[str]]] = {module: {} for module in modules}
    for module, entry, last in module_entries(report, tree, modules):
        contexts = entry.get("contexts")
        recorded = contexts if isinstance(contexts, dict) else {}
        for start, lines in executed_statements(entry, last):
            found = context_tests(recorded.get(str(start), ()), suite, collected)
            if found is not None:
                tests[module].update(dict.fromkeys(lines, found))
    return tests


def coverage_reach(tree: Path, suite: str, modules: Sequence[str], timeout: float) -> Reach | str:
    """Run a suite unmutated under coverage.py and give the lines of the changed modules it executed and the tests that
    ran each, or why not."""
    python = [sys.executable, "-B", "-m"]
    with tempfile.TemporaryDirectory(prefix="mutation-reach-") as temporary:
        folder = Path(temporary)
        settings = folder / "coveragerc"
        settings.write_text(coverage_settings(folder, (tree / module for module in modules)), encoding="utf-8")
        rcfile = f"--rcfile={settings}"
        # coverage.py's .pth file starts the measurement of the suite's process, and of each process it starts, from
        # the file. A test that runs the gate has its own PYTEST_CURRENT_TEST left out, so the suite's process gets
        # no static context.
        measured = {key: value for key, value in suite_environment().items() if key != "PYTEST_CURRENT_TEST"}
        measured["COVERAGE_PROCESS_START"] = str(settings)
        started = time.monotonic()
        try:
            finished = run_bounded(
                pytest_command(suite, "-q"),
                timeout,
                cwd=tree,
                env=measured,
                output_limit=OUTPUT_LIMIT,
                tree=platform_support.process_tree(),
            )
        except subprocess.TimeoutExpired:
            return "timed out unmutated"
        seconds = time.monotonic() - started
        if finished.returncode != 0:
            return f"failed unmutated with exit code {finished.returncode}"
        report = folder / "report.json"
        for command in (["combine", rcfile, "-q"], ["json", rcfile, "-q", "--show-contexts", "-o", str(report)]):
            step = f"coverage {command[0]}"
            try:
                done = run_bounded(
                    [*python, "coverage", *command],
                    MINIMUM_SUITE_SECONDS,
                    cwd=tree,
                    env=suite_environment(),
                    output_limit=OUTPUT_LIMIT,
                )
            except subprocess.TimeoutExpired:
                return f"timed out in {code(step)}"
            if done.returncode != 0:
                output = flat_text((done.stderr + done.stdout).decode("utf-8", "replace"))[-STEP_OUTPUT_CHARACTERS:]
                return f"failed in {code(step)} with exit code {done.returncode}" + (
                    f": {code(output)}" if output else ""
                )
        if not report.is_file():
            return f"failed in {code('coverage json')}, which wrote no report"
        recorded = json_reader.read_file(report)
        if not isinstance(recorded, dict) or not isinstance(recorded.get("files"), dict):
            return f"failed in {code('coverage json')}, whose report holds no files object"
        collected = suite_tests(tree, suite, folder / "collected.txt")
        return Reach(
            seconds,
            executed_lines(recorded, tree, modules),
            executed_tests(recorded, tree, modules, suite, collected) if collected else {},
        )


def suite_tests(tree: Path, suite: str, listing: Path) -> list[str]:
    """The node ids of a suite's tests, or none when pytest cannot list them, which runs the whole suite against every
    mutant. The listing is written to a file, so no output limit cuts it short."""
    try:
        with listing.open("wb") as output:
            finished = run_bounded(
                pytest_command(suite, "--collect-only", "-q"),
                MINIMUM_SUITE_SECONDS,
                stdout=output,
                cwd=tree,
                env=suite_environment(),
                output_limit=OUTPUT_LIMIT,
                tree=platform_support.process_tree(),
            )
    except subprocess.TimeoutExpired:
        return []
    if finished.returncode != 0:
        return []
    return collected_tests(listing.read_text(encoding="utf-8", errors="replace"), suite)


# The cleanup whose git results are left unread, with the reason.
RESULT_DISCARD_ALLOWED = {
    "worktrees": "removes the throwaway worktrees in a finally block, where a failure raised would replace the run's "
    "own outcome; they lie in a temporary directory that is deleted afterwards, so any later git worktree prune drops "
    "the record of one whose removal failed",
}


@contextlib.contextmanager
def worktrees(root: Path, count: int, folder: Path, git: GitClient) -> Iterator[list[Path]]:
    """`count` throwaway worktrees of HEAD under `folder`, removed afterwards whatever a suite left in them."""
    made: list[Path] = []
    try:
        for index in range(count):
            path = folder / f"tree-{index + 1}"
            git.output(["worktree", "add", "--detach", "--quiet", str(path), "HEAD"], directory=root)
            made.append(path)
        yield made
    finally:
        for path in made:
            git.run(["worktree", "remove", "--force", str(path)], directory=root)
        git.run(["worktree", "prune"], directory=root)


class Workers:
    """The worktrees, each lent to one task at a time, and the threads that run the tasks."""

    def __init__(self, trees: Sequence[Path], clock: Clock = time.monotonic) -> None:
        self.free: queue.Queue[Path] = queue.Queue()
        for tree in trees:
            self.free.put(tree)
        self.count = len(trees)
        self.clock = clock
        self.lock = threading.Lock()

    @contextlib.contextmanager
    def tree(self) -> Iterator[Path]:
        tree = self.free.get()
        try:
            yield tree
        finally:
            self.free.put(tree)

    def map(self, task: Callable[[Path, int], T], items: int) -> list[T]:
        """Each task's result, in order, each run with a worktree no other task holds meanwhile."""

        def lent(index: int) -> T:
            with self.tree() as tree:
                return task(tree, index)

        with ThreadPoolExecutor(max_workers=self.count) as pool:
            return list(pool.map(lent, range(items)))


def measure_reach(
    workers: Workers, suites: Sequence[str], modules: Sequence[str], deadline: float, reach: ReachRunner
) -> tuple[dict[str, Reach], dict[str, str], list[str]]:
    """Each suite's unmutated reach, the longest-recorded suites first, and the suites that failed unmutated or that
    the reach pass's share of the run left unmeasured."""
    recorded = host_job_seconds()
    ordered = sorted(suites, key=lambda suite: (-recorded.get(suite, 0.0), suite))

    def measure(tree: Path, index: int) -> Reach | str | None:
        left = deadline - workers.clock()
        if left <= 0:
            return None
        result = reach(tree, ordered[index], modules, left)
        if isinstance(result, str) and workers.clock() >= deadline:
            return None
        print(f"reach {ordered[index]}: {result if isinstance(result, str) else 'measured'}", flush=True)
        return result

    results = dict(zip(ordered, workers.map(measure, len(ordered)), strict=True))
    measured = {suite: result for suite, result in results.items() if isinstance(result, Reach)}
    failed = {suite: result for suite, result in results.items() if isinstance(result, str)}
    return measured, failed, [suite for suite, result in results.items() if result is None]


def covering_suites(mutant: Mutant, reach: Mapping[str, Reach]) -> list[str]:
    """The suites that executed a mutant's line unmutated, fastest first."""
    found = [suite for suite, measured in reach.items() if mutant.line in measured.lines.get(mutant.module, ())]
    return sorted(found, key=lambda suite: (reach[suite].seconds, suite))


def suite_selection(mutant: Mutant, measured: Reach) -> list[str] | None:
    """The node ids of a suite's tests to run against a mutant, none for the whole suite, or None when the suite ran
    none of the lines that read what the mutant's module-level assignment binds.

    The tests are those that ran the mutant's line when only the suite's own tests did, else, for a mutant
    with readers, those that ran a reading line, when only the suite's own tests ran each reading line it
    ran."""
    tests = measured.tests.get(mutant.module, {})
    if mutant.line in tests:
        return sorted(tests[mutant.line])
    if mutant.readers is None:
        return []
    # A suite is asked only once it executed the mutant's line, so it holds the module.
    executed = measured.lines[mutant.module]
    found: set[str] = set()
    for line in sorted(mutant.readers & executed):
        if line not in tests:
            return []
        found.update(tests[line])
    return sorted(found) if found else None


def suite_plan(mutant: Mutant, reach: Mapping[str, Reach]) -> list[tuple[str, list[str]]]:
    """Each suite to run against a mutant, fastest first, with the tests of it to run, none for the whole suite.

    A suite that ran no line reading what a module-level assignment binds cannot tell the mutant apart, so it is left
    out when another suite ran one. When none did, every suite that reached the line runs whole, which still kills a
    mutant that breaks the import."""
    planned = [(suite, suite_selection(mutant, reach[suite])) for suite in covering_suites(mutant, reach)]
    if any(tests is not None for _, tests in planned):
        return [(suite, tests) for suite, tests in planned if tests is not None]
    return [(suite, []) for suite, _ in planned]


def try_mutant(
    tree: Path, mutant: Mutant, reach: Mapping[str, Reach], deadline: float, run: SuiteRunner, clock: Clock
) -> Outcome:
    """Write a mutant into a worktree, run the suites that reach its line until one fails, each with the tests
    `suite_plan` selects, and put the module back."""
    plan = suite_plan(mutant, reach)
    if not plan:
        return Outcome(mutant, NOT_REACHED)
    path = tree / mutant.module
    original = path.read_bytes()
    path.write_text(mutant.source, encoding="utf-8", newline="")
    ran: list[str] = []
    tests: list[int] = []
    try:
        for suite, selected in plan:
            if clock() >= deadline:
                return Outcome(mutant, NOT_RUN, tuple(ran), tuple(tests))
            ran.append(suite)
            tests.append(len(selected))
            timeout = max(MINIMUM_SUITE_SECONDS, SUITE_TIME_FACTOR * reach[suite].seconds)
            result = run(tree, suite, timeout, selected)
            if result != PASSED:
                return Outcome(mutant, result, tuple(ran), tuple(tests))
        return Outcome(mutant, SURVIVED, tuple(ran), tuple(tests))
    finally:
        path.write_bytes(original)


def queue_order(selected: Iterable[Sequence[Mutant]]) -> list[Mutant]:
    """Every module's mutants in one queue, one from each module in turn, so no module waits for another's to finish."""
    return [mutant for turn in itertools.zip_longest(*selected) for mutant in turn if mutant is not None]


def try_mutants(
    workers: Workers, mutants: Sequence[Mutant], reach: Mapping[str, Reach], deadline: float, run: SuiteRunner
) -> list[Outcome]:
    """Each mutant's outcome, those the deadline leaves untested counted as not run."""

    def attempt(tree: Path, index: int) -> Outcome:
        mutant = mutants[index]
        if workers.clock() >= deadline:
            return Outcome(mutant, NOT_RUN)
        outcome = try_mutant(tree, mutant, reach, deadline, run, workers.clock)
        if outcome.suites or outcome.status == NOT_REACHED:
            print(f"{outcome.status:<11} {mutant.module}:{mutant.line} {mutant.before} -> {mutant.after}", flush=True)
        return outcome

    return workers.map(attempt, len(mutants))


def run_deadline(started: float, minutes: int) -> float:
    """When the run's cap is reached, in the clock's seconds."""
    return started + minutes * 60


def reach_deadline(started: float, deadline: float) -> float:
    """When the reach pass stops starting suites: once it has taken its share of the run."""
    return started + REACH_SHARE * (deadline - started)


def counts(outcomes: Iterable[Outcome]) -> dict[str, int]:
    found = dict.fromkeys((KILLED, TIMED_OUT, SURVIVED, NOT_REACHED, NOT_RUN), 0)
    for outcome in outcomes:
        found[outcome.status] += 1
    return found


def kill_ratio(found: Mapping[str, int]) -> str:
    """Killed and timed-out mutants over every mutant tested, as a percentage and the counts it is made of."""
    killed = found[KILLED] + found[TIMED_OUT]
    tested = killed + found[SURVIVED]
    return f"{100 * killed / tested:.0f}% ({killed} of {tested})" if tested else "none tested"


def code(text: str) -> str:
    """Text as a Markdown code span, fenced by one more backtick than the longest run of them it holds."""
    fence = "`" * (max((len(run) for run in re.findall("`+", text)), default=0) + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def survivor_lines(module: ModuleReport) -> list[str]:
    survivors = [outcome for outcome in module.outcomes if outcome.status == SURVIVED]
    if not survivors:
        return []
    noun = "mutant" if len(survivors) == 1 else "mutants"
    lines = ["", f"### `{module.module}`: {len(survivors)} surviving {noun}", ""]
    for outcome in survivors:
        mutant = outcome.mutant
        which = "ran the line" if mutant.readers is None else "ran the line or read a name it binds"
        named = []
        for index, suite in enumerate(outcome.suites):
            count = outcome.tests[index] if index < len(outcome.tests) else 0
            noun = "test" if count == 1 else "tests"
            named.append(f"`{suite}`" + (f" ({count} {noun} that {which})" if count else ""))
        suites = ", ".join(named)
        lines.append(f"- Line {mutant.line}: {code(mutant.before)} became {code(mutant.after)}; passed {suites}")
    return lines


def unreached_lines(module: ModuleReport) -> list[str]:
    """Each of a module's mutants that no suite executed unmutated, by its line and the mutation, so that its author
    need not infer the line from the gaps between the mutants that ran."""
    unreached = [outcome.mutant for outcome in module.outcomes if outcome.status == NOT_REACHED]
    if not unreached:
        return []
    noun = "mutant" if len(unreached) == 1 else "mutants"
    lines = ["", f"### `{module.module}`: {len(unreached)} {noun} not reached", ""]
    lines.extend(
        f"- Line {mutant.line}: {code(mutant.before)} became {code(mutant.after)}; no suite executed its statement"
        for mutant in unreached
    )
    return lines


def empty_selection_line(module: ModuleReport) -> str:
    """Why a changed module has no mutant, said so that a selection empty by mistake is seen."""
    if not module.lines:
        return f"`{module.module}` keeps no line this pull request added or changed, so it has nothing to mutate."
    noun = "line" if module.lines == 1 else "lines"
    return (
        f"`{module.module}`: mutmut made no mutant on the {module.lines} {noun} this pull request changed, so none ran."
    )


def table_line(module: ModuleReport) -> str:
    """The sanction tables whose changed lines made no mutant, each named with the validation policy that holds it."""
    held = ", ".join(f"`{name}` (`{policy}`)" for name, policy in module.tables)
    return (
        f"`{module.module}`: the changed lines of its sanction tables made no mutant, since the validation policy "
        f"that reads each holds it, not the module's suites: {held}."
    )


def unfinished_line(report: Report) -> str | None:
    """The line that leads the summary of a run the cap cut short, naming each module's mutants not run, how many of
    them run at import, and the suites whose reach was not measured; None for a finished run."""
    parts = []
    for module in report.modules:
        left = [outcome for outcome in module.outcomes if outcome.status == NOT_RUN]
        if left:
            at_import = sum(outcome.mutant.at_import for outcome in left)
            parts.append(
                f"{len(left)} of the {len(module.outcomes)} mutants of `{module.module}` did not run"
                + (f" ({at_import} on lines that run at import)" if at_import else "")
            )
    if report.unmeasured_suites:
        count = len(report.unmeasured_suites)
        parts.append(f"the reach of {count} {'suite' if count == 1 else 'suites'} was not measured")
    if not parts:
        return None
    return (
        f"**Unfinished, so not a verdict:** the {report.minutes}-minute cap cut the run short: {'; '.join(parts)}. "
        "Split the pull request, or move its module-level tables into the functions that use them, until it finishes."
    )


def summary(report: Report) -> str:
    """The Markdown the run's summary page shows: a first line saying so when the cap cut the run short, then each
    module's changed lines, counts, and kill ratio, each module with no mutant, then each surviving mutant and each
    mutant not reached."""
    unfinished = unfinished_line(report)
    lines = [
        *([unfinished, ""] if unfinished else []),
        "## Mutation gate",
        "",
        f"- Modules changed since `{report.base}`: {len(report.modules)}; mutants made by mutmut {report.mutmut} on "
        "the lines the pull request changed.",
        f"- {report.workers} workers ran for {report.seconds / 60:.0f} of the {report.minutes} minutes the gate is "
        "capped at; a mutant ran against the suites that executed its statement unmutated, fastest first, until one "
        "failed.",
        "",
        "| Module | Changed lines | Mutants | Killed | Timed out | Survived | Not reached | Not run | Kill ratio |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for module in report.modules:
        found = counts(module.outcomes)
        cells = [str(module.lines), str(len(module.outcomes)), *(str(found[status]) for status in found)]
        lines.append(f"| `{module.module}` | {' | '.join([*cells, kill_ratio(found)])} |")
    for module in report.modules:
        if module.tables:
            lines += ["", table_line(module)]
        elif not module.outcomes:
            lines += ["", empty_selection_line(module)]
    for module in report.modules:
        if module.cut_short:
            left = counts(module.outcomes)[NOT_RUN]
            lines += ["", f"The {report.minutes}-minute cap cut `{module.module}` short: {left} mutants did not run."]
    if report.unmeasured_suites:
        names = ", ".join(f"`{suite}`" for suite in report.unmeasured_suites)
        lines += ["", f"The cap left these suites' reach unmeasured, so no mutant ran against them: {names}."]
    for suite, reason in report.failed_suites.items():
        lines += ["", f"`{suite}` {reason}, so no mutant ran against it."]
    for module in report.modules:
        lines += survivor_lines(module) + unreached_lines(module)
    return "\n".join(lines) + "\n"


def no_module_line(base: str) -> str:
    return (
        f"**Mutation gate:** no Python module under `deployer/`, `tools/`, or a skill's `scripts/` changed since "
        f"`{base}`, so no mutant ran.\n"
    )


def other_sources(tree: Path, module: str) -> str:
    """The text of every Python file in a worktree but one module, read so that a name the module binds can be looked
    for in them; a byte that does not decode is replaced."""
    files = sorted(path for path in tree.rglob("*.py") if path.relative_to(tree).as_posix() != module)
    return "\n".join(path.read_bytes().decode("utf-8", "replace") for path in files)


def named_in(text: str, name: str) -> bool:
    """Whether text holds a name as a whole word."""
    return re.search(rf"\b{re.escape(name)}\b", text) is not None


@dataclass(frozen=True)
class Result:
    """A run's summary, and whether every mutant ran and every suite's reach was measured."""

    text: str
    finished: bool = True


@dataclass
class Gate:
    """One run's settings and the runners it calls, which a test replaces."""

    root: Path
    base: str
    minutes: int
    workers: int
    git: GitClient
    generate: Generator = mutmut_mutants
    reach: ReachRunner = coverage_reach
    run: SuiteRunner = run_suite
    clock: Clock = time.monotonic
    measure: Callable[[ReachRunner], str | None] = measurement_problem

    def select(self, tree: Path, modules: Sequence[str]) -> dict[str, Selection]:
        """Each module's count of changed lines, the mutants of those lines, and the sanction tables holding one, each
        with the policy that reads it; a changed table no policy reads stops the run."""
        policies = policy_tables(tree)
        selected = {}
        for module in modules:
            diff = self.git.output(
                ["diff", "-U0", "--no-color", f"{self.base}...HEAD", "--", module], directory=self.root
            )
            lines = frozenset(changed_lines(diff))
            source = (tree / module).read_bytes().decode("utf-8")
            tables = changed_tables(ast.parse(source), lines)
            problem = unread_tables(module, tables, policies)
            if problem:
                raise RuntimeError(problem)
            others = other_sources(tree, module)
            selected[module] = (
                len(lines),
                module_mutants(
                    module, source, lines, self.generate, functools.cache(functools.partial(named_in, others))
                ),
                tuple((name, policies[name]) for name in tables),
            )
        return selected

    def mutate(self, trees: Sequence[Path], modules: Sequence[str], started: float) -> Report:
        """Select the mutants of the changed lines, measure the reach of the suites that reach a module with one, then
        test them from one queue until the cap."""
        deadline = run_deadline(started, self.minutes)
        workers = Workers(trees, self.clock)
        selected = self.select(trees[0], modules)
        mutated = [module for module in modules if selected[module][1]]
        reach: dict[str, Reach] = {}
        failed: dict[str, str] = {}
        unmeasured: list[str] = []
        if mutated:
            problem = self.measure(self.reach)
            if problem:
                raise RuntimeError(problem)
            suites = [
                suite.relative_to(self.root).as_posix()
                for suite in reached_suites(self.root, mutated)
                if suite.suffix.casefold() == ".py"
            ]
            reach, failed, unmeasured = measure_reach(
                workers, suites, mutated, reach_deadline(started, deadline), self.reach
            )
        ordered = queue_order(mutants for _, mutants, _ in selected.values())
        outcomes = try_mutants(workers, ordered, reach, deadline, self.run)
        reports = []
        for module, (lines, _, tables) in selected.items():
            found = [outcome for outcome in outcomes if outcome.mutant.module == module]
            cut_short = any(outcome.status == NOT_RUN for outcome in found)
            reports.append(ModuleReport(module, found, cut_short, lines, tables))
        version = importlib.metadata.version("mutmut") if self.generate is mutmut_mutants else "(a test's generator)"
        return Report(self.base, self.minutes, self.clock() - started, len(trees), version, reports, failed, unmeasured)

    def report(self) -> Result:
        """The summary of one run, one line when no module changed, else every module's mutants tested, and whether
        the cap let it finish."""
        modules = changed_modules(self.root, self.base, self.git)
        if not modules:
            return Result(no_module_line(self.base))
        problem = generator_problem(self.generate)
        if problem:
            raise RuntimeError(problem)
        started = self.clock()
        with (
            tempfile.TemporaryDirectory(prefix="mutation-gate-") as temporary,
            worktrees(self.root, self.workers, Path(temporary), self.git) as trees,
        ):
            report = self.mutate(trees, modules, started)
        return Result(summary(report), unfinished_line(report) is None)


def parse_arguments(argv: Sequence[str] | None, environment: Mapping[str, str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mutate a pull request's changed lines and report the survivors.")
    parser.add_argument(
        "--base",
        default=f"origin/{environment.get('GITHUB_BASE_REF') or 'main'}",
        help="the branch the pull request merges into (default: origin/$GITHUB_BASE_REF, or origin/main)",
    )
    parser.add_argument(
        "--minutes", type=int, default=DEFAULT_MINUTES, help=f"the cap on the run (default: {DEFAULT_MINUTES})"
    )
    arguments = parser.parse_args(argv)
    if arguments.minutes < 1:
        parser.error("--minutes must be at least 1")
    return arguments


def main(argv: Sequence[str] | None = None, environment: Mapping[str, str] = os.environ) -> int:
    arguments = parse_arguments(argv, environment)
    gate = Gate(REPOSITORY_ROOT, arguments.base, arguments.minutes, worker_count(environment), GitClient())
    result = gate.report()
    print(result.text, end="", flush=True)
    append_step_summary(environment, result.text)
    if result.finished:
        return 0
    # A workflow command, which puts the summary's first line on the run's annotations.
    print(f"::error title=Mutation gate unfinished::{result.text.splitlines()[0]}", flush=True)
    return 1


if __name__ == "__main__":
    run_main(main)

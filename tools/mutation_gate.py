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
`job_selection.reached_suites` selects them for `--changed`, each run once unmutated under coverage.py with its
subprocess measurement on, which records the statements of the changed modules each suite executes, in its own process
or in one it started with the environment passed on; the gate's own coverage steps and suite runs are never measured.
coverage.py records a statement by its first line, and the lines its report names executed, missing, or excluded are the
first lines of all the module's statements, so each line up to the next of them belongs to the statement above it, and a
mutant on a continuation line of a multi-line statement is reached when that statement ran. coverage.py's
`test_function` dynamic context records, too, which test functions ran each statement, and `coverage json
--show-contexts` reports them, which the gate matches to the node ids `pytest --collect-only` lists for the suite. A
mutant then runs the suites that executed its statement, fastest first, until one fails: of each suite, only the tests
that ran the statement, when its own test functions alone did, and the whole suite when code outside any test function
ran it too, since a process the suite started, a set-up, a fixture, or an import records no test function; a context
naming no collected test of the suite, or a test function name more than one collected class holds, which an inherited
test method would be, and a suite pytest cannot list run whole as well. The mutant is killed, or timed out when a suite
runs past three times its unmutated time. A mutant every one of those suites passes survived. A mutant in a statement no
suite executed is not reached, and a suite that fails unmutated, or whose coverage data `coverage combine` or `coverage
json` fails or times out on, is left out and named with the step and its output, never read as a suite that reached
nothing. The Bash and PowerShell suites run no Python module and are not used.

When no changed line yields a mutant, no suite runs. Otherwise the run is bounded by --minutes (DEFAULT_MINUTES): the
reach pass, over the suites that reach a module with a mutant, takes at most half of it, and the mutants then run from
one queue that takes one mutant from each module in turn, each module's in line order, so a module late in the list
starts as soon as the first. A mutant the cap leaves untested is counted as not run, and the summary says which module
the cap cut short. Each module's kill ratio is its killed and timed-out mutants over every mutant it tested.

The report, each module's changed lines, counts, and kill ratio, each changed module with no mutant, and each surviving
mutant by its line, the mutation, and the suites it ran against, each with the count of its tests that ran when not
all of them did, goes to stdout and to the step summary
GITHUB_STEP_SUMMARY names. A pull request that changes no such module writes one line saying so. It exits 0 once the
gate has run, whatever survived, and 1 when it cannot run: mutmut no longer makes the mutant its canary expects on the
canary's line alone, or a git command fails.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import importlib
import importlib.metadata
import itertools
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "validation"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

from bounded_process import run_bounded
from console import run_main
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


@dataclass(frozen=True)
class Mutant:
    module: str
    line: int
    before: str
    after: str
    source: str


@dataclass(frozen=True)
class Reach:
    """A suite's unmutated run: its seconds, the lines of each changed module it executed, and, for each line only
    the suite's own test functions executed, those tests' node ids; a line it executed that `tests` leaves out runs the
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


def module_mutants(module: str, source: str, changed: frozenset[int], generate: Generator) -> list[Mutant]:
    """Every distinct mutant of a module whose first changed line is one the pull request changed, in line order."""
    if not changed:
        return []
    mutants = []
    for text in dict.fromkeys(generate(module, source, changed)):
        if text == source:
            continue
        line, before, after = describe(source, text)
        if line in changed:
            mutants.append(Mutant(module, line, before, after, text))
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
    """coverage.py's configuration for one reach run: data in its own folder, one file per measured process, the
    processes the suite starts measured too, and each line recorded with the test function that ran it."""
    return "\n".join(
        [
            "[run]",
            f"data_file = {folder / 'data'}",
            "parallel = true",
            "patch = subprocess",
            "dynamic_context = test_function",
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

    A context names a test function by its module's import name and its qualified name. The empty context is code that
    ran outside every test function: at import, in a set-up or a fixture, or in a process the suite started, which
    records no test function, and only the whole suite repeats it. A test method a class inherits is named by the class
    that defines it, so a context whose function name more than one collected class holds, or one that names no
    collected test of this suite, runs the whole suite too."""
    stem = PurePosixPath(suite).stem
    named: dict[str, list[str]] = {}
    for test in collected:
        named.setdefault(test.removeprefix(f"{suite}::").split("[", 1)[0].replace("::", "."), []).append(test)
    functions = Counter(name.rpartition(".")[2] for name in named)
    found: set[str] = set()
    for context in contexts:
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
    test functions ran, those tests' node ids."""
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
        started = time.monotonic()
        try:
            finished = run_bounded(
                [*python, "coverage", "run", rcfile, "-m", "pytest", suite, "-q"],
                timeout,
                cwd=tree,
                env=suite_environment(),
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
        recorded = json.loads(report.read_text(encoding="utf-8"))
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


def try_mutant(
    tree: Path, mutant: Mutant, reach: Mapping[str, Reach], deadline: float, run: SuiteRunner, clock: Clock
) -> Outcome:
    """Write a mutant into a worktree, run the suites that reach its line until one fails, each suite's tests that ran
    the line when only its own test functions did and the whole suite otherwise, and put the module back."""
    suites = covering_suites(mutant, reach)
    if not suites:
        return Outcome(mutant, NOT_REACHED)
    path = tree / mutant.module
    original = path.read_bytes()
    path.write_text(mutant.source, encoding="utf-8", newline="")
    ran: list[str] = []
    tests: list[int] = []
    try:
        for suite in suites:
            if clock() >= deadline:
                return Outcome(mutant, NOT_RUN, tuple(ran), tuple(tests))
            selected = sorted(reach[suite].tests.get(mutant.module, {}).get(mutant.line, ()))
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
        if outcome.suites:
            print(f"{outcome.status:<9} {mutant.module}:{mutant.line} {mutant.before} -> {mutant.after}", flush=True)
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
        named = []
        for index, suite in enumerate(outcome.suites):
            count = outcome.tests[index] if index < len(outcome.tests) else 0
            noun = "test" if count == 1 else "tests"
            named.append(f"`{suite}`" + (f" ({count} {noun} that ran the line)" if count else ""))
        suites = ", ".join(named)
        lines.append(f"- Line {mutant.line}: {code(mutant.before)} became {code(mutant.after)}; passed {suites}")
    return lines


def empty_selection_line(module: ModuleReport) -> str:
    """Why a changed module has no mutant, said so that a selection empty by mistake is seen."""
    if not module.lines:
        return f"`{module.module}` keeps no line this pull request added or changed, so it has nothing to mutate."
    noun = "line" if module.lines == 1 else "lines"
    return (
        f"`{module.module}`: mutmut made no mutant on the {module.lines} {noun} this pull request changed, so none ran."
    )


def summary(report: Report) -> str:
    """The Markdown the run's summary page shows: each module's changed lines, counts, and kill ratio, each module
    with no mutant, then each surviving mutant."""
    lines = [
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
        if not module.outcomes:
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
        lines += survivor_lines(module)
    return "\n".join(lines) + "\n"


def no_module_line(base: str) -> str:
    return (
        f"**Mutation gate:** no Python module under `deployer/`, `tools/`, or a skill's `scripts/` changed since "
        f"`{base}`, so no mutant ran.\n"
    )


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

    def select(self, tree: Path, modules: Sequence[str]) -> dict[str, tuple[int, list[Mutant]]]:
        """Each module's count of changed lines and the mutants of those lines."""
        selected = {}
        for module in modules:
            diff = self.git.output(
                ["diff", "-U0", "--no-color", f"{self.base}...HEAD", "--", module], directory=self.root
            )
            lines = frozenset(changed_lines(diff))
            source = (tree / module).read_bytes().decode("utf-8")
            selected[module] = (len(lines), module_mutants(module, source, lines, self.generate))
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
            suites = [
                suite.relative_to(self.root).as_posix()
                for suite in reached_suites(self.root, mutated)
                if suite.suffix.casefold() == ".py"
            ]
            reach, failed, unmeasured = measure_reach(
                workers, suites, mutated, reach_deadline(started, deadline), self.reach
            )
        ordered = queue_order(mutants for _, mutants in selected.values())
        outcomes = try_mutants(workers, ordered, reach, deadline, self.run)
        reports = []
        for module, (lines, _) in selected.items():
            found = [outcome for outcome in outcomes if outcome.mutant.module == module]
            reports.append(ModuleReport(module, found, any(outcome.status == NOT_RUN for outcome in found), lines))
        version = importlib.metadata.version("mutmut") if self.generate is mutmut_mutants else "(a test's generator)"
        return Report(self.base, self.minutes, self.clock() - started, len(trees), version, reports, failed, unmeasured)

    def report(self) -> str:
        """The summary of one run: one line when no module changed, else every module's mutants tested."""
        modules = changed_modules(self.root, self.base, self.git)
        if not modules:
            return no_module_line(self.base)
        problem = generator_problem(self.generate)
        if problem:
            raise RuntimeError(problem)
        started = self.clock()
        with (
            tempfile.TemporaryDirectory(prefix="mutation-gate-") as temporary,
            worktrees(self.root, self.workers, Path(temporary), self.git) as trees,
        ):
            return summary(self.mutate(trees, modules, started))


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
    text = gate.report()
    print(text, end="", flush=True)
    append_step_summary(environment, text)
    return 0


if __name__ == "__main__":
    run_main(main)

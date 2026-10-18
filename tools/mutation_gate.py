"""Mutate the Python modules a pull request changes and report the mutants its suites leave alive.

Usage:
  python -B tools/mutation_gate.py [--base REF] [--minutes N]

The `Mutation` workflow runs it on every pull request, on Linux, where requirements-mutation.txt installs mutmut and
coverage.py; it is not a required check, and it calls no model and reads no secret. It mutates each module the pull
request's commits change since the base (`origin/<GITHUB_BASE_REF>`, or origin/main, unless --base names another) under
deployer/, tools/, a skill's scripts/, or a repository skill's scripts/, leaving out suites, conftest.py, and deleted
files. mutmut's operators make the mutants, string literals included, each one mutation of the whole module.

mutmut's own runner is not used: it records which tests reach a function only inside its pytest process and finds a
mutant by a module name it derives from the file's path, while the suites here reach most modules through the
subprocesses they start (deploy.py, a skill's script) and import a skill's or a tool's module by its bare name. So the
gate runs the suites as the validation runner does, `python -B -m pytest <suite>` from the root of a throwaway git
worktree at HEAD, one worktree per worker, with the mutant written into the worktree's copy of the module, where every
process the suite starts imports it.

Which suites run against a mutant is measured, not guessed. The suites that reach the changed modules, as
`job_selection.reached_suites` selects them for `--changed`, each run once unmutated under coverage.py with its
subprocess measurement on, which records the lines of the changed modules each suite executes, in its own process or in
one it started with the environment passed on. A mutant then runs the suites that executed its line, fastest first,
until one fails: the mutant is killed, or timed out when a suite runs past three times its unmutated time. A mutant
every one of those suites passes survived. A mutant on a line no suite executed is not reached, and a suite that fails
unmutated is left out and named. The Bash and PowerShell suites run no Python module and are not used.

The run is bounded by --minutes (DEFAULT_MINUTES): the reach pass takes at most half of it, and each module then gets an
equal share of what is left when it starts, so one large module cannot starve the others. The mutants on the lines the
pull request changed run first. A mutant the cap leaves untested is counted as not run, and the summary says which
module the cap cut short. Each module's kill ratio is its killed and timed-out mutants over every mutant it tested.

The report, each module's counts and kill ratio and each surviving mutant by its line, the mutation, and the suites it
ran against, goes to stdout and to the step summary GITHUB_STEP_SUMMARY names. A pull request that changes no such
module writes one line saying so. It exits 0 once the gate has run, whatever survived, and 1 when it cannot run: mutmut
no longer makes the mutant its canary expects, or a git command fails.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import importlib
import importlib.metadata
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeVar

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "validation"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "skill-core" / "scripts"))

from bounded_process import run_bounded
from console import run_main
from flat_text import flat_text
from git_client import GitClient
from job_pool import append_step_summary, worker_count
from job_selection import reached_suites, recorded_job_seconds
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
    changed: bool


@dataclass(frozen=True)
class Reach:
    """A suite's unmutated run: its seconds, and the lines of each changed module it executed."""

    seconds: float
    lines: Mapping[str, frozenset[int]]


@dataclass(frozen=True)
class Outcome:
    mutant: Mutant
    status: str
    suites: tuple[str, ...] = ()


@dataclass
class ModuleReport:
    module: str
    outcomes: list[Outcome]
    cut_short: bool


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


Generator = Callable[[str, str], list[str]]
# Runs a suite in a worktree within a time limit and says whether it passed, failed (KILLED), or timed out.
SuiteRunner = Callable[[Path, str, float], str]
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


def module_mutants(module: str, source: str, changed: set[int], generate: Generator) -> list[Mutant]:
    """Every distinct mutant of a module, those on a line the pull request changed first, then in line order."""
    mutants = []
    for text in dict.fromkeys(generate(module, source)):
        if text == source:
            continue
        line, before, after = describe(source, text)
        mutants.append(Mutant(module, line, before, after, text, line in changed))
    return sorted(mutants, key=lambda mutant: (not mutant.changed, mutant.line))


def mutmut_mutants(module: str, source: str) -> list[str]:
    """Each mutant mutmut's operators make of a module, as the module's whole text with that one mutation.

    mutmut reads its configuration, [tool.mutmut] in pyproject.toml, from the working directory, so the mutants are
    made from the repository's root."""
    file_mutation = importlib.import_module("mutmut.mutation.file_mutation")
    with contextlib.chdir(REPOSITORY_ROOT):
        tree, mutations, _, _ = file_mutation.create_mutations(module, source)
    return [tree.deep_replace(mutation.original_node, mutation.mutated_node).code for mutation in mutations]


def generator_problem(generate: Generator) -> str | None:
    """Why the generator cannot be trusted, or None when it makes the canary's mutant."""
    if CANARY_MUTANT in generate("canary.py", CANARY_SOURCE):
        return None
    return f"the mutant generator no longer turns {CANARY_SOURCE.strip()!r} into {CANARY_MUTANT.strip()!r}"


def suite_environment() -> dict[str, str]:
    """This process's environment for a suite: no bytecode written, so a mutant the same size as the module and
    written within the same second is never read from a stale cache, and no coverage measurement."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("COVERAGE_PROCESS_")}
    return {**environment, "PYTHONDONTWRITEBYTECODE": "1"}


def run_suite(tree: Path, suite: str, timeout: float) -> str:
    """Run one suite in a worktree, stopping at its first failure, with every process it starts ended after it."""
    try:
        finished = run_bounded(
            pytest_command(suite, "-x", "-q"),
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
    """coverage.py's configuration for one reach run: data in its own folder, one file per measured process, and the
    processes the suite starts measured too."""
    return "\n".join(
        [
            "[run]",
            f"data_file = {folder / 'data'}",
            "parallel = true",
            "patch = subprocess",
            "include =",
            *(f"    {file}" for file in files),
            f"disable_warnings = {', '.join(COVERAGE_WARNINGS)}",
            "",
        ]
    )


def executed_lines(report: Mapping[str, object], tree: Path, modules: Sequence[str]) -> dict[str, frozenset[int]]:
    """The lines of each module a coverage.py JSON report says ran, its files named by any path into the worktree."""
    lines = {module: frozenset[int]() for module in modules}
    files = report.get("files")
    for name, entry in files.items() if isinstance(files, dict) else ():
        path = Path(name) if Path(name).is_absolute() else tree / name
        try:
            module = path.resolve().relative_to(tree.resolve()).as_posix()
        except ValueError:
            continue
        if module in lines and isinstance(entry, dict):
            lines[module] = frozenset(int(line) for line in entry.get("executed_lines", ()))
    return lines


def coverage_reach(tree: Path, suite: str, modules: Sequence[str], timeout: float) -> Reach | str:
    """Run a suite unmutated under coverage.py and give the lines of the changed modules it executed, or why not."""
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
        for command in (["combine", rcfile, "-q"], ["json", rcfile, "-q", "-o", str(report)]):
            run_bounded([*python, "coverage", *command], MINIMUM_SUITE_SECONDS, cwd=tree, output_limit=OUTPUT_LIMIT)
        data = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
        return Reach(seconds, executed_lines(data, tree, modules))


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
    recorded = recorded_job_seconds()
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
    """Write a mutant into a worktree, run the suites that reach its line until one fails, and put the module back."""
    suites = covering_suites(mutant, reach)
    if not suites:
        return Outcome(mutant, NOT_REACHED)
    path = tree / mutant.module
    original = path.read_bytes()
    path.write_text(mutant.source, encoding="utf-8", newline="")
    ran: list[str] = []
    try:
        for suite in suites:
            if clock() >= deadline:
                return Outcome(mutant, NOT_RUN, tuple(ran))
            ran.append(suite)
            result = run(tree, suite, max(MINIMUM_SUITE_SECONDS, SUITE_TIME_FACTOR * reach[suite].seconds))
            if result != PASSED:
                return Outcome(mutant, result, tuple(ran))
        return Outcome(mutant, SURVIVED, tuple(ran))
    finally:
        path.write_bytes(original)


def try_module(
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


def module_deadline(now: float, deadline: float, modules_left: int) -> float:
    """When a module starting now stops starting mutants: an equal share of what is left among the modules left."""
    return now + (deadline - now) / modules_left


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
        changed = " (a line this pull request changed)" if mutant.changed else ""
        suites = ", ".join(f"`{suite}`" for suite in outcome.suites)
        lines.append(
            f"- Line {mutant.line}{changed}: {code(mutant.before)} became {code(mutant.after)}; passed {suites}"
        )
    return lines


def summary(report: Report) -> str:
    """The Markdown the run's summary page shows: each module's counts and kill ratio, then each surviving mutant."""
    lines = [
        "## Mutation gate",
        "",
        f"- Modules changed since `{report.base}`: {len(report.modules)}; mutants made by mutmut {report.mutmut}.",
        f"- {report.workers} workers ran for {report.seconds / 60:.0f} of the {report.minutes} minutes the gate is "
        "capped at; a mutant ran against the suites that executed its line unmutated, fastest first, until one failed.",
        "",
        "| Module | Mutants | Killed | Timed out | Survived | Not reached | Not run | Kill ratio |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for module in report.modules:
        found = counts(module.outcomes)
        cells = [str(len(module.outcomes)), *(str(found[status]) for status in found), kill_ratio(found)]
        lines.append(f"| `{module.module}` | {' | '.join(cells)} |")
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

    def mutate(self, trees: Sequence[Path], modules: Sequence[str], started: float) -> Report:
        """Measure the suites' reach, then test each module's mutants within its share of what time is left."""
        deadline = run_deadline(started, self.minutes)
        workers = Workers(trees, self.clock)
        suites = [
            suite.relative_to(self.root).as_posix()
            for suite in reached_suites(self.root, list(modules))
            if suite.suffix.casefold() == ".py"
        ]
        reach, failed, unmeasured = measure_reach(
            workers, suites, modules, reach_deadline(started, deadline), self.reach
        )
        reports = []
        for index, module in enumerate(modules):
            share = module_deadline(self.clock(), deadline, len(modules) - index)
            diff = self.git.output(
                ["diff", "-U0", "--no-color", f"{self.base}...HEAD", "--", module], directory=self.root
            )
            source = (trees[0] / module).read_bytes().decode("utf-8")
            mutants = module_mutants(module, source, changed_lines(diff), self.generate)
            outcomes = try_module(workers, mutants, reach, share, self.run)
            reports.append(ModuleReport(module, outcomes, any(outcome.status == NOT_RUN for outcome in outcomes)))
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
    parser = argparse.ArgumentParser(description="Mutate a pull request's changed modules and report the survivors.")
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

"""Regression suites: how they are found, sharded, and run, the rules the skill guide states for them, that every
module is named by a test, and that the root conftest.py isolates git for every Python suite.

Each Python suite runs as `python -B -m pytest <suite>`, or as one shard of it with conftest.py's --suite-shard, in
a process of its own; a Bash or PowerShell suite runs as a program.
"""

from __future__ import annotations

import ast
import functools
import itertools
import re
import sys
import tomllib
import unittest
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

import file_presence
from bash_syntax import (
    BashSyntaxError,
    Command,
    CommandList,
    FunctionDefinition,
    Simple,
    Source,
    command_bodies,
    command_words,
    expanded_words,
    here_documents,
    parse,
)
from call_tables import PROCESS_STARTS
from command_wrappers import (
    ASSIGNMENT,
    COMMAND_WRAPPERS,
    SCRIPT_SHELLS,
    UNREAD_PROGRAM,
    bash_program,
    shell_script_index,
    trap_action,
    unwrapped,
    wrapper_name,
)
from dealing import deal
from job_pool import UNSPLIT_SUITE_WEIGHT, Job, run_process
from module_closure import MODULE_LOADERS
from option_reader import OptionTable, option_table
from path_containment import posix_within, relative_within, within, written_refusal
from shell_targets import run_bash_suite
from skill_scripts import INTERPRETER, PATH_METHODS, STANDARD_STREAMS
from toolchain import find_powershell
from type_check_roots import MYPY_PATH_PREFIX
from validation_support import (
    COMPUTED_NAME,
    REPOSITORY_ROOT,
    SKILL_GUIDE,
    SUITE_EXTENSIONS,
    SUITE_NAME_PATTERNS,
    CallableResolver,
    UnreadablePython,
    attribute_lookups,
    bound_values,
    call_argument,
    import_aliases,
    is_executable_script,
    is_main_guard,
    is_shell_script,
    is_test_script,
    listed,
    markdown_section,
    module_lookup,
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

CONFTEST = "conftest.py"
# A Python suite is split into one shard per this many tests, up to MAXIMUM_SHARDS. Smaller shards spread a long
# suite further, at the cost of one more process start and suite import each, about half a second on CI's Windows
# runners, which at six tests a shard was more than many shards' tests took. A recorded test counts as its seconds,
# and this stays below the least recorded test's seconds with room to grow, or recorded_shard_problems deals that test
# beside others once its suite gains a test or two.
TESTS_PER_SHARD = 8
MAXIMUM_SHARDS = 8
# The seconds a test takes run alone, recorded for a test that costs many times a typical one, by suite and then by
# the test's name within the suite as pytest names it, `Class::test`, never a test `parametrize` expands, which pytest
# collects only as its cases; any other test counts as one. The suite gets shards for its total, and conftest.py deals
# the heaviest test first onto the lightest shard, so a recorded test runs on a shard of its own instead of lengthening
# a shared one, as long as the suite's other tests fit on its other shards; recorded_shard_problems holds that, which
# can fail once a suite outgrows MAXIMUM_SHARDS. Measured on 2026-10-08 on a 24-CPU Windows machine, each test run
# alone.
RECORDED_TEST_SECONDS: dict[str, dict[str, float]] = {
    "skills/ejryan89-code-review-core/scripts/test_adversarial_inputs.py": {
        # Writes and reads back a path at this machine's limit, which with long paths enabled is 32,507 units.
        (
            "PathLengthTests::test_a_path_at_this_machines_limit_is_written_and_one_unit_longer_is_an_unsafe_path_"
            "exclusion"
        ): 13,
    },
    "skills/repo-cleanup/scripts/test_repo_cleanup.py": {
        # Pushes nine branches to a local remote, deletes them there, and plans and applies a cleanup of each with git.
        "PlanApplyTests::test_gone_branches_are_deleted_only_when_their_pull_request_proves_them_stale": 13,
    },
    "tests/validation/test_skill_scripts.py": {
        # Starts a Python per entry point, which imports the entry point and runs its __main__ block twice.
        "EntryPointRunnerTests::test_every_entry_point_turns_a_failure_into_a_failed_line_and_ctrl_c_into_exit_130": 10,
    },
    "tests/deployer/test_installed_update.py": {
        # Each clones this repository twice, deploys the latest release with its configure and deployer, and runs that
        # installed update.sh, which checks and redeploys every skill with this checkout's deployer. Measured on
        # 2026-10-24 on the same kind of machine.
        "test_an_update_with_no_later_release_redeploys_the_release": 43,
        "test_a_patch_release_updates_to_it": 43,
        "test_a_breaking_release_stops_until_crossed_then_deploys_moving_renamed_skills": 50,
        "test_the_update_fails_when_a_commit_moves_a_path_the_release_reads": 26,
    },
}


# Commands whose program the commands reader cannot read, by the scope that starts them, and why.
COMMANDS_ALLOWED = {
    "run_test_script": "starts the PowerShell 7 toolchain.find_powershell located",
}


def _needs_a_test(name: str, scripts: list[str]) -> bool:
    """Whether a repository path is a module under a skill's scripts/, deployer/, or tools/ that a test must import."""
    parts = name.split("/")
    in_scope = parts[0] in {"deployer", "tools"} or any(
        posix_within(name, directory, strict=True) for directory in scripts
    )
    return in_scope and name.endswith(".py") and parts[-1] != "__init__.py" and not is_test_script(Path(name))


def untested_module_problems(root: Path) -> list[str]:
    """Report each module under skills/*/scripts/, deployer/, or tools/ that no Python regression suite imports.

    A suite is a file regression_suites finds, by ejryan89-skill-core's suite-name rule, since only a suite that runs
    tests what it imports, and it imports the module itself: an import statement, an importlib.import_module or
    __import__ of a literal name, or a load of its file by path, each resolved to the file it loads as
    RepositoryModules resolves it, so another skill's module of the same name, or a standard-library module, is not
    this one. Naming, mentioning, or running it is not enough, and neither is a test file named after it. A package's
    __init__.py needs no test.
    """
    files = repository_files(root)
    modules = RepositoryModules(root)
    tested: set[Path] = set()
    for suite in regression_suites(root):
        if suite.suffix == ".py":
            tested |= set(modules.imports(suite, unique_loads=True))
    scripts = [directory.relative_to(root).as_posix() for directory in skill_script_directories(root)]
    problems: list[str] = []
    for path in sorted(files, key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        if _needs_a_test(name, scripts) and path.resolve() not in tested:
            problems.append(f"{name} is imported by no Python regression suite; add a test that imports it")
    return problems + modules.unreadable_problems()


def suite_discovery_documentation_problems(root: Path) -> list[str]:
    """Report a rule that decides whether a regression suite runs and that "Validation" in the skill guide omits."""
    section = markdown_section((root / SKILL_GUIDE).read_text(encoding="utf-8"), "## Validation")
    if section is None:
        return [f'{SKILL_GUIDE} has no "Validation" section']
    rules = [*SUITE_NAME_PATTERNS, *sorted(SUITE_EXTENSIONS), CONFTEST]
    return [f'{SKILL_GUIDE} "Validation" does not name `{rule}`' for rule in rules if f"`{rule}`" not in section]


def pytest_command(target: str, *options: str) -> list[str]:
    """How the runner starts a Python suite: pytest on the one file, from the repository root, with no bytecode. Its
    environment holds PYTEST_ENVIRONMENT over the one it inherits."""
    return [sys.executable, "-B", "-m", "pytest", target, *options]


# What every pytest the runner and the mutation gate start holds over the environment it inherits, so a suite is judged
# by the repository's pytest configuration and the suite alone: no options from the user's PYTEST_ADDOPTS, no plugin
# from PYTEST_PLUGINS, and no plugin an installed package registers, each of which can deselect or hide a test and
# leave the suite passing. Turning plugin autoloading off is safe because no package requirements-dev.txt or
# requirements-mutation.txt pins registers a pytest plugin.
PYTEST_ENVIRONMENT = {"PYTEST_ADDOPTS": "", "PYTEST_PLUGINS": "", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}


def run_test_script(path: Path) -> None:
    target = relative(path)
    suffix = path.suffix.casefold()
    if suffix == ".py":
        run_process(pytest_command(target), PYTEST_ENVIRONMENT)
    elif suffix == ".sh":
        run_bash_suite(target)
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
        if file_presence.is_directory(top, other_is_absent=True)
        for path in file_presence.matching(top, "**/*", skip=lambda folder: folder == root / FIXTURE_ROOT)
        if file_presence.is_present(path, other_is_absent=True)
        and is_test_script(path)
        and not within(path, root / FIXTURE_ROOT)
    )
    return sorted(found, key=lambda path: path.relative_to(root).as_posix().casefold())


def suite_cost(suite: Path, recorded: Mapping[str, float]) -> float:
    """A Python suite's cost in typical tests: one for each test, or its recorded seconds. A suite no policy can read
    costs one test; the policies that read it report it."""
    return defined_test_count(suite) + sum(seconds - 1 for seconds in recorded.values())


def defined_test_count(suite: Path) -> int:
    """The tests a Python suite defines, or one for a suite no policy can read."""
    try:
        return len(defined_tests(parse_python(suite)))
    except UnreadablePython:
        return 1


def shard_count(suite: Path, recorded: Mapping[str, float] | None = None) -> int:
    """A suite's shards: one per TESTS_PER_SHARD of its cost, up to MAXIMUM_SHARDS, and never more than its tests,
    since a shard dealt no test collects none, which pytest fails."""
    if suite.suffix.casefold() != ".py":
        return 1
    cost = int(suite_cost(suite, recorded or {})) // TESTS_PER_SHARD
    return max(1, min(MAXIMUM_SHARDS, defined_test_count(suite), cost))


def shard_options(index: int, count: int, recorded: Mapping[str, float]) -> list[str]:
    """The options that run shard `index` (from 0) of `count` of a suite, with each recorded test's seconds."""
    costs = [option for test, seconds in sorted(recorded.items()) for option in ("--test-seconds", f"{test}={seconds}")]
    return ["--suite-shard", f"{index + 1}/{count}", *costs]


def run_shard(suite: Path, index: int, count: int, recorded: Mapping[str, float]) -> None:
    run_process(pytest_command(relative(suite), *shard_options(index, count, recorded)), PYTEST_ENVIRONMENT)


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
        jobs.extend(
            Job(
                f"{label} [shard {index + 1}/{count}]",
                label,
                cost / count,
                functools.partial(run_shard, suite, index, count, seconds),
            )
            for index in range(count)
        )
    return jobs


def _is_test(node: ast.stmt) -> TypeGuard[ast.FunctionDef | ast.AsyncFunctionDef]:
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")


def _class_tests(tree: ast.Module) -> list[tuple[str, bool]]:
    """Each test the module defines, named as pytest names it within the suite: `Class::test` for a test* method of a
    class body, and `test` for a test* function at module level, with whether a block holds it, such as an
    `if sys.platform == "win32":`, so pytest collects it on some platforms only."""
    methods = [
        (f"{node.name}::{item.name}", item not in node.body)
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        for item in module_statements(node.body)
        if _is_test(item)
    ]
    functions = [(node.name, node not in tree.body) for node in module_statements(tree.body) if _is_test(node)]
    return methods + functions


def _marks_parametrize(expressions: Iterable[ast.expr]) -> bool:
    """Whether a decorator list or a `pytestmark` value applies `parametrize`, which pytest expands into one test per
    case, each named `test[id]`."""
    return any(
        (isinstance(node, ast.Attribute) and node.attr == "parametrize")
        or (isinstance(node, ast.Name) and node.id == "parametrize")
        for expression in expressions
        for node in ast.walk(expression)
    )


def _pytestmarks(body: list[ast.stmt]) -> list[ast.expr]:
    """The values a body assigns to `pytestmark`, which marks every test the body defines."""
    return [
        statement.value
        for statement in module_statements(body)
        if isinstance(statement, ast.Assign | ast.AnnAssign)
        and statement.value is not None
        and any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in (statement.targets if isinstance(statement, ast.Assign) else [statement.target])
        )
    ]


def parametrized_tests(tree: ast.Module) -> set[str]:
    """Each test, named as _class_tests names it, that `parametrize` expands: by its own decorator, its class's, or a
    `pytestmark` of its class or module. pytest collects it only as its cases, `test[id]`, never by the name alone."""
    everything = _marks_parametrize(_pytestmarks(tree.body))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            whole = everything or _marks_parametrize([*node.decorator_list, *_pytestmarks(node.body)])
            found.update(
                f"{node.name}::{item.name}"
                for item in module_statements(node.body)
                if _is_test(item) and (whole or _marks_parametrize(item.decorator_list))
            )
    found.update(
        node.name
        for node in module_statements(tree.body)
        if _is_test(node) and (everything or _marks_parametrize(node.decorator_list))
    )
    return found


def defined_tests(tree: ast.Module) -> list[str]:
    """Each test the module defines, as _class_tests names it, one in an if, try, or with block included: the one
    reader of a suite's tests, for its cost, its shards, the tests its recorded seconds name, and whether it has any.
    """
    return sorted({test for test, _ in _class_tests(tree)})


def recorded_cost_problems(
    root: Path, recorded: Mapping[str, Mapping[str, float]] = RECORDED_TEST_SECONDS
) -> list[str]:
    """Report a recorded test cost that names no Python regression suite, a test the suite does not define, a test it
    defines on some platforms only, which conftest.py's --test-seconds refuses where pytest does not collect it, a test
    `parametrize` expands, which pytest collects only as its cases and so by no name the suite's source gives, or a
    cost no heavier than a typical test."""
    problems: list[str] = []
    for name, tests in recorded.items():
        path = root / name
        reason = file_presence.refusal_reason(path)
        if reason not in (None, file_presence.MISSING):
            problems.append(f"{name} records test costs but {reason}")
            continue
        if reason is not None or not (is_test_script(path) and path.suffix == ".py"):
            problems.append(f"{name} records test costs but is not a regression suite")
            continue
        try:
            tree = parse_python(path)
        except UnreadablePython as error:
            problems.append(f"{name} {error}")
            continue
        found = _class_tests(tree)
        parametrized = parametrized_tests(tree)
        defined = {test for test, _ in found}
        conditional = {test for test, held in found if held}
        for test, seconds in tests.items():
            if test not in defined:
                problems.append(f"{name} records {test}, which it does not define")
            elif test in conditional:
                problems.append(
                    f"{name} records {test}, which a block of its suite holds, so pytest collects it on some "
                    "platforms only; record only a test every platform runs"
                )
            elif test in parametrized:
                problems.append(
                    f"{name} records {test}, which parametrize expands into one test per case, each collected as "
                    f"{test}[<id>]; record only a test that is not parametrized, or make the costly case a test of "
                    "its own"
                )
            elif seconds <= 1:
                problems.append(f"{name} records {test} at {seconds} seconds; record only tests that cost more than 1")
    return problems


def recorded_shard_problems(
    root: Path, recorded: Mapping[str, Mapping[str, float]] = RECORDED_TEST_SECONDS
) -> list[str]:
    """Report a recorded test that conftest.py would deal onto a shard beside another test.

    The heaviest test goes first onto an empty shard, and another test joins it only once every other shard carries as
    much. Below MAXIMUM_SHARDS a suite's shard count grows with its tests, so the others fit around a recorded test; at
    the cap a growing suite fills them, and the recorded test would lengthen a shared shard again.
    """
    problems: list[str] = []
    for name, seconds in recorded.items():
        path = root / name
        if not (path.suffix == ".py" and file_presence.is_present(path, other_is_absent=True)):
            continue
        try:
            defined = defined_tests(parse_python(path))
        except UnreadablePython:
            continue
        shards = deal(defined, shard_count(path, seconds), seconds)
        problems.extend(
            f"{name} deals {test} onto a shard beside other tests, since the suite fills its "
            f"{MAXIMUM_SHARDS} shards; raise MAXIMUM_SHARDS or split the suite"
            for test in sorted(set(seconds) & set(shards))
            if list(shards.values()).count(shards[test]) > 1
        )
    return problems


def suite_shape_problems(root: Path) -> list[str]:
    """Report a Python regression suite that defines no test pytest collects, or that still holds an
    `if __name__ == "__main__":` block.

    The runner starts every Python suite through pytest, so a suite with no test runs nothing, and a main block would
    run the suite a second way, without the root conftest.py that isolates git.
    """
    problems: list[str] = []
    suites = [path for path in regression_suites(root) if path.suffix.casefold() == ".py"]
    for _, name, tree in python_trees(suites, root, problems):
        if not defined_tests(tree):
            problems.append(f"{name} defines no test pytest collects: no test* method of a class and no test* function")
        problems += [
            f'{name}:{node.lineno} holds an `if __name__ == "__main__":` block; the runner starts the suite through '
            "pytest, so remove it"
            for node in module_statements(tree.body)
            if isinstance(node, ast.If) and is_main_guard(node.test)
        ]
    return problems


# A prefixed grep or head, as `ggrep` or `ghead`, is read as a program of its own, a stated limit, since no suite here
# names one.
GREP_PROGRAMS = frozenset({"grep", "egrep", "fgrep"})
RIPGREP = "rg"
# grep's options that end it before what writes into it may have written everything: at a match (-q, -m), or at its
# file's first match (-l, -L), as GNU grep documents "Scanning each input file stops upon first match".
EARLY_EXIT_LETTERS = frozenset("qmlL")
EARLY_EXIT_LONG_OPTIONS = frozenset(
    {"--quiet", "--silent", "--max-count", "--files-with-matches", "--files-without-match"}
)
# grep's options whose argument, the rest of a short option's word or the next word, is never an option of its own.
GREP_ARGUMENT_LETTERS = frozenset("ABCDdef")
GREP_ARGUMENT_LONG_OPTIONS = frozenset({"--regexp", "--file", "--after-context", "--before-context", "--context"})
# ripgrep's: -q, -m, and -l end it as grep's do, and these take an argument.
RIPGREP_EARLY_EXIT_LETTERS = frozenset("qml")
RIPGREP_EARLY_EXIT_LONG_OPTIONS = frozenset({"--quiet", "--max-count", "--files-with-matches"})
RIPGREP_ARGUMENT_LETTERS = frozenset("ABCEMTdefgjrt")
RIPGREP_ARGUMENT_LONG_OPTIONS = frozenset(
    {"--regexp", "--file", "--after-context", "--before-context", "--context", "--glob", "--type", "--type-not"}
)
# head exits once it has printed its count of lines or bytes, ten lines when given none; a negative count, all but the
# last, reads to the end.
HEAD = "head"
HEAD_COUNT_LETTERS = frozenset("nc")
HEAD_COUNT_LONG_OPTIONS = frozenset({"--lines", "--bytes"})
# Every long option GNU head takes, so a prefix is read as the one it names.
HEAD_LONG_OPTIONS = HEAD_COUNT_LONG_OPTIONS | {"--quiet", "--silent", "--verbose", "--zero-terminated"}
# The commands besides COMMAND_WRAPPERS that run the program a later word names, each with every option it takes, read
# as COMMAND_WRAPPERS's are (`unwrapped`): sudo also takes assignments before it.
READER_WRAPPERS: dict[str, OptionTable] = {
    "sudo": option_table(
        "Aa:BbC:c:D:E::eg:Hh::iKklNnPp:R:r:SsT:t:U:u:Vv",
        " ".join(
            (
                "askpass bell background close-from= login-class= chdir= preserve-env=? edit group= set-home help",
                "host= login remove-timestamp reset-timestamp list no-update non-interactive preserve-groups prompt=",
                "chroot= role= stdin shell command-timeout= type= other-user= user= version validate",
            )
        ),
        assignments=True,
    ),
    "doas": option_table("a:C:Lnsu:"),
    "setsid": option_table("cfw", "ctty fork wait help version"),
    "ionice": option_table("c:n:p:P:tu:", "class= classdata= pid= pgid= ignore uid= help version"),
}
# Every wrapper `_command_words` reads past.
WRAPPERS: dict[str, OptionTable] = {**COMMAND_WRAPPERS, **READER_WRAPPERS}
# The option set and shopt -o name, and the variable whose value lists it for a shell that starts with it.
PIPEFAIL = "pipefail"
OPTION_SETTERS = frozenset({"set", "shopt"})
SHELL_OPTIONS_VARIABLE = re.compile(r"\bSHELLOPTS=[^\s;&|]*pipefail")
# How deep the readers follow a command into the functions and shell scripts it runs.
NESTING_LIMIT = 16


def _long_option(word: str, options: frozenset[str]) -> str | None:
    """The one of `options` a long option word names, in full or by a prefix, as getopt_long takes a unique one."""
    option = word.partition("=")[0]
    return next((full for full in sorted(options) if len(option) > 2 and full.startswith(option)), None)


def _command_words(words: list[str]) -> list[str]:
    """A command's words from the program it runs: past assignments, and each wrapper of WRAPPERS, whether named
    by a path or not, or under a package manager's prefix, such as `env`, `exec`, `timeout`, `command`, `sudo`,
    `/usr/bin/env`, or `gtimeout`, as `unwrapped` reads it, env's -S value split into the words it runs, and a wrapper
    option it cannot place giving UNREAD_PROGRAM. xargs runs its command on no pipe, so it stays. _read_bash has
    already read past the reserved words that lead a command, so a word here that spells one is a literal, the program
    Bash runs."""
    while words:
        program = bash_program(words[0])
        wrapper = wrapper_name(program, WRAPPERS)
        if ASSIGNMENT.match(words[0]):
            words = words[1:]
        elif wrapper != "xargs" and wrapper in WRAPPERS:
            words = unwrapped([program, *words[1:]], WRAPPERS)
        else:
            break
    return words


def _shell_script(words: list[str]) -> str | None:
    """The script a command runs in a shell of its own or in this one: a shell's -c script, among its other options,
    as shell_script_index reads it, eval's arguments, joined as eval joins them, and the action a trap sets, as
    trap_action reads it, which this shell runs when the condition fires, reading this shell's standard input; None
    for any other command."""
    program = bash_program(words[0]) if words else ""
    if program == "eval":
        return " ".join(words[1:])
    if program == "trap":
        return trap_action(words[1:])
    if program not in SCRIPT_SHELLS:
        return None
    index = shell_script_index(words[1:])
    return None if index is None else words[index + 1]


def _early_exit_program(words: list[str]) -> str:
    """A command whose program may exit before what writes into it has written everything, as its program and the
    option that makes it, such as `grep -q` or `head -n`, or an empty string for a command that reads to the end. A
    program behind a wrapper option `unwrapped` cannot place may be such a reader, so it is one, named by the wrapper
    and that option."""
    if words and words[0].startswith(UNREAD_PROGRAM):
        return words[0].removeprefix(UNREAD_PROGRAM)
    program = bash_program(words[0]) if words else ""
    if program in GREP_PROGRAMS:
        return _early_exit_grep([program, *words[1:]])
    if program == RIPGREP:
        return _early_exit_grep(
            [program, *words[1:]],
            RIPGREP_EARLY_EXIT_LETTERS,
            RIPGREP_EARLY_EXIT_LONG_OPTIONS,
            RIPGREP_ARGUMENT_LETTERS,
            RIPGREP_ARGUMENT_LONG_OPTIONS,
        )
    if program == HEAD:
        return _early_exit_head(words)
    return ""


def _early_exit_reader(words: list[str], functions: Mapping[str, list[list[str]]] | None = None) -> str:
    """The reader a command a pipe feeds runs that may exit before what writes into it has written everything, as its
    program and the option that makes it, such as `grep -q` or `head -n`, or an empty string for a command that reads
    to the end. It reads past each wrapper `_command_words` reads, into the body of a function the script defines,
    and into the script a shell's -c or eval runs, the commands of their substitutions included, since each command
    there reads the same pipe. See _ReaderWalk for how often it reads each."""
    table = functions or {}
    return _ReaderWalk(table).reader(words, table)


# The functions a script defines, by name, as a table _ReaderWalk keeps once for each content it reads.
FunctionTable = Mapping[str, list[list[str]]]


class _ReaderWalk:
    """One question's walk for _early_exit_reader: each function's body is read once for each table of functions it
    is called under, and each shell's -c or eval script once for each table it runs under, the table its definitions
    extend kept once for each content, so a script that defines nothing new runs under the table it was given.
    Functions that call each other are read once each, never once for each order of calls, which grows factorially
    with them; a function already read, or being read, adds no reader, since the walk reads its whole body once and
    stops at the first reader it finds.

    The walk follows a command NESTING_LIMIT functions and scripts deep. A function or script is read again only when
    a path reaches it shallower than every read before, since a read at the limit leaves its deeper calls unread, so
    each is read at most once for each depth. A stated limit: each shell script on the path counts as a level, so a
    path through two -c scripts of the same shell ends one level sooner than the walk per path, which counted a
    shell's name once."""

    def __init__(self, functions: FunctionTable) -> None:
        # The shallowest depth each function, and each script, has been read at, by its table.
        self.read: dict[tuple[int, str], int] = {}
        self.scripts: dict[tuple[int, str], int] = {}
        self.tables: dict[tuple[tuple[str, tuple[tuple[str, ...], ...]], ...], FunctionTable] = {}
        self._table(functions)

    def _table(self, functions: FunctionTable) -> FunctionTable:
        """The one table kept for these definitions' content."""
        content = tuple(sorted((name, tuple(tuple(command) for command in body)) for name, body in functions.items()))
        return self.tables.setdefault(content, functions)

    def reader(self, words: list[str], functions: FunctionTable, depth: int = 0) -> str:
        if depth > NESTING_LIMIT:
            return ""
        words = _command_words(words) if words else []
        if not words:
            return ""
        if reader := _early_exit_program(words):
            return reader
        key = (id(functions), words[0])
        body = functions.get(words[0]) if depth < self.read.get(key, NESTING_LIMIT + 1) else None
        if body is not None:
            self.read[key] = depth
        script = _shell_script(words)
        if body is None and script is not None:
            key = (id(functions), script)
            if depth >= self.scripts.get(key, NESTING_LIMIT + 1):
                return ""
            self.scripts[key] = depth
            try:
                commands, inner = _read_bash(script)
            except ValueError:
                return ""
            body = [command for _, _, command in commands]
            functions = self._table({**functions, **inner})
        for command in body or []:
            if reader := self.reader(command, functions, depth + 1):
                return reader
        return ""


def _early_exit_head(words: list[str]) -> str:
    """head with the option that sets its count, or alone for its default ten lines: it exits once it has its count,
    unless the count is negative, all but the last lines or bytes, which reads to the end. head takes the last count it
    is given, and an option after a file name as well, as GNU getopt reads one, until `--`."""
    shown, negative = HEAD, False
    count_next = False
    for word in words[1:]:
        if count_next:
            negative, count_next = word.startswith("-"), False
        elif word == "--":
            break
        elif not word.startswith("-") or word == "-":
            continue
        elif word.startswith("--"):
            option = _long_option(word, HEAD_LONG_OPTIONS)
            if option in HEAD_COUNT_LONG_OPTIONS:
                _, equals, value = word.partition("=")
                shown, negative, count_next = f"{HEAD} {option}", value.startswith("-"), not equals
        elif word[1:].isdigit():
            shown, negative = f"{HEAD} {word}", False
        else:
            # A cluster of letters, as in -qn1: the first count letter takes the rest of the word, or the next word.
            for end, letter in enumerate(word[1:], 2):
                if letter in HEAD_COUNT_LETTERS:
                    shown, negative, count_next = f"{HEAD} -{letter}", word[end:].startswith("-"), end == len(word)
                    break
    return "" if negative else shown


def _early_exit_grep(
    words: list[str],
    early_letters: frozenset[str] = EARLY_EXIT_LETTERS,
    early_options: frozenset[str] = EARLY_EXIT_LONG_OPTIONS,
    argument_letters: frozenset[str] = GREP_ARGUMENT_LETTERS,
    argument_options: frozenset[str] = GREP_ARGUMENT_LONG_OPTIONS,
) -> str:
    """A grep or ripgrep command's program and the option that ends it early, as in `grep -q`, or an empty string for
    one that reads to the end; each program's letters and long options are its own."""
    argument_next = False
    for word in words[1:]:
        if argument_next:
            argument_next = False
        elif word == "--":
            return ""
        elif word.startswith("--"):
            option = _long_option(word, early_options)
            if option:
                return f"{words[0]} {option}"
            argument_next = "=" not in word and _long_option(word, argument_options) is not None
        elif word.startswith("-") and len(word) > 1:
            for end, letter in enumerate(word[1:], 2):
                if letter in early_letters:
                    return f"{words[0]} -{letter}"
                if letter in argument_letters:
                    argument_next = end == len(word)
                    break
    return ""


# A command Bash may run for a script, as (whether a pipe feeds its standard input, its line, its words from its
# program), and the functions a script defines, by name, with the words of each command their bodies run.
BashCommand = tuple[bool, int, list[str]]


class _BashReading:
    """The commands and functions of a Bash script, read from the tree of ejryan89-skill-core's bash_syntax, the
    repository's one Bash grammar, as _read_bash describes them."""

    def __init__(self) -> None:
        self.commands: list[BashCommand] = []
        self.functions: dict[str, list[list[str]]] = {}
        # The body of each function definition being read, innermost last, which each command read is added to.
        self.bodies: list[list[list[str]]] = []

    def read_list(self, node: CommandList, fed: bool) -> None:
        """Read a list: a command after the first of its pipeline reads the pipe, and the first reads what the list
        reads, in the background too, where Bash 5 keeps the pipe as its standard input and Bash 3.2 does not."""
        for and_or, _ in node.items:
            for pipeline in [and_or.first, *(pipeline for _, pipeline in and_or.rest)]:
                for place, command in enumerate(pipeline.commands):
                    self.read_command(command, place > 0 or fed)

    def read_command(self, node: Command, fed: bool) -> None:
        """Read a command and every command it runs: a compound command's bodies read what it reads, and so does
        each command or `<( )` substitution its words, its redirections, and an unquoted here-document's body hold,
        since each reads its command's standard input; a `>( )` substitution reads what the command writes into it.
        A function's definition runs nothing where it stands; its body is read as a command no pipe feeds, and kept
        under its name for each command that calls it."""
        if isinstance(node, FunctionDefinition):
            self.bodies.append([])
            self.read_command(node.body, False)
            body = self.bodies.pop()
            if node.name is not None:
                self.functions[node.name] = body
            return
        if isinstance(node, Simple) and (words := command_words(node)):
            self.commands.append((fed, node.line, words))
            if self.bodies:
                self.bodies[-1].append(words)
        for source in [text for word in expanded_words(node) for text in word.substitutions]:
            self.read_list(parse(source), fed and not (isinstance(source, Source) and source.form == ">("))
        for document in here_documents(node):
            for source in document.substitutions:
                try:
                    tree = parse(source)
                except BashSyntaxError:
                    continue  # a body's substitution Bash cannot read runs nothing, as bash_syntax reads it
                self.read_list(tree, fed)
        for branch in command_bodies(node):
            self.read_list(branch, fed)


def _read_bash(script: str) -> tuple[list[BashCommand], dict[str, list[list[str]]]]:
    """Each simple command a Bash script may run, as (whether a pipe feeds it, its line, its words from its program),
    and each function it defines, by name, with the words of each command its body runs, read through
    ejryan89-skill-core's bash_syntax, the repository's one Bash grammar: a command wherever Bash starts one, in a
    list, a pipeline, a compound command, a function's body, and each command or process substitution, and never in a
    comment, a here-document's body, or a case's word or patterns. Every command of a group or a compound command a
    pipe feeds, and of one inside it, reads that pipe, and so does each command of a substitution a fed command holds.
    Raises BashSyntaxError, a ValueError, for a script the grammar refuses, or one of whose substitutions it refuses:
    Bash runs the lines before a syntax error, so none of its pipes can be trusted as read."""
    reading = _BashReading()
    try:
        reading.read_list(parse(script), False)
    except RecursionError:
        raise BashSyntaxError("it nests deeper than the reader follows") from None
    return reading.commands, reading.functions


def _shell_scripts_run(commands: list[BashCommand], depth: int = 0) -> list[BashCommand]:
    """The commands each shell -c script and each eval among these commands runs, as _read_bash reads them, at the
    line of the command that runs it, and those of the scripts they run in turn; one the grammar refuses runs nothing
    this reader can see."""
    found: list[BashCommand] = []
    for _, line, words in commands:
        script = _shell_script(_command_words(words))
        if script is None or depth > NESTING_LIMIT:
            continue
        try:
            inner = [(fed, line, inner_words) for fed, _, inner_words in _read_bash(script)[0]]
        except ValueError:
            continue
        found += inner + _shell_scripts_run(inner, depth + 1)
    return found


def _sets_pipefail(script: str, commands: list[BashCommand]) -> bool:
    """Whether a script turns pipefail on: by set or shopt naming it, as `set -o pipefail`, `set -euo pipefail`,
    `shopt -so pipefail`, or `builtin set -o pipefail` write it, in the script or in a script it runs through a shell or
    eval; by a shell started with `-o pipefail`; or by SHELLOPTS naming it, which a shell started under it takes."""
    if SHELL_OPTIONS_VARIABLE.search(script):
        return True
    for _, _, words in commands:
        run = _command_words(words)
        program = bash_program(run[0]) if run else ""
        if PIPEFAIL in run and (program in OPTION_SETTERS or program in SCRIPT_SHELLS):
            return True
    return False


def pipe_race_problems(root: Path) -> list[str]:
    """Report each pipe into a reader that may end early, in a Bash regression suite that sets pipefail.

    grep -q, -m, -l, or -L, ripgrep's -q, -m, or -l, and head can exit before the command writing into them has
    written everything; the writer then dies of SIGPIPE, and pipefail fails the pipeline though the check held, so it
    fails at random. The suite is read through ejryan89-skill-core's bash_syntax, the repository's one Bash grammar,
    and one it refuses, or one of whose substitutions it refuses, is reported as unread. The reader is found in any
    shape around it: right after the pipe; in a brace or subshell group or a compound command (`if`, `while`, `until`,
    `for`, `select`, `case`) the pipe feeds; in the body of a function the pipe runs, whatever compound command the
    body is; in the script a shell's -c or eval runs; in a `$(...)`, backtick, or `<(...)` substitution, quoted or
    not, whether the pipe stands inside it or feeds the command, function body, or -c or eval script that holds it,
    whose standard input it reads; in the background in a group the pipe feeds, whose standard input Bash 5 keeps;
    named by a path; or behind an assignment, a reserved word, or a wrapper such as `env`
    (its -S line included), `exec`, `timeout`, `command`, `builtin`, or `sudo`, each named by a path or not; head is
    judged by the last count it is given. A suite turns pipefail on by set or shopt, in any spelling Bash takes, in the
    suite or in a script it runs through a shell or eval, by a shell started with `-o pipefail`, or by SHELLOPTS naming
    it. Each race is reported once.

    What it does not see: another reader that ends early, such as `read`, `sed q`, `awk` with `exit`, a loop that
    breaks, or a command that reads nothing; a reader in a `>(...)` substitution, which reads what its command
    writes rather than the pipe, in a script a file holds, as `bash script.sh` or `source` runs it, or behind a
    wrapper not named here, such as `coproc`; `head --help` or `--version`; and pipefail turned off again. A command
    in a fed group whose input is redirected from a file or a here-document is refused all the same.
    """
    problems: list[str] = []
    for suite in regression_suites(root):
        if not is_shell_script(suite):
            continue
        name = suite.relative_to(root).as_posix()
        try:
            text = suite.read_text(encoding="utf-8")
            commands, functions = _read_bash(text)
        except ValueError as error:
            problems.append(f"{name} cannot be read as Bash to find a pipe into a reader that ends early: {error}")
            continue
        commands += _shell_scripts_run(commands)
        if not _sets_pipefail(text, commands):
            continue
        races = {
            (line, reader) for fed, line, words in commands if fed and (reader := _early_exit_reader(words, functions))
        }
        problems += [
            f"{name}:{line} pipes into {reader} under pipefail, which fails the check when the writer dies "
            "of SIGPIPE after the reader ends; give it its text in a here-string or a file, or take a line by "
            "parameter expansion"
            for line, reader in sorted(races)
        ]
    return problems


def suite_python_modules(root: Path) -> list[Path]:
    """The Python the regression suites run: the root conftest.py, every module under tests/ outside tests/fixtures/,
    and each Python suite under a skill's scripts/."""
    found = {root / CONFTEST} if file_presence.is_present(root / CONFTEST) else set()
    tests = root / "tests"
    if file_presence.is_directory(tests):
        found |= {
            path
            for path in file_presence.matching(tests, "**/*.py", skip=lambda folder: folder == root / FIXTURE_ROOT)
            if file_presence.is_present(path, other_is_absent=True) and not within(path, root / FIXTURE_ROOT)
        }
    found |= {path for path in regression_suites(root) if path.suffix.casefold() == ".py"}
    return sorted(found, key=lambda path: path.relative_to(root).as_posix().casefold())


def _assertion_holders(node: ast.AST, aliases: dict[str, str]) -> Iterator[int]:
    """The line of each call that makes a unittest.TestCase itself, outside every class body."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            continue
        if isinstance(child, ast.Call) and qualified_name(child.func, aliases) == "unittest.TestCase":
            yield child.lineno
        yield from _assertion_holders(child, aliases)


def assertion_holder_problems(root: Path) -> list[str]:
    """Report a test module that makes a unittest.TestCase outside a class, as a holder of assertion methods.

    The lint rule set lets the suites assert, and pytest rewrites a plain assert to show both sides, so a function
    checks with assert, and a unittest test case with its own assertion methods.
    """
    problems: list[str] = []
    for _, name, tree in python_trees(suite_python_modules(root), root, problems):
        problems += [
            f"{name}:{line} makes a unittest.TestCase to check through; check with a plain assert, which pytest "
            "rewrites to show both sides"
            for line in _assertion_holders(tree, import_aliases(tree))
        ]
    return problems


def unsuited_script_problems(root: Path) -> list[str]:
    """Report a shipped or repository skill whose scripts/ holds executable files but no regression suite."""
    problems: list[str] = []
    for scripts in skill_script_directories(root):
        files = sorted(
            path
            for path in file_presence.matching(scripts, "**/*")
            if file_presence.is_present(path, other_is_absent=True)
        )
        if any(is_executable_script(path) and not is_test_script(path) for path in files) and not any(
            is_test_script(path) for path in files
        ):
            problems.append(
                f"{scripts.relative_to(root).as_posix()} holds scripts but no regression suite; add a "
                f"{listed(SUITE_NAME_PATTERNS)} Python, Bash, or PowerShell script beside them"
            )
    return problems


# The calls that load a module from a file by its path, by their last name, and the position and keyword of the path.
PATH_LOADERS = {
    "spec_from_file_location": (1, "location"),
    "SourceFileLoader": (1, "path"),
    "run_path": (0, "path_name"),
}


class RepositoryModules:
    """The repository modules a Python file imports, resolved as they are at run time.

    A dotted import resolves beside the file, then from the repository root, as deployer.x, tools.x, and tests.x
    do; a relative import from the file's package; and a name each candidate resolves to in the mypy_path
    directories, which validation keeps equal to the directories code puts on sys.path. A standard-library name
    resolves beside the file only, since the interpreter finds its own module before any other directory's. A
    module named by a literal to importlib.import_module or __import__ is imported as a statement imports it, and a
    file loaded by its path, by spec_from_file_location, a SourceFileLoader, or runpy.run_path, is found by the
    literal end of the path: beside the file, at the root, or every repository file whose path ends so. A file no
    policy can read imports nothing here, and unreadable_problems names it.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        pyproject = root / "pyproject.toml"
        configuration = (
            tomllib.loads(pyproject.read_text(encoding="utf-8")) if file_presence.is_present(pyproject) else {}
        )
        entries = configuration.get("tool", {}).get("mypy", {}).get("mypy_path", [])
        self.path_directories = [root / entry.removeprefix(MYPY_PATH_PREFIX) for entry in entries]
        self._sources: dict[Path, str] = {}
        self._trees: dict[Path, ast.Module] = {}
        self._imports: dict[tuple[Path, bool, bool], list[Path]] = {}
        self._resolved: dict[tuple[Path, str, int], list[Path]] = {}
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

    @staticmethod
    def _module_file(base: Path, parts: list[str]) -> Path | None:
        target = base.joinpath(*parts)
        for candidate in (target.with_name(f"{target.name}.py"), target / "__init__.py"):
            if file_presence.is_present(candidate, other_is_absent=True):
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
                if (
                    candidate is not None
                    and file_presence.is_present(candidate, other_is_absent=True)
                    and candidate.resolve() not in found
                ):
                    found.append(candidate.resolve())
            if found and not level:
                break
        return [file for file in found if within(file, self.root.resolve())]

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
            candidates = [
                base / ending
                for base in (path.parent, self.root)
                if file_presence.is_present(base / ending, other_is_absent=True)
            ][:1]
            if not candidates:
                candidates = [
                    file
                    for file in file_presence.matching(self.root, f"**/{tail[-1]}")
                    if f"/{file.relative_to(self.root).as_posix()}".endswith(f"/{ending}")
                ]
            if not unique or len(candidates) == 1:
                found += [candidate.resolve() for candidate in candidates]
        return found

    def imports(self, path: Path, unique_loads: bool = False, by_path: bool = True) -> list[Path]:
        """The repository files path's imports may load, in the order they appear: import statements, literal names
        given importlib.import_module or __import__, and, unless by_path is False, files loaded by path, every file a
        load may name, or, with unique_loads, only a load whose path names one file."""
        key = (path, unique_loads, by_path)
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
                and ast.unparse(node.func).rsplit(".", 1)[-1] in MODULE_LOADERS
                and isinstance(argument := call_argument(node, 0, "name"), ast.Constant)
                and isinstance(argument.value, str)
            ):
                names = [(argument.value, 0)]
            else:
                continue
            found += [file for name, level in names for file in self.resolve(path, name, level)]
        if by_path:
            found += self.loaded_files(path, unique_loads)
        self._imports[key] = list(dict.fromkeys(found))
        return self._imports[key]


# The autouse fixtures the root conftest.py isolates git with, against the scope each declares: a test's own home, and
# the homes a class's and a module's fixtures run in.
ISOLATION_FIXTURES = {"isolated_git_module": "module", "isolated_git_class": "class", "git_home": "function"}
# The configuration pytest runs every suite with, as pyproject.toml's [tool.pytest.ini_options] holds it: no cache
# written into the checkout, output not captured, every strictness option pytest has on (misspelled markers and
# options and duplicate parametrization ids refused), and nothing selected, deselected, or skipped.
PYTEST_CONFIGURATION = {
    "addopts": ["-p", "no:cacheprovider", "--capture=no"],
    "strict": True,
}
# The files pytest reads its configuration from, in the order it tries them in each folder, from the folder of the
# suite it is given up to the root; the first match wins. pytest.toml takes precedence even when empty, and so does
# pytest.ini; pyproject.toml, tox.ini, and setup.cfg match with a pytest table, and when nothing matches, the nearest
# pyproject.toml is the configuration file even without one. The root holds pyproject.toml alone, so the configuration
# stays in one file, and no folder below it holds any of them, whatever it contains, since the runner starts
# `python -B -m pytest <suite>` with no -c or --rootdir: pytest would read a nearer pytest.toml, .pytest.toml,
# pytest.ini, or .pytest.ini instead even when it is empty, a nearer pyproject.toml, tox.ini, or setup.cfg when it
# holds a pytest table, and a nearer pyproject.toml without one whenever the root's lost its table.
PYTEST_CONFIGURATION_FILES = (
    "pytest.toml",
    ".pytest.toml",
    "pytest.ini",
    ".pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
)


def _autouse_scope(function: ast.FunctionDef | ast.AsyncFunctionDef, aliases: dict[str, str]) -> str | None:
    """The scope a function declares as an autouse pytest fixture, or None when it is not one."""
    for decorator in function.decorator_list:
        if not (isinstance(decorator, ast.Call) and qualified_name(decorator.func, aliases) == "pytest.fixture"):
            continue
        keywords = {keyword.arg: keyword.value for keyword in decorator.keywords}
        autouse = keywords.get("autouse")
        scope = keywords.get("scope", ast.Constant("function"))
        if isinstance(autouse, ast.Constant) and autouse.value is True and isinstance(scope, ast.Constant):
            return str(scope.value)
    return None


def _suite_python_files(root: Path) -> list[Path]:
    """The Python under tests/, outside tests/fixtures/, and under each shipped or repository skill's scripts/."""
    tops = [root / "tests", *skill_script_directories(root)]
    return sorted(
        path
        for top in tops
        if file_presence.is_directory(top, other_is_absent=True)
        for path in file_presence.matching(
            top, "**/*.py", skip=lambda folder: folder == root / FIXTURE_ROOT or folder.name == "__pycache__"
        )
        if not within(path, root / FIXTURE_ROOT) and "__pycache__" not in path.parts
    )


def _overrides(tree: ast.Module) -> list[tuple[int, str]]:
    """Each definition, module- or class-level assignment of a value, or parametrize of an isolation fixture's name,
    which would take the fixture's place for the tests beneath it. An annotation alone binds nothing, so a test case
    may declare the `git_home` the fixture gives it."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and node.name in ISOLATION_FIXTURES:
            found.append((node.lineno, node.name))
        if (
            isinstance(node, ast.Call)
            and ast.unparse(node.func).rsplit(".", 1)[-1] == "parametrize"
            and isinstance(names := call_argument(node, 0, "argnames"), ast.Constant)
            and isinstance(names.value, str)
        ):
            found += [
                (node.lineno, name.strip()) for name in names.value.split(",") if name.strip() in ISOLATION_FIXTURES
            ]
        if isinstance(node, ast.Module | ast.ClassDef):
            for statement in node.body:
                targets = statement.targets if isinstance(statement, ast.Assign) else []
                if isinstance(statement, ast.AugAssign) or (
                    isinstance(statement, ast.AnnAssign) and statement.value is not None
                ):
                    targets = [statement.target]
                found += [
                    (statement.lineno, target.id)
                    for target in targets
                    if isinstance(target, ast.Name) and target.id in ISOLATION_FIXTURES
                ]
    return found


def git_isolation_problems(root: Path) -> list[str]:
    """Report a root conftest.py that does not define each isolation fixture as an autouse fixture of its scope, a
    conftest.py below the root, and a suite or helper that defines or assigns a fixture's name.

    Every Python suite runs through pytest from the root, which loads the root conftest.py for it, so its autouse
    fixtures isolate git for every test, class fixture, and module fixture there is. A nearer conftest.py or a
    definition of the same name would take a fixture's place for the tests beneath it, and the shell that started the
    suite would reach its git again: a command-scope setting outranks every file, a global hook runs on a fixture's
    commit, and a hook's GIT_DIR points every command at another repository.
    """
    problems: list[str] = []
    conftest = root / CONFTEST
    trees = list(python_trees([conftest] if file_presence.is_present(conftest) else [], root, problems))
    if not trees:
        problems.append(f"{CONFTEST} is missing; its autouse fixtures isolate git for every Python suite")
    for _, name, tree in trees:
        aliases = import_aliases(tree)
        declared = {
            node.name: _autouse_scope(node, aliases)
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        problems += [
            f"{name} does not define {fixture} as an autouse fixture of {scope} scope, which isolates git for each "
            f"{'test' if scope == 'function' else scope} of every suite"
            for fixture, scope in ISOLATION_FIXTURES.items()
            if declared.get(fixture) != scope
        ]
    files = _suite_python_files(root)
    problems += [
        f"{relative_name} is a conftest.py below the root; the root's conftest.py is the one that sets up every suite"
        for path in files
        if path.name == CONFTEST and (relative_name := path.relative_to(root).as_posix())
    ]
    for _, name, tree in python_trees(files, root, problems):
        problems += [
            f"{name}:{line} defines {fixture}, which takes the place of the root conftest.py's fixture that isolates "
            "git"
            for line, fixture in _overrides(tree)
        ]
    return problems


def _folders_below(suite: Path, root: Path) -> list[Path]:
    """The folders from a suite's own up to the root, the root left out: none for a suite directly in the root, or
    one outside it. Where the suite lies in the root is path_containment's answer."""
    relative = relative_within(suite.parent, root)
    parts = relative.parts if relative is not None else ()
    return [root.joinpath(*parts[:depth]) for depth in range(len(parts), 0, -1)]


def pytest_configuration_problems(root: Path) -> list[str]:
    """Report a pytest configuration other than PYTEST_CONFIGURATION, another configuration file at the root, or one
    in a suite's folder or a folder between it and the root, which pytest may read instead.

    A configuration could select, deselect, or skip tests, or turn the conftest plugin off, for every suite at once,
    so it is pinned here rather than read.
    """
    problems = [
        f"{name} is a pytest configuration file beside pyproject.toml; keep the configuration in "
        "[tool.pytest.ini_options] alone"
        for name in PYTEST_CONFIGURATION_FILES
        if name != "pyproject.toml" and file_presence.is_present(root / name, other_is_absent=True)
    ]
    below: dict[Path, Path] = {}
    for suite in regression_suites(root):
        for folder in _folders_below(suite, root):
            below.update(
                {
                    folder / name: suite
                    for name in PYTEST_CONFIGURATION_FILES
                    if file_presence.is_present(folder / name, other_is_absent=True)
                }
            )
    problems += [
        f"{path.relative_to(root).as_posix()} is a pytest configuration file pytest may read for "
        f"{suite.relative_to(root).as_posix()} instead of the root's pyproject.toml; keep the configuration there"
        for path, suite in sorted(below.items())
    ]
    configuration = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    table = configuration.get("tool", {}).get("pytest", {})
    if table != {"ini_options": PYTEST_CONFIGURATION}:
        problems.append(f"pyproject.toml: [tool.pytest] must be ini_options = {PYTEST_CONFIGURATION!r}, not {table!r}")
    return problems


# The builder of the command the runner starts a Python suite with, which the pytest-root policy reads as a pytest
# start wherever a suite calls it.
PYTEST_COMMAND = "suite_discovery.pytest_command"
# The text of a module that may start pytest: a call of pytest_command, or a command list holding `-m pytest`. The
# pytest-root policy reads a suite whose text, or the text of a module it imports, holds one, and follows a call into
# a module whose text holds one, a stated limit: a function that starts pytest only through a module that names
# neither is not followed.
PYTEST_START_TEXT = re.compile(r"""pytest_command|["']-m["']\s*,\s*["']pytest["']""")
# The text of a suite that may run a repository script as a program, whose own pytest the policy reads too: the
# interpreter's name, however the module imports or binds it, and a literal naming a Python file, which the path of a
# script this policy can read ends with. A suite with neither is not read for a script it starts; one with both is
# read through its syntax tree, which resolves the interpreter through the module's imports.
SCRIPT_START_TEXT = (re.compile(r"\bexecutable\b"), re.compile(r"""["'][^"'\s]*\.py["']"""))
# The options that pin a pytest run to a folder: --rootdir roots the run there and --confcutdir reads no conftest.py
# above it, so either, naming the folder of the suite the run starts, keeps pytest from collecting any folder above.
ROOT_PINNING_OPTIONS = frozenset({"--confcutdir", "--rootdir"})
# The pytest options, pytest's own and the root conftest.py's, that take the next argument as their value, so a
# command's argument after one is that value, never a suite. Every other argument that is not a literal is a suite.
PYTEST_VALUE_OPTIONS = frozenset(
    {
        *ROOT_PINNING_OPTIONS,
        "-c",
        "--config-file",
        "--basetemp",
        "-p",
        "-k",
        "-m",
        "-o",
        "--override-ini",
        "--ignore",
        "--ignore-glob",
        "--deselect",
        "-W",
        "--pythonwarnings",
        "--junit-xml",
        "--log-file",
        "--suite-shard",
        "--test-seconds",
    }
)
# The options that name the configuration file a pytest run reads, in place of any beside the suite: with one, a
# pytest.ini beside the suite is not read and the run's confcutdir defaults to the named file's folder, whatever
# --rootdir says, so only --confcutdir naming the suite's folder keeps pytest from collecting the folders above it.
CONFIGURATION_OPTIONS = frozenset({"-c", "--config-file"})
# The file a pytest started on a suite outside the repository finds beside the suite: pytest takes it as the run's
# configuration even when it is empty, and roots the run at its folder.
PYTEST_INI = "pytest.ini"
# The path methods that write a file, any of which leaves a pytest.ini that pytest reads.
FILE_WRITES = frozenset({"write_text", "write_bytes", "touch"})
# The calls that give a path back in another spelling; skill_scripts' PATH_METHODS are the path methods that give
# back the same path.
PATH_SPELLINGS = frozenset({"str", "os.fspath", "pathlib.Path", "os.path.abspath"})
# The interpreter's options that take the next argument as their value, in a command that starts a repository script
# with skill_scripts' INTERPRETER; -m and -c end its options with a module or a program text, which no script file
# names.
INTERPRETER_VALUE_OPTIONS = frozenset({"-X", "-W"})
INTERPRETER_PROGRAMS = ("-m", "-c")
# The argparse actions of an option that takes the next argument as its value; any other action is read as a flag, so
# the argument after it is read as a suite.
VALUE_ACTIONS = frozenset({"store", "append", "extend"})
# The characters no folder or file name a script's path is built from may hold: Windows refuses each in a name.
UNNAMEABLE = frozenset('<>:"|?*')
# The name pytest_root_problems reads a pytest.ini by, when a suite writes it by the constant rather than the literal.
PYTEST_INI_NAME = "suite_discovery.PYTEST_INI"
# The call that spells a path relative to the repository's root, which a pytest started there finds in the repository.
REPOSITORY_RELATIVE = frozenset({"validation_support.relative"})
# The calls that make or name a folder under the temporary directory, which lies outside the repository.
TEMPORARY_FOLDERS = frozenset({"tempfile.mkdtemp", "tempfile.TemporaryDirectory", "tempfile.gettempdir"})

# What a path expression names, as _path_key reads it.
PathKey = tuple[object, ...]
FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def _parent_key(key: PathKey | None) -> PathKey | None:
    """The key of the folder that holds the path a key names."""
    if key is None:
        return None
    if key[0] == "join":
        folder: PathKey = key[1]  # type: ignore[assignment]  # a join holds its folder's key first
        return folder
    return ("parent", key)


def _path_key(node: ast.expr, resolver: CallableResolver, seen: frozenset[int] = frozenset()) -> PathKey | None:
    """What a path expression names, as a key that two spellings of one path share: a name is followed to the one value
    bound to it, a conversion to str or to a Path and a resolve() are the path they convert, `<folder> / "<name>"` joins
    each literal name to the folder's key, and `.parent` and `.parents[<n>]` step up from a join to its folder. A name
    nothing binds by assignment, a parameter or a loop's target among them, is ("name", <name>, <qualified name>), and
    any other expression ("node", <expression>), which no other expression shares. None for a name bound more than
    once."""
    if (
        isinstance(node, ast.Call)
        and not node.keywords
        and len(node.args) == 1
        and not isinstance(node.args[0], ast.Starred)
        and resolver.name(node.func) in PATH_SPELLINGS
    ):
        return _path_key(node.args[0], resolver, seen)
    if (
        isinstance(node, ast.Call)
        and not node.args
        and not node.keywords
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in PATH_METHODS
    ):
        return _path_key(node.func.value, resolver, seen)
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        values = bound_values(node, resolver)
        if values == [node]:
            return ("name", node.id, resolver.name(node))
        if len(values) != 1 or id(values[0]) in seen:
            return None
        return _path_key(values[0], resolver, seen | {id(node)})
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        key = _path_key(node.left, resolver, seen)
        if key is None:
            return None
        if not (isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
            return ("join", key, ("node", node.right))
        for part in (part for part in re.split(r"[/\\]", node.right.value) if part):
            key = ("join", key, part)
        return key
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        return _parent_key(_path_key(node.value, resolver, seen))
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        and isinstance(node.slice, ast.Constant)
        and type(node.slice.value) is int
        and node.slice.value >= 0
    ):
        found = _path_key(node.value.value, resolver, seen)
        for _ in range(node.slice.value + 1):
            found = _parent_key(found)
        return found
    return ("node", node)


def _key_base(key: PathKey) -> PathKey:
    """The key a path's joins and steps up start from."""
    while key[0] in {"join", "parent"}:
        key = key[1]  # type: ignore[assignment]  # a join or a step up holds the key it starts from
    return key


def _base_level(base: PathKey, resolver: CallableResolver, depth: int | None) -> int | None:
    """How many folders below the repository's root a path's base lies, for a pytest started at the root, or None when
    it does not lie in the repository: a literal that path_containment reads as a relative path inside a tree on every
    platform, which names a path below the working directory; a path validation_support.relative spells relative to
    the root; the module's own __file__, `depth` folders down; or REPOSITORY_ROOT, the root itself."""
    if base[0] == "name":
        if base[1] == "__file__":
            return depth
        return 0 if str(base[2]).rsplit(".", 1)[-1] == "REPOSITORY_ROOT" else None
    node = base[1]
    if isinstance(node, ast.Call):
        return 1 if resolver.name(node.func) in REPOSITORY_RELATIVE else None
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and written_refusal(node.value) is None:
        return len([part for part in re.split(r"[/\\]", node.value) if part not in {"", "."}])
    return None


def _in_repository(key: PathKey, resolver: CallableResolver, depth: int | None) -> bool:
    """Whether the path a key names lies in the repository for a pytest started at its root: its base does, and no step
    up from it, by `.parent`, `.parents[<n>]`, or a `..` joined, climbs above the root. A name joined that is not a
    literal is read as one folder down, a stated limit: a name holding `..` or an absolute path is not read."""
    steps: list[PathKey] = []
    while key[0] in {"join", "parent"}:
        steps.append(key)
        key = key[1]  # type: ignore[assignment]  # a join or a step up holds the key it starts from
    level = _base_level(key, resolver, depth)
    if level is None:
        return False
    for step in reversed(steps):
        level += -1 if step[0] == "parent" or step[2] == ".." else 0 if step[2] == "." else 1
        if level < 0:
            return False
    return True


def _parameter_names(function: FunctionNode) -> set[str]:
    arguments = function.args
    extra = [argument for argument in (arguments.vararg, arguments.kwarg) if argument is not None]
    return {argument.arg for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, *extra]}


def _pin_option(item: ast.expr) -> tuple[str, ast.expr] | None:
    """The option and the value an f-string `--rootdir={folder}` or `--confcutdir={folder}` names, or None."""
    if not (isinstance(item, ast.JoinedStr) and len(item.values) == 2):
        return None
    option, value = item.values
    if (
        isinstance(option, ast.Constant)
        and isinstance(value, ast.FormattedValue)
        and value.format_spec is None
        and value.conversion == -1
        and str(option.value).endswith("=")
        and str(option.value).removesuffix("=") in ROOT_PINNING_OPTIONS
    ):
        return str(option.value).removesuffix("="), value.value
    return None


def _operands(operands: list[ast.expr]) -> tuple[list[ast.expr], list[tuple[str, ast.expr]], bool]:
    """The suites among the arguments a pytest command gives pytest, each root-pinning option with the value it names,
    and whether an option names the configuration file. A literal is an option or a relative path, the argument after
    an option of PYTEST_VALUE_OPTIONS is its value, an f-string that opens with a dash is an option, and a starred item
    is read as options, a stated limit: a suite it spreads in a command list is not read."""
    suites: list[ast.expr] = []
    pins: list[tuple[str, ast.expr]] = []
    configured = False
    index = 0
    while index < len(operands):
        item = operands[index]
        index += 1
        if isinstance(item, ast.Starred):
            continue
        if isinstance(item, ast.Constant):
            option = str(item.value)
            configured = configured or option in CONFIGURATION_OPTIONS or option.startswith("--config-file=")
            if option in PYTEST_VALUE_OPTIONS and index < len(operands):
                if option in ROOT_PINNING_OPTIONS:
                    pins.append((option, operands[index]))
                index += 1
            continue
        pinned = _pin_option(item)
        lead = item.values[0] if isinstance(item, ast.JoinedStr) and item.values else None
        option = str(lead.value) if isinstance(lead, ast.Constant) else ""
        if pinned is not None:
            pins.append(pinned)
        elif option.startswith("-"):
            # An f-string that opens with a dash is an option, such as `--config-file={path}`, never a suite.
            configured = configured or option.startswith(("--config-file=", "-c"))
        else:
            suites.append(item)
    return suites, pins, configured


@dataclass(frozen=True)
class _PytestStart:
    """One pytest a function starts from the repository: the line, the suites it runs, each --confcutdir or --rootdir
    with the value it names, whether it names its configuration file, and, for a start this policy cannot read, why."""

    line: int
    suites: tuple[ast.expr, ...]
    pins: tuple[tuple[str, ast.expr], ...] = ()
    configured: bool = False
    unread: str = ""
    # For a script started as a program, the options its parser declares that take a value.
    options: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _Verdict:
    """What a pytest start does with one suite: "" when the suite lies in the repository or the start pins its root;
    "parameter" or "each" when the suite is a module-level function's parameter, or each item of one, which its callers
    pin; "spread" when a module-level function spreads its own starred parameter into a script's arguments, which its
    callers give; "temporary" when it lies under the temporary folder and nothing pins its root; and "unread", with
    why, when this policy cannot place it."""

    kind: str
    detail: str = ""


@dataclass(frozen=True)
class _Passed:
    """A parameter of a module-level function whose argument holds a suite it starts pytest on from the repository with
    no root pinned, which its callers must pin: its position among the positional parameters (None for a keyword-only
    one, and the count of them for a starred one), its name, and whether its argument is the suite ("one"), a list of
    suites ("each"), or, for a starred parameter, each argument from its position on, given to a script whose parser
    takes the options that take a value ("spread"); and whether its start names its configuration file."""

    position: int | None
    name: str
    kind: str
    configured: bool
    options: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _Passing:
    """The parameters a module-level function passes suites through, and why any start it holds cannot be read."""

    parameters: tuple[_Passed, ...] = ()
    unread: tuple[str, ...] = ()


class _ModuleReader:
    """A module's syntax tree, its bindings, each node's parent and innermost function, and each function's calls and
    command lists."""

    def __init__(self, tree: ast.Module, depth: int | None) -> None:
        self.tree = tree
        # How many folders below the repository's root the module's own file lies, or None outside it.
        self.depth = depth
        self.resolver = CallableResolver(tree)
        self.parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        self.functions: dict[int, FunctionNode | None] = {}
        # The calls, lists, tuples, and loops of each function by its id, and of the module's own statements by
        # id(None).
        self.candidates: dict[int, list[ast.AST]] = {}
        pending: list[tuple[ast.AST, FunctionNode | None]] = [(tree, None)]
        while pending:
            node, function = pending.pop()
            self.functions[id(node)] = function
            if isinstance(node, ast.Call | ast.List | ast.Tuple | ast.For):
                self.candidates.setdefault(id(function), []).append(node)
            inner = node if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) else function
            pending += [(child, inner) for child in ast.iter_child_nodes(node)]
        self.module_functions = module_functions(tree)


def _names_pytest_ini(node: ast.expr, resolver: CallableResolver) -> bool:
    """Whether an expression names pytest.ini: the literal, PYTEST_INI imported from here, or a name bound to either."""
    if resolver.name(node) == PYTEST_INI_NAME:
        return True
    values = bound_values(node, resolver) if isinstance(node, ast.Name) else [node]
    return len(values) == 1 and (
        (isinstance(values[0], ast.Constant) and values[0].value == PYTEST_INI)
        or (values[0] is not node and resolver.name(values[0]) == PYTEST_INI_NAME)
    )


def _pytest_calls(function: FunctionNode, resolver: CallableResolver) -> bool:
    """Whether pytest calls a function itself, giving it its parameters: a test, by the name pytest collects one by, or
    a function decorated as a pytest fixture."""
    return function.name.startswith("test") or any(
        resolver.name(decorator.func if isinstance(decorator, ast.Call) else decorator) == "pytest.fixture"
        for decorator in function.decorator_list
    )


def script_parser(tree: ast.Module) -> tuple[frozenset[str], bool]:
    """The options a script's argparse parser declares that take a value, and whether it takes a positional argument:
    each add_argument call the module makes, its option strings from its literal arguments. An option whose action is
    not one of VALUE_ACTIONS, or whose nargs is 0, is a flag."""
    options: set[str] = set()
    positional = False
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"
        ):
            continue
        names = [str(item.value) for item in node.args if isinstance(item, ast.Constant)]
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        action, nargs = keywords.get("action"), keywords.get("nargs")
        if names and not any(name.startswith("-") for name in names):
            positional = True
        elif (action is None or (isinstance(action, ast.Constant) and action.value in VALUE_ACTIONS)) and not (
            isinstance(nargs, ast.Constant) and nargs.value == 0
        ):
            options |= {name for name in names if name.startswith("-")}
    return frozenset(options), positional


def _script_index(items: list[ast.expr]) -> int | None:
    """Where a command list that opens with the interpreter names its script: the first item after the interpreter's
    options, the value -X or -W takes skipped; None when -m or -c ends them with a module or a program text, or
    nothing follows them."""
    index = 1
    while index < len(items) and isinstance(flag := items[index], ast.Constant) and str(flag.value).startswith("-"):
        if str(flag.value).startswith(INTERPRETER_PROGRAMS):
            return None
        index += 2 if flag.value in INTERPRETER_VALUE_OPTIONS else 1
    return index if index < len(items) else None


def _names_interpreter(node: ast.expr, aliases: dict[str, str], names: Mapping[str, list[ast.expr]]) -> bool:
    """Whether an expression names the interpreter through the module's imports, directly or through a name the module
    binds once to it, without the binding reader."""
    if qualified_name(node, aliases) == INTERPRETER:
        return True
    values = names.get(node.id, []) if isinstance(node, ast.Name) else []
    return len(values) == 1 and qualified_name(values[0], aliases) == INTERPRETER


def _script_arguments(items: list[ast.expr], options: frozenset[str]) -> list[ast.expr]:
    """The suites among the arguments a command gives a script: every argument but a literal, which is an option or a
    relative path, the value after an option of `options`, and an f-string that opens with a dash. A starred item is
    kept, and judged by the folders its function makes."""
    suites: list[ast.expr] = []
    index = 0
    while index < len(items):
        item = items[index]
        index += 1
        if isinstance(item, ast.Constant):
            index += 1 if str(item.value) in options else 0
            continue
        lead = item.values[0] if isinstance(item, ast.JoinedStr) and item.values else None
        if isinstance(lead, ast.Constant) and str(lead.value).startswith("-"):
            continue
        suites.append(item)
    return suites


def _concrete_path(key: PathKey, file: Path, root: Path) -> Path | None:
    """The path a key names, from the module's own file, REPOSITORY_ROOT, or a relative literal at the root, through
    literal joins and steps up; None for any other key, or one whose names Windows refuses."""
    if key[0] == "join":
        folder = _concrete_path(key[1], file, root)  # type: ignore[arg-type]  # a join holds its folder's key first
        part = key[2]
        if folder is None or not isinstance(part, str) or UNNAMEABLE & set(part):
            return None
        return folder.parent if part == ".." else folder if part == "." else folder / part
    if key[0] == "parent":
        inner = _concrete_path(key[1], file, root)  # type: ignore[arg-type]  # a step up holds the key it starts from
        return inner.parent if inner is not None else None
    if key[0] == "name":
        return file if key[1] == "__file__" else root if str(key[2]).rsplit(".", 1)[-1] == "REPOSITORY_ROOT" else None
    node = key[1]
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and written_refusal(node.value) is None:
        parts = [part for part in re.split(r"[/\\]", node.value) if part not in {"", "."}]
        return None if any(UNNAMEABLE & set(part) for part in parts) else root.joinpath(*parts)
    return None


def module_functions(tree: ast.Module) -> dict[str, FunctionNode]:
    """The functions a module defines at its top level, in a module-level block too, by name."""
    return {
        node.name: node
        for node in module_statements(tree.body)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


class _PytestRoots:
    """The pytest runs a Python file starts, directly through pytest_command or a command list holding `-m pytest`, or
    through a module-level function, its own or one a repository module it imports defines, that starts pytest on a
    suite its caller gives it; and whether each suite it starts lies in the repository or has its root pinned."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.modules = RepositoryModules(root)
        self._readers: dict[Path, _ModuleReader] = {}
        self._passing: dict[tuple[Path, str], _Passing] = {}

    def reader(self, path: Path) -> _ModuleReader:
        if path not in self._readers:
            # RepositoryModules resolves the modules it finds, so the root is compared resolved too: a root under a
            # folder a link names, as macOS's /var or a Windows 8.3 short name, would otherwise place no module in it.
            relative = relative_within(path.resolve(), self.root.resolve())
            depth = len(relative.parts) if relative is not None else None
            self._readers[path] = _ModuleReader(self.modules.tree(path), depth)
        return self._readers[path]

    def names_a_start(self, path: Path) -> bool:
        """Whether a module's text holds a pytest start, or that of a repository module it imports by name does: the
        modules whose functions a call can name."""
        return any(
            PYTEST_START_TEXT.search(self.modules.source(module))
            for module in [path, *self.modules.imports(path, by_path=False)]
        )

    def names_a_script(self, path: Path) -> bool:
        """Whether a suite may run a repository script that may start pytest as a program: its text holds
        SCRIPT_START_TEXT, and a command list it holds opens with the interpreter, resolved through the module's
        imports, and names, after the interpreter's options, a path whose literal end, joined to the suite's folder or
        any folder above it up to the root, is a repository file whose text, or a module's it imports by name, names a
        pytest start. Read without the binding reader, which only such a suite is given; it can only choose more
        suites than the reader finds a start in, never fewer."""
        text = self.modules.source(path)
        if not all(pattern.search(text) for pattern in SCRIPT_START_TEXT):
            return False
        tree = self.modules.tree(path)
        names = name_values(tree)
        aliases = import_aliases(tree)
        folders = [path.parent, *(folder for folder in path.parent.parents if within(folder, self.root))]
        for node in ast.walk(tree):
            if not (isinstance(node, ast.List | ast.Tuple) and node.elts):
                continue
            index = _script_index(node.elts)
            if index is None or not _names_interpreter(node.elts[0], aliases, names):
                continue
            parts = path_parts(node.elts[index], names)
            tail = [str(part) for part in itertools.takewhile(lambda part: part is not None, reversed(parts))][::-1]
            if not tail or not tail[-1].endswith(".py") or any(UNNAMEABLE & set(part) for part in tail):
                continue
            for folder in folders:
                candidate = folder
                for part in tail:
                    candidate = candidate.parent if part == ".." else candidate if part == "." else candidate / part
                if (
                    within(candidate, self.root)
                    and file_presence.is_present(candidate, other_is_absent=True)
                    and self.names_a_start(candidate)
                ):
                    return True
        return False

    def callee(self, path: Path, call: ast.Call) -> tuple[Path, FunctionNode] | None:
        """The module-level function a call runs, defined in the file or in a repository module it imports whose text
        holds a pytest start, or None."""
        name = self.reader(path).resolver.name(call.func)
        if not name or name == PYTEST_COMMAND or name in PROCESS_STARTS:
            return None
        module, _, function = name.rpartition(".")
        if module == "<local>":
            found = self.reader(path).module_functions.get(function)
            return (path, found) if found is not None else None
        # A name the resolver gives through a binding of the module's own, such as `<local>.helper`, names no module,
        # and is never looked up as a file: Windows refuses `<` and `>` in a path.
        if not module or not all(part.isidentifier() for part in module.split(".")):
            return None
        for file in self.modules.resolve(path, module):
            if file != path and PYTEST_START_TEXT.search(self.modules.source(file)):
                found = module_functions(self.modules.tree(file)).get(function)
                if found is not None:
                    return file, found
        return None

    def starts(self, path: Path, scope: FunctionNode | None) -> list[_PytestStart]:
        """The pytest runs a function, or the module's own statements, start from the repository."""
        reader = self.reader(path)
        found: list[_PytestStart] = []
        for node in reader.candidates.get(id(scope), []):
            if isinstance(node, ast.Call) and reader.resolver.name(node.func) == PYTEST_COMMAND:
                found += self._command_start(reader, node)
            elif isinstance(node, ast.List | ast.Tuple):
                found += self._list_start(reader, node) or self._script_start(path, reader, node)
            elif isinstance(node, ast.Call) and (callee := self.callee(path, node)) is not None:
                found += self._passed_starts(node, *callee)
        return found

    @staticmethod
    def _started(reader: _ModuleReader, node: ast.AST) -> tuple[bool, ast.expr | None]:
        """Whether a pytest command is started, and the working directory it is started in: it is unless an
        assertion compares it before a process start runs it."""
        current = node
        while (parent := reader.parents.get(id(current))) is not None and not isinstance(parent, ast.stmt):
            if isinstance(parent, ast.Call):
                if reader.resolver.name(parent.func) in PROCESS_STARTS:
                    return True, call_argument(parent, None, "cwd")
                if isinstance(parent.func, ast.Attribute) and parent.func.attr.startswith("assert"):
                    return False, None
            current = parent
        return not isinstance(parent, ast.Assert), None

    @staticmethod
    def _from_repository(reader: _ModuleReader, directory: ast.expr | None) -> bool:
        """Whether a working directory lies in the repository: none, which a suite inherits from the runner, or one
        this policy reads as a folder of the repository. A pytest started in any other folder roots there, as the
        mutation gate's in its throwaway worktree does, a stated limit."""
        if directory is None:
            return True
        key = _path_key(directory, reader.resolver)
        return key is not None and _in_repository(key, reader.resolver, reader.depth)

    def _command_start(self, reader: _ModuleReader, call: ast.Call) -> list[_PytestStart]:
        parent = reader.parents.get(id(call))
        outer = reader.parents.get(id(parent)) if isinstance(parent, ast.Starred) else None
        listed = outer if isinstance(outer, ast.List | ast.Tuple) else None
        started, directory = self._started(reader, listed if listed is not None else call)
        if not started or not self._from_repository(reader, directory):
            return []
        if not call.args or isinstance(call.args[0], ast.Starred):
            return [_PytestStart(call.lineno, (), unread="pytest_command is given no one suite")]
        after = listed.elts[[id(item) for item in listed.elts].index(id(parent)) + 1 :] if listed is not None else []
        suites, pins, configured = _operands([*call.args[1:], *after])
        return [_PytestStart(call.lineno, (call.args[0], *suites), tuple(pins), configured)]

    def _list_start(self, reader: _ModuleReader, display: ast.List | ast.Tuple) -> list[_PytestStart]:
        items = display.elts
        found = next(
            (
                index + 2
                for index in range(len(items) - 1)
                if isinstance(first := items[index], ast.Constant)
                and first.value == "-m"
                and isinstance(second := items[index + 1], ast.Constant)
                and second.value == "pytest"
            ),
            None,
        )
        if found is None:
            return []
        started, directory = self._started(reader, display)
        if not started or not self._from_repository(reader, directory):
            return []
        suites, pins, configured = _operands(items[found:])
        return [_PytestStart(display.lineno, tuple(suites), tuple(pins), configured)]

    def script(self, path: Path, node: ast.expr) -> frozenset[str] | None:
        """The options that take a value of the repository script a command names, when it may start pytest on the
        suites its command line gives it: its text, or a repository module's it imports by name, names a pytest start,
        and its parser takes a positional argument, which this policy reads as the suites, without following the
        parsed arguments to the start, a stated limit; None for any other program."""
        key = _path_key(node, self.reader(path).resolver)
        file = _concrete_path(key, path, self.root) if key is not None else None
        if (
            file is None
            or file.suffix != ".py"
            or not within(file.resolve(), self.root.resolve())
            or not file_presence.is_present(file, other_is_absent=True)
            or not self.names_a_start(file)
        ):
            return None
        options, positional = script_parser(self.modules.tree(file))
        return options if positional else None

    def _script_start(self, path: Path, reader: _ModuleReader, display: ast.List | ast.Tuple) -> list[_PytestStart]:
        """A repository script a command list starts with the interpreter, whose own pytest takes the suites its
        command line gives it, as tests/tools/test_python_diagnostics.py's run_tool starts
        tools/python_diagnostics.py."""
        items = display.elts
        if not items or reader.resolver.name(items[0]) != INTERPRETER:
            return []
        index = _script_index(items)
        options = self.script(path, items[index]) if index is not None else None
        if index is None or options is None:
            return []
        # The script starts its own pytest, from where it chooses, as tools/python_diagnostics.py starts it from the
        # repository's root, so the working directory the script itself is given decides nothing here.
        started, _ = self._started(reader, display)
        if not started:
            return []
        suites = _script_arguments(items[index + 1 :], options)
        return [_PytestStart(display.lineno, tuple(suites), options=options)]

    def _passed_starts(self, call: ast.Call, file: Path, function: FunctionNode) -> list[_PytestStart]:
        """The pytest runs a call starts through a function that starts pytest on a suite its caller gives it."""
        passing = self.passing(file, function)
        found = [_PytestStart(call.lineno, (), unread=f"{function.name} {why}") for why in passing.unread]
        for passed in passing.parameters:
            name, configured = passed.name, passed.configured
            if passed.kind == "spread":
                given = call.args[passed.position or 0 :]
                suites = tuple(_script_arguments(given, passed.options))
                found.append(_PytestStart(call.lineno, suites, configured=configured, options=passed.options))
                continue
            argument = call_argument(call, passed.position, name)
            if passed.kind == "each" and isinstance(argument, ast.List | ast.Tuple):
                suites = tuple(item for item in argument.elts if not isinstance(item, ast.Starred))
                unread = "" if len(suites) == len(argument.elts) else f"a starred item of {function.name}'s {name}"
                found.append(_PytestStart(call.lineno, suites, configured=configured, unread=unread))
            elif argument is None or isinstance(argument, ast.Starred) or passed.kind == "each":
                found.append(_PytestStart(call.lineno, (), unread=f"{function.name}'s {name} is not one it can read"))
            else:
                found.append(_PytestStart(call.lineno, (argument,), configured=configured))
        return found

    def passing(self, path: Path, function: FunctionNode) -> _Passing:
        """What a module-level function passes to its callers; see _Passing."""
        key = (path, function.name)
        if key in self._passing:
            return self._passing[key]
        # A function that calls itself, directly or through others, passes nothing further on that call.
        self._passing[key] = _Passing()
        positional = [argument.arg for argument in [*function.args.posonlyargs, *function.args.args]]
        parameters: list[_Passed] = []
        unread: list[str] = []
        for start in self.starts(path, function):
            for verdict in self.verdicts(path, function, start):
                if verdict.kind == "spread":
                    parameters.append(
                        _Passed(len(positional), verdict.detail, "spread", start.configured, start.options)
                    )
                elif verdict.kind in {"parameter", "each"}:
                    index = positional.index(verdict.detail) if verdict.detail in positional else None
                    kind = "one" if verdict.kind == "parameter" else "each"
                    parameters.append(_Passed(index, verdict.detail, kind, start.configured))
                elif verdict.kind:
                    reason = verdict.detail or "the suite lies under the temporary folder and its root is not pinned"
                    unread.append(f"starts pytest at line {start.line} on a suite it does not pin: {reason}")
        self._passing[key] = _Passing(tuple(dict.fromkeys(parameters)), tuple(unread))
        return self._passing[key]

    def verdicts(self, path: Path, function: FunctionNode | None, start: _PytestStart) -> list[_Verdict]:
        """A verdict on each suite a start runs."""
        if start.unread:
            return [_Verdict("unread", start.unread)]
        return [self._verdict(path, function, start, suite) for suite in start.suites]

    def _verdict(self, path: Path, function: FunctionNode | None, start: _PytestStart, suite: ast.expr) -> _Verdict:
        reader = self.reader(path)
        resolver = reader.resolver
        if isinstance(suite, ast.Starred):
            return self._spread_verdict(path, function, start, suite)
        key = _path_key(suite, resolver)
        if key is None:
            return _Verdict("unread", f"{ast.unparse(suite)} names a path bound more than once")
        base = _key_base(key)
        folder = _parent_key(key)
        if _in_repository(key, resolver, reader.depth) or self._pinned(path, function, start, folder):
            return _Verdict("")
        if key[0] == "name" and function is not None:
            given = str(key[1]) in _parameter_names(function)
            passed = str(key[1]) if given else self._looped_parameter(reader, function, str(key[1]))
            if passed and _pytest_calls(function, resolver):
                return _Verdict(
                    "unread",
                    f"{ast.unparse(suite)} comes from a parameter pytest gives the test or fixture {function.name}",
                )
            if passed and reader.module_functions.get(function.name) is function:
                return _Verdict("parameter" if given else "each", passed)
            if passed:
                return _Verdict(
                    "unread", f"{ast.unparse(suite)} comes from a parameter of a method or a nested function"
                )
        if base[0] == "node" and isinstance(base[1], ast.Call) and resolver.name(base[1].func) in TEMPORARY_FOLDERS:
            return _Verdict("temporary", "configured" if start.configured else "")
        return _Verdict("unread", f"{ast.unparse(suite)} is neither in the repository nor under a temporary folder")

    def _spread_verdict(
        self, path: Path, function: FunctionNode | None, start: _PytestStart, suite: ast.Starred
    ) -> _Verdict:
        """A verdict on the suites a starred argument spreads into a script's arguments: a module-level function's own
        starred parameter passes them to its callers; otherwise they are read as suites under the folders the function
        makes, a stated limit, pinned when each folder holds a pytest.ini written before the start and named when the
        function makes none."""
        reader = self.reader(path)
        vararg = function.args.vararg if function is not None else None
        if (
            function is not None
            and vararg is not None
            and isinstance(suite.value, ast.Name)
            and suite.value.id == vararg.arg
        ):
            if _pytest_calls(function, reader.resolver) or reader.module_functions.get(function.name) is not function:
                return _Verdict("unread", f"{ast.unparse(suite)} comes from a parameter no caller it reads gives")
            return _Verdict("spread", vararg.arg)
        made = [
            node
            for node in reader.candidates.get(id(function), [])
            if isinstance(node, ast.Call)
            and (reader.resolver.name(node.func) in TEMPORARY_FOLDERS or self._makes_ini(path, node))
        ]
        if not made:
            return _Verdict("unread", "a starred item of the script's arguments, in a function that makes no folder")
        written = {
            _path_key(folder, reader.resolver)
            for line, folder in self.ini_folders(reader, function)
            if line <= start.line
        }
        if all(self._makes_ini(path, node) or ("node", node) in written for node in made):
            return _Verdict("")
        return _Verdict("temporary")

    def _makes_ini(self, path: Path, call: ast.Call) -> bool:
        made = self.callee(path, call)
        return made is not None and self.makes_ini_folder(*made)

    @staticmethod
    def _looped_parameter(reader: _ModuleReader, function: FunctionNode, name: str) -> str:
        """The parameter a function's loop takes a name from, item by item, or ""."""
        parameters = _parameter_names(function)
        for node in reader.candidates.get(id(function), []):
            if (
                isinstance(node, ast.For)
                and isinstance(node.target, ast.Name)
                and node.target.id == name
                and isinstance(node.iter, ast.Name)
                and node.iter.id in parameters
            ):
                return node.iter.id
        return ""

    def _pinned(self, path: Path, function: FunctionNode | None, start: _PytestStart, folder: PathKey | None) -> bool:
        """Whether a start pins its suite's folder: a --confcutdir or --rootdir naming it, a pytest.ini the function
        writes there before the start, or a folder a function makes holding a pytest.ini. A start that names its
        configuration file reads no pytest.ini beside the suite, and its confcutdir defaults to that file's folder
        whatever --rootdir says, so only --confcutdir pins it."""
        reader = self.reader(path)
        if folder is None:
            return False
        if any(
            _path_key(value, reader.resolver) == folder
            for option, value in start.pins
            if option == "--confcutdir" or not start.configured
        ):
            return True
        if start.configured:
            return False
        if any(
            line <= start.line and _path_key(written, reader.resolver) == folder
            for line, written in self.ini_folders(reader, function)
        ):
            return True
        if folder[0] == "node" and isinstance(folder[1], ast.Call):
            made = self.callee(path, folder[1])
            return made is not None and self.makes_ini_folder(*made)
        return False

    @staticmethod
    def ini_folders(reader: _ModuleReader, function: FunctionNode | None) -> list[tuple[int, ast.expr]]:
        """The line and folder of each pytest.ini a function, or the module's own statements, writes."""
        return [
            (node.lineno, node.func.value.left)
            for node in reader.candidates.get(id(function), [])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in FILE_WRITES
            and isinstance(node.func.value, ast.BinOp)
            and isinstance(node.func.value.op, ast.Div)
            and _names_pytest_ini(node.func.value.right, reader.resolver)
        ]

    def makes_ini_folder(self, path: Path, function: FunctionNode) -> bool:
        """Whether a function returns one folder, into which it writes a pytest.ini."""
        reader = self.reader(path)
        returned = [
            node.value
            for node in ast.walk(function)
            if isinstance(node, ast.Return) and reader.functions.get(id(node)) is function
        ]
        keys = {_path_key(value, reader.resolver) if value is not None else None for value in returned}
        return (
            len(keys) == 1
            and None not in keys
            and any(_path_key(written, reader.resolver) in keys for _, written in self.ini_folders(reader, function))
        )


def pytest_root_problems(root: Path) -> list[str]:
    """Report each pytest a Python regression suite starts from the repository on a suite outside it whose root it does
    not pin, and each such start this policy cannot read; see pytest_root_reading."""
    return pytest_root_reading(root)[0]


def pytest_root_reading(root: Path) -> tuple[list[str], list[str]]:
    """The problems pytest_root_problems reports, and each pytest start it read, as `<suite>:<line>`, so a check can
    hold that the reader still finds the starts the repository holds.

    With no configuration beside a suite, pytest roots a run at the path the suite and its working directory share. On
    Windows the repository and the temporary folder lie on different drives, so a suite written under the temporary
    folder roots at the repository, whose confcutdir leaves out no folder on the other drive: pytest collects every
    folder above the suite, the temporary folder that every parallel suite writes in among them, and a stat of an entry
    another suite removes meanwhile stops collection with exit 2, on Windows alone and only under load. A suite pins
    the root with a pytest.ini written beside the suite it starts, before it starts it, or a --confcutdir or --rootdir
    naming the suite's folder; a start that names its configuration file with -c or --config-file reads no pytest.ini
    beside the suite, and its confcutdir defaults to that file's folder whatever --rootdir says, so only --confcutdir
    pins it.

    A start is a call to pytest_command, or a command list holding `-m pytest`, that no assertion compares before a
    process start runs it, or a call of a module-level function, the suite's own or one a repository module it imports
    by name defines, that starts pytest on a suite its caller gives it, or on each item of a list it gives. A suite
    that is a parameter pytest gives a test or a fixture, or one of a method or a nested function, is named, since no
    caller this policy reads pins it.

    A suite that runs a repository script as a program, a command list opening with the interpreter and naming the
    script, as tests/tools/test_python_diagnostics.py's run_tool runs tools/python_diagnostics.py, starts pytest on
    each argument the command gives the script that is not an option or the value of an option its parser declares,
    when the script's text, or a module's it imports by name, names a pytest start and its parser takes a positional
    argument. A function that spreads its own starred parameter into such a command, as run_tool does, passes each
    argument from that position on to its callers. A starred argument is read as suites under the folders the
    function that gives it makes: pinned when each holds a pytest.ini, failed when one is a temporary folder that does
    not, and named when the function makes none.

    Stated limits, which no repository code writes: a script started through `-m <module>` or `-c`, or whose path
    names a file only through a binding this policy does not follow; a script command extended after it is built, by
    `+ [suite]`, append, or `+ list(arguments)`; the parsed arguments, which are not followed to the function that
    starts pytest on them, so every positional argument is read as a suite, and an option the parser declares through
    anything but a literal add_argument is read as a flag; a script that takes its suites by an option, from
    sys.argv, from parse_known_args' remainder, by a positional declared with dest=, or through a parser another
    module builds; a script whose pytest start lies two imports away; a starred argument, which is judged by the
    folders its function makes, never by where its items come from; a literal absolute path given a script, which is
    skipped as a literal; a starred item after an option that takes a value, which is read as suites; a function of a
    module loaded by its path, or another callable a name does not resolve to a module-level function; a suite a
    starred item of a `-m pytest` command list spreads; a `-m pytest` list that holds no suite and is extended later,
    by unpacking, append, or +=; pytest started as a program of its own, through pytest.main, or with `-mpytest` as
    one argument; a pytest_command or `-m pytest` start whose cwd this policy does not read as a folder of the
    repository, which starts elsewhere, as the mutation gate's in its own worktree, while a script's own cwd decides
    nothing, since the script starts its pytest from where it chooses; a start inside an assertion through a runner
    PROCESS_STARTS does not name, which is read as a command the assertion compares; a suite a starred item spreads
    after pytest_command's first argument, as `pytest_command(suite, *more)`, which is read as options; a literal
    naming an absolute path or a drive joined to a repository path, read as folders below it; and `-c<file>` as one
    literal, which is not read as naming a configuration file.
    """
    problems: list[str] = []
    read: list[str] = []
    reader = _PytestRoots(root)
    for path in regression_suites(root):
        if path.suffix.casefold() != ".py" or not (reader.names_a_start(path) or reader.names_a_script(path)):
            continue
        name = path.relative_to(root).as_posix()
        tree = reader.reader(path).tree
        functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]
        for function in [None, *functions]:
            for start in reader.starts(path, function):
                read.append(f"{name}:{start.line}")
                for verdict in reader.verdicts(path, function, start):
                    if verdict.kind == "temporary" and verdict.detail == "configured":
                        problems.append(
                            f"{name}:{start.line} starts pytest from the repository on a suite under the temporary "
                            "folder, naming its configuration file, and pins no root; pass --confcutdir naming the "
                            "suite's folder, since a run that names its configuration file reads no pytest.ini "
                            "beside the suite, so pytest collects no folder above it"
                        )
                    elif verdict.kind == "temporary":
                        problems.append(
                            f"{name}:{start.line} starts pytest from the repository on a suite under the temporary "
                            f"folder and pins no root; write a {PYTEST_INI} beside the suite before starting it, or "
                            "pass --confcutdir or --rootdir naming its folder, so pytest collects no folder above it"
                        )
                    elif verdict.kind == "unread":
                        problems.append(
                            f"{name}:{start.line} starts pytest from the repository on a suite this policy cannot "
                            f"place in the repository or see pinned ({verdict.detail}); build the suite's path from a "
                            f"tempfile folder in the function that starts it, and write a {PYTEST_INI} beside it or "
                            "pass --confcutdir or --rootdir naming its folder"
                        )
    return problems + reader.modules.unreadable_problems(), read


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
        # Points a standard stream's file descriptor at another file.
        "os.dup2",
    }
)
ENVIRONMENT_CHANGES = frozenset(
    {"update", "pop", "popitem", "clear", "setdefault", "__setitem__", "__delitem__", "__ior__"}
)
# The process environment as text and as bytes, each a view of the one environment a job starts from.
ENVIRONMENTS = frozenset({"os.environ", "os.environb"})
ENVIRONMENT_METHODS = frozenset(
    f"{environment}.{method}" for environment in ENVIRONMENTS for method in ENVIRONMENT_CHANGES
)
PATCHED_TARGETS = frozenset({*STANDARD_STREAMS, *ENVIRONMENTS})
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
    """The dotted name an expression stands for through the module's imports, the names it binds to one by a simple
    assignment, such as os.environ for `environment` after `environment = os.environ`, or for an assignment
    expression `(environment := os.environ)`, and what it looks up by a literal name in any form attribute_lookups
    reads, such as os.environ for getattr(os, "environ"), vars(os)["environ"], or sys.modules["os"].environ. A part a
    lookup computes stands for COMPUTED_NAME, as getattr(os, name) stands for os.?; an attrgetter of more than one name,
    which gives a tuple, stands for none, as does any expression that is not one of these."""
    if isinstance(node, ast.NamedExpr):
        return _name(node.value, resolver)
    if isinstance(node, ast.Attribute):
        base = _name(node.value, resolver)
        return f"{base}.{node.attr}" if base else ""
    if isinstance(node, ast.Name):
        return module_lookup(node, resolver) or resolver.name(node)
    if node is None:
        return ""
    lookups = attribute_lookups(node, lambda function: [resolver.name(function)])
    if lookups:
        owner = _name(lookups[0].receiver, resolver) if len(lookups) == 1 and lookups[0].alone else ""
        return f"{owner}.{_constant(lookups[0].name) or COMPUTED_NAME}" if owner else ""
    return module_lookup(node, resolver)


def _computed(name: str) -> bool:
    return COMPUTED_NAME in name.split(".")


def _reaches(name: str, targets: frozenset[str]) -> bool:
    """Whether a dotted name is one of targets, or may be one where a lookup computes a part of it, which stands for
    any one or more parts."""
    if not _computed(name):
        return name in targets
    pattern = r"\.".join(r"[^.]+(?:\.[^.]+)*" if part == COMPUTED_NAME else re.escape(part) for part in name.split("."))
    return any(re.fullmatch(pattern, target) for target in targets)


def _through_lookup(node: ast.AST) -> str:
    return (
        "may change a standard stream, os.environ, or the working directory through "
        f"`{ast.unparse(node)}`, a lookup by a computed name"
    )


def _stored(node: ast.expr) -> list[ast.expr]:
    """Each name, attribute, or subscript an assignment or deletion target stores to, through any unpacking."""
    if isinstance(node, (ast.Tuple, ast.List)):
        return [part for item in node.elts for part in _stored(item)]
    return _stored(node.value) if isinstance(node, ast.Starred) else [node]


def _changed_target(node: ast.expr, resolver: CallableResolver, augmented: bool) -> str:
    """The standard stream or environment an assignment or deletion target changes, itself or by an entry, or ""
    when it changes none.

    A name an assignment or deletion binds changes only the name, never what it stood for, so a bare name counts only
    for an augmented assignment, which changes os.environ in place through `environ |= ...` and leaves the name bound
    to it, as the resolver given in_place_updates reads it.
    """
    if isinstance(node, ast.Subscript):
        name = _name(node.value, resolver)
        if _reaches(name, ENVIRONMENTS):
            return name
        # A store into a namespace replaces the attribute it names, as vars(os)["environ"] = {} replaces os.environ.
        name = _name(node, resolver)
        return name if _reaches(name, PATCHED_TARGETS) else ""
    if isinstance(node, ast.Name) and not augmented:
        return ""
    name = _name(node, resolver)
    return name if _reaches(name, PATCHED_TARGETS) else ""


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
    target is a string or an object; a patch.object or setattr target is an object, and its attribute given by
    anything but a literal is COMPUTED_NAME."""
    if called in PATCH_CALLS:
        return [_constant(call_argument(call, 0, "target")) or ""]
    if called in PATCH_DICT_CALLS:
        dictionary = call_argument(call, 0, "in_dict")
        return [_constant(dictionary) or _name(dictionary, resolver)]
    if called in PATCH_OBJECT_CALLS:
        owner = _name(call_argument(call, 0, "target"), resolver)
        given = call_argument(call, 1, "attribute")
        attribute = "" if given is None else _constant(given) or COMPUTED_NAME
        return [f"{owner}.{attribute}"] if owner and attribute else []
    if called in PATCH_MULTIPLE_CALLS:
        target = call_argument(call, 0, "target")
        base = _constant(target) or _name(target, resolver)
        return [f"{base}.{attribute}" for attribute in _replaced_attributes(call)] if base else []
    return []


def _shares_state(node: ast.AST, resolver: CallableResolver) -> str:
    """What a node changes of the state the pool reads, or an empty string."""
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
        targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
        augmented = isinstance(node, ast.AugAssign)
        changed = [
            (stored, name)
            for target in targets
            for stored in _stored(target)
            if (name := _changed_target(stored, resolver, augmented))
        ]
        if not changed:
            return ""
        stored, name = changed[0]
        return _through_lookup(stored) if _computed(name) else f"changes {ast.unparse(stored)}"
    if not isinstance(node, ast.Call):
        return ""
    called = _name(node.func, resolver)
    if _reaches(called, SHARED_STATE_CALLS | ENVIRONMENT_METHODS):
        return _through_lookup(node.func) if _computed(called) else f"calls {called}"
    patched = [name for name in _patched(node, called, resolver) if _reaches(name, PATCHED_TARGETS)]
    if not patched:
        return ""
    return _through_lookup(node) if _computed(patched[0]) else f"patches {patched[0]}"


def shared_state_lines(tree: ast.Module) -> list[tuple[int, str]]:
    """Each line on which a module swaps a standard stream or changes os.environ or the working directory, and how,
    resolved through its imports, the names it binds to one by a simple assignment, as CallableResolver reads them,
    and what it looks up by name. A lookup by a computed name that may reach one is reported, since the policy
    cannot settle what it changes."""
    resolver = CallableResolver(tree, in_place_updates=True)
    found = {
        (node.lineno, change)
        for node in ast.walk(tree)
        if isinstance(node, (ast.stmt, ast.expr)) and (change := _shares_state(node, resolver))
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
        # A path whose stat fails raises, and one of the wrong kind is named so, never reported as one that is not
        # there.
        try:
            present = file_presence.is_present(path)
        except file_presence.NotAFile:
            problems.append(f"{path.relative_to(root).as_posix()}: a policy module {RUNNER} runs is not a file")
            continue
        if not present:
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
            "skills/update-coding-agent-skills/scripts/test_update_fixture.sh",
            "skills/update-coding-agent-skills/scripts/test_update_main.sh",
            "skills/update-coding-agent-skills/scripts/test_update_majors.sh",
            "skills/update-coding-agent-skills/scripts/test_update_pins.sh",
            "skills/update-coding-agent-skills/scripts/test_update_refs.sh",
            "skills/update-coding-agent-skills/scripts/test_update_release.sh",
            "skills/update-coding-agent-skills/scripts/test_update_installed.sh",
            "skills/ejryan89-code-review-core/scripts/test_review_pipeline.py",
        ):
            self.assertIn(expected, suites)
        self.assertNotIn("tests/deployer/harness.py", suites)
        self.assertNotIn("conftest.py", suites)

    def test_no_bash_suite_pipes_into_a_reader_that_ends_early(self) -> None:
        self.assertEqual([], pipe_race_problems(REPOSITORY_ROOT))

    def test_python_suites_define_tests_and_run_only_through_pytest(self) -> None:
        self.assertTrue([path for path in regression_suites() if path.suffix.casefold() == ".py"])
        self.assertEqual([], suite_shape_problems(REPOSITORY_ROOT))

    def test_no_test_module_makes_a_test_case_to_check_through(self) -> None:
        self.assertEqual([], assertion_holder_problems(REPOSITORY_ROOT))

    def test_the_pytest_configuration_selects_and_skips_nothing(self) -> None:
        self.assertEqual([], pytest_configuration_problems(REPOSITORY_ROOT))

    def test_a_pytest_a_suite_starts_on_a_suite_outside_the_repository_pins_its_root(self) -> None:
        problems, read = pytest_root_reading(REPOSITORY_ROOT)
        self.assertEqual([], problems)
        # The places that start pytest on a suite outside the repository, which a reader that stopped finding starts
        # would pass as having none.
        known = {
            "tests/test_conftest.py:120",
            "tests/validation/test_suite_discovery.py:69",
            "tests/validation/test_job_pool.py:551",
            "tests/tools/test_python_diagnostics.py:124",
            "tests/tools/test_python_diagnostics.py:175",
            "tests/tools/test_python_diagnostics.py:200",
            "tests/tools/test_python_diagnostics.py:216",
            "tests/tools/test_python_diagnostics.py:241",
        }
        # Each start by its line, so a start rewritten into a form the reader does not read fails here; a start that
        # only moves to another line is named here by its new one.
        self.assertEqual(known, known & set(read), read)

    def test_suite_discovery_rules_are_documented(self) -> None:
        self.assertEqual([], suite_discovery_documentation_problems(REPOSITORY_ROOT))

    def test_recorded_test_costs_name_tests_that_exist(self) -> None:
        self.assertEqual([], recorded_cost_problems(REPOSITORY_ROOT))

    def test_each_recorded_test_runs_on_a_shard_of_its_own(self) -> None:
        self.assertEqual([], recorded_shard_problems(REPOSITORY_ROOT))

    def test_every_module_is_named_by_a_test(self) -> None:
        self.assertEqual([], untested_module_problems(REPOSITORY_ROOT))

    def test_the_root_conftest_isolates_git_for_every_python_suite(self) -> None:
        self.assertEqual([], git_isolation_problems(REPOSITORY_ROOT))

    def test_no_policy_check_changes_what_the_pool_reads(self) -> None:
        self.assertEqual([], pool_shared_state_problems(REPOSITORY_ROOT))

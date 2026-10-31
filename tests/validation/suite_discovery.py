"""Regression suites: how they are found, sharded, and run, the rules the skill guide states for them, that every
module is named by a test, and that the root conftest.py isolates git for every Python suite.

Each Python suite runs as `python -B -m pytest <suite>`, or as one shard of it with conftest.py's --suite-shard, in
a process of its own; a Bash or PowerShell suite runs as a program.
"""

from __future__ import annotations

import ast
import functools
import io
import itertools
import re
import shlex
import sys
import tomllib
import unittest
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import TypeGuard

from dealing import deal
from job_pool import UNSPLIT_SUITE_WEIGHT, Job, run_process
from module_closure import MODULE_LOADERS
from path_containment import posix_within, within
from shell_targets import run_bash_suite
from skill_scripts import (
    ASSIGNMENT,
    COMMAND_WRAPPERS,
    SCRIPT_SHELLS,
    STANDARD_STREAMS,
    split_operators,
    substitution_spans,
    unwrapped,
)
from toolchain import find_powershell
from validation_support import (
    ATTRIBUTE_LOOKUPS,
    COMPUTED_NAME,
    REPOSITORY_ROOT,
    SKILL_GUIDE,
    SUITE_EXTENSIONS,
    SUITE_NAME_PATTERNS,
    CallableResolver,
    UnreadablePython,
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

from deployer.platform_support import program_name

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
    """How the runner starts a Python suite: pytest on the one file, from the repository root, with no bytecode."""
    return [sys.executable, "-B", "-m", "pytest", target, *options]


def run_test_script(path: Path) -> None:
    target = relative(path)
    suffix = path.suffix.casefold()
    if suffix == ".py":
        run_process(pytest_command(target))
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
        if top.is_dir()
        for path in top.rglob("*")
        if path.is_file() and is_test_script(path) and not within(path, root / FIXTURE_ROOT)
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
    run_process(pytest_command(relative(suite), *shard_options(index, count, recorded)))


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
        if not (path.is_file() and is_test_script(path) and path.suffix == ".py"):
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
        if not (path.is_file() and path.suffix == ".py"):
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


# A pipe hands the next command what the one before it writes.
PIPES = frozenset({"|", "|&"})
# What ends a simple command in a Bash suite. A redirection's < and > stay in the command they belong to.
COMMAND_BREAKS = frozenset({";", ";;", "&", "&&", "||", "|", "|&", "(", ")", "{", "}", "\n"})
# Bash's compound commands, each by the keyword that opens it and the one that closes it; every command of one a pipe
# feeds reads that pipe, as a group's do. The reserved words that may stand before a command's program in one, and a
# header whose words are no command: `for name in words`, `select name in words`, `case word in`.
COMPOUND_CLOSERS = {"if": "fi", "while": "done", "until": "done", "for": "done", "select": "done", "case": "esac"}
COMMAND_LEADERS = frozenset({"if", "then", "elif", "else", "while", "until", "do", "!"})
COMPOUND_HEADERS = frozenset({"for", "select", "case"})
# What ends a case item's commands, so a pattern follows.
CASE_ITEM_ENDS = frozenset({";;", ";&", ";;&"})
GREP_PROGRAMS = frozenset({"grep", "egrep", "fgrep"})
RIPGREP = "rg"
# Bash's $'...' string, in which a backslash escapes a single quote.
ANSI_C_STRING = re.compile(r"\$'(?:[^'\\]|\\.)*'", re.DOTALL)
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
# The commands besides COMMAND_WRAPPERS that run the program a later word names, with their options that take the next
# word as their value: sudo and doas also take assignments before it.
READER_WRAPPERS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "--user", "-g", "--group", "-C", "-D", "-h", "-p", "-r", "-t", "-T", "-U", "-R"}),
    "doas": frozenset({"-u", "-C"}),
    "setsid": frozenset(),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata"}),
}
# env's option whose value is a command line, split into the words it runs.
ENV_SPLIT_OPTIONS = ("-S", "--split-string")
# A shell's options that take the next word: -o and +o a set option's name, -O and +O a shopt option's, and its long
# options that name a file.
SHELL_ARGUMENT_LETTERS = frozenset("oO")
SHELL_ARGUMENT_LONG_OPTIONS = frozenset({"--rcfile", "--init-file"})
# The option set and shopt -o name, and the variable whose value lists it for a shell that starts with it.
PIPEFAIL = "pipefail"
OPTION_SETTERS = frozenset({"set", "shopt"})
SHELL_OPTIONS_VARIABLE = re.compile(r"\bSHELLOPTS=[^\s;&|]*pipefail")
# How deep the reader follows a command into the functions and shell scripts it runs.
NESTING_LIMIT = 16


def _program(word: str) -> str:
    """The program a command word names, by its file name: `/usr/bin/head` and `head.exe` are head."""
    return program_name(word.rpartition("/")[2])


def _long_option(word: str, options: frozenset[str]) -> str | None:
    """The one of `options` a long option word names, in full or by a prefix, as getopt_long takes a unique one."""
    option = word.partition("=")[0]
    return next((full for full in sorted(options) if len(option) > 2 and full.startswith(option)), None)


def _env_split(words: list[str]) -> list[str]:
    """An env command's words with its -S or --split-string value split into the words it stands for, as env splits
    it, read among env's own options and assignments up to the program; a value shlex cannot split stays one word."""
    options = COMMAND_WRAPPERS["env"] - set(ENV_SPLIT_OPTIONS)
    index = 1
    while index < len(words) and (words[index].startswith("-") or ASSIGNMENT.match(words[index])):
        word = words[index]
        option = next((option for option in ENV_SPLIT_OPTIONS if word.partition("=")[0] == option), None)
        option = option or ("-S" if word.startswith("-S") else None)
        if option is None:
            index += 2 if word in options else 1
            continue
        value = word[len(option) :].removeprefix("=")
        rest = index + 1
        if not value and rest < len(words):
            value, rest = words[rest], rest + 1
        try:
            split = shlex.split(value)
        except ValueError:
            split = [value]
        return [*words[:index], *split, *words[rest:]]
    return words


def _command_words(words: list[str]) -> list[str]:
    """A command's words from the program it runs: past the reserved words that may stand before it, assignments,
    and each wrapper, whether named by a path or not, such as `env`, `exec`, `timeout`, `command`, `sudo`, or
    `/usr/bin/env`; env's -S value is split into the words it runs. xargs runs its command on no pipe, so it stays."""
    while words:
        program = _program(words[0])
        if words[0] in COMMAND_LEADERS or ASSIGNMENT.match(words[0]):
            words = words[1:]
        elif program in READER_WRAPPERS:
            options, index = READER_WRAPPERS[program], 1
            while index < len(words) and (words[index].startswith("-") or ASSIGNMENT.match(words[index])):
                index += 2 if words[index] in options else 1
            words = words[index:]
        elif program != "xargs" and program in COMMAND_WRAPPERS:
            words = unwrapped(_env_split([program, *words[1:]]) if program == "env" else [program, *words[1:]])
        else:
            break
    return words


def _shell_script(words: list[str]) -> str | None:
    """The script a command runs in a shell of its own or in this one: a shell's -c script, among its other options,
    and eval's arguments, joined as eval joins them; None for any other command."""
    program = _program(words[0]) if words else ""
    if program == "eval":
        return " ".join(words[1:])
    if program not in SCRIPT_SHELLS:
        return None
    runs_script, argument_next, options_over = False, False, False
    for word in words[1:]:
        if argument_next:
            argument_next = False
        elif options_over or not word.startswith(("-", "+")) or word in {"-", "+"}:
            return word if runs_script else None
        elif word == "--":
            options_over = True
        elif word.startswith("--"):
            argument_next = word in SHELL_ARGUMENT_LONG_OPTIONS
        else:
            runs_script = runs_script or "c" in word[1:]
            argument_next = any(letter in SHELL_ARGUMENT_LETTERS for letter in word[1:])
    return None


def _early_exit_program(words: list[str]) -> str:
    """A command whose program may exit before what writes into it has written everything, as its program and the
    option that makes it, such as `grep -q` or `head -n`, or an empty string for a command that reads to the end."""
    program = _program(words[0]) if words else ""
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


def _early_exit_reader(
    words: list[str], functions: Mapping[str, list[list[str]]] | None = None, expanding: frozenset[str] = frozenset()
) -> str:
    """The reader a command a pipe feeds runs that may exit before what writes into it has written everything, as its
    program and the option that makes it, such as `grep -q` or `head -n`, or an empty string for a command that reads
    to the end. It reads past each wrapper `_command_words` reads, into the body of a function the script defines,
    and into the script a shell's -c or eval runs, since each command there reads the same pipe."""
    if not words or words[0] in COMPOUND_HEADERS or len(expanding) > NESTING_LIMIT:
        return ""
    words = _command_words(words)
    if not words:
        return ""
    if reader := _early_exit_program(words):
        return reader
    body = (functions or {}).get(words[0]) if words[0] not in expanding else None
    script = _shell_script(words)
    if body is None and script is not None:
        try:
            commands, inner = _read_bash(script)
        except ValueError:
            return ""
        body, functions = [command for _, _, command in commands], {**(functions or {}), **inner}
    inside = expanding | {words[0]}
    for command in body or []:
        if reader := _early_exit_reader(command, functions, inside):
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


class _ShellText(io.StringIO):
    """A script shlex reads, whose comments leave their newline to be read, so a comment still ends its command."""

    # typeshed gives StringIO both _TextIOBase.readline, returning str, and _IOBase.readline, returning bytes, and no
    # override can match both.
    def readline(self, size: int = -1, /) -> str:  # type: ignore[override]  # StringIO's bases disagree on the return
        start = self.tell()
        line = super().readline(size)
        if line.endswith("\n"):
            self.seek(start + len(line) - 1)
            return line[:-1]
        return line


class _Frame:
    """A group or compound command open where a Bash script is read: the word that closes it, whether a pipe feeds
    it, the function whose body it is, if any, and, for a case, whether its patterns are being read."""

    def __init__(self, closer: str, fed: bool, function: str | None = None) -> None:
        self.closer, self.fed, self.function = closer, fed, function
        self.patterns = False
        self.case_word = closer == "esac"


def _function_header(words: list[str]) -> str | None:
    """The function a command's words define, as `name ()`, `function name`, or `function name ()`, or None."""
    if len(words) == 2 and words[1] == "()" and words[0] != "function":
        return words[0]
    if words[:1] == ["function"] and len(words) in (2, 3) and words[2:] in ([], ["()"]):
        return words[1]
    return None


class _BashReader:
    """The commands and functions of a Bash script, read one shell part at a time, as _read_bash describes them."""

    def __init__(self, script: str, stream: io.StringIO) -> None:
        self.script, self.stream = script, stream
        self.substitutions = [(start, start + len(text)) for start, text in substitution_spans(script)]
        self.commands: list[tuple[str, int, list[str]]] = []
        self.functions: dict[str, list[list[str]]] = {}
        self.before, self.line, self.offset = "\n", 0, 0
        self.words: list[str] = []
        # Each group and compound command open here, innermost last. A parenthesis after a word, as in an unquoted
        # `$(`, opens one too, fed when the one around it is, so each closing one has its own to close.
        self.frames: list[_Frame] = []

    def fed(self) -> bool:
        return bool(self.frames) and self.frames[-1].fed

    def emit(self) -> None:
        """Record the command read so far, unless it stands inside a substitution, whose own reading records it."""
        if self.words and not any(start <= self.offset <= end for start, end in self.substitutions):
            self.commands.append(("|" if self.fed() else self.before, self.line, self.words))
            function = next((frame.function for frame in reversed(self.frames) if frame.function), None)
            if function is not None:
                self.functions[function].append(self.words)

    def close(self, closer: str) -> None:
        """End the innermost frame the word closes, and every frame left open inside it."""
        index = next((i for i in range(len(self.frames) - 1, -1, -1) if self.frames[i].closer == closer), None)
        if index is not None:
            del self.frames[index:]

    def case_part(self, part: str) -> bool:
        """Read a part of a case's word or patterns, which are no commands, and where `|` separates patterns; False
        when no case is reading them."""
        case = self.frames[-1] if self.frames and self.frames[-1].closer == "esac" else None
        if case is None or not (case.patterns or case.case_word):
            return False
        if case.case_word and part == "in":
            case.case_word, case.patterns, self.words = False, True, []
        elif case.patterns and part == "esac":
            self.close("esac")
        elif case.patterns and part == ")":
            case.patterns, self.before = False, "\n"
        return True

    def word(self, part: str, at_command: bool) -> None:
        """Read a word: the first of a command sets its line, and a reserved word opens or closes a compound."""
        if not self.words:
            self.offset = max(self.stream.tell() - 1, 0)
            self.line = self.script.count("\n", 0, self.offset) + 1
        if at_command and part in COMPOUND_CLOSERS:
            self.frames.append(_Frame(COMPOUND_CLOSERS[part], self.before in PIPES or self.fed()))
        elif not self.words and part in COMPOUND_CLOSERS.values():
            self.close(part)
        self.words.append(part)

    def ends_case_item(self, part: str) -> bool:
        """Read `;;`, `;&`, or `;;&` in a case's commands, after which its patterns follow; False for any other part."""
        case = self.frames[-1] if self.frames and self.frames[-1].closer == "esac" else None
        if case is None or not (
            part in CASE_ITEM_ENDS or (part == "&" and not self.words and self.before in {";", ";;"})
        ):
            return False
        self.emit()
        self.before, self.words, case.patterns = part, [], True
        return True

    def operator(self, part: str, at_command: bool) -> None:
        """Read an operator: it ends the command before it, and opens or closes a group, or a function's body."""
        function = _function_header(self.words)
        opens = part == "(" or (part == "{" and (at_command or function is not None))
        closes = part == ")" or (part == "}" and not self.words)
        feeds = (at_command and self.before in PIPES) or self.fed()
        if function is not None and opens:
            self.functions[function], self.words = [], []
        if self.words:
            self.emit()
            self.before, self.words = part, []
        elif part != "\n":
            self.before = part
        if opens:
            self.frames.append(_Frame(")" if part == "(" else "}", feeds and function is None, function))
        elif closes:
            self.close(part)

    def part(self, part: str) -> None:
        if self.case_part(part):
            return
        at_command = all(word in COMMAND_LEADERS for word in self.words)
        if part not in COMMAND_BREAKS or (part == "&" and self.words[-1:] in (["<"], [">"])):
            self.word(part, at_command)
        elif not self.ends_case_item(part):
            self.operator(part, at_command)


def _read_bash(script: str) -> tuple[list[tuple[str, int, list[str]]], dict[str, list[list[str]]]]:
    """Each simple command a Bash script runs, outside quotes and comments, as (the break before it, its line, its
    words), and each function it defines with a brace or subshell body, by name, with its body's commands. A pipe or a
    list operator at the end of a line carries over to the command on the next. Every command of a group or a compound
    command (`if`, `while`, `until`, `for`, `select`, `case`) a pipe feeds, and of one inside it, reads that pipe, so
    its break is given as `|`; a case's patterns are read as patterns, never as commands or pipes. A command whose
    first word stands inside a command substitution is left to the reading of that substitution. Raises ValueError
    for a script shlex cannot read."""
    # A line continuation becomes two spaces, and an ANSI-C string, whose \' shlex would read as its end, a word and a
    # plain single-quoted string of the same length and lines, so each offset still names its line as written.
    plain = ANSI_C_STRING.sub(lambda match: "x'" + re.sub(r"[^\n]", "x", match[0][2:-1]) + "'", script)
    stream = _ShellText(plain.replace("\\\n", "  "))
    lexer = shlex.shlex(stream, posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    reader = _BashReader(script, stream)
    for token in lexer:
        for part in split_operators(token):
            reader.part(part)
    reader.emit()
    return reader.commands, reader.functions


def _bash_commands(script: str) -> list[tuple[str, int, list[str]]]:
    """Each simple command a Bash script runs, as _read_bash reads them. Raises ValueError for a script shlex cannot
    read."""
    return _read_bash(script)[0]


def _substituted_commands(script: str) -> list[tuple[str, int, list[str]]]:
    """The commands each command substitution in a script runs, `$(...)` or backticks, quoted or not, as
    skill_scripts.substitution_spans finds them, at their own lines, and the commands of the substitutions they hold
    in turn; one shlex cannot read runs nothing this reader can see."""
    found: list[tuple[str, int, list[str]]] = []
    pending = [(script.count("\n", 0, offset), text) for offset, text in substitution_spans(script)]
    while pending:
        lines_before, text = pending.pop()
        try:
            inner = _bash_commands(text)
        except ValueError:
            inner = []
        found += [(before, lines_before + line, words) for before, line, words in inner]
        pending += [(lines_before + text.count("\n", 0, offset), nested) for offset, nested in substitution_spans(text)]
    return found


def _shell_scripts_run(commands: list[tuple[str, int, list[str]]], depth: int = 0) -> list[tuple[str, int, list[str]]]:
    """The commands each shell -c script and each eval among these commands runs, at the line of the command that runs
    it, and those of the scripts they run in turn; one shlex cannot read runs nothing this reader can see."""
    found: list[tuple[str, int, list[str]]] = []
    for _, line, words in commands:
        script = _shell_script(_command_words(words))
        if script is None or depth > NESTING_LIMIT:
            continue
        try:
            inner = _bash_commands(script)
        except ValueError:
            continue
        inner = [(before, line, inner_words) for before, _, inner_words in inner]
        found += inner + _shell_scripts_run(inner, depth + 1)
    return found


def _sets_pipefail(script: str, commands: list[tuple[str, int, list[str]]]) -> bool:
    """Whether a script turns pipefail on: by set or shopt naming it, as `set -o pipefail`, `set -euo pipefail`,
    `shopt -so pipefail`, or `builtin set -o pipefail` write it, in the script or in a script it runs through a shell or
    eval; by a shell started with `-o pipefail`; or by SHELLOPTS naming it, which a shell started under it takes."""
    if SHELL_OPTIONS_VARIABLE.search(script):
        return True
    for _, _, words in commands:
        run = _command_words(words)
        program = _program(run[0]) if run else ""
        if PIPEFAIL in run and (program in OPTION_SETTERS or program in SCRIPT_SHELLS):
            return True
    return False


def pipe_race_problems(root: Path) -> list[str]:
    """Report each pipe into a reader that may end early, in a Bash regression suite that sets pipefail.

    grep -q, -m, -l, or -L, ripgrep's -q, -m, or -l, and head can exit before the command writing into them has
    written everything; the writer then dies of SIGPIPE, and pipefail fails the pipeline though the check held, so it
    fails at random. The reader is found in any shape around it: right after the pipe; in a brace or subshell group or
    a compound command (`if`, `while`, `until`, `for`, `select`, `case`) the pipe feeds; in the body of a function
    the pipe runs; in the script a shell's -c or eval runs; in a `$(...)` or backtick substitution, quoted or not;
    named by a path; or behind an assignment, a reserved word, or a wrapper such as `env` (its -S line included),
    `exec`, `timeout`, `command`, `builtin`, or `sudo`, each named by a path or not; head is judged by the last count
    it is given. A suite turns pipefail on by set or shopt, in any spelling Bash takes, in the suite or in a script it
    runs through a shell or eval, by a shell started with `-o pipefail`, or by SHELLOPTS naming it. Each race is
    reported once.

    What it does not see: another reader that ends early, such as `read`, `sed q`, `awk` with `exit`, a loop that
    breaks, or a command that reads nothing; a reader in a process substitution such as `>(head)`, in a script a file
    holds, as `bash script.sh` or `source` runs it, in a function whose body is a compound command, or behind a
    wrapper not named here, such as `coproc`; `head --help` or `--version`; and pipefail turned off again. A command
    in a fed group whose input is redirected from a file is refused all the same.
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
        commands += _substituted_commands(text)
        commands += _shell_scripts_run(commands)
        if not _sets_pipefail(text, commands):
            continue
        races = {
            (line, reader)
            for before, line, words in commands
            if before in PIPES and (reader := _early_exit_reader(words, functions))
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
    found = {root / CONFTEST} if (root / CONFTEST).is_file() else set()
    tests = root / "tests"
    if tests.is_dir():
        found |= {path for path in tests.rglob("*.py") if path.is_file() and not within(path, root / FIXTURE_ROOT)}
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
        files = sorted(path for path in scripts.rglob("*") if path.is_file())
        if any(is_executable_script(path) and not is_test_script(path) for path in files) and not any(
            is_test_script(path) for path in files
        ):
            problems.append(
                f"{scripts.relative_to(root).as_posix()} holds scripts but no regression suite; add a "
                f"{listed(SUITE_NAME_PATTERNS)} Python, Bash, or PowerShell script beside them"
            )
    return problems


MYPY_PATH_PREFIX = "$MYPY_CONFIG_FILE_DIR/"
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
        configuration = tomllib.loads(pyproject.read_text(encoding="utf-8")) if pyproject.is_file() else {}
        entries = configuration.get("tool", {}).get("mypy", {}).get("mypy_path", [])
        self.path_directories = [root / entry.removeprefix(MYPY_PATH_PREFIX) for entry in entries]
        self._sources: dict[Path, str] = {}
        self._trees: dict[Path, ast.Module] = {}
        self._imports: dict[tuple[Path, bool], list[Path]] = {}
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
                and ast.unparse(node.func).rsplit(".", 1)[-1] in MODULE_LOADERS
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
# pytest.ini; pyproject.toml, tox.ini, and setup.cfg match only with a pytest table. The root holds pyproject.toml
# alone, so the configuration stays in one file, and no folder below it holds any of them, since the runner starts
# `python -B -m pytest <suite>` with no -c or --rootdir and pytest would read a nearer one instead.
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
        if top.is_dir()
        for path in top.rglob("*.py")
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
    trees = list(python_trees([conftest] if conftest.is_file() else [], root, problems))
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


def pytest_configuration_problems(root: Path) -> list[str]:
    """Report a pytest configuration other than PYTEST_CONFIGURATION, another configuration file at the root, or one
    in a suite's folder or a folder between it and the root, which pytest would read instead.

    A configuration could select, deselect, or skip tests, or turn the conftest plugin off, for every suite at once,
    so it is pinned here rather than read.
    """
    problems = [
        f"{name} is a pytest configuration file beside pyproject.toml; keep the configuration in "
        "[tool.pytest.ini_options] alone"
        for name in PYTEST_CONFIGURATION_FILES
        if name != "pyproject.toml" and (root / name).is_file()
    ]
    below: dict[Path, Path] = {}
    for suite in regression_suites(root):
        for folder in suite.parents:
            if folder == root:
                break
            below.update({folder / name: suite for name in PYTEST_CONFIGURATION_FILES if (folder / name).is_file()})
    problems += [
        f"{path.relative_to(root).as_posix()} is a pytest configuration file pytest reads for "
        f"{suite.relative_to(root).as_posix()} instead of the root's pyproject.toml; keep the configuration there"
        for path, suite in sorted(below.items())
    ]
    configuration = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    table = configuration.get("tool", {}).get("pytest", {})
    if table != {"ini_options": PYTEST_CONFIGURATION}:
        problems.append(f"pyproject.toml: [tool.pytest] must be ini_options = {PYTEST_CONFIGURATION!r}, not {table!r}")
    return problems


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
    expression `(environment := os.environ)`, and what it looks up by a literal name, such as os.environ for
    getattr(os, "environ") or for sys.modules["os"].environ. A part a lookup computes stands for COMPUTED_NAME, as
    getattr(os, name) stands for os.?; "" when it stands for none."""
    if isinstance(node, ast.NamedExpr):
        return _name(node.value, resolver)
    if isinstance(node, ast.Attribute):
        base = _name(node.value, resolver)
        return f"{base}.{node.attr}" if base else ""
    if isinstance(node, ast.Name):
        return module_lookup(node, resolver) or resolver.name(node)
    if isinstance(node, ast.Call) and resolver.name(node.func) in ATTRIBUTE_LOOKUPS and len(node.args) > 1:
        owner = _name(node.args[0], resolver)
        return f"{owner}.{_constant(node.args[1]) or COMPUTED_NAME}" if owner else ""
    return module_lookup(node, resolver) if node is not None else ""


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
        return name if _reaches(name, ENVIRONMENTS) else ""
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

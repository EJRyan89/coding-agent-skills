"""Every entry point's every command is driven under the environment faults it can meet, or named as not run, and
the harness that runs them does what it promises.

tests/validation/exit_tables.py finds the entry points by reading the code: a module whose `__main__` block calls
run_main, so a new one is held here from the pull request that adds it. faults.py drives each of its commands, its
argparse subcommands read from the code, or names it in FAULTS_NOT_RUN with the reason the harness cannot drive it.
fault_table.py names the faults: a file the command reads made unreadable, put in a directory's place, removed, or
given a stat that raises, and git exiting 128, git timing out, and gh failing; a member of a record of another type is
the content corpus's, test_malformed_input.py's field cases. The runs themselves are the suites test_faults_<group>.py,
one per group of faults, each judged by fault_check.py; the tests below show the harness stopping and judging a run.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "validation"))

import exit_tables
import faults
from fault_check import REPOSITORY, TRACEBACK, changes, environment, judged, placed, run_entry
from fault_table import (
    DIRECTORY,
    FAULTS,
    FILE_FAULTS,
    GH_FAILURE,
    GH_FAULTS,
    GIT_EXIT,
    GIT_FAULTS,
    GIT_TIMEOUT,
    MISSING,
    STAT_RAISES,
    UNREADABLE,
    Driver,
    Fixture,
)

ENTRIES = {entry.name: entry for entry in exit_tables.entry_points(REPOSITORY)}


def commands(entry: exit_tables.EntryPoint) -> set[str]:
    """An entry point's argparse subcommands, read from its code, or, for one that parses no command line itself, from
    each module where the code its main reaches parses one, as deploy.py's deployer does: each name given to
    add_parser, as a literal, as the variable of the loop over literals that holds the call, or as the argument a
    module's own wrapper of add_parser is given; and "" for one that has none, or whose subcommand is optional."""
    found: set[str] = set()
    optional = False
    reader = exit_tables.read_codes(REPOSITORY, entry)
    for path in {entry.path} if entry.path in reader.parsing else {entry.path, *reader.parsing}:
        tree = reader.modules[path].tree
        for loop in ast.walk(tree):
            if isinstance(loop, ast.For) and isinstance(loop.iter, ast.Tuple | ast.List):
                used = set(parser_arguments(loop))
                for position, target in loop_names(loop.target):
                    if target in used:
                        found.update(loop_values(loop.iter, position))
        found.update(argument for argument in parser_arguments(tree) if isinstance(argument, Literal))
        found.update(wrapped_parsers(tree))
        optional = optional or any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "add_subparsers"
            and not any(
                keyword.arg == "required" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
                for keyword in call.keywords
            )
            for call in ast.walk(tree)
        )
    return found | {""} if optional or not found else found


def wrapped_parsers(tree: ast.Module) -> set[str]:
    """The subcommands a module names through a function of its own that passes one of its parameters to add_parser:
    each literal, or module constant bound to one, that a call of it gives in that parameter's place."""
    constants = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    wrappers: dict[str, int] = {}
    for function in tree.body:
        if isinstance(function, ast.FunctionDef):
            names = [argument.arg for argument in function.args.args]
            for each in parser_arguments(function):
                if each in names:
                    wrappers[function.name] = names.index(each)
    found: set[str] = set()
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in wrappers:
            position = wrappers[call.func.id]
            given = call.args[position] if position < len(call.args) else None
            if isinstance(given, ast.Constant) and isinstance(given.value, str):
                found.add(given.value)
            elif isinstance(given, ast.Name) and given.id in constants:
                found.add(constants[given.id])
    return found


def loop_names(target: ast.expr) -> list[tuple[int | None, str]]:
    """Each name a loop binds, with its place in the tuple the loop unpacks, or None for a loop over single values."""
    if isinstance(target, ast.Name):
        return [(None, target.id)]
    if isinstance(target, ast.Tuple):
        return [(index, each.id) for index, each in enumerate(target.elts) if isinstance(each, ast.Name)]
    return []


def loop_values(values: ast.Tuple | ast.List, position: int | None) -> list[str]:
    """The literal strings a loop over values binds to the name at position: each value, or each tuple's item."""
    items = [
        value
        if position is None
        else (value.elts[position] if isinstance(value, ast.Tuple) and position < len(value.elts) else None)
        for value in values.elts
    ]
    return [item.value for item in items if isinstance(item, ast.Constant) and isinstance(item.value, str)]


class Literal(str):
    """A subcommand named by a literal, told apart from a loop variable's name."""


def parser_arguments(node: ast.AST) -> list[str]:
    """What each add_parser call below node names: a literal as a Literal, a variable by its name."""
    found: list[str] = []
    for call in ast.walk(node):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "add_parser":
            first = call.args[0] if call.args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append(Literal(first.value))
            elif isinstance(first, ast.Name):
                found.append(first.id)
    return found


def test_every_command_of_every_entry_point_is_driven_or_named_not_run() -> None:
    driven = {(driver.entry, driver.command) for driver in faults.DRIVERS}
    missing = [
        f"{name} {command}".strip()
        for name, entry in ENTRIES.items()
        if name not in faults.FAULTS_NOT_RUN
        for command in sorted(commands(entry))
        if (name, command) not in driven and f"{name} {command}".strip() not in faults.FAULTS_NOT_RUN
    ]
    assert missing == [], "add a driver for each to its area's faults_*.py, or name it in FAULTS_NOT_RUN with why"


def test_each_name_not_run_is_a_command_no_driver_drives() -> None:
    known = {f"{name} {command}".strip() for name, entry in ENTRIES.items() for command in commands(entry)}
    driven = {driver.name for driver in faults.DRIVERS}
    stale = [name for name in faults.FAULTS_NOT_RUN if name not in known | set(ENTRIES) or name in driven]
    assert stale == [], "remove each from FAULTS_NOT_RUN: it names no entry point or command, or a driver drives it"
    unknown = [driver.name for driver in faults.DRIVERS if driver.entry not in ENTRIES or driver.name not in known]
    assert unknown == [], "each driver names an entry point and one of its commands"


def test_each_driver_runs_a_fault_and_names_only_faults_it_could_run() -> None:
    empty = [driver.name for driver in faults.DRIVERS if not driver.runs()]
    assert empty == [], "a driver runs at least one fault"
    kinds = {
        driver.name: [
            named
            for named in driver.not_run
            for fault, _, path in [named.partition(" ")]
            if fault not in FAULTS
            or (path and (fault not in FILE_FAULTS or path not in driver.inputs))
            or (fault in FILE_FAULTS and not driver.inputs)
            or (fault in GIT_FAULTS and not driver.git)
            or (fault in GH_FAULTS and not driver.gh)
        ]
        for driver in faults.DRIVERS
    }
    assert {name: found for name, found in kinds.items() if found} == {}, "not_run names a fault the driver never runs"


# The harness itself: a fixture entry point, run through fault_runner.py, shows each stop and each injected fault.
FIXTURE_DOCSTRING = "A fixture.\n\nExit status:\n  0  done\n  1  a last line FAILED <reason>\n"
FIXTURE_HEADER = (
    "import sys\nfrom pathlib import Path\nfrom console import run_main\nfrom git_client import GitClient\n"
    "from github_client import GitHubClient\n"
)


def fixture_run(
    body: str, fault: str, target: str | None = None
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Run a fixture entry point whose main holds body under the fault, in a throwaway fixture; target names a file
    below the work folder, which exists."""
    with tempfile.TemporaryDirectory(prefix="fault fixture ") as temporary:
        root = Path(temporary).resolve()
        fixture = Fixture(root, root / "home", root / "work", root)
        fixture.home.mkdir()
        fixture.work.mkdir()
        entry = root / "entry.py"
        main = "".join(f"    {line}\n" for line in body.splitlines())
        entry.write_text(
            f'"""{FIXTURE_DOCSTRING}"""\n{FIXTURE_HEADER}def main() -> int:\n{main}    return 0\n'
            'if __name__ == "__main__":\n    run_main(main)\n',
            encoding="utf-8",
        )
        path = fixture.work / target if target else None
        if path is not None:
            path.write_text("text\n", encoding="utf-8")
        with placed(fault, path):
            return run_entry(entry, [], fault, path, fixture, environment(fixture, Driver("entry.py", "", ())))


@pytest.mark.parametrize(
    ("body", "stopped"),
    [
        ("import subprocess\nsubprocess.run([sys.executable, '--version'])", "started a program"),
        ("import socket\nsocket.create_connection(('127.0.0.1', 9), timeout=1)", "opened a network connection"),
        (f"Path({str(REPOSITORY / 'never-written.txt')!r}).write_text('x')", "wrote outside the run's folders"),
        (f"Path({str(REPOSITORY / 'never-made')!r}).mkdir()", "changed a path outside the run's folders"),
        ("try:\n    import subprocess\n    subprocess.run(['gh'])\nexcept Exception:\n    pass", "started a program"),
    ],
)
def test_the_runner_stops_a_program_a_connection_and_a_write_outside_the_temporary_folder_past_any_handler(
    body: str, stopped: str
) -> None:
    finished, record = fixture_run(body, GIT_EXIT)
    assert record["stopped"] is not None, finished
    assert record["stopped"].startswith(stopped)
    assert not (REPOSITORY / "never-written.txt").exists()
    assert not (REPOSITORY / "never-made").exists()


@pytest.mark.parametrize(
    ("body", "fault", "target", "said"),
    [
        ("GitClient().output(['status'])", GIT_EXIT, None, "bad config"),
        ("GitClient().output(['status'])", GIT_TIMEOUT, None, "did not finish"),
        ("GitHubClient().json(['api', 'user'], retry=False)", GH_FAILURE, None, "Validation Failed"),
        ("Path('input.txt').stat()", STAT_RAISES, "input.txt", "Permission denied"),
        ("Path('input.txt').read_text()", UNREADABLE, "input.txt", "input.txt"),
        ("Path('input.txt').read_text()", DIRECTORY, "input.txt", "input.txt"),
        ("Path('input.txt').read_text()", MISSING, "input.txt", "input.txt"),
    ],
)
def test_the_runner_injects_each_fault_and_notes_that_the_run_reached_it(
    body: str, fault: str, target: str | None, said: str
) -> None:
    finished, record = fixture_run(body, fault, target)
    assert record == {"exit": 1, "reached": True, "stopped": None}, finished
    assert finished.stdout.splitlines()[-1].startswith("FAILED"), finished
    assert said in finished.stdout
    work = Path("work") / (target or "")
    assert judged(FIXTURE_DOCSTRING, fault, work if target else None, finished, record) == []


def test_a_run_may_write_to_the_null_device_which_a_program_it_starts_takes_as_a_stream() -> None:
    body = "import os\nwith open(os.devnull, 'w') as sink:\n    sink.write('x')\nPath('input.txt').stat()"
    finished, record = fixture_run(body, STAT_RAISES, "input.txt")
    assert record == {"exit": 1, "reached": True, "stopped": None}, finished


def test_a_run_under_a_fault_other_than_git_s_starts_the_real_git() -> None:
    body = "print(GitClient().output(['--version']).split()[0])\nPath('input.txt').stat()"
    finished, record = fixture_run(body, STAT_RAISES, "input.txt")
    assert record == {"exit": 1, "reached": True, "stopped": None}, finished
    assert finished.stdout.splitlines()[0] == "git"


def test_a_run_that_never_meets_its_fault_is_not_reached() -> None:
    finished, record = fixture_run("print('DONE')", GIT_EXIT)
    assert record == {"exit": 0, "reached": False, "stopped": None}
    assert judged(FIXTURE_DOCSTRING, GIT_EXIT, None, finished, record) == [
        "it never reached the fault: the driver's arguments or inputs do not reach it",
        "it exited 0, as if nothing were wrong",
        "its output never names what failed ('bad config')",
        "no line it printed is a refusal: none is FAILED, starts with a word its docstring names, or names what failed",
    ]


def ended(stdout: str, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 1, stdout, stderr)


@pytest.mark.parametrize(
    ("finished", "record", "problem"),
    [
        (ended("FAILED bad config\n"), {"exit": 1, "reached": True, "stopped": None}, None),
        (ended("", "ERROR: bad config\n"), {"exit": 1, "reached": True, "stopped": None}, None),
        (ended("NOTHING_FOUND\n"), {"exit": 0, "reached": True, "stopped": None}, "it exited 0"),
        (ended("FAILED bad config\n"), {"exit": 3, "reached": True, "stopped": None}, "it exited 3"),
        (ended("FAILED bad config\n"), {"exit": 97, "reached": True, "stopped": "x"}, "the harness stopped it"),
        (ended("FAILED x\n", f"{TRACEBACK}\nbad config"), {"exit": 1, "reached": True, "stopped": None}, "traceback"),
        (ended("FAILED not a repository\n"), {"exit": 1, "reached": True, "stopped": None}, "never names"),
        (ended(""), {"exit": 1, "reached": True, "stopped": None}, "it printed nothing"),
    ],
)
def test_judged_names_each_way_a_run_ends_other_than_a_refusal_of_its_fault(
    finished: subprocess.CompletedProcess[str], record: dict[str, Any], problem: str | None
) -> None:
    found = judged(FIXTURE_DOCSTRING, GIT_EXIT, None, finished, record)
    if problem is None:
        assert found == []
    else:
        assert any(problem in each for each in found), found


def test_changes_names_a_write_and_leaves_the_faults_own_path_aside() -> None:
    root = Path("root")
    before = {"w": {"root/w": None, "root/w/a": b"1", "root/w/input": b"x"}}
    after = {"w": {"root/w": None, "root/w/a": b"2", "root/w/b": b"new", "root/w/input": None}}
    assert changes(before, after, Path("root/w/input"), root) == ["it changed ['w/a', 'w/b'] before it refused"]
    assert changes(before, before, None, root) == []


@pytest.mark.parametrize("fault", [UNREADABLE, DIRECTORY, MISSING, STAT_RAISES])
def test_placed_puts_the_file_back_as_it_was(fault: str) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "input.json"
        path.write_bytes(b'{"value": 1}')
        with placed(fault, path):
            pass
        assert path.read_bytes() == b'{"value": 1}'

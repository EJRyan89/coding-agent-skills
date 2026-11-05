"""Every entry point's every command, run under every environment fault it can meet, ends in a refusal it documents.

tests/validation/exit_tables.py finds the entry points by reading the code: a module whose `__main__` block calls
run_main, so a new one is held here from the pull request that adds it. faults.py drives each of its commands, its
argparse subcommands read from the code, or names it in FAULTS_NOT_RUN with the reason the harness cannot drive it.
fault_table.py names the faults: a file the command reads made unreadable, put in a directory's place, removed, or
given a stat that raises, and git exiting 128, git timing out, and gh failing; a member of a record of another type is
the content corpus's, test_malformed_input.py's field cases.

fault_runner.py runs each command as its own program in a throwaway fixture, with that home, under one fault, and
stops it, past any handler, if it starts a program other than git, opens a network connection, or writes outside the
temporary folder. Each run must reach its fault and end with a nonzero exit its docstring's exit table names, a last
line that is FAILED or a word its docstring names, no traceback, and the home, the work folder, and the outputs the
driver names as it found them: a command that meets a fault and ends as if nothing were there, or writes before it
refuses, fails here.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "validation"))

import corpus
import exit_tables
import faults
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
    git_environment,
)
from validation_support import repository_files

REPOSITORY = Path(__file__).resolve().parents[2]
RUNNER = Path(__file__).with_name("fault_runner.py")
SKILL_CORE = Path("skills") / "ejryan89-skill-core" / "scripts"
ENTRIES = {entry.name: entry for entry in exit_tables.entry_points(REPOSITORY)}
RUNS = [(driver, fault, path) for driver in faults.DRIVERS for fault, path in driver.runs()]
# How long one run may take before the suite stops it, far past any refusal.
TIMEOUT = 120
TRACEBACK = "Traceback (most recent call last)"
# How many changed paths a problem names before it says there are more.
CHANGES_SHOWN = 10
# The variables a run never inherits, since each names a real session, configuration, or account.
NOT_INHERITED = re.compile(
    r"^(?:CLAUDE|CODEX|COPILOT|CODE_REVIEW|GITHUB|GH|GIT|ANTHROPIC|OPENAI)_|^HOME$|^USERPROFILE$"
)


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


@pytest.fixture(scope="session")
def checkout() -> Iterator[Path]:
    """A copy of this checkout, every file a commit could hold, committed once in a repository of its own."""
    with tempfile.TemporaryDirectory(prefix="fault-checkout ") as temporary:
        root = Path(temporary) / "checkout"
        for path in repository_files(REPOSITORY):
            target = root / path.relative_to(REPOSITORY)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        home = Path(temporary) / "home"
        home.mkdir()
        fixture = Fixture(Path(temporary), home, root, root)
        fixture.git("init", "-q", "-b", "main")
        fixture.git("add", "-A")
        fixture.git("commit", "-q", "-m", "checkout")
        yield root


def tree(path: Path) -> dict[str, bytes | None]:
    """Every entry at and below path by its path: a file's bytes, or None for a folder or one this process cannot
    read; an empty mapping for a path that is not there."""
    if not path.exists():
        return {}
    if path.is_file():
        return {path.as_posix(): path.read_bytes()}
    found: dict[str, bytes | None] = {path.as_posix(): None}
    for entry in sorted(path.rglob("*")):
        try:
            found[entry.as_posix()] = entry.read_bytes() if entry.is_file() else None
        except OSError:
            found[entry.as_posix()] = None
    return found


@contextmanager
def placed(fault: str, path: Path | None) -> Iterator[None]:
    """The fault at path while the context lasts, and the file as it was afterwards."""
    if path is None or fault == STAT_RAISES:
        yield
        return
    assert path.is_file(), f"the input {path} is no file once the fixture is built"
    original = path.read_bytes()
    try:
        if fault == UNREADABLE:
            with corpus.denied(path):
                yield
            return
        path.unlink()
        if fault == DIRECTORY:
            path.mkdir()
        yield
    finally:
        if fault in {DIRECTORY, MISSING}:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            path.write_bytes(original)


@contextmanager
def restored(fixture: Fixture) -> Iterator[None]:
    """The fixture's checkout copy put back at its commit after the context, whatever a driver's prepare or its run
    changed in it, so the next run of the suite process reads it as it was."""
    try:
        yield
    finally:
        fixture.git("checkout", "-q", "--", ".", folder=fixture.checkout)
        fixture.git("clean", "-fdq", folder=fixture.checkout)


def environment(fixture: Fixture, driver: Driver) -> dict[str, str]:
    """What a run's command sees: the fixture's git environment, without a variable that names a real session or
    account, and the driver's own."""
    values = fixture.values()
    inherited = {key: value for key, value in git_environment(fixture).items() if not NOT_INHERITED.match(key)}
    git = {key: value for key, value in git_environment(fixture).items() if key.startswith("GIT_")}
    return {
        **inherited,
        **git,
        "HOME": str(fixture.home),
        "USERPROFILE": str(fixture.home),
        "PYTHONDONTWRITEBYTECODE": "1",
        **{key: value.format(**values) for key, value in driver.environment.items()},
    }


def evidence(fault: str, target: Path | None) -> tuple[str, ...]:
    """The words a refusal of the fault holds, any one of them, so it names what failed rather than reading the fault
    as something that is not there: git's reason, its time limit, gh's answer, or the file's name."""
    if fault == GIT_EXIT:
        return ("bad config",)
    if fault == GIT_TIMEOUT:
        # GitClient's words for a command past its limit, and for a stream that gave no output or did not exit in time.
        return ("did not finish", "timed out", "timeout", "gave no output for", "had not exited after")
    if fault == GH_FAILURE:
        return ("Validation Failed", "422")
    assert target is not None
    return (target.name,)


def judged(
    docstring: str, fault: str, target: Path | None, finished: subprocess.CompletedProcess[str], record: dict[str, Any]
) -> list[str]:
    """What is wrong with a run's end, judged against its entry point's docstring: a stop, a fault never reached, an
    exit 0 or one the table does not name, a traceback, output that never names what failed, or a last line that is
    no refusal."""
    problems: list[str] = []
    if record["stopped"]:
        problems.append(f"the harness stopped it: {record['stopped']}")
    if not record["reached"]:
        problems.append("it never reached the fault: the driver's arguments or inputs do not reach it")
    try:
        table = exit_tables.exit_table(docstring)
    except exit_tables.TableUnreadable as error:
        table = {}
        problems.append(f"its docstring {error}")
    if record["exit"] == 0:
        problems.append("it exited 0, as if nothing were wrong")
    elif record["exit"] not in table and not record["stopped"]:
        problems.append(f"it exited {record['exit']}, which its exit table does not name")
    output = finished.stdout + finished.stderr
    if TRACEBACK in output:
        problems.append("it printed a traceback")
    words = evidence(fault, target)
    if not any(word.casefold() in output.casefold() for word in words):
        problems.append(f"its output never names what failed ({' or '.join(map(repr, words))})")
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        problems.append("it printed nothing")
    elif not any(refuses(line, docstring, words) for line in lines):
        problems.append(
            "no line it printed is a refusal: none is FAILED, starts with a word its docstring names, or "
            "names what failed"
        )
    return problems


def refuses(line: str, docstring: str, words: tuple[str, ...]) -> bool:
    """Whether a line is a refusal: a FAILED line, one led by a word its docstring names, such as a finding's or
    ERROR, or one that names what failed. The refusal need not be the last line, since one may follow it with its
    remedy, as the deployer's does."""
    first = line.split()[0].rstrip(":")
    return (
        first == "FAILED"
        or (first.isupper() and re.search(rf"(?<![\w-]){re.escape(first)}(?![\w-])", docstring) is not None)
        or any(word.casefold() in line.casefold() for word in words)
    )


def shown(name: str, root: Path) -> str:
    """A changed path as a problem names it: below the fixture root when it lies there, else whole."""
    path = Path(name)
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()


def run_entry(
    entry: Path,
    arguments: list[str],
    fault: str,
    target: Path | None,
    fixture: Fixture,
    variables: dict[str, str],
    repository: Path = REPOSITORY,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Run an entry point through fault_runner.py under the fault, in the fixture's work folder, and return how it
    ended and the runner's record."""
    record_file = fixture.root / "record.json"
    spec = {
        "entry": str(entry),
        "arguments": arguments,
        "fault": fault,
        "target": str(target) if target else None,
        "writable": [tempfile.gettempdir()],
        "path": [str(repository / SKILL_CORE)],
        "repository": str(repository),
        "record": str(record_file),
    }
    spec_file = fixture.root / "run.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    finished = subprocess.run(
        [sys.executable, "-B", str(RUNNER), str(spec_file)],
        cwd=fixture.work,
        env=variables,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT,
        check=False,
    )
    assert record_file.is_file(), f"the runner wrote no record:\n{finished.stdout}\n{finished.stderr}"
    return finished, json.loads(record_file.read_text(encoding="utf-8"))


def changes(
    before: dict[str, dict[str, bytes | None]],
    after: dict[str, dict[str, bytes | None]],
    target: Path | None,
    root: Path,
) -> list[str]:
    """A problem naming each entry a run added, removed, or changed in the folders watched, the fault's own aside."""
    changed = sorted(
        name
        for folder in before
        for name in before[folder].keys() | after[folder].keys()
        if target is None or not Path(name).is_relative_to(target)
        if before[folder].get(name, b"\0absent") != after[folder].get(name, b"\0absent")
    )
    more = " and more" if len(changed) > CHANGES_SHOWN else ""
    shown_names = [shown(name, root) for name in changed[:CHANGES_SHOWN]]
    return [f"it changed {shown_names}{more} before it refused"] if changed else []


@pytest.mark.parametrize(
    ("driver", "fault", "path"),
    RUNS,
    ids=[f"{driver.name}-{fault}-{Path(path).name if path else ''}".rstrip("-") for driver, fault, path in RUNS],
)
def test_each_command_refuses_each_fault_as_its_docstring_says(
    driver: Driver, fault: str, path: str | None, checkout: Path
) -> None:
    with tempfile.TemporaryDirectory(prefix="fault run ") as temporary:
        root = Path(temporary)
        fixture = Fixture(root, root / "home", root / "work", checkout)
        fixture.home.mkdir()
        fixture.work.mkdir()
        with restored(fixture):
            if driver.prepare is not None:
                driver.prepare(fixture)
            values = fixture.values()
            repository = checkout if driver.from_checkout else REPOSITORY
            target = Path(path.format(**values)) if path else None
            assert target is None or target.is_relative_to(root) or target.is_relative_to(checkout), (
                f"{path} lies outside the fixture and its checkout copy, where no fault may be placed"
            )
            watched = [fixture.home, fixture.work, *(Path(each.format(**values)) for each in driver.outputs)]
            with placed(fault, target):
                before = {str(each): tree(each) for each in watched if each != target}
                finished, record = run_entry(
                    repository / driver.entry,
                    [argument.format(**values) for argument in driver.arguments],
                    fault,
                    target,
                    fixture,
                    environment(fixture, driver),
                    repository,
                )
                after = {str(each): tree(each) for each in watched if each != target}
            problems = judged(ast.get_docstring(ENTRIES[driver.entry].tree) or "", fault, target, finished, record)
            problems += changes(before, after, target, root)
    assert problems == [], (
        f"{driver.name} under {fault} ({FAULTS[fault]}){f' at {path}' if path else ''}: {'; '.join(problems)}\n"
        f"exit {record['exit']}\nstdout:\n{finished.stdout[-2000:]}\nstderr:\n{finished.stderr[-2000:]}"
    )


# The harness itself: a fixture entry point, run through fault_runner.py, shows each stop and each injected fault.
FIXTURE_DOCSTRING = "A fixture.\n\nExit status:\n  0  done\n  1  a last line FAILED <reason>\n"
FIXTURE_HEADER = (
    "import sys\nfrom pathlib import Path\nfrom console import run_main\nfrom git_client import GitClient\n"
    "from github_client import GitHubClient\n"
)


def fixture_run(body: str, fault: str, target: str | None = None) -> tuple[subprocess.CompletedProcess[str], dict]:
    """Run a fixture entry point whose main holds body under the fault, in a throwaway fixture; target names a file
    below the work folder, which exists."""
    with tempfile.TemporaryDirectory(prefix="fault fixture ") as temporary:
        root = Path(temporary)
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

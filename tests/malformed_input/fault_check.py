"""The run machinery of the fault corpus: one command of an entry point, run as its own program under one environment
fault in a throwaway fixture, and judged.

fault_runner.py runs the command under the fault and stops it, past any handler, if it starts a program other than
git, opens a network connection, or writes outside the temporary folder. A run must reach its fault and end with a
nonzero exit its docstring's exit table names, output that names what failed, a refusal line, no traceback, and the
home, the work folder, and the outputs the driver names as it found them. The suites test_faults_<group>.py run every
driver's runs of their group of faults, so the runner deals each group to a leg of its own; test_faults.py holds the
coverage and the harness's own tests.
"""

from __future__ import annotations

import ast
import atexit
import functools
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "validation"))

import corpus
import exit_tables
import faults
from fault_table import (
    DIRECTORY,
    FAULTS,
    GH_FAILURE,
    GIT_EXIT,
    GIT_TIMEOUT,
    MISSING,
    STAT_RAISES,
    UNREADABLE,
    Driver,
    Fixture,
    git_environment,
)
from validation_support import parse_python, repository_files

REPOSITORY = Path(__file__).resolve().parents[2]
RUNNER = Path(__file__).with_name("fault_runner.py")
SKILL_CORE = Path("skills") / "ejryan89-skill-core" / "scripts"
# How long one run may take before the suite stops it, far past any refusal.
TIMEOUT = 120
TRACEBACK = "Traceback (most recent call last)"
# How many changed paths a problem names before it says there are more.
CHANGES_SHOWN = 10
# The variables a run never inherits, since each names a real session, configuration, or account.
NOT_INHERITED = re.compile(
    r"^(?:CLAUDE|CODEX|COPILOT|CODE_REVIEW|GITHUB|GH|GIT|ANTHROPIC|OPENAI)_|^HOME$|^USERPROFILE$"
)
Run = tuple[Driver, str, str | None]


def runs_of(*kinds: str) -> list[Run]:
    """Every driver's runs of the given faults, each with the input it is placed at, or None for git's and gh's."""
    return [(driver, fault, path) for driver in faults.DRIVERS for fault, path in driver.runs() if fault in kinds]


def run_ids(runs: list[Run]) -> list[str]:
    """Each run's test ID: the driver, the fault, and the input's file name."""
    return [f"{driver.name}-{fault}-{Path(path).name if path else ''}".rstrip("-") for driver, fault, path in runs]


@functools.cache
def shared_checkout() -> Path:
    """A copy of this checkout, every file a commit could hold, committed once in a repository of its own, made once
    for the suite process and removed when it ends. Resolved, so the copy is named by the path a script resolves, not
    a Windows short name of the temporary folder. The folder's own cleanup removes it, which clears the read-only flag
    Windows git sets on a repository's objects, where a bare rmtree is refused."""
    folder = tempfile.TemporaryDirectory(prefix="fault-checkout ")
    atexit.register(folder.cleanup)
    temporary = Path(folder.name).resolve()
    root = temporary / "checkout"
    for path in repository_files(REPOSITORY):
        target = root / path.relative_to(REPOSITORY)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    home = temporary / "home"
    home.mkdir()
    fixture = Fixture(temporary, home, root, root)
    fixture.git("init", "-q", "-b", "main")
    fixture.git("add", "-A")
    fixture.git("commit", "-q", "-m", "checkout")
    return root


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
def restored(fixture: Fixture, used: bool) -> Iterator[None]:
    """The fixture's checkout copy put back at its commit after the context, when the driver runs from it, whatever
    its prepare or its run changed in it, so the next run of the suite process reads it as it was. A driver that does
    not run from the copy reads and writes only its own fixture, so the copy needs no putting back."""
    try:
        yield
    finally:
        if used:
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


def check(driver: Driver, fault: str, path: str | None) -> None:
    """Run one command of an entry point under one fault and fail with what is wrong with how it ended."""
    checkout = shared_checkout()
    with tempfile.TemporaryDirectory(prefix="fault run ") as temporary:
        root = Path(temporary).resolve()
        fixture = Fixture(root, root / "home", root / "work", checkout)
        fixture.home.mkdir()
        fixture.work.mkdir()
        with restored(fixture, driver.from_checkout):
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
            docstring = ast.get_docstring(parse_python(REPOSITORY / driver.entry)) or ""
            problems = judged(docstring, fault, target, finished, record)
            problems += changes(before, after, target, root)
    assert problems == [], (
        f"{driver.name} under {fault} ({FAULTS[fault]}){f' at {path}' if path else ''}: {'; '.join(problems)}\n"
        f"exit {record['exit']}\nstdout:\n{finished.stdout[-2000:]}\nstderr:\n{finished.stderr[-2000:]}"
    )

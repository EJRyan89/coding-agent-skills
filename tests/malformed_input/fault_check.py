"""The run machinery of the fault corpus: one command of an entry point, run as its own program under one environment
fault in a throwaway fixture, and judged.

fault_runner.py runs the command under the fault and stops it, past any handler, if it starts a program other than
git, opens a network connection, or writes outside the temporary folder and the runtime folder private_folder keeps a
script's state in, `private_folder.base()`, or inside this checkout, even where the temporary folder holds it. A run
must reach its fault and end with a nonzero exit its docstring's exit table names, a line that names what failed and
is its refusal, FAILED, led by a word its docstring names, or argparse's usage error at exit 2, which a remedy line may
follow, no traceback, and the home, the work folder, and the outputs the driver names as it found them. The suites
test_faults_<group>.py run every driver's runs of their group of faults, so the runner deals each group to a leg of
its own; test_faults.py holds the coverage and the harness's own tests.
"""

from __future__ import annotations

import ast
import atexit
import functools
import json
import os
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
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import corpus
import exit_tables
import faults
import private_folder
from fault_table import (
    DIRECTORY,
    FAULTS,
    GH_FAILURE,
    GIT_EXIT,
    GIT_TIMEOUT,
    LINK_TO_NOTHING,
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
# argparse's usage error, as ArgumentParser.error prints it: the program's name, then `error:` and the message.
USAGE_ERROR = re.compile(r"^\S+: error: \S")


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
    read; an empty mapping for a path that is not there. A path it cannot stat, and a folder it cannot list, raise,
    never read as absent or empty, since a change made there would then pass unseen."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return {}
    if path.is_file():
        return {path.as_posix(): path.read_bytes()}
    found: dict[str, bytes | None] = {path.as_posix(): None}
    for entry in corpus.entries(path):
        try:
            found[entry.as_posix()] = entry.read_bytes() if entry.is_file() else None
        except OSError:
            found[entry.as_posix()] = None
    return found


def nowhere(path: Path) -> Path:
    """Where the link-to-nothing fault's link at path leads: a file beside it that is not there, which a run that
    writes through the link would create."""
    return path.with_name(f"{path.name}.nowhere")


@contextmanager
def placed(fault: str, path: Path | None) -> Iterator[None]:
    """The fault at path while the context lasts, and the file as it was afterwards, with nothing left where a link to
    nothing led."""
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
        elif fault == LINK_TO_NOTHING:
            path.symlink_to(nowhere(path))
        yield
    finally:
        if fault in {DIRECTORY, MISSING, LINK_TO_NOTHING}:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif os.path.lexists(path):
                path.unlink()
            if fault == LINK_TO_NOTHING and os.path.lexists(nowhere(path)):
                nowhere(path).unlink()
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
    docstring: str,
    fault: str,
    target: Path | None,
    finished: subprocess.CompletedProcess[str],
    record: dict[str, Any],
    gap: str = "",
) -> list[str]:
    """What is wrong with a run's end, judged against its entry point's docstring: a stop, a fault never reached, an
    exit 0 or one the table does not name, a traceback, output that never names what failed, or no line naming it
    that is a refusal, as refuses reads one. A run its driver's refusal_gaps names, with the open
    issue that gives it a refusal line, is held to every check but the last, and fails once that check passes, so the
    gaps only shrink."""
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
    lines = [line for line in output.splitlines() if line.strip()]
    naming = [line for line in lines if named(line, words)]
    if not lines:
        problems.append("it printed nothing")
    elif not naming:
        problems.append(f"its output never names what failed ({' or '.join(map(repr, words))})")
    else:
        refused = any(refuses(line, docstring, record["exit"]) for line in naming)
        if not refused and not gap:
            problems.append(
                "no line naming what failed is its refusal: none is FAILED, starts with a word its docstring names, "
                "or is argparse's usage error at exit 2"
            )
        elif refused and gap:
            problems.append(f"its refusal gap is closed, so the driver's refusal_gaps entry goes: {gap}")
    return problems


def read_as_absent(docstring: str, finished: subprocess.CompletedProcess[str], record: dict[str, Any]) -> list[str]:
    """What is wrong with a run that may read its input missing as an answer, under a link to nothing at it: a stop, a
    fault never reached, an exit its exit table does not name, or a traceback. It may succeed and write, as it does
    with the file missing, so only written_through judges what it wrote."""
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
    if record["exit"] not in table and not record["stopped"]:
        problems.append(f"it exited {record['exit']}, which its exit table does not name")
    if TRACEBACK in finished.stdout + finished.stderr:
        problems.append("it printed a traceback")
    return problems


# The words a refusal of something of another kind in a file's place uses, which a link to nothing, read as the file
# missing, never draws.
OTHER_KIND = ("not a file", "not a folder", "not a directory")


def named_another_kind(fault: str, finished: subprocess.CompletedProcess[str]) -> list[str]:
    """A problem when a run under the link-to-nothing fault names what it met as something of another kind, where the
    link reads as the file missing."""
    if fault != LINK_TO_NOTHING:
        return []
    output = (finished.stdout + finished.stderr).casefold()
    words = [word for word in OTHER_KIND if word in output]
    return [f"it named the link to nothing {words[0]!r}, never reading it as the file missing"] if words else []


def written_through(fault: str, target: Path | None) -> list[str]:
    """A problem when a run under the link-to-nothing fault wrote through the link, creating the file it leads to."""
    if fault != LINK_TO_NOTHING or target is None or not os.path.lexists(nowhere(target)):
        return []
    return [f"it wrote through the link to nothing at {target.name}, creating {nowhere(target).name}"]


def refuses(line: str, docstring: str, code: int) -> bool:
    """Whether a line is a refusal's form: a FAILED line, one led by an upper-case word its docstring names, such as
    ERROR or a finding's, or, for a run that exits 2, argparse's usage error, `<program>: error: <message>`, which
    run_main passes through and every exit table names as a usage error. A remedy line may follow it, as the
    deployer's does, so it need not be the last."""
    first = line.split()[0].rstrip(":")
    return (
        first == "FAILED"
        or (first.isupper() and re.search(rf"(?<![\w-]){re.escape(first)}(?![\w-])", docstring) is not None)
        or (code == exit_tables.USAGE and USAGE_ERROR.match(line) is not None)
    )


def named(line: str, words: tuple[str, ...]) -> bool:
    """Whether a line names what failed: any one of the fault's evidence words."""
    return any(word.casefold() in line.casefold() for word in words)


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
    writable: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Run an entry point through fault_runner.py under the fault, in the fixture's work folder, and return how it
    ended and the runner's record. The run may write in `writable`, unless a caller names another the temporary folder
    and the folder private_folder keeps a script's state across runs in, the runtime directory where one is set, and
    never in this checkout, which lies under the temporary folder when the mutation gate runs the suite in a throwaway
    worktree."""
    record_file = fixture.root / "record.json"
    spec = {
        "entry": str(entry),
        "arguments": arguments,
        "fault": fault,
        "target": str(target) if target else None,
        "writable": [str(writable)] if writable else [tempfile.gettempdir(), str(private_folder.base())],
        "protected": [str(REPOSITORY)],
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
                written = written_through(fault, target)
            docstring = ast.get_docstring(parse_python(REPOSITORY / driver.entry)) or ""
            if fault == LINK_TO_NOTHING and path is not None and driver.absent_is_an_answer(path):
                problems = read_as_absent(docstring, finished, record)
            else:
                problems = judged(docstring, fault, target, finished, record, driver.refusal_gap(fault, path))
                problems += changes(before, after, target, root)
            problems += written + named_another_kind(fault, finished)
    assert problems == [], (
        f"{driver.name} under {fault} ({FAULTS[fault]}){f' at {path}' if path else ''}: {'; '.join(problems)}\n"
        f"exit {record['exit']}\nstdout:\n{finished.stdout[-2000:]}\nstderr:\n{finished.stderr[-2000:]}"
    )

"""One rule settles a committed run's transient backup, whether the run's own finalization or a later recovery meets it.

Each row puts the same backup on disk and runs it through pipeline._finalize and through recovery's _finish_backup,
which must leave the same tree and give the same warnings. The last tests hold that "When recovery fails" in
docs/recovery.md names every stop recovery makes, and how those stops are found.
"""

from __future__ import annotations

import ast
import contextlib
import errno
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import file_presence
from harness import REPOSITORY_ROOT, DeployerTestCase, backup_entry, make_directory_link

from deployer import fsops, hashing, journal, pipeline
from deployer.context import Context
from deployer.names import deletion_name, transient_name

RUN = "20260101-000000-sett"
PREVIOUS = "previous alpha\n"
CHANGED = "changed after the plan\n"


@dataclass(frozen=True)
class Row:
    """A backup's state when the run settles it, and what settling it must leave."""

    retain: bool
    changed: bool = False
    # What goes wrong while settling it: "stat" for a hash that cannot be read, "occupied" for something already at
    # the copy's place under .backups, "hold" for a copy that cannot be renamed to its deletion name, as Windows refuses
    # for a folder holding a file another program has open, "remove" for a deletion that fails partway, and "kill" for
    # a process killed partway through the deletion, which the next deployment's recovery meets.
    fault: str = ""
    # Where the copy ends: "kept" under .backups, "beside" its item, "deleting" for what is left of it at its deletion
    # name, or "deleted".
    ends: str = "kept"
    # The start of each warning settling it gives, in order.
    warnings: tuple[str, ...] = ()


ROWS = {
    "kept unchanged": Row(retain=True),
    "kept changed": Row(retain=True, changed=True),
    "unkept unchanged": Row(retain=False, ends="deleted"),
    "unkept changed": Row(retain=False, changed=True, warnings=("alpha changed after run",)),
    "unkept unreadable": Row(retain=False, fault="stat", warnings=("Could not tell whether alpha's previous copy",)),
    "unkept changed, its place taken": Row(
        retain=False, changed=True, fault="occupied", ends="beside", warnings=("alpha's previous copy is kept at",)
    ),
    "kept, its place taken": Row(
        retain=True, fault="occupied", ends="beside", warnings=("alpha's previous copy is kept at",)
    ),
    "unkept unchanged, held open": Row(
        retain=False, fault="hold", ends="beside", warnings=("alpha's previous copy could not be deleted, so it stays",)
    ),
    "unkept unchanged, deleted partway": Row(
        retain=False,
        fault="remove",
        ends="deleting",
        warnings=("alpha's previous copy could not be deleted, so what is left of it stays",),
    ),
    # The deployer's own deletion, stopped partway, is finished by recovery, never read as a change after the plan.
    "unkept unchanged, killed partway through its deletion": Row(retain=False, fault="kill", ends="deleted"),
}


class Killed(BaseException):
    """The process ending at a point, which nothing in the deployer catches."""


class SettlementTests(DeployerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.transient = self.skills_dir / transient_name("alpha")
        self.deletion = self.skills_dir / deletion_name("alpha")
        self.destination = self.skills_dir / ".backups" / RUN / "alpha"

    def backup(self, row: Row) -> dict[str, Any]:
        """The row's transient backup on disk beside the copy the run installed, and the entry the run journaled for
        it."""
        self.write(self.skills_dir / "alpha" / "SKILL.md", "installed alpha\n")
        self.write(self.transient / "SKILL.md", PREVIOUS)
        # A second file, so a deletion stopped after the first leaves part of the copy.
        self.write(self.transient / "notes.md", PREVIOUS)
        entry = backup_entry("alpha", hashing.hash_path(self.transient), row.retain, RUN)
        if row.changed:
            self.write(self.transient / "SKILL.md", CHANGED)
        if row.fault == "occupied":
            self.write(self.destination / "SKILL.md", "already there\n")
        return entry

    @contextmanager
    def faulted(self, row: Row) -> Iterator[list[str]]:
        """The row's fault in place, yielding the warnings settling gives."""
        warnings: list[str] = []
        real_remove, real_move = fsops.remove, fsops.move

        def remove(path: Path) -> None:
            # The copy being deleted, under whichever name the deletion reads it: part of it goes, then the fault.
            if path in (self.transient, self.deletion):
                real_remove(path / "SKILL.md")
                if row.fault == "kill":
                    raise Killed
                raise PermissionError(errno.EACCES, "Access is denied", str(path / "notes.md"))
            real_remove(path)

        def move(source: Path, destination: Path) -> None:
            if source == self.transient and destination == self.deletion:
                raise PermissionError(errno.EACCES, "Access is denied", str(source))
            real_move(source, destination)

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(journal, "_warn", side_effect=warnings.append))
            if row.fault == "stat":
                failure = PermissionError(errno.EACCES, "Access is denied")
                stack.enter_context(mock.patch.object(journal, "backup_unchanged", side_effect=failure))
            if row.fault in ("remove", "kill"):
                stack.enter_context(mock.patch.object(fsops, "remove", side_effect=remove))
            if row.fault == "hold":
                stack.enter_context(mock.patch.object(fsops, "move", side_effect=move))
            yield warnings

    def finalize(self, entry: dict[str, Any]) -> None:
        record = journal.Journal(self.staging_run(RUN) / "journal.jsonl", RUN)
        record.create()
        record.entries.append(cast(journal.BackupEntry, entry))
        pipeline._finalize(cast(Context, SimpleNamespace(paths=self.paths)), record, RUN)

    def recover(self, entry: dict[str, Any]) -> None:
        self.assertTrue(journal._finish_backup(self.paths, RUN, cast(journal.BackupEntry, entry)))

    def settled(self, row: Row, settle: Callable[[dict[str, Any]], None]) -> tuple[str, list[str]]:
        """Where the copy the row puts on disk ends once settle runs, and the warnings it gives."""
        entry = self.backup(row)
        try:
            with self.faulted(row) as warnings:
                if row.fault == "kill":
                    with self.assertRaises(Killed):
                        settle(entry)
                else:
                    settle(entry)
            if row.fault == "kill":
                # The next deployment's recovery meets the run the kill left.
                with self.faulted(Row(retain=row.retain)) as recovered:
                    self.recover(entry)
                warnings += recovered
            if self.transient.exists():
                ends = "beside"
            elif self.deletion.exists():
                ends = "deleting"
                self.assertFalse((self.deletion / "SKILL.md").exists())
            elif self.destination.exists() and row.fault != "occupied":
                ends = "kept"
                content = CHANGED if row.changed else PREVIOUS
                self.assertEqual(content, (self.destination / "SKILL.md").read_text(encoding="utf-8"))
            else:
                ends = "deleted"
        finally:
            # Each row starts from an empty root, whether or not the one before it held.
            fsops.remove(self.skills_dir)
            fsops.remove(self.staging_run(RUN))
        return ends, warnings

    def test_finalization_and_recovery_settle_each_backup_alike(self) -> None:
        for case, row in ROWS.items():
            for path, settle in (("finalization", self.finalize), ("recovery", self.recover)):
                with self.subTest(case=case, path=path):
                    ends, warnings = self.settled(row, settle)
                    self.assertEqual(row.ends, ends)
                    self.assertEqual(len(row.warnings), len(warnings), warnings)
                    for start, warning in zip(row.warnings, warnings, strict=True):
                        self.assertTrue(warning.startswith(start), warning)
                    if row.ends != "kept":
                        # A warning that names .backups as where the copy went names the folder that holds it.
                        self.assertFalse(any("instead of being deleted" in warning for warning in warnings))

    def test_a_retry_after_either_path_moved_the_copy_completes_with_no_warning(self) -> None:
        # Recovery repeats safely: an attempt stopped after the move, before the journal was cleared, is retried by
        # recovery over the copy already under .backups, whatever its content.
        for case, row in ROWS.items():
            if row.ends != "kept" or row.fault:
                continue
            for path, settle in (("finalization", self.finalize), ("recovery", self.recover)):
                with self.subTest(case=case, path=path):
                    entry = self.backup(row)
                    try:
                        with self.faulted(row):
                            settle(entry)
                        with self.faulted(row) as warnings:
                            self.recover(entry)
                        self.assertEqual([], warnings)
                        self.assertTrue(self.destination.is_dir())
                    finally:
                        # Each row starts from an empty root, whether or not the one before it held.
                        fsops.remove(self.skills_dir)
                        fsops.remove(self.staging_run(RUN))

    def test_a_retry_after_a_deletion_stopped_partway_finishes_it_with_no_warning(self) -> None:
        # What a failed deletion leaves at the deletion name is the deployer's own: a later recovery of the run deletes
        # it, as it does what a kill left there.
        for case, row in ROWS.items():
            if row.ends != "deleting":
                continue
            for path, settle in (("finalization", self.finalize), ("recovery", self.recover)):
                with self.subTest(case=case, path=path):
                    entry = self.backup(row)
                    try:
                        with self.faulted(row):
                            settle(entry)
                        self.assertTrue(self.deletion.is_dir())
                        with self.faulted(Row(retain=row.retain)) as warnings:
                            self.recover(entry)
                        self.assertEqual([], warnings)
                        self.assertEqual(["alpha"], sorted(child.name for child in self.skills_dir.iterdir()))
                    finally:
                        # Each row starts from an empty root, whether or not the one before it held.
                        fsops.remove(self.skills_dir)
                        fsops.remove(self.staging_run(RUN))

    def test_a_retry_refuses_what_stands_in_for_the_kept_copy(self) -> None:
        # A kept copy is kept whatever its content, but only the copy the run moved: a link, a link above it, or an item
        # of another kind than the one the run replaced stops recovery, as a missing copy does.
        outside = self.root / "outside"

        def junction() -> None:
            self.write(outside / "unrelated" / "SKILL.md", "unrelated\n")
            self.destination.parent.mkdir(parents=True)
            make_directory_link(self.destination, outside / "unrelated")

        def run_folder_junction() -> None:
            self.write(outside / "run" / "alpha" / "SKILL.md", "unrelated\n")
            self.destination.parent.parent.mkdir(parents=True)
            make_directory_link(self.destination.parent, outside / "run")

        def file_for_a_folder() -> None:
            self.write(self.destination, "a file where a folder was kept\n")

        def folder_for_a_file() -> None:
            fsops.remove(self.skills_dir / "alpha")
            self.write(self.skills_dir / "alpha", "an installed file\n")
            self.write(self.destination / "SKILL.md", "a folder where a file was kept\n")

        def neither_file_nor_folder() -> None:
            self.write(self.destination, "read as a device or a pipe\n")

        real_is_present = file_presence.is_present

        def is_present(path: Path, *rest: Any, **options: Any) -> bool:
            # A device or a pipe, which no fixture makes on every platform, reads as neither a file nor a folder.
            if path == self.destination:
                raise file_presence.NotAFile(path)
            return real_is_present(path, *rest, **options)

        something_else = mock.patch.object(file_presence, "is_present", side_effect=is_present)
        cases = {
            "a junction in its place": (junction, "is a symlink or junction", None),
            "its run folder a junction": (run_folder_junction, "sits under a symlink or junction", None),
            "a file for a folder": (file_for_a_folder, "is a file, not a folder as alpha is", None),
            "a folder for a file": (folder_for_a_file, "is a folder, not a file as alpha is", None),
            "neither a file nor a folder": (neither_file_nor_folder, "is something else, not a folder", something_else),
        }
        for case, (stand_in, says, reads) in cases.items():
            with self.subTest(case=case):
                entry = self.backup(ROWS["kept unchanged"])
                try:
                    fsops.remove(self.transient)
                    stand_in()
                    with self.faulted(ROWS["kept unchanged"]) as warnings, reads or contextlib.nullcontext():
                        reconciled = journal._finish_backup(self.paths, RUN, cast(journal.BackupEntry, entry))
                    self.assertFalse(reconciled)
                    self.assertEqual(1, len(warnings), warnings)
                    self.assertIn(says, warnings[0])
                    self.assertIn(f"The kept backup of alpha at .backups/{RUN}/alpha ", warnings[0])
                finally:
                    fsops.remove(self.skills_dir)
                    fsops.remove(outside)


# Each stop recovery makes, by the text of the WARNING or the ERROR line it prints before it answers False, or, for a
# DeployError it reports, by the function that reports it, mapped to the words "When recovery fails" in
# docs/recovery.md names it by. A stop added without its row fails the test below.
KEPT_STAND_IN = (
    "what stands where a copy was kept in `.backups/<run-id>/` is a link, sits under one, or is not the kind of item "
    "the run replaced"
)
STOPS = {
    "Installed destination missing for {}": "an item changed or went missing after the run installed it",
    "Installed {} hash mismatch (expected {}, got {})": "an item changed or went missing after the run installed it",
    "Unexpected state for {} during rollback": "an item changed or went missing after the run installed it",
    "Cannot rollback install of {} (hash mismatch)": "an item changed or went missing after the run installed it",
    "Cannot find backup for {}": "a backup is missing",
    "Both {} and backup are missing during rollback": "a backup is missing",
    "The kept backup of {} at {} sits under a symlink or junction, so it is not the copy run {} kept": KEPT_STAND_IN,
    "The kept backup of {} at {} is a symlink or junction, so it is not the copy run {} kept": KEPT_STAND_IN,
    "The kept backup of {} at {} is {}, not {} as {} is, so it is not the copy run {} kept": KEPT_STAND_IN,
    "Backup hash mismatch for {} during rollback": "a backup changed before a rollback could put it back",
    "Both {} and {} exist during rollback": "an item and its `.deploying-bak` both exist",
    # _undo_preserve reports the DeployError prepare_backup_destination raises.
    "_report in _undo_preserve": "the `.backups` path a rollback moves a kept copy back from is unusable",
    # _guarded reports each failure a step of recovery meets that a retry would meet again.
    "_report in _guarded": "`ERROR: Recovery of run <run-id> failed at <path>`",
    # Recovery of a committed run stops after the WARNING lines of the installs it could not verify.
    "ERROR: Install verification failed. Retaining all backups and journal.": (
        "an item changed or went missing after the run installed it"
    ),
    "ERROR: Staging run is a symlink or junction and was not replayed: {}": (
        "`ERROR: Staging run is a symlink or junction and was not replayed`"
    ),
    "ERROR: The manifest is missing or cannot be read, so recovery cannot tell whether run {} was committed.": (
        "when the manifest is missing or cannot be read, that this cannot be told"
    ),
    "ERROR: Recovery has unreconciled entries. Journal retained at: {}": (
        "prints a `WARNING` line for each item it could not reconcile"
    ),
    "ERROR: Malformed journal entry in run {}: {}": "`Malformed journal entry`",
    "ERROR: Journal validation failed. Retaining {} for manual inspection.": "`Malformed journal entry`",
    "ERROR: Run {}'s journal is not a file. Retaining {} for manual inspection.": "`journal is not a file`",
    "ERROR: Cannot list the staging runs in {}: {}": "`ERROR: Cannot list the staging runs in <path>`",
}


def _text(node: ast.expr) -> str:
    """A warning's text with each value it formats in as {}."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    assert isinstance(node, ast.JoinedStr), ast.dump(node)
    return "".join(_text(part) if isinstance(part, ast.Constant) else "{}" for part in node.values)


# The words a line recovery prints itself opens with when it reports a stop.
STOP_PREFIXES = ("ERROR:", "WARNING:")


def recovery_stops(source: str | None = None) -> set[str]:
    """Each stop in journal.py, or in the source given, that a block answers False to: each call in the run of calls
    just before the block's `return False` that reports one, a _warn by its text, a print of an ERROR or WARNING line
    by its text without its indentation, and a _report, which prints a DeployError's own lines, by the function that
    holds it. A call between them that reports nothing, such as one printing the manual steps, is passed over."""
    if source is None:
        source = (REPOSITORY_ROOT / "deployer" / "journal.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Each statement by the innermost function that holds it; ast.walk visits an outer function before an inner one.
    holder: dict[int, str] = {}
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(function):
                holder[id(node)] = function.name
    found = set()
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list) or not block:
                continue
            last = block[-1]
            if not (
                isinstance(last, ast.Return) and isinstance(last.value, ast.Constant) and last.value.value is False
            ):
                continue
            for statement in reversed(block[:-1]):
                call = statement.value if isinstance(statement, ast.Expr) else None
                if not isinstance(call, ast.Call):
                    break
                stop = _stop(call, holder.get(id(statement), "<module>"))
                if stop is not None:
                    found.add(stop)
    return found


def _stop(call: ast.Call, function: str) -> str | None:
    """The stop a call reports, as recovery_stops names it, or None when it reports none."""
    if not isinstance(call.func, ast.Name):
        return None
    if call.func.id == "_warn":
        return _text(call.args[0])
    if call.func.id == "_report":
        return f"_report in {function}"
    if call.func.id == "print" and call.args and isinstance(call.args[0], (ast.Constant, ast.JoinedStr)):
        text = _text(call.args[0]).strip()
        return text if text.startswith(STOP_PREFIXES) else None
    return None


class RecoveryStopsTests(DeployerTestCase):
    def test_when_recovery_fails_names_every_stop_recovery_makes(self) -> None:
        self.assertEqual(set(STOPS), recovery_stops())
        guide = (REPOSITORY_ROOT / "docs" / "recovery.md").read_text(encoding="utf-8")
        section = guide.split("## When recovery fails", 1)[1].split("\n## ", 1)[0]
        for warning, named in STOPS.items():
            with self.subTest(warning=warning):
                self.assertIn(named, section)

    def test_each_stop_is_found_by_its_line_or_by_the_function_that_reports_it(self) -> None:
        source = (
            "import sys\n"
            "def first(item):\n"
            "    _warn(f'Cannot read {item}')\n"
            "    return False\n"
            "def second():\n"
            "    print('  ERROR: Staging is a link', file=sys.stderr)\n"
            "    return False\n"
            "def third():\n"
            "    try:\n"
            "        step()\n"
            "    except DeployError as exc:\n"
            "        _report(exc)\n"
            "        return False\n"
            "def fourth():\n"
            "    def inner():\n"
            "        _report(error)\n"
            "        return False\n"
            "    print('Recovering')\n"
            "    _warn('Carrying on')\n"
            "    return True\n"
            "def fifth(run):\n"
            "    if ready:\n"
            "        return True\n"
            "    else:\n"
            "        print(f'  ERROR: Run {run} failed', file=sys.stderr)\n"
            "        print(f'  {see_recovery()}', file=sys.stderr)\n"
            "        _print_manual_steps(run)\n"
            "        return False\n"
            "def sixth():\n"
            "    _warn('Before the assignment')\n"
            "    done = False\n"
            "    return False\n"
        )
        self.assertEqual(
            recovery_stops(source),
            {
                "Cannot read {}",
                "ERROR: Staging is a link",
                "_report in third",
                "_report in inner",
                "ERROR: Run {} failed",
            },
        )

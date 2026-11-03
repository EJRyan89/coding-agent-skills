"""One rule settles a committed run's transient backup, whether the run's own finalization or a later recovery meets it.

Each row puts the same backup on disk and runs it through pipeline._finalize and through recovery's _finish_backup,
which must leave the same tree and give the same warnings. The last test holds that "When recovery fails" in
docs/recovery.md names every stop recovery makes.
"""

from __future__ import annotations

import ast
import contextlib
import errno
import itertools
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
from deployer.names import transient_name

RUN = "20260101-000000-sett"
PREVIOUS = "previous alpha\n"
CHANGED = "changed after the plan\n"


@dataclass(frozen=True)
class Row:
    """A backup's state when the run settles it, and what settling it must leave."""

    retain: bool
    changed: bool = False
    # What goes wrong while settling it: "stat" for a hash that cannot be read, "occupied" for something already at
    # the copy's place under .backups, "remove" for a deletion that fails.
    fault: str = ""
    # Where the copy ends: "kept" under .backups, "beside" its item, or "deleted".
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
        retain=False, fault="remove", ends="beside", warnings=("alpha's previous copy could not be deleted",)
    ),
}


class SettlementTests(DeployerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.transient = self.skills_dir / transient_name("alpha")
        self.destination = self.skills_dir / ".backups" / RUN / "alpha"

    def backup(self, row: Row) -> dict[str, Any]:
        """The row's transient backup on disk beside the copy the run installed, and the entry the run journaled for
        it."""
        self.write(self.skills_dir / "alpha" / "SKILL.md", "installed alpha\n")
        self.write(self.transient / "SKILL.md", PREVIOUS)
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
        real_remove = fsops.remove

        def remove(path: Path) -> None:
            if path == self.transient:
                raise PermissionError(errno.EACCES, "Access is denied", str(path))
            real_remove(path)

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(journal, "_warn", side_effect=warnings.append))
            if row.fault == "stat":
                failure = PermissionError(errno.EACCES, "Access is denied")
                stack.enter_context(mock.patch.object(journal, "backup_unchanged", side_effect=failure))
            if row.fault == "remove":
                stack.enter_context(mock.patch.object(fsops, "remove", side_effect=remove))
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
                settle(entry)
            if self.transient.exists():
                ends = "beside"
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


# Each stop recovery makes, by the text of the WARNING it gives before it answers False, mapped to the words "When
# recovery fails" in docs/recovery.md names it by. A stop added without its row fails the test below.
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
    "_report": "the `.backups` path a rollback moves a kept copy back from is unusable",
}


def _text(node: ast.expr) -> str:
    """A warning's text with each value it formats in as {}."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    assert isinstance(node, ast.JoinedStr), ast.dump(node)
    return "".join(_text(part) if isinstance(part, ast.Constant) else "{}" for part in node.values)


def recovery_stops() -> set[str]:
    """Each _warn or _report in journal.py that the statement after it answers False to."""
    tree = ast.parse((REPOSITORY_ROOT / "deployer" / "journal.py").read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for statement, after in itertools.pairwise(body):
            answers_false = (
                isinstance(after, ast.Return) and isinstance(after.value, ast.Constant) and after.value.value is False
            )
            call = statement.value if isinstance(statement, ast.Expr) else None
            if not answers_false or not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            if call.func.id == "_warn":
                found.add(_text(call.args[0]))
            elif call.func.id == "_report":
                found.add("_report")
    return found


class RecoveryStopsTests(DeployerTestCase):
    def test_when_recovery_fails_names_every_stop_recovery_makes(self) -> None:
        self.assertEqual(set(STOPS), recovery_stops())
        guide = (REPOSITORY_ROOT / "docs" / "recovery.md").read_text(encoding="utf-8")
        section = guide.split("## When recovery fails", 1)[1].split("\n## ", 1)[0]
        stops = section.strip().split("\n\n", 1)[0]
        for warning, named in STOPS.items():
            with self.subTest(warning=warning):
                self.assertIn(named, stops)

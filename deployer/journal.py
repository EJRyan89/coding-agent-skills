"""Write-ahead journal for deployments and recovery of interrupted runs."""

from __future__ import annotations

import functools
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import fsops, hashing, manifest, platform_support
from .errors import DeployError, see_recovery
from .names import safe_name_problem
from .paths import Paths
from .render import ADAPTER_STAGING, AGENT_STAGING

# The journal records each change by root, which skills and shared assets share, in its on-disk format, so these name
# roots by the strings a journal holds rather than through the kinds table.
KINDS_ALLOWED = {
    "ROOT_PREFIXES": "a journal entry's paths begin with its root's prefix, part of the on-disk format",
    "LEGACY_STAGING": "a journal an earlier version wrote names these staging roots, and still recovers",
    "root_directory": "maps each root a journal entry may name to its directory",
}
ROOT_PREFIXES = {
    "claude": ("skills", "staging"),
    "agents": ("agents", f"staging/{ADAPTER_STAGING}"),
    "claude-agents": ("claude-agents", f"staging/{AGENT_STAGING}"),
}
# Earlier versions labeled staging roots that did not exist: "staging-wrappers" before runtime adapters were called
# adapters, then "staging-adapters" and "staging-claude-agents". A run interrupted under any of them still recovers.
LEGACY_STAGING = {"agents": ("staging-adapters", "staging-wrappers"), "claude-agents": ("staging-claude-agents",)}


# The skills root, which an entry for it leaves unrecorded, as every entry did before other roots were journaled.
DEFAULT_ROOT = "claude"


def entry_root(entry: dict[str, Any]) -> str:
    """The root an entry names, or the skills root for one that names none."""
    return entry.get("root", DEFAULT_ROOT)


def root_directory(paths: Paths, root: str) -> Path:
    return {"claude": paths.dest_dir, "agents": paths.adapter_dest_dir, "claude-agents": paths.agent_dest_dir}[root]


@dataclass
class Journal:
    path: Path
    run_id: str
    entries: list[dict[str, Any]] = field(default_factory=list)

    def create(self) -> None:
        fsops.make_directories(self.path.parent)
        fsops.write_file(self.path, b"")

    def write(self, entry: dict[str, Any]) -> None:
        fsops.append_line(self.path, json.dumps(entry, separators=(",", ":")))
        self.entries.append(entry)

    def backup(self, root: str, item: str, backup_hash: str, retain: bool) -> None:
        prefix = ROOT_PREFIXES[root][0]
        entry: dict[str, Any] = {"op": "backup"}
        if root != DEFAULT_ROOT:
            entry["root"] = root
        entry.update(
            {
                "item": item,
                "from": f"{prefix}/{item}",
                "to": f"{prefix}/{item}.deploying-bak",
                "retain": retain,
                "backup_hash": backup_hash,
            }
        )
        if retain:
            entry["backup_dest"] = f".backups/{self.run_id}/{item}"
        self.write(entry)

    def install(self, root: str, item: str, staged_hash: str) -> None:
        destination, staging = ROOT_PREFIXES[root]
        entry: dict[str, Any] = {"op": "install"}
        if root != DEFAULT_ROOT:
            entry["root"] = root
        entry.update(
            {
                "item": item,
                "from": f"{staging}/{item}",
                "to": f"{destination}/{item}",
                "staged_hash": staged_hash,
            }
        )
        self.write(entry)

    def preserve(self, root: str, item: str) -> None:
        prefix = ROOT_PREFIXES[root][0]
        self.write(
            {
                "op": "preserve",
                "root": root,
                "item": item,
                "from": f"{prefix}/{item}.deploying-bak",
                "to": f".backups/{self.run_id}/{item}",
            }
        )


def valid_entry(entry: Any, run_id: str) -> bool:
    if not isinstance(entry, dict):
        return False
    root = entry_root(entry)
    if root not in ROOT_PREFIXES:
        return False
    destination, staging = ROOT_PREFIXES[root]
    op, item = entry.get("op"), entry.get("item")
    if not isinstance(op, str) or not isinstance(item, str) or not op or not item:
        return False
    if safe_name_problem(item, "journal item") is not None:
        return False
    if op == "backup":
        retain = entry.get("retain")
        if not isinstance(retain, bool):
            return False
        if not hashing.HASH_PATTERN.fullmatch(str(entry.get("backup_hash", ""))):
            return False
        if retain and entry.get("backup_dest") != f".backups/{run_id}/{item}":
            return False
        return entry.get("from") == f"{destination}/{item}" and entry.get("to") == f"{destination}/{item}.deploying-bak"
    if op == "install":
        if not hashing.HASH_PATTERN.fullmatch(str(entry.get("staged_hash", ""))):
            return False
        stagings = (staging, *LEGACY_STAGING.get(root, ()))
        return (
            entry.get("from") in {f"{label}/{item}" for label in stagings}
            and entry.get("to") == f"{destination}/{item}"
        )
    if op == "preserve":
        return (
            entry.get("from") == f"{destination}/{item}.deploying-bak"
            and entry.get("to") == f".backups/{run_id}/{item}"
        )
    return False


def prepare_backup_destination(paths: Paths, run_id: str, item: str, root: str) -> Path:
    """Create the only directory a retained backup may move to, rejecting links and escapes."""
    base = root_directory(paths, root)
    for name, context in ((run_id, "journal run ID"), (item, "backup item")):
        problem = safe_name_problem(name, context)
        if problem is not None:
            raise DeployError(problem)
    if not base.is_dir():
        raise DeployError(f"ERROR: Deployment destination is missing: {platform_support.normalize(base)}")
    backups = base / ".backups"
    run_backups = backups / run_id
    components = (base, backups, run_backups)

    def reject_links(verb: str) -> None:
        for component in components:
            if platform_support.is_link(component) or platform_support.is_reparse_point(component):
                raise DeployError(
                    f"ERROR: Backup path {verb} a symlink or junction: {platform_support.normalize(component)}"
                )

    reject_links("contains")
    fsops.make_directories(run_backups)
    reject_links("became")
    base_canonical = platform_support.canonical_directory(base).casefold()
    parent_canonical = platform_support.canonical_directory(run_backups)
    if not parent_canonical.casefold().startswith(base_canonical + "/"):
        raise DeployError(f"ERROR: Backup path resolves outside deployment root: {parent_canonical}")
    return run_backups / item


def _warn(message: str) -> None:
    print(f"  WARNING: {message}", file=sys.stderr)


UNSAFE_HASH = "unsafe"


def _hash_existing(path: Path) -> str | None:
    """Hash a recovery target; content containing a link never matches any journaled hash."""
    if not os.path.lexists(path):
        return None
    try:
        return hashing.hash_path(path)
    except DeployError as exc:
        print(*(f"  {line}" for line in exc.lines), sep="\n", file=sys.stderr)
        return UNSAFE_HASH


def _report(error: DeployError) -> None:
    print(*error.lines, sep="\n", file=sys.stderr)


def _os_failure(run_id: str, path: Path, error: OSError) -> DeployError:
    """An OSError during recovery, such as a file another program holds open, named by its run and its path."""
    where = os.fsdecode(error.filename) if error.filename is not None else path
    reason = error.strerror or str(error)
    return DeployError(f"  ERROR: Recovery of run {run_id} failed at {platform_support.normalize(where)}: {reason}")


def _guarded(run_id: str, path: Path, step: Callable[[], bool]) -> bool:
    """Run one recovery step; an OSError fails only that step, so the steps after it still run.

    Each step checks the tree before it changes anything, so replaying a run whose earlier attempt stopped partway
    reaches the same result; the failed step is left for the next run or for the user.
    """
    try:
        return step()
    except OSError as error:
        _report(_os_failure(run_id, path, error))
        return False


def _item_path(paths: Paths, entry: dict[str, Any], suffix: str = "") -> Path:
    return root_directory(paths, entry_root(entry)) / f"{entry['item']}{suffix}"


def _verify_install(paths: Paths, entry: dict[str, Any]) -> bool:
    target = _item_path(paths, entry)
    actual = _hash_existing(target)
    if actual is None and not os.path.lexists(target):
        _warn(f"Installed destination missing for {entry['item']}")
        return False
    if actual is not None and actual != entry["staged_hash"]:
        _warn(f"Installed {entry['item']} hash mismatch (expected {entry['staged_hash']}, got {actual})")
        return False
    return True


def backup_unchanged(paths: Paths, entry: dict[str, Any]) -> bool:
    """Whether a backup entry's transient backup is gone or still matches the hash the plan recorded for it.

    Only then may a backup the run does not keep be deleted: one that differs was changed after the plan read it, such
    as by an edit just before the move, and is kept as a modified item would have been.
    """
    return _hash_existing(_item_path(paths, entry, ".deploying-bak")) in (None, entry["backup_hash"])


def _finish_backup(paths: Paths, run_id: str, entry: dict[str, Any]) -> bool:
    """Delete an unchanged transient backup the committed run did not keep, or move it to permanent storage."""
    root = entry_root(entry)
    item = entry["item"]
    transient = _item_path(paths, entry, ".deploying-bak")
    kept_at = f".backups/{run_id}/{item}"
    if not entry["retain"]:
        if backup_unchanged(paths, entry):
            fsops.remove(transient)
            return True
        _warn(
            f"{item} changed after run {run_id} planned it, so its previous copy is kept at {kept_at} "
            "instead of being deleted."
        )
    try:
        destination = prepare_backup_destination(paths, run_id, item, root)
    except DeployError as exc:
        _report(exc)
        return False
    if os.path.lexists(transient):
        if os.path.lexists(destination):
            _warn(f"Both transient and permanent backups exist for {item}")
            return False
        if entry["retain"] and _hash_existing(transient) != entry["backup_hash"]:
            _warn(f"Backup hash mismatch for {item}")
            return False
        fsops.move(transient, destination)
        print(f"  Preserved backup: {item} -> {kept_at}")
        return True
    # Only a kept backup gets here: one the run did not keep and that is gone returned above.
    if os.path.lexists(destination):
        if _hash_existing(destination) != entry["backup_hash"]:
            _warn(f"Permanent backup hash mismatch for {item} at {kept_at}")
            return False
        return True
    _warn(f"Cannot find backup for {item}")
    return False


def _complete_committed(paths: Paths, run_id: str, entries: list[dict[str, Any]]) -> bool:
    print(f"Recovering committed run {run_id} (completing finalization)...")
    reconciled = True
    for entry in entries:
        if entry["op"] == "install":
            step = functools.partial(_verify_install, paths, entry)
            reconciled = _guarded(run_id, _item_path(paths, entry), step) and reconciled
    if not reconciled:
        print("  ERROR: Install verification failed. Retaining all backups and journal.", file=sys.stderr)
        return False
    for entry in entries:
        if entry["op"] == "backup":
            step = functools.partial(_finish_backup, paths, run_id, entry)
            reconciled = _guarded(run_id, _item_path(paths, entry, ".deploying-bak"), step) and reconciled
    return reconciled


def _undo_install(paths: Paths, entry: dict[str, Any], backup_hash: str | None) -> bool:
    """Remove what the run installed; backup_hash is the hash of the copy it replaced, if it replaced one."""
    target = _item_path(paths, entry)
    actual = _hash_existing(target)
    if actual is None:
        if os.path.lexists(target):
            _warn(f"Unexpected state for {entry['item']} during rollback")
            return False
        return True
    if actual == entry["staged_hash"]:
        fsops.remove(target)
        return True
    if actual == backup_hash and not os.path.lexists(_item_path(paths, entry, ".deploying-bak")):
        # An earlier rollback that stopped partway already put the replaced copy back.
        return True
    _warn(f"Cannot rollback install of {entry['item']} (hash mismatch)")
    return False


def _restore_backup(paths: Paths, entry: dict[str, Any]) -> bool:
    item = entry["item"]
    target = _item_path(paths, entry)
    transient = _item_path(paths, entry, ".deploying-bak")
    transient_exists = os.path.lexists(transient)
    target_exists = os.path.lexists(target)
    if transient_exists and not target_exists:
        if _hash_existing(transient) != entry["backup_hash"]:
            _warn(f"Backup hash mismatch for {item} during rollback")
            return False
        try:
            fsops.move(transient, target)
        except OSError:
            _warn(f"Failed to restore backup for {item}")
            return False
        return True
    if transient_exists and target_exists:
        _warn(f"Both {item} and {item}.deploying-bak exist during rollback")
        return False
    if not transient_exists and not target_exists:
        _warn(f"Both {item} and backup are missing during rollback")
        return False
    return True


def _undo_preserve(paths: Paths, run_id: str, entry: dict[str, Any]) -> bool:
    """Move a backup the run had already preserved back beside its item, so the backup entry can restore it."""
    try:
        destination = prepare_backup_destination(paths, run_id, entry["item"], entry_root(entry))
    except DeployError as exc:
        _report(exc)
        return False
    transient = _item_path(paths, entry, ".deploying-bak")
    if os.path.lexists(destination) and not os.path.lexists(transient):
        fsops.move(destination, transient)
    return True


def _roll_back(paths: Paths, run_id: str, entries: list[dict[str, Any]]) -> bool:
    print(f"Recovering uncommitted run {run_id} (rolling back)...")
    reconciled = True
    replaced = {
        (entry_root(entry), entry["item"]): entry["backup_hash"] for entry in entries if entry["op"] == "backup"
    }
    for entry in reversed(entries):
        step: Callable[[], bool]
        if entry["op"] == "install":
            backup_hash = replaced.get((entry_root(entry), entry["item"]))
            step = functools.partial(_undo_install, paths, entry, backup_hash)
        elif entry["op"] == "backup":
            step = functools.partial(_restore_backup, paths, entry)
        else:
            step = functools.partial(_undo_preserve, paths, run_id, entry)
        reconciled = _guarded(run_id, _item_path(paths, entry), step) and reconciled
    return reconciled


def _print_manual_steps(run_dir: Path, run_id: str, committed: bool) -> None:
    """Say what a reconciled run looks like, since only the user can decide what an unexpected file is."""
    keep = "keep the copies it installed" if committed else "put back the copies it replaced"
    print(
        f"  Run {run_id} was {'' if committed else 'not '}committed to the manifest, so {keep}. "
        "A replaced copy sits beside its item as <name>.deploying-bak; a retained backup is under "
        f".backups/{run_id}/ in the same root.",
        file=sys.stderr,
    )
    print(
        f"  When no .deploying-bak remains, delete {platform_support.normalize(run_dir)} and rerun with --dry-run. "
        f"{see_recovery('When recovery fails')}",
        file=sys.stderr,
    )


def _committed_run_id(paths: Paths) -> str | None:
    """The run the manifest records as committed, "" when none, or None when the manifest cannot be trusted.

    It is read through the validated loader: a manifest the deployment would refuse decides no recovery either.
    """
    try:
        return manifest.load(paths.manifest_file).last_run_id()
    except DeployError:
        return None


@dataclass
class Entries:
    """A journal as recovery reads it.

    entries are the valid lines up to the first malformed one, which is malformed (None when every line is valid).
    torn is a last line with no newline, set aside when every line before it is valid. Journal.write appends each
    entry in full, newline included, before the change it records, so a line without its newline was cut off by the
    interruption before that change began, and dropping it loses nothing. Anywhere else, a bad line stops recovery.
    """

    entries: list[dict[str, Any]]
    malformed: str | None = None
    torn: str | None = None


def _read_entries(journal_file: Path, run_id: str) -> Entries:
    lines = journal_file.read_text(encoding="utf-8", errors="replace").split("\n")
    torn = lines.pop()  # the text after the last newline, empty when the journal ends with one
    entries: list[dict[str, Any]] = []
    for line in lines:
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            entry = None
        if not valid_entry(entry, run_id):
            return Entries(entries, malformed=line)
        entries.append(entry)
    return Entries(entries, torn=torn or None)


def _recover_run(paths: Paths, run_dir: Path) -> bool:
    """Reconcile one interrupted run; return False when it needs manual inspection."""
    if platform_support.is_link(run_dir) or platform_support.is_reparse_point(run_dir):
        print(
            "  ERROR: Staging run is a symlink or junction and was "
            f"not replayed: {platform_support.normalize(run_dir)}",
            file=sys.stderr,
        )
        return False
    journal_file = run_dir / "journal.jsonl"
    if not journal_file.is_file():
        fsops.remove(run_dir)
        return True
    run_id = run_dir.name
    committed = _committed_run_id(paths)
    if committed is None:
        print("  ERROR: Cannot read manifest during recovery.", file=sys.stderr)
        return False
    read = _read_entries(journal_file, run_id)
    entries, malformed = read.entries, read.malformed
    if read.torn is not None:
        _warn(
            f"Dropped the unterminated last line of run {run_id}'s journal; the run stopped while writing it, "
            f"before the change it records: {read.torn}"
        )
    if malformed is not None:
        print(f"  ERROR: Malformed journal entry in run {run_id}: {malformed}", file=sys.stderr)
        print(
            f"  ERROR: Journal validation failed. Retaining {platform_support.normalize(journal_file)} "
            "for manual inspection.",
            file=sys.stderr,
        )
        _print_manual_steps(run_dir, run_id, committed == run_id)
        return False
    if not entries:
        fsops.remove(run_dir)
        return True
    if committed == run_id:
        reconciled = _complete_committed(paths, run_id, entries)
    else:
        reconciled = _roll_back(paths, run_id, entries)
    if not reconciled:
        print(
            "  ERROR: Recovery has unreconciled entries. Journal retained at: "
            f"{platform_support.normalize(journal_file)}",
            file=sys.stderr,
        )
        _print_manual_steps(run_dir, run_id, committed == run_id)
        return False
    fsops.remove(run_dir)
    print("  Recovery complete.")
    return True


def pending_runs(paths: Paths) -> list[tuple[str, bool]]:
    """Each interrupted run the next deployment will reconcile, with whether it can do so without the user.

    Changes nothing, for --dry-run. A run without journal entries is not listed, since recovery only deletes it.
    """
    if not paths.staging_root.is_dir():
        return []
    try:
        run_dirs = sorted(path for path in paths.staging_root.iterdir() if path.is_dir())
    except OSError as error:
        raise DeployError(
            f"ERROR: Cannot list the staging runs in {platform_support.normalize(paths.staging_root)}: "
            f"{error.strerror or error}",
            see_recovery("When recovery fails"),
        ) from error
    pending: list[tuple[str, bool]] = []
    for run_dir in run_dirs:
        if platform_support.is_link(run_dir) or platform_support.is_reparse_point(run_dir):
            pending.append((run_dir.name, False))
            continue
        journal_file = run_dir / "journal.jsonl"
        if not journal_file.is_file():
            continue
        try:
            read = _read_entries(journal_file, run_dir.name)
        except OSError:
            pending.append((run_dir.name, False))
            continue
        if read.malformed is not None:
            pending.append((run_dir.name, False))
        elif read.entries:
            pending.append((run_dir.name, True))
    return pending


def recover_incomplete(paths: Paths) -> bool:
    """Reconcile every interrupted run; return False when any run needs manual inspection.

    An OSError, such as a file another program holds open, fails the run it hit, and recovery moves on to the next run,
    so one held file neither hides the state of the others nor prints a traceback in place of the guidance.
    """
    if not paths.staging_root.is_dir():
        return True
    try:
        run_dirs = sorted(path for path in paths.staging_root.iterdir() if path.is_dir())
    except OSError as error:
        print(
            f"  ERROR: Cannot list the staging runs in {platform_support.normalize(paths.staging_root)}: "
            f"{error.strerror or error}",
            file=sys.stderr,
        )
        print(f"  {see_recovery('When recovery fails')}", file=sys.stderr)
        return False
    succeeded = True
    for run_dir in run_dirs:
        try:
            recovered = _recover_run(paths, run_dir)
        except OSError as error:
            _report(_os_failure(run_dir.name, run_dir, error))
            _print_manual_steps(run_dir, run_dir.name, _committed_run_id(paths) == run_dir.name)
            recovered = False
        succeeded = recovered and succeeded
    return succeeded

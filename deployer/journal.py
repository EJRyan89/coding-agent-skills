"""Write-ahead journal for deployments and recovery of interrupted runs."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import fsops, hashing, platform_support
from .errors import DeployError, see_recovery
from .names import safe_name_problem
from .paths import Paths

ROOT_PREFIXES = {
    "claude": ("skills", "staging"),
    "agents": ("agents", "staging-adapters"),
    "claude-agents": ("claude-agents", "staging-claude-agents"),
}
# Before runtime adapters were called adapters, the journal labeled their staging root "staging-wrappers". A run
# interrupted under that version still recovers.
LEGACY_STAGING = {"agents": ("staging-wrappers",)}


def root_directory(paths: Paths, root: str) -> Path:
    return {"claude": paths.dest_dir, "agents": paths.adapter_dest_dir, "claude-agents": paths.agent_dest_dir}[root]


@dataclass
class Journal:
    path: Path
    run_id: str
    entries: list[dict[str, Any]] = field(default_factory=list)

    def create(self) -> None:
        fsops.make_directories(self.path.parent)
        self.path.touch()

    def write(self, entry: dict[str, Any]) -> None:
        fsops.append_line(self.path, json.dumps(entry, separators=(",", ":")))
        self.entries.append(entry)

    def backup(self, root: str, item: str, backup_hash: str, retain: bool) -> None:
        prefix = ROOT_PREFIXES[root][0]
        entry: dict[str, Any] = {"op": "backup"}
        if root != "claude":
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
        if root != "claude":
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
    root = entry.get("root", "claude")
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
        return entry.get("from") in {f"{label}/{item}" for label in stagings} and entry.get("to") == f"{destination}/{item}"
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


def _complete_committed(paths: Paths, run_id: str, entries: list[dict[str, Any]]) -> bool:
    print(f"Recovering committed run {run_id} (completing finalization)...")
    reconciled = True
    for entry in entries:
        if entry["op"] != "install":
            continue
        target = root_directory(paths, entry.get("root", "claude")) / entry["item"]
        actual = _hash_existing(target)
        if actual is None and not os.path.lexists(target):
            _warn(f"Installed destination missing for {entry['item']}")
            reconciled = False
        elif actual is not None and actual != entry["staged_hash"]:
            _warn(f"Installed {entry['item']} hash mismatch (expected {entry['staged_hash']}, got {actual})")
            reconciled = False
    if not reconciled:
        print("  ERROR: Install verification failed. Retaining all backups and journal.", file=sys.stderr)
        return False
    for entry in entries:
        if entry["op"] != "backup":
            continue
        root = entry.get("root", "claude")
        item = entry["item"]
        transient = root_directory(paths, root) / f"{item}.deploying-bak"
        if not entry["retain"]:
            fsops.remove(transient)
            continue
        try:
            destination = prepare_backup_destination(paths, run_id, item, root)
        except DeployError as exc:
            print(*exc.lines, sep="\n", file=sys.stderr)
            reconciled = False
            continue
        if os.path.lexists(transient):
            if os.path.lexists(destination):
                _warn(f"Both transient and permanent backups exist for {item}")
                reconciled = False
            elif _hash_existing(transient) != entry["backup_hash"]:
                _warn(f"Backup hash mismatch for {item}")
                reconciled = False
            else:
                try:
                    fsops.move(transient, destination)
                except OSError:
                    reconciled = False
                    continue
                print(f"  Preserved backup: {item} -> {entry['backup_dest']}")
        elif os.path.lexists(destination):
            if _hash_existing(destination) != entry["backup_hash"]:
                _warn(f"Permanent backup hash mismatch for {item} at {entry['backup_dest']}")
                reconciled = False
        else:
            _warn(f"Cannot find backup for {item}")
            reconciled = False
    return reconciled


def _roll_back(paths: Paths, run_id: str, entries: list[dict[str, Any]]) -> bool:
    print(f"Recovering uncommitted run {run_id} (rolling back)...")
    reconciled = True
    for entry in reversed(entries):
        root = entry.get("root", "claude")
        item = entry["item"]
        target = root_directory(paths, root) / item
        if entry["op"] == "install":
            actual = _hash_existing(target)
            if actual is None:
                if os.path.lexists(target):
                    _warn(f"Unexpected state for {item} during rollback")
                    reconciled = False
            elif actual == entry["staged_hash"]:
                fsops.remove(target)
            else:
                _warn(f"Cannot rollback install of {item} (hash mismatch)")
                reconciled = False
        elif entry["op"] == "backup":
            transient = root_directory(paths, root) / f"{item}.deploying-bak"
            transient_exists = os.path.lexists(transient)
            target_exists = os.path.lexists(target)
            if transient_exists and not target_exists:
                if _hash_existing(transient) != entry["backup_hash"]:
                    _warn(f"Backup hash mismatch for {item} during rollback")
                    reconciled = False
                    continue
                try:
                    fsops.move(transient, target)
                except OSError:
                    _warn(f"Failed to restore backup for {item}")
                    reconciled = False
            elif transient_exists and target_exists:
                _warn(f"Both {item} and {item}.deploying-bak exist during rollback")
                reconciled = False
            elif not transient_exists and not target_exists:
                _warn(f"Both {item} and backup are missing during rollback")
                reconciled = False
        elif entry["op"] == "preserve":
            try:
                destination = prepare_backup_destination(paths, run_id, item, root)
            except DeployError as exc:
                print(*exc.lines, sep="\n", file=sys.stderr)
                reconciled = False
                continue
            transient = root_directory(paths, root) / f"{item}.deploying-bak"
            if os.path.lexists(destination) and not os.path.lexists(transient):
                try:
                    fsops.move(destination, transient)
                except OSError:
                    reconciled = False
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
    if not paths.manifest_file.is_file():
        return ""
    try:
        data = json.loads(paths.manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    value = data.get("last_run_id", "") if isinstance(data, dict) else ""
    return value if isinstance(value, str) else ""


def recover_incomplete(paths: Paths) -> bool:
    """Reconcile every interrupted run; return False when any run needs manual inspection."""
    if not paths.staging_root.is_dir():
        return True
    succeeded = True
    for run_dir in sorted(path for path in paths.staging_root.iterdir() if path.is_dir()):
        if platform_support.is_link(run_dir) or platform_support.is_reparse_point(run_dir):
            print(
                f"  ERROR: Staging run is a symlink or junction and was not replayed: {platform_support.normalize(run_dir)}",
                file=sys.stderr,
            )
            succeeded = False
            continue
        journal_file = run_dir / "journal.jsonl"
        if not journal_file.is_file():
            fsops.remove(run_dir)
            continue
        run_id = run_dir.name
        committed = _committed_run_id(paths)
        if committed is None:
            print("  ERROR: Cannot read manifest during recovery.", file=sys.stderr)
            succeeded = False
            continue
        entries: list[dict[str, Any]] = []
        malformed = None
        for line in journal_file.read_text(encoding="utf-8", errors="replace").split("\n"):
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                entry = None
            if not valid_entry(entry, run_id):
                malformed = line
                break
            entries.append(entry)
        if malformed is not None:
            print(f"  ERROR: Malformed journal entry in run {run_id}: {malformed}", file=sys.stderr)
            print(
                f"  ERROR: Journal validation failed. Retaining {platform_support.normalize(journal_file)} "
                "for manual inspection.",
                file=sys.stderr,
            )
            _print_manual_steps(run_dir, run_id, committed == run_id)
            succeeded = False
            continue
        if not entries:
            fsops.remove(run_dir)
            continue
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
            succeeded = False
            continue
        fsops.remove(run_dir)
        print("  Recovery complete.")
    return succeeded

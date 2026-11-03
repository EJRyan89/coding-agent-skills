"""Write-ahead journal for deployments and recovery of interrupted runs."""

from __future__ import annotations

import contextlib
import functools
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, TypeGuard, assert_never

import file_presence
import json_writer
from path_containment import posix_within

from . import fsops, hashing, manifest, platform_support, state_files
from .errors import DeployError, see_recovery
from .names import BACKUP_SUFFIX, safe_name_problem, transient_name
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

# Each line of a journal is one of these entries, told apart by its op. "from" is a keyword, so each is declared in the
# functional form. An entry for the skills root may leave root out, and recovery reads a preserve entry without one,
# though the deployer always writes it.
BackupEntry = TypedDict(
    "BackupEntry",
    {
        "op": Literal["backup"],
        "root": NotRequired[str],
        "item": str,
        "from": str,
        "to": str,
        "retain": bool,
        "backup_hash": str,
        "backup_dest": NotRequired[str],
    },
)
InstallEntry = TypedDict(
    "InstallEntry",
    {"op": Literal["install"], "root": NotRequired[str], "item": str, "from": str, "to": str, "staged_hash": str},
)
PreserveEntry = TypedDict(
    "PreserveEntry", {"op": Literal["preserve"], "root": NotRequired[str], "item": str, "from": str, "to": str}
)
JournalEntry = BackupEntry | InstallEntry | PreserveEntry
# The op that tells the entries apart.
Op = Literal["backup", "install", "preserve"]


# The presence reads here that read a path they cannot stat or list as nothing there, each with why that is
# the answer it should give.
PRESENCE_ALLOWED = {
    "journaled_runs": "None is its answer for staging runs it cannot read, apart from [] for none, and the report then"
    " says nothing about what is installed",
    "recover_incomplete": "a staging root it cannot list is reported as an ERROR and recovery answers False, that a "
    "run needs inspection, never that none was interrupted",
}


def entry_root(entry: JournalEntry) -> str:
    """The root an entry names, or the skills root for one that names none."""
    return entry.get("root", DEFAULT_ROOT)


def root_directory(paths: Paths, root: str) -> Path:
    return {"claude": paths.dest_dir, "agents": paths.adapter_dest_dir, "claude-agents": paths.agent_dest_dir}[root]


@dataclass
class Journal:
    path: Path
    run_id: str
    entries: list[JournalEntry] = field(default_factory=list)

    def create(self) -> None:
        fsops.make_directories(self.path.parent)
        fsops.write_file(self.path, b"")

    def write(self, entry: JournalEntry) -> None:
        """Append an entry before the change it records. An entry the JSON writer refuses is a DeployError naming
        the journal, raised before that change, which the run's journaled recovery handles."""
        try:
            line = json_writer.dumps(entry, separators=(",", ":"))
        except json_writer.JsonWriteRefused as exc:
            raise DeployError(
                f"ERROR: Cannot write the journal {platform_support.normalize(self.path)}: {exc}"
            ) from exc
        fsops.append_line(self.path, line)
        self.entries.append(entry)

    def backup(self, root: str, item: str, backup_hash: str, retain: bool) -> None:
        prefix = ROOT_PREFIXES[root][0]
        entry: BackupEntry = {
            "op": "backup",
            "root": root,
            "item": item,
            "from": f"{prefix}/{item}",
            "to": f"{prefix}/{transient_name(item)}",
            "retain": retain,
            "backup_hash": backup_hash,
        }
        if root == DEFAULT_ROOT:
            del entry["root"]
        if retain:
            entry["backup_dest"] = f".backups/{self.run_id}/{item}"
        self.write(entry)

    def install(self, root: str, item: str, staged_hash: str) -> None:
        destination, staging = ROOT_PREFIXES[root]
        entry: InstallEntry = {
            "op": "install",
            "root": root,
            "item": item,
            "from": f"{staging}/{item}",
            "to": f"{destination}/{item}",
            "staged_hash": staged_hash,
        }
        if root == DEFAULT_ROOT:
            del entry["root"]
        self.write(entry)

    def preserve(self, root: str, item: str) -> None:
        prefix = ROOT_PREFIXES[root][0]
        self.write(
            {
                "op": "preserve",
                "root": root,
                "item": item,
                "from": f"{prefix}/{transient_name(item)}",
                "to": f".backups/{self.run_id}/{item}",
            }
        )


def valid_entry(entry: object, run_id: str) -> TypeGuard[JournalEntry]:
    if not isinstance(entry, dict):
        return False
    # Every field is read from the file, so its type is checked before a hashed lookup, which a list or an object fails.
    root = entry.get("root", DEFAULT_ROOT)
    if not isinstance(root, str) or root not in ROOT_PREFIXES:
        return False
    destination, staging = ROOT_PREFIXES[root]
    op, item = entry.get("op"), entry.get("item")
    if not isinstance(op, str) or not isinstance(item, str) or not op or not item:
        return False
    if safe_name_problem(item, "journal item") is not None:
        return False
    # The op is read from the file, so an op no entry has is a malformed entry, not a missing handler.
    match op:
        case "backup":
            retain = entry.get("retain")
            if not isinstance(retain, bool):
                return False
            if not hashing.HASH_PATTERN.fullmatch(str(entry.get("backup_hash", ""))):
                return False
            if retain and entry.get("backup_dest") != f".backups/{run_id}/{item}":
                return False
            return (
                entry.get("from") == f"{destination}/{item}"
                and entry.get("to") == f"{destination}/{transient_name(item)}"
            )
        case "install":
            if not hashing.HASH_PATTERN.fullmatch(str(entry.get("staged_hash", ""))):
                return False
            stagings = (staging, *LEGACY_STAGING.get(root, ()))
            origin = entry.get("from")
            return (
                isinstance(origin, str)
                and origin in {f"{label}/{item}" for label in stagings}
                and entry.get("to") == f"{destination}/{item}"
            )
        case "preserve":
            return (
                entry.get("from") == f"{destination}/{transient_name(item)}"
                and entry.get("to") == f".backups/{run_id}/{item}"
            )
        case _:
            return False


BACKUP_PATH_REMEDY = "Replace it with a real directory after verifying its contents, then retry."


def _reject_backup_links(components: tuple[Path, ...], verb: str) -> None:
    for component in components:
        if platform_support.is_link_or_junction(component):
            raise DeployError(
                f"ERROR: Backup path {verb} a symlink or junction: {platform_support.normalize(component)}",
                BACKUP_PATH_REMEDY,
            )


def check_backup_root(paths: Paths, root: str, run_id: str | None = None) -> None:
    """Refuse, creating nothing, a root's .backups that a retained backup could not move into: a symlink, a junction,
    or anything but a directory, and, given the run's ID, a .backups/<run-id> that is already there. A root or a
    .backups that does not exist yet passes, since the run creates it.

    The plan runs it for every root before the first change, so the deployment, and the dry run, refuse such a path
    while the home is unchanged, rather than after the manifest commit, when prepare_backup_destination meets it. Given
    the run's ID, as the deployment gives it under its lock before its first write, it also proves the run can create
    its own folder there; the dry run changes nothing, so it does not.
    """
    base = root_directory(paths, root)
    backups = base / ".backups"
    _reject_backup_links((base, backups), "contains")
    if file_presence.is_occupied(backups) and not file_presence.is_directory(backups, other_is_absent=True):
        raise DeployError(
            f"ERROR: Backup path is not a directory: {platform_support.normalize(backups)}", BACKUP_PATH_REMEDY
        )
    if run_id is None:
        return
    run_backups = backups / run_id
    if file_presence.is_occupied(run_backups):
        raise DeployError(
            f"ERROR: Permanent backup folder for this run already exists: {platform_support.normalize(run_backups)}",
            "Move it out of the deployment root after checking its contents, then retry.",
            see_recovery("Backups"),
        )
    if file_presence.is_directory(base, other_is_absent=True):
        _probe_backup_folder(backups, run_backups)


def _probe_backup_folder(backups: Path, run_backups: Path) -> None:
    """Create this run's backup folder, a file and a folder in it, as a kept file or folder would be, then remove what
    the probe made, .backups with it when the probe made that too, whether or not it succeeded, so a .backups the run
    cannot write, by its permissions or its owner, is refused before the first change and the home is left as found.

    What changes after the probe, before the run keeps a copy there, is met after the commit by keep_backup."""
    made = [run_backups] if file_presence.is_occupied(backups) else [run_backups, backups]
    try:
        fsops.make_directories(run_backups / PROBE_NAME)
        fsops.write_file(run_backups / f"{PROBE_NAME}.file", b"")
    except OSError as exc:
        raise _not_writable(backups, exc) from exc
    finally:
        for path in made:
            with contextlib.suppress(OSError):
                fsops.remove(path)


# The name of what the probe makes in this run's own backup folder, which no other run, and no item, shares.
PROBE_NAME = ".probe"


def _not_writable(backups: Path, error: OSError) -> DeployError:
    return DeployError(
        f"ERROR: Backup path is not writable: {platform_support.normalize(backups)}: {error.strerror or error}",
        "Give your account write access to it, or replace it with a directory you own, then retry.",
        see_recovery("Backups"),
    )


def check_backup_roots(paths: Paths, run_id: str | None = None) -> None:
    """check_backup_root for every root a run writes."""
    for root in ROOT_PREFIXES:
        check_backup_root(paths, root, run_id)


def prepare_backup_destination(paths: Paths, run_id: str, item: str, root: str) -> Path:
    """Create the only directory a retained backup may move to, rejecting links and escapes."""
    base = root_directory(paths, root)
    for name, context in ((run_id, "journal run ID"), (item, "backup item")):
        problem = safe_name_problem(name, context)
        if problem is not None:
            raise DeployError(problem)
    if not file_presence.is_directory(base, other_is_absent=True):
        raise DeployError(f"ERROR: Deployment destination is missing: {platform_support.normalize(base)}")
    check_backup_root(paths, root)
    backups = base / ".backups"
    run_backups = backups / run_id
    components = (base, backups, run_backups)

    def reject_links(verb: str) -> None:
        _reject_backup_links(components, verb)

    reject_links("contains")
    try:
        fsops.make_directories(run_backups)
    except OSError as exc:
        raise _not_writable(backups, exc) from exc
    reject_links("became")
    # Compared as the file system holding the root compares names: without regard to case only where it folds.
    base_canonical = platform_support.name_key(platform_support.canonical_directory(base), base)
    parent_canonical = platform_support.canonical_directory(run_backups)
    if not posix_within(platform_support.name_key(parent_canonical, base), base_canonical, strict=True):
        raise DeployError(f"ERROR: Backup path resolves outside deployment root: {parent_canonical}")
    return run_backups / item


def _warn(message: str) -> None:
    print(f"  WARNING: {message}", file=sys.stderr)


UNSAFE_HASH = "unsafe"


def _hash_existing(path: Path) -> str | None:
    """Hash a recovery target; content containing a link never matches any journaled hash. A target whose metadata
    cannot be read raises its OSError, which fails the run, rather than reading as absent."""
    if not file_presence.is_occupied(path):
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


def _item_path(paths: Paths, entry: JournalEntry, suffix: str = "") -> Path:
    return root_directory(paths, entry_root(entry)) / f"{entry['item']}{suffix}"


def _verify_install(paths: Paths, entry: InstallEntry) -> bool:
    target = _item_path(paths, entry)
    actual = _hash_existing(target)
    if actual is None and not file_presence.is_occupied(target):
        _warn(f"Installed destination missing for {entry['item']}")
        return False
    if actual is not None and actual != entry["staged_hash"]:
        _warn(f"Installed {entry['item']} hash mismatch (expected {entry['staged_hash']}, got {actual})")
        return False
    return True


def backup_unchanged(paths: Paths, entry: BackupEntry) -> bool:
    """Whether a backup entry's transient backup is gone or still matches the hash the plan recorded for it.

    Only then may a backup the run does not keep be deleted: one that differs was changed after the plan read it, such
    as by an edit just before the move, and is kept as a modified item would have been.
    """
    return _hash_existing(_item_path(paths, entry, BACKUP_SUFFIX)) in (None, entry["backup_hash"])


def backup_disposition(paths: Paths, entry: BackupEntry) -> tuple[Literal["delete", "keep", "gone"], str]:
    """What a committed run's finalization does with an entry's transient backup, with the reason a stat failed or "":
    delete one the run does not keep while it is unchanged, keep one that is still there, or nothing when it is gone.
    A copy whose stat fails is kept, never deleted or left unreported."""
    try:
        if not entry["retain"] and backup_unchanged(paths, entry):
            return "delete", ""
        return ("keep" if file_presence.is_occupied(_item_path(paths, entry, BACKUP_SUFFIX)) else "gone"), ""
    except OSError as error:
        return "keep", error.strerror or str(error)


def keep_backup(
    paths: Paths, run_id: str, entry: BackupEntry, before_move: Callable[[], None] | None = None
) -> str | None:
    """Move a committed run's transient backup into .backups/<run-id>/ in its root and return where it went, as that
    root names it, after running before_move, which journals the move. Should the folder not be made, the move fail, or
    something already be there, the copy stays beside its item, a warning says why, and None is returned.

    The run is committed, so its items stay, and no check before the commit covers what changes between it and this
    move. Every one of these ends the same way, after the commit as in recovery: the copy is never deleted, the run
    finishes, and the next deployment of the item refuses until the user moves the copy.
    """
    root, item = entry_root(entry), entry["item"]
    transient = _item_path(paths, entry, BACKUP_SUFFIX)
    kept_at = f".backups/{run_id}/{item}"
    try:
        destination = prepare_backup_destination(paths, run_id, item, root)
        if file_presence.is_occupied(destination):
            raise DeployError(f"ERROR: Something is already at {platform_support.normalize(destination)}")
        if before_move is not None:
            before_move()
        fsops.move(transient, destination)
    except (DeployError, OSError) as exc:
        reason = exc.lines[0].removeprefix("ERROR: ") if isinstance(exc, DeployError) else exc.strerror or str(exc)
        _warn(
            f"{item}'s previous copy is kept at {platform_support.normalize(transient)}, since it could not be moved "
            f"to {kept_at}: {reason}. Move it out of the root after checking it. {see_recovery('Backups')}"
        )
        return None
    return kept_at


def finish_backup(
    paths: Paths, run_id: str, entry: BackupEntry, before_move: Callable[[], None] | None = None
) -> tuple[Literal["delete", "keep", "gone"], str | None]:
    """Settle a committed run's transient backup, the one rule finalization and recovery share, and return what
    backup_disposition decided with where the copy was kept, or None when it was not moved under .backups.

    A copy the run does not keep is deleted while it matches the hash the plan recorded. Any other copy is moved to
    .backups/<run-id>/ by keep_backup, whatever its content: one the run keeps, as a forced replacement's, one changed
    after the plan read it, and one whose stat fails. Only once keep_backup says where the copy went does a warning say
    that a changed or unreadable copy was kept there; when it stays beside its item, keep_backup's warning says so.

    The run is committed, so a deletion that fails, such as of a file another program holds open, does not fail it:
    the copy stays beside its item, named with the reason, as one keep_backup cannot move does.
    """
    item = entry["item"]
    transient = _item_path(paths, entry, BACKUP_SUFFIX)
    disposition, unreadable = backup_disposition(paths, entry)
    if disposition == "delete":
        try:
            fsops.remove(transient)
        except OSError as error:
            _warn(
                f"{item}'s previous copy could not be deleted, so it, or what is left of it, stays at "
                f"{platform_support.normalize(transient)}: {error.strerror or error}. Delete it once nothing holds it "
                f"open. {see_recovery('Backups')}"
            )
        return disposition, None
    if disposition == "gone":
        return disposition, None
    kept_at = keep_backup(paths, run_id, entry, before_move)
    if kept_at is None:
        return disposition, None
    if unreadable:
        _warn(
            f"Could not tell whether {item}'s previous copy changed after run {run_id} planned it ({unreadable}), "
            f"so it was kept at {kept_at}."
        )
    elif not entry["retain"]:
        _warn(
            f"{item} changed after run {run_id} planned it, so its previous copy was kept at {kept_at} "
            "instead of being deleted."
        )
    return disposition, kept_at


def _finish_backup(paths: Paths, run_id: str, entry: BackupEntry) -> bool:
    """Settle a committed run's transient backup through finish_backup, or, for a kept one already gone, check the copy
    an earlier attempt moved to .backups/<run-id>/."""
    item = entry["item"]
    disposition, kept_at = finish_backup(paths, run_id, entry)
    if kept_at is not None:
        print(f"  Preserved backup: {item} -> {kept_at}")
    if disposition != "gone":
        return True
    # Only a kept backup gets here: one the run did not keep and that is gone has nothing left to delete. A kept copy is
    # kept whatever its content, so the one an earlier attempt moved is not judged by its hash: a retry after that move
    # completes as the attempt itself would have. What stands in its place is judged as the copy the run moved could
    # be, though: a real folder or file, as the item the run installed in its place is, reached through no link.
    base = root_directory(paths, entry_root(entry))
    destination = base / ".backups" / run_id / item
    kept_at = f".backups/{run_id}/{item}"
    if any(platform_support.is_link_or_junction(path) for path in (destination.parent.parent, destination.parent)):
        _warn(
            f"The kept backup of {item} at {kept_at} sits under a symlink or junction, so it is not the copy run "
            f"{run_id} kept"
        )
        return False
    if platform_support.is_link_or_junction(destination):
        _warn(
            f"The kept backup of {item} at {kept_at} is a symlink or junction, so it is not the copy run {run_id} kept"
        )
        return False
    found = _kind_of(destination)
    if found == "nothing":
        _warn(f"Cannot find backup for {item}")
        return False
    # A retained backup is written only by a replacement, which installs the item, so the installed copy is the kind
    # the run replaced.
    expected = _kind_of(_item_path(paths, entry))
    if found != expected:
        _warn(
            f"The kept backup of {item} at {kept_at} is {found}, not {expected} as {item} is, so it is not the copy "
            f"run {run_id} kept"
        )
        return False
    return True


def _kind_of(path: Path) -> Literal["a folder", "a file", "something else", "nothing"]:
    """What stands at path, following no link: the caller has refused one already."""
    if file_presence.is_directory(path, other_is_absent=True):
        return "a folder"
    try:
        return "a file" if file_presence.is_present(path) else "nothing"
    except file_presence.NotAFile:
        return "something else"


def _complete_committed(paths: Paths, run_id: str, entries: list[JournalEntry]) -> bool:
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
            reconciled = _guarded(run_id, _item_path(paths, entry, BACKUP_SUFFIX), step) and reconciled
    return reconciled


def _undo_install(paths: Paths, entry: InstallEntry, backup_hash: str | None) -> bool:
    """Remove what the run installed; backup_hash is the hash of the copy it replaced, if it replaced one."""
    target = _item_path(paths, entry)
    actual = _hash_existing(target)
    if actual is None:
        if file_presence.is_occupied(target):
            _warn(f"Unexpected state for {entry['item']} during rollback")
            return False
        return True
    if actual == entry["staged_hash"]:
        fsops.remove(target)
        return True
    if actual == backup_hash and not file_presence.is_occupied(_item_path(paths, entry, BACKUP_SUFFIX)):
        # An earlier rollback that stopped partway already put the replaced copy back.
        return True
    _warn(f"Cannot rollback install of {entry['item']} (hash mismatch)")
    return False


def _restore_backup(paths: Paths, entry: BackupEntry) -> bool:
    item = entry["item"]
    target = _item_path(paths, entry)
    transient = _item_path(paths, entry, BACKUP_SUFFIX)
    transient_exists = file_presence.is_occupied(transient)
    target_exists = file_presence.is_occupied(target)
    if transient_exists and not target_exists:
        if _hash_existing(transient) != entry["backup_hash"]:
            _warn(f"Backup hash mismatch for {item} during rollback")
            return False
        # A move that fails, such as of a backup another program holds open, reaches _guarded, which names the path.
        fsops.move(transient, target)
        return True
    if transient_exists and target_exists:
        _warn(f"Both {item} and {transient_name(item)} exist during rollback")
        return False
    if not transient_exists and not target_exists:
        _warn(f"Both {item} and backup are missing during rollback")
        return False
    return True


def _undo_preserve(paths: Paths, run_id: str, entry: PreserveEntry) -> bool:
    """Move a backup the run had already preserved back beside its item, so the backup entry can restore it."""
    try:
        destination = prepare_backup_destination(paths, run_id, entry["item"], entry_root(entry))
    except DeployError as exc:
        _report(exc)
        return False
    transient = _item_path(paths, entry, BACKUP_SUFFIX)
    if file_presence.is_occupied(destination) and not file_presence.is_occupied(transient):
        fsops.move(destination, transient)
    return True


def _roll_back(paths: Paths, run_id: str, entries: list[JournalEntry]) -> bool:
    print(f"Recovering uncommitted run {run_id} (rolling back)...")
    reconciled = True
    replaced = {
        (entry_root(entry), entry["item"]): entry["backup_hash"] for entry in entries if entry["op"] == "backup"
    }
    for entry in reversed(entries):
        step: Callable[[], bool]
        match entry["op"]:
            case "install":
                backup_hash = replaced.get((entry_root(entry), entry["item"]))
                step = functools.partial(_undo_install, paths, entry, backup_hash)
            case "backup":
                step = functools.partial(_restore_backup, paths, entry)
            case "preserve":
                step = functools.partial(_undo_preserve, paths, run_id, entry)
            case _:
                assert_never(entry["op"])
        reconciled = _guarded(run_id, _item_path(paths, entry), step) and reconciled
    return reconciled


def _print_manual_steps(run_dir: Path, run_id: str, committed: bool) -> None:
    """Say what a reconciled run looks like, since only the user can decide what an unexpected file is."""
    keep = "keep the copies it installed" if committed else "put back the copies it replaced"
    print(
        f"  Run {run_id} was {'' if committed else 'not '}committed to the manifest, so {keep}. "
        f"A replaced copy sits beside its item as {transient_name('<name>')}; a retained backup is under "
        f".backups/{run_id}/ in the same root.",
        file=sys.stderr,
    )
    print(
        f"  When no {BACKUP_SUFFIX} remains, delete {platform_support.normalize(run_dir)} and rerun with --dry-run. "
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

    entries: list[JournalEntry]
    malformed: str | None = None
    torn: str | None = None


def _read_entries(journal_file: Path, run_id: str) -> Entries:
    lines = journal_file.read_text(encoding="utf-8", errors="replace").split("\n")
    torn = lines.pop()  # the text after the last newline, empty when the journal ends with one
    entries: list[JournalEntry] = []
    for line in lines:
        if not line:
            continue
        entry = state_files.parse_json(line)
        if not valid_entry(entry, run_id):
            return Entries(entries, malformed=line)
        entries.append(entry)
    return Entries(entries, torn=torn or None)


def clear_run(run_dir: Path) -> None:
    """Delete a reconciled run's journal, then the rest of its staging folder.

    A journal that cannot be deleted raises its OSError, which fails the run: one left behind would be replayed after a
    later commit, as a run that was not committed. The rest holds no record, so a folder that cannot be deleted, such as
    one holding a file another program has open, is left, named with the reason, and the next deployment's recovery
    deletes it as a run with no journal.
    """
    fsops.remove(run_dir / "journal.jsonl")
    _remove_leftover_run(run_dir)


def _remove_leftover_run(run_dir: Path) -> None:
    """Delete a staging folder that holds no journal, or say why it stays, since nothing is left in it to recover."""
    try:
        fsops.remove(run_dir)
    except OSError as error:
        _warn(
            f"Could not delete the staging folder {platform_support.normalize(run_dir)}: {error.strerror or error}. "
            "It holds no journal, so the next deployment deletes it."
        )


def _recover_run(paths: Paths, run_dir: Path) -> bool:
    """Reconcile one interrupted run; return False when it needs manual inspection."""
    if platform_support.is_link_or_junction(run_dir):
        print(
            "  ERROR: Staging run is a symlink or junction and was "
            f"not replayed: {platform_support.normalize(run_dir)}",
            file=sys.stderr,
        )
        return False
    journal_file = run_dir / "journal.jsonl"
    # A journal whose stat fails raises here, failing the run, so a run is never deleted with a journal it could not
    # read.
    if not file_presence.is_occupied(journal_file):
        _remove_leftover_run(run_dir)
        return True
    run_id = run_dir.name
    committed = _committed_run_id(paths)
    if committed is None:
        print("  ERROR: Cannot read manifest during recovery.", file=sys.stderr)
        return False
    try:
        is_journal = file_presence.is_present(journal_file)
    except file_presence.NotAFile:
        is_journal = False
    if not is_journal:
        # A directory or anything else in the journal's place is no journal recovery can read, and deleting the run
        # with it could delete what the run moved aside, so it is kept for the user like a malformed line.
        print(
            f"  ERROR: Run {run_id}'s journal is not a file. Retaining {platform_support.normalize(run_dir)} "
            "for manual inspection.",
            file=sys.stderr,
        )
        _print_manual_steps(run_dir, run_id, committed == run_id)
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
        clear_run(run_dir)
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
    clear_run(run_dir)
    print("  Recovery complete.")
    return True


def _run_folders(paths: Paths) -> list[Path]:
    """The folders in the staging root, by name: anything else there is no run, and an entry whose stat fails raises
    its OSError rather than reading as no run."""
    return [
        path for path in sorted(paths.staging_root.iterdir()) if file_presence.is_directory(path, other_is_absent=True)
    ]


def pending_runs(paths: Paths) -> list[tuple[str, bool]]:
    """Each interrupted run the next deployment will reconcile, with whether it can do so without the user.

    Changes nothing, for --dry-run. A run without journal entries is not listed, since recovery only deletes it.
    """
    try:
        if not file_presence.is_directory(paths.staging_root):
            return []
        run_dirs = _run_folders(paths)
    except OSError as error:
        raise DeployError(
            f"ERROR: Cannot list the staging runs in {platform_support.normalize(paths.staging_root)}: "
            f"{error.strerror or error}",
            see_recovery("When recovery fails"),
        ) from error
    pending: list[tuple[str, bool]] = []
    for run_dir in run_dirs:
        if platform_support.is_link_or_junction(run_dir):
            pending.append((run_dir.name, False))
            continue
        journal_file = run_dir / "journal.jsonl"
        try:
            journaled = file_presence.is_occupied(journal_file)
        except OSError:
            pending.append((run_dir.name, False))
            continue
        if not journaled:
            continue
        if not journal_file.is_file():
            pending.append((run_dir.name, False))
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


def journaled_runs(paths: Paths) -> list[tuple[str, bool]] | None:
    """Each run whose journal is still on disk, with whether the manifest records it as committed.

    Changes nothing, for the report after recovery stops; recovery decides the same way, by the manifest's last run ID.
    None when the manifest or the staging runs cannot be read, so the report then says nothing about what is installed.
    """
    committed = _committed_run_id(paths)
    if committed is None:
        return None
    try:
        if not file_presence.is_directory(paths.staging_root):
            return []
        run_dirs = [
            path for path in sorted(paths.staging_root.iterdir()) if file_presence.is_occupied(path / "journal.jsonl")
        ]
    except OSError:
        return None
    return [(run_dir.name, run_dir.name == committed) for run_dir in run_dirs]


def recover_incomplete(paths: Paths) -> bool:
    """Reconcile every interrupted run; return False when any run needs manual inspection.

    An OSError, such as a file another program holds open, fails the run it hit, and recovery moves on to the next run,
    so one held file neither hides the state of the others nor prints a traceback in place of the guidance.
    """
    try:
        # Anything but a folder at the staging root is unexpected state, refused as one it cannot list.
        if not file_presence.is_directory(paths.staging_root):
            return True
        run_dirs = _run_folders(paths)
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

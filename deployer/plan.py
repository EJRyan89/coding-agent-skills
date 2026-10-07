"""The per-item plan: what one run does to every item, checked before rendering and carried out under the journal.

The dry run prints the plan and the deployment carries it out, so the two cannot disagree (#23).
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, replace
from pathlib import Path

from . import fsops, hashing, journal, platform_support, render
from .arguments import PROG
from .context import Context
from .errors import DeployError, see_recovery
from .kinds import ADAPTER_KIND, AGENT_KIND, BY_LABEL, MODIFIED, SHARED, SHARED_ASSET, SKILL, ItemKind
from .names import require_safe_name
from .paths import Paths
from .report import ACTION_LABELS, DRY_RUN, DRY_RUN_ACTIONS, SKIPPED_WITH_SKILL, ReportLine, print_report

# Actions that move the rendered copy into place, and actions that end this source's ownership of an item.
INSTALLING = frozenset({"INSTALL", "UPDATE", "UNCHANGED", "ADOPT", "REPLACE"})
RELEASING = frozenset({"REMOVE", "DROP"})
DIFF_LINES_REPORTED = 40


@dataclass(frozen=True)
class PlanEntry:
    """What this run does to one item: the dry run prints it, and the deployment carries out exactly that.

    state is what the run found at the destination: absent, wrong type, unmodified (it matches the manifest), modified,
    unmanaged and identical, unmanaged and differs, or still needed (a retained shared asset, left unread).
    """

    name: str
    kind: str
    state: str
    action: str
    reason: str = ""
    existing: str = ""  # the current copy's hash, when the destination has the expected type
    staged: str = ""  # the rendered copy's hash, for an item this run selects
    diff: tuple[str, ...] = ()

    def report_line(self, title: str) -> ReportLine:
        dry = title == DRY_RUN
        label = ACTION_LABELS[self.action][0 if dry else 1]
        return ReportLine(
            self.name, label, self.reason, self.diff if dry else self.diff[:DIFF_LINES_REPORTED], self.kind
        )


def _tree_diff(destination: Path, staged: dict[str, bytes], label: str) -> list[str]:
    existing = hashing.read_tree(destination)
    lines: list[str] = []
    for relative in sorted(set(existing) | set(staged)):
        if relative not in staged:
            lines.append(f"Only in {platform_support.normalize(destination)}: {relative}")
            continue
        if relative not in existing:
            lines.append(f"Only in {label}: {relative}")
            continue
        if existing[relative] == staged[relative]:
            continue
        before = existing[relative].decode("utf-8", "replace").splitlines()
        after = staged[relative].decode("utf-8", "replace").splitlines()
        lines += difflib.unified_diff(
            before,
            after,
            f"{platform_support.normalize(destination)}/{relative}",
            f"{label}/{relative}",
            lineterm="",
        )
    return lines


def _plan_selected(
    context: Context,
    kind: ItemKind,
    name: str,
    owned_hash: str | None,
    staged_hash: str,
    staged_tree: dict[str, bytes] | None = None,
) -> PlanEntry:
    """Plan an item this run installs, with --force and --force-item applied."""
    destination = journal.root_directory(context.paths, kind.root) / name
    if not os.path.lexists(destination):
        return PlanEntry(name, kind.label, "absent", "INSTALL", staged=staged_hash)
    if not kind.has_expected_type(destination):
        return PlanEntry(
            name, kind.label, "wrong type", "SKIP", f"destination is not {kind.type_name}", staged=staged_hash
        )
    existing = hashing.hash_path(destination)

    def found(state: str, action: str, reason: str = "", diff: list[str] | None = None) -> PlanEntry:
        return PlanEntry(name, kind.label, state, action, reason, existing, staged_hash, tuple(diff or ()))

    forced = context.forced(name)
    if owned_hash is not None:
        if existing == owned_hash:
            return found("unmodified", "UNCHANGED" if existing == staged_hash else "UPDATE")
        if forced:
            return found("modified", "REPLACE", "forced, previous copy backed up")
        return found("modified", "SKIP", MODIFIED)
    if existing == staged_hash:
        return found("unmanaged and identical", "ADOPT", "byte-identical")
    if forced:
        return found("unmanaged and differs", "REPLACE", "forced, was unmanaged, previous copy backed up")
    diff = _tree_diff(destination, staged_tree, f"staged/{name}") if staged_tree is not None else []
    return found("unmanaged and differs", "SKIP", kind.unmanaged_reason, diff)


def _plan_unselected(context: Context, kind: ItemKind, name: str, owned_hash: str) -> PlanEntry:
    """Plan an owned item this run no longer installs: removed when unmodified, otherwise left for the user."""
    destination = journal.root_directory(context.paths, kind.root) / name
    if kind.has_expected_type(destination):
        existing = hashing.hash_path(destination)
        if existing == owned_hash:
            return PlanEntry(name, kind.label, "unmodified", "REMOVE", kind.removal_reason, existing)
        return PlanEntry(name, kind.label, "modified", "PRESERVE", MODIFIED, existing)
    if os.path.lexists(destination):
        return PlanEntry(name, kind.label, "wrong type", "PRESERVE", f"destination is not {kind.type_name}")
    return PlanEntry(name, kind.label, "absent", "DROP", "already absent")


def build(
    context: Context,
    selected: list[str],
    adapters: list[str],
    staged: render.Staged,
    staged_shared: list[str],
    agents: list[str],
) -> list[PlanEntry]:
    """Decide once what this run does to every item it selects or owns, in the order the deployment carries it out."""
    owned = context.owned
    unselected_skills = [
        _plan_unselected(context, SKILL, name, value)
        for name, value in sorted(owned.skills.items())
        if name not in selected
    ]
    skills = [
        _plan_selected(context, SKILL, name, owned.skills.get(name), staged.skill_hash(name), staged.skills[name])
        for name in selected
    ]
    # An owned skill whose installed copy this run leaves in place keeps the shared assets it was deployed with.
    survivors = sorted(
        entry.name
        for entry in [*unselected_skills, *skills]
        if entry.action in ("SKIP", "PRESERVE") and entry.name in owned.skills
    )
    retained = _retained_shared(context, survivors, staged_shared)
    keep = set(staged_shared) | set(retained)
    skipped_skills = {entry.name for entry in skills if entry.action == "SKIP"}
    adapter_entries: list[PlanEntry] = []
    for name in adapters:
        entry = _plan_selected(context, ADAPTER_KIND, name, owned.adapters.get(name), staged.adapter_hash(name))
        if name in skipped_skills:
            entry = replace(entry, action="SKIP", reason=SKIPPED_WITH_SKILL, diff=())
        adapter_entries.append(entry)
    return [
        *unselected_skills,
        *(
            PlanEntry(asset, SHARED_ASSET, "still needed", "KEEP", f"needed by {reason}")
            for asset, reason in retained.items()
        ),
        *(
            _plan_unselected(context, SHARED, asset, value)
            for asset, value in sorted(owned.shared.items())
            if asset not in keep
        ),
        *(
            _plan_unselected(context, ADAPTER_KIND, name, value)
            for name, value in sorted(owned.adapters.items())
            if name not in adapters
        ),
        *(
            _plan_unselected(context, AGENT_KIND, name, value)
            for name, value in sorted(owned.agents.items())
            if name not in agents
        ),
        *skills,
        *adapter_entries,
        *(
            _plan_selected(context, SHARED, asset, owned.shared.get(asset), staged.shared_hash(asset))
            for asset in sorted(staged.shared)
        ),
        *(
            _plan_selected(context, AGENT_KIND, name, owned.agents.get(name), staged.agent_hash(name))
            for name in agents
        ),
    ]


def dry_run(entries: list[PlanEntry]) -> None:
    print_report(DRY_RUN, DRY_RUN_ACTIONS, [entry.report_line(DRY_RUN) for entry in entries])


def validate_shared_assets(context: Context, selected: list[str]) -> dict[str, str]:
    paths, src, data = context.paths, context.source, context.manifest
    assets: dict[str, str] = {}
    for asset, role in src.shared_assets.items():
        require_safe_name(asset, "shared asset name")
        assets[asset] = role if isinstance(role, str) else str(role)
    for asset in sorted(assets):
        if asset in src.skills:
            raise DeployError(f"ERROR: Shared asset '{asset}' collides with a skill of the same name")
    needed = {dependency for name in selected for dependency in src.skills[name].shared_deps}
    for asset, role in sorted(assets.items()):
        if role not in ("owner", "dependency"):
            raise DeployError(
                f"ERROR: Shared asset '{asset}' has invalid role '{role}' (must be 'owner' or 'dependency')"
            )
        if role == "owner" and not (paths.skills_src / asset).is_file():
            raise DeployError(
                f"ERROR: source.json declares '{asset}' as owned but file not found at "
                f"{platform_support.normalize(paths.skills_src / asset)}"
            )
        if role == "dependency" and asset in needed:
            destination = paths.dest_dir / asset
            if not destination.is_file():
                raise DeployError(
                    f"ERROR: Dependency '{asset}' not found at destination ({platform_support.normalize(destination)})"
                )
            owner = data.shared_owners.get(asset)
            if owner is None:
                raise DeployError(f"ERROR: Dependency '{asset}' has no owner in the manifest")
            if owner == context.source_id:
                raise DeployError(
                    f"ERROR: Dependency '{asset}' is declared as both dependency and owned by this source"
                )
            entry = data.entry(owner, "shared", asset) or {}
            owner_role = entry.get("role", "")
            if owner_role != "owner":
                raise DeployError(
                    f"ERROR: Dependency '{asset}' owner '{owner}' does not have role 'owner' (has '{owner_role}')"
                )
            expected = entry.get("hash", "")
            if not isinstance(expected, str) or not hashing.HASH_PATTERN.fullmatch(expected):
                raise DeployError(f"ERROR: Dependency '{asset}' owner '{owner}' has no valid SHA-256 hash in manifest")
            if hashing.hash_path(destination) != expected:
                raise DeployError(f"ERROR: Dependency '{asset}' at destination does not match owner's manifest hash")
    for name in selected:
        for dependency in src.skills[name].shared_deps:
            if dependency not in assets:
                raise DeployError(
                    f"ERROR: Skill '{name}' declares shared_dep "
                    f"'{dependency}' but it is not in source.json shared_assets"
                )
    return assets


def shared_to_stage(context: Context, selected: list[str], assets: dict[str, str]) -> list[str]:
    """The shared assets this source owns that the selected skills need, which this run renders and installs."""
    needed = {dependency for name in selected for dependency in context.source.skills[name].shared_deps}
    return sorted(asset for asset in needed if assets.get(asset) == "owner")


def _retained_shared(context: Context, survivors: list[str], staged_shared: list[str]) -> dict[str, str]:
    """Owned shared assets this run does not install but keeps, each with what still needs it."""
    required_by: dict[str, str] = {}
    for name in survivors:
        for asset in context.owned.skill_shared_deps.get(name, []):
            required_by.setdefault(asset, name)
    for other, entry in sorted(context.manifest.sources.items()):
        if other == context.source_id:
            continue
        for skill_entry in (entry.get("skills") or {}).values():
            for asset in skill_entry.get("shared_deps", []):
                required_by.setdefault(asset, f"source {other}")
    return {
        asset: required_by[asset]
        for asset in sorted(context.owned.shared)
        if asset not in staged_shared and asset in required_by
    }


def _ensure_transient_available(item: str, base: Path) -> None:
    transient = base / f"{item}.deploying-bak"
    if os.path.lexists(transient):
        raise DeployError(
            f"ERROR: Refusing to deploy '{item}': transient backup "
            f"already exists at {platform_support.normalize(transient)}",
            "Resolve or remove the stale backup after verifying its contents, then retry.",
            see_recovery("Backups"),
        )


def check_ownership(
    context: Context, selected: list[str], adapters: list[str], staged_shared: list[str], agents: list[str]
) -> None:
    data, owned, source_id, paths = context.manifest, context.owned, context.source_id, context.paths
    ownership = see_recovery("Ownership held by another source")

    def transfer(subject: str, owner: str) -> DeployError:
        return DeployError(
            f"ERROR: {subject} is owned by source '{owner}'.",
            f"To take it over, run '{PROG} --migrate-from {owner}', then rerun this deployment.",
            ownership,
        )

    def collision(subject: str, kind: str, owner: str) -> DeployError:
        return DeployError(
            f"ERROR: {subject} collides with {kind} owned by source '{owner}'.",
            f"One name cannot be deployed as two kinds. Rename it in one source, or stop deploying it from '{owner}'.",
            ownership,
        )

    wanted = {SKILL: selected, SHARED: staged_shared, ADAPTER_KIND: adapters, AGENT_KIND: agents}
    # Skills and shared assets share one root, so a name owned as one cannot be deployed as the other.
    shares_root = {SKILL: SHARED, SHARED: SKILL}
    for kind, names in wanted.items():
        base = journal.root_directory(paths, kind.root)
        for name in names:
            owner = data.owners_of(kind).get(name)
            if owner is not None and owner != source_id:
                raise transfer(f"{kind.title} '{name}'", owner)
            other = shares_root.get(kind)
            if other is not None and name in data.owners_of(other):
                raise collision(f"{kind.title} '{name}'", f"a {other.noun}", data.owners_of(other)[name])
            _ensure_transient_available(name, base)
        for name in sorted(owned.of(kind)):
            if name not in names:
                _ensure_transient_available(name, base)
    candidates = [
        journal.root_directory(paths, kind.root) / name
        for kind, names in wanted.items()
        for name in [*names, *owned.of(kind)]
    ]
    for destination in candidates:
        link = hashing.find_link(destination) if os.path.lexists(destination) else None
        if link is not None:
            raise DeployError(
                f"ERROR: Destination '{destination.name}' contains a "
                f"symlink or junction: {platform_support.normalize(link)}",
                "Remove it or restore the deployed copy after verifying its contents, then retry.",
            )


def _replace(
    record: journal.Journal,
    root: str,
    base: Path,
    source_path: Path,
    name: str,
    staged_hash: str,
    existing: str | None,
    retain: bool,
) -> None:
    destination = base / name
    if existing is not None:
        record.backup(root, name, existing, retain=retain)
        fsops.move(destination, base / f"{name}.deploying-bak")
    record.install(root, name, staged_hash)
    fsops.move(source_path, destination)


def carry_out(record: journal.Journal, paths: Paths, staging_dir: Path, entry: PlanEntry) -> None:
    """Make the one filesystem change the plan entry names; skipped, preserved, kept, and dropped items need none."""
    kind = BY_LABEL[entry.kind]
    base = journal.root_directory(paths, kind.root)
    if entry.action == "REMOVE":
        record.backup(kind.root, entry.name, entry.existing, retain=False)
        fsops.move(base / entry.name, base / f"{entry.name}.deploying-bak")
    elif entry.action in INSTALLING:
        _replace(
            record,
            kind.root,
            base,
            staging_dir / kind.staging / entry.name,
            entry.name,
            entry.staged,
            entry.existing or None,
            entry.action == "REPLACE",
        )

"""The per-item plan: what one run does to every item, checked before rendering and carried out under the journal.

The dry run prints the plan and the deployment carries it out, so the two cannot disagree.
"""

from __future__ import annotations

import difflib
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from . import fsops, hashing, journal, platform_support, render
from .arguments import PROG
from .context import Context
from .errors import DeployError, see_recovery
from .kinds import ADAPTER_KIND, AGENT_KIND, BY_LABEL, DEPENDENCY_KINDS, KINDS, MODIFIED, SHARED, SKILL, ItemKind
from .names import require_safe_name
from .paths import Paths
from .report import ACTION_LABELS, DRY_RUN, DRY_RUN_ACTIONS, SKIPPED_WITH_SKILL, ReportLine, print_report
from .source import Source

# Actions that move the rendered copy into place, and actions that end this source's ownership of an item.
INSTALLING = frozenset({"INSTALL", "UPDATE", "UNCHANGED", "ADOPT", "REPLACE"})
RELEASING = frozenset({"REMOVE", "DROP"})
DIFF_LINES_REPORTED = 40


@dataclass(frozen=True)
class PlanEntry:
    """What this run does to one item: the dry run prints it, and the deployment carries out exactly that.

    state is what the run found at the destination: absent, wrong type, unmodified (it matches the manifest), modified,
    unmanaged and identical, unmanaged and differs, still needed (a retained shared asset or agent, left unread), or
    owned by another source (an item the file system treats as another source's, left unread).
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

    forced = context.forced(kind, name)
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


def _claims(context: Context, kind: ItemKind, name: str) -> list[tuple[ItemKind, str, str]]:
    """Every owned item the file system treats as this one: same root, same name key. Each as (kind, name, owner)."""
    key = platform_support.name_key(name)
    return [
        (other, existing, owner)
        for other in KINDS
        if other.root == kind.root
        for existing, owner in sorted(context.manifest.owners_of(other).items())
        if platform_support.name_key(existing) == key
    ]


def _plan_unselected(context: Context, kind: ItemKind, name: str, owned_hash: str) -> PlanEntry:
    """Plan an owned item this run no longer installs: removed when unmodified, otherwise left for the user.

    An item another source also owns under a name the file system treats as the same, which a manifest written before
    such names were refused can hold, is that source's file too, so this source only gives up its ownership.
    """
    for _, existing, owner in _claims(context, kind, name):
        if owner != context.source_id:
            reason = f"the same file as '{existing}', owned by source '{owner}'"
            return PlanEntry(name, kind.label, "owned by another source", "DROP", reason)
    destination = journal.root_directory(context.paths, kind.root) / name
    if kind.has_expected_type(destination):
        existing = hashing.hash_path(destination)
        if existing == owned_hash:
            return PlanEntry(name, kind.label, "unmodified", "REMOVE", kind.removal_reason, existing)
        return PlanEntry(name, kind.label, "modified", "PRESERVE", MODIFIED, existing)
    if os.path.lexists(destination):
        return PlanEntry(name, kind.label, "wrong type", "PRESERVE", f"destination is not {kind.type_name}")
    return PlanEntry(name, kind.label, "absent", "DROP", "already absent")


def build(context: Context, wanted: Mapping[ItemKind, list[str]], staged: render.Staged) -> list[PlanEntry]:
    """Decide once what this run does to every item it selects or owns, in the order the deployment carries it out.

    The owned items it releases come first and then the items it installs, each pass in the kinds' order. Skills are
    planned before the rest because the others depend on them: a skill left in place keeps the shared assets and agents
    it was deployed with, and a runtime adapter is skipped with its skill.
    """
    owned = context.owned
    selected, adapters = wanted[SKILL], wanted[ADAPTER_KIND]
    unselected_skills = [
        _plan_unselected(context, SKILL, name, value)
        for name, value in sorted(owned.skills.items())
        if name not in selected
    ]
    skills = [
        _plan_selected(context, SKILL, name, owned.skills.get(name), staged.skill_hash(name), staged.skills[name])
        for name in selected
    ]
    # An owned skill whose installed copy this run leaves in place keeps the shared assets and agents it was deployed
    # with.
    survivors = sorted(
        entry.name
        for entry in [*unselected_skills, *skills]
        if entry.action in ("SKIP", "PRESERVE") and entry.name in owned.skills
    )
    retained = {kind: _retained(context, kind, survivors, wanted[kind]) for kind in DEPENDENCY_KINDS}
    kept = {kind: set(names) for kind, names in wanted.items()}
    for kind, items in retained.items():
        kept[kind] |= set(items)
    skipped_skills = {entry.name for entry in skills if entry.action == "SKIP"}
    adapter_entries: list[PlanEntry] = []
    for name in adapters:
        entry = _plan_selected(context, ADAPTER_KIND, name, owned.adapters.get(name), staged.adapter_hash(name))
        if name in skipped_skills:
            entry = replace(entry, action="SKIP", reason=SKIPPED_WITH_SKILL, diff=())
        adapter_entries.append(entry)
    # Each kind's rendered content has its own shape in render.Staged, so each hash is read through its own method.
    installs = {
        SKILL: skills,
        SHARED: [
            _plan_selected(context, SHARED, asset, owned.shared.get(asset), staged.shared_hash(asset))
            for asset in wanted[SHARED]
        ],
        ADAPTER_KIND: adapter_entries,
        AGENT_KIND: [
            _plan_selected(context, AGENT_KIND, name, owned.agents.get(name), staged.agent_hash(name))
            for name in wanted[AGENT_KIND]
        ],
    }
    return [
        *unselected_skills,
        *(
            PlanEntry(name, kind.label, "still needed", "KEEP", f"needed by {reason}")
            for kind, items in retained.items()
            for name, reason in items.items()
        ),
        *(
            _plan_unselected(context, kind, name, value)
            for kind in KINDS
            if kind is not SKILL
            for name, value in sorted(owned.of(kind).items())
            if name not in kept[kind]
        ),
        *(entry for kind in KINDS for entry in installs[kind]),
    ]


def check_forced_items(context: Context, wanted: Mapping[ItemKind, list[str]]) -> None:
    """Refuse a --force-item that names no item this run installs, by its deployed name or its own name."""
    accepted = {name: kind.item_name(name) for kind, names in wanted.items() for name in names}
    known = set(accepted) | set(accepted.values())
    unknown = [f"'{name}'" for name in dict.fromkeys(context.options.force_items) if name not in known]
    if not unknown:
        return
    named = " and ".join(unknown) if len(unknown) < 3 else f"{', '.join(unknown[:-1])}, and {unknown[-1]}"
    choices = sorted(set(accepted.values()))
    raise DeployError(
        f"ERROR: --force-item names {named}, which this run does not install.",
        f"Name one of: {', '.join(choices)}."
        if choices
        else "This run installs nothing, so --force-item has nothing to replace.",
    )


def dry_run(entries: list[PlanEntry]) -> None:
    print_report(DRY_RUN, DRY_RUN_ACTIONS, [entry.report_line(DRY_RUN) for entry in entries])


def validate_shared_assets(context: Context, selected: list[str]) -> dict[str, str]:
    src = context.source
    assets = _declared_assets(src)
    needed = {dependency for name in selected for dependency in src.skills[name].shared_deps}
    for asset, role in sorted(assets.items()):
        _check_role(context.paths, asset, role)
        if role == "dependency" and asset in needed:
            _check_dependency(context, asset)
    for name in selected:
        for dependency in src.skills[name].shared_deps:
            if dependency not in assets:
                raise DeployError(
                    f"ERROR: Skill '{name}' declares shared_dep "
                    f"'{dependency}' but it is not in source.json shared_assets"
                )
    return assets


def _declared_assets(src: Source) -> dict[str, str]:
    """source.json's shared assets and their roles, once every name is safe and none names a skill's file or another's.

    Names are compared as the file system compares them, so Guide.md and guide.md are one file on Windows.
    """
    assets: dict[str, str] = {}
    for asset, role in src.shared_assets.items():
        require_safe_name(asset, "shared asset name")
        assets[asset] = role
    taken = {platform_support.name_key(name): ("skill", name) for name in sorted(src.skills)}
    for asset in sorted(assets):
        if asset in src.skills:
            raise DeployError(f"ERROR: Shared asset '{asset}' collides with a skill of the same name")
        key = platform_support.name_key(asset)
        if key in taken:
            noun, existing = taken[key]
            raise DeployError(
                f"ERROR: Shared asset '{asset}' collides with {noun} '{existing}'.",
                f"The file system treats '{asset}' and '{existing}' as one name. Rename one of them.",
            )
        taken[key] = ("shared asset", asset)
    return assets


def _check_role(paths: Paths, asset: str, role: str) -> None:
    """A role is owner or dependency, and an owned asset is a file in this source."""
    if role not in ("owner", "dependency"):
        raise DeployError(f"ERROR: Shared asset '{asset}' has invalid role '{role}' (must be 'owner' or 'dependency')")
    if role == "owner" and not (paths.skills_src / asset).is_file():
        raise DeployError(
            f"ERROR: source.json declares '{asset}' as owned but file not found at "
            f"{platform_support.normalize(paths.skills_src / asset)}"
        )


def _check_dependency(context: Context, asset: str) -> None:
    """A needed dependency is installed, and another source owns it with the hash of what is installed."""
    destination = context.paths.dest_dir / asset
    if not destination.is_file():
        raise DeployError(
            f"ERROR: Dependency '{asset}' not found at destination ({platform_support.normalize(destination)})"
        )
    owner = context.manifest.shared_owners.get(asset)
    if owner is None:
        raise DeployError(f"ERROR: Dependency '{asset}' has no owner in the manifest")
    if owner == context.source_id:
        raise DeployError(f"ERROR: Dependency '{asset}' is declared as both dependency and owned by this source")
    entry = context.manifest.entry(owner, "shared", asset) or {}
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


def shared_to_stage(context: Context, selected: list[str], assets: dict[str, str]) -> list[str]:
    """The shared assets this source owns that the selected skills need, which this run renders and installs."""
    needed = {dependency for name in selected for dependency in context.source.skills[name].shared_deps}
    return sorted(asset for asset in needed if assets.get(asset) == "owner")


def _retained(context: Context, kind: ItemKind, survivors: list[str], staged: list[str]) -> dict[str, str]:
    """Owned items of a kind skills depend on that this run does not install but keeps, each with what needs it.

    A skill another source deploys can depend on a shared asset this source owns, declared there with the role
    dependency, so another source's skills keep shared assets too. An agent is deployed only by the source whose skill
    declares it, and two sources never own one agent, so only this source's skills keep an agent.
    """
    required_by: dict[str, str] = {}
    for name in survivors:
        for item in context.owned.deps_of(kind).get(name, []):
            required_by.setdefault(item, name)
    if kind is SHARED:
        for other, entry in sorted(context.manifest.sources.items()):
            if other == context.source_id:
                continue
            for skill_entry in (entry.get(SKILL.key) or {}).values():
                for asset in skill_entry.get(SHARED.dependency_key, []):
                    required_by.setdefault(asset, f"source {other}")
    return {
        item: required_by[item] for item in sorted(context.owned.of(kind)) if item not in staged and item in required_by
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


def check_ownership(context: Context, wanted: Mapping[ItemKind, list[str]]) -> None:
    owned, source_id, paths = context.owned, context.source_id, context.paths
    ownership = see_recovery("Ownership held by another source")

    def transfer(subject: str, owner: str) -> DeployError:
        return DeployError(
            f"ERROR: {subject} is owned by source '{owner}'.",
            f"To take it over, run '{PROG} --migrate-from {owner}', then rerun this deployment.",
            ownership,
        )

    def collision(subject: str, name: str, other: ItemKind, existing: str, owner: str) -> DeployError:
        if existing == name:
            return DeployError(
                f"ERROR: {subject} collides with a {other.noun} owned by source '{owner}'.",
                "One name cannot be deployed as two kinds. "
                f"Rename it in one source, or stop deploying it from '{owner}'.",
                ownership,
            )
        if owner == source_id:
            remedy = (
                f"Keep the name '{existing}', or deploy once without it so that it is removed, then deploy '{name}'."
            )
        else:
            remedy = f"Rename it in one source, or stop deploying '{existing}' from '{owner}'."
        return DeployError(
            f"ERROR: {subject} collides with {other.noun} '{existing}' owned by source '{owner}'.",
            f"The file system treats '{name}' and '{existing}' as one name. {remedy}",
            ownership,
        )

    # An owned item under the same root whose name the file system treats as this one's is the same file: another
    # source's copy of this very item is a transfer, and anything else is a collision, such as a skill and a shared
    # asset of one name, or Guide.md and guide.md on Windows.
    for kind, names in wanted.items():
        base = journal.root_directory(paths, kind.root)
        for name in names:
            subject = f"{kind.title} '{name}'"
            for other, existing, owner in _claims(context, kind, name):
                if other is not kind or existing != name:
                    raise collision(subject, name, other, existing, owner)
                if owner != source_id:
                    raise transfer(subject, owner)
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

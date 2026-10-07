"""The deployment pipeline: select, render, validate, and apply under a journal."""

from __future__ import annotations

import difflib
import os
import secrets
import sys
import textwrap
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TextIO

from . import config, fsops, hashing, journal, lock, manifest, platform_support, render, source, tools
from .arguments import CHECK_COMMAND_LINE, CONFIGURE_COMMAND_LINE, PROG, ParserExit, deploy_parser
from .errors import Cancelled, DeployError, print_error, see_recovery
from .manifest import Ownership
from .names import require_safe_name
from .paths import Paths, canary_home, claim_canary_home, validate_managed_roots

TAKE_OVER_SOURCE = "--take-over-source"


@dataclass
class Options:
    select_all: bool = False
    force: bool = False
    force_items: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    dry_run: bool = False
    migrate_from: str = ""
    take_over_source: bool = False
    canary_home: str = ""


def parse_arguments(arguments: list[str], source_id: str) -> Options:
    options = Options(**vars(deploy_parser().parse_args(arguments)))
    if options.include and not options.select_all:
        raise DeployError("ERROR: --include can only be used with --all")
    if options.migrate_from:
        if not source.SOURCE_ID_PATTERN.fullmatch(options.migrate_from):
            raise DeployError(f"ERROR: Invalid migration source ID: {options.migrate_from}")
        if options.migrate_from == source_id:
            raise DeployError("ERROR: --migrate-from must name a different source")
        if options.dry_run:
            raise DeployError("ERROR: --migrate-from cannot be combined with --dry-run")
    if options.take_over_source and options.dry_run:
        raise DeployError(f"ERROR: {TAKE_OVER_SOURCE} cannot be combined with --dry-run")
    if options.canary_home:
        for flag, used in (
            ("--dry-run", options.dry_run),
            ("--migrate-from", options.migrate_from),
            (TAKE_OVER_SOURCE, options.take_over_source),
        ):
            if used:
                raise DeployError(f"ERROR: --canary-home cannot be combined with {flag}")
    return options


@dataclass
class Selection:
    bundles: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    deselect_all: bool = False


@dataclass
class Context:
    paths: Paths
    options: Options
    source: source.Source
    config: dict[str, str]
    manifest: manifest.Manifest
    owned: Ownership  # not manifest.Ownership: in this class body, manifest names the field above
    stdin: TextIO
    selection: Selection = field(default_factory=Selection)

    @property
    def source_id(self) -> str:
        return self.source.source_id

    def forced(self, item: str) -> bool:
        return self.options.force or item in self.options.force_items


def currently_chosen(src: source.Source, owned: manifest.Ownership, name: str, kind: str) -> bool:
    """Whether a menu item is currently deployed from this source."""
    if kind == "bundle":
        return name in owned.requested_bundles or all(member in owned.skills for member in src.bundles[name])
    return name in owned.requested_skills or name in owned.skills


def _select_all(context: Context, include: list[str]) -> Selection:
    """Every menu item except opt-in ones, which stay only when already installed or named with --include."""
    src = context.source
    selection = Selection()
    for names, kind, chosen in (
        (sorted(src.bundles), "bundle", selection.bundles),
        (source.root_names(src), "skill", selection.skills),
    ):
        for name in names:
            if not source.is_opt_in(src, name) or name in include or currently_chosen(src, context.owned, name, kind):
                chosen.append(name)
    return selection


def _select(context: Context) -> Selection | None:
    src = context.source
    selection = Selection()
    if context.options.select_all:
        roots = set(src.bundles) | set(source.root_names(src))
        unknown = [name for name in context.options.include if name not in roots]
        if unknown:
            raise DeployError(f"ERROR: --include names no bundle or skill in this source: {', '.join(unknown)}")
        return _select_all(context, context.options.include)
    print("")
    if not src.skills:
        print("No current skills discovered; previously owned skills will be considered for removal.")
        selection.deselect_all = True
        return selection
    print("Select what to deploy:")
    choices = [(name, "bundle") for name in sorted(src.bundles)] + [(name, "skill") for name in source.root_names(src)]
    if not choices:
        print("No selectable bundles or skills discovered; previously owned skills will be considered for removal.")
        selection.deselect_all = True
        return selection
    for index, (name, kind) in enumerate(choices, start=1):
        labels = [*(["bundle"] if kind == "bundle" else []), *(["opt-in"] if source.is_opt_in(src, name) else [])]
        suffix = f" ({', '.join(labels)})" if labels else ""
        mark = "*" if currently_chosen(src, context.owned, name, kind) else " "
        print(f"  [{mark}] {index}. {name}{suffix}")
    print("[*] = currently deployed")
    print("Enter numbers separated by spaces, 'all', or 'none'. Ctrl+C cancels.")
    print("Selection: ", end="", flush=True)
    try:
        line = context.stdin.readline()
    except KeyboardInterrupt:
        print("")
        raise Cancelled("Cancelled; nothing was changed.") from None
    if not line:
        raise DeployError("ERROR: No selection was provided.")
    answer = line.rstrip("\r\n")
    if answer == "all":
        selection = _select_all(context, [])
    elif answer == "none":
        selection.deselect_all = True
    else:
        for token in answer.split():
            if not token.isdigit():
                raise DeployError(f"ERROR: Invalid selection '{token}'")
            index = int(token) - 1
            if index < 0 or index >= len(choices):
                raise DeployError(f"ERROR: Selection '{token}' is out of range")
            name, kind = choices[index]
            (selection.bundles if kind == "bundle" else selection.skills).append(name)
    if not selection.bundles and not selection.skills and not selection.deselect_all:
        print("", file=sys.stderr)
        print("No skills selected.", file=sys.stderr)
        print("", file=sys.stderr)
        return None
    return selection


def _require_variables(context: Context, selected: list[str]) -> None:
    missing: list[str] = []
    for name in selected:
        for variable in context.source.skills[name].required_vars:
            if variable in config.DERIVED_VARIABLES or context.config.get(variable):
                continue
            if variable not in missing:
                missing.append(variable)
    if missing:
        lines = [
            f"ERROR: Selected skills require variables not set in config: {' '.join(missing)}",
            "Skills requiring these:",
        ]
        for variable in missing:
            lines += [
                f"  {name} -> {variable}" for name in selected if variable in context.source.skills[name].required_vars
            ]
        raise DeployError(*lines, f"Run '{CONFIGURE_COMMAND_LINE}' to set them.")


def _validate_shared_assets(context: Context, selected: list[str]) -> dict[str, str]:
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


@dataclass(frozen=True)
class ReportLine:
    name: str
    action: str
    detail: str = ""
    extra: tuple[str, ...] = ()
    kind: str = ""

    @property
    def text(self) -> str:
        detail = _detail(self.kind, self.detail)
        return f"  {self.name}{f' ({detail})' if detail else ''}"


ADAPTER = "runtime adapter"
SHARED_ASSET = "shared asset"
AGENT = "agent"
KIND_ORDER = {"": 0, ADAPTER: 1, SHARED_ASSET: 2, AGENT: 3}


def _detail(*parts: str) -> str:
    return ", ".join(part for part in parts if part)


MODIFIED = "modified since last deploy"
DIFFERS = "unmanaged and differs"
DIFFERS_FROM_SKILL = "unmanaged and differs from the rendered skill"
SKIPPED_WITH_SKILL = "its skill was skipped"

DRY_RUN = "DRY RUN"
DEPLOYED = "DEPLOYED"
# Each planned action, in report order with attention first, and its label in the dry run and the deployment report.
ACTION_LABELS = {
    "SKIP": ("CONFLICT", "SKIPPED"),
    "PRESERVE": ("PRESERVE", "PRESERVED"),
    "REPLACE": ("REPLACE", "REPLACED"),
    "REMOVE": ("REMOVE", "REMOVED"),
    "UPDATE": ("UPDATE", "UPDATED"),
    "INSTALL": ("FRESH INSTALL", "INSTALLED"),
    "ADOPT": ("ADOPT", "ADOPTED"),
    "DROP": ("DROP OWNERSHIP", "DROPPED OWNERSHIP"),
    "KEEP": ("KEEP", "KEPT"),
    "UNCHANGED": ("UNCHANGED", "UNCHANGED"),
}
DRY_RUN_ACTIONS = tuple(dry for dry, _ in ACTION_LABELS.values())
DEPLOY_ACTIONS = tuple(applied for _, applied in ACTION_LABELS.values())
# Reasons that --force or --force-item resolve; a destination of the wrong type needs manual cleanup instead.
FORCEABLE = {label: frozenset({MODIFIED, DIFFERS, DIFFERS_FROM_SKILL}) for label in ACTION_LABELS["SKIP"]}
FORCE_HINTS = {
    DRY_RUN: "Replace conflicting items with --force-item NAME or --force (backups are kept).",
    DEPLOYED: "Replace skipped items with --force-item NAME or --force (backups are kept).",
}

ATTENTION_ACTIONS = frozenset(label for action in ("SKIP", "PRESERVE", "REPLACE") for label in ACTION_LABELS[action])


def _fold_adapters(lines: list[ReportLine]) -> list[ReportLine]:
    """Drop runtime adapter lines that only repeat their skill's line.

    An adapter is a thin pointer to its skill, so it keeps its own line only when it needs attention for a
    reason of its own, or when it does something other than its skill and other than staying unchanged.
    """
    skill_actions = {line.name: line.action for line in lines if not line.kind}

    def repeats_skill(line: ReportLine) -> bool:
        if line.kind != ADAPTER or line.name not in skill_actions:
            return False
        if line.action == "UNCHANGED" or line.detail == SKIPPED_WITH_SKILL:
            return True
        return line.action == skill_actions[line.name] and line.action not in ATTENTION_ACTIONS

    return [line for line in lines if not repeats_skill(line)]


REPORT_WIDTH = 80


def _wrap(text: str) -> str:
    """Wrap a long report line between words, never inside a hyphenated name, with an indented continuation."""
    return textwrap.fill(
        text, width=REPORT_WIDTH, subsequent_indent="    ", break_long_words=False, break_on_hyphens=False
    )


def print_report(title: str, order: tuple[str, ...], lines: list[ReportLine]) -> None:
    """Print report lines grouped by action, attention first and each group alphabetized, then any force hint."""
    lines = _fold_adapters(lines)
    forceable = any(line.detail in FORCEABLE.get(line.action, ()) for line in lines)
    print("")
    print(f"=== {title} ===")
    actions = [*order, *sorted({line.action for line in lines} - set(order))]
    for action in actions:
        group = sorted(
            (line for line in lines if line.action == action),
            key=lambda line: (line.name, KIND_ORDER.get(line.kind, len(KIND_ORDER)), line.kind, line.detail),
        )
        if not group:
            continue
        print("")
        print(f"{action} ({len(group)}):")
        for line in group:
            print(_wrap(line.text))
            for extra in line.extra:
                print(extra)
    print("")
    if forceable and title in FORCE_HINTS:
        print(FORCE_HINTS[title])
        print("")


# Actions that move the rendered copy into place, and actions that end this source's ownership of an item.
INSTALLING = frozenset({"INSTALL", "UPDATE", "UNCHANGED", "ADOPT", "REPLACE"})
RELEASING = frozenset({"REMOVE", "DROP"})
DIFF_LINES_REPORTED = 40


@dataclass(frozen=True)
class ItemKind:
    """Where one kind of item is deployed, and the reasons the plan gives for it."""

    label: str
    root: str  # the journal's name for the managed root
    staging: str  # the kind's directory under the run's staging directory
    directory: bool
    removal_reason: str
    unmanaged_reason: str

    @property
    def type_name(self) -> str:
        return "a directory" if self.directory else "a file"

    def has_expected_type(self, path: Path) -> bool:
        return path.is_dir() if self.directory else path.is_file()


SKILL_KIND = ItemKind("", "claude", "", True, "deselected or absent from source", DIFFERS_FROM_SKILL)
SHARED_KIND = ItemKind(SHARED_ASSET, "claude", "", False, "obsolete", DIFFERS)
ADAPTER_KIND = ItemKind(ADAPTER, "agents", render.ADAPTER_STAGING, True, "obsolete", DIFFERS)
AGENT_KIND = ItemKind(AGENT, "claude-agents", render.AGENT_STAGING, False, "no selected skill needs it", DIFFERS)
ITEM_KINDS = {kind.label: kind for kind in (SKILL_KIND, SHARED_KIND, ADAPTER_KIND, AGENT_KIND)}


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


def _plan(
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
        _plan_unselected(context, SKILL_KIND, name, value)
        for name, value in sorted(owned.skills.items())
        if name not in selected
    ]
    skills = [
        _plan_selected(context, SKILL_KIND, name, owned.skills.get(name), staged.skill_hash(name), staged.skills[name])
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
            _plan_unselected(context, SHARED_KIND, asset, value)
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
            _plan_selected(context, SHARED_KIND, asset, owned.shared.get(asset), staged.shared_hash(asset))
            for asset in sorted(staged.shared)
        ),
        *(
            _plan_selected(context, AGENT_KIND, name, owned.agents.get(name), staged.agent_hash(name))
            for name in agents
        ),
    ]


def _dry_run(entries: list[PlanEntry]) -> None:
    print_report(DRY_RUN, DRY_RUN_ACTIONS, [entry.report_line(DRY_RUN) for entry in entries])


def _ensure_transient_available(item: str, base: Path) -> None:
    transient = base / f"{item}.deploying-bak"
    if os.path.lexists(transient):
        raise DeployError(
            f"ERROR: Refusing to deploy '{item}': transient backup "
            f"already exists at {platform_support.normalize(transient)}",
            "Resolve or remove the stale backup after verifying its contents, then retry.",
            see_recovery("Backups"),
        )


def _shared_to_stage(context: Context, selected: list[str], assets: dict[str, str]) -> list[str]:
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


def _check_ownership(
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

    for name in selected:
        owner = data.skill_owners.get(name)
        if owner is not None and owner != source_id:
            raise transfer(f"Skill '{name}'", owner)
        if name in data.shared_owners:
            raise collision(f"Skill '{name}'", "a shared asset", data.shared_owners[name])
        _ensure_transient_available(name, paths.dest_dir)
    for name in sorted(owned.skills):
        if name not in selected:
            _ensure_transient_available(name, paths.dest_dir)
    for asset in staged_shared:
        owner = data.shared_owners.get(asset)
        if owner is not None and owner != source_id:
            raise transfer(f"Shared asset '{asset}'", owner)
        if asset in data.skill_owners:
            raise collision(f"Shared asset '{asset}'", "a skill", data.skill_owners[asset])
        _ensure_transient_available(asset, paths.dest_dir)
    for asset in sorted(owned.shared):
        if asset not in staged_shared:
            _ensure_transient_available(asset, paths.dest_dir)
    for name in adapters:
        owner = data.adapter_owners.get(name)
        if owner is not None and owner != source_id:
            raise transfer(f"Runtime adapter '{name}'", owner)
        _ensure_transient_available(name, paths.adapter_dest_dir)
    for name in sorted(owned.adapters):
        if name not in adapters:
            _ensure_transient_available(name, paths.adapter_dest_dir)
    for name in agents:
        owner = data.agent_owners.get(name)
        if owner is not None and owner != source_id:
            raise transfer(f"Agent '{name}'", owner)
        _ensure_transient_available(name, paths.agent_dest_dir)
    for name in sorted(owned.agents):
        if name not in agents:
            _ensure_transient_available(name, paths.agent_dest_dir)
    candidates = [
        *(paths.dest_dir / name for name in [*selected, *owned.skills]),
        *(paths.dest_dir / asset for asset in [*staged_shared, *owned.shared]),
        *(paths.adapter_dest_dir / name for name in [*adapters, *owned.adapters]),
        *(paths.agent_dest_dir / name for name in [*agents, *owned.agents]),
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


def _carry_out(record: journal.Journal, paths: Paths, staging_dir: Path, entry: PlanEntry) -> None:
    """Make the one filesystem change the plan entry names; skipped, preserved, kept, and dropped items need none."""
    kind = ITEM_KINDS[entry.kind]
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


def _apply(context: Context, selected: list[str], entries: list[PlanEntry], staged: render.Staged, run_id: str) -> None:
    paths = context.paths
    staging_dir = paths.staging_root / run_id
    staged.write(staging_dir)
    record = journal.Journal(staging_dir / "journal.jsonl", run_id)
    record.create()
    fsops.make_directories(paths.dest_dir)
    fsops.make_directories(paths.adapter_dest_dir)
    fsops.make_directories(paths.agent_dest_dir)
    for entry in entries:
        _carry_out(record, paths, staging_dir, entry)
    _commit_manifest(context, selected, entries, run_id)
    backups = _finalize(context, record, run_id)
    fsops.remove(staging_dir)
    _summary(context, run_id, entries, backups)


def _commit_manifest(context: Context, selected: list[str], entries: list[PlanEntry], run_id: str) -> None:
    owned, data = context.owned, context.manifest
    skills = {
        name: {"hash": value, "shared_deps": list(owned.skill_shared_deps.get(name, []))}
        for name, value in owned.skills.items()
    }
    shared = {
        name: {"hash": value, "role": owned.shared_roles.get(name, "owner")} for name, value in owned.shared.items()
    }
    adapter_entries = {name: {"hash": value} for name, value in owned.adapters.items()}
    agent_entries = {name: {"hash": value} for name, value in owned.agents.items()}
    recorded: dict[str, dict[str, dict]] = {
        SKILL_KIND.label: skills,
        SHARED_ASSET: shared,
        ADAPTER: adapter_entries,
        AGENT: agent_entries,
    }
    for entry in entries:
        if entry.action in RELEASING:
            recorded[entry.kind].pop(entry.name, None)
        elif entry.action in INSTALLING:
            details: dict[str, object] = {"hash": entry.staged}
            if entry.kind == SKILL_KIND.label:
                details["shared_deps"] = sorted(context.source.skills[entry.name].shared_deps)
            elif entry.kind == SHARED_ASSET:
                details["role"] = "owner"
            recorded[entry.kind][entry.name] = details
    removed_skills = {entry.name for entry in entries if entry.kind == SKILL_KIND.label and entry.action in RELEASING}
    selected_skills = list(owned.selected_skills)
    for name in selected:
        if name not in selected_skills:
            selected_skills.append(name)
    selected_skills = [name for name in selected_skills if name not in removed_skills]
    data.data["last_run_id"] = run_id
    data.sources[context.source_id] = {
        "source_dir": platform_support.normalize(context.paths.source_dir),
        "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requested_bundles": list(context.selection.bundles),
        "requested_skills": list(context.selection.skills),
        "selected_skills": selected_skills,
        "skills": dict(sorted(skills.items())),
        "shared": dict(sorted(shared.items())),
        manifest.ADAPTERS: dict(sorted(adapter_entries.items())),
        "agents": dict(sorted(agent_entries.items())),
    }
    data.save()


def _finalize(context: Context, record: journal.Journal, run_id: str) -> list[tuple[str, str]]:
    """Move retained backups to permanent storage and return each (item, backup destination)."""
    backups: list[tuple[str, str]] = []
    for entry in list(record.entries):
        if entry["op"] != "backup" or not entry["retain"]:
            continue
        root = entry.get("root", "claude")
        item = entry["item"]
        transient = journal.root_directory(context.paths, root) / f"{item}.deploying-bak"
        try:
            destination = journal.prepare_backup_destination(context.paths, run_id, item, root)
        except DeployError as exc:
            raise DeployError(*exc.lines, f"ERROR: Refusing unsafe permanent backup destination for {item}") from exc
        if os.path.lexists(transient):
            if os.path.lexists(destination):
                raise DeployError(
                    f"ERROR: Permanent backup destination already exists: {platform_support.normalize(destination)}",
                    "Move it out of the deployment root after checking its contents, then retry.",
                    see_recovery("Backups"),
                )
            record.preserve(root, item)
            fsops.move(transient, destination)
            backups.append((item, entry["backup_dest"]))
    for entry in record.entries:
        if entry["op"] == "backup" and not entry["retain"]:
            fsops.remove(
                journal.root_directory(context.paths, entry.get("root", "claude")) / f"{entry['item']}.deploying-bak"
            )
    return backups


def _summary(context: Context, run_id: str, entries: list[PlanEntry], backups: list[tuple[str, str]]) -> None:
    print_report(DEPLOYED, DEPLOY_ACTIONS, [entry.report_line(DEPLOYED) for entry in entries])
    if backups:
        print(f"BACKED UP ({len(backups)}):")
        for item, destination in sorted(backups):
            print(f"  {item}: {destination}")
        print("")
    print(f"Run ID: {run_id}")
    print(f"Manifest: {platform_support.normalize(context.paths.manifest_file)}")
    print("")


def _migration_candidates(context: Context, old_entry: dict, kind: str, label: str, base: Path) -> list[str]:
    restore = (
        "Restore the copy the old source deployed, for example by deploying from that source again, "
        "then rerun --migrate-from.",
        see_recovery("Ownership held by another source"),
    )
    candidates: list[str] = []
    for name in sorted(old_entry.get(kind) or {}):
        if kind == "shared":
            require_safe_name(name, "migration shared asset")
            if context.source.shared_assets.get(name) != "owner":
                continue
            if not (context.paths.skills_src / name).is_file():
                raise DeployError(f"ERROR: Cannot migrate shared asset '{name}': source owner file is missing.")
            if (old_entry["shared"][name] or {}).get("role") != "owner":
                continue
        elif kind == "agents":
            if not name.endswith(".md") or name[: -len(".md")] not in context.source.agents:
                continue
        elif name not in context.source.skills:
            continue
        expected = (old_entry[kind][name] or {}).get("hash", "")
        subject = f"{label} '{name}'" if label else f"'{name}'"
        if not isinstance(expected, str) or not hashing.HASH_PATTERN.fullmatch(expected):
            raise DeployError(f"ERROR: Old manifest has no valid hash for {label or 'skill'} '{name}'.")
        destination = base / name
        present = destination.is_file() if kind in ("shared", "agents") else destination.is_dir()
        if not present:
            raise DeployError(f"ERROR: Cannot migrate {subject}: destination is missing.", *restore)
        if hashing.hash_path(destination) != expected:
            raise DeployError(f"ERROR: Cannot migrate {subject}: destination differs from the old manifest.", *restore)
        candidates.append(name)
    return candidates


def _migrate(context: Context, old: str) -> None:
    paths, data = context.paths, context.manifest
    if not paths.manifest_file.is_file():
        raise DeployError(
            "ERROR: Cannot migrate without an existing manifest.",
            "Nothing is deployed in this home, so there is nothing to take over: deploy without --migrate-from.",
        )
    old_entry = data.source(old)
    if old_entry is None:
        raise DeployError(
            f"ERROR: Migration source '{old}' is not present in the manifest.",
            f'Name a source ID listed under "sources" in {platform_support.normalize(paths.manifest_file)}.',
        )
    skills = _migration_candidates(context, old_entry, "skills", "", paths.dest_dir)
    shared = _migration_candidates(context, old_entry, "shared", "shared asset", paths.dest_dir)
    adapters = _migration_candidates(context, old_entry, manifest.ADAPTERS, ADAPTER, paths.adapter_dest_dir)
    agents = _migration_candidates(context, old_entry, "agents", AGENT, paths.agent_dest_dir)
    if not (skills or shared or adapters or agents):
        print("")
        print(f"No intersecting ownership entries to migrate from '{old}'.")
        print("")
        return
    new_entry = data.sources.setdefault(
        context.source_id,
        {
            "source_dir": "",
            "deployed_at": None,
            "requested_bundles": [],
            "requested_skills": [],
            "selected_skills": [],
            "skills": {},
            "shared": {},
            manifest.ADAPTERS: {},
            "agents": {},
        },
    )
    new_entry["source_dir"] = platform_support.normalize(paths.source_dir)
    for name in skills:
        new_entry.setdefault("skills", {})[name] = old_entry["skills"].pop(name)
        for field_name in ("selected_skills", "requested_skills"):
            if name in old_entry.get(field_name, []):
                new_entry[field_name] = sorted(set(new_entry.get(field_name, [])) | {name})
                old_entry[field_name] = [value for value in old_entry[field_name] if value != name]
    for name in shared:
        new_entry.setdefault("shared", {})[name] = old_entry["shared"].pop(name)
    for name in adapters:
        new_entry.setdefault(manifest.ADAPTERS, {})[name] = old_entry[manifest.ADAPTERS].pop(name)
    for name in agents:
        new_entry.setdefault("agents", {})[name] = old_entry["agents"].pop(name)
    # A bundle request alone does not keep the old source: it names no item, and the new source counts a bundle whose
    # members it owns as chosen.
    if not any(
        old_entry.get(key)
        for key in ("skills", "shared", manifest.ADAPTERS, "agents", "requested_skills", "selected_skills")
    ):
        del data.sources[old]
    data.save()
    print("")
    print(f"Moved ownership from '{old}' to '{context.source_id}'.")
    print_report(
        "MIGRATED",
        ("MIGRATED",),
        [
            *(ReportLine(name, "MIGRATED") for name in skills),
            *(ReportLine(name, "MIGRATED", kind="shared asset") for name in shared),
            *(ReportLine(name, "MIGRATED", kind=ADAPTER) for name in adapters),
            *(ReportLine(name, "MIGRATED", kind=AGENT) for name in agents),
        ],
    )


def _deploy(
    paths: Paths, options: Options, src: source.Source, values: dict[str, str], stdin: TextIO, run_id: str | None
) -> int:
    data = manifest.load(paths.manifest_file)
    context = Context(paths, options, src, values, data, data.ownership(src.source_id), stdin)
    if options.migrate_from:
        _migrate(context, options.migrate_from)
        return 0
    selection = _select(context)
    if selection is None:
        return 0
    context.selection = selection
    selected = [] if selection.deselect_all else source.expand(src, selection.bundles, selection.skills)
    adapters = [name for name in selected if src.skills[name].selectable]
    shadowed = [name for name in adapters if os.path.lexists(paths.copilot_skills_dir / name)]
    if shadowed:
        print("", file=sys.stderr)
        print(
            "WARNING: Higher-priority GitHub Copilot personal skills shadow generated runtime adapters:",
            file=sys.stderr,
        )
        for name in shadowed:
            print(f"  - {name} ({platform_support.normalize(paths.copilot_skills_dir / name)})", file=sys.stderr)
        print(
            "The deployer will not modify ~/.copilot/skills. Resolve these paths manually, then check "
            "which copy Copilot uses with 'python deploy.py verify'.",
            file=sys.stderr,
        )
    _print_selection(src, selection, selected)
    _warn_missing_tools(src, selection)
    _require_variables(context, selected)
    staged_shared = _shared_to_stage(context, selected, _validate_shared_assets(context, selected))
    agents = source.agent_closure(src, selected)
    _check_ownership(context, selected, adapters, staged_shared, agents)
    staged = render.render(src, selected, adapters, staged_shared, values, paths.skills_src, agents)
    render.reject_unexpanded_tokens(staged)
    render.validate_executables(staged)
    print("")
    if run_id is None:
        print("Rendered and validated in memory (dry run).")
    else:
        print("Rendered and validated.")
    # Planned last, after the slow validation, so the destinations it read are the ones the deployment changes.
    entries = _plan(context, selected, adapters, staged, staged_shared, agents)
    if run_id is None:
        _dry_run(entries)
        return 0
    _apply(context, selected, entries, staged, run_id)
    return 0


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def warn_ignored_home(home: Path) -> None:
    """Warn on stderr when HOME names another directory than the profile folder this run uses."""
    ignored = platform_support.ignored_home_variable(home)
    if ignored is None:
        return
    # Flush first, so the warning cannot appear out of order when both streams go to one terminal or file.
    sys.stdout.flush()
    print("", file=sys.stderr)
    print(
        textwrap.fill(
            f"WARNING: HOME is set to {ignored}, but Claude Code, Codex, and Copilot CLI read skills from "
            f"{platform_support.RUNTIME_HOME}, so this run uses {platform_support.normalize(home)}. To deploy into a "
            "throwaway home instead, use --canary-home.",
            width=REPORT_WIDTH,
            break_long_words=False,
            break_on_hyphens=False,
        ),
        file=sys.stderr,
    )
    sys.stderr.flush()


def _print_source(src: source.Source, home: Path) -> None:
    print("")
    print(f"Source: {source.label(src.source_id, src.name)}")
    print(f"Home: {platform_support.normalize(home)}")
    found = _plural(len(src.skills), "skill")
    if src.bundles:
        found += f" and {_plural(len(src.bundles), 'bundle')}"
    print(f"Found {found}.")
    warn_ignored_home(home)


def _warn_missing_tools(src: source.Source, selection: Selection) -> None:
    """Warn, without stopping, when a selected skill runs a required tool that is not installed."""
    if selection.deselect_all:
        return
    required = source.required_tools(src, selection.bundles, selection.skills)
    missing = {
        name: roots
        for name, roots in source.tool_users(src, selection.bundles, selection.skills).items()
        if name in required and not tools.SKILL_TOOLS[name].optional and tools.SKILL_TOOLS[name].locate() is None
    }
    if not missing:
        return
    print("", file=sys.stderr)
    print("WARNING: Selected skills use tools that are not installed:", file=sys.stderr)
    for name, roots in missing.items():
        print(_wrap(f"  {name} (used by {', '.join(roots)})"), file=sys.stderr)
    print("Those skills will fail until the tools are installed.", file=sys.stderr)
    print(f"Run '{CHECK_COMMAND_LINE}' for details.", file=sys.stderr)


def _print_selection(src: source.Source, selection: Selection, selected: list[str]) -> None:
    """Show what was requested, with bundle members and pulled-in dependencies indented beneath it."""
    print("")
    if selection.deselect_all:
        print("Selected nothing; unmodified items this source deployed will be removed.")
        return

    def print_dependencies(roots: list[str]) -> None:
        for dependency in sorted(set(source.expand(src, [], roots)) - set(roots)):
            print(f"    {dependency} (dependency)")

    items = len(selection.bundles) + len(selection.skills)
    print(f"Selected {_plural(items, 'item')} ({_plural(len(selected), 'skill')}):")
    for bundle in sorted(selection.bundles):
        print(f"  {bundle} (bundle)")
        for member in sorted(src.bundles[bundle]):
            print(f"    {member}")
        print_dependencies(src.bundles[bundle])
    for skill in sorted(selection.skills):
        print(f"  {skill}")
        print_dependencies([skill])


RECOVERY_FAILED = "When recovery fails"


def _stop_for_pending_recovery(paths: Paths) -> bool:
    """Say which interrupted runs the next deployment will reconcile, and stop a dry run when there are any.

    Recovery changes what is installed, so a preview planned before it would describe the wrong tree, and its leftover
    .deploying-bak files would read as stale backups to delete by hand.
    """
    pending = journal.pending_runs(paths)
    if not pending:
        return False
    stuck = [run_id for run_id, recoverable in pending if not recoverable]
    lines = [
        *(
            f"Pending recovery: the next deployment will recover run {run_id}."
            for run_id, recoverable in pending
            if recoverable
        ),
        *(
            f"ERROR: Run {run_id} cannot be recovered automatically; the next deployment will stop until it is "
            "reconciled by hand."
            for run_id in stuck
        ),
        "The dry run stops here: recovery changes what is installed, so a preview before it would be wrong.",
        see_recovery(RECOVERY_FAILED if stuck else "Interrupted deployments"),
    ]
    if stuck:
        raise DeployError(*lines)
    print("")
    print(*lines, sep="\n")
    print("")
    return True


def _reject_other_checkout(paths: Paths, source_id: str, take_over: bool) -> None:
    """Refuse a checkout other than the one the manifest records for this source, unless the user takes it over.

    Two clones share a source ID and a configuration, so a deployment from the wrong one would silently re-point every
    installed skill at it and remove whatever it lacks.
    """
    entry = manifest.load(paths.manifest_file).source(source_id) or {}
    recorded = entry.get("source_dir")
    if not isinstance(recorded, str) or not recorded:
        return
    if platform_support.same_directory(Path(recorded), paths.source_dir):
        return
    current = platform_support.normalize(paths.source_dir)
    if not take_over:
        gone = "" if Path(recorded).is_dir() else ", which no longer exists"
        raise DeployError(
            f"ERROR: Source '{source_id}' is deployed from {recorded}{gone}, not from this checkout, {current}.",
            f"Deploy from {recorded}; or, if this checkout replaces it, rerun with {TAKE_OVER_SOURCE} to record this "
            "checkout as its source.",
            see_recovery("Deploying from another checkout"),
        )
    owned = ", ".join(
        _plural(len(entry.get(kind) or {}), noun)
        for kind, noun in (
            ("skills", "skill"),
            ("shared", "shared asset"),
            (manifest.ADAPTERS, "runtime adapter"),
            ("agents", "agent"),
        )
    )
    print("")
    print(f"Taking over source '{source_id}' from {recorded}: {owned}.")
    print(f"This checkout, {current}, is recorded as their source when this deployment commits.")


def run(
    arguments: list[str],
    paths: Paths,
    *,
    probe: lock.ProcessProbe = platform_support.process_status,
    stdin: TextIO | None = None,
) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    try:
        platform_support.ensure_supported()
        source_id = source.load_source_id(paths)
        options = parse_arguments(arguments, source_id)
        if options.canary_home:
            paths = Paths(paths.source_dir, canary_home(options.canary_home))
        validate_managed_roots(paths)
        # A canary home's values are set when it is claimed, after every check; the real configuration is unread.
        values = (
            {}
            if options.canary_home
            else config.load(paths.config_file(source_id), source_id, paths.home, paths.source_dir)
        )
        src = source.discover(paths, source_id)
    except ParserExit as exc:
        return exc.code
    except DeployError as exc:
        print_error(exc)
        return exc.exit_code
    _print_source(src, paths.home)
    if options.dry_run:
        try:
            if _stop_for_pending_recovery(paths):
                return 0
            return _deploy(paths, options, src, values, stdin, None)
        except DeployError as exc:
            print_error(exc)
            return exc.exit_code
    try:
        if options.canary_home:
            # A throwaway home is discarded with its recorded source path, so a linked worktree may deploy into it.
            claim_canary_home(paths.home)
            values = config.canary(source_id, paths.home, paths.source_dir)
        else:
            source.reject_linked_worktree(paths)
        held = lock.acquire(paths, probe)
    except DeployError as exc:
        print_error(exc)
        return exc.exit_code
    try:
        _reject_other_checkout(paths, source_id, options.take_over_source)
    except DeployError as exc:
        print_error(exc)
        held.release()
        return exc.exit_code
    if not journal.recover_incomplete(paths):
        print("", file=sys.stderr)
        print("ERROR: Recovery failed, so nothing was deployed.", file=sys.stderr)
        print(
            f"Reconcile the run named above by hand, then rerun with --dry-run. {see_recovery(RECOVERY_FAILED)}",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        held.release()
        return 1
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    try:
        code = _deploy(paths, options, src, values, stdin, run_id)
    except Cancelled as exc:
        # Raised only at the selection prompt, before staging or any destination change.
        print_error(exc)
        held.release()
        return exc.exit_code
    except (Exception, KeyboardInterrupt) as exc:
        if isinstance(exc, DeployError):
            print_error(exc)
            code = exc.exit_code
        else:
            print(f"ERROR: Unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
            code = 130 if isinstance(exc, KeyboardInterrupt) else 1
        print("Deployment failed; reconciling the current journal before exit...", file=sys.stderr)
        if journal.recover_incomplete(paths):
            held.release()
        else:
            print("ERROR: Immediate recovery failed; deployment evidence and lock were retained.", file=sys.stderr)
            print(
                "The next run reclaims the lock and retries recovery. If that fails too, "
                f"reconcile the run by hand. {see_recovery(RECOVERY_FAILED)}",
                file=sys.stderr,
            )
        print("", file=sys.stderr)
        return code
    held.release()
    return code

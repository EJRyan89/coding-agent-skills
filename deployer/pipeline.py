"""The deployment pipeline: select, render, validate, and apply under a journal."""

from __future__ import annotations

import difflib
import os
import secrets
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TextIO

from . import config, fsops, hashing, journal, lock, manifest, platform_support, render, source, tools
from .arguments import CHECK_COMMAND_LINE, CONFIGURE_COMMAND_LINE, PROG, ParserExit, deploy_parser
from .errors import Cancelled, DeployError, print_error, see_recovery
from .names import require_safe_name
from .paths import Paths, canary_home, claim_canary_home, validate_managed_roots


@dataclass
class Options:
    select_all: bool = False
    force: bool = False
    force_items: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    dry_run: bool = False
    migrate_from: str = ""
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
    if options.canary_home:
        for flag, used in (("--dry-run", options.dry_run), ("--migrate-from", options.migrate_from)):
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
    owned: manifest.Ownership
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
                    f"ERROR: Skill '{name}' declares shared_dep '{dependency}' but it is not in source.json shared_assets"
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
AGENT = "agent"
KIND_ORDER = {"": 0, ADAPTER: 1, "shared asset": 2, AGENT: 3}


def _detail(*parts: str) -> str:
    return ", ".join(part for part in parts if part)


MODIFIED = "modified since last deploy"
DIFFERS = "unmanaged and differs"
DIFFERS_FROM_SKILL = "unmanaged and differs from the rendered skill"
# Reasons that --force or --force-item resolve; a destination of the wrong type needs manual cleanup instead.
FORCEABLE = {
    "CONFLICT": frozenset({MODIFIED, "differs"}),
    "BOOTSTRAP DIFF": frozenset({"unmanaged, differs"}),
    "SKIPPED": frozenset({MODIFIED, DIFFERS, DIFFERS_FROM_SKILL}),
}
FORCE_HINTS = {
    "DRY RUN": "Replace conflicting items with --force-item NAME or --force (backups are kept).",
    "DEPLOYED": "Replace skipped items with --force-item NAME or --force (backups are kept).",
}

ATTENTION_ACTIONS = frozenset({"CONFLICT", "BOOTSTRAP DIFF", "PRESERVE", "SKIPPED", "PRESERVED", "REPLACED"})
SKIPPED_WITH_SKILL = "its skill was skipped"


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


DRY_RUN_ACTIONS = (
    "CONFLICT",
    "BOOTSTRAP DIFF",
    "PRESERVE",
    "REMOVE",
    "UPDATE",
    "FRESH INSTALL",
    "ADOPT",
    "DROP OWNERSHIP",
    "KEEP",
    "EXISTS",
    "UNCHANGED",
)


def _dry_run(
    context: Context,
    selected: list[str],
    adapters: list[str],
    staged: render.Staged,
    plan: SharedPlan,
    agents: list[str],
) -> None:
    paths, owned = context.paths, context.owned
    lines: list[ReportLine] = []

    def report(name: str, action: str, detail: str = "", extra: list[str] | None = None, kind: str = "") -> None:
        lines.append(ReportLine(name, action, detail, tuple(extra or ()), kind))

    for name in selected:
        destination = paths.dest_dir / name
        if destination.is_dir():
            existing = hashing.hash_path(destination)
            if name in owned.skills:
                if existing != owned.skills[name]:
                    report(name, "CONFLICT", MODIFIED)
                elif existing == staged.skill_hash(name):
                    report(name, "UNCHANGED")
                else:
                    report(name, "UPDATE")
            elif existing == staged.skill_hash(name):
                report(name, "ADOPT", "unmanaged, byte-identical")
            else:
                report(
                    name,
                    "BOOTSTRAP DIFF",
                    "unmanaged, differs",
                    _tree_diff(destination, staged.skills[name], f"staged/{name}"),
                )
        elif os.path.lexists(destination):
            report(name, "CONFLICT", "destination is not a directory")
        else:
            report(name, "FRESH INSTALL")
    for name in adapters:
        destination = paths.adapter_dest_dir / name
        if destination.is_dir():
            existing = hashing.hash_path(destination)
            if owned.adapters.get(name) == existing:
                report(name, "UNCHANGED" if existing == staged.adapter_hash(name) else "UPDATE", kind=ADAPTER)
            elif existing == staged.adapter_hash(name):
                report(name, "ADOPT", "byte-identical", kind=ADAPTER)
            else:
                report(name, "CONFLICT", "differs", kind=ADAPTER)
        elif os.path.lexists(destination):
            report(name, "CONFLICT", "destination is not a directory", kind=ADAPTER)
        else:
            report(name, "FRESH INSTALL", kind=ADAPTER)
    for name in agents:
        destination = paths.agent_dest_dir / name
        if destination.is_file():
            existing = hashing.hash_path(destination)
            if name in owned.agents:
                if existing != owned.agents[name]:
                    report(name, "CONFLICT", MODIFIED, kind=AGENT)
                else:
                    report(name, "UNCHANGED" if existing == staged.agent_hash(name) else "UPDATE", kind=AGENT)
            elif existing == staged.agent_hash(name):
                report(name, "ADOPT", "unmanaged, byte-identical", kind=AGENT)
            else:
                report(name, "CONFLICT", "differs", kind=AGENT)
        elif os.path.lexists(destination):
            report(name, "CONFLICT", "destination is not a file", kind=AGENT)
        else:
            report(name, "FRESH INSTALL", kind=AGENT)
    for name in sorted(owned.agents):
        if name in agents:
            continue
        destination = paths.agent_dest_dir / name
        if destination.is_file():
            if hashing.hash_path(destination) == owned.agents[name]:
                report(name, "REMOVE", "no selected skill needs it", kind=AGENT)
            else:
                report(name, "PRESERVE", "modified", kind=AGENT)
        elif os.path.lexists(destination):
            report(name, "PRESERVE", "unexpected destination type", kind=AGENT)
        else:
            report(name, "DROP OWNERSHIP", "already absent", kind=AGENT)
    for asset in sorted(staged.shared):
        destination = paths.dest_dir / asset
        if not os.path.lexists(destination):
            action = "FRESH INSTALL"
        elif destination.is_file() and hashing.hash_path(destination) == staged.shared_hash(asset):
            action = "UNCHANGED"
        else:
            action = "EXISTS"
        report(asset, action, kind="shared asset")
    for name in sorted(owned.skills):
        if name in selected:
            continue
        destination = paths.dest_dir / name
        if destination.is_dir():
            if hashing.hash_path(destination) == owned.skills[name]:
                report(name, "REMOVE", "deselected or absent from source")
            else:
                report(name, "PRESERVE", "deselected but modified")
        elif os.path.lexists(destination):
            report(name, "PRESERVE", "unexpected destination type")
        else:
            report(name, "DROP OWNERSHIP", "destination already absent")
    for asset in sorted(owned.shared):
        if asset in plan.retained:
            report(asset, "KEEP", f"needed by {plan.retained[asset]}", kind="shared asset")
        if asset in plan.keep:
            continue
        destination = paths.dest_dir / asset
        if destination.is_file():
            if hashing.hash_path(destination) == owned.shared[asset]:
                report(asset, "REMOVE", "obsolete", kind="shared asset")
            else:
                report(asset, "PRESERVE", "obsolete but modified", kind="shared asset")
        elif os.path.lexists(destination):
            report(asset, "PRESERVE", "unexpected destination type", kind="shared asset")
        else:
            report(asset, "DROP OWNERSHIP", "already absent", kind="shared asset")
    for name in sorted(owned.adapters):
        if name in adapters:
            continue
        destination = paths.adapter_dest_dir / name
        if destination.is_dir():
            if hashing.hash_path(destination) == owned.adapters[name]:
                report(name, "REMOVE", "obsolete", kind=ADAPTER)
            else:
                report(name, "PRESERVE", "modified", kind=ADAPTER)
        else:
            report(name, "DROP OWNERSHIP", "already absent", kind=ADAPTER)
    print_report("DRY RUN", DRY_RUN_ACTIONS, lines)


DEPLOY_ACTIONS = (
    "SKIPPED",
    "PRESERVED",
    "REPLACED",
    "REMOVED",
    "UPDATED",
    "INSTALLED",
    "ADOPTED",
    "DROPPED OWNERSHIP",
    "KEPT",
    "UNCHANGED",
)


@dataclass
class Outcome:
    lines: list[ReportLine] = field(default_factory=list)
    skipped_skills: set[str] = field(default_factory=set)
    skipped_shared: set[str] = field(default_factory=set)
    skipped_adapters: set[str] = field(default_factory=set)
    skipped_agents: set[str] = field(default_factory=set)
    removed_skills: set[str] = field(default_factory=set)
    removed_shared: set[str] = field(default_factory=set)
    removed_adapters: set[str] = field(default_factory=set)
    removed_agents: set[str] = field(default_factory=set)

    def add(self, name: str, action: str, detail: str = "", kind: str = "") -> None:
        self.lines.append(ReportLine(name, action, detail, kind=kind))


def _ensure_transient_available(item: str, base: Path) -> None:
    transient = base / f"{item}.deploying-bak"
    if os.path.lexists(transient):
        raise DeployError(
            f"ERROR: Refusing to deploy '{item}': transient backup already exists at {platform_support.normalize(transient)}",
            "Resolve or remove the stale backup after verifying its contents, then retry.",
            see_recovery("Backups"),
        )


@dataclass
class SharedPlan:
    """Which shared assets this run installs, and which owned assets it keeps for skills that stay installed."""

    staged: list[str]
    retained: dict[str, str]

    @property
    def keep(self) -> set[str]:
        return set(self.staged) | set(self.retained)


def _surviving_skills(context: Context, selected: list[str]) -> list[str]:
    """Owned skills whose current installed copy will remain after this run: preserved or skipped as modified."""
    survivors: list[str] = []
    for name, owned_hash in sorted(context.owned.skills.items()):
        destination = context.paths.dest_dir / name
        if not os.path.lexists(destination):
            continue
        if not destination.is_dir() or hashing.find_link(destination) is not None:
            survivors.append(name)
        elif hashing.hash_path(destination) != owned_hash and not (name in selected and context.forced(name)):
            survivors.append(name)
    return survivors


def _plan_shared(context: Context, selected: list[str], assets: dict[str, str]) -> SharedPlan:
    needed = {dependency for name in selected for dependency in context.source.skills[name].shared_deps}
    staged = sorted(asset for asset in needed if assets.get(asset) == "owner")
    required_by: dict[str, str] = {}
    for name in _surviving_skills(context, selected):
        for asset in context.owned.skill_shared_deps.get(name, []):
            required_by.setdefault(asset, name)
    for other, entry in sorted(context.manifest.sources.items()):
        if other == context.source_id:
            continue
        for skill_entry in (entry.get("skills") or {}).values():
            for asset in skill_entry.get("shared_deps", []):
                required_by.setdefault(asset, f"source {other}")
    retained = {
        asset: required_by[asset]
        for asset in sorted(context.owned.shared)
        if asset not in staged and asset in required_by
    }
    return SharedPlan(staged=staged, retained=retained)


def _check_ownership(
    context: Context, selected: list[str], adapters: list[str], plan: SharedPlan, agents: list[str]
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
    for asset in plan.staged:
        owner = data.shared_owners.get(asset)
        if owner is not None and owner != source_id:
            raise transfer(f"Shared asset '{asset}'", owner)
        if asset in data.skill_owners:
            raise collision(f"Shared asset '{asset}'", "a skill", data.skill_owners[asset])
        _ensure_transient_available(asset, paths.dest_dir)
    for asset in sorted(owned.shared):
        if asset not in plan.staged:
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
        *(paths.dest_dir / asset for asset in [*plan.staged, *owned.shared]),
        *(paths.adapter_dest_dir / name for name in [*adapters, *owned.adapters]),
        *(paths.agent_dest_dir / name for name in [*agents, *owned.agents]),
    ]
    for destination in candidates:
        link = hashing.find_link(destination) if os.path.lexists(destination) else None
        if link is not None:
            raise DeployError(
                f"ERROR: Destination '{destination.name}' contains a symlink or junction: {platform_support.normalize(link)}",
                "Remove it or restore the deployed copy after verifying its contents, then retry.",
            )


def _remove_obsolete(
    record: journal.Journal,
    root: str,
    base: Path,
    owned: dict[str, str],
    keep: set[str],
    is_expected_type: Callable[[Path], bool],
    labels: tuple[str, str, str],
    outcome: Outcome,
    removed: set[str],
    skipped: set[str],
) -> None:
    kind, removal_reason, wrong_type = labels
    for name in sorted(owned):
        if name in keep:
            continue
        destination = base / name
        if is_expected_type(destination):
            existing = hashing.hash_path(destination)
            if existing == owned[name]:
                record.backup(root, name, existing, retain=False)
                fsops.move(destination, base / f"{name}.deploying-bak")
                removed.add(name)
                outcome.add(name, "REMOVED", removal_reason, kind)
            else:
                outcome.add(name, "PRESERVED", MODIFIED, kind)
                skipped.add(name)
        elif os.path.lexists(destination):
            outcome.add(name, "PRESERVED", f"destination is not {wrong_type}", kind)
            skipped.add(name)
        else:
            removed.add(name)
            outcome.add(name, "DROPPED OWNERSHIP", "already absent", kind)


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


def _apply_item(
    context: Context,
    record: journal.Journal,
    root: str,
    name: str,
    staged_path: Path,
    staged_hash: str,
    owned_hash: str | None,
    is_expected_type: Callable[[Path], bool],
    labels: dict[str, str],
) -> ReportLine:
    """Install one item and return the report line describing what happened to it."""
    kind = labels["kind"]
    base = journal.root_directory(context.paths, root)
    destination = base / name
    if not os.path.lexists(destination):
        _replace(record, root, base, staged_path, name, staged_hash, None, False)
        return ReportLine(name, "INSTALLED", kind=kind)
    if not is_expected_type(destination):
        return ReportLine(name, "SKIPPED", f"destination is not {labels['type']}", kind=kind)
    existing = hashing.hash_path(destination)
    if owned_hash is not None:
        if existing == owned_hash:
            _replace(record, root, base, staged_path, name, staged_hash, existing, False)
            return ReportLine(name, "UNCHANGED" if existing == staged_hash else "UPDATED", kind=kind)
        if context.forced(name):
            _replace(record, root, base, staged_path, name, staged_hash, existing, True)
            return ReportLine(name, "REPLACED", "forced, previous copy backed up", kind=kind)
        return ReportLine(name, "SKIPPED", labels["modified"], kind=kind)
    if existing == staged_hash:
        _replace(record, root, base, staged_path, name, staged_hash, existing, False)
        return ReportLine(name, "ADOPTED", "byte-identical", kind=kind)
    if context.forced(name):
        _replace(record, root, base, staged_path, name, staged_hash, existing, True)
        return ReportLine(name, "REPLACED", "forced, was unmanaged, previous copy backed up", kind=kind)
    diff = _tree_diff(destination, hashing.read_tree(staged_path), labels["diff"])[:40] if "diff" in labels else []
    return ReportLine(name, "SKIPPED", labels["unmanaged"], tuple(diff), kind)


def _apply(
    context: Context,
    selected: list[str],
    adapters: list[str],
    staged: render.Staged,
    plan: SharedPlan,
    run_id: str,
    agents: list[str],
) -> None:
    paths, owned = context.paths, context.owned
    staging_dir = paths.staging_root / run_id
    staged.write(staging_dir)
    record = journal.Journal(staging_dir / "journal.jsonl", run_id)
    record.create()
    fsops.make_directories(paths.dest_dir)
    fsops.make_directories(paths.adapter_dest_dir)
    fsops.make_directories(paths.agent_dest_dir)
    outcome = Outcome()
    is_dir = Path.is_dir
    is_file = Path.is_file
    _remove_obsolete(
        record,
        "claude",
        paths.dest_dir,
        owned.skills,
        set(selected),
        is_dir,
        ("", "deselected or absent from source", "a directory"),
        outcome,
        outcome.removed_skills,
        outcome.skipped_skills,
    )
    for asset, reason in plan.retained.items():
        outcome.add(asset, "KEPT", f"needed by {reason}", "shared asset")
    _remove_obsolete(
        record,
        "claude",
        paths.dest_dir,
        owned.shared,
        plan.keep,
        is_file,
        ("shared asset", "obsolete", "a file"),
        outcome,
        outcome.removed_shared,
        outcome.skipped_shared,
    )
    _remove_obsolete(
        record,
        "agents",
        paths.adapter_dest_dir,
        owned.adapters,
        set(adapters),
        is_dir,
        (ADAPTER, "obsolete", "a directory"),
        outcome,
        outcome.removed_adapters,
        outcome.skipped_adapters,
    )
    _remove_obsolete(
        record,
        "claude-agents",
        paths.agent_dest_dir,
        owned.agents,
        set(agents),
        is_file,
        (AGENT, "no selected skill needs it", "a file"),
        outcome,
        outcome.removed_agents,
        outcome.skipped_agents,
    )

    skill_labels = {
        "kind": "",
        "type": "a directory",
        "modified": MODIFIED,
        "unmanaged": DIFFERS_FROM_SKILL,
    }
    for name in selected:
        labels = dict(skill_labels, diff=f"staged/{name}")
        line = _apply_item(
            context,
            record,
            "claude",
            name,
            staging_dir / name,
            staged.skill_hash(name),
            owned.skills.get(name),
            is_dir,
            labels,
        )
        _record(outcome, line, outcome.skipped_skills)

    adapter_labels = {
        "kind": ADAPTER,
        "type": "a directory",
        "modified": MODIFIED,
        "unmanaged": DIFFERS,
    }
    for name in adapters:
        if name in outcome.skipped_skills:
            _record(outcome, ReportLine(name, "SKIPPED", SKIPPED_WITH_SKILL, kind=ADAPTER), outcome.skipped_adapters)
            continue
        line = _apply_item(
            context,
            record,
            "agents",
            name,
            staging_dir / render.ADAPTER_STAGING / name,
            staged.adapter_hash(name),
            owned.adapters.get(name),
            is_dir,
            adapter_labels,
        )
        _record(outcome, line, outcome.skipped_adapters)

    shared_labels = {
        "kind": "shared asset",
        "type": "a file",
        "modified": MODIFIED,
        "unmanaged": DIFFERS,
    }
    for asset in sorted(staged.shared):
        line = _apply_item(
            context,
            record,
            "claude",
            asset,
            staging_dir / asset,
            staged.shared_hash(asset),
            owned.shared.get(asset),
            is_file,
            shared_labels,
        )
        _record(outcome, line, outcome.skipped_shared)

    agent_labels = {
        "kind": AGENT,
        "type": "a file",
        "modified": MODIFIED,
        "unmanaged": DIFFERS,
    }
    for name in agents:
        line = _apply_item(
            context,
            record,
            "claude-agents",
            name,
            staging_dir / render.AGENT_STAGING / name,
            staged.agent_hash(name),
            owned.agents.get(name),
            is_file,
            agent_labels,
        )
        _record(outcome, line, outcome.skipped_agents)

    _commit_manifest(context, selected, adapters, staged, plan, outcome, run_id, agents)
    backups = _finalize(context, record, run_id)
    fsops.remove(staging_dir)
    _summary(context, run_id, outcome, backups)


def _record(outcome: Outcome, line: ReportLine, skipped: set[str]) -> None:
    outcome.lines.append(line)
    if line.action == "SKIPPED":
        skipped.add(line.name)


def _commit_manifest(
    context: Context,
    selected: list[str],
    adapters: list[str],
    staged: render.Staged,
    plan: SharedPlan,
    outcome: Outcome,
    run_id: str,
    agents: list[str],
) -> None:
    owned, data = context.owned, context.manifest
    skills = {
        name: {"hash": value, "shared_deps": list(owned.skill_shared_deps.get(name, []))}
        for name, value in owned.skills.items()
    }
    for name in selected:
        if name not in outcome.skipped_skills:
            skills[name] = {
                "hash": staged.skill_hash(name),
                "shared_deps": sorted(context.source.skills[name].shared_deps),
            }
    for name in outcome.removed_skills:
        skills.pop(name, None)
    shared = {
        name: {"hash": value, "role": owned.shared_roles.get(name, "owner")} for name, value in owned.shared.items()
    }
    for asset in staged.shared:
        if asset not in outcome.skipped_shared:
            shared[asset] = {"hash": staged.shared_hash(asset), "role": "owner"}
    for name in outcome.removed_shared:
        shared.pop(name, None)
    adapter_entries = {name: {"hash": value} for name, value in owned.adapters.items()}
    for name in adapters:
        if name not in outcome.skipped_adapters:
            adapter_entries[name] = {"hash": staged.adapter_hash(name)}
    for name in outcome.removed_adapters:
        adapter_entries.pop(name, None)
    agent_entries = {name: {"hash": value} for name, value in owned.agents.items()}
    for name in agents:
        if name not in outcome.skipped_agents:
            agent_entries[name] = {"hash": staged.agent_hash(name)}
    for name in outcome.removed_agents:
        agent_entries.pop(name, None)
    selected_skills = list(owned.selected_skills)
    for name in selected:
        if name not in selected_skills:
            selected_skills.append(name)
    selected_skills = [name for name in selected_skills if name not in outcome.removed_skills]
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


def _summary(context: Context, run_id: str, outcome: Outcome, backups: list[tuple[str, str]]) -> None:
    print_report("DEPLOYED", DEPLOY_ACTIONS, outcome.lines)
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
    if not any(
        old_entry.get(key)
        for key in (
            "skills",
            "shared",
            manifest.ADAPTERS,
            "agents",
            "requested_bundles",
            "requested_skills",
            "selected_skills",
        )
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
    plan = _plan_shared(context, selected, _validate_shared_assets(context, selected))
    agents = source.agent_closure(src, selected)
    _check_ownership(context, selected, adapters, plan, agents)
    staged = render.render(src, selected, adapters, plan.staged, values, paths.skills_src, agents)
    render.reject_unexpanded_tokens(staged)
    render.validate_executables(staged)
    print("")
    if run_id is None:
        print("Rendered and validated in memory (dry run).")
    else:
        print("Rendered and validated.")
    if run_id is None:
        _dry_run(context, selected, adapters, staged, plan, agents)
        return 0
    _apply(context, selected, adapters, staged, plan, run_id, agents)
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

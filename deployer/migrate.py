"""--migrate-from: move ownership of the items two sources both deploy from the old source to this one."""

from __future__ import annotations

from pathlib import Path

from . import hashing, journal, platform_support
from .context import Context
from .errors import DeployError, see_recovery
from .kinds import AGENT_KIND, KINDS, SHARED, SKILL, ItemKind
from .names import require_safe_name
from .report import ReportLine, print_report

KINDS_ALLOWED = {
    "_migration_candidates": "this source takes over a shared asset only if it owns one, an agent only from its "
    "agents, and a skill only from its skills",
    "migrate": "only a skill is requested by name, so only a skill's request moves with it",
}


def _migration_candidates(context: Context, old_entry: dict, kind: ItemKind) -> list[str]:
    restore = (
        "Restore the copy the old source deployed, for example by deploying from that source again, "
        "then rerun --migrate-from.",
        see_recovery("Ownership held by another source"),
    )
    candidates: list[str] = []
    for name in sorted(old_entry.get(kind.key) or {}):
        if kind is SHARED:
            require_safe_name(name, "migration shared asset")
            if context.source.shared_assets.get(name) != "owner":
                continue
            if not (context.paths.skills_src / name).is_file():
                raise DeployError(f"ERROR: Cannot migrate shared asset '{name}': source owner file is missing.")
            if (old_entry[kind.key][name] or {}).get("role") != "owner":
                continue
        elif kind is AGENT_KIND:
            if not name.endswith(kind.suffix) or kind.item_name(name) not in context.source.agents:
                continue
        elif name not in context.source.skills:
            continue
        expected = (old_entry[kind.key][name] or {}).get("hash", "")
        subject = f"{kind.label} '{name}'" if kind.label else f"'{name}'"
        if not isinstance(expected, str) or not hashing.HASH_PATTERN.fullmatch(expected):
            raise DeployError(f"ERROR: Old manifest has no valid hash for {kind.noun} '{name}'.")
        destination = journal.root_directory(context.paths, kind.root) / name
        if not kind.has_expected_type(destination):
            raise DeployError(f"ERROR: Cannot migrate {subject}: destination is missing.", *restore)
        if hashing.hash_path(destination) != expected:
            raise DeployError(f"ERROR: Cannot migrate {subject}: destination differs from the old manifest.", *restore)
        candidates.append(name)
    return candidates


def _record_takeover(context: Context) -> None:
    """Record this checkout as the source's, as --take-over-source promised, when a migration moves nothing.

    A migration that moves items records the checkout with them. Without --take-over-source, or from the recorded
    checkout, the manifest is left as it is.
    """
    entry = context.manifest.source(context.source_id)
    if not context.options.take_over_source or entry is None:
        return
    recorded = entry.get("source_dir")
    current = context.paths.source_dir
    if isinstance(recorded, str) and recorded and platform_support.same_directory(Path(recorded), current):
        return
    entry["source_dir"] = platform_support.normalize(current)
    context.manifest.save()
    print(f"Recorded this checkout, {platform_support.normalize(current)}, as the source of '{context.source_id}'.")


def migrate(context: Context, old: str) -> None:
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
    # In the table's order, so a refusal names the same item it always did.
    candidates = {kind.key: _migration_candidates(context, old_entry, kind) for kind in KINDS}
    if not any(candidates.values()):
        print("")
        print(f"No intersecting ownership entries to migrate from '{old}'.")
        _record_takeover(context)
        print("")
        return
    new_entry = data.sources.setdefault(
        context.source_id,
        {
            "source_dir": "",
            "deployed_at": None,
            "requested_bundles": [],
            "requested_skills": [],
            **{kind.key: {} for kind in KINDS},
        },
    )
    new_entry["source_dir"] = platform_support.normalize(paths.source_dir)
    for kind in KINDS:
        for name in candidates[kind.key]:
            new_entry.setdefault(kind.key, {})[name] = old_entry[kind.key].pop(name)
            if kind is SKILL and name in old_entry.get("requested_skills", []):
                new_entry["requested_skills"] = sorted(set(new_entry.get("requested_skills", [])) | {name})
                old_entry["requested_skills"] = [value for value in old_entry["requested_skills"] if value != name]
    # A bundle request alone does not keep the old source: it names no item, and the new source counts a bundle whose
    # members it owns as chosen. Nor does an old entry's selected_skills, which nothing reads any more.
    if not any(old_entry.get(key) for key in (*(kind.key for kind in KINDS), "requested_skills")):
        del data.sources[old]
    data.save()
    print("")
    print(f"Moved ownership from '{old}' to '{context.source_id}'.")
    print_report(
        "MIGRATED",
        ("MIGRATED",),
        [ReportLine(name, "MIGRATED", kind=kind.label) for kind in KINDS for name in candidates[kind.key]],
    )

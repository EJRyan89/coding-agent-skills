"""The ownership manifest shared by every source deployed into a home directory."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import fsops
from .errors import DeployError
from .hashing import HASH_PATTERN
from .names import safe_name_problem
from .source import SOURCE_ID_PATTERN, is_valid_name

MANIFEST_VERSION = 7
# Version 7 adds agent ownership. A version 6 manifest is read as having no agents and saved as version 7, which a
# version 6 deployer refuses, so it never rewrites a source entry and drops the agents this version owns.
OLDEST_READABLE_VERSION = 6
# Runtime adapters are recorded under "wrappers", their name before they were called adapters. Renaming the key
# would need a manifest version and a migration, so it stays.
ADAPTERS = "wrappers"
OWNED_KINDS = ("skills", "shared", ADAPTERS, "agents")


@dataclass
class Ownership:
    skills: dict[str, str] = field(default_factory=dict)
    skill_shared_deps: dict[str, list[str]] = field(default_factory=dict)
    shared: dict[str, str] = field(default_factory=dict)
    shared_roles: dict[str, str] = field(default_factory=dict)
    adapters: dict[str, str] = field(default_factory=dict)
    agents: dict[str, str] = field(default_factory=dict)
    requested_skills: set[str] = field(default_factory=set)
    requested_bundles: set[str] = field(default_factory=set)
    selected_skills: list[str] = field(default_factory=list)


@dataclass
class Manifest:
    path: Path
    data: dict[str, Any]
    skill_owners: dict[str, str] = field(default_factory=dict)
    shared_owners: dict[str, str] = field(default_factory=dict)
    adapter_owners: dict[str, str] = field(default_factory=dict)
    agent_owners: dict[str, str] = field(default_factory=dict)

    @property
    def sources(self) -> dict[str, Any]:
        return self.data.setdefault("sources", {})

    def source(self, source_id: str) -> dict[str, Any] | None:
        entry = self.sources.get(source_id)
        return entry if isinstance(entry, dict) else None

    def ownership(self, source_id: str) -> Ownership:
        entry = self.source(source_id) or {}
        owned = Ownership()
        for name, value in (entry.get("skills") or {}).items():
            owned.skills[name] = _hash_of(value)
            owned.skill_shared_deps[name] = list(value.get("shared_deps", [])) if isinstance(value, dict) else []
        for name, value in (entry.get("shared") or {}).items():
            owned.shared[name] = _hash_of(value)
            owned.shared_roles[name] = value.get("role", "owner") if isinstance(value, dict) else "owner"
        for name, value in (entry.get(ADAPTERS) or {}).items():
            owned.adapters[name] = _hash_of(value)
        for name, value in (entry.get("agents") or {}).items():
            owned.agents[name] = _hash_of(value)
        owned.requested_skills = set(entry.get("requested_skills", []))
        owned.requested_bundles = set(entry.get("requested_bundles", []))
        owned.selected_skills = list(entry.get("selected_skills", []))
        return owned

    def entry(self, source_id: str, kind: str, name: str) -> dict[str, Any] | None:
        value = ((self.source(source_id) or {}).get(kind) or {}).get(name)
        return value if isinstance(value, dict) else None

    def last_run_id(self) -> str:
        value = self.data.get("last_run_id", "")
        return value if isinstance(value, str) else ""

    def save(self) -> None:
        self.data["manifest_version"] = MANIFEST_VERSION
        fsops.write_atomic(self.path, (json.dumps(self.data, indent=2) + "\n").encode("utf-8"))


def _hash_of(value: Any) -> str:
    if isinstance(value, dict) and isinstance(value.get("hash"), str):
        return value["hash"]
    return ""


def _safe_names(values: Any) -> bool:
    return isinstance(values, list) and all(
        isinstance(value, str) and safe_name_problem(value, "shared dependency") is None for value in values
    )


def _validate(data: dict[str, Any], path: Path) -> None:
    """Reject any manifest value that could later be joined to a path or trusted as a hash."""
    run_id = data.get("last_run_id", "")
    if not isinstance(run_id, str) or (run_id and safe_name_problem(run_id, "run ID") is not None):
        raise DeployError(f"ERROR: Manifest last_run_id is malformed: {path}")
    for source_id, entry in data["sources"].items():
        if not isinstance(source_id, str) or len(source_id) > 128 or not SOURCE_ID_PATTERN.fullmatch(source_id):
            raise DeployError(f"ERROR: Manifest source ID is malformed: {source_id!r}")
        if not isinstance(entry, dict):
            raise DeployError(f"ERROR: Manifest source '{source_id}' is malformed")
        for field_name in ("selected_skills", "requested_skills", "requested_bundles"):
            values = entry.get(field_name, [])
            if not isinstance(values, list) or not all(isinstance(value, str) and is_valid_name(value) for value in values):
                raise DeployError("ERROR: Manifest selection fields are malformed")
        for kind in OWNED_KINDS:
            items = entry.get(kind, {})
            if not isinstance(items, dict):
                raise DeployError("ERROR: Manifest selection fields are malformed")
            for name, value in items.items():
                named_like_skill = kind in ("skills", ADAPTERS)
                safe = isinstance(name, str) and safe_name_problem(name, "item") is None
                agent_file = safe and name.endswith(".md") and is_valid_name(name[: -len(".md")])
                if (
                    not safe
                    or (named_like_skill and not is_valid_name(name))
                    or (kind == "agents" and not agent_file)
                    or not isinstance(value, dict)
                    or not isinstance(value.get("hash"), str)
                    or not HASH_PATTERN.fullmatch(value["hash"])
                    or (kind == "shared" and value.get("role", "owner") not in ("owner", "dependency"))
                    or (kind == "skills" and not _safe_names(value.get("shared_deps", [])))
                ):
                    raise DeployError(
                        f"ERROR: Manifest entry is malformed: source '{source_id}' {kind} {name!r}"
                    )


def _record_owners(
    data: dict[str, Any], kind: str, label: str, owners: dict[str, str]
) -> None:
    for source_id, entry in data["sources"].items():
        for name in (entry.get(kind) or {}):
            if name in owners and owners[name] != source_id:
                raise DeployError(
                    f"ERROR: {label} '{name}' is owned by both '{owners[name]}' and '{source_id}' in manifest"
                )
            owners[name] = source_id


def load(path: Path) -> Manifest:
    if not path.is_file():
        return Manifest(path, {"manifest_version": MANIFEST_VERSION, "sources": {}})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("sources", {}), dict):
        raise DeployError(f"ERROR: Manifest is malformed: {path}")
    data.setdefault("sources", {})
    version = data.get("manifest_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < OLDEST_READABLE_VERSION:
        raise DeployError(f"ERROR: Manifest has unsupported schema version '{version}'")
    if version > MANIFEST_VERSION:
        raise DeployError(
            f"ERROR: Manifest schema version '{version}' is newer than supported version {MANIFEST_VERSION}"
        )
    _validate(data, path)
    manifest = Manifest(path, data)
    _record_owners(data, "skills", "Skill", manifest.skill_owners)
    _record_owners(data, "shared", "Shared asset", manifest.shared_owners)
    _record_owners(data, ADAPTERS, "Runtime adapter", manifest.adapter_owners)
    _record_owners(data, "agents", "Agent", manifest.agent_owners)
    for name in sorted(manifest.skill_owners):
        if name in manifest.shared_owners:
            raise DeployError(
                f"ERROR: Destination name '{name}' is owned as a skill by '{manifest.skill_owners[name]}' "
                f"and as a shared asset by '{manifest.shared_owners[name]}' in manifest"
            )
    return manifest

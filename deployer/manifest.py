"""The ownership manifest shared by every source deployed into a home directory."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import fsops
from .errors import DeployError, os_error
from .hashing import HASH_PATTERN
from .kinds import ADAPTERS, AGENT_KIND, DEPENDENCY_KINDS, KINDS, SHARED, SKILL, ItemKind
from .names import safe_name_problem
from .source import SOURCE_ID_PATTERN, is_valid_name
from .source_commit import COMMIT_PATTERN

MANIFEST_VERSION = 7
# Version 7 adds agent ownership. A version 6 manifest is read as having no agents and saved as version 7, which a
# version 6 deployer refuses, so it never rewrites a source entry and drops the agents this version owns.
# A skill entry's agent_deps came later in version 7 without a new version: an entry without it records no agents, and
# a deployer that predates it drops it on rewrite, which loses only the agents a skill left in place would keep, never
# an owned item.
OLDEST_READABLE_VERSION = 6
OWNED_KINDS = tuple(kind.key for kind in KINDS)


def _by_kind() -> dict[str, dict[str, str]]:
    return {kind.key: {} for kind in KINDS}


def _by_dependency_kind() -> dict[str, dict[str, list[str]]]:
    return {kind.key: {} for kind in DEPENDENCY_KINDS}


@dataclass
class Ownership:
    hashes: dict[str, dict[str, str]] = field(default_factory=_by_kind)  # kind key -> item name -> hash
    # kind key -> skill name -> the items of that kind the skill was deployed with
    skill_deps: dict[str, dict[str, list[str]]] = field(default_factory=_by_dependency_kind)
    shared_roles: dict[str, str] = field(default_factory=dict)
    requested_skills: set[str] = field(default_factory=set)
    requested_bundles: set[str] = field(default_factory=set)

    def of(self, kind: ItemKind) -> dict[str, str]:
        return self.hashes[kind.key]

    def deps_of(self, kind: ItemKind) -> dict[str, list[str]]:
        return self.skill_deps[kind.key]

    @property
    def skills(self) -> dict[str, str]:
        return self.hashes["skills"]

    @property
    def shared(self) -> dict[str, str]:
        return self.hashes["shared"]

    @property
    def adapters(self) -> dict[str, str]:
        return self.hashes[ADAPTERS]

    @property
    def agents(self) -> dict[str, str]:
        return self.hashes["agents"]


@dataclass
class Manifest:
    path: Path
    data: dict[str, Any]
    owners: dict[str, dict[str, str]] = field(default_factory=_by_kind)  # kind key -> item name -> source ID

    def owners_of(self, kind: ItemKind) -> dict[str, str]:
        return self.owners[kind.key]

    @property
    def skill_owners(self) -> dict[str, str]:
        return self.owners["skills"]

    @property
    def shared_owners(self) -> dict[str, str]:
        return self.owners["shared"]

    @property
    def sources(self) -> dict[str, Any]:
        return self.data.setdefault("sources", {})

    def source(self, source_id: str) -> dict[str, Any] | None:
        entry = self.sources.get(source_id)
        return entry if isinstance(entry, dict) else None

    def source_commit(self, source_id: str) -> str | None:
        """The commit this source was last deployed from, or None for an entry written before it was recorded."""
        value = (self.source(source_id) or {}).get("source_commit")
        return value if isinstance(value, str) else None

    def ownership(self, source_id: str) -> Ownership:
        entry = self.source(source_id) or {}
        owned = Ownership()
        for kind in KINDS:
            for name, value in (entry.get(kind.key) or {}).items():
                owned.of(kind)[name] = _hash_of(value)
                details = value if isinstance(value, dict) else {}
                if kind is SKILL:
                    for dependency in DEPENDENCY_KINDS:
                        owned.deps_of(dependency)[name] = list(details.get(dependency.dependency_key, []))
                elif kind is SHARED:
                    owned.shared_roles[name] = details.get("role", "owner")
        owned.requested_skills = set(entry.get("requested_skills", []))
        owned.requested_bundles = set(entry.get("requested_bundles", []))
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


def _is_agent_file(name: Any) -> bool:
    """An agent's deployed file name: a safe name that is a valid item name followed by .md."""
    return (
        isinstance(name, str)
        and safe_name_problem(name, "agent") is None
        and name.endswith(AGENT_KIND.suffix)
        and is_valid_name(AGENT_KIND.item_name(name))
    )


def _valid_dependencies(values: Any, kind: ItemKind) -> bool:
    """A skill entry's list of the items of one kind it depends on, each a name that kind's items may have."""
    if not isinstance(values, list):
        return False
    if kind is AGENT_KIND:
        return all(_is_agent_file(value) for value in values)
    return all(isinstance(value, str) and safe_name_problem(value, "shared dependency") is None for value in values)


def _validate_source_commit(source_id: str, entry: dict[str, Any]) -> None:
    """A recorded commit is a full object name; an entry written before it was recorded has none until it deploys."""
    if "source_commit" not in entry:
        return
    value = entry["source_commit"]
    if not isinstance(value, str) or not COMMIT_PATTERN.fullmatch(value):
        raise DeployError(f"ERROR: Manifest source '{source_id}' source_commit is malformed")


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
        _validate_source_commit(source_id, entry)
        # selected_skills is no longer written, but an older entry still carries it until its source next
        # deploys, so it is validated, never trusted, on read.
        for field_name in ("selected_skills", "requested_skills", "requested_bundles"):
            values = entry.get(field_name, [])
            if not isinstance(values, list) or not all(
                isinstance(value, str) and is_valid_name(value) for value in values
            ):
                raise DeployError("ERROR: Manifest selection fields are malformed")
        for kind in OWNED_KINDS:
            items = entry.get(kind, {})
            if not isinstance(items, dict):
                raise DeployError("ERROR: Manifest selection fields are malformed")
            for name, value in items.items():
                named_like_skill = kind in ("skills", ADAPTERS)
                safe = isinstance(name, str) and safe_name_problem(name, "item") is None
                if (
                    not safe
                    or (named_like_skill and not is_valid_name(name))
                    or (kind == "agents" and not _is_agent_file(name))
                    or not isinstance(value, dict)
                    or not isinstance(value.get("hash"), str)
                    or not HASH_PATTERN.fullmatch(value["hash"])
                    or (kind == "shared" and value.get("role", "owner") not in ("owner", "dependency"))
                    or (
                        kind == "skills"
                        and not all(
                            _valid_dependencies(value.get(dependency.dependency_key, []), dependency)
                            for dependency in DEPENDENCY_KINDS
                        )
                    )
                ):
                    raise DeployError(f"ERROR: Manifest entry is malformed: source '{source_id}' {kind} {name!r}")


def _record_owners(data: dict[str, Any], kind: str, label: str, owners: dict[str, str]) -> None:
    for source_id, entry in data["sources"].items():
        for name in entry.get(kind) or {}:
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
    except OSError as exc:
        # A file another process holds, or one this user may not read, is not malformed: say which it is.
        raise os_error(exc, "read the manifest") from exc
    except (UnicodeError, json.JSONDecodeError):
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
    for kind in KINDS:
        _record_owners(data, kind.key, kind.title, manifest.owners_of(kind))
    for name in sorted(manifest.skill_owners):
        if name in manifest.shared_owners:
            raise DeployError(
                f"ERROR: Destination name '{name}' is owned as a skill by '{manifest.skill_owners[name]}' "
                f"and as a shared asset by '{manifest.shared_owners[name]}' in manifest"
            )
    return manifest

"""The ownership manifest shared by every source deployed into a home directory."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import fsops
from .errors import DeployError, os_error
from .hashing import HASH_PATTERN
from .kinds import ADAPTER_KIND, AGENT_KIND, DEPENDENCY_KINDS, KINDS, SHARED, SKILL, ItemKind
from .names import safe_name_problem
from .source import SOURCE_ID_PATTERN, is_valid_name
from .source_commit import COMMIT_PATTERN
from .source_ref import REF_PATTERN

KINDS_ALLOWED = {
    "Manifest.ownership": "a skill entry also records the items it depends on, and a shared asset entry its role",
    "_valid_dependencies": "a skill names skill dependencies by skill names, agents by their file names, and shared "
    "assets by any safe name",
    "_validate": "each kind names its items its own way: a skill and its runtime adapter by a skill name and an agent "
    "by its file name, and only a shared asset entry has a role and only a skill entry dependencies",
}

MANIFEST_VERSION = 8
# Version 8 records direct skill dependencies so a skill left in place keeps its transitive closure. Older deployers
# refuse it rather than silently dropping those edges and removing dependencies. Versions 6/7 are upgraded in memory
# on read; the next atomic manifest commit saves the upgrade with the deployment's journaled changes.
# Version 7 added agent ownership; a version 6 manifest has no agents. Optional agent_deps and source_ref came later:
# an older entry without them records no agents for a preserved skill and no pinned ref, respectively.
OLDEST_READABLE_VERSION = 6


def _by_kind() -> dict[str, dict[str, str]]:
    return {kind.key: {} for kind in KINDS}


def _by_dependency_kind() -> dict[str, dict[str, list[str]]]:
    return {kind.key: {} for kind in DEPENDENCY_KINDS}


def _independent(skills: Collection[str], edges: Mapping[str, list[str]]) -> set[str]:
    """The skills no other skill depends on by these edges, less any edge on a cycle."""

    def reaches(start: str, goal: str) -> bool:
        seen, pending = {start}, [start]
        while pending:
            for dependency in edges.get(pending.pop(), []):
                if dependency == goal:
                    return True
                if dependency not in seen:
                    seen.add(dependency)
                    pending.append(dependency)
        return False

    depended = {
        dependency
        for name, dependencies in edges.items()
        for dependency in dependencies
        if dependency != name and not reaches(dependency, name)
    }
    return set(skills) - depended


@dataclass
class Ownership:
    hashes: dict[str, dict[str, str]] = field(default_factory=_by_kind)  # kind key -> item name -> hash
    # kind key -> skill name -> the items of that kind the skill was deployed with
    skill_deps: dict[str, dict[str, list[str]]] = field(default_factory=_by_dependency_kind)
    shared_roles: dict[str, str] = field(default_factory=dict)
    requested_skills: set[str] = field(default_factory=set)
    requested_bundles: set[str] = field(default_factory=set)
    # requested bundle -> the members the deployment that recorded the request expanded it to; an entry written
    # before members were recorded has none
    requested_bundle_members: dict[str, set[str]] = field(default_factory=dict)
    # whether the entry records the members of its bundle requests: an entry an earlier release wrote, by deploying or
    # by migrating, does not
    bundle_members_recorded: bool = False

    def of(self, kind: ItemKind) -> dict[str, str]:
        return self.hashes[kind.key]

    def deps_of(self, kind: ItemKind) -> dict[str, list[str]]:
        return self.skill_deps[kind.key]

    @property
    def skills(self) -> dict[str, str]:
        return self.of(SKILL)

    def installed_roots(self, bundles: Mapping[str, Collection[str]]) -> set[str]:
        """The installed skills that no other installed skill was deployed depending on, read against the source's
        `bundles`.

        A deployment refuses a dependency cycle, so the skill dependencies it records never form one. An edge on a
        cycle is one `_upgrade_dependencies` filled in for an entry that recorded none, giving each skill every other
        skill its source owned, and says nothing about what was chosen: it excludes no skill, so such an entry keeps
        more, not less, as it does whether it is read in memory before its next deployment or after a deployment of
        another source, or a migration, saved it as filled in.

        The first deployer to write version 8 recorded no bundle members, and a skill it left in place, preserved or
        skipped, kept the edges the upgrade filled in beside the exact ones it recorded for the skills it installed, so
        they need not form a cycle. That deployment may also have installed a skill the upgraded manifest did not own,
        which a filled-in list therefore does not name, and it may have preserved a skill the user deselected. So in an
        entry that requests a bundle whose members it does not record and a skill on its own, a skill's list may be
        filled in when every skill it leaves out is requested on its own or depended on, by another skill's recorded
        edges, by one that is. The entry cannot tell such a list from an exact one: an exact list leaves out the same
        skills whenever other requests reach them, and a skill that deployment installed may depend on a preserved
        skill and on every skill its filled-in list names. Each reading is wrong somewhere, so both are taken. Read as
        recorded, the edges count no dependency as chosen. With every such list set aside, they keep what the
        conservative edges kept, and a skill only that reading finds stands for a request only where counting it keeps
        and never adds: when no bundle of the source holds it, or the entry holds every member of its bundle. Otherwise
        it would choose a bundle one of whose members is not installed and make --all install that member, which is how
        a skill installed only as another root's dependency would choose itself and its bundle.

        So the entry cannot tell a renamed bundle that has since gained a member, beside a preserved skill, from an
        unchosen bundle one of whose members another skill depends on: it reads both as unchosen, and --all keeps the
        renamed one only when it is named with --include. Nor can it tell a renamed bundle from an unchosen one every
        member of which another skill depends on: it reads both as chosen, which installs nothing. A skill that
        deployment installed only through a bundle request is one no request on its own reaches, so beside it a
        filled-in list is read as recorded and a bundle the source has since renamed stays chosen only when named with
        --include: the entry cannot tell that history from one whose deployment preserved nothing and installed a
        requested bundle's members that nothing depends on. An entry that requests no skill on its own cannot tell a
        filled-in list from an exact one either, and its edges are read as recorded.
        """
        edges = self.deps_of(SKILL)
        roots = _independent(self.skills, edges)
        if not (self.requested_bundles - self.requested_bundle_members.keys() and self.requested_skills):
            return roots
        kept = _independent(
            self.skills,
            {
                name: dependencies
                for name, dependencies in edges.items()
                if not set(self.skills) - {name, *dependencies} <= self._requested_reach(without=name)
            },
        )
        bundle_of = {member: set(members) for members in bundles.values() for member in members}
        return roots | {name for name in kept - roots if bundle_of.get(name, set()) <= self.skills.keys()}

    def _requested_reach(self, without: str) -> set[str]:
        """The skills requested on their own and every skill they depend on, by the recorded edges of each skill but
        `without`, whose own edges are the ones in question."""
        reached, pending = set(self.requested_skills), [name for name in self.requested_skills if name != without]
        while pending:
            for dependency in self.deps_of(SKILL).get(pending.pop(), []):
                if dependency not in reached:
                    reached.add(dependency)
                    if dependency != without:
                        pending.append(dependency)
        return reached

    @property
    def shared(self) -> dict[str, str]:
        return self.of(SHARED)

    @property
    def adapters(self) -> dict[str, str]:
        return self.of(ADAPTER_KIND)

    @property
    def agents(self) -> dict[str, str]:
        return self.of(AGENT_KIND)


@dataclass
class Manifest:
    path: Path
    data: dict[str, Any]
    owners: dict[str, dict[str, str]] = field(default_factory=_by_kind)  # kind key -> item name -> source ID

    def owners_of(self, kind: ItemKind) -> dict[str, str]:
        return self.owners[kind.key]

    @property
    def skill_owners(self) -> dict[str, str]:
        return self.owners_of(SKILL)

    @property
    def shared_owners(self) -> dict[str, str]:
        return self.owners_of(SHARED)

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

    def source_ref(self, source_id: str) -> str | None:
        """The ref a deployment made with --at named, or None when the source was last deployed as its checkout is."""
        value = (self.source(source_id) or {}).get("source_ref")
        return value if isinstance(value, str) else None

    def ownership(self, source_id: str) -> Ownership:
        entry = self.source(source_id) or {}
        owned = Ownership()
        for kind in KINDS:
            for name, value in entry.get(kind.key, {}).items():
                owned.of(kind)[name] = _hash_of(value)
                details = value if isinstance(value, dict) else {}
                if kind is SKILL:
                    for dependency in DEPENDENCY_KINDS:
                        owned.deps_of(dependency)[name] = list(details.get(dependency.dependency_key, []))
                elif kind is SHARED:
                    owned.shared_roles[name] = details.get("role", "owner")
        owned.requested_skills = set(entry.get("requested_skills", []))
        owned.requested_bundles = set(entry.get("requested_bundles", []))
        owned.requested_bundle_members = {
            name: set(members) for name, members in entry.get("requested_bundle_members", {}).items()
        }
        owned.bundle_members_recorded = "requested_bundle_members" in entry
        return owned

    def entry(self, source_id: str, kind: str, name: str) -> dict[str, Any] | None:
        value = (self.source(source_id) or {}).get(kind, {}).get(name)
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
        and safe_name_problem(name, AGENT_KIND.noun) is None
        and name.endswith(AGENT_KIND.suffix)
        and is_valid_name(AGENT_KIND.item_name(name))
    )


def _valid_dependencies(values: Any, kind: ItemKind) -> bool:
    """A skill entry's list of the items of one kind it depends on, each a name that kind's items may have."""
    if not isinstance(values, list):
        return False
    if kind is AGENT_KIND:
        return all(_is_agent_file(value) for value in values)
    if kind is SKILL:
        return all(isinstance(value, str) and is_valid_name(value) for value in values)
    return all(isinstance(value, str) and safe_name_problem(value, "shared dependency") is None for value in values)


def _validate_source_commit(source_id: str, entry: dict[str, Any]) -> None:
    """A recorded commit is a full object name; an entry written before it was recorded has none until it deploys.

    A deployment made with --at also records the ref it named, which is only ever printed, and only beside a commit.
    """
    if "source_commit" in entry:
        value = entry["source_commit"]
        if not isinstance(value, str) or not COMMIT_PATTERN.fullmatch(value):
            raise DeployError(f"ERROR: Manifest source '{source_id}' source_commit is malformed")
    if "source_ref" in entry:
        ref = entry["source_ref"]
        if "source_commit" not in entry or not isinstance(ref, str) or not REF_PATTERN.fullmatch(ref):
            raise DeployError(f"ERROR: Manifest source '{source_id}' source_ref is malformed")


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
        members = entry.get("requested_bundle_members", {})
        if not isinstance(members, dict) or not all(
            isinstance(name, str)
            and is_valid_name(name)
            and isinstance(values, list)
            and all(isinstance(value, str) and is_valid_name(value) for value in values)
            for name, values in members.items()
        ):
            raise DeployError("ERROR: Manifest selection fields are malformed")
        for kind in KINDS:
            items = entry.get(kind.key, {})
            if not isinstance(items, dict):
                raise DeployError("ERROR: Manifest selection fields are malformed")
            for name, value in items.items():
                named_like_skill = kind in (SKILL, ADAPTER_KIND)
                safe = isinstance(name, str) and safe_name_problem(name, "item") is None
                if (
                    not safe
                    or (named_like_skill and not is_valid_name(name))
                    or (kind is AGENT_KIND and not _is_agent_file(name))
                    or not isinstance(value, dict)
                    or not isinstance(value.get("hash"), str)
                    or not HASH_PATTERN.fullmatch(value["hash"])
                    or (kind is SHARED and value.get("role", "owner") not in ("owner", "dependency"))
                    or (
                        kind is SKILL
                        and not all(
                            _valid_dependencies(
                                value.get(
                                    dependency.dependency_key,
                                    None if dependency is SKILL and data["manifest_version"] >= 8 else [],
                                ),
                                dependency,
                            )
                            for dependency in DEPENDENCY_KINDS
                        )
                    )
                ):
                    raise DeployError(f"ERROR: Manifest entry is malformed: source '{source_id}' {kind.key} {name!r}")


def _record_owners(data: dict[str, Any], kind: str, label: str, owners: dict[str, str]) -> None:
    for source_id, entry in data["sources"].items():
        for name in entry.get(kind, {}):
            if name in owners and owners[name] != source_id:
                raise DeployError(
                    f"ERROR: {label} '{name}' is owned by both '{owners[name]}' and '{source_id}' in manifest"
                )
            owners[name] = source_id


def _upgrade_dependencies(data: dict[str, Any]) -> None:
    """Prepare an older manifest's missing edges without guessing from metadata newer than its installed skills.

    Unknown historical edges conservatively retain the source's other owned skills. A successful deployment replaces
    them with exact edges for each skill it installs; skipped and preserved skills keep the conservative graph.
    This changes only memory, so a dry run, a refusal, or a failed uncommitted deployment leaves the manifest intact.
    """
    if data["manifest_version"] >= 8:
        return
    for entry in data["sources"].values():
        skills = entry.get(SKILL.key, {})
        for name, details in skills.items():
            details.setdefault(SKILL.dependency_key, sorted(set(skills) - {name}))


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
    _upgrade_dependencies(data)
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

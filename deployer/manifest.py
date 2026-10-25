"""The ownership manifest shared by every source deployed into a home directory."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import json_writer

from . import fsops, platform_support, state_files
from .errors import STATE_FILE, DeployError, os_error, see_recovery
from .hashing import HASH_PATTERN
from .kinds import ADAPTER_KIND, AGENT_KIND, DEPENDENCY_KINDS, KINDS, SHARED, SKILL, ItemKind, KindKey
from .names import safe_name_problem
from .source import SOURCE_ID_MAX_LENGTH, SOURCE_ID_PATTERN, is_valid_name
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
# The first version that records each skill's skill_deps; an older manifest's are upgraded in memory.
SKILL_DEPS_VERSION = 8
# Version 8 records direct skill dependencies so a skill left in place keeps its transitive closure. Older deployers
# refuse it rather than silently dropping those edges and removing dependencies. Versions 6/7 are upgraded in memory
# on read; the next atomic manifest commit saves the upgrade with the deployment's journaled changes.
# Version 7 added agent ownership; a version 6 manifest has no agents. Optional agent_deps and source_ref came later:
# an older entry without them records no agents for a preserved skill and no pinned ref, respectively.
OLDEST_READABLE_VERSION = 6
# What to do with a manifest the deployer cannot read, before it plans anything.
MANIFEST_REMEDY = (
    "Put back a copy you trust, or move it out of the skills folder and rerun with --dry-run.",
    see_recovery(STATE_FILE),
)


class ItemEntry(TypedDict):
    """One item's ownership entry, under its kind's key in a source entry.

    A shared asset's entry may record its role, and a skill's records the items of each kind it was deployed with,
    though an entry an older deployer wrote may lack a skill's agent_deps; `load` fills in skill_deps for a manifest
    older than SKILL_DEPS_VERSION.
    """

    hash: str
    role: NotRequired[str]
    skill_deps: NotRequired[list[str]]
    shared_deps: NotRequired[list[str]]
    agent_deps: NotRequired[list[str]]


class SourceEntry(TypedDict):
    """One source's entry under "sources", as `load` validates it and `source_entry` writes it.

    An entry an older deployer wrote may lack any field. source_dir and deployed_at are not validated, since the
    deployer only compares the one and never reads the other, so a reader checks source_dir is a string.
    selected_skills is no longer written, and an older entry keeps it until its source next deploys.
    """

    source_dir: NotRequired[object]
    source_commit: NotRequired[str]
    source_ref: NotRequired[str]
    deployed_at: NotRequired[object]
    requested_bundles: NotRequired[list[str]]
    requested_bundle_members: NotRequired[dict[str, list[str]]]
    requested_skills: NotRequired[list[str]]
    selected_skills: NotRequired[list[str]]
    skills: NotRequired[dict[str, ItemEntry]]
    shared: NotRequired[dict[str, ItemEntry]]
    wrappers: NotRequired[dict[str, ItemEntry]]
    agents: NotRequired[dict[str, ItemEntry]]


class ManifestData(TypedDict):
    """The manifest file's content; a field the deployer does not know is kept as it is."""

    manifest_version: int
    sources: dict[str, SourceEntry]
    last_run_id: NotRequired[str]


def source_entry(
    source_dir: str,
    deployed_at: str | None,
    *,
    commit: str | None = None,
    ref: str | None = None,
    requested_bundles: Collection[str] = (),
    requested_bundle_members: Mapping[str, list[str]] | None = None,
    requested_skills: Collection[str] = (),
    items: Mapping[KindKey, Mapping[str, ItemEntry]] | None = None,
) -> SourceEntry:
    """A source's entry as a deployment or a migration writes it, in the order the manifest has always held its fields.

    items maps each kind's key to its entries, written sorted by name; a kind it leaves out is written empty.
    """
    entry: SourceEntry = {"source_dir": source_dir}
    if commit is not None:
        entry["source_commit"] = commit
    if ref is not None:
        entry["source_ref"] = ref
    entry["deployed_at"] = deployed_at
    entry["requested_bundles"] = list(requested_bundles)
    entry["requested_bundle_members"] = dict(requested_bundle_members or {})
    entry["requested_skills"] = list(requested_skills)
    for kind in KINDS:
        entry[kind.key] = dict(sorted((items or {}).get(kind.key, {}).items()))
    return entry


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

    def installed_roots(self, declared: Mapping[str, Collection[str]]) -> set[str]:
        """The installed skills no other installed skill depends on, counting a dependency only when this entry records
        it and the source still declares it in `declared`, each skill's dependencies there.

        This is the reading of an entry that does not record what its bundle requests stood for. A skill a dependency
        explains is left out, so one installed only as another's dependency stands for no request. A dependency the
        entry records but the source no longer declares explains nothing: the upgrade of a manifest that recorded none
        filled in every other skill its source owned, and a skill an earlier deployment left in place kept that list,
        which no reading of the entry can tell from an exact one. Nor does a dependency the source declares but the
        entry does not record, so a skill a request stood for never becomes a dependency because the source added one,
        and the closure a skipped or preserved skill keeps, read from the entry alone, is unchanged. Every installed
        skill is then either one of these roots or a dependency the source declares of an installed skill, so reading
        the roots as chosen keeps every installed skill the source still offers. A dependency the source has dropped
        since makes its skill a root, which keeps that skill and completes its bundle.
        """
        recorded = self.deps_of(SKILL)
        explained = {
            dependency
            for name in self.skills
            for dependency in recorded.get(name, [])
            if dependency != name and dependency in declared.get(name, ())
        }
        return set(self.skills) - explained

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
    data: ManifestData
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
    def sources(self) -> dict[str, SourceEntry]:
        return self.data.setdefault("sources", {})

    def source(self, source_id: str) -> SourceEntry | None:
        return self.sources.get(source_id)

    def source_commit(self, source_id: str) -> str | None:
        """The commit this source was last deployed from, or None for an entry written before it was recorded."""
        return (self.source(source_id) or {}).get("source_commit")

    def source_ref(self, source_id: str) -> str | None:
        """The ref a deployment made with --at named, or None when the source was last deployed as its checkout is."""
        return (self.source(source_id) or {}).get("source_ref")

    def ownership(self, source_id: str) -> Ownership:
        entry: SourceEntry = self.source(source_id) or {}
        owned = Ownership()
        for kind in KINDS:
            for name, value in entry.get(kind.key, {}).items():
                owned.of(kind)[name] = _hash_of(value)
                if kind is SKILL:
                    for dependency in DEPENDENCY_KINDS:
                        owned.deps_of(dependency)[name] = list(value.get(dependency.dependency_key, []))
                elif kind is SHARED:
                    owned.shared_roles[name] = value.get("role", "owner")
        owned.requested_skills = set(entry.get("requested_skills", []))
        owned.requested_bundles = set(entry.get("requested_bundles", []))
        owned.requested_bundle_members = {
            name: set(members) for name, members in entry.get("requested_bundle_members", {}).items()
        }
        owned.bundle_members_recorded = "requested_bundle_members" in entry
        return owned

    def entry(self, source_id: str, kind: KindKey, name: str) -> ItemEntry | None:
        entry: SourceEntry = self.source(source_id) or {}
        value = entry.get(kind, {}).get(name)
        return value if isinstance(value, dict) else None

    def last_run_id(self) -> str:
        return self.data.get("last_run_id", "")

    def save(self) -> None:
        """Replace the manifest file. A value the JSON writer refuses is a DeployError naming the file, which the
        run's journaled recovery handles. The paths the manifest records are refused before any write by the allowlist
        `config.derived_values` holds HOME and SOURCE_ROOT to, so none reaches it holding a lone surrogate."""
        self.data["manifest_version"] = MANIFEST_VERSION
        try:
            text = json_writer.dumps(self.data, indent=2)
        except json_writer.JsonWriteRefused as exc:
            raise DeployError(
                f"ERROR: Cannot write the manifest {platform_support.normalize(self.path)}: {exc}"
            ) from exc
        fsops.write_atomic(self.path, (text + "\n").encode("utf-8"))


def _hash_of(value: object) -> str:
    recorded = value.get("hash") if isinstance(value, dict) else None
    return recorded if isinstance(recorded, str) else ""


def _is_agent_file(name: object) -> bool:
    """An agent's deployed file name: a safe name that is a valid item name followed by .md."""
    return (
        isinstance(name, str)
        and safe_name_problem(name, AGENT_KIND.noun) is None
        and name.endswith(AGENT_KIND.suffix)
        and is_valid_name(AGENT_KIND.item_name(name))
    )


def _valid_dependencies(values: object, kind: ItemKind) -> bool:
    """A skill entry's list of the items of one kind it depends on, each a name that kind's items may have."""
    if not isinstance(values, list):
        return False
    if kind is AGENT_KIND:
        return all(_is_agent_file(value) for value in values)
    if kind is SKILL:
        return all(isinstance(value, str) and is_valid_name(value) for value in values)
    return all(isinstance(value, str) and safe_name_problem(value, "shared dependency") is None for value in values)


def _validate_source_commit(source_id: str, entry: dict[object, object]) -> None:
    """A recorded commit is a full object name; an entry written before it was recorded has none until it deploys.

    A deployment made with --at also records the ref it named, which is only ever printed, and only beside a commit.
    """
    if "source_commit" in entry:
        value = entry["source_commit"]
        if not isinstance(value, str) or not COMMIT_PATTERN.fullmatch(value):
            raise DeployError(f"ERROR: Manifest source '{source_id}' source_commit is malformed", *MANIFEST_REMEDY)
    if "source_ref" in entry:
        ref = entry["source_ref"]
        if "source_commit" not in entry or not isinstance(ref, str) or not REF_PATTERN.fullmatch(ref):
            raise DeployError(f"ERROR: Manifest source '{source_id}' source_ref is malformed", *MANIFEST_REMEDY)


def _validate(data: dict[object, object], sources: dict[object, object], version: int, path: Path) -> ManifestData:
    """The manifest as its shape, once no value that could later be joined to a path or trusted as a hash is malformed.

    sources and version are data's sources and manifest_version, which the caller has already read.
    """
    run_id = data.get("last_run_id", "")
    if not isinstance(run_id, str) or (run_id and safe_name_problem(run_id, "run ID") is not None):
        raise DeployError(
            f"ERROR: Manifest last_run_id is malformed: {platform_support.normalize(path)}", *MANIFEST_REMEDY
        )
    for source_id, entry in sources.items():
        if (
            not isinstance(source_id, str)
            or len(source_id) > SOURCE_ID_MAX_LENGTH
            or not SOURCE_ID_PATTERN.fullmatch(source_id)
        ):
            raise DeployError(f"ERROR: Manifest source ID is malformed: {source_id!r}", *MANIFEST_REMEDY)
        if not isinstance(entry, dict):
            raise DeployError(f"ERROR: Manifest source '{source_id}' is malformed", *MANIFEST_REMEDY)
        _validate_source_commit(source_id, entry)
        # selected_skills is no longer written, but an older entry still carries it until its source next
        # deploys, so it is validated, never trusted, on read.
        for field_name in ("selected_skills", "requested_skills", "requested_bundles"):
            values = entry.get(field_name, [])
            if not isinstance(values, list) or not all(
                isinstance(value, str) and is_valid_name(value) for value in values
            ):
                raise DeployError("ERROR: Manifest selection fields are malformed", *MANIFEST_REMEDY)
        members = entry.get("requested_bundle_members", {})
        if not isinstance(members, dict) or not all(
            isinstance(name, str)
            and is_valid_name(name)
            and isinstance(values, list)
            and all(isinstance(value, str) and is_valid_name(value) for value in values)
            for name, values in members.items()
        ):
            raise DeployError("ERROR: Manifest selection fields are malformed", *MANIFEST_REMEDY)
        for kind in KINDS:
            items = entry.get(kind.key, {})
            if not isinstance(items, dict):
                raise DeployError("ERROR: Manifest selection fields are malformed", *MANIFEST_REMEDY)
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
                                    None if dependency is SKILL and version >= SKILL_DEPS_VERSION else [],
                                ),
                                dependency,
                            )
                            for dependency in DEPENDENCY_KINDS
                        )
                    )
                ):
                    raise DeployError(
                        f"ERROR: Manifest entry is malformed: source '{source_id}' {kind.key} {name!r}",
                        *MANIFEST_REMEDY,
                    )
    # Every field the shape gives a type was checked above; source_dir and deployed_at, which it types as object, and
    # a field it does not name are kept as they are.
    return cast(ManifestData, data)


def _record_owners(data: ManifestData, kind: KindKey, label: str, owners: dict[str, str]) -> None:
    for source_id, entry in data["sources"].items():
        for name in entry.get(kind, {}):
            if name in owners and owners[name] != source_id:
                raise DeployError(
                    f"ERROR: {label} '{name}' is owned by both '{owners[name]}' and '{source_id}' in manifest",
                    *MANIFEST_REMEDY,
                )
            owners[name] = source_id


def _upgrade_dependencies(data: ManifestData) -> None:
    """Prepare an older manifest's missing edges without guessing from metadata newer than its installed skills.

    Unknown historical edges conservatively retain the source's other owned skills. A successful deployment replaces
    them with exact edges for each skill it installs; skipped and preserved skills keep the conservative graph.
    This changes only memory, so a dry run, a refusal, or a failed uncommitted deployment leaves the manifest intact.
    """
    if data["manifest_version"] >= SKILL_DEPS_VERSION:
        return
    for entry in data["sources"].values():
        skills = entry.get(SKILL.key, {})
        for name, details in skills.items():
            details.setdefault(SKILL.dependency_key, sorted(set(skills) - {name}))


def load(path: Path) -> Manifest:
    if not state_files.is_present(path, "The manifest", *MANIFEST_REMEDY):
        return Manifest(path, {"manifest_version": MANIFEST_VERSION, "sources": {}})
    raw: object
    try:
        raw = state_files.parse_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        # A file another process holds, or one this user may not read, is not malformed: say which it is.
        raise os_error(exc, "read the manifest") from exc
    except UnicodeError:
        raw = None
    sources = raw.setdefault("sources", {}) if isinstance(raw, dict) else None
    if not isinstance(raw, dict) or not isinstance(sources, dict):
        raise DeployError(f"ERROR: Manifest is malformed: {platform_support.normalize(path)}", *MANIFEST_REMEDY)
    version = raw.get("manifest_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < OLDEST_READABLE_VERSION:
        raise DeployError(f"ERROR: Manifest has unsupported schema version '{version}'", *MANIFEST_REMEDY)
    if version > MANIFEST_VERSION:
        raise DeployError(
            f"ERROR: Manifest schema version '{version}' is newer than supported version {MANIFEST_VERSION}",
            # A newer deployer wrote it, so this checkout is older than the one that last deployed.
            "Update this checkout with 'git pull', or deploy from the checkout that wrote the manifest.",
        )
    data = _validate(raw, sources, version, path)
    _upgrade_dependencies(data)
    manifest = Manifest(path, data)
    for kind in KINDS:
        _record_owners(data, kind.key, kind.title, manifest.owners_of(kind))
    for name in sorted(manifest.skill_owners):
        if name in manifest.shared_owners:
            raise DeployError(
                f"ERROR: Destination name '{name}' is owned as a skill by '{manifest.skill_owners[name]}' "
                f"and as a shared asset by '{manifest.shared_owners[name]}' in manifest",
                *MANIFEST_REMEDY,
            )
    return manifest

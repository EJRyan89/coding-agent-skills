"""Structured flag creation, listing, and resolution."""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from review_config import validate_repository_identity
from review_io import ResourceLock, atomic_write_json, read_json
from typed_fields import FieldError, FieldReader

# Version 2 adds review_version, so a finding ID names one review; finding IDs restart at F001 in every review.
SCHEMA_VERSION = 2
FIELDS = frozenset(
    {
        "id",
        "status",
        "created_at",
        "resolved_at",
        "repository",
        "pull_number",
        "review_version",
        "finding_id",
        "category",
        "body",
        "resolution",
    }
)
# A finding's ID within its review, as records assign it.
FINDING_ID = re.compile(r"F[0-9]{3,}")


class FlagError(ValueError):
    pass


class FlagAlreadyResolved(FlagError):
    """The locked, validated store already holds a resolution; no write was made."""

    def __init__(self, flag_id: str) -> None:
        super().__init__(f"Flag is already resolved: {flag_id}")
        self.flag_id = flag_id


class FlagFieldError(FieldError, FlagError):
    """A flag store field of the wrong kind."""


READ = FieldReader(FlagFieldError)


def default_flags_path() -> Path:
    """CODE_REVIEW_FLAGS, else the standard flag store beside the code-review configuration."""
    override = os.environ.get("CODE_REVIEW_FLAGS")
    if override:
        return Path(override)
    return Path.home() / ".coding-agent-skills" / "code-review" / "flags.json"


def empty_store() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "next_id": 1, "flags": []}


def validate_store(value: Any) -> dict[str, Any]:
    store = _validate_store_envelope(value)
    ids: set[str] = set()
    for index, flag in enumerate(store["flags"]):
        path = f"flag-store.flags[{index}]"
        _validate_flag_identity(flag, path, ids)
        ids.add(flag["id"])
        _validate_flag_target(flag, path)
        _validate_flag_text(flag, path)
        _validate_flag_resolution(flag, path)
    allocated = [int(item["id"].removeprefix("RF-")) for item in store["flags"]]
    if allocated and store["next_id"] <= max(allocated):
        raise FlagError("Flag next_id must be greater than every allocated ID")
    return store


def _validate_store_envelope(value: Any) -> dict[str, Any]:
    """The store's own fields: its shape, schema version, next ID, and flag list."""
    store = READ.json_object(value, "flag-store", required={"schema_version", "next_id", "flags"})
    if READ.integer(store["schema_version"], "flag-store.schema_version") != SCHEMA_VERSION:
        raise FlagError("Flag store schema version is unsupported")
    READ.integer(store["next_id"], "flag-store.next_id", minimum=1)
    READ.array(store["flags"], "flag-store.flags")
    return store


def _validate_flag_identity(flag: Any, path: str, ids: set[str]) -> None:
    """A flag's shape, and an ID no earlier flag has, in the RF-NNNNNN format."""
    READ.json_object(flag, path, required=FIELDS)
    identifier = READ.string(flag["id"], f"{path}.id")
    if identifier in ids:
        raise FlagError("Flag IDs must be unique strings")
    if not re.fullmatch(r"RF-[0-9]{6}", identifier):
        raise FlagError("Flag ID format is invalid")


def _validate_flag_target(flag: dict[str, Any], path: str) -> None:
    """A known status, and what the flag is about: an optional repository, pull request, and review version, the last
    only with a pull request, and an optional finding ID, which is a string."""
    READ.one_of(flag["status"], f"{path}.status", ("open", "resolved"))
    repository = READ.optional(READ.string, flag["repository"], f"{path}.repository")
    if repository is not None:
        validate_repository_identity(repository)
    pull_number = READ.optional(READ.integer, flag["pull_number"], f"{path}.pull_number", minimum=1)
    review_version = READ.optional(READ.integer, flag["review_version"], f"{path}.review_version", minimum=1)
    if review_version is not None and pull_number is None:
        raise FlagError("Flag review version is invalid")
    # A flag written before add checked the finding ID's form keeps whatever string it was given.
    READ.optional(READ.string, flag["finding_id"], f"{path}.finding_id", empty=True)


def _validate_flag_text(flag: dict[str, Any], path: str) -> None:
    for field in ("created_at", "category", "body"):
        READ.string(flag[field], f"{path}.{field}")
    try:
        datetime.fromisoformat(flag["created_at"])
    except ValueError as exc:
        raise FlagError("Flag created_at is invalid") from exc


def _validate_flag_resolution(flag: dict[str, Any], path: str) -> None:
    """An open flag has no resolution; a resolved one has a resolution and a time."""
    if flag["status"] == "open":
        if flag["resolved_at"] is not None or flag["resolution"] is not None:
            raise FlagError("Open flag cannot contain resolution metadata")
        return
    if flag["resolved_at"] is None or flag["resolution"] is None:
        raise FlagError("Resolved flag requires resolution metadata")
    resolved_at = READ.string(flag["resolved_at"], f"{path}.resolved_at")
    READ.string(flag["resolution"], f"{path}.resolution")
    try:
        datetime.fromisoformat(resolved_at)
    except ValueError as exc:
        raise FlagError("Flag resolved_at is invalid") from exc


def upgrade_store(value: Any) -> Any:
    """A version 1 store as version 2: its flags name no review version, so none of them can be linked to a finding.
    Anything else is returned as it is, for validate_store to judge."""
    try:
        store = READ.json_object(value, "flag-store")
        legacy = READ.integer(store.get("schema_version"), "flag-store.schema_version") == 1
        flags = READ.array(store.get("flags"), "flag-store.flags")
    except FieldError:
        return value
    if not legacy:
        return value
    return {**store, "schema_version": SCHEMA_VERSION, "flags": [_upgraded_flag(flag) for flag in flags]}


def _upgraded_flag(flag: Any) -> Any:
    """A version 1 flag with no review version; anything that is not a flag object is left for validate_store."""
    try:
        READ.json_object(flag, "flag-store.flags[]")
    except FieldError:
        return flag
    return flag if "review_version" in flag else {**flag, "review_version": None}


def load_store(path: Path) -> dict[str, Any]:
    return validate_store(upgrade_store(read_json(path))) if path.exists() else empty_store()


def validate_target(
    repository: str | None, pull_number: int | None, review_version: int | None, finding_id: str | None
) -> str | None:
    """What a new flag is about, checked before anything is read: a finding ID in the form reports give it, named
    with the repository, pull request, and review version that make it one finding. Returns the repository."""
    if finding_id is not None and not FINDING_ID.fullmatch(finding_id):
        raise FlagError(
            f"Finding ID {finding_id!r} is not a finding ID such as F001; a report label such as v2 F003 names "
            "review version 2 and finding F003"
        )
    if finding_id is not None and None in (repository, pull_number, review_version):
        raise FlagError("A flag that names a finding must name its repository, pull request, and review version")
    if review_version is not None and (pull_number is None or review_version < 1):
        raise FlagError("Review version must be positive and name a pull request")
    if repository is not None:
        repository = validate_repository_identity(repository)
    if pull_number is not None and pull_number < 1:
        raise FlagError("Pull number must be positive")
    return repository


def add_flag(
    path: Path,
    *,
    category: str,
    body: str,
    repository: str | None = None,
    pull_number: int | None = None,
    review_version: int | None = None,
    finding_id: str | None = None,
) -> dict[str, Any]:
    """Add an open flag. A finding is named by its ID within one review, so it needs the review version too. That the
    finding exists is the caller's to check, against the archive."""
    if not category.strip() or not body.strip():
        raise FlagError("Flag category and body are required")
    repository = validate_target(repository, pull_number, review_version, finding_id)
    lock = path.parent / ".locks" / "flags.lock"
    with ResourceLock(lock):
        store = load_store(path)
        numeric = store["next_id"]
        now = datetime.now(UTC).isoformat()
        flag = {
            "id": f"RF-{numeric:06d}",
            "status": "open",
            "created_at": now,
            "resolved_at": None,
            "repository": repository,
            "pull_number": pull_number,
            "review_version": review_version,
            "finding_id": finding_id,
            "category": category,
            "body": body,
            "resolution": None,
        }
        store["next_id"] = numeric + 1
        store["flags"].append(flag)
        atomic_write_json(path, store, validator=validate_store)
        return flag


def resolve_flag(path: Path, flag_id: str, resolution: str) -> dict[str, Any]:
    """Resolve under the store lock, or raise FlagAlreadyResolved without changing the earlier resolution."""
    if not resolution.strip():
        raise FlagError("Resolution is required")
    lock = path.parent / ".locks" / "flags.lock"
    with ResourceLock(lock):
        store = load_store(path)
        matching = [item for item in store["flags"] if item["id"] == flag_id]
        if len(matching) != 1:
            raise FlagError(f"Unknown flag ID: {flag_id}")
        flag = matching[0]
        if flag["status"] != "open":
            raise FlagAlreadyResolved(flag_id)
        flag["status"] = "resolved"
        flag["resolved_at"] = datetime.now(UTC).isoformat()
        flag["resolution"] = resolution
        atomic_write_json(path, store, validator=validate_store)
        return flag

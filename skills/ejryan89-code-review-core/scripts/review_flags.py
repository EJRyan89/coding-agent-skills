"""Structured flag creation, listing, editing, and resolution."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from review_config import validate_repository_identity
from review_io import ResourceLock, atomic_write_json, read_json
from review_records import FINDING_ID
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
# The most a flag store is read at, and so the most a write may leave it holding.
STORE_MAXIMUM_BYTES = 4 * 1024 * 1024
# The most characters a new or edited category and body hold, so one flag cannot take the store near its bound.
CATEGORY_LIMIT = 120
BODY_LIMIT = 4000


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
    if not path.exists():
        return empty_store()
    return validate_store(upgrade_store(read_json(path, maximum_bytes=STORE_MAXIMUM_BYTES)))


def _write_store(path: Path, store: dict[str, Any]) -> None:
    """Write the store as atomic_write_json serializes it, or refuse, writing nothing, a store too large to read."""
    size = len((json.dumps(store, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
    if size > STORE_MAXIMUM_BYTES:
        raise FlagError(f"The flag store would exceed {STORE_MAXIMUM_BYTES} bytes, the most a flag store is read at")
    atomic_write_json(path, store, validator=validate_store)


def _check_text(text: str, field: str) -> str:
    """A category or body that is not blank and within its limit."""
    if not text.strip():
        raise FlagError(f"Flag {field} must not be blank")
    limit = CATEGORY_LIMIT if field == "category" else BODY_LIMIT
    if len(text) > limit:
        raise FlagError(f"Flag {field} holds {len(text)} characters; the most it holds is {limit}")
    return text


def validate_target(
    repository: str | None, pull_number: int | None, review_version: int | None, finding_id: str | None
) -> str | None:
    """What a new flag is about, checked before anything is read: a finding ID in the form reports give it, named
    with the repository, pull request, and review version that make it one finding; a pull request named with its
    repository; and a review version named with the finding it gives the version of. Returns the repository."""
    if finding_id is not None and not FINDING_ID.fullmatch(finding_id):
        raise FlagError(
            f"Finding ID {finding_id!r} is not a finding ID such as F001; a report label such as v2 F003 names "
            "review version 2 and finding F003"
        )
    if finding_id is not None and None in (repository, pull_number, review_version):
        raise FlagError("A flag that names a finding must name its repository, pull request, and review version")
    if review_version is not None and (pull_number is None or review_version < 1):
        raise FlagError("Review version must be positive and name a pull request")
    if review_version is not None and finding_id is None:
        raise FlagError("A flag that names a review version must name the finding in it")
    if pull_number is not None and repository is None:
        raise FlagError("A flag that names a pull request must name its repository")
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
    _check_text(category, "category")
    _check_text(body, "body")
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
        _write_store(path, store)
        return flag


def _stored_flag(store: dict[str, Any], flag_id: str) -> dict[str, Any]:
    matching = [item for item in store["flags"] if item["id"] == flag_id]
    if len(matching) != 1:
        raise FlagError(f"Unknown flag ID: {flag_id}")
    return matching[0]


def _refuse_without_store(path: Path, flag_id: str) -> None:
    """Refuse a flag ID when there is no store, before the lock creates the store's folder."""
    if not path.exists():
        raise FlagError(f"Unknown flag ID: {flag_id}")


def _new_text(value: Any, field: str) -> str | None:
    """A replacement for a flag's category or body: a string that is not blank, or None to keep the old one."""
    if value is None:
        return None
    return _check_text(READ.string(value, f"flag.{field}", empty=True), field)


def edit_flag(path: Path, flag_id: str, *, category: str | None = None, body: str | None = None) -> dict[str, Any]:
    """Replace an open flag's category, body, or both under the store lock. What the flag is about, its status, and
    its times stay as they are; a resolved or unknown flag is refused without a write."""
    replacements = {"category": _new_text(category, "category"), "body": _new_text(body, "body")}
    changes = {field: text for field, text in replacements.items() if text is not None}
    if not changes:
        raise FlagError("Name a new category, a new body, or both")
    _refuse_without_store(path, flag_id)
    lock = path.parent / ".locks" / "flags.lock"
    with ResourceLock(lock):
        store = load_store(path)
        flag = _stored_flag(store, flag_id)
        if flag["status"] != "open":
            raise FlagError(f"Flag {flag_id} is resolved; only an open flag is edited")
        flag.update(changes)
        _write_store(path, store)
        return flag


def resolve_flag(path: Path, flag_id: str, resolution: str) -> dict[str, Any]:
    """Resolve under the store lock, or raise FlagAlreadyResolved without changing the earlier resolution."""
    if not resolution.strip():
        raise FlagError("Resolution is required")
    _refuse_without_store(path, flag_id)
    lock = path.parent / ".locks" / "flags.lock"
    with ResourceLock(lock):
        store = load_store(path)
        flag = _stored_flag(store, flag_id)
        if flag["status"] != "open":
            raise FlagAlreadyResolved(flag_id)
        flag["status"] = "resolved"
        flag["resolved_at"] = datetime.now(UTC).isoformat()
        flag["resolution"] = resolution
        _write_store(path, store)
        return flag

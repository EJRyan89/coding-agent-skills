"""Structured flag creation, listing, and resolution."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import re
from pathlib import Path
from typing import Any

from review_config import validate_repository_identity
from review_io import ResourceLock, atomic_write_json, read_json


# Version 2 adds review_version, so a finding ID names one review; finding IDs restart at F001 in every review.
SCHEMA_VERSION = 2
FIELDS = frozenset({
    "id", "status", "created_at", "resolved_at", "repository", "pull_number", "review_version", "finding_id",
    "category", "body", "resolution",
})


class FlagError(ValueError):
    pass


def default_flags_path() -> Path:
    """CODE_REVIEW_FLAGS, else the standard flag store beside the code-review configuration."""
    override = os.environ.get("CODE_REVIEW_FLAGS")
    if override:
        return Path(override)
    return Path.home() / ".coding-agent-skills" / "code-review" / "flags.json"


def empty_store() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "next_id": 1, "flags": []}


def validate_store(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "next_id", "flags"}:
        raise FlagError("Flag store shape is invalid")
    if value["schema_version"] != SCHEMA_VERSION:
        raise FlagError("Flag store schema version is unsupported")
    if not isinstance(value["next_id"], int) or isinstance(value["next_id"], bool) or value["next_id"] < 1:
        raise FlagError("Flag next_id is invalid")
    if not isinstance(value["flags"], list):
        raise FlagError("Flag list is invalid")
    ids: set[str] = set()
    for flag in value["flags"]:
        if not isinstance(flag, dict) or set(flag) != FIELDS:
            raise FlagError("Flag record shape is invalid")
        if not isinstance(flag["id"], str) or flag["id"] in ids:
            raise FlagError("Flag IDs must be unique strings")
        if not re.fullmatch(r"RF-[0-9]{6}", flag["id"]):
            raise FlagError("Flag ID format is invalid")
        ids.add(flag["id"])
        if flag["status"] not in {"open", "resolved"}:
            raise FlagError("Flag status is invalid")
        if flag["repository"] is not None:
            validate_repository_identity(flag["repository"])
        if flag["pull_number"] is not None and (
            not isinstance(flag["pull_number"], int)
            or isinstance(flag["pull_number"], bool)
            or flag["pull_number"] < 1
        ):
            raise FlagError("Flag pull number is invalid")
        if flag["review_version"] is not None and (
            not _positive(flag["review_version"]) or flag["pull_number"] is None
        ):
            raise FlagError("Flag review version is invalid")
        for field in ("created_at", "category", "body"):
            if not isinstance(flag[field], str) or not flag[field].strip():
                raise FlagError(f"Flag {field} is invalid")
        try:
            datetime.fromisoformat(flag["created_at"])
        except ValueError as exc:
            raise FlagError("Flag created_at is invalid") from exc
        if flag["status"] == "open":
            if flag["resolved_at"] is not None or flag["resolution"] is not None:
                raise FlagError("Open flag cannot contain resolution metadata")
        else:
            if (
                not isinstance(flag["resolved_at"], str)
                or not isinstance(flag["resolution"], str)
                or not flag["resolution"].strip()
            ):
                raise FlagError("Resolved flag requires resolution metadata")
            try:
                datetime.fromisoformat(flag["resolved_at"])
            except ValueError as exc:
                raise FlagError("Flag resolved_at is invalid") from exc
    allocated = [int(item["id"].removeprefix("RF-")) for item in value["flags"]]
    if allocated and value["next_id"] <= max(allocated):
        raise FlagError("Flag next_id must be greater than every allocated ID")
    return value


def _positive(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def upgrade_store(value: Any) -> Any:
    """A version 1 store as version 2: its flags name no review version, so none of them can be linked to a finding."""
    if isinstance(value, dict) and value.get("schema_version") == 1 and isinstance(value.get("flags"), list):
        value = {**value, "schema_version": SCHEMA_VERSION, "flags": [
            {**flag, "review_version": None} if isinstance(flag, dict) and "review_version" not in flag else flag
            for flag in value["flags"]
        ]}
    return value


def load_store(path: Path) -> dict[str, Any]:
    return validate_store(upgrade_store(read_json(path))) if path.exists() else empty_store()


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
    """Add an open flag. A finding is named by its ID within one review, so it needs the review version too."""
    if not category.strip() or not body.strip():
        raise FlagError("Flag category and body are required")
    if finding_id is not None and None in (repository, pull_number, review_version):
        raise FlagError("A flag that names a finding must name its repository, pull request, and review version")
    if review_version is not None and (pull_number is None or not _positive(review_version)):
        raise FlagError("Review version must be positive and name a pull request")
    if repository is not None:
        repository = validate_repository_identity(repository)
    if pull_number is not None and (
        not isinstance(pull_number, int)
        or isinstance(pull_number, bool)
        or pull_number < 1
    ):
        raise FlagError("Pull number must be positive")
    lock = path.parent / ".locks" / "flags.lock"
    with ResourceLock(lock):
        store = load_store(path)
        numeric = store["next_id"]
        now = datetime.now(timezone.utc).isoformat()
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
            raise FlagError(f"Flag is already resolved: {flag_id}")
        flag["status"] = "resolved"
        flag["resolved_at"] = datetime.now(timezone.utc).isoformat()
        flag["resolution"] = resolution
        atomic_write_json(path, store, validator=validate_store)
        return flag

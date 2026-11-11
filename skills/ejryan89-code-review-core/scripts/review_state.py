"""Versioned per-repository mutable review state."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import calendar_date
from executable_locator import absolute_home
from review_config import validate_repository_identity
from review_io import ResourceLock, atomic_write_json, read_json, record_present
from typed_fields import FieldError, FieldReader

SCHEMA_VERSION = 1


class StateError(ValueError):
    pass


class StateFieldError(FieldError, StateError):
    """A review state field of the wrong kind."""


READ = FieldReader(StateFieldError)


def default_state_path() -> Path:
    override = os.environ.get("CODE_REVIEW_STATE")
    if override:
        return Path(override)
    return absolute_home() / ".coding-agent-skills" / "code-review" / "state.json"


def utc_today() -> date:
    """Today's date in UTC, the calendar GitHub's merge and update times are read on.

    A watermark set from the local date east of UTC would be a day ahead of a pull request merged later that UTC day.
    """
    return datetime.fromtimestamp(time.time(), UTC).date()


def utc_date(value: str) -> date:
    """The UTC date of an ISO 8601 time, such as GitHub's `mergedAt`; a time without an offset is already UTC."""
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).date()


def watermark_date(identity: str, value: str) -> date:
    """The date a repository's merged_since watermark names: its first ten characters, as YYYY-MM-DD."""
    # The contract's YYYY-MM-DD: date.fromisoformat alone also takes a week date or the basic form.
    found = calendar_date.parse(value[:10])
    if found is not None:
        return found
    raise StateError(f"State {identity}.merged_since must begin with a YYYY-MM-DD date, not {value!r}")


def validate_state(value: Any) -> dict[str, Any]:
    """The state, its repositories keyed by their lowercase identities; a key in another case reads as that identity,
    and two spellings of one identity are refused."""
    state = READ.json_object(value, "state", optional={"schema_version", "repositories"})
    version = READ.integer(state.get("schema_version"), "state.schema_version")
    if version != SCHEMA_VERSION:
        raise StateError(f"Unsupported state schema version: {version!r}")
    repositories = READ.json_object(state.get("repositories"), "state.repositories")
    keyed: dict[str, Any] = {}
    for identity, entry in repositories.items():
        # Identities match ignoring case, so each entry is keyed by the lowercase form every lookup and write uses,
        # and two spellings of one repository are refused: which one's watermark holds cannot be told.
        key = validate_repository_identity(READ.string(identity, "a key of state.repositories"))
        if key in keyed:
            raise StateError(f"State repositories hold {key} under more than one spelling")
        keyed[key] = entry
        path = f"state.repositories.{identity}"
        READ.json_object(entry, path, optional={"merged_since", "updated_at"})
        for field in ("merged_since", "updated_at"):
            if field in entry:
                READ.string(entry[field], f"{path}.{field}", empty=True)
        if "merged_since" in entry:
            watermark_date(identity, entry["merged_since"])
    return {**state, "repositories": keyed}


def empty_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "repositories": {}}


def load_state(path: Path | None = None) -> dict[str, Any]:
    """The validated batch state, or an empty one before the first batch run writes it. A state file it cannot stat,
    or anything else in its place, is the FileUnreadable `record_present` raises, never a first run's empty state, which
    would start every repository's watermark over."""
    selected = path or default_state_path()
    if not record_present(selected):
        return empty_state()
    return validate_state(read_json(selected))


def update_state(path: Path, update: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    lock_path = path.parent / ".locks" / "state.lock"
    with ResourceLock(lock_path):
        current = load_state(path)
        replacement = validate_state(update(current))
        atomic_write_json(path, replacement, validator=validate_state)
        return replacement

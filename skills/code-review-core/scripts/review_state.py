"""Versioned per-repository mutable review state."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from review_config import validate_repository_identity
from review_io import ResourceLock, atomic_write_json, read_json

SCHEMA_VERSION = 1


class StateError(ValueError):
    pass


def default_state_path() -> Path:
    override = os.environ.get("CODE_REVIEW_STATE")
    if override:
        return Path(override)
    return Path.home() / ".coding-agent-skills" / "code-review" / "state.json"


def validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"schema_version", "repositories"}:
        raise StateError("State must contain only schema_version and repositories")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise StateError(f"Unsupported state schema version: {value.get('schema_version')!r}")
    repositories = value.get("repositories")
    if not isinstance(repositories, dict):
        raise StateError("State repositories must be an object")
    for identity, state in repositories.items():
        validate_repository_identity(identity)
        if not isinstance(state, dict):
            raise StateError(f"State for {identity} must be an object")
        if set(state) - {"merged_since", "updated_at"}:
            raise StateError(f"State for {identity} contains unknown fields")
        for field in ("merged_since", "updated_at"):
            if field in state and not isinstance(state[field], str):
                raise StateError(f"State {identity}.{field} must be a string")
    return value


def empty_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "repositories": {}}


def load_state(path: Path | None = None) -> dict[str, Any]:
    selected = path or default_state_path()
    if not selected.exists():
        return empty_state()
    return validate_state(read_json(selected))


def update_state(
    path: Path,
    update: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lock_path = path.parent / ".locks" / "state.lock"
    with ResourceLock(lock_path):
        current = load_state(path)
        if expected is not None and current != expected:
            raise StateError("State changed after it was read; retry with fresh state")
        replacement = validate_state(update(current))
        atomic_write_json(path, replacement, validator=validate_state)
        return replacement

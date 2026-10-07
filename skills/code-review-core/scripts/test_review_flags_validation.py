"""validate_store, pinned: every flag store it accepts, every fault it refuses with its exact error, and the order in
which it detects faults. The stores are literal, so a change to which flag stores load shows up here."""

from __future__ import annotations

import copy
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_config import ConfigurationError
from review_flags import FlagError, validate_store

Mutation = Callable[[Any], Any]
Key = str | int


def _open_flag() -> dict[str, Any]:
    return {
        "id": "RF-000001",
        "status": "open",
        "created_at": "2026-10-06T12:00:00+00:00",
        "resolved_at": None,
        "repository": "owner/repo",
        "pull_number": 7,
        "review_version": 2,
        "finding_id": "F001",
        "category": "false-positive",
        "body": "Not a bug.",
        "resolution": None,
    }


def _resolved_flag() -> dict[str, Any]:
    return {
        "id": "RF-000002",
        "status": "resolved",
        "created_at": "2026-10-06T12:00:00",
        "resolved_at": "2026-10-07",
        "repository": None,
        "pull_number": None,
        "review_version": None,
        "finding_id": None,
        "category": "noise",
        "body": "Too chatty.",
        "resolution": "Tuned the prompt.",
    }


def _store() -> dict[str, Any]:
    """An open flag on a finding and a resolved flag on nothing in particular."""
    return {"schema_version": 2, "next_id": 3, "flags": [_open_flag(), _resolved_flag()]}


def _set(path: tuple[Key, ...], value: Any) -> Mutation:
    def apply(store: Any) -> Any:
        target = store
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = copy.deepcopy(value)
        return store

    return apply


def _delete(path: tuple[Key, ...]) -> Mutation:
    def apply(store: Any) -> Any:
        target = store
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
        return store

    return apply


def _replace(value: Any) -> Mutation:
    return lambda _ignored: copy.deepcopy(value)


def _chain(*mutations: Mutation) -> Mutation:
    def apply(store: Any) -> Any:
        for mutation in mutations:
            store = mutation(store)
        return store

    return apply


def _same(store: Any) -> Any:
    return store


SHAPE = "Flag store shape is invalid"
SCHEMA = "Flag store schema version is unsupported"
NEXT_ID = "Flag next_id is invalid"
RECORD = "Flag record shape is invalid"
UNIQUE = "Flag IDs must be unique strings"
ID_FORMAT = "Flag ID format is invalid"
PULL = "Flag pull number is invalid"
REVIEW_VERSION = "Flag review version is invalid"
OPEN_METADATA = "Open flag cannot contain resolution metadata"
RESOLVED_METADATA = "Resolved flag requires resolution metadata"
ALLOCATED = "Flag next_id must be greater than every allocated ID"

# Stores validate_store accepts, from the base store.
ACCEPTED: list[tuple[str, Mutation]] = [
    ("base", _same),
    ("no flags", _chain(_set(("flags",), []), _set(("next_id",), 1))),
    ("no flags and a later next_id", _set(("flags",), [])),
    ("a next_id well past the last ID", _set(("next_id",), 1000)),
    ("schema version 2.0, which equals 2", _set(("schema_version",), 2.0)),
    ("IDs out of order", _chain(_set(("flags", 0, "id"), "RF-000002"), _set(("flags", 1, "id"), "RF-000001"))),
    ("a repository in another case", _set(("flags", 0, "repository"), "Owner/Repo")),
    ("a pull number without a repository", _set(("flags", 0, "repository"), None)),
    ("a pull number without a review version", _set(("flags", 0, "review_version"), None)),
    (
        "no repository, pull number, or review version",
        _chain(*(_set(("flags", 0, k), None) for k in ("repository", "pull_number", "review_version"))),
    ),
    ("a finding ID of any value", _set(("flags", 0, "finding_id"), 5)),
    ("a created_at with Z", _set(("flags", 0, "created_at"), "2026-10-06T12:00:00Z")),
    ("a created_at that is only a date", _set(("flags", 0, "created_at"), "2026-10-06")),
    ("a resolution with spaces around it", _set(("flags", 1, "resolution"), " Tuned. ")),
    ("a category with spaces around it", _set(("flags", 0, "category"), " noise ")),
]

# Stores validate_store refuses, from the base store, with the exception class and message it raises.
REJECTED: list[tuple[str, Mutation, type[Exception], str]] = [
    ("a list", _replace([]), FlagError, SHAPE),
    ("null", _replace(None), FlagError, SHAPE),
    ("no flags field", _delete(("flags",)), FlagError, SHAPE),
    ("an extra field", _set(("extra",), 1), FlagError, SHAPE),
    ("schema version 1", _set(("schema_version",), 1), FlagError, SCHEMA),
    ("schema version as a string", _set(("schema_version",), "2"), FlagError, SCHEMA),
    ("schema version true", _set(("schema_version",), True), FlagError, SCHEMA),
    ("a next_id that is a string", _set(("next_id",), "3"), FlagError, NEXT_ID),
    ("a next_id that is true", _set(("next_id",), True), FlagError, NEXT_ID),
    ("a next_id of 0", _set(("next_id",), 0), FlagError, NEXT_ID),
    ("a negative next_id", _set(("next_id",), -1), FlagError, NEXT_ID),
    ("a next_id that is a float", _set(("next_id",), 3.0), FlagError, NEXT_ID),
    ("flags that are an object", _set(("flags",), {}), FlagError, "Flag list is invalid"),
    ("a flag that is a list", _set(("flags", 0), []), FlagError, RECORD),
    ("a flag without a body", _delete(("flags", 0, "body")), FlagError, RECORD),
    ("a flag with an extra field", _set(("flags", 0, "extra"), 1), FlagError, RECORD),
    ("an ID that is a number", _set(("flags", 0, "id"), 1), FlagError, UNIQUE),
    ("an ID used twice", _set(("flags", 1, "id"), "RF-000001"), FlagError, UNIQUE),
    ("a short ID", _set(("flags", 0, "id"), "RF-1"), FlagError, ID_FORMAT),
    ("a lowercase ID", _set(("flags", 0, "id"), "rf-000001"), FlagError, ID_FORMAT),
    ("a long ID", _set(("flags", 0, "id"), "RF-0000001"), FlagError, ID_FORMAT),
    ("an unknown status", _set(("flags", 0, "status"), "closed"), FlagError, "Flag status is invalid"),
    ("a null status", _set(("flags", 0, "status"), None), FlagError, "Flag status is invalid"),
    (
        "an invalid repository",
        _set(("flags", 0, "repository"), "owner"),
        ConfigurationError,
        "Invalid repository identity: 'owner'",
    ),
    (
        "a repository that is not a string",
        _set(("flags", 0, "repository"), 5),
        ConfigurationError,
        "Invalid repository identity: 5",
    ),
    ("a pull number that is a string", _set(("flags", 0, "pull_number"), "7"), FlagError, PULL),
    ("a pull number that is true", _set(("flags", 0, "pull_number"), True), FlagError, PULL),
    ("a pull number of 0", _set(("flags", 0, "pull_number"), 0), FlagError, PULL),
    ("a review version of 0", _set(("flags", 0, "review_version"), 0), FlagError, REVIEW_VERSION),
    ("a review version that is true", _set(("flags", 0, "review_version"), True), FlagError, REVIEW_VERSION),
    ("a review version that is a string", _set(("flags", 0, "review_version"), "2"), FlagError, REVIEW_VERSION),
    (
        "a review version without a pull number",
        _set(("flags", 0, "pull_number"), None),
        FlagError,
        REVIEW_VERSION,
    ),
    ("a created_at that is not a string", _set(("flags", 0, "created_at"), 5), FlagError, "Flag created_at is invalid"),
    ("a blank created_at", _set(("flags", 0, "created_at"), " "), FlagError, "Flag created_at is invalid"),
    ("an empty category", _set(("flags", 0, "category"), ""), FlagError, "Flag category is invalid"),
    ("a category that is not a string", _set(("flags", 0, "category"), None), FlagError, "Flag category is invalid"),
    ("a blank body", _set(("flags", 0, "body"), "\n"), FlagError, "Flag body is invalid"),
    (
        "a created_at that is not a date",
        _set(("flags", 0, "created_at"), "yesterday"),
        FlagError,
        "Flag created_at is invalid",
    ),
    ("an open flag resolved at a time", _set(("flags", 0, "resolved_at"), "2026-10-07"), FlagError, OPEN_METADATA),
    ("an open flag with a resolution", _set(("flags", 0, "resolution"), "Done."), FlagError, OPEN_METADATA),
    ("an open flag with an empty resolution", _set(("flags", 0, "resolution"), ""), FlagError, OPEN_METADATA),
    ("a resolved flag without a time", _set(("flags", 1, "resolved_at"), None), FlagError, RESOLVED_METADATA),
    ("a resolved flag with a numeric time", _set(("flags", 1, "resolved_at"), 5), FlagError, RESOLVED_METADATA),
    ("a resolved flag without a resolution", _set(("flags", 1, "resolution"), None), FlagError, RESOLVED_METADATA),
    ("a resolved flag with a blank resolution", _set(("flags", 1, "resolution"), "  "), FlagError, RESOLVED_METADATA),
    (
        "a resolved_at that is not a date",
        _set(("flags", 1, "resolved_at"), "today"),
        FlagError,
        "Flag resolved_at is invalid",
    ),
    ("a resolved_at that is blank", _set(("flags", 1, "resolved_at"), ""), FlagError, "Flag resolved_at is invalid"),
    ("a next_id equal to the last ID", _set(("next_id",), 2), FlagError, ALLOCATED),
    ("a next_id below the last ID", _set(("next_id",), 1), FlagError, ALLOCATED),
    (
        "a next_id below an ID listed first",
        _chain(_set(("flags", 0, "id"), "RF-000009"), _set(("next_id",), 9)),
        FlagError,
        ALLOCATED,
    ),
    (
        "the first faulty flag is the one reported",
        _chain(_set(("flags", 0, "status"), "closed"), _set(("flags", 1, "id"), "RF-1")),
        FlagError,
        "Flag status is invalid",
    ),
]

# One fault per check, in the order validate_store detects them. Each later fault is applied first, so an earlier
# fault on the same field wins. Faults land on the open flag, except the resolved flag's own checks.
STAGES: list[tuple[str, Mutation, type[Exception], str]] = [
    ("store shape", _set(("extra",), 1), FlagError, SHAPE),
    ("schema version", _set(("schema_version",), 1), FlagError, SCHEMA),
    ("next_id", _set(("next_id",), 0), FlagError, NEXT_ID),
    ("flag list", _set(("flags",), {}), FlagError, "Flag list is invalid"),
    ("record shape", _set(("flags", 0, "extra"), 1), FlagError, RECORD),
    ("unique ID", _set(("flags", 0, "id"), 1), FlagError, UNIQUE),
    ("ID format", _set(("flags", 0, "id"), "RF-1"), FlagError, ID_FORMAT),
    ("status", _set(("flags", 0, "status"), "closed"), FlagError, "Flag status is invalid"),
    (
        "repository",
        _set(("flags", 0, "repository"), "owner"),
        ConfigurationError,
        "Invalid repository identity: 'owner'",
    ),
    ("pull number", _set(("flags", 0, "pull_number"), 0), FlagError, PULL),
    ("review version", _set(("flags", 0, "review_version"), 0), FlagError, REVIEW_VERSION),
    ("text field", _set(("flags", 0, "category"), ""), FlagError, "Flag category is invalid"),
    ("created_at", _set(("flags", 0, "created_at"), "yesterday"), FlagError, "Flag created_at is invalid"),
    ("open metadata", _set(("flags", 0, "resolution"), "Done."), FlagError, OPEN_METADATA),
    ("resolved metadata", _set(("flags", 1, "resolution"), None), FlagError, RESOLVED_METADATA),
    ("resolved_at", _set(("flags", 1, "resolved_at"), "today"), FlagError, "Flag resolved_at is invalid"),
    ("allocated IDs", _set(("next_id",), 2), FlagError, ALLOCATED),
]


class FlagStoreValidationTests(unittest.TestCase):
    def assert_refused(self, store: Any, error: type[Exception], message: str) -> None:
        with self.assertRaises(Exception) as caught:
            validate_store(store)
        self.assertIs(error, type(caught.exception))
        self.assertEqual(message, str(caught.exception))

    def test_accepted_stores_are_returned_unchanged(self) -> None:
        for name, mutation in ACCEPTED:
            with self.subTest(name):
                store = mutation(_store())
                before = copy.deepcopy(store)
                self.assertIs(store, validate_store(store))
                self.assertEqual(before, store)

    def test_each_fault_is_refused_with_its_error(self) -> None:
        for name, mutation, error, message in REJECTED:
            with self.subTest(name):
                self.assert_refused(mutation(_store()), error, message)

    def test_each_stage_is_refused_alone(self) -> None:
        for name, mutation, error, message in STAGES:
            with self.subTest(name):
                self.assert_refused(mutation(_store()), error, message)

    def test_faults_are_detected_in_order(self) -> None:
        # With the fault of every check from k on present at once, check k's fault is the one reported.
        for index, (name, _mutation, error, message) in enumerate(STAGES):
            with self.subTest(name):
                store: Any = _store()
                for _later, mutation, _error, _message in reversed(STAGES[index:]):
                    store = mutation(store)
                self.assert_refused(store, error, message)

    def test_stages_cover_every_distinct_error(self) -> None:
        stage_errors = {(error, message) for _name, _mutation, error, message in STAGES}
        rejected_errors = {(error, message) for _name, _mutation, error, message in REJECTED}
        # The same checks as a stage, with a message that names another value or field.
        variants = {(ConfigurationError, "Invalid repository identity: 5"), (FlagError, "Flag body is invalid")}
        self.assertEqual(set(), rejected_errors - stage_errors - variants)


if __name__ == "__main__":
    unittest.main()

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
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from review_config import ConfigurationError
from review_flags import FlagError, FlagFieldError, upgrade_store, validate_store
from typed_fields import FieldError

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


F = FlagFieldError
SCHEMA = "Flag store schema version is unsupported"
UNIQUE = "Flag IDs must be unique strings"
ID_FORMAT = "Flag ID format is invalid"
REVIEW_VERSION = "Flag review version is invalid"
OPEN_METADATA = "Open flag cannot contain resolution metadata"
RESOLVED_METADATA = "Resolved flag requires resolution metadata"
ALLOCATED = "Flag next_id must be greater than every allocated ID"
FIRST = "flag-store.flags[0]"
SECOND = "flag-store.flags[1]"
NEXT_ID = "flag-store.next_id must be an integer of at least 1, found"
STATUS = 'flag-store.flags[0].status must be one of "open", "resolved", found'
PULL = "flag-store.flags[0].pull_number must be an integer of at least 1 or null, found"
VERSION = "flag-store.flags[0].review_version must be an integer of at least 1 or null, found"
FINDING = "flag-store.flags[0].finding_id must be a string or null, found"

# Stores validate_store accepts, from the base store.
ACCEPTED: list[tuple[str, Mutation]] = [
    ("base", _same),
    ("no flags", _chain(_set(("flags",), []), _set(("next_id",), 1))),
    ("no flags and a later next_id", _set(("flags",), [])),
    ("a next_id well past the last ID", _set(("next_id",), 1000)),
    ("IDs out of order", _chain(_set(("flags", 0, "id"), "RF-000002"), _set(("flags", 1, "id"), "RF-000001"))),
    ("a repository in another case", _set(("flags", 0, "repository"), "Owner/Repo")),
    ("a pull number without a repository", _set(("flags", 0, "repository"), None)),
    ("a pull number without a review version", _set(("flags", 0, "review_version"), None)),
    (
        "no repository, pull number, or review version",
        _chain(*(_set(("flags", 0, k), None) for k in ("repository", "pull_number", "review_version"))),
    ),
    ("a finding ID in any form", _set(("flags", 0, "finding_id"), "v2 F002")),
    ("an empty finding ID", _set(("flags", 0, "finding_id"), "")),
    ("a created_at with Z", _set(("flags", 0, "created_at"), "2026-10-06T12:00:00Z")),
    ("a created_at that is only a date", _set(("flags", 0, "created_at"), "2026-10-06")),
    ("a resolution with spaces around it", _set(("flags", 1, "resolution"), " Tuned. ")),
    ("a category with spaces around it", _set(("flags", 0, "category"), " noise ")),
]

# Stores validate_store refuses, from the base store, with the exception class and message it raises. A field of the
# wrong kind raises FlagFieldError; a value of the right kind that breaks a rule raises FlagError.
REJECTED: list[tuple[str, Mutation, type[Exception], str]] = [
    ("a list", _replace([]), F, "flag-store must be an object, found an array"),
    ("null", _replace(None), F, "flag-store must be an object, found null"),
    ("a string", _replace("{}"), F, 'flag-store must be an object, found string "{}"'),
    (
        "no flags field",
        _delete(("flags",)),
        F,
        "flag-store must be an object with field flags, found an object without it",
    ),
    (
        "no fields",
        _replace({}),
        F,
        "flag-store must be an object with fields flags, next_id, schema_version, found an object without them",
    ),
    (
        "an extra field",
        _set(("extra",), 1),
        F,
        'flag-store must be an object of known fields, found unknown field "extra"',
    ),
    ("schema version 1", _set(("schema_version",), 1), FlagError, SCHEMA),
    (
        "schema version as a string",
        _set(("schema_version",), "2"),
        F,
        'flag-store.schema_version must be an integer, found string "2"',
    ),
    (
        "schema version true",
        _set(("schema_version",), True),
        F,
        "flag-store.schema_version must be an integer, found boolean true",
    ),
    (
        "schema version 2.0",
        _set(("schema_version",), 2.0),
        F,
        "flag-store.schema_version must be an integer, found number 2.0",
    ),
    ("a next_id that is a string", _set(("next_id",), "3"), F, f'{NEXT_ID} string "3"'),
    ("a next_id that is true", _set(("next_id",), True), F, f"{NEXT_ID} boolean true"),
    ("a next_id of 0", _set(("next_id",), 0), F, f"{NEXT_ID} integer 0"),
    ("a negative next_id", _set(("next_id",), -1), F, f"{NEXT_ID} integer -1"),
    ("a next_id that is a float", _set(("next_id",), 3.0), F, f"{NEXT_ID} number 3.0"),
    ("a null next_id", _set(("next_id",), None), F, f"{NEXT_ID} null"),
    ("flags that are an object", _set(("flags",), {}), F, "flag-store.flags must be an array, found an object"),
    ("flags that are null", _set(("flags",), None), F, "flag-store.flags must be an array, found null"),
    ("a flag that is a list", _set(("flags", 0), []), F, f"{FIRST} must be an object, found an array"),
    (
        "a flag that is a string",
        _set(("flags", 0), "RF-000001"),
        F,
        f'{FIRST} must be an object, found string "RF-000001"',
    ),
    (
        "a flag without a body",
        _delete(("flags", 0, "body")),
        F,
        f"{FIRST} must be an object with field body, found an object without it",
    ),
    (
        "a flag with an extra field",
        _set(("flags", 0, "extra"), 1),
        F,
        f'{FIRST} must be an object of known fields, found unknown field "extra"',
    ),
    (
        "an ID that is a number",
        _set(("flags", 0, "id"), 1),
        F,
        f"{FIRST}.id must be a non-empty string, found integer 1",
    ),
    (
        "an empty ID",
        _set(("flags", 0, "id"), ""),
        F,
        f"{FIRST}.id must be a non-empty string, found an empty string",
    ),
    ("an ID used twice", _set(("flags", 1, "id"), "RF-000001"), FlagError, UNIQUE),
    ("a short ID", _set(("flags", 0, "id"), "RF-1"), FlagError, ID_FORMAT),
    ("a lowercase ID", _set(("flags", 0, "id"), "rf-000001"), FlagError, ID_FORMAT),
    ("a long ID", _set(("flags", 0, "id"), "RF-0000001"), FlagError, ID_FORMAT),
    ("an unknown status", _set(("flags", 0, "status"), "closed"), F, f'{STATUS} string "closed"'),
    ("a null status", _set(("flags", 0, "status"), None), F, f"{STATUS} null"),
    ("a status that is true", _set(("flags", 0, "status"), True), F, f"{STATUS} boolean true"),
    ("a status that is a list", _set(("flags", 0, "status"), ["open"]), F, f"{STATUS} an array"),
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
    ("a pull number that is a string", _set(("flags", 0, "pull_number"), "7"), F, f'{PULL} string "7"'),
    ("a pull number that is true", _set(("flags", 0, "pull_number"), True), F, f"{PULL} boolean true"),
    ("a pull number of 0", _set(("flags", 0, "pull_number"), 0), F, f"{PULL} integer 0"),
    ("a pull number that is a float", _set(("flags", 0, "pull_number"), 7.0), F, f"{PULL} number 7.0"),
    ("a review version of 0", _set(("flags", 0, "review_version"), 0), F, f"{VERSION} integer 0"),
    ("a review version that is true", _set(("flags", 0, "review_version"), True), F, f"{VERSION} boolean true"),
    ("a review version that is a string", _set(("flags", 0, "review_version"), "2"), F, f'{VERSION} string "2"'),
    (
        "a review version without a pull number",
        _set(("flags", 0, "pull_number"), None),
        FlagError,
        REVIEW_VERSION,
    ),
    ("a finding ID that is a number", _set(("flags", 0, "finding_id"), 5), F, f"{FINDING} integer 5"),
    ("a finding ID that is a list", _set(("flags", 0, "finding_id"), ["F001"]), F, f"{FINDING} an array"),
    (
        "a created_at that is not a string",
        _set(("flags", 0, "created_at"), 5),
        F,
        f"{FIRST}.created_at must be a non-empty string, found integer 5",
    ),
    (
        "a blank created_at",
        _set(("flags", 0, "created_at"), " "),
        F,
        f"{FIRST}.created_at must be a non-empty string, found a blank string",
    ),
    (
        "an empty category",
        _set(("flags", 0, "category"), ""),
        F,
        f"{FIRST}.category must be a non-empty string, found an empty string",
    ),
    (
        "a category that is not a string",
        _set(("flags", 0, "category"), None),
        F,
        f"{FIRST}.category must be a non-empty string, found null",
    ),
    (
        "a category that is an object",
        _set(("flags", 0, "category"), {}),
        F,
        f"{FIRST}.category must be a non-empty string, found an object",
    ),
    (
        "a blank body",
        _set(("flags", 0, "body"), "\n"),
        F,
        f"{FIRST}.body must be a non-empty string, found a blank string",
    ),
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
    (
        "a resolved flag with a numeric time",
        _set(("flags", 1, "resolved_at"), 5),
        F,
        f"{SECOND}.resolved_at must be a non-empty string, found integer 5",
    ),
    ("a resolved flag without a resolution", _set(("flags", 1, "resolution"), None), FlagError, RESOLVED_METADATA),
    (
        "a resolved flag with a blank resolution",
        _set(("flags", 1, "resolution"), "  "),
        F,
        f"{SECOND}.resolution must be a non-empty string, found a blank string",
    ),
    (
        "a resolved flag with a resolution that is a list",
        _set(("flags", 1, "resolution"), ["Tuned."]),
        F,
        f"{SECOND}.resolution must be a non-empty string, found an array",
    ),
    (
        "a resolved_at that is not a date",
        _set(("flags", 1, "resolved_at"), "today"),
        FlagError,
        "Flag resolved_at is invalid",
    ),
    (
        "a resolved_at that is blank",
        _set(("flags", 1, "resolved_at"), ""),
        F,
        f"{SECOND}.resolved_at must be a non-empty string, found an empty string",
    ),
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
        F,
        f'{STATUS} string "closed"',
    ),
]

# One fault per check, in the order validate_store detects them. Each later fault is applied first, so an earlier
# fault on the same field wins. Faults land on the open flag, except the resolved flag's own checks and the unique ID,
# which only a later flag can break.
STAGES: list[tuple[str, Mutation, type[Exception], str]] = [
    (
        "store shape",
        _set(("extra",), 1),
        F,
        'flag-store must be an object of known fields, found unknown field "extra"',
    ),
    (
        "schema version kind",
        _set(("schema_version",), "2"),
        F,
        'flag-store.schema_version must be an integer, found string "2"',
    ),
    ("schema version", _set(("schema_version",), 1), FlagError, SCHEMA),
    ("next_id", _set(("next_id",), 0), F, f"{NEXT_ID} integer 0"),
    ("flag list", _set(("flags",), {}), F, "flag-store.flags must be an array, found an object"),
    (
        "record shape",
        _set(("flags", 0, "extra"), 1),
        F,
        f'{FIRST} must be an object of known fields, found unknown field "extra"',
    ),
    ("ID kind", _set(("flags", 0, "id"), 1), F, f"{FIRST}.id must be a non-empty string, found integer 1"),
    ("ID format", _set(("flags", 0, "id"), "RF-1"), FlagError, ID_FORMAT),
    ("status", _set(("flags", 0, "status"), "closed"), F, f'{STATUS} string "closed"'),
    (
        "repository",
        _set(("flags", 0, "repository"), "owner"),
        ConfigurationError,
        "Invalid repository identity: 'owner'",
    ),
    ("pull number", _set(("flags", 0, "pull_number"), 0), F, f"{PULL} integer 0"),
    ("review version", _set(("flags", 0, "review_version"), 0), F, f"{VERSION} integer 0"),
    ("review version's pull request", _set(("flags", 0, "pull_number"), None), FlagError, REVIEW_VERSION),
    ("finding ID", _set(("flags", 0, "finding_id"), ["F001"]), F, f"{FINDING} an array"),
    (
        "created_at kind",
        _set(("flags", 0, "created_at"), 5),
        F,
        f"{FIRST}.created_at must be a non-empty string, found integer 5",
    ),
    (
        "category",
        _set(("flags", 0, "category"), ""),
        F,
        f"{FIRST}.category must be a non-empty string, found an empty string",
    ),
    ("body", _set(("flags", 0, "body"), "\n"), F, f"{FIRST}.body must be a non-empty string, found a blank string"),
    ("created_at", _set(("flags", 0, "created_at"), "yesterday"), FlagError, "Flag created_at is invalid"),
    ("open metadata", _set(("flags", 0, "resolution"), "Done."), FlagError, OPEN_METADATA),
    ("unique ID", _set(("flags", 1, "id"), "RF-000001"), FlagError, UNIQUE),
    ("resolved metadata", _set(("flags", 1, "resolution"), None), FlagError, RESOLVED_METADATA),
    (
        "resolved_at kind",
        _set(("flags", 1, "resolved_at"), 5),
        F,
        f"{SECOND}.resolved_at must be a non-empty string, found integer 5",
    ),
    (
        "resolution kind",
        _set(("flags", 1, "resolution"), "  "),
        F,
        f"{SECOND}.resolution must be a non-empty string, found a blank string",
    ),
    ("resolved_at", _set(("flags", 1, "resolved_at"), "today"), FlagError, "Flag resolved_at is invalid"),
    ("allocated IDs", _set(("next_id",), 2), FlagError, ALLOCATED),
]

# Stores upgrade_store leaves as they are, a wrong kind among them, with the error validate_store then raises.
NOT_UPGRADED: list[tuple[str, Any, type[Exception], str]] = [
    ("a list", [], F, "flag-store must be an object, found an array"),
    ("null", None, F, "flag-store must be an object, found null"),
    (
        "a version 1 that is a string",
        {"schema_version": "1", "next_id": 1, "flags": []},
        F,
        'flag-store.schema_version must be an integer, found string "1"',
    ),
    (
        "a version 1 that is true",
        {"schema_version": True, "next_id": 1, "flags": []},
        F,
        "flag-store.schema_version must be an integer, found boolean true",
    ),
    (
        "a version 1 that is a float",
        {"schema_version": 1.0, "next_id": 1, "flags": []},
        F,
        "flag-store.schema_version must be an integer, found number 1.0",
    ),
    (
        "no version",
        {"next_id": 1, "flags": []},
        F,
        "flag-store must be an object with field schema_version, found an object without it",
    ),
    ("a version 1 whose flags are an object", {"schema_version": 1, "next_id": 1, "flags": {}}, FlagError, SCHEMA),
    (
        "a version 1 without flags",
        {"schema_version": 1, "next_id": 1},
        F,
        "flag-store must be an object with field flags, found an object without it",
    ),
]


def _check_of(error: type[Exception], message: str) -> tuple[type[Exception], str]:
    """The check an error comes from: a field error's field, named before " must be ", or a rule's own message."""
    return (error, message.split(" must be ", 1)[0]) if error is F else (error, message)


class FlagStoreValidationTests(unittest.TestCase):
    def assert_refused(self, store: Any, error: type[Exception], message: str) -> None:
        with self.assertRaises(Exception) as caught:
            validate_store(store)
        self.assertIs(error, type(caught.exception))
        self.assertEqual(message, str(caught.exception))

    def test_a_field_error_is_a_flag_error(self) -> None:
        self.assertTrue(issubclass(FlagFieldError, FieldError))
        self.assertTrue(issubclass(FlagFieldError, FlagError))

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
        stage_checks = {_check_of(error, message) for _name, _mutation, error, message in STAGES}
        rejected_checks = {_check_of(error, message) for _name, _mutation, error, message in REJECTED}
        # The same check as a stage, with a message that names another value.
        variants = {(ConfigurationError, "Invalid repository identity: 5")}
        self.assertEqual(set(), rejected_checks - stage_checks - variants)


class UpgradeStoreTests(unittest.TestCase):
    def test_a_store_that_is_not_a_version_1_store_is_left_for_validate_store(self) -> None:
        for name, value, error, message in NOT_UPGRADED:
            with self.subTest(name):
                before = copy.deepcopy(value)
                self.assertIs(value, upgrade_store(value))
                self.assertEqual(before, value)
                with self.assertRaises(Exception) as caught:
                    validate_store(value)
                self.assertIs(error, type(caught.exception))
                self.assertEqual(message, str(caught.exception))

    def test_a_version_1_flag_that_is_not_an_object_is_left_for_validate_store(self) -> None:
        flag = {key: value for key, value in _open_flag().items() if key != "review_version"}
        legacy: dict[str, Any] = {"schema_version": 1, "next_id": 3, "flags": [[], flag]}
        upgraded = upgrade_store(copy.deepcopy(legacy))
        self.assertEqual(2, upgraded["schema_version"])
        self.assertEqual([], upgraded["flags"][0])
        self.assertIsNone(upgraded["flags"][1]["review_version"])
        with self.assertRaises(Exception) as caught:
            validate_store(upgraded)
        self.assertIs(F, type(caught.exception))
        self.assertEqual("flag-store.flags[0] must be an object, found an array", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

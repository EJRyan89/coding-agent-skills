#!/usr/bin/env python3
"""The code-review formats in docs/code-review-operations-contract.md agree with the validators that enforce them.

Each table in its "Formats" section describes one object of one format. This suite builds fixtures of every format
through the code-review core, finds each table's objects in them, and checks the table against the validator:

- every field an object has is a row, and every row's field occurs in some fixture;
- a `yes` field is in every object and removing it is rejected; a `no` field can be absent or removed; `with X`
  is optional together with X but not alone; `when ...` is optional but required in some fixture;
- every value has a documented type, every documented type occurs or is accepted, and a value of an undocumented
  type (null included) is rejected;
- every listed value ("One of ...") occurs or is accepted, every value occurs in the list, and other values are
  rejected;
- an unlisted field is rejected.

The adapter request has no validator, so its tables are checked against what `build_adapter_request` writes. A
specialist result is judged by `load_role_result` against the role each fixture names. The adapter result is described
by `review-adapter.schema.json`, which an entrypoint reviewer's author reads, and its objects get the same checks from
the schema's `properties`, `required`, `type`, and `enum`.
"""

from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import unittest
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "code-review-core" / "scripts"))

import review_runtime
from review_canary import validate_fixture_pull
from review_config import validate_config
from review_flags import validate_store
from review_operation import legacy_index
from review_records import build_record, carried_findings, validate_adapter_result, validate_record
from review_runtime import build_adapter_request, validate_adapter_manifest
from review_specialists import load_role_result
from review_state import validate_state

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPOSITORY_ROOT / "docs" / "code-review-operations-contract.md"
SCRIPTS = REPOSITORY_ROOT / "skills" / "code-review-core" / "scripts"
REFERENCES = SCRIPTS.parent / "references"

JSON_TYPES = ("null", "boolean", "integer", "number", "string", "array", "object")
# Values of each JSON type to probe a field with, in the order they are tried.
PROBES: tuple[tuple[str, Any], ...] = (
    ("null", None),
    ("string", "probe"),
    ("integer", 7),
    ("array", []),
    ("object", {}),
    ("boolean", True),
)
# A value of each type, for a documented type no fixture has: the validator must accept it somewhere.
EXAMPLES = {**dict(PROBES), "number": 0.5}
UNDOCUMENTED_VALUE = "not-a-documented-value"
UNDOCUMENTED_FIELD = "undocumented_field"
HEADER = ["Field", "Type", "Required", "Meaning"]
SHA = "b" * 40
URL = "https://github.com/example/one/pull/12"


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise TypeError(f"not a JSON value: {value!r}")


def has_type(types: frozenset[str], value: Any) -> bool:
    kind = json_type(value)
    return kind in types or (kind == "integer" and "number" in types)


@dataclass(frozen=True)
class Row:
    name: str
    types: frozenset[str] | None  # None: any value
    required: str  # yes, no, with, or when
    partner: str | None
    values: tuple[Any, ...] | None
    meaning: str


@dataclass
class Table:
    heading: str
    paths: tuple[str, ...]
    rows: dict[str, Row] = field(default_factory=dict)


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _value(token: str) -> Any:
    try:
        return json.loads(token)
    except ValueError:
        return token


def parse_row(cells: list[str]) -> Row:
    if len(cells) != 4:
        raise ValueError(f"a row needs four cells: {cells}")
    name_cell, type_cell, required_cell, meaning = cells
    name = re.fullmatch(r"`([^`]+)`", name_cell)
    if name is None:
        raise ValueError(f"a field is one name in backticks: {name_cell!r}")
    if type_cell == "any":
        types = None
    else:
        types = frozenset(type_cell.split(" or "))
        if not types <= set(JSON_TYPES):
            raise ValueError(f"{name.group(1)} has an unknown type: {type_cell!r}")
    partner = None
    if required_cell in ("yes", "no"):
        required = required_cell
    elif match := re.fullmatch(r"with `([^`]+)`", required_cell):
        required, partner = "with", match.group(1)
    elif required_cell.startswith("when "):
        required = "when"
    else:
        raise ValueError(f"{name.group(1)} has an unknown Required value: {required_cell!r}")
    values = None
    if listed := re.search(r"One of ((?:`[^`]+`(?:, or |, | or )?)+)", meaning):
        values = tuple(_value(token) for token in re.findall(r"`([^`]+)`", listed.group(1)))
    return Row(name.group(1), types, required, partner, values, meaning)


def parse_tables(text: str) -> list[Table]:
    """Each `####` heading of the Formats section, the backticked paths it names, and the table under it."""
    section = re.search(r"^## Formats\n(.*?)(?=^## |\Z)", text, flags=re.MULTILINE | re.DOTALL)
    if section is None:
        raise ValueError("the contract has no '## Formats' section")
    tables: list[Table] = []
    for line in section.group(1).splitlines():
        if line.startswith("#### "):
            paths = tuple(re.findall(r"`([^`]+)`", line))
            if not paths:
                raise ValueError(f"a table heading names its paths in backticks: {line!r}")
            tables.append(Table(line[5:].strip(), paths))
        elif line.startswith("|") and tables:
            cells = _cells(line)
            if cells == HEADER or all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                continue
            row = parse_row(cells)
            if row.name in tables[-1].rows:
                raise ValueError(f"{tables[-1].heading} lists {row.name} twice")
            tables[-1].rows[row.name] = row
    return tables


# A fixture is one valid value of a format and how its validator judges a changed copy: True when accepted, False
# when rejected, None when the format has no validator (its tables are only compared with what the code writes).
Accepts = Callable[[Any], bool] | None


@dataclass(frozen=True)
class Fixture:
    name: str
    value: Any
    accepts: Accepts


def _judge(validate: Callable[[Any], Any]) -> Callable[[Any], bool]:
    """Every validator rejects with a ValueError subclass. Any other exception is a crash, and fails the test."""

    def accepts(value: Any) -> bool:
        try:
            validate(value)
        except ValueError:
            return False
        return True

    return accepts


def _locations(value: Any, steps: list[str], prefix: tuple = ()) -> list[tuple]:
    """Key paths of the objects a path's steps lead to: `name[]` is each item, `<name>` each value of an object."""
    if not steps:
        return [prefix] if isinstance(value, dict) else []
    step, rest = steps[0], steps[1:]
    if step.startswith("<"):
        items = value.items() if isinstance(value, dict) else ()
        return [found for key, item in items for found in _locations(item, rest, (*prefix, key))]
    many = step.endswith("[]")
    name = step[:-2] if many else step
    if not isinstance(value, dict) or name not in value:
        return []
    if not many:
        return _locations(value[name], rest, (*prefix, name))
    entries = value[name] if isinstance(value[name], list) else []
    return [found for index, item in enumerate(entries) for found in _locations(item, rest, (*prefix, name, index))]


def _at(value: Any, location: tuple) -> Any:
    for key in location:
        value = value[key]
    return value


@dataclass(frozen=True)
class Instance:
    fixture: Fixture
    location: tuple

    @property
    def object(self) -> dict[str, Any]:
        return _at(self.fixture.value, self.location)

    def accepts(self, change: Callable[[dict[str, Any]], None]) -> bool:
        judge = self.fixture.accepts
        if judge is None:
            raise AssertionError(f"{self.fixture.name} has no validator to judge a change by")
        changed = copy.deepcopy(self.fixture.value)
        change(_at(changed, self.location))
        return judge(changed)


def _without(*names: str) -> Callable[[dict[str, Any]], None]:
    def change(target: dict[str, Any]) -> None:
        for name in names:
            target.pop(name)

    return change


def _set(name: str, value: Any) -> Callable[[dict[str, Any]], None]:
    return lambda target: target.__setitem__(name, copy.deepcopy(value))


def instances(paths: tuple[str, ...], fixtures: dict[str, list[Fixture]]) -> list[Instance]:
    found: list[Instance] = []
    for path in paths:
        root, *steps = path.split(".")
        for fixture in fixtures[root]:
            found.extend(Instance(fixture, location) for location in _locations(fixture.value, steps))
    return found


def check_table(heading: str, rows: dict[str, Row], found: list[Instance]) -> list[str]:
    """Where a table and its fixtures disagree, as one message each; empty when they agree."""
    if not found:
        return [f"{heading}: no fixture has this object"]
    judged = [item for item in found if item.fixture.accepts is not None]
    problems = _field_problems(heading, rows, found, judged)
    for name, row in rows.items():
        present = [item for item in found if name in item.object]
        if not present:
            continue
        problems += _row_problems(f"{heading}: {name}", name, row, found, judged, present)
    return problems


def _field_problems(heading: str, rows: dict[str, Row], found: list[Instance], judged: list[Instance]) -> list[str]:
    """Fields with no row, rows with no field, and fixtures that accept a field no row names."""
    problems: list[str] = []
    seen = set().union(*(item.object for item in found))
    problems += [f"{heading}: {name} occurs in a fixture but has no row" for name in sorted(seen - set(rows))]
    problems += [f"{heading}: {name} occurs in no fixture" for name in sorted(set(rows) - seen)]
    for item in judged:
        if item.accepts(_set(UNDOCUMENTED_FIELD, "x")):
            problems.append(f"{heading}: an unlisted field is accepted in {item.fixture.name}")
    return problems


def _row_problems(
    label: str, name: str, row: Row, found: list[Instance], judged: list[Instance], present: list[Instance]
) -> list[str]:
    """Where one row disagrees with the values fixtures have and the changes their validators accept."""
    values = [item.object[name] for item in present]
    problems = _value_problems(label, row, values)
    problems += _required(label, name, row, found, judged)
    removable = [item for item in judged if name in item.object]
    if row.types is not None:
        problems += _type_probe_problems(label, name, row.types, values, removable)
    if row.values is not None:
        problems += _listed_probe_problems(label, name, row.values, values, removable)
    return problems


def _value_problems(label: str, row: Row, values: list[Any]) -> list[str]:
    """Each fixture value of an undocumented type, then each value the row does not list."""
    problems: list[str] = []
    if row.types is not None:
        problems += [
            f"{label} has the undocumented type {json_type(value)}"
            for value in values
            if not has_type(row.types, value)
        ]
    if row.values is not None:
        problems += [
            f"{label} has the unlisted value {value!r}"
            for value in values
            if value not in row.values and not (value is None and "null" in (row.types or ()))
        ]
    return problems


def _type_probe_problems(
    label: str, name: str, types: frozenset[str], values: list[Any], removable: list[Instance]
) -> list[str]:
    """Undocumented types a validator accepts, then documented types no fixture has and no validator accepts."""
    problems: list[str] = []
    for kind, probe in _wrong_probes(types):
        accepted = [item.fixture.name for item in removable if item.accepts(_set(name, probe))]
        problems += [f"{label} accepts {kind}, which is not documented, in {fixture}" for fixture in accepted]
    for kind in sorted(types):
        if any(json_type(value) == kind or (kind, json_type(value)) == ("number", "integer") for value in values):
            continue
        if not any(item.accepts(_set(name, EXAMPLES[kind])) for item in removable):
            problems.append(f"{label} documents {kind}, which no fixture has or accepts")
    return problems


def _listed_probe_problems(
    label: str, name: str, listed: tuple[Any, ...], values: list[Any], removable: list[Instance]
) -> list[str]:
    """Listed values no fixture has and no validator accepts, then validators that accept an unlisted value."""
    problems: list[str] = []
    for value in listed:
        if value not in values and not any(item.accepts(_set(name, value)) for item in removable):
            problems.append(f"{label} lists {value!r}, which no fixture has or accepts")
    accepted = [item.fixture.name for item in removable if item.accepts(_set(name, UNDOCUMENTED_VALUE))]
    problems += [f"{label} accepts an unlisted value in {fixture}" for fixture in accepted]
    return problems


def _wrong_probes(types: frozenset[str]) -> list[tuple[str, Any]]:
    """Null unless it is documented, and the first other probe of a type the row does not document."""
    wrong = [(kind, probe) for kind, probe in PROBES if not has_type(types, probe)]
    return [item for item in wrong if item[0] == "null"] + [item for item in wrong if item[0] != "null"][:1]


def _required(label: str, name: str, row: Row, found: list[Instance], judged: list[Instance]) -> list[str]:
    absent = any(name not in item.object for item in found)
    if row.required == "yes":
        if absent:
            return [f"{label} is required but absent from a fixture"]
        return [
            f"{label} is required but removing it is accepted in {item.fixture.name}"
            for item in judged
            if item.accepts(_without(name))
        ]
    removed = [name] if row.partner is None else [name, row.partner]
    together = [item for item in judged if all(each in item.object for each in removed)]
    optional = absent or any(item.accepts(_without(*removed)) for item in together)
    problems = [] if optional else [f"{label} is documented as optional but no fixture lacks it or accepts that"]
    if row.required == "with":
        problems += [
            f"{label} needs {row.partner} but removing it alone is accepted in {item.fixture.name}"
            for item in together
            if item.accepts(_without(name))
        ]
    if row.required == "when" and not any(not item.accepts(_without(name)) for item in judged if name in item.object):
        problems.append(f"{label} is documented as sometimes required but every fixture accepts its removal")
    return problems


def nested_tables_missing(tables: list[Table], fixtures: dict[str, list[Fixture]]) -> list[str]:
    """An object field, or an array of objects, needs its own table unless its row says what it is keyed by."""
    paths = {path for table in tables for path in table.paths}
    missing = []
    for table in tables:
        for path in table.paths:
            for name, row in table.rows.items():
                if "keyed by" in row.meaning.casefold():
                    continue
                for item in instances((path,), fixtures):
                    value = item.object.get(name)
                    nested = (
                        f"{path}.{name}"
                        if isinstance(value, dict)
                        else (
                            f"{path}.{name}[]"
                            if isinstance(value, list) and any(isinstance(x, dict) for x in value)
                            else None
                        )
                    )
                    if nested and nested not in paths and nested not in missing:
                        missing.append(nested)
    return missing


# Fixtures. Between them they hold every documented field, so a table can be checked row by row.


def _finding(key: str, severity: str, line: int, **extra: Any) -> dict[str, Any]:
    return {
        "candidate_key": key,
        "severity": severity,
        "category": "Correctness",
        "path": "src/file.cs",
        "line": line,
        "body": f"Problem {key} breaks the rule.",
        "evidence": "The added line shows it.",
        "source": "generic",
        **extra,
    }


def _disposition(identifier: str, value: str, key: str = "finding_id") -> dict[str, Any]:
    return {key: identifier, "disposition": value, "rationale": "Checked against the current code."}


COMMENTS: list[dict[str, Any]] = [
    {
        "id": "C1",
        "author": "octocat",
        "path": "src/file.cs",
        "line": 7,
        "outdated": False,
        "body": "Please handle zero.",
        "url": f"{URL}#discussion_r1",
    },
    {
        "id": "C2",
        "author": "hubot",
        "path": "src/other.cs",
        "line": None,
        "outdated": True,
        "body": "Is this still needed?",
        "url": f"{URL}#discussion_r2",
    },
]


def _record_request(mode: str, **extra: Any) -> dict[str, Any]:
    return {
        "repository": "example/one",
        "pull_number": 12,
        "pull_url": URL,
        "title": "Improve behavior",
        "base_ref": "main",
        "base_sha": "a" * 40,
        "head_sha": SHA,
        "mode": mode,
        "adapter": {"name": "generic", "scope": "generic", "source_commit": None, "source_hashes": {}},
        **extra,
    }


def _result(
    findings: list[dict[str, Any]], dispositions: Sequence[dict[str, Any]] = (), **extra: Any
) -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "repository": "example/one",
        "pull_number": 12,
        "head_sha": SHA,
        "summary": "Reviewed.",
        "reviewer": "fixture-reviewer",
        "status": "complete",
        "findings": findings,
        "prior_dispositions": list(dispositions),
        "usage": None,
        **extra,
    }


def record_fixtures() -> list[dict[str, Any]]:
    """An initial review with every optional field; a re-review by a repository reviewer that judges each of its
    entries with a different disposition, and the same re-review when it could not compare with v1; and a
    re-review recorded before ledgers."""
    initial = build_record(
        _record_request(
            "initial",
            head_ref="feature/boundary",
            unavailable_sources=["assets/large.txt"],
            uncovered_files=["build/settings.props"],
            github_comments=COMMENTS,
            reviewers=[
                {
                    "id": "generic-review",
                    "category": "General",
                    "files": 3,
                    "findings": 6,
                    "retries": 0,
                    "dispositions_only": False,
                    "model": "claude-opus-5-5",
                    "seconds": 95,
                    "files_read": 4,
                    "bytes_read": 2048,
                },
                {
                    "id": "database-review",
                    "category": "Database",
                    "files": 1,
                    "findings": 0,
                    "retries": 1,
                    "dispositions_only": True,
                    "files_read": None,
                    "bytes_read": None,
                },
            ],
            patches={"src/file.cs": {"sha256": "c" * 64, "lines": 12}},
            dispatch="inline",
            snapshot={
                "source": "checkout",
                "files": 120,
                "bytes": 409_600,
                "excluded": {"agent-instruction": 2, "configured": 31},
                "seconds": {"fetch": 0.5, "materialize": 2, "prompts": 0.1},
            },
        ),
        _result(
            [
                _finding(
                    "a",
                    "MUST_FIX",
                    10,
                    title="Zero is not handled",
                    analyzer={"coverage": "custom-candidate", "tool": "Roslyn", "rule": "unchecked-zero"},
                ),
                _finding("b", "SHOULD_FIX", 20, title="Retry never stops"),
                _finding("c", "SHOULD_FIX", 30),
                _finding("d", "SUGGESTION", 40),
                _finding("e", "SUGGESTION", 50),
                _finding("f", "SHOULD_FIX", 60, repeats="a"),
            ],
            comment_dispositions=[
                _disposition("C1", "still_present", "comment_id"),
                _disposition("C2", "superseded", "comment_id"),
            ],
        ),
        version=1,
        policy={"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
        reviewed_at="2026-10-01T09:00:00+00:00",
    )
    initial["artifacts"] = {"payload_sha256": "d" * 64, "markdown_sha256": "e" * 64}
    rereview = build_record(
        _record_request(
            "re-review",
            adapter={
                "name": "one-review",
                "scope": "repository",
                "source_commit": "c" * 40,
                "source_hashes": {".claude/skills/review/SKILL.md": "e" * 64},
            },
            scope={
                "requested": "auto",
                "used": "incremental",
                "reason": "4 of 40 changed lines differ",
                "since_version": 1,
                "files_changed": 1,
                "files_total": 3,
                "lines_changed": 4,
                "lines_total": 40,
            },
            dispatch="copilot-host",
            snapshot={
                "source": "tarball",
                "files": 3,
                "bytes": 96,
                "seconds": {"fetch": 1, "materialize": 0, "prompts": 0},
            },
        ),
        _result(
            [_finding("g", "SUGGESTION", 70), _finding("h", "MUST_FIX", 80, repeats="v1:F001")],
            [
                _disposition("v1:F001", "still_present"),
                _disposition("v1:F002", "partially_addressed"),
                _disposition("v1:F003", "addressed"),
                _disposition("v1:F004", "superseded"),
                _disposition("v1:F005", "unable_to_verify"),
            ],
        ),
        version=2,
        policy={},
        reviewed_at="2026-10-02T09:00:00+00:00",
        prior_ledger=initial["ledger"],
    )
    uncompared = copy.deepcopy(rereview)
    uncompared["review"]["scope"].update(
        used="full", reason="v1 has no patch fingerprints", files_changed=None, lines_changed=None
    )
    older = build_record(
        _record_request("initial"),
        _result([_finding("a", "SHOULD_FIX", 10)]),
        version=1,
        policy={},
        reviewed_at="2026-09-01T09:00:00+00:00",
    )
    del older["ledger"]
    older["review"].update(version=2, mode="re-review")
    older["prior_dispositions"] = [_disposition("F001", "addressed")]
    return [initial, rereview, uncompared, older]


def config_fixtures() -> list[dict[str, Any]]:
    """Every field: a repository reviewer named by skill and a local manifest, one named by a committed manifest,
    and generic reviewers with and without their optional fields. And a configuration with only what it needs."""
    full = {
        "schema_version": 1,
        "default_repository_set": "primary",
        "repository_sets": {
            "primary": ["example/one", "example/two", "example/five"],
            "tracked": ["example/three", "example/four"],
        },
        "repositories": {
            "example/one": {
                "reviewer": {
                    "id": "one-review",
                    "protocol_version": 1,
                    "trusted_ref": "main",
                    "scope": "repository",
                    "manifest_path": None,
                    "skill": ".claude/skills/review/SKILL.md",
                    "manifest": "C:\\Reviews\\reviewers\\one.json",
                },
                "checkout_path": "C:\\Repos\\One",
            },
            "example/five": {
                "reviewer": {
                    "id": "five-review",
                    "protocol_version": 1,
                    "scope": "repository",
                    "skill": ".claude/agents/review.md",
                    "manifest": True,
                },
                "checkout_path": "C:\\Repos\\Five",
                "snapshot_exclude": ["**/*.resx", "Reports/Generated/**"],
            },
            "example/two": {
                "reviewer": {
                    "id": "two-review",
                    "protocol_version": 1,
                    "scope": "repository",
                    "manifest_path": ".github/review/manifest.json",
                    "skill": None,
                    "manifest": None,
                },
                "checkout_path": "C:\\Repos\\Two",
            },
            "example/three": {"reviewer": {"id": "generic", "protocol_version": 1, "scope": "generic"}},
            "example/four": {
                "reviewer": {
                    "id": "generic",
                    "protocol_version": 1,
                    "trusted_ref": None,
                    "scope": "generic",
                    "manifest_path": None,
                    "skill": None,
                    "manifest": None,
                },
                "checkout_path": None,
            },
        },
        "archive_root": "C:\\Reviews\\Archive",
        "local_mirror_root": "C:\\Reviews\\Mirror",
        "summary_root": "C:\\Reviews\\Summaries",
        "dashboard_file": "C:\\Reviews\\Dashboard.md",
        "github_login": "octocat",
        "runtime": "auto",
        "verdict_policy": {"request_changes_for": ["MUST_FIX", "SHOULD_FIX"], "should_fix_threshold": 3},
        "operation_repository_sets": {"update-pr-tracker": "tracked"},
        "reviewer_effort": "high",
        "re_review_scope": {"full_share": 0.5, "full_lines": 1000},
        "model_names": {"arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/abc": "Opus 5.5"},
        "dashboard": {
            "start_marker": "<!-- tracker:start -->",
            "end_marker": "<!-- tracker:end -->",
            "status_overrides": {"example/one#12": "blocked on design"},
            "author_names": {"octocat": "Mona Lisa"},
        },
    }
    minimal = {
        "schema_version": 1,
        "default_repository_set": "primary",
        "repository_sets": {"primary": ["example/one"]},
        "repositories": {"example/one": {"reviewer": {"id": "generic", "protocol_version": 1, "scope": "generic"}}},
        "archive_root": "C:\\Reviews\\Archive",
        "summary_root": "C:\\Reviews\\Summaries",
        "dashboard_file": "C:\\Reviews\\Dashboard.md",
    }
    return [full, minimal]


def manifest_fixtures() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    entrypoint = {
        "schema_version": 1,
        "id": "repository-review",
        "protocol_version": 1,
        "supports": ["initial", "re-review"],
        "required_capabilities": ["agent-delegation", "read-diff", "write-result"],
        "entrypoint": ".claude/skills/repository-review/SKILL.md",
        "resources": [".claude/skills/repository-review/references/rules.md"],
        "agent_profiles": [".claude/agents/repository-review.md"],
    }
    specialists = {
        "schema_version": 2,
        "id": "repository-specialists",
        "protocol_version": 1,
        "kind": "specialists",
        "supports": ["initial"],
        "required_capabilities": ["agent-delegation", "read-diff", "write-result"],
        "resources": ["docs/review/conventions.md"],
        "specialists": [
            {
                "id": "database-review",
                "category": "Database",
                "profile": ".claude/agents/database-review.md",
                "include": ["^db/.*\\.sql$"],
                "exclude": ["^db/generated/"],
                "resources": ["docs/review/database.md"],
                "when": "compatibility-window-open",
                "model": "sonnet",
                "effort": "high",
            },
            {
                "id": "api-review",
                "category": "API",
                "profile": ".claude/agents/api-review.md",
                "include": ["^api/"],
                "exclude": [],
                "resources": [],
                "when": None,
            },
        ],
        "conditions": {
            "compatibility-window-open": {
                "script": "tools/review/compatibility_window.py",
                "reads": ["db/migrations/**", "global.json"],
            }
        },
        "uncovered": "ignore",
        "finding_categories": ["Correctness", "Security", "Style", "Other"],
        "fallback_finding_category": "Other",
    }
    plain = copy.deepcopy(specialists)
    del plain["uncovered"]
    del plain["finding_categories"]
    del plain["fallback_finding_category"]
    # Without agent-delegation, its specialists may also run inline.
    plain["required_capabilities"] = ["read-diff", "write-result"]
    return entrypoint, [specialists, plain]


def flag_fixtures() -> list[dict[str, Any]]:
    return [
        {
            "schema_version": 2,
            "next_id": 3,
            "flags": [
                {
                    "id": "RF-000001",
                    "status": "open",
                    "created_at": "2026-10-01T09:00:00+00:00",
                    "resolved_at": None,
                    "repository": "example/one",
                    "pull_number": 12,
                    "review_version": 2,
                    "finding_id": "F001",
                    "category": "false-positive",
                    "body": "The zero case is handled by the caller.",
                    "resolution": None,
                },
                {
                    "id": "RF-000002",
                    "status": "resolved",
                    "created_at": "2026-10-01T09:00:00+00:00",
                    "resolved_at": "2026-10-03T09:00:00+00:00",
                    "repository": None,
                    "pull_number": None,
                    "review_version": None,
                    "finding_id": None,
                    "category": "missed",
                    "body": "Reviews miss retries.",
                    "resolution": "Accepted in a review-insights decision.",
                },
            ],
        },
        {"schema_version": 2, "next_id": 1, "flags": []},
    ]


LEGACY_INDEX = {
    "schema_version": 1,
    "kind": "legacy-review-index",
    "repository": "example/one",
    "pull_number": 12,
    "reviewed_at": "2025-06-01T09:00:00+00:00",
    "reviewed_head_sha": SHA,
    "verdict": "APPROVED",
    "source_sha256": "f" * 64,
    "source_path": "legacy-review.md",
    "source_file_sha256": "f" * 64,
}


def fixture_pull_fixtures() -> list[dict[str, Any]]:
    thread = {
        "author": "reviewer",
        "path": "app/service.py",
        "line": 2,
        "outdated": False,
        "body": "Is this safe?",
        "url": "https://example.invalid/c/1",
    }
    pull = {
        "schema_version": 1,
        "repository": "example/one",
        "number": 12,
        "title": "Total the items",
        "base_ref": "main",
        "head_ref": "totals",
        "threads": [thread, {**thread, "line": None, "outdated": True}],
    }
    return [pull, {**pull, "threads": []}]


def state_fixtures() -> list[dict[str, Any]]:
    """A state with one repository's advanced watermark and one not yet advanced, and an empty state."""
    advanced = {"merged_since": "2026-03-10", "updated_at": "2026-03-11"}
    return [
        {"schema_version": 1, "repositories": {"example/one": advanced, "example/two": {}}},
        {"schema_version": 1, "repositories": {}},
    ]


def specialist_fixtures(scratch: Path) -> list[Fixture]:
    """A re-review role's result under declared finding categories, with review comments, every severity spelling,
    each analyzer coverage, and a repeat of each kind; and an initial review role's result with neither categories
    nor comments. Each is judged by writing it to its role's result file and loading it as `check` does."""
    added = {"src/app.py": {str(line): f"line {line}" for line in range(1, 7)}}
    common = {"files": ["src/app.py"], "dispositions_only": False, "category": "App"}
    rereview = {
        **common,
        "id": "app-review",
        "result_file": str(scratch / "app-review.json"),
        "prior_ids": ["v1:F001"],
        "prior_severities": {"v1:F001": "MUST_FIX"},
        "comment_ids": ["C1"],
        "finding_categories": ["Correctness", "Style"],
    }
    initial = {
        **common,
        "id": "generic-review",
        "result_file": str(scratch / "generic-review.json"),
        "prior_ids": [],
        "comment_ids": [],
    }

    def judge(role: dict[str, Any]) -> Accepts:
        def accepts(value: Any) -> bool:
            Path(role["result_file"]).write_text(json.dumps(value), encoding="utf-8")
            try:
                load_role_result(role, added, ["ruff"])
            except ValueError:
                return False
            return True

        return accepts

    def finding(line: int, severity: str, **extra: Any) -> dict[str, Any]:
        return {"path": "src/app.py", "line": line, "severity": severity, "title": f"Defect {line}", **extra}

    rereview_result = {
        "model": "claude-opus-5-5",
        "summary": "Two defects, one carried from v1.",
        "findings": [
            finding(
                1,
                "MUST_FIX",
                category="Correctness",
                body="Divides by zero.",
                analyzer={"coverage": "available", "tool": "Ruff", "rule": "B008"},
            ),
            finding(
                2,
                "SHOULD FIX",
                category="style",
                body="The same division.",
                repeats=0,
                analyzer={"coverage": "known", "tool": "pylint", "rule": "W0102"},
            ),
            finding(
                3,
                "SUGGESTION",
                category="Style",
                body="Still the v1 problem.",
                repeats="v1:F001",
                analyzer={"coverage": "custom-candidate", "tool": "ruff", "rule": "unchecked-divisor"},
            ),
            finding(4, "MUST FIX", category="Correctness", body="Leaks a handle."),
            finding(5, "SHOULD_FIX", category="Correctness", body="Ignores the result."),
        ],
        "prior_dispositions": [
            {"finding_id": "v1:F001", "disposition": "still_present", "rationale": "Line 3 still divides."}
        ],
        "comment_dispositions": [{"comment_id": "C1", "disposition": "addressed", "rationale": "Now guarded."}],
    }
    initial_result = {
        "model": "unknown",
        "summary": "One defect.",
        "findings": [finding(6, "SUGGESTION", body="Unused import.")],
        "prior_dispositions": [],
    }
    return [
        Fixture("re-review specialist result", rereview_result, judge(rereview)),
        Fixture("initial specialist result", initial_result, judge(initial)),
    ]


def legacy_accepts(scratch: Path) -> Callable[[Any], bool]:
    def accepts(value: Any) -> bool:
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            folder = Path(temporary) / "example" / "one" / "pulls" / "12"
            folder.mkdir(parents=True)
            (folder / "legacy-review.json").write_text(json.dumps(value), encoding="utf-8")
            return legacy_index(Path(temporary), "example/one", 12) is not None

    return accepts


def request_fixtures(scratch: Path, prior: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Requests as `build_adapter_request` writes them: one with a head branch, prior findings, review comments, and
    a changed file the snapshot could not hold, and one with none of them."""
    diff = scratch / "diff.patch"
    diff.write_text(
        "diff --git a/src/file.cs b/src/file.cs\ndiff --git a/assets/large.txt b/assets/large.txt\n", encoding="utf-8"
    )
    snapshot = {"excluded_paths": {"assets/large.txt": "file-size-limit"}}
    common: dict[str, Any] = {
        "repository": "example/one",
        "pull_number": 12,
        "base_ref": "main",
        "base_sha": "a" * 40,
        "head_sha": SHA,
        "title": "Improve behavior",
        "url": URL,
        "diff_path": diff,
        "source_snapshot_root": scratch / "snapshot",
    }
    with mock.patch.object(review_runtime, "verify_source_snapshot", return_value=snapshot):
        full = build_adapter_request(
            mode="re-review", prior_findings=prior, github_comments=COMMENTS, head_ref="feature/boundary", **common
        )
    with mock.patch.object(review_runtime, "verify_source_snapshot", return_value={"excluded_paths": {}}):
        plain = build_adapter_request(mode="initial", **common)
    return [full, plain]


def adapter_fixtures() -> list[Fixture]:
    published = json.loads((REFERENCES / "fixtures" / "adapter-result.json").read_text(encoding="utf-8"))
    prior = [{"id": "v1:F001", "severity": "MUST_FIX"}, {"id": "v1:F002", "severity": "SHOULD_FIX"}]
    full = _result(
        [
            _finding(
                "a",
                "MUST_FIX",
                10,
                title="Zero is not handled",
                analyzer={"coverage": "custom-candidate", "tool": "Roslyn", "rule": "unchecked-zero"},
            ),
            _finding(
                "b",
                "SHOULD_FIX",
                20,
                repeats="a",
                analyzer={"coverage": "known", "tool": "Roslynator.Analyzers", "rule": "RCS1001"},
            ),
            _finding(
                "c",
                "SUGGESTION",
                30,
                repeats="v1:F001",
                analyzer={"coverage": "available", "tool": "StyleCop.Analyzers", "rule": "SA1515"},
            ),
        ],
        [_disposition("v1:F001", "still_present"), _disposition("v1:F002", "addressed")],
        comment_dispositions=[_disposition("C1", "partially_addressed", "comment_id")],
        usage={"input_tokens": 1200, "output_tokens": 300},
    )

    def judge(repository: str, number: int, prior_findings: list[dict[str, Any]], comments: list[str]) -> Accepts:
        return _judge(
            lambda value: validate_adapter_result(
                value,
                expected_repository=repository,
                expected_number=number,
                expected_head_sha=SHA,
                prior_ids=[item["id"] for item in prior_findings],
                comment_ids=comments,
                prior_severities={item["id"]: item["severity"] for item in prior_findings},
            )
        )

    return [
        Fixture("published adapter-result.json", published, judge("example/example-repository", 17, [], [])),
        Fixture("re-review result", full, judge("example/one", 12, prior, ["C1"])),
    ]


def schema_tables(schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Each object the adapter result schema describes, with its path."""
    findings = schema["properties"]["findings"]["items"]
    return [
        ("adapter-result", schema),
        ("adapter-result.findings[]", findings),
        ("adapter-result.findings[].analyzer", findings["properties"]["analyzer"]),
        ("adapter-result.prior_dispositions[]", schema["properties"]["prior_dispositions"]["items"]),
        ("adapter-result.comment_dispositions[]", schema["properties"]["comment_dispositions"]["items"]),
    ]


def schema_rows(described: dict[str, Any]) -> dict[str, Row]:
    """The schema's properties as rows: its required list, and each property's type, const, or enum."""
    rows = {}
    for name, prop in described["properties"].items():
        values = tuple(prop["enum"]) if "enum" in prop else ((prop["const"],) if "const" in prop else None)
        declared = prop.get("type")
        if declared is not None:
            types = frozenset([declared] if isinstance(declared, str) else declared)
        elif values is not None:
            types = frozenset(json_type(value) for value in values)
        else:
            types = None
        rows[name] = Row(name, types, "yes" if name in described.get("required", ()) else "no", None, values, "")
    return rows


class FormatContractTest(unittest.TestCase):
    scratch: tempfile.TemporaryDirectory
    fixtures: ClassVar[dict[str, list[Fixture]]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = tempfile.TemporaryDirectory()
        scratch = Path(cls.scratch.name)
        records = record_fixtures()
        entrypoint, specialists = manifest_fixtures()
        record = _judge(validate_record)
        manifest = _judge(validate_adapter_manifest)
        config = _judge(validate_config)
        flags = _judge(validate_store)
        cls.fixtures = {
            "config": [Fixture(f"config {index}", value, config) for index, value in enumerate(config_fixtures())],
            "entrypoint-manifest": [Fixture("entrypoint manifest", entrypoint, manifest)],
            "specialists-manifest": [
                Fixture(f"specialists manifest {index}", value, manifest) for index, value in enumerate(specialists)
            ],
            "request": [
                Fixture(f"request {index}", value, None)
                for index, value in enumerate(
                    request_fixtures(scratch, carried_findings(records[:2], flag_fixtures()[0]["flags"]))
                )
            ],
            "record": [
                Fixture(name, value, record)
                for name, value in zip(
                    ("initial record", "re-review record", "uncompared re-review record", "older record"),
                    records,
                    strict=True,
                )
            ],
            "flag-store": [Fixture(f"flag store {index}", value, flags) for index, value in enumerate(flag_fixtures())],
            "state": [
                Fixture(f"state {index}", value, _judge(validate_state)) for index, value in enumerate(state_fixtures())
            ],
            "specialist-result": specialist_fixtures(scratch),
            "legacy-index": [Fixture("legacy index", LEGACY_INDEX, legacy_accepts(scratch))],
            "fixture-pull": [
                Fixture(f"fixture pull {index}", value, _judge(validate_fixture_pull))
                for index, value in enumerate(fixture_pull_fixtures())
            ],
            "adapter-result": adapter_fixtures(),
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scratch.cleanup()

    @property
    def tables(self) -> list[Table]:
        return parse_tables(CONTRACT.read_text(encoding="utf-8"))

    def test_fixtures_are_valid(self) -> None:
        for fixtures in self.fixtures.values():
            for fixture in fixtures:
                with self.subTest(fixture=fixture.name):
                    self.assertTrue(fixture.accepts is None or fixture.accepts(copy.deepcopy(fixture.value)))

    def test_every_table_agrees_with_its_validator(self) -> None:
        self.assertTrue(self.tables, "docs/code-review-operations-contract.md has no format tables")
        for table in self.tables:
            with self.subTest(table=table.heading):
                unknown = [path for path in table.paths if path.split(".")[0] not in self.fixtures]
                self.assertEqual([], unknown, "a table heading names a format this suite does not know")
                self.assertEqual([], check_table(table.heading, table.rows, instances(table.paths, self.fixtures)))

    def test_every_format_and_nested_object_has_a_table(self) -> None:
        documented = {path.split(".")[0] for table in self.tables for path in table.paths}
        self.assertEqual(set(self.fixtures) - {"adapter-result"}, documented)
        self.assertEqual([], nested_tables_missing(self.tables, self.fixtures))

    def test_adapter_schema_agrees_with_the_adapter_validator(self) -> None:
        schema = json.loads((REFERENCES / "review-adapter.schema.json").read_text(encoding="utf-8"))
        for path, described in schema_tables(schema):
            with self.subTest(path=path):
                self.assertIs(False, described["additionalProperties"])
                self.assertEqual([], check_table(path, schema_rows(described), instances((path,), self.fixtures)))

    def test_a_doctored_table_disagrees(self) -> None:
        text = CONTRACT.read_text(encoding="utf-8")
        row = re.search(r"^\| `number` \| integer \| yes \|.*$", text, flags=re.MULTILINE)
        if row is None:
            self.fail("the record's pull request table lists number as a required integer")
        for name, doctored, expected in (
            (
                "optional",
                text.replace(row.group(0), row.group(0).replace("| yes |", "| no |")),
                "documented as optional",
            ),
            ("missing row", text.replace(row.group(0) + "\n", ""), "number occurs in a fixture but has no row"),
            (
                "wrong type",
                text.replace(row.group(0), row.group(0).replace("| integer |", "| string |")),
                "number has the undocumented type integer",
            ),
            (
                "unlisted value",
                text.replace("One of `APPROVED`,", "One of `PENDING`, `APPROVED`,"),
                "lists 'PENDING', which no fixture has or accepts",
            ),
        ):
            with self.subTest(doctored=name):
                problems = [
                    problem
                    for table in parse_tables(doctored)
                    for problem in check_table(table.heading, table.rows, instances(table.paths, self.fixtures))
                ]
                self.assertTrue(any(expected in problem for problem in problems), problems)

    def test_a_nullable_or_loosely_typed_row_is_checked(self) -> None:
        rows = {"line": parse_row(["`line`", "integer", "yes", "A line."])}
        comments = instances(("record.github_comments[]",), self.fixtures)
        self.assertIn(
            "record.github_comments[]: line has the undocumented type null",
            check_table("record.github_comments[]", rows, comments),
        )
        loose = {"line": parse_row(["`line`", "integer or null or string", "yes", "A line."])}
        self.assertIn(
            "record.github_comments[]: line documents string, which no fixture has or accepts",
            check_table("record.github_comments[]", loose, comments),
        )
        narrow = {"line": parse_row(["`line`", "integer or null or array", "yes", "A line."])}
        self.assertIn(
            "record.github_comments[]: line documents array, which no fixture has or accepts",
            check_table("record.github_comments[]", narrow, comments),
        )

    def test_rows_parse_their_required_value_and_listed_values(self) -> None:
        row = parse_row(
            [
                "`scope`",
                "string",
                "when the reviewer is a repository reviewer",
                "One of `generic` or `repository`. Defaults to `repository`.",
            ]
        )
        self.assertEqual(("when", ("generic", "repository")), (row.required, row.values))
        row = parse_row(["`github_comments`", "array", "with `comment_dispositions`", "One of `1`, `2`, or `3`."])
        self.assertEqual(("with", "comment_dispositions", (1, 2, 3)), (row.required, row.partner, row.values))
        self.assertIsNone(parse_row(["`usage`", "any", "no", "Whatever the reviewer returned."]).types)
        for cells in (["usage", "any", "no", "x"], ["`usage`", "map", "no", "x"], ["`usage`", "any", "maybe", "x"]):
            with self.subTest(cells=cells), self.assertRaises(ValueError):
                parse_row(cells)


def toy_validator(
    kinds: tuple[str, ...] = ("x", "y"), note_types: tuple[type, ...] = (str,), notes: tuple[str, ...] | None = None
) -> Callable[[Any], Any]:
    """A validator for {"item": {...}}: kind is required and listed, count an optional integer, note optional and
    nullable but only beside count, and listed when notes is given, and no other field."""

    def validate(value: Any) -> None:
        item = value["item"]
        if not isinstance(item, dict) or not set(item) <= {"kind", "count", "note"}:
            raise ValueError("fields")
        if "kind" not in item or item["kind"] not in kinds:
            raise ValueError("kind")
        if "count" in item and (not isinstance(item["count"], int) or isinstance(item["count"], bool)):
            raise ValueError("count")
        if "note" in item and item["note"] is not None and not isinstance(item["note"], note_types):
            raise ValueError("note")
        if notes is not None and isinstance(item.get("note"), str) and item["note"] not in notes:
            raise ValueError("unlisted note")
        if "note" in item and "count" not in item:
            raise ValueError("note needs count")

    return validate


def toy_instances(accepts: Accepts, *extra: dict[str, Any]) -> list[Instance]:
    values = [{"kind": "x", "count": 1, "note": "n"}, {"kind": "y"}, *extra]
    names = ["full", "bare", *(f"extra {index}" for index in range(len(extra)))]
    fixtures = [Fixture(name, {"item": value}, accepts) for name, value in zip(names, values, strict=True)]
    return instances(("toy.item",), {"toy": fixtures})


def toy_rows(**changed: list[str] | None) -> dict[str, Row]:
    """The rows that agree with toy_validator, with any named row replaced by its cells or removed by None."""
    cells = {
        "kind": ["`kind`", "string", "yes", "One of `x` or `y`."],
        "count": ["`count`", "integer", "with `note`", "A count."],
        "note": ["`note`", "string or null", "no", "A note."],
        **changed,
    }
    return {name: parse_row(row) for name, row in cells.items() if row is not None}


class CheckTableTests(unittest.TestCase):
    """check_table called directly on literal rows and fixtures, with every message it gives, in its order."""

    def check(self, rows: dict[str, Row], found: list[Instance]) -> list[str]:
        return check_table("toy.item", rows, found)

    def test_a_table_that_agrees_with_its_validator_has_no_problems(self) -> None:
        self.assertEqual([], self.check(toy_rows(), toy_instances(_judge(toy_validator()))))

    def test_a_table_no_fixture_has_says_only_that(self) -> None:
        self.assertEqual(["toy.item: no fixture has this object"], self.check(toy_rows(), []))

    def test_fields_without_rows_and_rows_without_fields_are_named_in_sorted_order(self) -> None:
        rows = toy_rows(count=None, note=None, gone=["`gone`", "string", "yes", "One of `a`."])
        rows["absent"] = parse_row(["`absent`", "integer", "no", "Never present."])
        self.assertEqual(
            [
                "toy.item: count occurs in a fixture but has no row",
                "toy.item: note occurs in a fixture but has no row",
                "toy.item: absent occurs in no fixture",
                "toy.item: gone occurs in no fixture",
            ],
            self.check(rows, toy_instances(_judge(toy_validator()))),
        )

    def test_fixtures_without_a_validator_are_compared_but_never_judged(self) -> None:
        rows = toy_rows(kind=["`kind`", "any", "yes", "Anything."], note=["`note`", "string", "no", "A note."])
        self.assertEqual([], self.check(rows, toy_instances(None)))
        wide = {"kind": "x", "f": 1, "b": 2, "e": 3, "a": 4, "d": 5, "c": 6}
        self.assertEqual(
            [f"toy.item: {name} occurs in a fixture but has no row" for name in "abcdef"],
            self.check(rows, toy_instances(None, wide)),
        )
        # With nothing to judge a probe by, a documented type no fixture has is never shown to be accepted.
        self.assertEqual(
            [
                "toy.item: kind has the undocumented type string",
                "toy.item: kind has the undocumented type string",
                "toy.item: kind documents integer, which no fixture has or accepts",
                "toy.item: note documents null, which no fixture has or accepts",
            ],
            self.check(toy_rows(kind=["`kind`", "integer", "yes", "A kind."]), toy_instances(None)),
        )

    def test_a_validator_that_accepts_everything_is_reported_row_by_row(self) -> None:
        self.assertEqual(
            [
                "toy.item: an unlisted field is accepted in full",
                "toy.item: an unlisted field is accepted in bare",
                "toy.item: kind is required but removing it is accepted in full",
                "toy.item: kind is required but removing it is accepted in bare",
                "toy.item: kind accepts null, which is not documented, in full",
                "toy.item: kind accepts null, which is not documented, in bare",
                "toy.item: kind accepts integer, which is not documented, in full",
                "toy.item: kind accepts integer, which is not documented, in bare",
                "toy.item: kind accepts an unlisted value in full",
                "toy.item: kind accepts an unlisted value in bare",
                "toy.item: count needs note but removing it alone is accepted in full",
                "toy.item: count accepts null, which is not documented, in full",
                "toy.item: count accepts string, which is not documented, in full",
                "toy.item: note accepts integer, which is not documented, in full",
            ],
            self.check(toy_rows(), toy_instances(_judge(lambda value: None))),
        )

    def test_values_of_an_undocumented_type_or_unlisted_value_are_reported_each_time(self) -> None:
        rows = toy_rows(kind=["`kind`", "integer", "yes", "One of `1`."])
        self.assertEqual(
            [
                "toy.item: kind has the undocumented type string",
                "toy.item: kind has the undocumented type string",
                "toy.item: kind has the unlisted value 'x'",
                "toy.item: kind has the unlisted value 'y'",
                "toy.item: kind documents integer, which no fixture has or accepts",
                "toy.item: kind lists 1, which no fixture has or accepts",
            ],
            self.check(rows, toy_instances(_judge(toy_validator()))),
        )

    def test_a_null_value_of_a_nullable_row_is_not_an_unlisted_value(self) -> None:
        nullable = toy_rows(note=["`note`", "string or null", "no", "One of `n`."])
        extra = {"kind": "x", "count": 2, "note": None}
        listed = _judge(toy_validator(notes=("n",)))
        self.assertEqual([], self.check(nullable, toy_instances(listed, extra)))
        not_nullable = toy_rows(note=["`note`", "string", "no", "One of `n`."])
        self.assertEqual(
            [
                "toy.item: note has the undocumented type null",
                "toy.item: note has the unlisted value None",
                "toy.item: note accepts null, which is not documented, in full",
                "toy.item: note accepts null, which is not documented, in extra 0",
            ],
            self.check(not_nullable, toy_instances(listed, extra)),
        )

    def test_a_documented_type_or_listed_value_no_fixture_has_must_be_accepted_somewhere(self) -> None:
        rows = toy_rows(
            kind=["`kind`", "string", "yes", "One of `x`, `y`, or `z`."],
            note=["`note`", "string or integer or array or object or boolean or null", "no", "A note."],
        )
        self.assertEqual(
            [
                "toy.item: kind lists 'z', which no fixture has or accepts",
                "toy.item: note documents array, which no fixture has or accepts",
                "toy.item: note documents boolean, which no fixture has or accepts",
                "toy.item: note documents integer, which no fixture has or accepts",
                "toy.item: note documents object, which no fixture has or accepts",
            ],
            self.check(rows, toy_instances(_judge(toy_validator()))),
        )
        wider = _judge(toy_validator(kinds=("x", "y", "z"), note_types=(str, int, list, dict)))
        self.assertEqual([], self.check(rows, toy_instances(wider)))

    def test_an_integer_value_stands_for_a_documented_number(self) -> None:
        rows = toy_rows(count=["`count`", "number", "with `note`", "A count."])
        self.assertEqual([], self.check(rows, toy_instances(_judge(toy_validator()))))
        rows = toy_rows(count=["`count`", "number or string", "with `note`", "A count."])
        self.assertEqual(
            ["toy.item: count documents string, which no fixture has or accepts"],
            self.check(rows, toy_instances(_judge(toy_validator()))),
        )

    def test_a_row_of_any_type_or_without_listed_values_skips_those_checks(self) -> None:
        rows = toy_rows(kind=["`kind`", "any", "yes", "A kind."], count=["`count`", "any", "with `note`", "A count."])
        self.assertEqual([], self.check(rows, toy_instances(_judge(toy_validator()))))
        self.assertEqual(
            [
                "toy.item: an unlisted field is accepted in full",
                "toy.item: an unlisted field is accepted in bare",
                "toy.item: kind is required but removing it is accepted in full",
                "toy.item: kind is required but removing it is accepted in bare",
                "toy.item: count needs note but removing it alone is accepted in full",
                "toy.item: note accepts integer, which is not documented, in full",
            ],
            self.check(rows, toy_instances(_judge(lambda value: None))),
        )


if __name__ == "__main__":
    unittest.main()

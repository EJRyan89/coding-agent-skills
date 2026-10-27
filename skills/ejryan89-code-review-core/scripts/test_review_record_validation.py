"""validate_record, pinned: every record shape it accepts, every fault it refuses with its exact error, and the order
in which it detects faults. The records are literal, so a change to what the pipeline accepts shows up here."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_config import ConfigurationError
from review_records import (
    RecordError,
    RecordFieldError,
    _validate_ledger,
    is_suite_profile,
    validate_adapter_result,
    validate_record,
    validate_record_pair,
    write_record_pair,
)

Mutation = Callable[[Any], Any]
Key = str | int

BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40
SHA256 = "3" * 64
# The kinds a field error names as expected, written out independently of the reader.
TEXT = "a non-empty string"
TITLE_KIND = "a non-empty trimmed single-line string of at most 120 characters"
POSITIVE = "an integer of at least 1"
COUNT = "an integer of at least 0"
SHA_KIND = "a lowercase hexadecimal digest of 40 or 64 characters"
DIGEST_KIND = "a lowercase hexadecimal digest of 64 characters"
SEVERITY_KIND = 'one of "MUST_FIX", "SHOULD_FIX", "SUGGESTION"'
DISPOSITION_KIND = 'one of "addressed", "partially_addressed", "still_present", "superseded", "unable_to_verify"'
STATUS_KIND = 'one of "complete", "partial", "failed"'
VERDICT_KIND = 'one of "APPROVED", "CHANGES_REQUESTED", "INCOMPLETE"'
DISPATCH_KIND = 'one of "subagents", "copilot-host", "inline"'
SNAPSHOT_SOURCE_KIND = 'one of "checkout", "checkout-lazy", "tarball"'
ADAPTER_SOURCE_KIND = 'one of "generic", "trusted-ref", "base", "default-branch", "generic-fallback"'
# What an analyzer without its fields is refused with, after its path.
ANALYZER_MISSING = "must be an object with fields coverage, rule, tool, found an object without them"
NOT_HEX = "a string with a character that is not a lowercase hexadecimal digit"
# Values that are not a SHA-256 digest, each with what a field error names as found.
INVALID_DIGESTS: tuple[tuple[Any, str], ...] = (
    (BASE_SHA, "a string of 40 characters"),
    ("a" * 63, "a string of 63 characters"),
    ("a" * 65, "a string of 65 characters"),
    ("A" * 64, NOT_HEX),
    ("g" * 64, NOT_HEX),
    ("", "a string of 0 characters"),
    ("x", "a string of 1 character"),
    (SHA256 + "\n", "a string of 65 characters"),
    (None, "null"),
    (True, "boolean true"),
    (1, "integer 1"),
    ([], "an array"),
    ({}, "an object"),
)
GENERIC_SOURCE = "Review adapter source is invalid for a generic reviewer"


def _finding(identifier: str, severity: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "candidate_key": f"key-{identifier}",
        "severity": severity,
        "category": "correctness",
        "path": "src/app.py",
        "line": 3,
        "body": "Zero is not handled.",
        "evidence": "divide(1, 0) raises.",
        "source": "reviewer",
    }


SNAPSHOT = {
    "source": "checkout",
    "files": 12_700,
    "bytes": 213_909_504,
    "seconds": {"fetch": 1.5, "materialize": 33.2, "prompts": 0.4},
}
REVIEWER = {"id": "generic", "category": "General", "files": 1, "findings": 1, "retries": 0, "dispositions_only": False}
READ_COUNTS = "Review reviewer generic files_read and bytes_read must both be non-negative integers or both null"
SEARCHES = "Review reviewer generic searches must come with files_read and bytes_read, and be null when they are"
SNAPSHOT_SECONDS = "Review snapshot seconds must give fetch, materialize, prompts as non-negative numbers"
SNAPSHOT_EXCLUDED = "Review snapshot excluded must give a positive count for each exclusion reason"


def _record() -> Any:
    """The smallest valid record: an initial review with one finding and none of the optional parts."""
    return {
        "schema_version": 1,
        "repository": "owner/repo",
        "pull_request": {
            "number": 7,
            "url": "https://github.com/owner/repo/pull/7",
            "title": "Handle zero",
            "base_ref": "main",
            "base_sha": BASE_SHA,
            "head_sha": HEAD_SHA,
        },
        "review": {
            "version": 1,
            "mode": "initial",
            "reviewed_at": "2026-10-06T12:00:00+00:00",
            "summary": "One problem.",
            "verdict": "CHANGES_REQUESTED",
            "counts": {"MUST_FIX": 1, "SHOULD_FIX": 0, "SUGGESTION": 0},
            "adapter": {
                "name": "generic",
                "protocol_version": 1,
                "scope": "generic",
                "source_commit": None,
                "source_hashes": {},
                "reviewer": "claude-code",
                "status": "complete",
                "usage": None,
            },
        },
        "findings": [_finding("F001", "MUST_FIX")],
        "prior_dispositions": [],
    }


def _full_record() -> Any:
    """A valid version 2 re-review using every optional part: a finding that repeats an earlier one, with its ledger,
    and a file of a type the review does not read."""
    record = _record()
    record["schema_version"] = 2
    record["pull_request"]["head_ref"] = "fix/zero"
    record["review"].update(
        {
            "version": 2,
            "mode": "re-review",
            "counts": {"MUST_FIX": 1, "SHOULD_FIX": 1, "SUGGESTION": 0},
            "coverage": {
                "unavailable_sources": [],
                "uncovered_files": ["docs/notes.md"],
                "unreviewed_files": {"dist/Setup.MSI": ".msi"},
            },
            "reviewers": [
                {
                    "id": "correctness",
                    "category": "correctness",
                    "files": 2,
                    "findings": 2,
                    "retries": 0,
                    "dispositions_only": False,
                    "seconds": 125,
                    "model": "model-x",
                    "files_read": 14,
                    "bytes_read": 52_000,
                }
            ],
            "patches": {"src/app.py": {"sha256": SHA256, "lines": 10}},
            "scope": {
                "requested": "auto",
                "used": "incremental",
                "reason": "Small change since v1.",
                "since_version": 1,
                "files_changed": 1,
                "files_total": 2,
                "lines_changed": 3,
                "lines_total": 10,
            },
            "dispatch": "inline",
            "snapshot": SNAPSHOT,
        }
    )
    record["review"]["adapter"].update(
        {"source_commit": HEAD_SHA, "source_hashes": {"references/rules.md": SHA256}, "usage": {"input_tokens": 1}}
    )
    first = _finding("F001", "MUST_FIX")
    first["title"] = "Zero is not handled"
    first["analyzer"] = {"coverage": "available", "tool": "ruff", "rule": "B006"}
    repeat = _finding("F002", "SHOULD_FIX")
    repeat["repeats"] = {"version": 1, "id": "F001"}
    record["findings"] = [first, repeat]
    record["prior_dispositions"] = [{"finding_id": "v1:F001", "disposition": "still_present", "rationale": "Still."}]
    record["ledger"] = [
        {
            "version": 1,
            "id": "F001",
            "severity": "MUST_FIX",
            "category": "correctness",
            "state": "open",
            "judged_in": 2,
            "dispositions": [{"version": 2, "disposition": "still_present"}],
            "repeats": [{"version": 2, "id": "F002"}],
        },
        {
            "version": 2,
            "id": "F001",
            "severity": "MUST_FIX",
            "category": "correctness",
            "state": "open",
            "judged_in": 2,
            "dispositions": [],
            "repeats": [],
        },
    ]
    record["github_comments"] = [
        {
            "id": "C1",
            "author": "alice",
            "path": "src/app.py",
            "line": 3,
            "outdated": False,
            "body": "Zero?",
            "url": "https://github.com/owner/repo/pull/7#discussion_r1",
        }
    ]
    record["comment_dispositions"] = [{"comment_id": "C1", "disposition": "addressed", "rationale": "Fixed."}]
    record["artifacts"] = {"payload_sha256": SHA256, "markdown_sha256": SHA256}
    return record


def _set(path: tuple[Key, ...], value: Any) -> Mutation:
    def apply(record: Any) -> Any:
        target = record
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = copy.deepcopy(value)
        return record

    return apply


def _delete(path: tuple[Key, ...]) -> Mutation:
    def apply(record: Any) -> Any:
        target = record
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
        return record

    return apply


def _replace(value: Any) -> Mutation:
    return lambda _ignored: copy.deepcopy(value)


def _chain(*mutations: Mutation) -> Mutation:
    def apply(record: Any) -> Any:
        for mutation in mutations:
            record = mutation(record)
        return record

    return apply


PULL = ("pull_request",)
REVIEW = ("review",)
ADAPTER = ("review", "adapter")
FINDING = ("findings", 0)
PRIOR = {"finding_id": "v1:F001", "disposition": "addressed", "rationale": "Done."}

# Records validate_record accepts, each built from the minimal record unless it starts from the full one.
ACCEPTED: list[tuple[str, Callable[[], dict[str, Any]], Mutation]] = [
    ("minimal record", _record, _chain()),
    *(
        (
            f"POSIX finding and source hash path {path}",
            _record,
            _chain(_set((*FINDING, "path"), path), _set((*ADAPTER, "source_hashes"), {path: SHA256})),
        )
        for path in ("src/space name.py", "src/a:b.py", "1:notes.md", "notes:archive/x.md")
    ),
    ("every optional part", _full_record, _chain()),
    ("SHA-256 commit hashes", _record, _chain(_set((*PULL, "base_sha"), SHA256), _set((*PULL, "head_sha"), SHA256))),
    ("head_ref", _record, _set((*PULL, "head_ref"), "fix/zero")),
    # How the reviewers were worked; literal values, since a record keeps them.
    ("dispatched as subagents", _record, _set((*REVIEW, "dispatch"), "subagents")),
    ("dispatched to the Copilot CLI host", _record, _set((*REVIEW, "dispatch"), "copilot-host")),
    ("worked inline", _record, _set((*REVIEW, "dispatch"), "inline")),
    # The snapshot and the reads a guard counted; literal values, since a record keeps them.
    ("snapshot from a checkout", _record, _set((*REVIEW, "snapshot"), SNAPSHOT)),
    (
        "snapshot from a tarball, empty, timed in whole seconds",
        _record,
        _set(
            (*REVIEW, "snapshot"),
            {"source": "tarball", "files": 0, "bytes": 0, "seconds": {"fetch": 4, "materialize": 0, "prompts": 0}},
        ),
    ),
    (
        "snapshot with its exclusions by reason",
        _record,
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "excluded": {"agent-instruction": 3, "configured": 1_200}}),
    ),
    ("snapshot that excluded nothing", _record, _set((*REVIEW, "snapshot"), {**SNAPSHOT, "excluded": {}})),
    *(
        (
            f"diff over the diff endpoint's limit, read from {source}",
            _record,
            _set((*REVIEW, "snapshot"), {**SNAPSHOT, "diff": {"source": source, "limit": limit}}),
        )
        for source, limit in (("files-api", "300 files"), ("checkout", "20000 lines"))
    ),
    ("reads counted", _record, _set((*REVIEW, "reviewers"), [{**REVIEWER, "files_read": 0, "bytes_read": 0}])),
    ("reads unknown", _record, _set((*REVIEW, "reviewers"), [{**REVIEWER, "files_read": None, "bytes_read": None}])),
    *[
        (f"searches {name}", _record, _set((*REVIEW, "reviewers"), [{**REVIEWER, **counts}]))
        for name, counts in (
            ("counted", {"files_read": 0, "bytes_read": 0, "searches": 4}),
            ("unknown", {"files_read": None, "bytes_read": None, "searches": None}),
            ("unknown beside counted reads", {"files_read": 2, "bytes_read": 9, "searches": None}),
        )
    ],
    ("reads not recorded", _record, _set((*REVIEW, "reviewers"), [REVIEWER])),
    ("coverage without uncovered files", _record, _set((*REVIEW, "coverage"), {"unavailable_sources": []})),
    # A version 1 record, written before unreviewed files were recorded, is still read, as is a version 2 one.
    ("version 2 record", _record, _set(("schema_version",), 2)),
    (
        "version 2 APPROVED with unreviewed files alone",
        _record,
        _chain(
            _set(("schema_version",), 2),
            _set((*REVIEW, "verdict"), "APPROVED"),
            _set(
                (*REVIEW, "coverage"),
                {
                    "unavailable_sources": [],
                    "unreviewed_files": {"assets/Logo.PNG": ".png", "pkg/tool.tar.lz": ".tar.lz", "a.msi": ".msi"},
                },
            ),
        ),
    ),
    (
        "version 2 INCOMPLETE with an unavailable source beside an unreviewed file",
        _record,
        _chain(
            _set(("schema_version",), 2),
            _set((*REVIEW, "verdict"), "INCOMPLETE"),
            _set(
                (*REVIEW, "coverage"), {"unavailable_sources": ["data/blob.bin"], "unreviewed_files": {"a.msi": ".msi"}}
            ),
        ),
    ),
    (
        "INCOMPLETE with unavailable sources",
        _record,
        _chain(
            _set((*REVIEW, "verdict"), "INCOMPLETE"),
            _set((*REVIEW, "coverage"), {"unavailable_sources": ["github"], "uncovered_files": []}),
        ),
    ),
    ("APPROVED verdict", _record, _set((*REVIEW, "verdict"), "APPROVED")),
    ("source_commit set", _record, _set((*ADAPTER, "source_commit"), BASE_SHA)),
    ("SHA-256 source_commit", _record, _set((*ADAPTER, "source_commit"), SHA256)),
    ("source hashes", _record, _set((*ADAPTER, "source_hashes"), {"a.md": SHA256, "dir/b.md": SHA256})),
    (
        "a suite profile keyed by its reference beside source hashes",
        _record,
        _set((*ADAPTER, "source_hashes"), {"a.md": SHA256, "suite:design-review": SHA256}),
    ),
    ("usage object", _record, _set((*ADAPTER, "usage"), {})),
    (
        "repository adapter, partial",
        _record,
        _chain(_set((*ADAPTER, "scope"), "repository"), _set((*ADAPTER, "status"), "partial")),
    ),
    ("failed adapter", _record, _set((*ADAPTER, "status"), "failed")),
    # Where the reviewer came from; literal values, since a record keeps them.
    ("the configured generic reviewer", _record, _set((*ADAPTER, "source"), "generic")),
    ("the generic reviewer in place of a review skill", _record, _set((*ADAPTER, "source"), "generic-fallback")),
    *(
        (
            f"a repository reviewer read at its {source}",
            _record,
            _chain(
                _set((*ADAPTER, "scope"), "repository"),
                _set((*ADAPTER, "source"), source),
                _set((*ADAPTER, "source_commit"), BASE_SHA),
            ),
        )
        for source in ("trusted-ref", "base", "default-branch")
    ),
    (
        "no findings",
        _record,
        _chain(_set(("findings",), []), _set((*REVIEW, "counts"), {"MUST_FIX": 0, "SHOULD_FIX": 0, "SUGGESTION": 0})),
    ),
    ("prior disposition without a ledger", _record, _set(("prior_dispositions",), [PRIOR])),
    (
        "artifacts with every lowercase hex digit",
        _record,
        _set(("artifacts",), {"payload_sha256": "0123456789abcdef" * 4, "markdown_sha256": "fedcba9876543210" * 4}),
    ),
    ("artifacts null", _record, _set(("artifacts",), None)),
    ("comments without artifacts", _full_record, _delete(("artifacts",))),
    *((f"comment body {value!r}", _full_record, _set(("github_comments", 0, "body"), value)) for value in ("", " ")),
    (
        "ledger without comments",
        _full_record,
        _chain(_delete(("github_comments",)), _delete(("comment_dispositions",))),
    ),
]

R = RecordError
C = ConfigurationError
F = RecordFieldError
# What a field error names as found, for each wrong value the cases below use, written out independently of the reader.
FOUND: dict[str, str] = {
    "None": "null",
    "True": "boolean true",
    "0": "integer 0",
    "1": "integer 1",
    "-1": "integer -1",
    "1.0": "number 1.0",
    "1.5": "number 1.5",
    "7.0": "number 7.0",
    "[]": "an array",
    "{}": "an object",
}


def _values(*values: Any) -> tuple[Any, ...]:
    """Values of mixed kinds, typed so the case lists built from them type-check."""
    return values


def _found(value: Any, *, text: bool = False) -> str:
    """What a field error names as found: a listed literal, a quoted short string, or an array or object; where a
    string is expected (`text`), an empty or blank string is named as such."""
    if text and isinstance(value, str) and not value.strip():
        return "a blank string" if value else "an empty string"
    if repr(value) in FOUND:
        return FOUND[repr(value)]
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "an object"
    return f"string {json.dumps(value)}"


def _wrong(path: str, expected: str, value: Any) -> str:
    """The one shape a field of the wrong kind is refused with, written out independently of the reader."""
    return f"{path} must be {expected}, found {_found(value, text='string' in expected)}"


def _missing(path: str, field: str) -> str:
    return f"{path} must be an object with field {field}, found an object without it"


def _unknown(path: str, *fields: str) -> str:
    names = ", ".join(f'"{field}"' for field in fields)
    return f"{path} must be an object of known fields, found unknown field{'s' if len(fields) > 1 else ''} {names}"


def _dotted(path: tuple[Key, ...]) -> str:
    """A record path as a field error names it: `record`, then each key after a dot and each index in brackets."""
    return "record" + "".join(f"[{key}]" if isinstance(key, int) else f".{key}" for key in path)


def _blank_and_non_string(path: tuple[Key, ...]) -> list[tuple[str, Mutation, type[Exception], str]]:
    name = ".".join(map(str, path))
    return [
        (f"{name} blank", _set(path, " "), F, _wrong(_dotted(path), TEXT, " ")),
        (f"{name} not a string", _set(path, 1), F, _wrong(_dotted(path), TEXT, 1)),
    ]


SOURCE_HASHES = "record.review.adapter.source_hashes"
EXCLUDED_COUNT = "record.review.snapshot.excluded.configured"
FILES_READ = "record.review.reviewers[0].files_read"

# Records validate_record refuses, from the minimal record, with the exception class and message it raises.
UNREVIEWED = "record.review.coverage.unreviewed_files"
UNREVIEWED_SUFFIX_KIND = "a lowercase file-name suffix the path's name ends in"


def _unreviewed(files: Any) -> Mutation:
    """A version 2 record whose coverage holds these unreviewed files and no other file."""
    return _chain(
        _set(("schema_version",), 2),
        _set((*REVIEW, "coverage"), {"unavailable_sources": [], "unreviewed_files": files}),
    )


REJECTED: list[tuple[str, Mutation, type[Exception], str]] = [
    *(
        (
            f"schema_version {value!r}",
            _set(("schema_version",), value),
            F,
            _wrong("record.schema_version", "an integer", value),
        )
        for value in (True, 1.0)
    ),
    *(
        (
            f"protocol_version {value!r}",
            _set((*ADAPTER, "protocol_version"), value),
            F,
            _wrong("record.review.adapter.protocol_version", "an integer", value),
        )
        for value in (True, 1.0)
    ),
    *(
        (
            f"drive-letter source hash path {path}",
            _set((*ADAPTER, "source_hashes"), {path: SHA256}),
            R,
            "Review adapter source hash path is unsafe",
        )
        for path in ("C:/x.md", "c:/x.md", "C:x.md", "C:\\x.md")
    ),
    *(
        (f"drive-letter finding path {path}", _set((*FINDING, "path"), path), R, "Review finding F001.path is unsafe")
        for path in ("C:/x.py", "c:/x.py", "C:x.py", "C:\\x.py")
    ),
    ("record not an object", _replace([]), F, "record must be an object, found an array"),
    ("unknown top-level key", _set(("extra",), 1), F, _unknown("record", "extra")),
    *(
        (f"schema_version {value}", _set(("schema_version",), value), R, "Unsupported or malformed review record")
        for value in (0, 3)
    ),
    ("schema_version missing", _delete(("schema_version",)), F, "record.schema_version must be an integer, found null"),
    (
        "comments empty",
        _set(("github_comments",), []),
        F,
        "record.github_comments must be a non-empty array, found an empty array",
    ),
    (
        "comment dispositions alone",
        _set(("comment_dispositions",), []),
        R,
        "Review comment dispositions need the comments they answer",
    ),
    ("repository missing", _delete(("repository",)), F, "record.repository must be a non-empty string, found null"),
    ("repository malformed", _set(("repository",), "bad"), C, "Invalid repository identity: 'bad'"),
    ("pull_request not an object", _set(PULL, []), F, "record.pull_request must be an object, found an array"),
    ("pull_request missing", _delete(PULL), F, "record.pull_request must be an object, found null"),
    ("review not an object", _set(REVIEW, "x"), F, 'record.review must be an object, found string "x"'),
    ("review missing", _delete(REVIEW), F, "record.review must be an object, found null"),
    ("pull field missing", _delete((*PULL, "url")), F, _missing("record.pull_request", "url")),
    ("pull field unknown", _set((*PULL, "extra"), 1), F, _unknown("record.pull_request", "extra")),
    *_blank_and_non_string((*PULL, "head_ref")),
    ("body_characters alone", _set((*PULL, "body_characters"), 1), R, "Review pull-request fields are malformed"),
    ("body_given alone", _set((*PULL, "body_given"), 1), R, "Review pull-request fields are malformed"),
    *(
        (f"number {value!r}", _set((*PULL, "number"), value), F, _wrong("record.pull_request.number", POSITIVE, value))
        for value in (True, 0, "7")
    ),
    *_blank_and_non_string((*PULL, "url")),
    *_blank_and_non_string((*PULL, "title")),
    *_blank_and_non_string((*PULL, "base_ref")),
    (
        "base_sha uppercase",
        _set((*PULL, "base_sha"), "A" * 40),
        F,
        f"record.pull_request.base_sha must be {SHA_KIND}, found {NOT_HEX}",
    ),
    (
        "base_sha short",
        _set((*PULL, "base_sha"), "1" * 39),
        F,
        f"record.pull_request.base_sha must be {SHA_KIND}, found a string of 39 characters",
    ),
    (
        "base_sha not a string",
        _set((*PULL, "base_sha"), None),
        F,
        f"record.pull_request.base_sha must be {SHA_KIND}, found null",
    ),
    (
        "head_sha uppercase",
        _set((*PULL, "head_sha"), "A" * 64),
        F,
        f"record.pull_request.head_sha must be {SHA_KIND}, found {NOT_HEX}",
    ),
    ("review field missing", _delete((*REVIEW, "summary")), F, _missing("record.review", "summary")),
    ("review field unknown", _set((*REVIEW, "extra"), 1), F, _unknown("record.review", "extra")),
    (
        "reviewers empty",
        _set((*REVIEW, "reviewers"), []),
        F,
        "record.review.reviewers must be a non-empty array, found an empty array",
    ),
    ("patches empty", _set((*REVIEW, "patches"), {}), R, "Review patches must be a non-empty object"),
    *(
        (
            f"verdict {value!r}",
            _set((*REVIEW, "verdict"), value),
            F,
            _wrong("record.review.verdict", VERDICT_KIND, value),
        )
        for value in _values("MAYBE", [])
    ),
    (
        "coverage not an object",
        _set((*REVIEW, "coverage"), []),
        F,
        "record.review.coverage must be an object, found an array",
    ),
    (
        "coverage without sources",
        _set((*REVIEW, "coverage"), {"uncovered_files": []}),
        F,
        _missing("record.review.coverage", "unavailable_sources"),
    ),
    (
        "coverage unknown key",
        _set((*REVIEW, "coverage"), {"unavailable_sources": [], "x": []}),
        F,
        _unknown("record.review.coverage", "x"),
    ),
    (
        "coverage duplicate source",
        _set((*REVIEW, "coverage"), {"unavailable_sources": ["a", "a"]}),
        R,
        "Review coverage is malformed",
    ),
    (
        "coverage empty source",
        _set((*REVIEW, "coverage"), {"unavailable_sources": [""]}),
        F,
        _wrong("record.review.coverage.unavailable_sources[0]", TEXT, ""),
    ),
    (
        "coverage sources not a list",
        _set((*REVIEW, "coverage"), {"unavailable_sources": "a"}),
        F,
        'record.review.coverage.unavailable_sources must be an array, found string "a"',
    ),
    (
        "coverage uncovered files not a list",
        _set((*REVIEW, "coverage"), {"unavailable_sources": [], "uncovered_files": "a"}),
        F,
        'record.review.coverage.uncovered_files must be an array, found string "a"',
    ),
    (
        "coverage unreviewed files on a version 1 record",
        _chain(
            _set(("schema_version",), 1),
            _set((*REVIEW, "coverage"), {"unavailable_sources": [], "unreviewed_files": {"a.msi": ".msi"}}),
        ),
        F,
        _unknown("record.review.coverage", "unreviewed_files"),
    ),
    (
        "coverage unreviewed files not an object",
        _unreviewed([]),
        F,
        "record.review.coverage.unreviewed_files must be an object, found an array",
    ),
    (
        "coverage unreviewed file with an empty path",
        _unreviewed({"": ".msi"}),
        F,
        _wrong("a key of record.review.coverage.unreviewed_files", TEXT, ""),
    ),
    *(
        (
            f"coverage unreviewed file suffix {value!r}",
            _unreviewed({"a.msi": value}),
            F,
            _wrong(f"{UNREVIEWED}.a.msi", TEXT, value),
        )
        for value in (None, 1, "", [".msi"])
    ),
    *(
        (
            f"coverage unreviewed file {path} with suffix {suffix!r}",
            _unreviewed({path: suffix}),
            F,
            _wrong(f"{UNREVIEWED}.{path}", UNREVIEWED_SUFFIX_KIND, suffix),
        )
        for path, suffix in (
            ("a.msi", ".MSI"),
            ("a.msi", "msi"),
            ("a.msi", ".zip"),
            ("a.msi", "*.msi"),
            ("a.msi", "." + "m" * 32),
            (".msi", ".msi"),
            ("dist/.msi", ".msi"),
            ("a.msi.txt", ".msi"),
        )
    ),
    (
        "coverage unreviewed file also unavailable",
        _chain(
            _set(("schema_version",), 2),
            _set((*REVIEW, "coverage"), {"unavailable_sources": ["a.msi"], "unreviewed_files": {"a.msi": ".msi"}}),
        ),
        R,
        "Review coverage names a.msi both as unavailable and as unreviewed",
    ),
    (
        "INCOMPLETE with unreviewed files alone",
        _chain(
            _unreviewed({"a.msi": ".msi"}),
            _set((*REVIEW, "verdict"), "INCOMPLETE"),
        ),
        R,
        "An INCOMPLETE review must list its unavailable sources",
    ),
    (
        "INCOMPLETE without coverage",
        _set((*REVIEW, "verdict"), "INCOMPLETE"),
        R,
        "An INCOMPLETE review must list its unavailable sources",
    ),
    (
        "INCOMPLETE with no sources",
        _chain(_set((*REVIEW, "verdict"), "INCOMPLETE"), _set((*REVIEW, "coverage"), {"unavailable_sources": []})),
        R,
        "An INCOMPLETE review must list its unavailable sources",
    ),
    *(
        (f"version {value!r}", _set((*REVIEW, "version"), value), F, _wrong("record.review.version", POSITIVE, value))
        for value in (True, 0, "1")
    ),
    (
        "mode unknown",
        _set((*REVIEW, "mode"), "second"),
        F,
        _wrong("record.review.mode", 'one of "initial", "re-review"', "second"),
    ),
    ("scope on an initial review", _set((*REVIEW, "scope"), {}), R, "Only a re-review has a scope"),
    *(
        (
            f"dispatch {value!r}",
            _set((*REVIEW, "dispatch"), value),
            F,
            _wrong("record.review.dispatch", DISPATCH_KIND, value),
        )
        # An unknown mode, a known one in another case, a list, and null.
        for value in ("workflow", "Inline", ["inline"], None)
    ),
    ("snapshot null", _set((*REVIEW, "snapshot"), None), F, "record.review.snapshot must be an object, found null"),
    (
        "snapshot seconds null",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "seconds": None}),
        F,
        "record.review.snapshot.seconds must be an object, found null",
    ),
    (
        "snapshot without seconds",
        _set((*REVIEW, "snapshot"), {key: SNAPSHOT[key] for key in ("source", "files", "bytes")}),
        F,
        _missing("record.review.snapshot", "seconds"),
    ),
    (
        "snapshot field unknown",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "paths": []}),
        F,
        _unknown("record.review.snapshot", "paths"),
    ),
    (
        "snapshot source unknown",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "source": "archive"}),
        F,
        _wrong("record.review.snapshot.source", SNAPSHOT_SOURCE_KIND, "archive"),
    ),
    (
        "snapshot diff null",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "diff": None}),
        F,
        "record.review.snapshot.diff must be an object, found null",
    ),
    (
        "snapshot diff without its limit",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "diff": {"source": "checkout"}}),
        F,
        _missing("record.review.snapshot.diff", "limit"),
    ),
    (
        "snapshot diff source unknown",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "diff": {"source": "github", "limit": "300 files"}}),
        F,
        _wrong("record.review.snapshot.diff.source", 'one of "checkout", "files-api"', "github"),
    ),
    *[
        (
            f"snapshot diff limit {limit!r}",
            _set((*REVIEW, "snapshot"), {**SNAPSHOT, "diff": {"source": "files-api", "limit": limit}}),
            R,
            "Review snapshot diff limit must be a count and what it counts, as in 300 files",
        )
        for limit in ("300", "files", "0 files", "300 Files", "300 files\n", "-1 files")
    ],
    *[
        (
            f"snapshot {field} {value!r}",
            _set((*REVIEW, "snapshot"), {**SNAPSHOT, field: value}),
            F,
            _wrong(f"record.review.snapshot.{field}", COUNT, value),
        )
        for field in ("files", "bytes")
        for value in (-1, 1.5, True, "1", None)
    ],
    *[
        (f"snapshot excluded {name}", _set((*REVIEW, "snapshot"), {**SNAPSHOT, "excluded": excluded}), F, message)
        for name, excluded, message in (
            ("null", None, "record.review.snapshot.excluded must be an object, found null"),
            ("a list", ["configured"], "record.review.snapshot.excluded must be an object, found an array"),
            *(
                (f"a {label} count", {"configured": count}, _wrong(EXCLUDED_COUNT, POSITIVE, count))
                for label, count in (("zero", 0), ("negative", -1), ("fractional", 1.5), ("true", True))
            ),
            (
                "an empty reason",
                {"": 1},
                f"a key of record.review.snapshot.excluded must be {TEXT}, found an empty string",
            ),
        )
    ],
    *[
        (
            f"snapshot excluded {name}",
            _set((*REVIEW, "snapshot"), {**SNAPSHOT, "excluded": excluded}),
            R,
            SNAPSHOT_EXCLUDED,
        )
        for name, excluded in (
            ("a reason in capitals", {"Configured": 1}),
            ("a reason with a space", {"file size": 1}),
            ("a reason of 41 characters", {"a" * 41: 1}),
        )
    ],
    *[
        (f"snapshot seconds {name}", _set((*REVIEW, "snapshot"), {**SNAPSHOT, "seconds": seconds}), F, message)
        for name, seconds, message in (
            (
                "missing a phase",
                {"fetch": 1, "materialize": 2},
                _missing("record.review.snapshot.seconds", "prompts"),
            ),
            (
                "with another phase",
                {"fetch": 1, "materialize": 2, "prompts": 3, "review": 4},
                _unknown("record.review.snapshot.seconds", "review"),
            ),
            (
                "infinite",
                {"fetch": float("inf"), "materialize": 2, "prompts": 3},
                "record.review.snapshot.seconds.fetch must be a finite number, found number inf",
            ),
            (
                "not a number",
                {"fetch": "1", "materialize": 2, "prompts": 3},
                'record.review.snapshot.seconds.fetch must be a finite number, found string "1"',
            ),
            (
                "true",
                {"fetch": True, "materialize": 2, "prompts": 3},
                "record.review.snapshot.seconds.fetch must be a finite number, found boolean true",
            ),
            ("one number", 36.5, "record.review.snapshot.seconds must be an object, found number 36.5"),
        )
    ],
    (
        "snapshot seconds negative",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "seconds": {"fetch": -0.1, "materialize": 2, "prompts": 3}}),
        R,
        SNAPSHOT_SECONDS,
    ),
    *[
        (f"reads {name}", _set((*REVIEW, "reviewers"), [{**REVIEWER, **counts}]), R, READ_COUNTS)
        for name, counts in (
            ("files alone", {"files_read": 3}),
            ("bytes alone", {"bytes_read": 3}),
            ("one null", {"files_read": 3, "bytes_read": None}),
        )
    ],
    *[
        (
            f"reads {name}",
            _set((*REVIEW, "reviewers"), [{**REVIEWER, "files_read": value, "bytes_read": 2}]),
            F,
            _wrong(FILES_READ, f"{COUNT} or null", value),
        )
        for name, value in (("negative", -1), ("not integers", 1.0), ("true", True))
    ],
    *[
        (f"searches {name}", _set((*REVIEW, "reviewers"), [{**REVIEWER, **counts}]), R, SEARCHES)
        for name, counts in (
            ("alone", {"searches": 3}),
            ("null alone", {"searches": None}),
            ("counted beside unknown reads", {"files_read": None, "bytes_read": None, "searches": 0}),
        )
    ],
    *[
        (
            f"searches {name}",
            _set((*REVIEW, "reviewers"), [{**REVIEWER, "files_read": 1, "bytes_read": 2, "searches": value}]),
            F,
            _wrong("record.review.reviewers[0].searches", f"{COUNT} or null", value),
        )
        for name, value in (("negative", -1), ("not integers", 1.0), ("true", True))
    ],
    ("reviewed_at not a string", _set((*REVIEW, "reviewed_at"), 1), F, _wrong("record.review.reviewed_at", TEXT, 1)),
    ("reviewed_at unparsable", _set((*REVIEW, "reviewed_at"), "yesterday"), R, "Review timestamp is invalid"),
    *_blank_and_non_string((*REVIEW, "summary")),
    ("adapter not an object", _set(ADAPTER, []), F, "record.review.adapter must be an object, found an array"),
    ("adapter field missing", _delete((*ADAPTER, "usage")), F, _missing("record.review.adapter", "usage")),
    ("adapter field unknown", _set((*ADAPTER, "extra"), 1), F, _unknown("record.review.adapter", "extra")),
    ("protocol_version 2", _set((*ADAPTER, "protocol_version"), 2), R, "Review adapter protocol is unsupported"),
    (
        "protocol_version string",
        _set((*ADAPTER, "protocol_version"), "1"),
        F,
        _wrong("record.review.adapter.protocol_version", "an integer", "1"),
    ),
    (
        "adapter scope unknown",
        _set((*ADAPTER, "scope"), "global"),
        F,
        _wrong("record.review.adapter.scope", 'one of "generic", "repository"', "global"),
    ),
    *(
        (
            f"adapter source {value!r}",
            _set((*ADAPTER, "source"), value),
            F,
            _wrong("record.review.adapter.source", ADAPTER_SOURCE_KIND, value),
        )
        for value in ("head", ["base"])
    ),
    ("adapter source of a repository reviewer", _set((*ADAPTER, "source"), "base"), R, GENERIC_SOURCE),
    (
        "generic adapter source read at a commit",
        _chain(_set((*ADAPTER, "source"), "generic-fallback"), _set((*ADAPTER, "source_commit"), BASE_SHA)),
        R,
        GENERIC_SOURCE,
    ),
    (
        "repository adapter source of the generic reviewer",
        _chain(
            _set((*ADAPTER, "scope"), "repository"),
            _set((*ADAPTER, "source"), "generic"),
            _set((*ADAPTER, "source_commit"), BASE_SHA),
        ),
        R,
        "Review adapter source is invalid for a repository reviewer",
    ),
    (
        "repository adapter source without a commit",
        _chain(
            _set((*ADAPTER, "scope"), "repository"),
            _set((*ADAPTER, "source"), "default-branch"),
            _set((*ADAPTER, "source_commit"), None),
        ),
        R,
        "Review adapter source is invalid for a repository reviewer",
    ),
    *_blank_and_non_string((*ADAPTER, "name")),
    *_blank_and_non_string((*ADAPTER, "reviewer")),
    (
        "adapter status unknown",
        _set((*ADAPTER, "status"), "done"),
        F,
        _wrong("record.review.adapter.status", STATUS_KIND, "done"),
    ),
    (
        "source_commit invalid",
        _set((*ADAPTER, "source_commit"), "abc"),
        F,
        f"record.review.adapter.source_commit must be {SHA_KIND} or null, found a string of 3 characters",
    ),
    (
        "source_hashes not an object",
        _set((*ADAPTER, "source_hashes"), []),
        F,
        f"{SOURCE_HASHES} must be an object, found an array",
    ),
    *[
        (
            f"source hash path {path!r}",
            _set((*ADAPTER, "source_hashes"), {path: SHA256}),
            F,
            _wrong(f"a key of {SOURCE_HASHES}", TEXT, path),
        )
        for path in ("", 1)
    ],
    *[
        (
            f"source hash path {path!r}",
            _set((*ADAPTER, "source_hashes"), {path: SHA256}),
            R,
            "Review adapter source hash path is unsafe",
        )
        for path in ("/a.md", "\\a.md", "a\\b.md", "../a.md", "a/../b.md")
    ],
    (
        "source hash invalid",
        _set((*ADAPTER, "source_hashes"), {"a.md": "A" * 64}),
        F,
        f"{SOURCE_HASHES}.a.md must be {DIGEST_KIND}, found {NOT_HEX}",
    ),
    (
        "source hash not a string",
        _set((*ADAPTER, "source_hashes"), {"a.md": None}),
        F,
        f"{SOURCE_HASHES}.a.md must be {DIGEST_KIND}, found null",
    ),
    (
        "first bad source hash wins",
        _set((*ADAPTER, "source_hashes"), {"b.md": "x", "/a.md": SHA256}),
        F,
        f"{SOURCE_HASHES}.b.md must be {DIGEST_KIND}, found a string of 1 character",
    ),
    (
        "usage not an object",
        _set((*ADAPTER, "usage"), []),
        F,
        "record.review.adapter.usage must be an object or null, found an array",
    ),
    ("findings not a list", _set(("findings",), {}), F, "record.findings must be an array, found an object"),
    ("findings missing", _delete(("findings",)), F, "record.findings must be an array, found null"),
    (
        "finding ID out of sequence",
        _set((*FINDING, "id"), "F002"),
        R,
        "Review finding IDs are not stable and contiguous",
    ),
    ("finding ID missing", _delete((*FINDING, "id")), R, "Review finding IDs are not stable and contiguous"),
    (
        "finding not an object",
        _set(("findings",), ["F001"]),
        F,
        'record.findings[0] must be an object, found string "F001"',
    ),
    ("finding field missing", _delete((*FINDING, "body")), F, _missing("record.findings[0]", "body")),
    ("finding field unknown", _set((*FINDING, "extra"), 1), F, _unknown("record.findings[0]", "extra")),
    *(
        (
            f"finding severity {value!r}",
            _set((*FINDING, "severity"), value),
            F,
            _wrong("record.findings[0].severity", SEVERITY_KIND, value),
        )
        for value in _values("LOW", [])
    ),
    (
        "finding title blank",
        _set((*FINDING, "title"), " "),
        F,
        f"record.findings[0].title must be {TITLE_KIND}, found a blank string",
    ),
    (
        "finding title two lines",
        _set((*FINDING, "title"), "a\nb"),
        F,
        f"record.findings[0].title must be {TITLE_KIND}, found a string of more than one line",
    ),
    ("finding analyzer empty", _set((*FINDING, "analyzer"), {}), F, f"record.findings[0].analyzer {ANALYZER_MISSING}"),
    *[
        case
        for field in ("candidate_key", "category", "path", "body", "evidence", "source")
        for case in _blank_and_non_string((*FINDING, field))
    ],
    *[
        (f"finding path {path!r}", _set((*FINDING, "path"), path), R, "Review finding F001.path is unsafe")
        for path in ("/a.py", "\\a.py", "a\\b.py", "../a.py", "a/../b.py")
    ],
    *(
        (
            f"finding line {value!r}",
            _set((*FINDING, "line"), value),
            F,
            _wrong("record.findings[0].line", POSITIVE, value),
        )
        for value in (True, 0, "3")
    ),
    (
        "first bad finding wins",
        _chain(
            _set(("findings",), [_finding("F001", "MUST_FIX"), _finding("F002", "MUST_FIX")]),
            _set(("findings", 1, "line"), 0),
            _set(("findings", 0, "body"), ""),
        ),
        F,
        "record.findings[0].body must be a non-empty string, found an empty string",
    ),
    ("counts wrong", _set((*REVIEW, "counts", "MUST_FIX"), 2), R, "Review finding counts do not match findings"),
    (
        "counts key missing",
        _delete((*REVIEW, "counts", "SUGGESTION")),
        F,
        _missing("record.review.counts", "SUGGESTION"),
    ),
    ("counts key unknown", _set((*REVIEW, "counts", "NIT"), 0), F, _unknown("record.review.counts", "NIT")),
    ("counts not an object", _set((*REVIEW, "counts"), None), F, "record.review.counts must be an object, found null"),
    (
        "prior dispositions missing",
        _delete(("prior_dispositions",)),
        F,
        "record.prior_dispositions must be an array, found null",
    ),
    (
        "prior dispositions not a list",
        _set(("prior_dispositions",), {}),
        F,
        "record.prior_dispositions must be an array, found an object",
    ),
    (
        "prior disposition not an object",
        _set(("prior_dispositions",), ["v1:F001"]),
        F,
        'record.prior_dispositions[0] must be an object, found string "v1:F001"',
    ),
    (
        "prior disposition field unknown",
        _set(("prior_dispositions",), [{**PRIOR, "extra": 1}]),
        F,
        _unknown("record.prior_dispositions[0]", "extra"),
    ),
    *(
        (
            f"prior disposition ID {value!r}",
            _set(("prior_dispositions",), [{**PRIOR, "finding_id": value}]),
            F,
            _wrong("record.prior_dispositions[0].finding_id", TEXT, value),
        )
        for value in (1, "")
    ),
    (
        "prior disposition ID duplicated",
        _set(("prior_dispositions",), [PRIOR, PRIOR]),
        R,
        "Review prior disposition IDs must be unique",
    ),
    (
        "prior disposition unknown",
        _set(("prior_dispositions",), [{**PRIOR, "disposition": "fixed"}]),
        F,
        _wrong("record.prior_dispositions[0].disposition", DISPOSITION_KIND, "fixed"),
    ),
    (
        "prior disposition rationale blank",
        _set(("prior_dispositions",), [{**PRIOR, "rationale": " "}]),
        F,
        _wrong("record.prior_dispositions[0].rationale", TEXT, " "),
    ),
    (
        "repeat without a ledger",
        _set((*FINDING, "repeats"), {"version": 1, "id": "F001"}),
        R,
        "Review finding repeats need a ledger",
    ),
    ("artifacts not an object", _set(("artifacts",), []), F, "record.artifacts must be an object, found an array"),
    (
        "artifacts key missing",
        _set(("artifacts",), {"payload_sha256": SHA256}),
        F,
        _missing("record.artifacts", "markdown_sha256"),
    ),
    *(
        (
            f"artifacts.{field} {value!r}",
            _set(("artifacts",), {"payload_sha256": SHA256, "markdown_sha256": SHA256, field: value}),
            F,
            f"record.artifacts.{field} must be {DIGEST_KIND}, found {found}",
        )
        for field in ("payload_sha256", "markdown_sha256")
        for value, found in INVALID_DIGESTS
    ),
]

# One fault per check, in the order validate_record makes its checks. Each touches its own field or replaces what a
# check before it would read, so any suffix of the list can be applied to one record at once.
STAGES: list[tuple[str, Mutation, type[Exception], str]] = [
    ("record object", _replace([]), F, "record must be an object, found an array"),
    ("record", _set(("extra",), 1), F, _unknown("record", "extra")),
    (
        "schema_version kind",
        _set(("schema_version",), True),
        F,
        "record.schema_version must be an integer, found boolean true",
    ),
    ("schema_version", _set(("schema_version",), 3), R, "Unsupported or malformed review record"),
    (
        "comments needed",
        _chain(_set(("github_comments",), None), _set(("comment_dispositions",), [])),
        R,
        "Review comment dispositions need the comments they answer",
    ),
    (
        "comments",
        _set(("github_comments",), []),
        F,
        "record.github_comments must be a non-empty array, found an empty array",
    ),
    ("repository kind", _set(("repository",), None), F, "record.repository must be a non-empty string, found null"),
    ("repository", _set(("repository",), "bad"), C, "Invalid repository identity: 'bad'"),
    ("pull_request", _set(PULL, []), F, "record.pull_request must be an object, found an array"),
    ("review", _set(REVIEW, []), F, "record.review must be an object, found an array"),
    ("pull fields", _set((*PULL, "extra"), 1), F, _unknown("record.pull_request", "extra")),
    ("pull body counts", _set((*PULL, "body_characters"), 1), R, "Review pull-request fields are malformed"),
    ("head_ref", _set((*PULL, "head_ref"), ""), F, _wrong("record.pull_request.head_ref", TEXT, "")),
    ("number", _set((*PULL, "number"), 0), F, _wrong("record.pull_request.number", POSITIVE, 0)),
    ("url", _set((*PULL, "url"), ""), F, _wrong("record.pull_request.url", TEXT, "")),
    ("title", _set((*PULL, "title"), ""), F, _wrong("record.pull_request.title", TEXT, "")),
    ("base_ref", _set((*PULL, "base_ref"), ""), F, _wrong("record.pull_request.base_ref", TEXT, "")),
    (
        "base_sha",
        _set((*PULL, "base_sha"), ""),
        F,
        f"record.pull_request.base_sha must be {SHA_KIND}, found a string of 0 characters",
    ),
    (
        "head_sha",
        _set((*PULL, "head_sha"), ""),
        F,
        f"record.pull_request.head_sha must be {SHA_KIND}, found a string of 0 characters",
    ),
    ("review fields", _set((*REVIEW, "extra"), 1), F, _unknown("record.review", "extra")),
    (
        "dispatch",
        _set((*REVIEW, "dispatch"), "workflow"),
        F,
        _wrong("record.review.dispatch", DISPATCH_KIND, "workflow"),
    ),
    ("snapshot fields", _set((*REVIEW, "snapshot"), []), F, "record.review.snapshot must be an object, found an array"),
    (
        "snapshot excluded",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "excluded": []}),
        F,
        "record.review.snapshot.excluded must be an object, found an array",
    ),
    (
        "snapshot excluded reason kind",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "excluded": {"": 1}}),
        F,
        f"a key of record.review.snapshot.excluded must be {TEXT}, found an empty string",
    ),
    (
        "snapshot excluded reason",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "excluded": {"Configured": 1}}),
        R,
        SNAPSHOT_EXCLUDED,
    ),
    (
        "snapshot excluded count",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "excluded": {"configured": 0}}),
        F,
        _wrong(EXCLUDED_COUNT, POSITIVE, 0),
    ),
    (
        "snapshot source",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "source": "x"}),
        F,
        _wrong("record.review.snapshot.source", SNAPSHOT_SOURCE_KIND, "x"),
    ),
    (
        "snapshot diff",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "diff": []}),
        F,
        "record.review.snapshot.diff must be an object, found an array",
    ),
    (
        "snapshot diff source",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "diff": {"source": "x", "limit": "300 files"}}),
        F,
        _wrong("record.review.snapshot.diff.source", 'one of "checkout", "files-api"', "x"),
    ),
    (
        "snapshot diff limit",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "diff": {"source": "checkout", "limit": "300"}}),
        R,
        "Review snapshot diff limit must be a count and what it counts, as in 300 files",
    ),
    (
        "snapshot files",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "files": -1}),
        F,
        _wrong("record.review.snapshot.files", COUNT, -1),
    ),
    (
        "snapshot bytes",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "bytes": -1}),
        F,
        _wrong("record.review.snapshot.bytes", COUNT, -1),
    ),
    (
        "snapshot seconds",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "seconds": []}),
        F,
        "record.review.snapshot.seconds must be an object, found an array",
    ),
    (
        "snapshot seconds kind",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "seconds": {"fetch": None, "materialize": 0, "prompts": 0}}),
        F,
        "record.review.snapshot.seconds.fetch must be a finite number, found null",
    ),
    (
        "snapshot seconds negative",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "seconds": {"fetch": -1, "materialize": 0, "prompts": 0}}),
        R,
        SNAPSHOT_SECONDS,
    ),
    (
        "reviewers",
        _set((*REVIEW, "reviewers"), []),
        F,
        "record.review.reviewers must be a non-empty array, found an empty array",
    ),
    ("reads", _set((*REVIEW, "reviewers"), [{**REVIEWER, "files_read": 1}]), R, READ_COUNTS),
    (
        "reads kind",
        _set((*REVIEW, "reviewers"), [{**REVIEWER, "files_read": -1, "bytes_read": 0}]),
        F,
        _wrong(FILES_READ, f"{COUNT} or null", -1),
    ),
    ("searches", _set((*REVIEW, "reviewers"), [{**REVIEWER, "searches": 0}]), R, SEARCHES),
    (
        "searches kind",
        _set((*REVIEW, "reviewers"), [{**REVIEWER, "files_read": 1, "bytes_read": 0, "searches": -1}]),
        F,
        _wrong("record.review.reviewers[0].searches", f"{COUNT} or null", -1),
    ),
    ("patches", _set((*REVIEW, "patches"), {}), R, "Review patches must be a non-empty object"),
    ("verdict", _set((*REVIEW, "verdict"), "MAYBE"), F, _wrong("record.review.verdict", VERDICT_KIND, "MAYBE")),
    ("coverage", _set((*REVIEW, "coverage"), []), F, "record.review.coverage must be an object, found an array"),
    (
        "coverage sources",
        _set((*REVIEW, "coverage"), {"unavailable_sources": "a"}),
        F,
        'record.review.coverage.unavailable_sources must be an array, found string "a"',
    ),
    (
        "coverage source",
        _set((*REVIEW, "coverage"), {"unavailable_sources": [""]}),
        F,
        _wrong("record.review.coverage.unavailable_sources[0]", TEXT, ""),
    ),
    (
        "coverage duplicate",
        _set((*REVIEW, "coverage"), {"unavailable_sources": ["a", "a"]}),
        R,
        "Review coverage is malformed",
    ),
    (
        "coverage uncovered files",
        _set((*REVIEW, "coverage"), {"unavailable_sources": [], "uncovered_files": "a"}),
        F,
        'record.review.coverage.uncovered_files must be an array, found string "a"',
    ),
    (
        "coverage unreviewed files",
        _unreviewed([]),
        F,
        "record.review.coverage.unreviewed_files must be an object, found an array",
    ),
    (
        "coverage unreviewed file path",
        _unreviewed({"": ".msi"}),
        F,
        _wrong("a key of record.review.coverage.unreviewed_files", TEXT, ""),
    ),
    ("coverage unreviewed file suffix kind", _unreviewed({"a.msi": 1}), F, _wrong(f"{UNREVIEWED}.a.msi", TEXT, 1)),
    (
        "coverage unreviewed file suffix",
        _unreviewed({"a.msi": ".zip"}),
        F,
        _wrong(f"{UNREVIEWED}.a.msi", UNREVIEWED_SUFFIX_KIND, ".zip"),
    ),
    (
        "coverage unreviewed file unavailable",
        _chain(
            _set(("schema_version",), 2),
            _set((*REVIEW, "coverage"), {"unavailable_sources": ["a.msi"], "unreviewed_files": {"a.msi": ".msi"}}),
        ),
        R,
        "Review coverage names a.msi both as unavailable and as unreviewed",
    ),
    (
        "incomplete",
        _set((*REVIEW, "verdict"), "INCOMPLETE"),
        R,
        "An INCOMPLETE review must list its unavailable sources",
    ),
    ("version", _set((*REVIEW, "version"), 0), F, _wrong("record.review.version", POSITIVE, 0)),
    (
        "mode",
        _set((*REVIEW, "mode"), "second"),
        F,
        _wrong("record.review.mode", 'one of "initial", "re-review"', "second"),
    ),
    ("scope", _set((*REVIEW, "scope"), {}), R, "Only a re-review has a scope"),
    ("reviewed_at kind", _set((*REVIEW, "reviewed_at"), 1), F, _wrong("record.review.reviewed_at", TEXT, 1)),
    ("reviewed_at", _set((*REVIEW, "reviewed_at"), "yesterday"), R, "Review timestamp is invalid"),
    ("summary", _set((*REVIEW, "summary"), ""), F, _wrong("record.review.summary", TEXT, "")),
    ("adapter", _set(ADAPTER, []), F, "record.review.adapter must be an object, found an array"),
    (
        "protocol kind",
        _set((*ADAPTER, "protocol_version"), "1"),
        F,
        _wrong("record.review.adapter.protocol_version", "an integer", "1"),
    ),
    ("protocol", _set((*ADAPTER, "protocol_version"), 2), R, "Review adapter protocol is unsupported"),
    (
        "adapter scope",
        _set((*ADAPTER, "scope"), "global"),
        F,
        _wrong("record.review.adapter.scope", 'one of "generic", "repository"', "global"),
    ),
    (
        "adapter source",
        _set((*ADAPTER, "source"), "head"),
        F,
        _wrong("record.review.adapter.source", ADAPTER_SOURCE_KIND, "head"),
    ),
    ("adapter source scope", _set((*ADAPTER, "source"), "base"), R, GENERIC_SOURCE),
    ("adapter name", _set((*ADAPTER, "name"), ""), F, _wrong("record.review.adapter.name", TEXT, "")),
    ("adapter reviewer", _set((*ADAPTER, "reviewer"), ""), F, _wrong("record.review.adapter.reviewer", TEXT, "")),
    (
        "adapter status",
        _set((*ADAPTER, "status"), "done"),
        F,
        _wrong("record.review.adapter.status", STATUS_KIND, "done"),
    ),
    (
        "source_commit",
        _set((*ADAPTER, "source_commit"), "abc"),
        F,
        f"record.review.adapter.source_commit must be {SHA_KIND} or null, found a string of 3 characters",
    ),
    ("source_hashes", _set((*ADAPTER, "source_hashes"), []), F, f"{SOURCE_HASHES} must be an object, found an array"),
    (
        "source hash path kind",
        _set((*ADAPTER, "source_hashes"), {"": SHA256}),
        F,
        f"a key of {SOURCE_HASHES} must be {TEXT}, found an empty string",
    ),
    (
        "source hash path",
        _set((*ADAPTER, "source_hashes"), {"/a.md": SHA256}),
        R,
        "Review adapter source hash path is unsafe",
    ),
    (
        "source hash",
        _set((*ADAPTER, "source_hashes"), {"a.md": "x"}),
        F,
        f"{SOURCE_HASHES}.a.md must be {DIGEST_KIND}, found a string of 1 character",
    ),
    (
        "usage",
        _set((*ADAPTER, "usage"), []),
        F,
        "record.review.adapter.usage must be an object or null, found an array",
    ),
    ("findings", _set(("findings",), {}), F, "record.findings must be an array, found an object"),
    ("finding object", _set(FINDING, "F001"), F, 'record.findings[0] must be an object, found string "F001"'),
    ("finding IDs", _set((*FINDING, "id"), "F002"), R, "Review finding IDs are not stable and contiguous"),
    ("finding fields", _set((*FINDING, "extra"), 1), F, _unknown("record.findings[0]", "extra")),
    (
        "finding severity",
        _set((*FINDING, "severity"), "LOW"),
        F,
        _wrong("record.findings[0].severity", SEVERITY_KIND, "LOW"),
    ),
    (
        "finding title",
        _set((*FINDING, "title"), ""),
        F,
        f"record.findings[0].title must be {TITLE_KIND}, found an empty string",
    ),
    ("finding analyzer", _set((*FINDING, "analyzer"), {}), F, f"record.findings[0].analyzer {ANALYZER_MISSING}"),
    *[
        (f"finding {field}", _set((*FINDING, field), ""), F, _wrong(f"record.findings[0].{field}", TEXT, ""))
        for field in ("candidate_key", "category", "path", "body", "evidence", "source")
    ],
    ("finding path unsafe", _set((*FINDING, "path"), "/a.py"), R, "Review finding F001.path is unsafe"),
    ("finding line", _set((*FINDING, "line"), 0), F, _wrong("record.findings[0].line", POSITIVE, 0)),
    ("counts object", _set((*REVIEW, "counts"), None), F, "record.review.counts must be an object, found null"),
    ("counts", _set((*REVIEW, "counts", "MUST_FIX"), 2), R, "Review finding counts do not match findings"),
    (
        "prior dispositions",
        _set(("prior_dispositions",), {}),
        F,
        "record.prior_dispositions must be an array, found an object",
    ),
    (
        "prior fields",
        _set(("prior_dispositions",), [[]]),
        F,
        "record.prior_dispositions[0] must be an object, found an array",
    ),
    (
        "prior ID kind",
        _set(("prior_dispositions",), [{**PRIOR, "finding_id": 1}]),
        F,
        _wrong("record.prior_dispositions[0].finding_id", TEXT, 1),
    ),
    ("prior IDs", _set(("prior_dispositions",), [PRIOR, PRIOR]), R, "Review prior disposition IDs must be unique"),
    (
        "prior disposition",
        _set(("prior_dispositions",), [{**PRIOR, "disposition": "fixed"}]),
        F,
        _wrong("record.prior_dispositions[0].disposition", DISPOSITION_KIND, "fixed"),
    ),
    (
        "prior rationale",
        _set(("prior_dispositions",), [{**PRIOR, "rationale": ""}]),
        F,
        _wrong("record.prior_dispositions[0].rationale", TEXT, ""),
    ),
    ("ledger", _set((*FINDING, "repeats"), {"version": 1, "id": "F001"}), R, "Review finding repeats need a ledger"),
    ("artifacts", _set(("artifacts",), []), F, "record.artifacts must be an object, found an array"),
    (
        "payload hash",
        _set(("artifacts",), {"payload_sha256": "x", "markdown_sha256": SHA256}),
        F,
        f"record.artifacts.payload_sha256 must be {DIGEST_KIND}, found a string of 1 character",
    ),
    (
        "markdown hash",
        _set(("artifacts",), {"payload_sha256": SHA256, "markdown_sha256": "x"}),
        F,
        f"record.artifacts.markdown_sha256 must be {DIGEST_KIND}, found a string of 1 character",
    ),
]

# A value of the wrong kind for each kind validate_record reads, refused as a field error naming the field's path, the
# kind it expects, and what it found. The messages are written out whole, independently of the reader and the helpers.
WRONG_KINDS: list[tuple[str, Mutation, str]] = [
    ("string", _set((*PULL, "url"), 1), "record.pull_request.url must be a non-empty string, found integer 1"),
    (
        "integer",
        _set((*REVIEW, "version"), "1"),
        'record.review.version must be an integer of at least 1, found string "1"',
    ),
    (
        "number",
        _set((*REVIEW, "snapshot"), {**SNAPSHOT, "seconds": {"fetch": [], "materialize": 0, "prompts": 0}}),
        "record.review.snapshot.seconds.fetch must be a finite number, found an array",
    ),
    (
        "boolean",
        _set((*REVIEW, "reviewers"), [{**REVIEWER, "dispositions_only": "no"}]),
        'record.review.reviewers[0].dispositions_only must be a boolean, found string "no"',
    ),
    ("object", _set(ADAPTER, "generic"), 'record.review.adapter must be an object, found string "generic"'),
    ("array", _set(("findings",), "F001"), 'record.findings must be an array, found string "F001"'),
    (
        "enumerated value",
        _set((*REVIEW, "verdict"), True),
        'record.review.verdict must be one of "APPROVED", "CHANGES_REQUESTED", "INCOMPLETE", found boolean true',
    ),
    (
        "hash",
        _set((*PULL, "head_sha"), 2),
        "record.pull_request.head_sha must be a lowercase hexadecimal digest of 40 or 64 characters, found integer 2",
    ),
    (
        "hash or null",
        _set((*ADAPTER, "source_commit"), 1),
        "record.review.adapter.source_commit must be a lowercase hexadecimal digest of 40 or 64 characters or null, "
        "found integer 1",
    ),
    (
        "object or null",
        _set((*ADAPTER, "usage"), "x"),
        'record.review.adapter.usage must be an object or null, found string "x"',
    ),
    (
        "integer or null",
        _set((*REVIEW, "reviewers"), [{**REVIEWER, "files_read": "1", "bytes_read": 0}]),
        'record.review.reviewers[0].files_read must be an integer of at least 0 or null, found string "1"',
    ),
    (
        "title",
        _set((*FINDING, "title"), 1),
        "record.findings[0].title must be a non-empty trimmed single-line string of at most 120 characters, "
        "found integer 1",
    ),
    (
        "location",
        _set((*FINDING, "location"), " Section 2"),
        "record.findings[0].location must be a non-empty trimmed single-line string of at most 200 characters, "
        "found a string with whitespace around it",
    ),
    (
        "model",
        _set((*REVIEW, "reviewers"), [{**REVIEWER, "model": "model\u2028x"}]),
        "record.review.reviewers[0].model must be a non-empty trimmed single-line string of at most 200 characters, "
        "found a string of more than one line",
    ),
    (
        "analyzer coverage",
        _set((*FINDING, "analyzer"), {"coverage": 1, "tool": "ruff", "rule": "B006"}),
        'record.findings[0].analyzer.coverage must be one of "available", "known", "custom-candidate", found integer 1',
    ),
]

# Faults that exist only while an optional part is absent: a scope on an initial review, comment dispositions
# without comments, and a repeat without a ledger. The full record has those parts, so it cannot carry them.
ABSENCE_FAULTS = {"scope on an initial review", "comment dispositions alone", "repeat without a ledger"}
# Errors whose message names the faulty value or the key it is under: a stage carries the same check with another
# value or under another key. A field error is named by its check, the part before what it found.
VALUE_VARIANTS = {
    (R, "Review adapter source is invalid for a repository reviewer"),
    (F, f"{SOURCE_HASHES}.b.md must be {DIGEST_KIND}"),
    *((F, f"{UNREVIEWED}.{path} must be {UNREVIEWED_SUFFIX_KIND}") for path in (".msi", "dist/.msi", "a.msi.txt")),
}


def _check(error: type[Exception], message: str) -> str:
    """The check a refusal comes from. A field of the wrong kind is one check whatever was found, so it compares by the
    part before ", found "; and an object is one read whether it is not one, lacks a field, or holds an unknown one,
    so those compare by the field's path."""
    if error is not F:
        return message
    expected = message.split(", found ")[0]
    path, shape, _rest = expected.partition(" must be an object")
    return path + shape if shape else expected


class RecordValidationTests(unittest.TestCase):
    def assert_refused(self, record: Any, error: type[Exception], message: str) -> None:
        with self.assertRaises(Exception) as caught:
            validate_record(record)
        self.assertIs(error, type(caught.exception))
        self.assertEqual(message, str(caught.exception))

    def test_accepted_records_are_returned_unchanged(self) -> None:
        for name, build, mutation in ACCEPTED:
            with self.subTest(name):
                record = mutation(build())
                before = copy.deepcopy(record)
                self.assertIs(record, validate_record(record))
                self.assertEqual(before, record)

    def test_each_fault_is_refused_with_its_error(self) -> None:
        for name, mutation, error, message in REJECTED:
            with self.subTest(name):
                self.assert_refused(mutation(_record()), error, message)

    def test_each_fault_is_refused_in_the_full_record(self) -> None:
        # The same faults with every optional part present, so no check depends on the parts being absent.
        for name, mutation, error, message in REJECTED:
            if name in ABSENCE_FAULTS:
                continue
            with self.subTest(name):
                self.assert_refused(mutation(_full_record()), error, message)

    def test_a_review_comment_names_its_author_path_and_url(self) -> None:
        # GitHub may leave a comment's body empty, which the body cases above accept; the rest it always names.
        for field in ("author", "path", "url"):
            for value in ("", " "):
                with self.subTest(field=field, value=value):
                    record = _set(("github_comments", 0, field), value)(_full_record())
                    message = _wrong(f"record.github_comments[0].{field}", TEXT, value)
                    self.assert_refused(record, RecordFieldError, message)

    def test_each_stage_is_refused_alone(self) -> None:
        for name, mutation, error, message in STAGES:
            with self.subTest(name):
                self.assert_refused(mutation(_record()), error, message)

    def test_faults_are_detected_in_order(self) -> None:
        # With the fault of every check from k on present at once, check k's fault is the one reported.
        for index, (name, _mutation, error, message) in enumerate(STAGES):
            with self.subTest(name):
                record: Any = _record()
                for _later, mutation, _error, _message in reversed(STAGES[index:]):
                    record = mutation(record)
                self.assert_refused(record, error, message)

    def test_stages_cover_every_distinct_error(self) -> None:
        stage_errors = {(error, _check(error, message)) for _name, _mutation, error, message in STAGES}
        rejected_errors = {(error, _check(error, message)) for _name, _mutation, error, message in REJECTED}
        self.assertEqual(set(), rejected_errors - stage_errors - VALUE_VARIANTS)

    def test_each_wrong_kind_is_a_field_error(self) -> None:
        for kind, mutation, message in WRONG_KINDS:
            with self.subTest(kind):
                self.assert_refused(mutation(_record()), RecordFieldError, message)

    def test_other_digests_keep_their_strict_sha256_rule(self) -> None:
        for value, found in INVALID_DIGESTS:
            with self.subTest(value=value):
                source = _record()
                source["review"]["adapter"]["source_hashes"] = {"src/app.py": value}
                self.assert_refused(
                    source,
                    RecordFieldError,
                    f"record.review.adapter.source_hashes.src/app.py must be {DIGEST_KIND}, found {found}",
                )
                patch = _record()
                patch["review"]["patches"] = {"src/app.py": {"sha256": value, "lines": 1}}
                self.assert_refused(
                    patch,
                    RecordFieldError,
                    f"record.review.patches.src/app.py.sha256 must be {DIGEST_KIND}, found {found}",
                )


# A Git hash where an artifact's SHA-256 hash belongs, as a field error names it.
SHORT_ARTIFACT_HASH = (
    "record.artifacts.{field} must be a lowercase hexadecimal digest of 64 characters, found a string of 40 characters"
)


class ArtifactPairValidationTests(unittest.TestCase):
    def test_written_artifact_hashes_are_full_sha256_and_the_pair_validates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            json_path = Path(temporary) / "review.json"
            markdown_path = Path(temporary) / "review.md"
            persisted = write_record_pair(json_path, markdown_path, _record())
            artifacts = persisted.get("artifacts")
            if artifacts is None:
                raise AssertionError("a written record has its artifact hashes")
            self.assertRegex(artifacts["payload_sha256"], r"\A[0-9a-f]{64}\Z")
            self.assertRegex(artifacts["markdown_sha256"], r"\A[0-9a-f]{64}\Z")
            self.assertEqual(persisted, validate_record_pair(json_path, markdown_path))

    def test_a_short_artifact_hash_is_refused_before_reading_the_report(self) -> None:
        for field in ("payload_sha256", "markdown_sha256"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                record = _full_record()
                record["artifacts"][field] = BASE_SHA
                json_path = Path(temporary) / "review.json"
                json_path.write_text(json.dumps(record), encoding="utf-8")
                with self.assertRaises(RecordError) as caught:
                    validate_record_pair(json_path, Path(temporary) / "missing.md")
                self.assertIs(RecordFieldError, type(caught.exception))
                self.assertEqual(SHORT_ARTIFACT_HASH.format(field=field), str(caught.exception))

    def test_a_short_artifact_hash_is_not_silently_replaced_when_writing(self) -> None:
        for field in ("payload_sha256", "markdown_sha256"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                record = _full_record()
                record["artifacts"][field] = BASE_SHA
                before = copy.deepcopy(record)
                json_path = Path(temporary) / "review.json"
                markdown_path = Path(temporary) / "review.md"
                json_path.write_text("existing record", encoding="utf-8")
                markdown_path.write_text("existing report", encoding="utf-8")
                with self.assertRaises(RecordError) as caught:
                    write_record_pair(json_path, markdown_path, record)
                self.assertIs(RecordFieldError, type(caught.exception))
                self.assertEqual(SHORT_ARTIFACT_HASH.format(field=field), str(caught.exception))
                self.assertEqual(before, record)
                self.assertEqual("existing record", json_path.read_text(encoding="utf-8"))
                self.assertEqual("existing report", markdown_path.read_text(encoding="utf-8"))


# validate_adapter_result, pinned the same way: every reviewer result it accepts, every fault it refuses with its
# exact error, and the order in which it detects faults. The request arguments are literal too.
REQUEST: dict[str, Any] = {"expected_repository": "owner/repo", "expected_number": 7, "expected_head_sha": HEAD_SHA}
PRIOR_ID = "v1:F001"
RESULT_PRIOR = {"finding_id": PRIOR_ID, "disposition": "still_present", "rationale": "Still."}
RESULT_COMMENT = {"comment_id": "C1", "disposition": "addressed", "rationale": "Fixed."}
# The arguments that go with the full result: the prior finding and the comment it answers.
FULL_REQUEST: dict[str, Any] = {
    "prior_ids": [PRIOR_ID],
    "comment_ids": ["C1"],
    "prior_severities": {PRIOR_ID: "MUST_FIX"},
}
PRIOR_REPEAT: dict[str, Any] = {"prior_ids": [PRIOR_ID], "prior_severities": {PRIOR_ID: "MUST_FIX"}}
ResultCase = tuple[str, Mutation, type[Exception], str, dict[str, Any]]


def _result_finding(key: str, severity: str = "MUST_FIX") -> dict[str, Any]:
    return {
        "candidate_key": key,
        "severity": severity,
        "category": "correctness",
        "path": "src/app.py",
        "line": 3,
        "body": "Zero is not handled.",
        "evidence": "divide(1, 0) raises.",
        "source": "reviewer",
    }


def _result() -> dict[str, Any]:
    """The smallest valid reviewer result: one finding, no dispositions, and none of the optional fields."""
    return {
        "protocol_version": 1,
        "repository": "owner/repo",
        "pull_number": 7,
        "head_sha": HEAD_SHA,
        "summary": "One problem.",
        "reviewer": "claude-code",
        "status": "complete",
        "findings": [_result_finding("key-a")],
        "prior_dispositions": [],
        "usage": None,
    }


def _full_result() -> dict[str, Any]:
    """A valid result using every optional field: a titled finding with analyzer coverage, a repeat of it, a repeat of
    a prior finding, the prior and comment dispositions, and usage. It goes with FULL_REQUEST."""
    result = _result()
    first = _result_finding("key-a")
    first["title"] = "Zero is not handled"
    first["analyzer"] = {"coverage": "available", "tool": "ruff", "rule": "B006"}
    second = _result_finding("key-b", "SHOULD_FIX")
    second["repeats"] = "key-a"
    third = _result_finding("key-c", "SUGGESTION")
    third["repeats"] = PRIOR_ID
    result["findings"] = [first, second, third]
    result["prior_dispositions"] = [copy.deepcopy(RESULT_PRIOR)]
    result["comment_dispositions"] = [copy.deepcopy(RESULT_COMMENT)]
    result["usage"] = {"input_tokens": 1}
    return result


RESULT_FINDING = ("findings", 0)
SECOND_FINDING = ("findings", 1)
THIRD_FINDING = ("findings", 2)
TWO_FINDINGS = _set(("findings",), [_result_finding("key-a"), _result_finding("key-b", "SHOULD_FIX")])
DISPOSITION_VALUES = ("addressed", "partially_addressed", "still_present", "superseded", "unable_to_verify")

# Results validate_adapter_result accepts: the name, the result builder, the mutation, and the extra arguments.
ACCEPTED_RESULTS: list[tuple[str, Callable[[], dict[str, Any]], Mutation, dict[str, Any]]] = [
    *(
        (f"POSIX finding path {path}", _result, _set((*RESULT_FINDING, "path"), path), {})
        for path in ("src/space name.py", "src/a:b.py", "1:notes.md", "notes:archive/x.md")
    ),
    ("minimal result", _result, _chain(), {}),
    ("every optional field", _full_result, _chain(), FULL_REQUEST),
    ("no findings", _result, _set(("findings",), []), {}),
    ("partial status", _result, _set(("status",), "partial"), {}),
    ("failed status", _result, _set(("status",), "failed"), {}),
    ("SHA-256 head", _result, _set(("head_sha",), SHA256), {"expected_head_sha": SHA256}),
    ("repository in another case", _result, _set(("repository",), "Owner/Repo"), {}),
    ("expected repository in another case", _result, _chain(), {"expected_repository": "OWNER/REPO"}),
    ("usage object", _result, _set(("usage",), {}), {}),
    ("usage absent", _result, _delete(("usage",)), {}),
    ("prior dispositions absent", _result, _delete(("prior_dispositions",)), {}),
    ("comment dispositions empty with no comments", _result, _set(("comment_dispositions",), []), {}),
    (
        "comment dispositions omitted by an older repository reviewer",
        _result,
        _chain(),
        {"comment_ids": ["C1"], "require_comment_dispositions": False},
    ),
    (
        "comment dispositions given by an older repository reviewer",
        _result,
        _set(("comment_dispositions",), [RESULT_COMMENT]),
        {"comment_ids": ["C1"], "require_comment_dispositions": False},
    ),
    (
        "partially addressed prior repeat",
        _full_result,
        _set(("prior_dispositions", 0, "disposition"), "partially_addressed"),
        FULL_REQUEST,
    ),
    (
        "repeat of an equally severe finding",
        _full_result,
        _set((*SECOND_FINDING, "severity"), "MUST_FIX"),
        FULL_REQUEST,
    ),
    (
        "every disposition value for prior findings not repeated",
        _result,
        _set(
            ("prior_dispositions",),
            [
                {"finding_id": f"v1:F00{index}", "disposition": disposition, "rationale": "Judged."}
                for index, disposition in enumerate(DISPOSITION_VALUES, start=1)
            ],
        ),
        {"prior_ids": [f"v1:F00{index}" for index in range(1, 6)]},
    ),
]


def _result_text(field: str) -> list[ResultCase]:
    path = f"adapter-result.{field}"
    return [
        (f"{field} blank", _set((field,), " "), F, _wrong(path, TEXT, " "), {}),
        (f"{field} not a string", _set((field,), 1), F, _wrong(path, TEXT, 1), {}),
        (f"{field} missing", _delete((field,)), F, _wrong(path, TEXT, None), {}),
    ]


def _result_finding_text(field: str) -> list[ResultCase]:
    path = f"adapter-result.findings[0].{field}"
    return [
        (f"finding {field} blank", _set((*RESULT_FINDING, field), " "), F, _wrong(path, TEXT, " "), {}),
        (f"finding {field} not a string", _set((*RESULT_FINDING, field), None), F, _wrong(path, TEXT, None), {}),
    ]


def _disposition_faults(label: str, key: str, item: dict[str, Any], identifier: str, ids: str) -> list[ResultCase]:
    """The faults validate_dispositions refuses, for the prior or the comment dispositions of a result."""
    field = f"{label.lower()}_dispositions"
    path = f"adapter-result.{field}"
    listed = {ids: [identifier]}

    def given(value: Any) -> Mutation:
        return _set((field,), value)

    unique = f"{label} disposition IDs must be unique strings"
    return [
        (f"{field} not a list", given({}), F, f"{path} must be an array, found an object", {}),
        (f"{field} null", given(None), F, f"{path} must be an array, found null", {}),
        (
            f"{label} disposition not an object",
            given([identifier]),
            F,
            f"{path}[0] must be an object, found string {json.dumps(identifier)}",
            {},
        ),
        (
            f"{label} disposition field missing",
            given([{key: identifier, "disposition": "addressed"}]),
            F,
            _missing(f"{path}[0]", "rationale"),
            {},
        ),
        (f"{label} disposition field unknown", given([{**item, "extra": 1}]), F, _unknown(f"{path}[0]", "extra"), {}),
        (f"{label} disposition ID not a string", given([{**item, key: 1}]), F, _wrong(f"{path}[0].{key}", TEXT, 1), {}),
        (f"{label} disposition ID duplicated", given([item, item]), R, unique, listed),
        *(
            (
                f"{label} disposition {name}",
                given([{**item, "disposition": value}]),
                F,
                _wrong(f"{path}[0].disposition", DISPOSITION_KIND, value),
                listed,
            )
            for name, value in (("unknown", "fixed"), ("unhashable", []))
        ),
        *(
            (
                f"{label} rationale {name}",
                given([{**item, "rationale": value}]),
                F,
                _wrong(f"{path}[0].rationale", TEXT, value),
                listed,
            )
            for name, value in (("blank", " "), ("not a string", None))
        ),
        (
            f"{label} disposition missing",
            given([]),
            R,
            f"{label} dispositions mismatch; missing=['{identifier}'], unknown=[]",
            listed,
        ),
        (
            f"{label} disposition for an unlisted ID",
            given([item]),
            R,
            f"{label} dispositions mismatch; missing=[], unknown=['{identifier}']",
            {},
        ),
        (
            f"{label} dispositions missing and unknown, sorted",
            given([{**item, key: "Z9"}, {**item, key: "A1"}]),
            R,
            f"{label} dispositions mismatch; missing=['{identifier}', 'x2'], unknown=['A1', 'Z9']",
            {ids: ["x2", identifier]},
        ),
    ]


def _repeat(target: Any) -> Mutation:
    """Two findings, the second SHOULD_FIX and repeating `target`."""
    return _chain(TWO_FINDINGS, _set((*SECOND_FINDING, "repeats"), target))


KEYS = "Adapter candidate keys must be unique non-empty strings"
UNSAFE_PATH = "Finding key-a.path must be a safe repository-relative path"
OPEN_RULE = "so that finding's disposition must be still_present or partially_addressed"
RESULT_SHA = "adapter-result.head_sha"

# Results validate_adapter_result refuses, from the minimal result: the exception class, the message, and the extra
# arguments.
REJECTED_RESULTS: list[ResultCase] = [
    *(
        (
            f"protocol_version {value!r}",
            _set(("protocol_version",), value),
            F,
            _wrong("adapter-result.protocol_version", "an integer", value),
            {},
        )
        for value in (True, 1.0)
    ),
    *(
        (
            f"drive-letter finding path {path}",
            _set((*RESULT_FINDING, "path"), path),
            R,
            "Finding key-a.path must be a safe repository-relative path",
            {},
        )
        for path in ("C:/x.py", "c:/x.py", "C:x.py", "C:\\x.py")
    ),
    ("result not an object", _replace([]), F, "adapter-result must be an object, found an array", {}),
    ("result null", _replace(None), F, "adapter-result must be an object, found null", {}),
    ("unknown field", _set(("extra",), 1), F, _unknown("adapter-result", "extra"), {}),
    (
        "unknown fields, sorted",
        _chain(_set(("zeta",), 1), _set(("alpha",), 1)),
        F,
        _unknown("adapter-result", "alpha", "zeta"),
        {},
    ),
    ("a record field", _set(("ledger",), []), F, _unknown("adapter-result", "ledger"), {}),
    ("protocol_version 2", _set(("protocol_version",), 2), R, "Unsupported adapter protocol version", {}),
    (
        "protocol_version string",
        _set(("protocol_version",), "1"),
        F,
        _wrong("adapter-result.protocol_version", "an integer", "1"),
        {},
    ),
    (
        "protocol_version missing",
        _delete(("protocol_version",)),
        F,
        _wrong("adapter-result.protocol_version", "an integer", None),
        {},
    ),
    ("repository malformed", _set(("repository",), "bad"), C, "Invalid repository identity: 'bad'", {}),
    ("repository missing", _delete(("repository",)), F, _wrong("adapter-result.repository", TEXT, None), {}),
    (
        "repository other",
        _set(("repository",), "owner/other"),
        R,
        "Adapter result repository does not match request",
        {},
    ),
    ("pull_number other", _set(("pull_number",), 8), R, "Adapter result pull number does not match request", {}),
    *(
        (
            f"pull_number {value!r}",
            _set(("pull_number",), value),
            F,
            _wrong("adapter-result.pull_number", "an integer of at least 1", value),
            {},
        )
        # A string, the right number as a float, and zero: a pull number is a positive integer.
        for value in ("7", 7.0, 0)
    ),
    (
        "pull_number missing",
        _delete(("pull_number",)),
        F,
        _wrong("adapter-result.pull_number", "an integer of at least 1", None),
        {},
    ),
    ("head_sha other", _set(("head_sha",), BASE_SHA), R, "Adapter result head SHA does not match request", {}),
    (
        "head_sha uppercase",
        _set(("head_sha",), "2" * 39 + "A"),
        F,
        f"{RESULT_SHA} must be {SHA_KIND}, found {NOT_HEX}",
        {},
    ),
    ("head_sha missing", _delete(("head_sha",)), F, f"{RESULT_SHA} must be {SHA_KIND}, found null", {}),
    *_result_text("summary"),
    *_result_text("reviewer"),
    *(
        (
            f"status {name}",
            mutation,
            F,
            _wrong("adapter-result.status", STATUS_KIND, value),
            {},
        )
        for name, mutation, value in (
            ("unknown", _set(("status",), "done"), "done"),
            ("unhashable", _set(("status",), []), []),
            ("missing", _delete(("status",)), None),
        )
    ),
    (
        "findings not a list",
        _set(("findings",), {}),
        F,
        "adapter-result.findings must be an array, found an object",
        {},
    ),
    ("findings missing", _delete(("findings",)), F, "adapter-result.findings must be an array, found null", {}),
    (
        "finding not an object",
        _set(("findings",), ["key-a"]),
        F,
        'adapter-result.findings[0] must be an object, found string "key-a"',
        {},
    ),
    (
        "finding field missing",
        _delete((*RESULT_FINDING, "body")),
        F,
        _missing("adapter-result.findings[0]", "body"),
        {},
    ),
    (
        "finding field unknown",
        _set((*RESULT_FINDING, "id"), "F001"),
        F,
        _unknown("adapter-result.findings[0]", "id"),
        {},
    ),
    *(
        (
            f"candidate_key {name}",
            _set((*RESULT_FINDING, "candidate_key"), value),
            F,
            _wrong("adapter-result.findings[0].candidate_key", TEXT, value),
            {},
        )
        for name, value in (("empty", ""), ("not a string", 1), ("unhashable", []))
    ),
    ("candidate_key duplicated", _chain(TWO_FINDINGS, _set((*SECOND_FINDING, "candidate_key"), "key-a")), R, KEYS, {}),
    (
        "title blank",
        _set((*RESULT_FINDING, "title"), " "),
        F,
        f"adapter-result.findings[0].title must be {TITLE_KIND}, found a blank string",
        {},
    ),
    (
        "title too long",
        _set((*RESULT_FINDING, "title"), "x" * 121),
        F,
        f"adapter-result.findings[0].title must be {TITLE_KIND}, found a string of 121 characters",
        {},
    ),
    (
        "analyzer empty",
        _set((*RESULT_FINDING, "analyzer"), {}),
        F,
        f"adapter-result.findings[0].analyzer {ANALYZER_MISSING}",
        {},
    ),
    *(
        (
            f"severity {name}",
            _set((*RESULT_FINDING, "severity"), value),
            F,
            _wrong("adapter-result.findings[0].severity", SEVERITY_KIND, value),
            {},
        )
        for name, value in (("unknown", "LOW"), ("unhashable", []))
    ),
    *[case for field in ("category", "path", "body", "evidence", "source") for case in _result_finding_text(field)],
    *[
        (f"finding path {path!r}", _set((*RESULT_FINDING, "path"), path), R, UNSAFE_PATH, {})
        for path in ("/a.py", "\\a.py", "a\\b.py", "../a.py", "a/../b.py")
    ],
    *(
        (
            f"line {name}",
            _set((*RESULT_FINDING, "line"), value),
            F,
            _wrong("adapter-result.findings[0].line", POSITIVE, value),
            {},
        )
        for name, value in (("true", True), ("zero", 0), ("string", "3"), ("float", 1.0))
    ),
    (
        "first bad finding wins",
        _chain(TWO_FINDINGS, _set((*SECOND_FINDING, "line"), 0), _set((*RESULT_FINDING, "body"), "")),
        F,
        "adapter-result.findings[0].body must be a non-empty string, found an empty string",
        {},
    ),
    *_disposition_faults("Prior", "finding_id", RESULT_PRIOR, PRIOR_ID, "prior_ids"),
    *_disposition_faults("Comment", "comment_id", RESULT_COMMENT, "C1", "comment_ids"),
    (
        "comment dispositions required",
        _chain(),
        R,
        "Comment dispositions mismatch; missing=['C1'], unknown=[]",
        {"comment_ids": ["C1"]},
    ),
    (
        "partial comment dispositions from an older repository reviewer",
        _set(("comment_dispositions",), [RESULT_COMMENT]),
        R,
        "Comment dispositions mismatch; missing=['C2'], unknown=[]",
        {"comment_ids": ["C1", "C2"], "require_comment_dispositions": False},
    ),
    (
        "prior dispositions before comment dispositions",
        _set(("comment_dispositions",), {}),
        R,
        f"Prior dispositions mismatch; missing=['{PRIOR_ID}'], unknown=[]",
        {"prior_ids": [PRIOR_ID]},
    ),
    *(
        (
            f"repeats {name}",
            _repeat(value),
            F,
            _wrong("adapter-result.findings[1].repeats", TEXT, value),
            {},
        )
        for name, value in (("empty", ""), ("not a string", 1))
    ),
    ("repeats itself", _repeat("key-b"), R, "Finding key-b cannot repeat itself", {}),
    (
        "repeats an ambiguous target",
        _chain(
            _repeat(PRIOR_ID),
            _set((*RESULT_FINDING, "candidate_key"), PRIOR_ID),
            _set(("prior_dispositions",), [RESULT_PRIOR]),
        ),
        R,
        f"Finding key-b.repeats is ambiguous: {PRIOR_ID} is a candidate key and a prior finding ID",
        PRIOR_REPEAT,
    ),
    (
        "repeats a repeat",
        _chain(
            _set(("findings",), [_result_finding("key-a"), _result_finding("key-b"), _result_finding("key-c")]),
            _set((*SECOND_FINDING, "repeats"), "key-a"),
            _set((*THIRD_FINDING, "repeats"), "key-b"),
        ),
        R,
        "Finding key-c repeats a repeat: link it to what key-b repeats instead",
        {},
    ),
    (
        "repeats a prior finding judged addressed",
        _chain(_repeat(PRIOR_ID), _set(("prior_dispositions",), [{**RESULT_PRIOR, "disposition": "addressed"}])),
        R,
        f"Finding key-b repeats prior finding {PRIOR_ID}, {OPEN_RULE}",
        PRIOR_REPEAT,
    ),
    (
        "repeats a prior finding of unknown severity",
        _chain(_repeat(PRIOR_ID), _set(("prior_dispositions",), [RESULT_PRIOR])),
        R,
        f"Finding key-b repeats prior finding {PRIOR_ID}, whose severity is unknown",
        {"prior_ids": [PRIOR_ID]},
    ),
    (
        "repeats a prior finding of invalid severity",
        _chain(_repeat(PRIOR_ID), _set(("prior_dispositions",), [RESULT_PRIOR])),
        R,
        f"Finding key-b repeats prior finding {PRIOR_ID}, whose severity is unknown",
        {"prior_ids": [PRIOR_ID], "prior_severities": {PRIOR_ID: "LOW"}},
    ),
    ("repeats an unknown finding", _repeat("key-z"), R, "Finding key-b repeats an unknown finding: key-z", {}),
    (
        "repeats a prior severity with no prior ID",
        _repeat(PRIOR_ID),
        R,
        f"Finding key-b repeats an unknown finding: {PRIOR_ID}",
        {"prior_severities": {PRIOR_ID: "MUST_FIX"}},
    ),
    (
        "repeats a less severe finding",
        _chain(_repeat("key-a"), _set((*RESULT_FINDING, "severity"), "SUGGESTION")),
        R,
        "Finding key-b repeats a less severe finding: key-a",
        {},
    ),
    (
        "repeats a less severe prior finding",
        _chain(_repeat(PRIOR_ID), _set(("prior_dispositions",), [RESULT_PRIOR])),
        R,
        f"Finding key-b repeats a less severe finding: {PRIOR_ID}",
        {"prior_ids": [PRIOR_ID], "prior_severities": {PRIOR_ID: "SUGGESTION"}},
    ),
    ("usage a list", _set(("usage",), []), F, "adapter-result.usage must be an object or null, found an array", {}),
    (
        "usage a string",
        _set(("usage",), "1"),
        F,
        'adapter-result.usage must be an object or null, found string "1"',
        {},
    ),
]

# One fault per check, in the order validate_adapter_result makes its checks, under the default arguments. Each
# touches its own field or replaces what a check before it would read, so any suffix can be applied at once.
RESULT_STAGES: list[tuple[str, Mutation, type[Exception], str]] = [
    ("object", _replace([]), F, "adapter-result must be an object, found an array"),
    ("unknown fields", _set(("extra",), 1), F, _unknown("adapter-result", "extra")),
    (
        "protocol kind",
        _set(("protocol_version",), "1"),
        F,
        _wrong("adapter-result.protocol_version", "an integer", "1"),
    ),
    ("protocol", _set(("protocol_version",), 2), R, "Unsupported adapter protocol version"),
    ("repository kind", _set(("repository",), None), F, _wrong("adapter-result.repository", TEXT, None)),
    ("repository identity", _set(("repository",), "bad"), C, "Invalid repository identity: 'bad'"),
    ("repository", _set(("repository",), "owner/other"), R, "Adapter result repository does not match request"),
    (
        "pull number kind",
        _set(("pull_number",), "7"),
        F,
        _wrong("adapter-result.pull_number", "an integer of at least 1", "7"),
    ),
    ("pull number", _set(("pull_number",), 8), R, "Adapter result pull number does not match request"),
    ("head SHA kind", _set(("head_sha",), "2" * 39 + "A"), F, f"{RESULT_SHA} must be {SHA_KIND}, found {NOT_HEX}"),
    ("head SHA", _set(("head_sha",), BASE_SHA), R, "Adapter result head SHA does not match request"),
    ("summary", _set(("summary",), ""), F, _wrong("adapter-result.summary", TEXT, "")),
    ("reviewer", _set(("reviewer",), ""), F, _wrong("adapter-result.reviewer", TEXT, "")),
    ("status", _set(("status",), "done"), F, _wrong("adapter-result.status", STATUS_KIND, "done")),
    ("findings", _set(("findings",), {}), F, "adapter-result.findings must be an array, found an object"),
    (
        "finding object",
        _set(RESULT_FINDING, "key-a"),
        F,
        'adapter-result.findings[0] must be an object, found string "key-a"',
    ),
    ("finding fields", _set((*RESULT_FINDING, "extra"), 1), F, _unknown("adapter-result.findings[0]", "extra")),
    (
        "candidate key",
        _set((*RESULT_FINDING, "candidate_key"), ""),
        F,
        _wrong("adapter-result.findings[0].candidate_key", TEXT, ""),
    ),
    ("candidate key duplicated", _chain(TWO_FINDINGS, _set((*SECOND_FINDING, "candidate_key"), "key-a")), R, KEYS),
    (
        "title",
        _set((*RESULT_FINDING, "title"), ""),
        F,
        f"adapter-result.findings[0].title must be {TITLE_KIND}, found an empty string",
    ),
    ("analyzer", _set((*RESULT_FINDING, "analyzer"), {}), F, f"adapter-result.findings[0].analyzer {ANALYZER_MISSING}"),
    (
        "severity",
        _set((*RESULT_FINDING, "severity"), "LOW"),
        F,
        _wrong("adapter-result.findings[0].severity", SEVERITY_KIND, "LOW"),
    ),
    *[
        (
            f"finding {field}",
            _set((*RESULT_FINDING, field), ""),
            F,
            _wrong(f"adapter-result.findings[0].{field}", TEXT, ""),
        )
        for field in ("category", "path", "body", "evidence", "source")
    ],
    ("finding path unsafe", _set((*RESULT_FINDING, "path"), "/a.py"), R, UNSAFE_PATH),
    ("finding line", _set((*RESULT_FINDING, "line"), 0), F, _wrong("adapter-result.findings[0].line", POSITIVE, 0)),
    (
        "prior dispositions",
        _set(("prior_dispositions",), {}),
        F,
        "adapter-result.prior_dispositions must be an array, found an object",
    ),
    (
        "prior fields",
        _set(("prior_dispositions",), [[]]),
        F,
        "adapter-result.prior_dispositions[0] must be an object, found an array",
    ),
    (
        "prior ID kind",
        _set(("prior_dispositions",), [{**RESULT_PRIOR, "finding_id": 1}]),
        F,
        _wrong("adapter-result.prior_dispositions[0].finding_id", TEXT, 1),
    ),
    (
        "prior IDs",
        _set(("prior_dispositions",), [RESULT_PRIOR, RESULT_PRIOR]),
        R,
        "Prior disposition IDs must be unique strings",
    ),
    (
        "prior disposition",
        _set(("prior_dispositions",), [{**RESULT_PRIOR, "disposition": "fixed"}]),
        F,
        _wrong("adapter-result.prior_dispositions[0].disposition", DISPOSITION_KIND, "fixed"),
    ),
    (
        "prior rationale",
        _set(("prior_dispositions",), [{**RESULT_PRIOR, "rationale": ""}]),
        F,
        _wrong("adapter-result.prior_dispositions[0].rationale", TEXT, ""),
    ),
    (
        "prior mismatch",
        _set(("prior_dispositions",), [RESULT_PRIOR]),
        R,
        f"Prior dispositions mismatch; missing=[], unknown=['{PRIOR_ID}']",
    ),
    (
        "comment dispositions",
        _set(("comment_dispositions",), {}),
        F,
        "adapter-result.comment_dispositions must be an array, found an object",
    ),
    (
        "comment fields",
        _set(("comment_dispositions",), [[]]),
        F,
        "adapter-result.comment_dispositions[0] must be an object, found an array",
    ),
    (
        "comment ID kind",
        _set(("comment_dispositions",), [{**RESULT_COMMENT, "comment_id": 1}]),
        F,
        _wrong("adapter-result.comment_dispositions[0].comment_id", TEXT, 1),
    ),
    (
        "comment IDs",
        _set(("comment_dispositions",), [RESULT_COMMENT, RESULT_COMMENT]),
        R,
        "Comment disposition IDs must be unique strings",
    ),
    (
        "comment disposition",
        _set(("comment_dispositions",), [{**RESULT_COMMENT, "disposition": "fixed"}]),
        F,
        _wrong("adapter-result.comment_dispositions[0].disposition", DISPOSITION_KIND, "fixed"),
    ),
    (
        "comment rationale",
        _set(("comment_dispositions",), [{**RESULT_COMMENT, "rationale": ""}]),
        F,
        _wrong("adapter-result.comment_dispositions[0].rationale", TEXT, ""),
    ),
    (
        "comment mismatch",
        _set(("comment_dispositions",), [RESULT_COMMENT]),
        R,
        "Comment dispositions mismatch; missing=[], unknown=['C1']",
    ),
    ("repeats kind", _set((*RESULT_FINDING, "repeats"), 1), F, _wrong("adapter-result.findings[0].repeats", TEXT, 1)),
    ("repeats", _set((*RESULT_FINDING, "repeats"), "key-z"), R, "Finding key-a repeats an unknown finding: key-z"),
    ("usage", _set(("usage",), []), F, "adapter-result.usage must be an object or null, found an array"),
]

# A value of the wrong kind for each kind validate_adapter_result reads, refused as a field error; the messages are
# written out whole, independently of the reader and the helpers.
WRONG_RESULT_KINDS: list[tuple[str, Mutation, str]] = [
    (
        "string",
        _set(("summary",), ["One problem."]),
        "adapter-result.summary must be a non-empty string, found an array",
    ),
    (
        "integer",
        _set(("pull_number",), 7.0),
        "adapter-result.pull_number must be an integer of at least 1, found number 7.0",
    ),
    ("object", _set(RESULT_FINDING, None), "adapter-result.findings[0] must be an object, found null"),
    ("array", _set(("findings",), "key-a"), 'adapter-result.findings must be an array, found string "key-a"'),
    (
        "enumerated value",
        _set(("status",), 1),
        'adapter-result.status must be one of "complete", "partial", "failed", found integer 1',
    ),
    (
        "hash",
        _set(("head_sha",), 2),
        "adapter-result.head_sha must be a lowercase hexadecimal digest of 40 or 64 characters, found integer 2",
    ),
    (
        "positive integer",
        _set((*RESULT_FINDING, "line"), 3.0),
        "adapter-result.findings[0].line must be an integer of at least 1, found number 3.0",
    ),
    (
        "object or null",
        _set(("usage",), True),
        "adapter-result.usage must be an object or null, found boolean true",
    ),
]


class AdapterResultValidationTests(unittest.TestCase):
    def assert_refused(self, result: Any, error: type[Exception], message: str, arguments: dict[str, Any]) -> None:
        with self.assertRaises(Exception) as caught:
            validate_adapter_result(result, **{**REQUEST, **arguments})
        self.assertIs(error, type(caught.exception))
        self.assertEqual(message, str(caught.exception))

    def test_accepted_results_are_returned_unchanged(self) -> None:
        for name, build, mutation, arguments in ACCEPTED_RESULTS:
            with self.subTest(name):
                result = mutation(build())
                before = copy.deepcopy(result)
                self.assertIs(result, validate_adapter_result(result, **{**REQUEST, **arguments}))
                self.assertEqual(before, result)

    def test_prior_ids_may_be_a_one_shot_iterator(self) -> None:
        # The IDs are read twice, for the dispositions and for the repeats, so a generator must be read only once.
        result = _full_result()
        arguments = {**FULL_REQUEST, "prior_ids": (identifier for identifier in [PRIOR_ID])}
        self.assertIs(result, validate_adapter_result(result, **{**REQUEST, **arguments}))

    def test_each_fault_is_refused_with_its_error(self) -> None:
        for name, mutation, error, message, arguments in REJECTED_RESULTS:
            with self.subTest(name):
                self.assert_refused(mutation(_result()), error, message, arguments)

    def test_each_stage_is_refused_alone(self) -> None:
        for name, mutation, error, message in RESULT_STAGES:
            with self.subTest(name):
                self.assert_refused(mutation(_result()), error, message, {})

    def test_faults_are_detected_in_order(self) -> None:
        # With the fault of every check from k on present at once, check k's fault is the one reported.
        for index, (name, _mutation, error, message) in enumerate(RESULT_STAGES):
            with self.subTest(name):
                result: Any = _result()
                for _later, mutation, _error, _message in reversed(RESULT_STAGES[index:]):
                    result = mutation(result)
                self.assert_refused(result, error, message, {})

    def test_each_wrong_kind_is_a_field_error(self) -> None:
        for kind, mutation, message in WRONG_RESULT_KINDS:
            with self.subTest(kind):
                self.assert_refused(mutation(_result()), RecordFieldError, message, {})

    def test_stages_cover_every_check(self) -> None:
        # Every raise site of validate_adapter_result has a stage. The checks inside validate_dispositions and
        # _validate_result_repeats, which the split does not touch, are placed by their stages above.
        stage_errors = {(error, _check(error, message)) for _name, _mutation, error, message in RESULT_STAGES}
        own_errors = {
            (error, _check(error, message))
            for _name, _mutation, error, message, arguments in REJECTED_RESULTS
            if not arguments and not message.startswith(("Finding key-b", "Finding key-c"))
        }
        self.assertEqual(set(), own_errors - stage_errors - RESULT_VARIANTS)


# Refusals another stage already places in the order: the same check of another finding.
RESULT_VARIANTS = {
    (F, "adapter-result.findings[1].repeats must be a non-empty string"),
}


# _validate_ledger, pinned the same way: the ledgers it accepts as consistent with the review's findings and prior
# dispositions, every inconsistency it refuses with its exact message, and the order in which it detects them.
# validate_record calls it after the findings and prior dispositions have passed their own checks, so the records
# here hold only those parts, already valid, with the ledger; `review` carries the version and mode it is called with.
def _entry(
    version: int,
    identifier: str,
    severity: str = "MUST_FIX",
    *,
    state: str = "open",
    judged_in: int | None = None,
    dispositions: tuple[tuple[int, str], ...] = (),
    repeats: tuple[tuple[int, str], ...] = (),
) -> dict[str, Any]:
    return {
        "version": version,
        "id": identifier,
        "severity": severity,
        "category": "correctness",
        "state": state,
        "judged_in": version if judged_in is None else judged_in,
        "dispositions": [{"version": number, "disposition": value} for number, value in dispositions],
        "repeats": [{"version": number, "id": value} for number, value in repeats],
    }


def _repeating(identifier: str, severity: str, version: int, target: str) -> dict[str, Any]:
    finding = _finding(identifier, severity)
    finding["repeats"] = {"version": version, "id": target}
    return finding


V1_ENTRY = _entry(1, "F001", judged_in=2, dispositions=((2, "still_present"),), repeats=((2, "F002"),))
V2_ENTRY = _entry(2, "F001", judged_in=2, repeats=((2, "F003"),))
LEDGER_PRIOR = {"finding_id": "v1:F001", "disposition": "still_present", "rationale": "Still."}


def _ledger_record() -> dict[str, Any]:
    """A consistent re-review, version 2: F001 is new, F002 repeats v1:F001 (judged still present), and F003 repeats
    F001 of this review."""
    return {
        "review": {"version": 2, "mode": "re-review"},
        "findings": [
            _finding("F001", "MUST_FIX"),
            _repeating("F002", "SHOULD_FIX", 1, "F001"),
            _repeating("F003", "SUGGESTION", 2, "F001"),
        ],
        "prior_dispositions": [copy.deepcopy(LEDGER_PRIOR)],
        "ledger": copy.deepcopy([V1_ENTRY, V2_ENTRY]),
    }


def _initial_ledger_record() -> dict[str, Any]:
    """A consistent initial review, version 1, with one finding and its fresh ledger."""
    return {
        "review": {"version": 1, "mode": "initial"},
        "findings": [_finding("F001", "MUST_FIX")],
        "prior_dispositions": [],
        "ledger": [_entry(1, "F001")],
    }


def _validate(record: dict[str, Any]) -> None:
    _validate_ledger(record, record["review"]["version"], record["review"]["mode"])


def _target(path: tuple[Key, ...]) -> Any:
    def find(record: Any) -> Any:
        target = record
        for key in path:
            target = target[key]
        return target

    return find


def _insert(path: tuple[Key, ...], index: int, value: Any) -> Mutation:
    def apply(record: Any) -> Any:
        _target(path)(record).insert(index, copy.deepcopy(value))
        return record

    return apply


def _append(path: tuple[Key, ...], value: Any) -> Mutation:
    def apply(record: Any) -> Any:
        _target(path)(record).append(copy.deepcopy(value))
        return record

    return apply


def _remove(path: tuple[Key, ...], value: Any) -> Mutation:
    def apply(record: Any) -> Any:
        _target(path)(record).remove(value)
        return record

    return apply


def _duplicate(path: tuple[Key, ...], source: int, index: int) -> Mutation:
    """Insert a copy of the item at `source`, as it is when the mutation runs, at `index`."""

    def apply(record: Any) -> Any:
        items = _target(path)(record)
        items.insert(index, copy.deepcopy(items[source]))
        return record

    return apply


ENTRY = ("ledger", 0)
SECOND_ENTRY = ("ledger", 1)
EARLIER_REPEAT = ("findings", 1)
SAME_REPEAT = ("findings", 2)
JUDGED = ("ledger", 0, "dispositions", 0, "disposition")
GIVEN = ("prior_dispositions", 0, "disposition")
FIELDS = "Review ledger entry fields are malformed"
DISPOSITIONS_MALFORMED = "Review ledger entry v1:F001 dispositions are malformed"
REPEATS_MALFORMED = "Review ledger entry v1:F001 repeats are malformed"
ENTRY_MISMATCH = "Review ledger entry for F001 does not match the finding"
VERSION_ENTRIES = "Review ledger entries of this version must be its findings without repeats"
JUDGMENTS = "Review prior dispositions do not match the ledger"
LINK_MALFORMED = "Review finding F002.repeats is malformed"
NOT_OPEN = "Review finding F002 repeats v1:F001, so its disposition must be still_present or partially_addressed"
LISTED = "Review ledger repeats of this version must be its findings with repeats"


def _judged(disposition: str) -> Mutation:
    """Judge v1:F001 `disposition` in this review, in the prior dispositions and the ledger alike."""
    return _chain(_set(GIVEN, disposition), _set(JUDGED, disposition))


def _without_ledger(record: dict[str, Any]) -> dict[str, Any]:
    """The re-review's new finding and prior disposition alone, as a record written before the ledger existed."""
    record["findings"] = [_finding("F001", "MUST_FIX")]
    del record["ledger"]
    return record


# Ledgers _validate_ledger accepts: the name, the record builder, and the mutation.
ACCEPTED_LEDGERS: list[tuple[str, Callable[[], dict[str, Any]], Mutation]] = [
    ("re-review", _ledger_record, _chain()),
    ("initial review", _initial_ledger_record, _chain()),
    ("initial review without a ledger", _initial_ledger_record, _delete(("ledger",))),
    ("initial review with a null ledger", _initial_ledger_record, _set(("ledger",), None)),
    ("no findings and an empty ledger", _initial_ledger_record, _chain(_set(("findings",), []), _set(("ledger",), []))),
    ("re-review without a ledger or repeats", _ledger_record, _without_ledger),
    ("prior finding still partially present", _ledger_record, _judged("partially_addressed")),
    ("repeat of an equally severe earlier finding", _ledger_record, _set((*EARLIER_REPEAT, "severity"), "MUST_FIX")),
    (
        "repeat of an equally severe finding of this review",
        _ledger_record,
        _set((*SAME_REPEAT, "severity"), "MUST_FIX"),
    ),
    (
        "prior finding closed and not repeated",
        _ledger_record,
        _chain(
            _set(("findings",), [_finding("F001", "MUST_FIX")]),
            _set(GIVEN, "addressed"),
            _set(
                ("ledger",),
                [_entry(1, "F001", state="closed", judged_in=2, dispositions=((2, "addressed"),)), _entry(2, "F001")],
            ),
        ),
    ),
    (
        "prior finding unverified and not repeated",
        _ledger_record,
        _chain(
            _set(("findings",), [_finding("F001", "MUST_FIX")]),
            _set(GIVEN, "unable_to_verify"),
            _set(
                ("ledger",),
                [
                    _entry(1, "F001", state="unverified", judged_in=2, dispositions=((2, "unable_to_verify"),)),
                    _entry(2, "F001"),
                ],
            ),
        ),
    ),
    (
        # A repeat may not be more severe than what it repeats, so a finding restating a still-present entry at a
        # higher severity is recorded unlinked, as a new finding with its own entry beside the open one.
        "finding more severe than a still-present entry, recorded as new",
        _ledger_record,
        _chain(
            _set(("findings",), [_finding("F001", "MUST_FIX")]),
            _set(
                ("ledger",),
                [
                    _entry(1, "F001", "SHOULD_FIX", judged_in=2, dispositions=((2, "still_present"),)),
                    _entry(2, "F001"),
                ],
            ),
        ),
    ),
    ("earlier entry not judged in this review", _ledger_record, _insert(("ledger",), 1, _entry(1, "F002"))),
    (
        "entries ordered by the number in the finding ID",
        _ledger_record,
        _chain(_insert(("ledger",), 1, _entry(1, "F999")), _insert(("ledger",), 2, _entry(1, "F1000"))),
    ),
    (
        "an earlier review's repeat in an entry",
        _ledger_record,
        _insert((*ENTRY, "repeats"), 0, {"version": 1, "id": "F002"}),
    ),
]


LedgerCase = tuple[str, Mutation, type[Exception], str]
ENTRY_PATH = "record.ledger[0]"
LINK_PATH = "record.findings[1].repeats"


def _entry_field(path: tuple[Key, ...], value: Any, error: type[Exception], message: str) -> LedgerCase:
    return (f"entry {'.'.join(map(str, path))} {value!r}", _set((*ENTRY, *path), value), error, message)


def _entry_kind(field: str, expected: str, value: Any) -> LedgerCase:
    """An entry field of the wrong kind, refused as a field error at its path."""
    return _entry_field((field,), value, F, _wrong(f"{ENTRY_PATH}.{field}", expected, value))


# Ledgers _validate_ledger refuses, from the re-review record unless a builder is named in the mutation, with the
# exception class and message it raises.
REJECTED_LEDGERS: list[LedgerCase] = [
    ("repeats without a ledger", _delete(("ledger",)), R, "Review finding repeats need a ledger"),
    ("repeats with a null ledger", _set(("ledger",), None), R, "Review finding repeats need a ledger"),
    ("ledger not a list", _set(("ledger",), {}), F, "record.ledger must be an array, found an object"),
    ("entry not an object", _set(ENTRY, "v1:F001"), F, f'{ENTRY_PATH} must be an object, found string "v1:F001"'),
    ("entry field missing", _delete((*ENTRY, "judged_in")), F, _missing(ENTRY_PATH, "judged_in")),
    _entry_field(("extra",), 1, F, _unknown(ENTRY_PATH, "extra")),
    _entry_kind("version", "an integer", True),
    _entry_field(("version",), 0, R, FIELDS),
    _entry_kind("version", "an integer", "1"),
    _entry_field(("id",), "F1", R, FIELDS),
    _entry_kind("id", TEXT, 1),
    _entry_field(("version",), 3, R, "Review ledger entry v3:F001 is after this review"),
    _entry_kind("severity", SEVERITY_KIND, "LOW"),
    _entry_kind("severity", SEVERITY_KIND, []),
    _entry_kind("category", TEXT, " "),
    _entry_kind("category", TEXT, 1),
    _entry_field(("dispositions",), "x", F, f'{ENTRY_PATH}.dispositions must be an array, found string "x"'),
    _entry_field(("dispositions",), ["x"], F, f'{ENTRY_PATH}.dispositions[0] must be an object, found string "x"'),
    _entry_field(("dispositions",), [{"version": 2}], F, _missing(f"{ENTRY_PATH}.dispositions[0]", "disposition")),
    _entry_field(
        ("dispositions",),
        [{"version": True, "disposition": "still_present"}],
        F,
        _wrong(f"{ENTRY_PATH}.dispositions[0].version", COUNT, True),
    ),
    *(
        _entry_field(
            ("dispositions",),
            [{"version": 2, "disposition": value}],
            F,
            _wrong(f"{ENTRY_PATH}.dispositions[0].disposition", DISPOSITION_KIND, value),
        )
        for value in _values("fixed", [])
    ),
    _entry_field(
        ("dispositions",),
        [{"version": 2, "disposition": "still_present"}, {"version": 2, "disposition": "still_present"}],
        R,
        DISPOSITIONS_MALFORMED,
    ),
    _entry_field(("dispositions",), [{"version": 1, "disposition": "still_present"}], R, DISPOSITIONS_MALFORMED),
    _entry_field(("dispositions",), [{"version": 3, "disposition": "still_present"}], R, DISPOSITIONS_MALFORMED),
    (
        "entry dispositions out of order",
        _chain(
            _set(("review", "version"), 3),
            _set(
                (*ENTRY, "dispositions"),
                [{"version": 3, "disposition": "still_present"}, {"version": 2, "disposition": "still_present"}],
            ),
        ),
        R,
        DISPOSITIONS_MALFORMED,
    ),
    _entry_field(("repeats",), "x", F, f'{ENTRY_PATH}.repeats must be an array, found string "x"'),
    _entry_field(("repeats",), [{"version": 2}], F, _missing(f"{ENTRY_PATH}.repeats[0]", "id")),
    _entry_field(("repeats",), [{"version": 3, "id": "F002"}], R, REPEATS_MALFORMED),
    _entry_field(("repeats",), [{"version": 2, "id": "F2"}], R, REPEATS_MALFORMED),
    _entry_field(("repeats",), [{"version": 2, "id": "F002"}, {"version": 2, "id": "F002"}], R, REPEATS_MALFORMED),
    (
        "entry repeat before the entry",
        _set((*SECOND_ENTRY, "repeats"), [{"version": 1, "id": "F003"}]),
        R,
        "Review ledger entry v2:F001 repeats are malformed",
    ),
    _entry_field(("state",), "closed", R, "Review ledger entry v1:F001 state must be open"),
    (
        "entry state of a closed finding",
        _chain(
            _set((*ENTRY, "dispositions"), [{"version": 2, "disposition": "addressed"}]), _set((*ENTRY, "repeats"), [])
        ),
        R,
        "Review ledger entry v1:F001 state must be closed",
    ),
    _entry_field(("judged_in",), 1, R, "Review ledger entry v1:F001 judged_in must be 2"),
    ("entry duplicated", _duplicate(("ledger",), 0, 1), R, "Review ledger entries must be unique"),
    (
        "entries out of order",
        _set(("ledger",), [V2_ENTRY, V1_ENTRY]),
        R,
        "Review ledger entries must be ordered by version and finding ID",
    ),
    (
        "entries ordered by the text of the finding ID",
        _chain(_insert(("ledger",), 1, _entry(1, "F1000")), _insert(("ledger",), 2, _entry(1, "F999"))),
        R,
        "Review ledger entries must be ordered by version and finding ID",
    ),
    (
        "initial review with an earlier entry",
        _set(("review", "mode"), "initial"),
        R,
        "An initial review starts a fresh ledger",
    ),
    ("entry of a new finding missing", _delete(SECOND_ENTRY), R, "Review ledger entry for F001 is missing"),
    ("entry severity differs from the finding", _set((*SECOND_ENTRY, "severity"), "SHOULD_FIX"), R, ENTRY_MISMATCH),
    ("entry category differs from the finding", _set((*SECOND_ENTRY, "category"), "style"), R, ENTRY_MISMATCH),
    (
        "entry of this version for a repeat",
        _append(("ledger",), _entry(2, "F002", "SHOULD_FIX")),
        R,
        VERSION_ENTRIES,
    ),
    ("entry of this version without a finding", _append(("ledger",), _entry(2, "F009")), R, VERSION_ENTRIES),
    ("prior disposition differs from the ledger", _set(GIVEN, "partially_addressed"), R, JUDGMENTS),
    (
        "prior disposition the ledger lacks",
        _append(("prior_dispositions",), {"finding_id": "v1:F002", "disposition": "addressed", "rationale": "Done."}),
        R,
        JUDGMENTS,
    ),
    ("ledger judgment without a prior disposition", _set(("prior_dispositions",), []), R, JUDGMENTS),
    ("prior disposition without a ledger judgment", _set((*ENTRY, "dispositions"), []), R, JUDGMENTS),
    *[
        (f"repeats {target!r}", _set((*EARLIER_REPEAT, "repeats"), target), F, message)
        for target, message in (
            ("v1:F001", f'{LINK_PATH} must be an object, found string "v1:F001"'),
            ({"version": 1}, _missing(LINK_PATH, "id")),
            ({"version": 1, "id": "F001", "extra": 1}, _unknown(LINK_PATH, "extra")),
            ({"version": True, "id": "F001"}, _wrong(f"{LINK_PATH}.version", "an integer", True)),
        )
    ],
    *[
        (f"repeats {target!r}", _set((*EARLIER_REPEAT, "repeats"), target), R, LINK_MALFORMED)
        for target in (
            {"version": 0, "id": "F001"},
            {"version": 3, "id": "F001"},
            {"version": 1, "id": "X001"},
        )
    ],
    (
        "repeats itself",
        _set((*SAME_REPEAT, "repeats"), {"version": 2, "id": "F003"}),
        R,
        "Review finding F003 cannot repeat itself",
    ),
    (
        "repeats an unknown finding of this review",
        _set((*SAME_REPEAT, "repeats"), {"version": 2, "id": "F008"}),
        R,
        "Review finding F003 repeats an unknown finding: F008",
    ),
    (
        "repeats a repeat",
        _set((*SAME_REPEAT, "repeats"), {"version": 2, "id": "F002"}),
        R,
        "Review finding F003 repeats a repeat: F002",
    ),
    (
        "repeats an unknown earlier finding",
        _set((*EARLIER_REPEAT, "repeats"), {"version": 1, "id": "F005"}),
        R,
        "Review finding F002 repeats an unknown finding: v1:F005",
    ),
    *[
        (f"repeats a prior finding judged {value}", _judged(value), R, NOT_OPEN)
        for value in ("addressed", "superseded", "unable_to_verify")
    ],
    (
        "repeats a prior finding not judged in this review",
        _chain(_set(("prior_dispositions",), []), _set((*ENTRY, "dispositions"), [])),
        R,
        NOT_OPEN,
    ),
    (
        "repeats a less severe earlier finding",
        _set((*ENTRY, "severity"), "SUGGESTION"),
        R,
        "Review finding F002 repeats a less severe finding",
    ),
    (
        "repeats a less severe finding of this review",
        _chain(
            _set(("findings", 0, "severity"), "SHOULD_FIX"),
            _set((*SECOND_ENTRY, "severity"), "SHOULD_FIX"),
            _set((*SAME_REPEAT, "severity"), "MUST_FIX"),
        ),
        R,
        "Review finding F003 repeats a less severe finding",
    ),
    (
        "repeat missing from an earlier entry",
        _remove((*ENTRY, "repeats"), {"version": 2, "id": "F002"}),
        R,
        "Review finding F002 is not in its target's ledger entry",
    ),
    (
        "repeat missing from an entry of this review",
        _remove((*SECOND_ENTRY, "repeats"), {"version": 2, "id": "F003"}),
        R,
        "Review finding F003 is not in its target's ledger entry",
    ),
    (
        "repeat listed without a finding",
        _append((*SECOND_ENTRY, "repeats"), {"version": 2, "id": "F006"}),
        R,
        LISTED,
    ),
    (
        "repeat listed in two entries",
        _append((*SECOND_ENTRY, "repeats"), {"version": 2, "id": "F002"}),
        R,
        LISTED,
    ),
    ("new finding listed as a repeat", _append((*ENTRY, "repeats"), {"version": 2, "id": "F001"}), R, LISTED),
    # Where a loop meets a fault in more than one item, the first item's fault is the one raised.
    (
        "duplicate entry before a malformed one",
        _set(("ledger",), [V1_ENTRY, V1_ENTRY, "x"]),
        R,
        "Review ledger entries must be unique",
    ),
    (
        "malformed entry before a duplicate",
        _set(("ledger",), [V1_ENTRY, "x", V1_ENTRY]),
        F,
        'record.ledger[1] must be an object, found string "x"',
    ),
    (
        "first new finding's mismatch before the second's missing entry",
        _chain(
            _set(("findings",), [_finding("F001", "MUST_FIX"), _finding("F002", "MUST_FIX")]),
            _set(("ledger",), [_entry(2, "F001", "SHOULD_FIX")]),
            _set(("prior_dispositions",), []),
        ),
        R,
        ENTRY_MISMATCH,
    ),
    (
        "first new finding's missing entry before the second's mismatch",
        _chain(
            _set(("findings",), [_finding("F001", "MUST_FIX"), _finding("F002", "MUST_FIX")]),
            _set(("ledger",), [_entry(2, "F002", "SHOULD_FIX")]),
            _set(("prior_dispositions",), []),
        ),
        R,
        "Review ledger entry for F001 is missing",
    ),
    (
        "first repeat's malformed link before the second's",
        _chain(_set((*EARLIER_REPEAT, "repeats"), "x"), _set((*SAME_REPEAT, "repeats"), {"version": 2, "id": "F003"})),
        F,
        f'{LINK_PATH} must be an object, found string "x"',
    ),
    (
        "first repeat's unknown target before the second's malformed link",
        _chain(_set((*EARLIER_REPEAT, "repeats"), {"version": 1, "id": "F005"}), _set((*SAME_REPEAT, "repeats"), "x")),
        R,
        "Review finding F002 repeats an unknown finding: v1:F005",
    ),
]

# One fault per check, in the order _validate_ledger makes its checks, from the re-review record. Each touches its own
# field, replaces what a check before it reads, or inserts after what the earlier stages target, so any suffix of the
# list can be applied to one record at once. The link stages share the first repeat, F002, and each sets its link.
LEDGER_STAGES: list[LedgerCase] = [
    ("no ledger", _set(("ledger",), None), R, "Review finding repeats need a ledger"),
    ("ledger array", _set(("ledger",), {}), F, "record.ledger must be an array, found an object"),
    ("entry fields", _set((*ENTRY, "extra"), 1), F, _unknown(ENTRY_PATH, "extra")),
    ("entry version kind", _set((*ENTRY, "version"), "1"), F, _wrong(f"{ENTRY_PATH}.version", "an integer", "1")),
    ("entry id kind", _set((*ENTRY, "id"), 1), F, _wrong(f"{ENTRY_PATH}.id", TEXT, 1)),
    ("entry key", _set((*ENTRY, "version"), 0), R, FIELDS),
    ("entry after the review", _set((*ENTRY, "version"), 3), R, "Review ledger entry v3:F001 is after this review"),
    ("entry severity", _set((*ENTRY, "severity"), "LOW"), F, _wrong(f"{ENTRY_PATH}.severity", SEVERITY_KIND, "LOW")),
    ("entry category", _set((*ENTRY, "category"), ""), F, _wrong(f"{ENTRY_PATH}.category", TEXT, "")),
    (
        "entry dispositions",
        _set((*ENTRY, "dispositions"), "x"),
        F,
        f'{ENTRY_PATH}.dispositions must be an array, found string "x"',
    ),
    (
        "entry disposition object",
        _set((*ENTRY, "dispositions"), ["x"]),
        F,
        f'{ENTRY_PATH}.dispositions[0] must be an object, found string "x"',
    ),
    (
        "entry disposition version",
        _set((*ENTRY, "dispositions"), [{"version": True, "disposition": "still_present"}]),
        F,
        _wrong(f"{ENTRY_PATH}.dispositions[0].version", COUNT, True),
    ),
    (
        "entry disposition value",
        _set((*ENTRY, "dispositions"), [{"version": 2, "disposition": "fixed"}]),
        F,
        _wrong(f"{ENTRY_PATH}.dispositions[0].disposition", DISPOSITION_KIND, "fixed"),
    ),
    (
        "entry disposition versions",
        _set((*ENTRY, "dispositions"), [{"version": 1, "disposition": "still_present"}]),
        R,
        DISPOSITIONS_MALFORMED,
    ),
    ("entry repeats", _set((*ENTRY, "repeats"), "x"), F, f'{ENTRY_PATH}.repeats must be an array, found string "x"'),
    (
        "entry repeat link",
        _set((*ENTRY, "repeats"), [{"version": 2}]),
        F,
        _missing(f"{ENTRY_PATH}.repeats[0]", "id"),
    ),
    ("entry repeat versions", _set((*ENTRY, "repeats"), [{"version": 3, "id": "F002"}]), R, REPEATS_MALFORMED),
    ("entry state", _set((*ENTRY, "state"), "closed"), R, "Review ledger entry v1:F001 state must be open"),
    ("entry judged_in", _set((*ENTRY, "judged_in"), 1), R, "Review ledger entry v1:F001 judged_in must be 2"),
    ("unique", _duplicate(("ledger",), 0, 1), R, "Review ledger entries must be unique"),
    (
        "order",
        _append(("ledger",), _entry(1, "F002")),
        R,
        "Review ledger entries must be ordered by version and finding ID",
    ),
    ("initial", _set(("review", "mode"), "initial"), R, "An initial review starts a fresh ledger"),
    (
        "missing entry",
        _insert(("findings",), 0, _finding("F007", "MUST_FIX")),
        R,
        "Review ledger entry for F007 is missing",
    ),
    ("entry mismatch", _set((*SECOND_ENTRY, "severity"), "SHOULD_FIX"), R, ENTRY_MISMATCH),
    ("entries of this version", _append(("ledger",), _entry(2, "F009")), R, VERSION_ENTRIES),
    ("judgments", _set(GIVEN, "partially_addressed"), R, JUDGMENTS),
    ("link object", _set((*EARLIER_REPEAT, "repeats"), "x"), F, f'{LINK_PATH} must be an object, found string "x"'),
    (
        "link version kind",
        _set((*EARLIER_REPEAT, "repeats"), {"version": True, "id": "F001"}),
        F,
        _wrong(f"{LINK_PATH}.version", "an integer", True),
    ),
    ("link malformed", _set((*EARLIER_REPEAT, "repeats"), {"version": 0, "id": "F001"}), R, LINK_MALFORMED),
    (
        "self",
        _set((*EARLIER_REPEAT, "repeats"), {"version": 2, "id": "F002"}),
        R,
        "Review finding F002 cannot repeat itself",
    ),
    (
        "unknown of this review",
        _set((*EARLIER_REPEAT, "repeats"), {"version": 2, "id": "F008"}),
        R,
        "Review finding F002 repeats an unknown finding: F008",
    ),
    (
        "repeat of a repeat",
        _set((*EARLIER_REPEAT, "repeats"), {"version": 2, "id": "F003"}),
        R,
        "Review finding F002 repeats a repeat: F003",
    ),
    (
        "unknown earlier",
        _set((*EARLIER_REPEAT, "repeats"), {"version": 1, "id": "F005"}),
        R,
        "Review finding F002 repeats an unknown finding: v1:F005",
    ),
    ("not open", _judged("addressed"), R, NOT_OPEN),
    (
        "less severe",
        _set((*ENTRY, "severity"), "SUGGESTION"),
        R,
        "Review finding F002 repeats a less severe finding",
    ),
    (
        "not in the entry",
        _remove((*SECOND_ENTRY, "repeats"), {"version": 2, "id": "F003"}),
        R,
        "Review finding F003 is not in its target's ledger entry",
    ),
    ("listed", _append((*SECOND_ENTRY, "repeats"), {"version": 2, "id": "F006"}), R, LISTED),
]

# A value of the wrong kind for each kind _validate_ledger reads, refused as a field error; the messages are written
# out whole, independently of the reader and the helpers.
WRONG_LEDGER_KINDS: list[tuple[str, Mutation, str]] = [
    ("array", _set(("ledger",), "v1:F001"), 'record.ledger must be an array, found string "v1:F001"'),
    ("object", _set(ENTRY, 1), "record.ledger[0] must be an object, found integer 1"),
    ("integer", _set((*ENTRY, "version"), 1.0), "record.ledger[0].version must be an integer, found number 1.0"),
    ("string", _set((*ENTRY, "id"), None), "record.ledger[0].id must be a non-empty string, found null"),
    (
        "enumerated severity",
        _set((*ENTRY, "severity"), "must_fix"),
        'record.ledger[0].severity must be one of "MUST_FIX", "SHOULD_FIX", "SUGGESTION", found string "must_fix"',
    ),
    (
        "enumerated state",
        _set((*ENTRY, "state"), 1),
        'record.ledger[0].state must be one of "open", "closed", "unverified", found integer 1',
    ),
    ("judged_in", _set((*ENTRY, "judged_in"), "2"), 'record.ledger[0].judged_in must be an integer, found string "2"'),
    (
        "disposition version",
        _set((*ENTRY, "dispositions"), [{"version": "2", "disposition": "still_present"}]),
        'record.ledger[0].dispositions[0].version must be an integer of at least 0, found string "2"',
    ),
    (
        "repeat ID",
        _set((*ENTRY, "repeats"), [{"version": 2, "id": 2}]),
        "record.ledger[0].repeats[0].id must be a non-empty string, found integer 2",
    ),
    (
        "link ID",
        _set((*EARLIER_REPEAT, "repeats"), {"version": 1, "id": ["F001"]}),
        "record.findings[1].repeats.id must be a non-empty string, found an array",
    ),
]

# Refusals another stage already places in the order: the same check with another value, or the second of the
# entry's two dispositions checks, which _validate_ledger_entry makes and this split does not touch.
LEDGER_VARIANTS = {
    (R, "Review ledger entry v2:F001 repeats are malformed"),
    (R, "Review ledger entry v1:F001 state must be closed"),
    (R, "Review ledger entry for F001 is missing"),
    (R, "Review finding F003 cannot repeat itself"),
    (R, "Review finding F003 repeats an unknown finding: F008"),
    (R, "Review finding F003 repeats a repeat: F002"),
    (R, "Review finding F003 repeats a less severe finding"),
    (R, "Review finding F002 is not in its target's ledger entry"),
    (F, "record.ledger[1] must be an object"),
}


class LedgerValidationTests(unittest.TestCase):
    def assert_refused(self, record: Any, error: type[Exception], message: str) -> None:
        with self.assertRaises(Exception) as caught:
            _validate(record)
        self.assertIs(error, type(caught.exception))
        self.assertEqual(message, str(caught.exception))

    def test_accepted_ledgers_leave_the_record_unchanged(self) -> None:
        for name, build, mutation in ACCEPTED_LEDGERS:
            with self.subTest(name):
                record = mutation(build())
                before = copy.deepcopy(record)
                _validate(record)
                self.assertEqual(before, record)

    def test_each_inconsistency_is_refused_with_its_error(self) -> None:
        for name, mutation, error, message in REJECTED_LEDGERS:
            with self.subTest(name):
                self.assert_refused(mutation(_ledger_record()), error, message)

    def test_each_stage_is_refused_alone(self) -> None:
        for name, mutation, error, message in LEDGER_STAGES:
            with self.subTest(name):
                self.assert_refused(mutation(_ledger_record()), error, message)

    def test_inconsistencies_are_detected_in_order(self) -> None:
        # With the fault of every check from k on present at once, check k's fault is the one reported.
        for index, (name, _mutation, error, message) in enumerate(LEDGER_STAGES):
            with self.subTest(name):
                record: Any = _ledger_record()
                for _later, mutation, _error, _message in reversed(LEDGER_STAGES[index:]):
                    record = mutation(record)
                self.assert_refused(record, error, message)

    def test_each_wrong_kind_is_a_field_error(self) -> None:
        for kind, mutation, message in WRONG_LEDGER_KINDS:
            with self.subTest(kind):
                self.assert_refused(mutation(_ledger_record()), RecordFieldError, message)

    def test_stages_cover_every_distinct_error(self) -> None:
        stage_errors = {(error, _check(error, message)) for _name, _mutation, error, message in LEDGER_STAGES}
        rejected_errors = {(error, _check(error, message)) for _name, _mutation, error, message in REJECTED_LEDGERS}
        self.assertEqual(set(), rejected_errors - stage_errors - LEDGER_VARIANTS)


class SuiteProfileReferenceTests(unittest.TestCase):
    def test_only_the_suite_scheme_names_a_suite_profile(self) -> None:
        self.assertTrue(is_suite_profile("suite:design-review"))
        for path in ("docs/suite.md", "suites:design-review", "Suite:design-review", "a/suite:design-review"):
            with self.subTest(path):
                self.assertFalse(is_suite_profile(path))

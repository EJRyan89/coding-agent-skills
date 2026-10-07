"""validate_record, pinned: every record shape it accepts, every fault it refuses with its exact error, and the order
in which it detects faults. The records are literal, so a change to what the pipeline accepts shows up here."""

from __future__ import annotations

import copy
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_config import ConfigurationError
from review_records import RecordError, validate_adapter_result, validate_record

Mutation = Callable[[Any], Any]
Key = str | int

BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40
SHA256 = "3" * 64
TITLE_RULE = "must be a single non-blank line of at most 120 characters"
ANALYZER_RULE = (
    "must be an object with exactly coverage (available, known, custom-candidate), tool, and rule; tool and rule are "
    "at most 100 characters with no whitespace, backticks, pipes, or angle brackets, and a custom-candidate rule is a "
    "lowercase kebab-case pattern name of at most 60 characters"
)
SHA_RULE = "must be a lowercase Git or SHA-256 hash"


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


def _record() -> dict[str, Any]:
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


def _full_record() -> dict[str, Any]:
    """A valid re-review using every optional part: a finding that repeats an earlier one, with its ledger."""
    record = _record()
    record["pull_request"]["head_ref"] = "fix/zero"
    record["review"].update(
        {
            "version": 2,
            "mode": "re-review",
            "counts": {"MUST_FIX": 1, "SHOULD_FIX": 1, "SUGGESTION": 0},
            "coverage": {"unavailable_sources": [], "uncovered_files": ["docs/notes.md"]},
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
    ("every optional part", _full_record, _chain()),
    ("SHA-256 commit hashes", _record, _chain(_set((*PULL, "base_sha"), SHA256), _set((*PULL, "head_sha"), SHA256))),
    ("head_ref", _record, _set((*PULL, "head_ref"), "fix/zero")),
    ("coverage without uncovered files", _record, _set((*REVIEW, "coverage"), {"unavailable_sources": []})),
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
    ("source hashes", _record, _set((*ADAPTER, "source_hashes"), {"a.md": SHA256, "dir/b.md": SHA256})),
    ("usage object", _record, _set((*ADAPTER, "usage"), {})),
    (
        "repository adapter, partial",
        _record,
        _chain(_set((*ADAPTER, "scope"), "repository"), _set((*ADAPTER, "status"), "partial")),
    ),
    ("failed adapter", _record, _set((*ADAPTER, "status"), "failed")),
    (
        "no findings",
        _record,
        _chain(_set(("findings",), []), _set((*REVIEW, "counts"), {"MUST_FIX": 0, "SHOULD_FIX": 0, "SUGGESTION": 0})),
    ),
    ("prior disposition without a ledger", _record, _set(("prior_dispositions",), [PRIOR])),
    ("artifacts", _record, _set(("artifacts",), {"payload_sha256": BASE_SHA, "markdown_sha256": SHA256})),
    ("artifacts null", _record, _set(("artifacts",), None)),
    ("comments without artifacts", _full_record, _delete(("artifacts",))),
    (
        "ledger without comments",
        _full_record,
        _chain(_delete(("github_comments",)), _delete(("comment_dispositions",))),
    ),
    # Quirks pinned as they stand, not endorsed: the version fields compare with `!=`, so true and 1.0 equal 1, and a
    # drive-letter path is not caught by the relative-path checks.
    ("schema_version true", _record, _set(("schema_version",), True)),
    ("schema_version 1.0", _record, _set(("schema_version",), 1.0)),
    ("protocol_version true", _record, _set((*ADAPTER, "protocol_version"), True)),
    ("protocol_version 1.0", _record, _set((*ADAPTER, "protocol_version"), 1.0)),
    ("drive-letter source hash path", _record, _set((*ADAPTER, "source_hashes"), {"C:/x.md": SHA256})),
    ("drive-letter finding path", _record, _set((*FINDING, "path"), "C:/x.py")),
]

R = RecordError
C = ConfigurationError


def _blank_and_non_string(path: tuple[Key, ...], message: str) -> list[tuple[str, Mutation, type[Exception], str]]:
    return [
        (f"{'.'.join(map(str, path))} blank", _set(path, " "), R, message),
        (f"{'.'.join(map(str, path))} not a string", _set(path, 1), R, message),
    ]


def _finding_field(field: str) -> list[tuple[str, Mutation, type[Exception], str]]:
    return _blank_and_non_string((*FINDING, field), f"Review finding F001.{field} is invalid")


# Records validate_record refuses, from the minimal record, with the exception class and message it raises.
REJECTED: list[tuple[str, Mutation, type[Exception], str]] = [
    ("record not an object", _replace([]), R, "Unsupported or malformed review record"),
    ("unknown top-level key", _set(("extra",), 1), R, "Unsupported or malformed review record"),
    ("schema_version 2", _set(("schema_version",), 2), R, "Unsupported or malformed review record"),
    ("schema_version missing", _delete(("schema_version",)), R, "Unsupported or malformed review record"),
    ("comments empty", _set(("github_comments",), []), R, "Review comment dispositions need the comments they answer"),
    (
        "comment dispositions alone",
        _set(("comment_dispositions",), []),
        R,
        "Review comment dispositions need the comments they answer",
    ),
    ("repository missing", _delete(("repository",)), C, "Invalid repository identity: None"),
    ("repository malformed", _set(("repository",), "bad"), C, "Invalid repository identity: 'bad'"),
    ("pull_request not an object", _set(PULL, []), R, "Review record metadata is malformed"),
    ("pull_request missing", _delete(PULL), R, "Review record metadata is malformed"),
    ("review not an object", _set(REVIEW, "x"), R, "Review record metadata is malformed"),
    ("review missing", _delete(REVIEW), R, "Review record metadata is malformed"),
    ("pull field missing", _delete((*PULL, "url")), R, "Review pull-request fields are malformed"),
    ("pull field unknown", _set((*PULL, "extra"), 1), R, "Review pull-request fields are malformed"),
    ("head_ref blank", _set((*PULL, "head_ref"), " "), R, "Review pull_request.head_ref is invalid"),
    ("head_ref not a string", _set((*PULL, "head_ref"), 1), R, "Review pull_request.head_ref is invalid"),
    ("number true", _set((*PULL, "number"), True), R, "Review pull number is invalid"),
    ("number zero", _set((*PULL, "number"), 0), R, "Review pull number is invalid"),
    ("number string", _set((*PULL, "number"), "7"), R, "Review pull number is invalid"),
    *_blank_and_non_string((*PULL, "url"), "Review pull_request.url is invalid"),
    *_blank_and_non_string((*PULL, "title"), "Review pull_request.title is invalid"),
    *_blank_and_non_string((*PULL, "base_ref"), "Review pull_request.base_ref is invalid"),
    ("base_sha uppercase", _set((*PULL, "base_sha"), "A" * 40), R, f"pull_request.base_sha {SHA_RULE}"),
    ("base_sha short", _set((*PULL, "base_sha"), "1" * 39), R, f"pull_request.base_sha {SHA_RULE}"),
    ("base_sha not a string", _set((*PULL, "base_sha"), None), R, f"pull_request.base_sha {SHA_RULE}"),
    ("head_sha uppercase", _set((*PULL, "head_sha"), "A" * 64), R, f"pull_request.head_sha {SHA_RULE}"),
    ("review field missing", _delete((*REVIEW, "summary")), R, "Review metadata fields are malformed"),
    ("review field unknown", _set((*REVIEW, "extra"), 1), R, "Review metadata fields are malformed"),
    ("reviewers empty", _set((*REVIEW, "reviewers"), []), R, "Review reviewers must be a non-empty array"),
    ("patches empty", _set((*REVIEW, "patches"), {}), R, "Review patches must be a non-empty object"),
    ("verdict unknown", _set((*REVIEW, "verdict"), "MAYBE"), R, "Review verdict is invalid"),
    ("verdict unhashable", _set((*REVIEW, "verdict"), []), R, "Review verdict is invalid"),
    ("coverage not an object", _set((*REVIEW, "coverage"), []), R, "Review coverage is malformed"),
    (
        "coverage without sources",
        _set((*REVIEW, "coverage"), {"uncovered_files": []}),
        R,
        "Review coverage is malformed",
    ),
    (
        "coverage unknown key",
        _set((*REVIEW, "coverage"), {"unavailable_sources": [], "x": []}),
        R,
        "Review coverage is malformed",
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
        R,
        "Review coverage is malformed",
    ),
    (
        "coverage sources not a list",
        _set((*REVIEW, "coverage"), {"unavailable_sources": "a"}),
        R,
        "Review coverage is malformed",
    ),
    (
        "coverage uncovered files not a list",
        _set((*REVIEW, "coverage"), {"unavailable_sources": [], "uncovered_files": "a"}),
        R,
        "Review coverage is malformed",
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
    ("version true", _set((*REVIEW, "version"), True), R, "Review version is invalid"),
    ("version zero", _set((*REVIEW, "version"), 0), R, "Review version is invalid"),
    ("version string", _set((*REVIEW, "version"), "1"), R, "Review version is invalid"),
    ("mode unknown", _set((*REVIEW, "mode"), "second"), R, "Review mode is invalid"),
    ("scope on an initial review", _set((*REVIEW, "scope"), {}), R, "Only a re-review has a scope"),
    ("reviewed_at not a string", _set((*REVIEW, "reviewed_at"), 1), R, "Review timestamp is invalid"),
    ("reviewed_at unparsable", _set((*REVIEW, "reviewed_at"), "yesterday"), R, "Review timestamp is invalid"),
    *_blank_and_non_string((*REVIEW, "summary"), "Review summary is invalid"),
    ("adapter not an object", _set(ADAPTER, []), R, "Review adapter metadata is malformed"),
    ("adapter field missing", _delete((*ADAPTER, "usage")), R, "Review adapter metadata is malformed"),
    ("adapter field unknown", _set((*ADAPTER, "extra"), 1), R, "Review adapter metadata is malformed"),
    ("protocol_version 2", _set((*ADAPTER, "protocol_version"), 2), R, "Review adapter protocol is unsupported"),
    ("protocol_version string", _set((*ADAPTER, "protocol_version"), "1"), R, "Review adapter protocol is unsupported"),
    ("adapter scope unknown", _set((*ADAPTER, "scope"), "global"), R, "Review adapter scope is invalid"),
    *_blank_and_non_string((*ADAPTER, "name"), "Review adapter name is invalid"),
    *_blank_and_non_string((*ADAPTER, "reviewer"), "Review adapter reviewer is invalid"),
    ("adapter status unknown", _set((*ADAPTER, "status"), "done"), R, "Review adapter status is invalid"),
    ("source_commit invalid", _set((*ADAPTER, "source_commit"), "abc"), R, f"review.adapter.source_commit {SHA_RULE}"),
    (
        "source_hashes not an object",
        _set((*ADAPTER, "source_hashes"), []),
        R,
        "Review adapter source_hashes is invalid",
    ),
    *[
        (
            f"source hash path {path!r}",
            _set((*ADAPTER, "source_hashes"), {path: SHA256}),
            R,
            "Review adapter source hash path is unsafe",
        )
        for path in ("", 1, "/a.md", "\\a.md", "a\\b.md", "../a.md", "a/../b.md")
    ],
    (
        "source hash invalid",
        _set((*ADAPTER, "source_hashes"), {"a.md": "A" * 64}),
        R,
        "Review adapter source hash is invalid: a.md",
    ),
    (
        "source hash not a string",
        _set((*ADAPTER, "source_hashes"), {"a.md": None}),
        R,
        "Review adapter source hash is invalid: a.md",
    ),
    (
        "first bad source hash wins",
        _set((*ADAPTER, "source_hashes"), {"b.md": "x", "/a.md": SHA256}),
        R,
        "Review adapter source hash is invalid: b.md",
    ),
    ("usage not an object", _set((*ADAPTER, "usage"), []), R, "Review adapter usage is invalid"),
    ("findings not a list", _set(("findings",), {}), R, "Review findings must be an array"),
    ("findings missing", _delete(("findings",)), R, "Review findings must be an array"),
    (
        "finding ID out of sequence",
        _set((*FINDING, "id"), "F002"),
        R,
        "Review finding IDs are not stable and contiguous",
    ),
    ("finding ID missing", _delete((*FINDING, "id")), R, "Review finding IDs are not stable and contiguous"),
    ("finding not an object", _set(("findings",), ["F001"]), R, "Review finding IDs are not stable and contiguous"),
    ("finding field missing", _delete((*FINDING, "body")), R, "Review finding fields are malformed"),
    ("finding field unknown", _set((*FINDING, "extra"), 1), R, "Review finding fields are malformed"),
    ("finding severity unknown", _set((*FINDING, "severity"), "LOW"), R, "Review finding F001 severity is invalid"),
    ("finding severity unhashable", _set((*FINDING, "severity"), []), R, "Review finding F001 severity is invalid"),
    ("finding title blank", _set((*FINDING, "title"), " "), R, f"Review finding F001.title {TITLE_RULE}"),
    ("finding title two lines", _set((*FINDING, "title"), "a\nb"), R, f"Review finding F001.title {TITLE_RULE}"),
    ("finding analyzer empty", _set((*FINDING, "analyzer"), {}), R, f"Review finding F001.analyzer {ANALYZER_RULE}"),
    *[
        case
        for field in ("candidate_key", "category", "path", "body", "evidence", "source")
        for case in _finding_field(field)
    ],
    *[
        (f"finding path {path!r}", _set((*FINDING, "path"), path), R, "Review finding F001.path is unsafe")
        for path in ("/a.py", "\\a.py", "a\\b.py", "../a.py", "a/../b.py")
    ],
    ("finding line true", _set((*FINDING, "line"), True), R, "Review finding F001.line is invalid"),
    ("finding line zero", _set((*FINDING, "line"), 0), R, "Review finding F001.line is invalid"),
    ("finding line string", _set((*FINDING, "line"), "3"), R, "Review finding F001.line is invalid"),
    (
        "first bad finding wins",
        _chain(
            _set(("findings",), [_finding("F001", "MUST_FIX"), _finding("F002", "MUST_FIX")]),
            _set(("findings", 1, "line"), 0),
            _set(("findings", 0, "body"), ""),
        ),
        R,
        "Review finding F001.body is invalid",
    ),
    ("counts wrong", _set((*REVIEW, "counts", "MUST_FIX"), 2), R, "Review finding counts do not match findings"),
    (
        "counts key missing",
        _delete((*REVIEW, "counts", "SUGGESTION")),
        R,
        "Review finding counts do not match findings",
    ),
    ("counts key unknown", _set((*REVIEW, "counts", "NIT"), 0), R, "Review finding counts do not match findings"),
    ("counts not an object", _set((*REVIEW, "counts"), None), R, "Review finding counts do not match findings"),
    ("prior dispositions missing", _delete(("prior_dispositions",)), R, "Review prior dispositions must be an array"),
    (
        "prior dispositions not a list",
        _set(("prior_dispositions",), {}),
        R,
        "Review prior dispositions must be an array",
    ),
    (
        "prior disposition not an object",
        _set(("prior_dispositions",), ["v1:F001"]),
        R,
        "Review prior disposition fields are malformed",
    ),
    (
        "prior disposition field unknown",
        _set(("prior_dispositions",), [{**PRIOR, "extra": 1}]),
        R,
        "Review prior disposition fields are malformed",
    ),
    (
        "prior disposition ID not a string",
        _set(("prior_dispositions",), [{**PRIOR, "finding_id": 1}]),
        R,
        "Review prior disposition IDs must be unique",
    ),
    (
        "prior disposition ID empty",
        _set(("prior_dispositions",), [{**PRIOR, "finding_id": ""}]),
        R,
        "Review prior disposition IDs must be unique",
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
        R,
        "Review prior disposition is invalid: v1:F001",
    ),
    (
        "prior disposition rationale blank",
        _set(("prior_dispositions",), [{**PRIOR, "rationale": " "}]),
        R,
        "Review prior disposition rationale is invalid: v1:F001",
    ),
    (
        "repeat without a ledger",
        _set((*FINDING, "repeats"), {"version": 1, "id": "F001"}),
        R,
        "Review finding repeats need a ledger",
    ),
    ("artifacts not an object", _set(("artifacts",), []), R, "Review artifact hashes are malformed"),
    (
        "artifacts key missing",
        _set(("artifacts",), {"payload_sha256": SHA256}),
        R,
        "Review artifact hashes are malformed",
    ),
    (
        "payload hash invalid",
        _set(("artifacts",), {"payload_sha256": "x", "markdown_sha256": SHA256}),
        R,
        f"artifacts.payload_sha256 {SHA_RULE}",
    ),
    (
        "markdown hash invalid",
        _set(("artifacts",), {"payload_sha256": SHA256, "markdown_sha256": "x"}),
        R,
        f"artifacts.markdown_sha256 {SHA_RULE}",
    ),
]

# One fault per check, in the order validate_record makes its checks. Each touches its own field or replaces what a
# check before it would read, so any suffix of the list can be applied to one record at once.
STAGES: list[tuple[str, Mutation, type[Exception], str]] = [
    ("record", _set(("extra",), 1), R, "Unsupported or malformed review record"),
    ("comments", _set(("github_comments",), []), R, "Review comment dispositions need the comments they answer"),
    ("repository", _set(("repository",), "bad"), C, "Invalid repository identity: 'bad'"),
    ("pull_request", _set(PULL, []), R, "Review record metadata is malformed"),
    ("review", _set(REVIEW, []), R, "Review record metadata is malformed"),
    ("pull fields", _set((*PULL, "extra"), 1), R, "Review pull-request fields are malformed"),
    ("head_ref", _set((*PULL, "head_ref"), ""), R, "Review pull_request.head_ref is invalid"),
    ("number", _set((*PULL, "number"), 0), R, "Review pull number is invalid"),
    ("url", _set((*PULL, "url"), ""), R, "Review pull_request.url is invalid"),
    ("title", _set((*PULL, "title"), ""), R, "Review pull_request.title is invalid"),
    ("base_ref", _set((*PULL, "base_ref"), ""), R, "Review pull_request.base_ref is invalid"),
    ("base_sha", _set((*PULL, "base_sha"), ""), R, f"pull_request.base_sha {SHA_RULE}"),
    ("head_sha", _set((*PULL, "head_sha"), ""), R, f"pull_request.head_sha {SHA_RULE}"),
    ("review fields", _set((*REVIEW, "extra"), 1), R, "Review metadata fields are malformed"),
    ("reviewers", _set((*REVIEW, "reviewers"), []), R, "Review reviewers must be a non-empty array"),
    ("patches", _set((*REVIEW, "patches"), {}), R, "Review patches must be a non-empty object"),
    ("verdict", _set((*REVIEW, "verdict"), "MAYBE"), R, "Review verdict is invalid"),
    ("coverage", _set((*REVIEW, "coverage"), []), R, "Review coverage is malformed"),
    (
        "incomplete",
        _set((*REVIEW, "verdict"), "INCOMPLETE"),
        R,
        "An INCOMPLETE review must list its unavailable sources",
    ),
    ("version", _set((*REVIEW, "version"), 0), R, "Review version is invalid"),
    ("mode", _set((*REVIEW, "mode"), "second"), R, "Review mode is invalid"),
    ("scope", _set((*REVIEW, "scope"), {}), R, "Only a re-review has a scope"),
    ("reviewed_at", _set((*REVIEW, "reviewed_at"), "yesterday"), R, "Review timestamp is invalid"),
    ("summary", _set((*REVIEW, "summary"), ""), R, "Review summary is invalid"),
    ("adapter", _set(ADAPTER, []), R, "Review adapter metadata is malformed"),
    ("protocol", _set((*ADAPTER, "protocol_version"), 2), R, "Review adapter protocol is unsupported"),
    ("adapter scope", _set((*ADAPTER, "scope"), "global"), R, "Review adapter scope is invalid"),
    ("adapter name", _set((*ADAPTER, "name"), ""), R, "Review adapter name is invalid"),
    ("adapter reviewer", _set((*ADAPTER, "reviewer"), ""), R, "Review adapter reviewer is invalid"),
    ("adapter status", _set((*ADAPTER, "status"), "done"), R, "Review adapter status is invalid"),
    ("source_commit", _set((*ADAPTER, "source_commit"), "abc"), R, f"review.adapter.source_commit {SHA_RULE}"),
    ("source_hashes", _set((*ADAPTER, "source_hashes"), []), R, "Review adapter source_hashes is invalid"),
    (
        "source hash path",
        _set((*ADAPTER, "source_hashes"), {"/a.md": SHA256}),
        R,
        "Review adapter source hash path is unsafe",
    ),
    ("source hash", _set((*ADAPTER, "source_hashes"), {"a.md": "x"}), R, "Review adapter source hash is invalid: a.md"),
    ("usage", _set((*ADAPTER, "usage"), []), R, "Review adapter usage is invalid"),
    ("findings", _set(("findings",), {}), R, "Review findings must be an array"),
    ("finding IDs", _set((*FINDING, "id"), "F002"), R, "Review finding IDs are not stable and contiguous"),
    ("finding fields", _set((*FINDING, "extra"), 1), R, "Review finding fields are malformed"),
    ("finding severity", _set((*FINDING, "severity"), "LOW"), R, "Review finding F001 severity is invalid"),
    ("finding title", _set((*FINDING, "title"), ""), R, f"Review finding F001.title {TITLE_RULE}"),
    ("finding analyzer", _set((*FINDING, "analyzer"), {}), R, f"Review finding F001.analyzer {ANALYZER_RULE}"),
    *[
        (f"finding {field}", _set((*FINDING, field), ""), R, f"Review finding F001.{field} is invalid")
        for field in ("candidate_key", "category", "path", "body", "evidence", "source")
    ],
    ("finding path unsafe", _set((*FINDING, "path"), "/a.py"), R, "Review finding F001.path is unsafe"),
    ("finding line", _set((*FINDING, "line"), 0), R, "Review finding F001.line is invalid"),
    ("counts", _set((*REVIEW, "counts", "MUST_FIX"), 2), R, "Review finding counts do not match findings"),
    ("prior dispositions", _set(("prior_dispositions",), {}), R, "Review prior dispositions must be an array"),
    ("prior fields", _set(("prior_dispositions",), [{}]), R, "Review prior disposition fields are malformed"),
    ("prior IDs", _set(("prior_dispositions",), [PRIOR, PRIOR]), R, "Review prior disposition IDs must be unique"),
    (
        "prior disposition",
        _set(("prior_dispositions",), [{**PRIOR, "disposition": "fixed"}]),
        R,
        "Review prior disposition is invalid: v1:F001",
    ),
    (
        "prior rationale",
        _set(("prior_dispositions",), [{**PRIOR, "rationale": ""}]),
        R,
        "Review prior disposition rationale is invalid: v1:F001",
    ),
    ("ledger", _set((*FINDING, "repeats"), {"version": 1, "id": "F001"}), R, "Review finding repeats need a ledger"),
    ("artifacts", _set(("artifacts",), []), R, "Review artifact hashes are malformed"),
    (
        "payload hash",
        _set(("artifacts",), {"payload_sha256": "x", "markdown_sha256": SHA256}),
        R,
        f"artifacts.payload_sha256 {SHA_RULE}",
    ),
    (
        "markdown hash",
        _set(("artifacts",), {"payload_sha256": SHA256, "markdown_sha256": "x"}),
        R,
        f"artifacts.markdown_sha256 {SHA_RULE}",
    ),
]

# Faults that exist only while an optional part is absent: a scope on an initial review, comment dispositions
# without comments, and a repeat without a ledger. The full record has those parts, so it cannot carry them.
ABSENCE_FAULTS = {"scope on an initial review", "comment dispositions alone", "repeat without a ledger"}
# Errors whose message names the faulty value: a stage carries the same check with another value.
VALUE_VARIANTS = {
    (C, "Invalid repository identity: None"),
    (R, "Review adapter source hash is invalid: b.md"),
}


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
        stage_errors = {(error, message) for _name, _mutation, error, message in STAGES}
        rejected_errors = {(error, message) for _name, _mutation, error, message in REJECTED}
        self.assertEqual(set(), rejected_errors - stage_errors - VALUE_VARIANTS)


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
    result["prior_dispositions"] = [RESULT_PRIOR]
    result["comment_dispositions"] = [RESULT_COMMENT]
    result["usage"] = {"input_tokens": 1}
    return result


RESULT_FINDING = ("findings", 0)
SECOND_FINDING = ("findings", 1)
THIRD_FINDING = ("findings", 2)
TWO_FINDINGS = _set(("findings",), [_result_finding("key-a"), _result_finding("key-b", "SHOULD_FIX")])
DISPOSITION_VALUES = ("addressed", "partially_addressed", "still_present", "superseded", "unable_to_verify")

# Results validate_adapter_result accepts: the name, the result builder, the mutation, and the extra arguments.
ACCEPTED_RESULTS: list[tuple[str, Callable[[], dict[str, Any]], Mutation, dict[str, Any]]] = [
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
    # Quirks pinned as they stand, not endorsed: the version and pull number compare with `!=`, so true and 1.0 equal
    # 1 and 7.0 equals 7, and a drive-letter path is not caught by the relative-path check.
    ("protocol_version true", _result, _set(("protocol_version",), True), {}),
    ("protocol_version 1.0", _result, _set(("protocol_version",), 1.0), {}),
    ("pull_number 7.0", _result, _set(("pull_number",), 7.0), {}),
    ("drive-letter finding path", _result, _set((*RESULT_FINDING, "path"), "C:/x.py"), {}),
]


def _result_text(field: str, message: str) -> list[ResultCase]:
    return [
        (f"{field} blank", _set((field,), " "), R, message, {}),
        (f"{field} not a string", _set((field,), 1), R, message, {}),
        (f"{field} missing", _delete((field,)), R, message, {}),
    ]


def _result_finding_text(field: str) -> list[ResultCase]:
    message = f"Finding key-a.{field} must be non-empty"
    return [
        (f"finding {field} blank", _set((*RESULT_FINDING, field), " "), R, message, {}),
        (f"finding {field} not a string", _set((*RESULT_FINDING, field), None), R, message, {}),
    ]


def _disposition_faults(label: str, key: str, item: dict[str, Any], identifier: str, ids: str) -> list[ResultCase]:
    """The faults validate_dispositions refuses, for the prior or the comment dispositions of a result."""
    field = f"{label.lower()}_dispositions"
    subject = {"Prior": "prior finding", "Comment": "review comment"}[label]
    listed = {ids: [identifier]}

    def given(value: Any) -> Mutation:
        return _set((field,), value)

    fields = f"{label} disposition fields do not match the protocol"
    unique = f"{label} disposition IDs must be unique strings"
    invalid = f"Invalid disposition for {subject} {identifier}"
    rationale = f"{subject[0].upper()}{subject[1:]} {identifier} requires a rationale"
    return [
        (f"{field} not a list", given({}), R, f"{field} must be an array", {}),
        (f"{field} null", given(None), R, f"{field} must be an array", {}),
        (f"{label} disposition not an object", given([identifier]), R, fields, {}),
        (f"{label} disposition field missing", given([{key: identifier, "disposition": "addressed"}]), R, fields, {}),
        (f"{label} disposition field unknown", given([{**item, "extra": 1}]), R, fields, {}),
        (f"{label} disposition ID not a string", given([{**item, key: 1}]), R, unique, {}),
        (f"{label} disposition ID duplicated", given([item, item]), R, unique, listed),
        (f"{label} disposition unknown", given([{**item, "disposition": "fixed"}]), R, invalid, listed),
        (f"{label} disposition unhashable", given([{**item, "disposition": []}]), R, invalid, listed),
        (f"{label} rationale blank", given([{**item, "rationale": " "}]), R, rationale, listed),
        (f"{label} rationale not a string", given([{**item, "rationale": None}]), R, rationale, listed),
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
LINE = "Finding key-a.line must be a positive integer"
OPEN_RULE = "so that finding's disposition must be still_present or partially_addressed"

# Results validate_adapter_result refuses, from the minimal result: the exception class, the message, and the extra
# arguments.
REJECTED_RESULTS: list[ResultCase] = [
    ("result not an object", _replace([]), R, "Adapter result must be an object", {}),
    ("result null", _replace(None), R, "Adapter result must be an object", {}),
    ("unknown field", _set(("extra",), 1), R, "Adapter result contains unknown fields: extra", {}),
    (
        "unknown fields, sorted",
        _chain(_set(("zeta",), 1), _set(("alpha",), 1)),
        R,
        "Adapter result contains unknown fields: alpha, zeta",
        {},
    ),
    ("a record field", _set(("ledger",), []), R, "Adapter result contains unknown fields: ledger", {}),
    ("protocol_version 2", _set(("protocol_version",), 2), R, "Unsupported adapter protocol version", {}),
    ("protocol_version string", _set(("protocol_version",), "1"), R, "Unsupported adapter protocol version", {}),
    ("protocol_version missing", _delete(("protocol_version",)), R, "Unsupported adapter protocol version", {}),
    ("repository malformed", _set(("repository",), "bad"), C, "Invalid repository identity: 'bad'", {}),
    ("repository missing", _delete(("repository",)), C, "Invalid repository identity: None", {}),
    (
        "repository other",
        _set(("repository",), "owner/other"),
        R,
        "Adapter result repository does not match request",
        {},
    ),
    ("pull_number other", _set(("pull_number",), 8), R, "Adapter result pull number does not match request", {}),
    ("pull_number string", _set(("pull_number",), "7"), R, "Adapter result pull number does not match request", {}),
    ("pull_number missing", _delete(("pull_number",)), R, "Adapter result pull number does not match request", {}),
    ("head_sha other", _set(("head_sha",), BASE_SHA), R, "Adapter result head SHA does not match request", {}),
    (
        "head_sha uppercase",
        _set(("head_sha",), "2" * 39 + "A"),
        R,
        "Adapter result head SHA does not match request",
        {},
    ),
    ("head_sha missing", _delete(("head_sha",)), R, "Adapter result head SHA does not match request", {}),
    *_result_text("summary", "Adapter result summary is required"),
    *_result_text("reviewer", "Adapter result reviewer is required"),
    ("status unknown", _set(("status",), "done"), R, "Adapter result status is invalid", {}),
    ("status unhashable", _set(("status",), []), R, "Adapter result status is invalid", {}),
    ("status missing", _delete(("status",)), R, "Adapter result status is invalid", {}),
    ("findings not a list", _set(("findings",), {}), R, "Adapter findings must be an array", {}),
    ("findings missing", _delete(("findings",)), R, "Adapter findings must be an array", {}),
    ("finding not an object", _set(("findings",), ["key-a"]), R, "Every adapter finding must be an object", {}),
    (
        "finding field missing",
        _delete((*RESULT_FINDING, "body")),
        R,
        "Adapter finding fields do not match the protocol",
        {},
    ),
    (
        "finding field unknown",
        _set((*RESULT_FINDING, "id"), "F001"),
        R,
        "Adapter finding fields do not match the protocol",
        {},
    ),
    ("candidate_key empty", _set((*RESULT_FINDING, "candidate_key"), ""), R, KEYS, {}),
    ("candidate_key not a string", _set((*RESULT_FINDING, "candidate_key"), 1), R, KEYS, {}),
    ("candidate_key unhashable", _set((*RESULT_FINDING, "candidate_key"), []), R, KEYS, {}),
    ("candidate_key duplicated", _chain(TWO_FINDINGS, _set((*SECOND_FINDING, "candidate_key"), "key-a")), R, KEYS, {}),
    ("title blank", _set((*RESULT_FINDING, "title"), " "), R, f"Finding key-a.title {TITLE_RULE}", {}),
    ("title too long", _set((*RESULT_FINDING, "title"), "x" * 121), R, f"Finding key-a.title {TITLE_RULE}", {}),
    ("analyzer empty", _set((*RESULT_FINDING, "analyzer"), {}), R, f"Finding key-a.analyzer {ANALYZER_RULE}", {}),
    ("severity unknown", _set((*RESULT_FINDING, "severity"), "LOW"), R, "Invalid finding severity for key-a", {}),
    ("severity unhashable", _set((*RESULT_FINDING, "severity"), []), R, "Invalid finding severity for key-a", {}),
    *[case for field in ("category", "path", "body", "evidence", "source") for case in _result_finding_text(field)],
    *[
        (f"finding path {path!r}", _set((*RESULT_FINDING, "path"), path), R, UNSAFE_PATH, {})
        for path in ("/a.py", "\\a.py", "a\\b.py", "../a.py", "a/../b.py")
    ],
    ("line true", _set((*RESULT_FINDING, "line"), True), R, LINE, {}),
    ("line zero", _set((*RESULT_FINDING, "line"), 0), R, LINE, {}),
    ("line string", _set((*RESULT_FINDING, "line"), "3"), R, LINE, {}),
    (
        "first bad finding wins",
        _chain(TWO_FINDINGS, _set((*SECOND_FINDING, "line"), 0), _set((*RESULT_FINDING, "body"), "")),
        R,
        "Finding key-a.body must be non-empty",
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
    ("repeats empty", _repeat(""), R, "Finding key-b.repeats must be a candidate key or prior finding ID", {}),
    ("repeats not a string", _repeat(1), R, "Finding key-b.repeats must be a candidate key or prior finding ID", {}),
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
    ("usage a list", _set(("usage",), []), R, "usage must be an object or null", {}),
    ("usage a string", _set(("usage",), "1"), R, "usage must be an object or null", {}),
]

# One fault per check, in the order validate_adapter_result makes its checks, under the default arguments. Each
# touches its own field or replaces what a check before it would read, so any suffix can be applied at once.
RESULT_STAGES: list[tuple[str, Mutation, type[Exception], str]] = [
    ("object", _replace([]), R, "Adapter result must be an object"),
    ("unknown fields", _set(("extra",), 1), R, "Adapter result contains unknown fields: extra"),
    ("protocol", _set(("protocol_version",), 2), R, "Unsupported adapter protocol version"),
    ("repository identity", _set(("repository",), "bad"), C, "Invalid repository identity: 'bad'"),
    ("repository", _set(("repository",), "owner/other"), R, "Adapter result repository does not match request"),
    ("pull number", _set(("pull_number",), 8), R, "Adapter result pull number does not match request"),
    ("head SHA", _set(("head_sha",), BASE_SHA), R, "Adapter result head SHA does not match request"),
    ("summary", _set(("summary",), ""), R, "Adapter result summary is required"),
    ("reviewer", _set(("reviewer",), ""), R, "Adapter result reviewer is required"),
    ("status", _set(("status",), "done"), R, "Adapter result status is invalid"),
    ("findings", _set(("findings",), {}), R, "Adapter findings must be an array"),
    ("finding object", _set(RESULT_FINDING, "key-a"), R, "Every adapter finding must be an object"),
    ("finding fields", _set((*RESULT_FINDING, "extra"), 1), R, "Adapter finding fields do not match the protocol"),
    ("candidate key", _set((*RESULT_FINDING, "candidate_key"), ""), R, KEYS),
    ("title", _set((*RESULT_FINDING, "title"), ""), R, f"Finding key-a.title {TITLE_RULE}"),
    ("analyzer", _set((*RESULT_FINDING, "analyzer"), {}), R, f"Finding key-a.analyzer {ANALYZER_RULE}"),
    ("severity", _set((*RESULT_FINDING, "severity"), "LOW"), R, "Invalid finding severity for key-a"),
    *[
        (f"finding {field}", _set((*RESULT_FINDING, field), ""), R, f"Finding key-a.{field} must be non-empty")
        for field in ("category", "path", "body", "evidence", "source")
    ],
    ("finding path unsafe", _set((*RESULT_FINDING, "path"), "/a.py"), R, UNSAFE_PATH),
    ("finding line", _set((*RESULT_FINDING, "line"), 0), R, LINE),
    ("prior dispositions", _set(("prior_dispositions",), {}), R, "prior_dispositions must be an array"),
    ("prior fields", _set(("prior_dispositions",), [{}]), R, "Prior disposition fields do not match the protocol"),
    (
        "prior IDs",
        _set(("prior_dispositions",), [{**RESULT_PRIOR, "finding_id": 1}]),
        R,
        "Prior disposition IDs must be unique strings",
    ),
    (
        "prior disposition",
        _set(("prior_dispositions",), [{**RESULT_PRIOR, "disposition": "fixed"}]),
        R,
        f"Invalid disposition for prior finding {PRIOR_ID}",
    ),
    (
        "prior rationale",
        _set(("prior_dispositions",), [{**RESULT_PRIOR, "rationale": ""}]),
        R,
        f"Prior finding {PRIOR_ID} requires a rationale",
    ),
    (
        "prior mismatch",
        _set(("prior_dispositions",), [RESULT_PRIOR]),
        R,
        f"Prior dispositions mismatch; missing=[], unknown=['{PRIOR_ID}']",
    ),
    ("comment dispositions", _set(("comment_dispositions",), {}), R, "comment_dispositions must be an array"),
    (
        "comment fields",
        _set(("comment_dispositions",), [{}]),
        R,
        "Comment disposition fields do not match the protocol",
    ),
    (
        "comment IDs",
        _set(("comment_dispositions",), [{**RESULT_COMMENT, "comment_id": 1}]),
        R,
        "Comment disposition IDs must be unique strings",
    ),
    (
        "comment disposition",
        _set(("comment_dispositions",), [{**RESULT_COMMENT, "disposition": "fixed"}]),
        R,
        "Invalid disposition for review comment C1",
    ),
    (
        "comment rationale",
        _set(("comment_dispositions",), [{**RESULT_COMMENT, "rationale": ""}]),
        R,
        "Review comment C1 requires a rationale",
    ),
    (
        "comment mismatch",
        _set(("comment_dispositions",), [RESULT_COMMENT]),
        R,
        "Comment dispositions mismatch; missing=[], unknown=['C1']",
    ),
    ("repeats", _set((*RESULT_FINDING, "repeats"), "key-z"), R, "Finding key-a repeats an unknown finding: key-z"),
    ("usage", _set(("usage",), []), R, "usage must be an object or null"),
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

    def test_stages_cover_every_check(self) -> None:
        # Every raise site of validate_adapter_result has a stage. The checks inside validate_dispositions and
        # _validate_result_repeats, which the split does not touch, are placed by their stages above.
        stage_errors = {(error, message) for _name, _mutation, error, message in RESULT_STAGES}
        own_errors = {
            (error, message)
            for _name, _mutation, error, message, arguments in REJECTED_RESULTS
            if not arguments and not message.startswith(("Finding key-b", "Finding key-c"))
        }
        self.assertEqual(set(), own_errors - stage_errors - RESULT_VARIANTS)


# Refusals another stage already places in the order: the same check with another value.
RESULT_VARIANTS = {
    (C, "Invalid repository identity: None"),
    (R, "Adapter result contains unknown fields: alpha, zeta"),
    (R, "Adapter result contains unknown fields: ledger"),
}


if __name__ == "__main__":
    unittest.main()

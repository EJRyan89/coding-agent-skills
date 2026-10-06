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
from review_records import RecordError, validate_record

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


if __name__ == "__main__":
    unittest.main()

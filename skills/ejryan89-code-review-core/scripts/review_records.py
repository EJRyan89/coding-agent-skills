"""Review adapter validation, verdict calculation, and paired rendering."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, NotRequired, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import whole_number
from flat_text import flat_text
from markdown_literal import markdown_line as _text
from markdown_literal import markdown_literal as _literal
from markdown_literal import navigation_url as _navigation_url
from review_config import (
    VerdictPolicy,
    is_unreviewed_suffix,
    safe_repository_path,
    validate_repository_identity,
    verdict_policy,
)
from review_io import atomic_write_json, atomic_write_text, read_json
from review_vocabularies import (
    DISPATCH_MODES,
    DISPOSITIONS,
    LEDGER_STATE_NAMES,
    LEDGER_STATES,
    OPEN_DISPOSITIONS,
    SEVERITIES,
    SEVERITY_RANK,
    SEVERITY_SECTIONS,
    VERDICTS,
    DispatchMode,
    Disposition,
    LedgerState,
    Severity,
    Verdict,
)
from typed_fields import FieldError, FieldReader, describe

RECORD_SCHEMA_VERSION = 2
# The record versions a reader accepts. A version 1 record, written before unreviewed files were recorded, has no
# `review.coverage.unreviewed_files`; a version 2 record may.
READABLE_RECORD_VERSIONS = (1, RECORD_SCHEMA_VERSION)
# Where the reviewer that ran came from, as its record's `review.adapter.source` says, with each one's scope: the
# suite's generic reviewer the repository is configured with; a repository reviewer read at its trusted ref, at the pull
# request's base, or at the default branch's tip because the base predates its review skill; or the generic reviewer
# in place of a review skill that neither the base nor the tip could supply.
ADAPTER_SOURCES = {
    "generic": "generic",
    "trusted-ref": "repository",
    "base": "repository",
    "default-branch": "repository",
    "generic-fallback": "generic",
}
ADAPTER_PROTOCOL_VERSION = 1
# A profile the suite ships, which a specialist names as `suite:<name>` in place of a repository path, is keyed in
# `review.adapter.source_hashes` by that reference beside the repository files the reviewer was built from. Defined
# here, with the records, so the review that writes the key and a reader of records that must tell the two apart
# share one rule without the reader importing the review runtime.
SUITE_PROFILE_PREFIX = "suite:"
FINDING_FIELDS = frozenset({"candidate_key", "severity", "category", "path", "line", "body", "evidence", "source"})
OPTIONAL_FINDING_FIELDS = frozenset({"title", "analyzer", "repeats"})
# A record's finding may also say where in a changed document its line is, which the suite adds and no reviewer gives.
RECORD_OPTIONAL_FINDING_FIELDS = OPTIONAL_FINDING_FIELDS | {"location"}
MAX_LOCATION_CHARACTERS = 200
# The finding ledger: one entry per problem raised on a pull request since its latest initial review, identified by
# the version and finding ID where it first appeared. A repeat is linked to the finding it repeats, never counted
# twice, so a repeat needs a target at least as severe. A disposition of a prior finding judges its entry.
FINDING_ID = re.compile(r"F[0-9]{3,}")
LEDGER_ID = re.compile(r"v([1-9][0-9]*):(F[0-9]{3,})")
LEDGER_FIELDS = frozenset({"version", "id", "severity", "category", "state", "judged_in", "dispositions", "repeats"})
# A ledger entry's key: the version and finding ID where the finding first appeared.
LedgerKey = tuple[int, str]
COMMENT_FIELDS = ("id", "author", "path", "line", "outdated", "body", "url")
REVIEWER_FIELDS = frozenset({"id", "category", "files", "findings", "retries", "dispositions_only"})
# Records written before reviewer timing, model reporting, or read counts existed, or by a reviewer that does not
# report them (a repository entrypoint reviewer has no model field in its protocol), omit them. The read counts are
# null together when no guard counted the reviewer's reads: an inline, Copilot CLI host, or unguarded reviewer.
OPTIONAL_REVIEWER_FIELDS = frozenset({"seconds", "model", "files_read", "bytes_read"})
READ_COUNT_FIELDS = ("files_read", "bytes_read")
# Where a review's source snapshot came from, how large it was, and how long each step of prepare around it took:
# fetching the head, materializing the snapshot, and writing the request and every role's prompt.
SNAPSHOT_SOURCES = ("checkout", "checkout-lazy", "tarball")
SNAPSHOT_FIELDS = frozenset({"source", "files", "bytes", "seconds"})
# How many head paths the snapshot left out, by the reason its manifest records, and, when GitHub's diff endpoint
# refused the change over one of its limits, where the diff came from instead; each absent from older records.
OPTIONAL_SNAPSHOT_FIELDS = frozenset({"excluded", "diff"})
EXCLUSION_REASON = re.compile(r"[a-z][a-z-]{0,39}")
# Where prepare takes a diff GitHub's diff endpoint refuses, in words: git in the configured checkout, else GitHub's
# files API.
DIFF_SOURCES = {"checkout": "git's diff in the checkout", "files-api": "GitHub's files API"}
DIFF_FIELDS = frozenset({"source", "limit"})
# The limit GitHub's diff endpoint names: a count and what it counts, as in `300 files`.
DIFF_LIMIT = re.compile(r"[1-9][0-9]{0,9} [a-z]{1,20}")
SNAPSHOT_PHASES = ("fetch", "materialize", "prompts")
TITLE_MAXIMUM_LENGTH = 120
MODEL_MAXIMUM_LENGTH = 200
# How a diagnostic analyzer could catch a finding instead of a reviewer: a rule in an analyzer the repository already
# has but does not enforce, a rule in an established analyzer it does not use, or a pattern no rule covers yet.
ANALYZER_COVERAGES = ("available", "known", "custom-candidate")
ANALYZER_FIELDS = frozenset({"coverage", "tool", "rule"})
ANALYZER_NAME = re.compile(r"[^\s`|<>]{1,100}")
CUSTOM_RULE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
CUSTOM_RULE_MAXIMUM_LENGTH = 60


# The shapes of a reviewer's result and of a review record, as the validators below return them once every field they
# type has been read. A field a version 1 record, or a record written before the field existed, may lack is NotRequired.


class Analyzer(TypedDict):
    coverage: str
    tool: str
    rule: str


class FindingReference(TypedDict):
    """A finding named by the review version it appeared in and its ID there."""

    version: int
    id: str


class AdapterFinding(TypedDict):
    """A finding as a reviewer returns it; its `repeats` names a candidate key of the same result or a prior ledger
    ID."""

    candidate_key: str
    severity: Severity
    category: str
    path: str
    line: int
    body: str
    evidence: str
    source: str
    title: NotRequired[str]
    analyzer: NotRequired[Analyzer]
    repeats: NotRequired[str]
    # Where in a changed document the finding's line is, which the suite adds once the result is validated.
    location: NotRequired[str]


class Finding(TypedDict):
    """A record's finding: a reviewer's, numbered, with its `repeats` resolved to the finding it repeats."""

    id: str
    candidate_key: str
    severity: Severity
    category: str
    path: str
    line: int
    body: str
    evidence: str
    source: str
    title: NotRequired[str]
    analyzer: NotRequired[Analyzer]
    location: NotRequired[str]
    repeats: NotRequired[FindingReference]


class PriorDisposition(TypedDict):
    finding_id: str
    disposition: Disposition
    rationale: str


class CommentDisposition(TypedDict):
    comment_id: str
    disposition: Disposition
    rationale: str


class AdapterResult(TypedDict):
    """A reviewer's result. A repository reviewer that predates comment dispositions gives none."""

    protocol_version: int
    repository: str
    pull_number: int
    head_sha: str
    summary: str
    reviewer: str
    status: str
    findings: list[AdapterFinding]
    prior_dispositions: NotRequired[list[PriorDisposition]]
    comment_dispositions: NotRequired[list[CommentDisposition]]
    usage: NotRequired[dict[str, object] | None]


class LedgerDisposition(TypedDict):
    """A disposition as the ledger reads it: the ledger ID of the entry it judges, and how."""

    finding_id: str
    disposition: Disposition


class LedgerJudgment(TypedDict):
    version: int
    disposition: Disposition


class LedgerEntry(TypedDict):
    """One problem raised on a pull request: where it first appeared, how each later review judged it, and where it
    was raised again."""

    version: int
    id: str
    severity: Severity
    category: str
    state: LedgerState
    judged_in: int
    dispositions: list[LedgerJudgment]
    repeats: list[FindingReference]


class PullRequest(TypedDict):
    number: int
    url: str
    title: str
    base_ref: str
    base_sha: str
    head_sha: str
    head_ref: NotRequired[str]
    body_characters: NotRequired[int]
    body_given: NotRequired[int]


class Coverage(TypedDict):
    unavailable_sources: list[str]
    uncovered_files: NotRequired[list[str]]
    unreviewed_files: NotRequired[dict[str, str]]


class ReviewerRun(TypedDict):
    """One reviewer that ran. Its read counts are both null when no guard counted its reads."""

    id: str
    category: str
    files: int
    findings: int
    retries: int
    dispositions_only: bool
    seconds: NotRequired[int]
    model: NotRequired[str]
    files_read: NotRequired[int | None]
    bytes_read: NotRequired[int | None]


class DiffFallback(TypedDict):
    """Where a diff came from when GitHub's diff endpoint refused it, and the limit it named, as in `300 files`."""

    source: str  # a key of DIFF_SOURCES
    limit: str


class Snapshot(TypedDict):
    source: str
    files: int
    bytes: int
    seconds: dict[str, float]
    excluded: NotRequired[dict[str, int]]
    diff: NotRequired[DiffFallback]


class Patch(TypedDict):
    sha256: str
    lines: int


class Scope(TypedDict):
    """A re-review's scope; the changed counts are both null when it could not compare with an earlier version."""

    requested: str
    used: str
    reason: str
    since_version: int
    files_changed: int | None
    files_total: int
    lines_changed: int | None
    lines_total: int


class Adapter(TypedDict):
    name: str
    protocol_version: int
    scope: str
    source: NotRequired[str]
    source_commit: str | None
    source_hashes: dict[str, str]
    reviewer: str
    status: str
    usage: dict[str, object] | None


class Review(TypedDict):
    version: int
    mode: str
    reviewed_at: str
    summary: str
    verdict: Verdict
    counts: dict[Severity, int]
    adapter: Adapter
    coverage: NotRequired[Coverage]
    reviewers: NotRequired[list[ReviewerRun]]
    patches: NotRequired[dict[str, Patch]]
    scope: NotRequired[Scope]
    dispatch: NotRequired[DispatchMode]
    snapshot: NotRequired[Snapshot]


class Comment(TypedDict):
    """An open review comment a record answers, as its `C<n>` ID names it."""

    id: str
    author: str
    path: str
    line: int | None
    outdated: bool
    body: str
    url: str


class Artifacts(TypedDict):
    payload_sha256: str
    markdown_sha256: str


class ReviewRecord(TypedDict):
    """A review record. One written before ledgers has no `ledger`, and one without open review comments neither
    `github_comments` nor `comment_dispositions`."""

    schema_version: int
    repository: str
    pull_request: PullRequest
    review: Review
    findings: list[Finding]
    prior_dispositions: list[PriorDisposition]
    github_comments: NotRequired[list[Comment]]
    comment_dispositions: NotRequired[list[CommentDisposition]]
    artifacts: NotRequired[Artifacts | None]
    ledger: NotRequired[list[LedgerEntry]]


class RecordError(ValueError):
    pass


class RecordFieldError(FieldError, RecordError):
    """A record or adapter-result field of the wrong kind."""


READ = FieldReader(RecordFieldError)


ANALYZER_NAME_KIND = "a name of at most 100 characters without whitespace, backticks, pipes, or angle brackets"


CUSTOM_RULE_KIND = f"a lowercase kebab-case pattern name of at most {CUSTOM_RULE_MAXIMUM_LENGTH} characters"


def read_title(reader: FieldReader, value: object, path: str) -> str:
    """A finding headline: one trimmed, non-blank line short enough for a report summary row."""
    return reader.string(value, path, single_line=True, trimmed=True, maximum=TITLE_MAXIMUM_LENGTH)


def read_model(reader: FieldReader, value: object, path: str) -> str:
    """The model a reviewer reports it ran on: one trimmed, non-blank line."""
    return reader.string(value, path, single_line=True, trimmed=True, maximum=MODEL_MAXIMUM_LENGTH)


def read_location(reader: FieldReader, value: object, path: str) -> str:
    """A finding's place in a changed document: one trimmed, non-blank line within the limit."""
    return reader.string(value, path, single_line=True, trimmed=True, maximum=MAX_LOCATION_CHARACTERS)


def read_analyzer(reader: FieldReader, value: object, path: str) -> Analyzer:
    """A finding's analyzer coverage. Tool and rule are single tokens, so a report line can name them unquoted, and a
    custom candidate's rule is a pattern name."""
    analyzer = reader.json_object(value, path, required=ANALYZER_FIELDS)
    coverage = reader.one_of(analyzer["coverage"], f"{path}.coverage", ANALYZER_COVERAGES)
    for field in ("tool", "rule"):
        name = reader.string(analyzer[field], f"{path}.{field}")
        if not ANALYZER_NAME.fullmatch(name):
            raise reader.fail(f"{path}.{field}", ANALYZER_NAME_KIND, describe(name))
    rule = analyzer["rule"]
    if coverage == "custom-candidate" and not (
        len(rule) <= CUSTOM_RULE_MAXIMUM_LENGTH and CUSTOM_RULE.fullmatch(rule) is not None
    ):
        raise reader.fail(f"{path}.rule", CUSTOM_RULE_KIND, describe(rule))
    return cast(Analyzer, analyzer)


# A re-review asks for one of these scopes and runs as `full` or `incremental`. Its record keeps what was asked,
# what ran, why, and how much changed since the version it compared with (null when it could not compare).
RE_REVIEW_SCOPES = ("auto", "full", "incremental")
SCOPE_FIELDS = frozenset(
    {"requested", "used", "reason", "since_version", "files_changed", "files_total", "lines_changed", "lines_total"}
)
PATCH_FIELDS = frozenset({"sha256", "lines"})


def valid_analyzer(value: object) -> bool:
    """Whether a value is analyzer coverage, for a reader that passes over coverage it cannot use."""
    try:
        read_analyzer(READ, value, "analyzer")
    except FieldError:
        return False
    return True


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_dispositions(
    dispositions: object,
    key: str,
    expected: set[str],
    label: str,
    *,
    path: str,
    reader: FieldReader = READ,
) -> None:
    """Exactly one well-formed disposition for each expected ID (a prior finding or a review comment). `reader` reads
    them as the format that holds them, at `path`."""
    actual: set[str] = set()
    for index, disposition in enumerate(reader.array(dispositions, path)):
        item = f"{path}[{index}]"
        reader.json_object(disposition, item, required={key, "disposition", "rationale"})
        identifier = reader.string(disposition[key], f"{item}.{key}")
        if identifier in actual:
            raise RecordError(f"{label} disposition IDs must be unique strings")
        actual.add(identifier)
        reader.one_of(disposition["disposition"], f"{item}.disposition", DISPOSITIONS)
        reader.string(disposition["rationale"], f"{item}.rationale")
    if actual != expected:
        missing = sorted(expected - actual)
        unknown_ids = sorted(actual - expected)
        raise RecordError(f"{label} dispositions mismatch; missing={missing}, unknown={unknown_ids}")


ADAPTER_RESULT_FIELDS = frozenset(
    {
        "protocol_version",
        "repository",
        "pull_number",
        "head_sha",
        "summary",
        "reviewer",
        "status",
        "findings",
        "prior_dispositions",
        "comment_dispositions",
        "usage",
    }
)


def validate_adapter_result(
    value: object,
    *,
    expected_repository: str,
    expected_number: int,
    expected_head_sha: str,
    prior_ids: Iterable[str] = (),
    comment_ids: Iterable[str] = (),
    require_comment_dispositions: bool = True,
    prior_severities: dict[str, str] | None = None,
) -> AdapterResult:
    """Validate a reviewer result against its request.

    Every prior finding needs exactly one disposition. So does every open review comment the request listed,
    except that a repository reviewer that predates comment dispositions (require_comment_dispositions=False)
    may omit them all; when it gives any, they must cover every comment exactly once. A finding's `repeats`
    names another finding's candidate key or a prior finding ID (whose severity `prior_severities` gives).
    """
    prior_ids = list(prior_ids)
    result = _validate_result_envelope(value)
    _validate_result_request(result, expected_repository, expected_number, expected_head_sha)
    _validate_result_metadata(result)
    findings = _validate_result_findings(result.get("findings"))
    _validate_result_dispositions(result, prior_ids, comment_ids, require_comment_dispositions)
    _validate_result_repeats(findings, result.get("prior_dispositions", []), prior_severities or {}, set(prior_ids))
    READ.optional(READ.json_object, result.get("usage"), "adapter-result.usage")
    # Every field the shape gives a type was read above.
    return cast(AdapterResult, result)


def _validate_result_envelope(value: object) -> dict[str, Any]:
    """An object of known fields, in the protocol version this module reads."""
    result = READ.json_object(value, "adapter-result", optional=ADAPTER_RESULT_FIELDS)
    if READ.integer(result.get("protocol_version"), "adapter-result.protocol_version") != ADAPTER_PROTOCOL_VERSION:
        raise RecordError("Unsupported adapter protocol version")
    return result


def _validate_result_request(
    value: dict[str, Any], expected_repository: str, expected_number: int, expected_head_sha: str
) -> None:
    """The result answers the request: the same repository, pull request, and head."""
    repository = READ.string(value.get("repository"), "adapter-result.repository")
    if validate_repository_identity(repository) != expected_repository.lower():
        raise RecordError("Adapter result repository does not match request")
    if READ.integer(value.get("pull_number"), "adapter-result.pull_number", minimum=1) != expected_number:
        raise RecordError("Adapter result pull number does not match request")
    if READ.hex_digest(value.get("head_sha"), "adapter-result.head_sha", 40, 64) != expected_head_sha:
        raise RecordError("Adapter result head SHA does not match request")


def _validate_result_metadata(value: dict[str, Any]) -> None:
    READ.string(value.get("summary"), "adapter-result.summary")
    READ.string(value.get("reviewer"), "adapter-result.reviewer")
    READ.one_of(value.get("status"), "adapter-result.status", ("complete", "partial", "failed"))


def _validate_result_findings(value: object) -> list[dict[str, Any]]:
    """The findings, each checked in order; the first fault found is the one raised."""
    findings = READ.array(value, "adapter-result.findings")
    seen_keys: set[str] = set()
    for index, finding in enumerate(findings):
        _validate_result_finding(finding, f"adapter-result.findings[{index}]", seen_keys)
    return findings


def _validate_result_finding(finding: object, path: str, seen_keys: set[str]) -> None:
    """One finding, at `path`, whose candidate key must not be in `seen_keys`; the key is added to it."""
    finding = READ.json_object(finding, path, required=FINDING_FIELDS, optional=OPTIONAL_FINDING_FIELDS)
    key = READ.string(finding["candidate_key"], f"{path}.candidate_key")
    if key in seen_keys:
        raise RecordError("Adapter candidate keys must be unique non-empty strings")
    seen_keys.add(key)
    if "title" in finding:
        read_title(READ, finding["title"], f"{path}.title")
    if "analyzer" in finding:
        read_analyzer(READ, finding["analyzer"], f"{path}.analyzer")
    READ.one_of(finding["severity"], f"{path}.severity", SEVERITIES)
    for field in ("category", "path", "body", "evidence", "source"):
        READ.string(finding[field], f"{path}.{field}")
    if not safe_repository_path(finding["path"]):
        raise RecordError(f"Finding {key}.path must be a safe repository-relative path")
    READ.integer(finding["line"], f"{path}.line", minimum=1)


def _validate_result_dispositions(
    value: dict[str, Any], prior_ids: list[str], comment_ids: Iterable[str], require_comment_dispositions: bool
) -> None:
    """One disposition per prior finding, then the comment dispositions when given or required."""
    validate_dispositions(
        value.get("prior_dispositions", []),
        "finding_id",
        set(prior_ids),
        "Prior",
        path="adapter-result.prior_dispositions",
    )
    expected_comments = set(comment_ids)
    if "comment_dispositions" in value or (expected_comments and require_comment_dispositions):
        validate_dispositions(
            value.get("comment_dispositions", []),
            "comment_id",
            expected_comments,
            "Comment",
            path="adapter-result.comment_dispositions",
        )


def _validate_result_repeats(
    findings: list[dict[str, Any]],
    dispositions: list[dict[str, Any]],
    prior_severities: dict[str, str],
    prior_ids: set[str],
) -> None:
    """Each `repeats` names one finding by its candidate key or prior finding ID, as validate_repeat requires."""
    keys = {finding["candidate_key"]: finding for finding in findings}
    judged = {disposition["finding_id"]: disposition["disposition"] for disposition in dispositions}
    for index, finding in enumerate(findings):
        if "repeats" not in finding:
            continue
        key = finding["candidate_key"]
        target = READ.string(finding["repeats"], f"adapter-result.findings[{index}].repeats")
        linked = keys.get(target)
        # A finding that names its own key is refused for repeating itself, ahead of the key being ambiguous.
        if linked is not None and linked is not finding and target in prior_ids:
            raise RecordError(f"Finding {key}.repeats is ambiguous: {target} is a candidate key and a prior finding ID")
        if linked is None and target not in prior_ids:
            raise RecordError(f"Finding {key} repeats an unknown finding: {target}")
        validate_repeat(key, finding, linked, judged, prior_severities)


def validate_repeat(
    label: object,
    finding: dict[str, Any],
    linked: dict[str, Any] | None,
    judged: dict[str, str],
    prior_severities: dict[str, str],
) -> None:
    """One reviewer finding's `repeats` link, once its reference is resolved: `linked` is the finding of the same
    result it names, or None for a prior finding ID. A finding of the result must be another one and not itself a
    repeat; a prior finding must have been judged still present, at least in part, and have a known severity; either
    must be at least as severe. `label` names the finding in the message."""
    target = finding["repeats"]
    if linked is not None:
        if linked is finding:
            raise RecordError(f"Finding {label} cannot repeat itself")
        if "repeats" in linked:
            raise RecordError(f"Finding {label} repeats a repeat: link it to what {target} repeats instead")
        severity = linked["severity"]
    else:
        if judged.get(target) not in OPEN_DISPOSITIONS:
            raise RecordError(
                f"Finding {label} repeats prior finding {target}, so that finding's disposition must "
                "be still_present or partially_addressed"
            )
        severity = prior_severities.get(target)
        if severity not in SEVERITY_RANK:
            raise RecordError(f"Finding {label} repeats prior finding {target}, whose severity is unknown")
    if SEVERITY_RANK[severity] < SEVERITY_RANK[finding["severity"]]:
        raise RecordError(f"Finding {label} repeats a less severe finding: {target}")


def assign_finding_ids(findings: Iterable[AdapterFinding]) -> list[tuple[str, AdapterFinding]]:
    """Copies of a reviewer's findings in report order, each with the ID its record gives it."""
    ordered = sorted(
        (copy.deepcopy(item) for item in findings),
        key=lambda item: (item["path"].casefold(), item["line"], item["candidate_key"]),
    )
    return [(f"F{index:03d}", finding) for index, finding in enumerate(ordered, start=1)]


def _record_finding(identifier: str, finding: AdapterFinding, identifiers: dict[str, str], version: int) -> Finding:
    """A reviewer's finding as its record holds it: numbered `identifier`, with its `repeats` resolved to the finding of
    this review whose candidate key it names (`identifiers` gives each one's ID) or to a prior ledger entry."""
    recorded: Finding = {
        "id": identifier,
        "candidate_key": finding["candidate_key"],
        "severity": finding["severity"],
        "category": finding["category"],
        "path": finding["path"],
        "line": finding["line"],
        "body": finding["body"],
        "evidence": finding["evidence"],
        "source": finding["source"],
    }
    if "title" in finding:
        recorded["title"] = finding["title"]
    if "analyzer" in finding:
        recorded["analyzer"] = finding["analyzer"]
    if "location" in finding:
        recorded["location"] = finding["location"]
    if "repeats" in finding:
        target = finding["repeats"]
        prior = LEDGER_ID.fullmatch(target)
        unknown = RecordError(f"Finding {finding['candidate_key']} repeats an unknown finding: {target}")
        if target in identifiers:
            recorded["repeats"] = {"version": version, "id": identifiers[target]}
        elif prior is not None:
            recorded["repeats"] = {"version": whole_number.read(prior.group(1), unknown), "id": prior.group(2)}
        else:
            raise unknown
    return recorded


def _without_repeats(finding: Finding) -> Finding:
    """A finding with no `repeats` link."""
    unlinked = copy.copy(finding)
    unlinked.pop("repeats", None)
    return unlinked


def _reference(entry: LedgerEntry) -> FindingReference:
    """The finding that opened a ledger entry."""
    return {"version": entry["version"], "id": entry["id"]}


def ledger_id(version: int, identifier: str) -> str:
    """A ledger entry's ID, `v<version>:<finding ID>`: the prior-finding ID a re-review disposes."""
    return f"v{version}:{identifier}"


def display_id(version: int, identifier: str) -> str:
    """A finding's ID as reports show it, `v1 F001`, since finding IDs restart in every review."""
    return f"v{version} {identifier}"


def ledger_display_id(entry_id: str) -> str:
    """A ledger entry's ID, as `ledger_id` writes it, in the form `display_id` gives."""
    version, identifier = entry_id.removeprefix("v").split(":", 1)
    return display_id(int(version), identifier)


def finding_reference(repository: str, pull: int, version: int, identifier: str) -> str:
    """The one reference to a finding across reviews, `owner/repo#12 v1 F001`, its repository lowercased as GitHub
    compares it."""
    return f"{repository.lower()}#{pull} {display_id(version, identifier)}"


SOURCE_SEPARATOR = " + "


def joined_source(sources: Iterable[str]) -> str:
    """A merged finding's `source`: the reviewers that raised it, in order, as `source_parts` reads them back."""
    return SOURCE_SEPARATOR.join(sources)


def source_parts(source: str) -> list[str]:
    """The reviewers a finding's `source` names, as `joined_source` joins them: each part trimmed and named once, in
    order, with an empty part dropped."""
    return list(dict.fromkeys(part.strip() for part in source.split(SOURCE_SEPARATOR) if part.strip()))


def _entry_key(entry: LedgerEntry) -> tuple[int, int]:
    return entry["version"], int(entry["id"][1:])


def entry_state(entry: LedgerEntry) -> tuple[LedgerState, int]:
    """An entry's state and the version that last judged it. Raising or repeating the finding judges it open;
    otherwise its latest disposition decides."""
    raised = [entry["version"], *(repeat["version"] for repeat in entry["repeats"])]
    latest = max([*raised, *(item["version"] for item in entry["dispositions"])])
    if latest in raised:
        return "open", latest
    return LEDGER_STATES[entry["dispositions"][-1]["disposition"]], latest


def extend_ledger(
    prior_ledger: Iterable[LedgerEntry],
    *,
    version: int,
    findings: Iterable[Finding],
    prior_dispositions: Iterable[LedgerDisposition],
) -> list[LedgerEntry]:
    """The ledger after a review: each disposition judges its entry, each finding without `repeats` opens an
    entry, and each repeat joins the entry of the finding it repeats."""
    ledger = copy.deepcopy(list(prior_ledger))
    entries = {ledger_id(entry["version"], entry["id"]): entry for entry in ledger}
    for disposition in prior_dispositions:
        judged = entries.get(disposition["finding_id"])
        if judged is None:
            raise RecordError(f"Prior disposition {disposition['finding_id']} names no ledger entry")
        judged["dispositions"].append({"version": version, "disposition": disposition["disposition"]})
    findings = list(findings)
    for finding in findings:
        if "repeats" not in finding:
            opened: LedgerEntry = {
                "version": version,
                "id": finding["id"],
                "severity": finding["severity"],
                "category": finding["category"],
                "state": "open",
                "judged_in": version,
                "dispositions": [],
                "repeats": [],
            }
            ledger.append(opened)
            entries[ledger_id(version, finding["id"])] = opened
    for finding in findings:
        if "repeats" in finding:
            holder = entries.get(ledger_id(finding["repeats"]["version"], finding["repeats"]["id"]))
            if holder is None:
                raise RecordError(f"Finding {finding['id']} repeats a finding with no ledger entry")
            holder["repeats"].append({"version": version, "id": finding["id"]})
    for entry in ledger:
        entry["state"], entry["judged_in"] = entry_state(entry)
    return sorted(ledger, key=_entry_key)


def _legacy_ledger(base: list[LedgerEntry], record: ReviewRecord, compared: int) -> list[LedgerEntry]:
    """The ledger of a re-review recorded before ledgers. Its dispositions name bare finding IDs of the version it
    compared with; an ID that names no entry there is skipped. Its findings carry no links."""
    holders: dict[tuple[int, str], str] = {}
    for entry in base:
        for occurrence in (_reference(entry), *entry["repeats"]):
            holders[(occurrence["version"], occurrence["id"])] = ledger_id(entry["version"], entry["id"])
    dispositions: dict[str, LedgerDisposition] = {}
    for disposition in record["prior_dispositions"]:
        holder = holders.get((compared, disposition["finding_id"]))
        if holder is not None:
            dispositions.setdefault(holder, {"finding_id": holder, "disposition": disposition["disposition"]})
    findings = [_without_repeats(finding) for finding in record["findings"]]
    return extend_ledger(
        base, version=record["review"]["version"], findings=findings, prior_dispositions=dispositions.values()
    )


def ledger_history(records: Iterable[ReviewRecord]) -> dict[int, list[LedgerEntry]]:
    """Each version's ledger for one pull request's validated records: the ledger a record stores, or for a record
    written before ledgers, one computed from its findings and dispositions. An initial review starts afresh."""
    history: dict[int, list[LedgerEntry]] = {}
    previous: int | None = None
    for record in sorted(records, key=lambda item: item["review"]["version"]):
        review = record["review"]
        if "ledger" in record:
            ledger = copy.deepcopy(record["ledger"])
        elif review["mode"] == "initial" or previous is None:
            ledger = _legacy_ledger([], record, 0)
        else:
            scope = review.get("scope")
            ledger = _legacy_ledger(history[previous], record, scope["since_version"] if scope else previous)
        history[review["version"]] = ledger
        previous = review["version"]
    return history


def carried_findings(records: Iterable[ReviewRecord], flags: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """The prior findings a re-review disposes: every open or unverified entry of the latest ledger, where it was
    last reported. A finding a review only judged stays here until a review closes it. An entry with flags in the
    flag store (`flags`) carries each one's ID, category, and rationale, so the reviewer can weigh it."""
    records = list(records)
    if not records:
        return []
    history = ledger_history(records)
    by_version: dict[int, ReviewRecord] = {record["review"]["version"]: record for record in records}
    latest = by_version[max(history)]
    flagged = flagged_entries(history[max(history)], flags, latest["repository"], latest["pull_request"]["number"])
    carried = []
    for entry in history[max(history)]:
        if entry["state"] == "closed":
            continue
        where = entry["repeats"][-1] if entry["repeats"] else _reference(entry)
        raised = by_version.get(where["version"])
        finding = next(
            (item for item in (raised["findings"] if raised is not None else []) if item["id"] == where["id"]),
            None,
        )
        if finding is None:
            raise RecordError(f"Review v{where['version']} with finding {where['id']} is missing from the archive")
        identifier = ledger_id(entry["version"], entry["id"])
        flags_on = [
            {"id": flag["id"], "category": flag["category"], "rationale": flag["body"]}
            for flag in flagged.get(identifier, [])
        ]
        carried.append(
            {
                "id": identifier,
                "severity": entry["severity"],
                "category": entry["category"],
                "path": finding["path"],
                "line": finding["line"],
                **({"title": finding["title"]} if "title" in finding else {}),
                "body": finding["body"],
                **({"flags": flags_on} if flags_on else {}),
            }
        )
    return carried


def ledger_summary(ledger: Iterable[LedgerEntry], version: int) -> dict[str, Any]:
    """Open entries by severity, entries addressed, the earliest version an open entry dates from, and the version
    whose ledger this is. For a record written before ledgers, pass the ledger `ledger_history` computes: such a
    re-review's counts cover only the findings it raised, not a prior one it judged still present."""
    opened = dict.fromkeys(sorted(SEVERITIES), 0)
    addressed = 0
    since: int | None = None
    for entry in ledger:
        if entry["state"] == "open":
            opened[entry["severity"]] += 1
            since = entry["version"] if since is None else min(since, entry["version"])
        elif entry["state"] == "closed" and entry["dispositions"][-1]["disposition"] == "addressed":
            addressed += 1
    return {"open": opened, "addressed": addressed, "since": since, "version": version}


def flagged_entries(
    ledger: Iterable[LedgerEntry], flags: Iterable[dict[str, Any]], repository: str, number: int
) -> dict[str, list[dict[str, Any]]]:
    """The flags naming each ledger entry's findings, keyed by ledger ID: a flag names the finding where the entry
    first appeared or one of its repeats, by review version and finding ID. A resolved flag still counts, because
    resolving one records that the improvement was handled, not that the finding stood."""
    holders: dict[tuple[int, str], str] = {}
    for entry in ledger:
        for occurrence in (_reference(entry), *entry["repeats"]):
            holders[(occurrence["version"], occurrence["id"])] = ledger_id(entry["version"], entry["id"])
    flagged: dict[str, list[dict[str, Any]]] = {}
    for flag in flags:
        if (flag["repository"] or "").lower() != repository.lower() or flag["pull_number"] != number:
            continue
        holder = holders.get((flag["review_version"], flag["finding_id"]))
        if holder is not None:
            flagged.setdefault(holder, []).append(flag)
    return flagged


def calculate_verdict(
    ledger: Iterable[LedgerEntry], policy: VerdictPolicy, unavailable_sources: Iterable[str] = ()
) -> Verdict:
    """CHANGES_REQUESTED when the open ledger entries require it; else INCOMPLETE when changed source could not be
    reviewed in full; else APPROVED. A blocking finding is never hidden by a coverage gap. A finding carried from an
    earlier review counts until a review closes it, and a linked repeat counts once, as its entry."""
    severities = [entry["severity"] for entry in ledger if entry["state"] == "open"]
    if any(severity in policy.request_changes_for for severity in severities):
        return "CHANGES_REQUESTED"
    if severities.count("SHOULD_FIX") >= policy.should_fix_threshold:
        return "CHANGES_REQUESTED"
    if list(unavailable_sources):
        return "INCOMPLETE"
    return "APPROVED"


def build_record(
    request: dict[str, Any],
    adapter_result: AdapterResult,
    *,
    version: int,
    policy: dict[str, Any],
    reviewed_at: str | None = None,
    prior_ledger: Iterable[LedgerEntry] | None = None,
) -> ReviewRecord:
    """A review record. A re-review passes the latest record's ledger as `prior_ledger`, which this review's
    dispositions judge and its findings extend; an initial review starts a fresh ledger."""
    numbered = assign_finding_ids(adapter_result["findings"])
    identifiers = {finding["candidate_key"]: identifier for identifier, finding in numbered}
    findings = [_record_finding(identifier, finding, identifiers, version) for identifier, finding in numbered]
    ledger = extend_ledger(
        prior_ledger or [],
        version=version,
        findings=findings,
        prior_dispositions=adapter_result.get("prior_dispositions", []),
    )
    counts = {severity: sum(item["severity"] == severity for item in findings) for severity in sorted(SEVERITIES)}
    unavailable = sorted(request.get("unavailable_sources", []))
    adapter: Adapter = {
        "name": request["adapter"]["name"],
        "protocol_version": ADAPTER_PROTOCOL_VERSION,
        "scope": request["adapter"]["scope"],
        "source_commit": request["adapter"].get("source_commit"),
        "source_hashes": request["adapter"].get("source_hashes", {}),
        "reviewer": adapter_result["reviewer"],
        "status": adapter_result["status"],
        "usage": adapter_result.get("usage"),
    }
    # Absent from runs prepared before the source was recorded.
    if "source" in request["adapter"]:
        adapter["source"] = request["adapter"]["source"]
    record: ReviewRecord = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "repository": request["repository"],
        "pull_request": {
            "number": request["pull_number"],
            "url": request["pull_url"],
            "title": request["title"],
            "base_ref": request["base_ref"],
            "base_sha": request["base_sha"],
            "head_sha": request["head_sha"],
        },
        "review": {
            "version": version,
            "mode": request["mode"],
            "reviewed_at": reviewed_at or datetime.now(UTC).isoformat(),
            "summary": adapter_result["summary"],
            "verdict": calculate_verdict(ledger, verdict_policy(policy), unavailable),
            "counts": counts,
            "adapter": adapter,
        },
        "findings": findings,
        "prior_dispositions": copy.deepcopy(adapter_result.get("prior_dispositions", [])),
        "ledger": ledger,
    }
    if request.get("head_ref"):
        record["pull_request"]["head_ref"] = request["head_ref"]
    if request.get("body_characters") is not None:
        record["pull_request"]["body_characters"] = request["body_characters"]
        record["pull_request"]["body_given"] = request["body_given"]
    coverage = _record_coverage(request)
    if coverage is not None:
        record["review"]["coverage"] = coverage
    if request.get("reviewers"):
        record["review"]["reviewers"] = copy.deepcopy(request["reviewers"])
    if request.get("patches"):
        record["review"]["patches"] = copy.deepcopy(request["patches"])
    if request.get("scope"):
        record["review"]["scope"] = copy.deepcopy(request["scope"])
    if request.get("dispatch"):
        record["review"]["dispatch"] = request["dispatch"]
    if request.get("snapshot"):
        record["review"]["snapshot"] = copy.deepcopy(request["snapshot"])
    if adapter_result.get("comment_dispositions"):
        record["github_comments"] = [_comment(comment) for comment in request.get("github_comments", [])]
        record["comment_dispositions"] = copy.deepcopy(adapter_result["comment_dispositions"])
    return record


def _comment(comment: dict[str, Any]) -> Comment:
    """An open review comment as a record holds it: COMMENT_FIELDS, from the comment the request listed."""
    return {
        "id": comment["id"],
        "author": comment["author"],
        "path": comment["path"],
        "line": comment["line"],
        "outdated": comment["outdated"],
        "body": comment["body"],
        "url": comment["url"],
    }


def _record_coverage(request: dict[str, Any]) -> Coverage | None:
    """The record's coverage, or None when none of its lists has a file."""
    unavailable = sorted(request.get("unavailable_sources", []))
    uncovered = sorted(request.get("uncovered_files", []))
    unreviewed = dict(sorted(request.get("unreviewed_files", {}).items()))
    if not (unavailable or uncovered or unreviewed):
        return None
    coverage: Coverage = {"unavailable_sources": unavailable}
    if uncovered:
        # Files no specialist covers, which the reviewer manifest left unreviewed. A deliberate opt-out, so they do
        # not make the review INCOMPLETE.
        coverage["uncovered_files"] = uncovered
    if unreviewed:
        # Binary files of a type the review does not read, with the suffix that matched; they do not make the review
        # INCOMPLETE either.
        coverage["unreviewed_files"] = unreviewed
    return coverage


def _size(count: int) -> str:
    """A byte count as `512 B`, `3.4 KiB`, or `204.0 MiB`."""
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f} KiB"
    return f"{count / 2**20:.1f} MiB"


def _files_read(reviewer: ReviewerRun) -> str:
    """A reviewer's distinct snapshot files read and their size, `unknown` when no guard counted them, or `-` in a
    record written before reads were counted."""
    if "files_read" not in reviewer:
        return "-"
    files_read, bytes_read = reviewer["files_read"], reviewer.get("bytes_read")
    if files_read is None or bytes_read is None:
        return "unknown"
    return f"{files_read} ({_size(bytes_read)})"


def snapshot_seconds(seconds: dict[str, float]) -> dict[str, float]:
    """Each phase's seconds as a review records them: to a tenth of a second, and never below zero."""
    return {phase: round(max(seconds[phase], 0.0), 1) for phase in SNAPSHOT_PHASES}


def describe_snapshot(snapshot: Snapshot) -> str:
    """A review's source snapshot in one line: its source, size, the paths it left out by reason, and the seconds of
    each phase of prepare."""
    seconds = ", ".join(f"{phase} {snapshot['seconds'][phase]:.1f}s" for phase in SNAPSHOT_PHASES)
    excluded = snapshot.get("excluded") or {}
    left_out = (
        " (excluded: " + ", ".join(f"{reason} {count:,}" for reason, count in excluded.items()) + ")"
        if excluded
        else ""
    )
    return f"{snapshot['source']}: {snapshot['files']:,} files, {_size(snapshot['bytes'])}{left_out}; {seconds}"


def describe_reviewer_source(adapter: Adapter) -> str:
    """Where the reviewer that ran came from, in one line, from a record's adapter that names its source."""
    commit = _code(str(adapter["source_commit"])[:12])
    return {
        "generic": "the suite's generic reviewer",
        "trusted-ref": f"the configured trusted ref, at {commit}",
        "base": f"the pull request's base, {commit}",
        "default-branch": f"the default branch's tip, {commit}, because the base predates the review skill",
        "generic-fallback": "the suite's generic reviewer, in place of a review skill the base predates",
    }[adapter["source"]]


def _duration(seconds: int | None) -> str:
    """A reviewer's time as `4m 05s`, or `-` when it was not timed."""
    if seconds is None:
        return "-"
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}m {remainder:02d}s" if minutes else f"{remainder}s"


def read_repository_path(read: FieldReader, value: object, path: str) -> str:
    """A repository file's path as git names it: any string but an empty one, a blank one included, since git takes a
    file named by Unicode whitespace alone, such as a no-break space."""
    if not isinstance(value, str) or not value:
        raise read.fail(path, "a non-empty string", describe(value) if value != "" else "an empty string")
    return value


def _read_path_set(value: object, path: str) -> list[str]:
    """A list of distinct non-empty paths."""
    paths = READ.array(value, path, partial(read_repository_path, READ))
    if len(set(paths)) != len(paths):
        raise RecordError("Review coverage is malformed")
    return paths


def _validate_timestamp(value: object) -> None:
    """An ISO 8601 timestamp string, as a review's reviewed_at must be."""
    try:
        datetime.fromisoformat(READ.string(value, "record.review.reviewed_at"))
    except FieldError:
        raise
    except ValueError as exc:
        raise RecordError("Review timestamp is invalid") from exc


def _validate_reviewers(value: object) -> None:
    """The reviewers that ran: which files each covered, what it found, how often it was retried, and how
    long it took."""
    seen: set[str] = set()
    for index, reviewer in enumerate(READ.array(value, "record.review.reviewers", nonempty=True)):
        path = f"record.review.reviewers[{index}]"
        READ.json_object(reviewer, path, required=REVIEWER_FIELDS, optional=OPTIONAL_REVIEWER_FIELDS)
        identifier = READ.string(reviewer["id"], f"{path}.id")
        if identifier in seen:
            raise RecordError("Review reviewer IDs must be unique non-empty strings")
        seen.add(identifier)
        READ.string(reviewer["category"], f"{path}.category")
        for field in ("files", "findings", "retries"):
            READ.integer(reviewer[field], f"{path}.{field}", minimum=0)
        READ.boolean(reviewer["dispositions_only"], f"{path}.dispositions_only")
        if "seconds" in reviewer:
            READ.integer(reviewer["seconds"], f"{path}.seconds", minimum=0)
        if "model" in reviewer:
            read_model(READ, reviewer["model"], f"{path}.model")
        _validate_read_counts(reviewer, path)


def _validate_read_counts(reviewer: dict[str, Any], path: str) -> None:
    """A reviewer's read counts: both absent, both null (not counted), or both non-negative integers."""
    present = [field for field in READ_COUNT_FIELDS if field in reviewer]
    if not present:
        return
    if len(present) != len(READ_COUNT_FIELDS) or len({reviewer[field] is None for field in READ_COUNT_FIELDS}) > 1:
        raise RecordError(
            f"Review reviewer {reviewer['id']} files_read and bytes_read must both be non-negative integers "
            "or both null"
        )
    for field in READ_COUNT_FIELDS:
        READ.optional(READ.integer, reviewer[field], f"{path}.{field}", minimum=0)


def _validate_snapshot(value: object) -> None:
    """The source snapshot's origin, file count, byte total, exclusions by reason, the seconds of each phase of
    prepare, and where the diff came from when GitHub's diff endpoint refused it."""
    path = "record.review.snapshot"
    snapshot = READ.json_object(value, path, required=SNAPSHOT_FIELDS, optional=OPTIONAL_SNAPSHOT_FIELDS)
    excluded = READ.json_object(snapshot.get("excluded", {}), f"{path}.excluded")
    for key, count in excluded.items():
        reason = READ.string(key, f"a key of {path}.excluded")
        if not EXCLUSION_REASON.fullmatch(reason):
            raise RecordError("Review snapshot excluded must give a positive count for each exclusion reason")
        READ.integer(count, f"{path}.excluded.{reason}", minimum=1)
    READ.one_of(snapshot["source"], f"{path}.source", SNAPSHOT_SOURCES)
    if "diff" in snapshot:
        diff = READ.json_object(snapshot["diff"], f"{path}.diff", required=DIFF_FIELDS)
        READ.one_of(diff["source"], f"{path}.diff.source", tuple(DIFF_SOURCES))
        if not DIFF_LIMIT.fullmatch(READ.string(diff["limit"], f"{path}.diff.limit")):
            raise RecordError("Review snapshot diff limit must be a count and what it counts, as in 300 files")
    READ.integer(snapshot["files"], f"{path}.files", minimum=0)
    READ.integer(snapshot["bytes"], f"{path}.bytes", minimum=0)
    seconds = READ.json_object(snapshot["seconds"], f"{path}.seconds", required=SNAPSHOT_PHASES)
    for phase in SNAPSHOT_PHASES:
        if READ.number(seconds[phase], f"{path}.seconds.{phase}") < 0:
            raise RecordError(f"Review snapshot seconds must give {', '.join(SNAPSHOT_PHASES)} as non-negative numbers")


def _validate_patches(value: object) -> None:
    """Each changed file's patch fingerprint, so the next re-review can tell which files changed since."""
    patches = READ.json_object(value, "record.review.patches")
    if not patches:
        raise RecordError("Review patches must be a non-empty object")
    for key, patch in patches.items():
        path = READ.string(key, "a key of record.review.patches")
        if not safe_repository_path(path):
            raise RecordError("Review patch path is unsafe")
        item = f"record.review.patches.{path}"
        READ.json_object(patch, item, required=PATCH_FIELDS)
        READ.hex_digest(patch["sha256"], f"{item}.sha256", 64)
        READ.integer(patch["lines"], f"{item}.lines", minimum=0)


def _validate_scope(value: object, review: dict[str, Any]) -> None:
    if review["mode"] != "re-review":
        raise RecordError("Only a re-review has a scope")
    path = "record.review.scope"
    scope = READ.json_object(value, path, required=SCOPE_FIELDS)
    READ.one_of(scope["requested"], f"{path}.requested", RE_REVIEW_SCOPES)
    used = READ.one_of(scope["used"], f"{path}.used", ("full", "incremental"))
    READ.string(scope["reason"], f"{path}.reason")
    since = READ.integer(scope["since_version"], f"{path}.since_version")
    if not 1 <= since < review["version"]:
        raise RecordError("Review scope.since_version must be an earlier version")
    files_total = READ.integer(scope["files_total"], f"{path}.files_total", minimum=0)
    lines_total = READ.integer(scope["lines_total"], f"{path}.lines_total", minimum=0)
    files_changed = READ.optional(READ.integer, scope["files_changed"], f"{path}.files_changed", minimum=0)
    lines_changed = READ.optional(READ.integer, scope["lines_changed"], f"{path}.lines_changed", minimum=0)
    if files_changed is None or lines_changed is None:
        compared = False
        if (files_changed is None) != (lines_changed is None):
            raise RecordError("Review scope changed counts must both be null or both be within their totals")
    else:
        compared = True
        if files_changed > files_total or lines_changed > lines_total:
            raise RecordError("Review scope changed counts must both be null or both be within their totals")
    if used == "incremental" and not compared:
        raise RecordError("An incremental re-review must have compared with an earlier version")


def describe_scope(scope: Scope) -> str:
    """One line on how much of a pull request a re-review covered, and why."""
    if scope["files_changed"] is None:
        compared = f"could not compare with v{scope['since_version']}"
    else:
        compared = (
            f"{scope['files_changed']} of {scope['files_total']} files and {scope['lines_changed']} of "
            f"{scope['lines_total']} changed lines differ from v{scope['since_version']}"
        )
    return f"{scope['used']}, {compared} (requested {scope['requested']}: {scope['reason']})"


def _validate_comments(record: dict[str, Any]) -> None:
    """Open review comments and their dispositions come together: exactly one disposition per comment."""
    comments = record.get("github_comments")
    dispositions = record.get("comment_dispositions")
    if comments is None and dispositions is None:
        return
    if comments is None:
        raise RecordError("Review comment dispositions need the comments they answer")
    ids: set[str] = set()
    for index, comment in enumerate(READ.array(comments, "record.github_comments", nonempty=True)):
        path = f"record.github_comments[{index}]"
        READ.json_object(comment, path, required=COMMENT_FIELDS)
        identifier = READ.string(comment["id"], f"{path}.id")
        if not re.fullmatch(r"C[1-9][0-9]*", identifier) or identifier in ids:
            raise RecordError("Review comment IDs must be unique C<n> values")
        ids.add(identifier)
        for field in ("author", "path", "url"):
            READ.string(comment[field], f"{path}.{field}")
        # GitHub may leave a comment's body empty, as its reader keeps it.
        READ.string(comment["body"], f"{path}.body", empty=True)
        READ.optional(READ.integer, comment["line"], f"{path}.line", minimum=1)
        READ.boolean(comment["outdated"], f"{path}.outdated")
    validate_dispositions(dispositions, "comment_id", ids, "Comment", path="record.comment_dispositions")


def _read_reference(value: object, path: str, latest: int, malformed: str) -> dict[str, Any]:
    """A `{version, id}` reference to a finding in this review or an earlier one; `malformed` is the error for one
    that names no such finding."""
    reference = READ.json_object(value, path, required={"version", "id"})
    version = READ.integer(reference["version"], f"{path}.version")
    identifier = READ.string(reference["id"], f"{path}.id")
    if not 1 <= version <= latest or not FINDING_ID.fullmatch(identifier):
        raise RecordError(malformed)
    return reference


def _validate_ledger_entry(value: object, path: str, version: int) -> LedgerEntry:
    entry = READ.json_object(value, path, required=LEDGER_FIELDS)
    entry_version = READ.integer(entry["version"], f"{path}.version")
    identifier = READ.string(entry["id"], f"{path}.id")
    if entry_version < 1 or not FINDING_ID.fullmatch(identifier):
        raise RecordError("Review ledger entry fields are malformed")
    label = ledger_id(entry_version, identifier)
    if entry_version > version:
        raise RecordError(f"Review ledger entry {label} is after this review")
    READ.one_of(entry["severity"], f"{path}.severity", SEVERITIES)
    READ.string(entry["category"], f"{path}.category")
    dispositions = READ.array(entry["dispositions"], f"{path}.dispositions")
    for index, item in enumerate(dispositions):
        item_path = f"{path}.dispositions[{index}]"
        READ.json_object(item, item_path, required={"version", "disposition"})
        READ.integer(item["version"], f"{item_path}.version", minimum=0)
        READ.one_of(item["disposition"], f"{item_path}.disposition", DISPOSITIONS)
    versions = [item["version"] for item in dispositions]
    if versions != sorted(set(versions)) or any(not entry_version < item <= version for item in versions):
        raise RecordError(f"Review ledger entry {label} dispositions are malformed")
    malformed = f"Review ledger entry {label} repeats are malformed"
    repeats = READ.array(entry["repeats"], f"{path}.repeats")
    for index, item in enumerate(repeats):
        if _read_reference(item, f"{path}.repeats[{index}]", version, malformed)["version"] < entry_version:
            raise RecordError(malformed)
    if len({(item["version"], item["id"]) for item in repeats}) != len(repeats):
        raise RecordError(malformed)
    # Every field the shape gives a type was read above but the state and the version that judged it, which are
    # checked against what the rest gives.
    read = cast(LedgerEntry, entry)
    state, judged_in = entry_state(read)
    if READ.one_of(entry["state"], f"{path}.state", LEDGER_STATE_NAMES) != state:
        raise RecordError(f"Review ledger entry {label} state must be {state}")
    if READ.integer(entry["judged_in"], f"{path}.judged_in") != judged_in:
        raise RecordError(f"Review ledger entry {label} judged_in must be {judged_in}")
    return read


def _validate_ledger(record: dict[str, Any], version: int, mode: str) -> None:
    """The ledger agrees with this review: its unlinked findings opened the entries of this version, its prior
    dispositions are the entries' judgments in this version, and each repeat is in the entry of what it repeats.
    What earlier reviews contributed cannot be checked without them, so it is checked when it is computed."""
    findings = {finding["id"]: finding for finding in record["findings"]}
    ledger = record.get("ledger")
    if ledger is None:
        if any("repeats" in finding for finding in findings.values()):
            raise RecordError("Review finding repeats need a ledger")
        return
    entries = _ledger_entries(ledger, version, mode)
    _validate_opened_entries(entries, findings, version)
    given = _validate_ledger_judgments(ledger, record["prior_dispositions"], version)
    linked = [
        (identifier, _validate_repeat_link(identifier, finding, findings, entries, given, version))
        for identifier, finding in findings.items()
        if "repeats" in finding
    ]
    _validate_listed_repeats(ledger, linked, version)


def _ledger_entries(value: object, version: int, mode: str) -> dict[LedgerKey, LedgerEntry]:
    """The ledger's entries by key: each well formed and unique, in order, and all of this version in an initial
    review."""
    ledger: list[LedgerEntry] = []
    entries: dict[LedgerKey, LedgerEntry] = {}
    for index, item in enumerate(READ.array(value, "record.ledger")):
        entry = _validate_ledger_entry(item, f"record.ledger[{index}]", version)
        ledger.append(entry)
        if (entry["version"], entry["id"]) in entries:
            raise RecordError("Review ledger entries must be unique")
        entries[(entry["version"], entry["id"])] = entry
    if [_entry_key(entry) for entry in ledger] != sorted(_entry_key(entry) for entry in ledger):
        raise RecordError("Review ledger entries must be ordered by version and finding ID")
    if mode == "initial" and any(entry["version"] != version for entry in ledger):
        raise RecordError("An initial review starts a fresh ledger")
    return entries


def _validate_opened_entries(
    entries: dict[LedgerKey, LedgerEntry], findings: dict[str, dict[str, Any]], version: int
) -> None:
    """The entries of this version are exactly this review's findings without repeats, each with its severity and
    category."""
    for identifier, finding in findings.items():
        if "repeats" in finding:
            continue
        entry = entries.get((version, identifier))
        if entry is None:
            raise RecordError(f"Review ledger entry for {identifier} is missing")
        if (entry["severity"], entry["category"]) != (finding["severity"], finding["category"]):
            raise RecordError(f"Review ledger entry for {identifier} does not match the finding")
    if {key[1] for key in entries if key[0] == version} != {i for i, f in findings.items() if "repeats" not in f}:
        raise RecordError("Review ledger entries of this version must be its findings without repeats")


def _validate_ledger_judgments(
    ledger: list[LedgerEntry], prior_dispositions: list[dict[str, Any]], version: int
) -> dict[str, str]:
    """The prior dispositions are the ledger's judgments in this version. Returns them by ledger ID."""
    judged = {
        ledger_id(entry["version"], entry["id"]): item["disposition"]
        for entry in ledger
        for item in entry["dispositions"]
        if item["version"] == version
    }
    given = {item["finding_id"]: item["disposition"] for item in prior_dispositions}
    if judged != given:
        raise RecordError("Review prior dispositions do not match the ledger")
    return given


def _validate_repeat_link(
    identifier: str,
    finding: dict[str, Any],
    findings: dict[str, dict[str, Any]],
    entries: dict[LedgerKey, LedgerEntry],
    given: dict[str, str],
    version: int,
) -> LedgerKey:
    """A finding's `repeats` names a finding at least as severe, whose ledger entry lists the repeat. Returns that
    entry's key."""
    target = _read_reference(
        finding["repeats"],
        f"record.findings[{int(identifier[1:]) - 1}].repeats",
        version,
        f"Review finding {identifier}.repeats is malformed",
    )
    key = (target["version"], target["id"])
    if target["version"] == version:
        severity = _same_review_target_severity(identifier, target["id"], findings)
    else:
        severity = _earlier_target_severity(identifier, key, entries, given)
    if SEVERITY_RANK[severity] < SEVERITY_RANK[finding["severity"]]:
        raise RecordError(f"Review finding {identifier} repeats a less severe finding")
    if {"version": version, "id": identifier} not in entries[key]["repeats"]:
        raise RecordError(f"Review finding {identifier} is not in its target's ledger entry")
    return key


def _same_review_target_severity(identifier: str, target_id: str, findings: dict[str, dict[str, Any]]) -> Severity:
    """The severity of the finding of this review that `identifier` repeats: another finding, not itself a repeat."""
    if target_id == identifier:
        raise RecordError(f"Review finding {identifier} cannot repeat itself")
    other = findings.get(target_id)
    if other is None:
        raise RecordError(f"Review finding {identifier} repeats an unknown finding: {target_id}")
    if "repeats" in other:
        raise RecordError(f"Review finding {identifier} repeats a repeat: {target_id}")
    severity: Severity = other["severity"]
    return severity


def _earlier_target_severity(
    identifier: str, key: LedgerKey, entries: dict[LedgerKey, LedgerEntry], given: dict[str, str]
) -> Severity:
    """The severity of the earlier finding that `identifier` repeats: an entry this review judged still open."""
    entry = entries.get(key)
    if entry is None:
        raise RecordError(f"Review finding {identifier} repeats an unknown finding: {ledger_id(*key)}")
    if given.get(ledger_id(*key)) not in OPEN_DISPOSITIONS:
        raise RecordError(
            f"Review finding {identifier} repeats {ledger_id(*key)}, so its disposition must "
            "be still_present or partially_addressed"
        )
    return entry["severity"]


def _validate_listed_repeats(ledger: list[LedgerEntry], linked: list[tuple[str, LedgerKey]], version: int) -> None:
    """The repeats of this version that the entries list are exactly this review's linked findings."""
    listed = [
        (item["id"], (entry["version"], entry["id"]))
        for entry in ledger
        for item in entry["repeats"]
        if item["version"] == version
    ]
    if sorted(listed) != sorted(linked):
        raise RecordError("Review ledger repeats of this version must be its findings with repeats")


RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "repository",
        "pull_request",
        "review",
        "findings",
        "prior_dispositions",
        "github_comments",
        "comment_dispositions",
        "artifacts",
        "ledger",
    }
)


def _validate_pull_request(pull: dict[str, Any]) -> None:
    pull_fields = {"number", "url", "title", "base_ref", "base_sha", "head_sha"}
    READ.json_object(
        pull, "record.pull_request", required=pull_fields, optional={"head_ref", "body_characters", "body_given"}
    )
    if ("body_characters" in pull) != ("body_given" in pull):
        raise RecordError("Review pull-request fields are malformed")
    if "head_ref" in pull:
        READ.string(pull["head_ref"], "record.pull_request.head_ref")
    characters = READ.integer(pull.get("body_characters", 0), "record.pull_request.body_characters", minimum=0)
    given = READ.integer(pull.get("body_given", 0), "record.pull_request.body_given", minimum=0)
    if given > characters:
        raise RecordError("Review pull_request.body_given is more than body_characters")
    READ.integer(pull["number"], "record.pull_request.number", minimum=1)
    for field in ("url", "title", "base_ref"):
        READ.string(pull[field], f"record.pull_request.{field}")
    READ.hex_digest(pull["base_sha"], "record.pull_request.base_sha", 40, 64)
    READ.hex_digest(pull["head_sha"], "record.pull_request.head_sha", 40, 64)


def _validate_coverage(review: dict[str, Any], version: int) -> None:
    """The sources a review could not reach, the files no reviewer covered, and, from version 2, the files of a type
    the review does not read; an INCOMPLETE review names a source."""
    # Records written before uncovered files were recorded omit them, and version 1 records have no unreviewed files.
    coverage = READ.json_object(
        review.get("coverage", {"unavailable_sources": []}),
        "record.review.coverage",
        required={"unavailable_sources"},
        optional={"uncovered_files", *(["unreviewed_files"] if version >= 2 else [])},
    )
    unavailable = _read_path_set(coverage["unavailable_sources"], "record.review.coverage.unavailable_sources")
    _read_path_set(coverage.get("uncovered_files", []), "record.review.coverage.uncovered_files")
    _validate_unreviewed(coverage.get("unreviewed_files", {}), set(unavailable))
    if review["verdict"] == "INCOMPLETE" and not unavailable:
        raise RecordError("An INCOMPLETE review must list its unavailable sources")


def _validate_unreviewed(value: object, unavailable: set[str]) -> None:
    """Each unreviewed file keyed by its path, which no unavailable source repeats, with the suffix its name ends in."""
    field = "record.review.coverage.unreviewed_files"
    for path, suffix in READ.json_object(value, field).items():
        READ.string(path, f"a key of {field}")
        READ.string(suffix, f"{field}.{path}")
        name = path.rpartition("/")[2].lower()
        if not is_unreviewed_suffix(suffix) or len(name) <= len(suffix) or not name.endswith(suffix):
            raise READ.fail(f"{field}.{path}", "a lowercase file-name suffix the path's name ends in", describe(suffix))
        if path in unavailable:
            raise RecordError(f"Review coverage names {path} both as unavailable and as unreviewed")


def _validate_review(review: dict[str, Any], version: int) -> None:
    """The review's own metadata, up to its adapter."""
    review_fields = {"version", "mode", "reviewed_at", "summary", "verdict", "counts", "adapter"}
    # Records written before patches, scopes, dispatch modes, or snapshots were recorded omit them.
    optional = {"coverage", "reviewers", "patches", "scope", "dispatch", "snapshot"}
    READ.json_object(review, "record.review", required=review_fields, optional=optional)
    if "dispatch" in review:
        READ.one_of(review["dispatch"], "record.review.dispatch", DISPATCH_MODES)
    if "snapshot" in review:
        _validate_snapshot(review["snapshot"])
    if "reviewers" in review:
        _validate_reviewers(review["reviewers"])
    if "patches" in review:
        _validate_patches(review["patches"])
    READ.one_of(review["verdict"], "record.review.verdict", VERDICTS)
    _validate_coverage(review, version)
    READ.integer(review["version"], "record.review.version", minimum=1)
    READ.one_of(review["mode"], "record.review.mode", ("initial", "re-review"))
    if "scope" in review:
        _validate_scope(review["scope"], review)
    _validate_timestamp(review["reviewed_at"])
    READ.string(review["summary"], "record.review.summary")


def is_suite_profile(profile: str) -> bool:
    """Whether a validated specialist profile, or a `source_hashes` key, names a profile the suite ships rather than a
    repository file."""
    return profile.startswith(SUITE_PROFILE_PREFIX)


def _validate_source_hashes(value: object) -> None:
    """The adapter's source files by safe relative path, each with its SHA-256 hash."""
    source_hashes = READ.json_object(value, "record.review.adapter.source_hashes")
    for key, hash_value in source_hashes.items():
        path = READ.string(key, "a key of record.review.adapter.source_hashes")
        if not safe_repository_path(path):
            raise RecordError("Review adapter source hash path is unsafe")
        READ.hex_digest(hash_value, f"record.review.adapter.source_hashes.{path}", 64)


def _validate_adapter(value: object) -> None:
    """Which adapter produced the review, at which source, and how it ended. Records written before the source was
    recorded have no `source`."""
    path = "record.review.adapter"
    fields = {"name", "protocol_version", "scope", "source_commit", "source_hashes", "reviewer", "status", "usage"}
    adapter = READ.json_object(value, path, required=fields, optional={"source"})
    if READ.integer(adapter["protocol_version"], f"{path}.protocol_version") != ADAPTER_PROTOCOL_VERSION:
        raise RecordError("Review adapter protocol is unsupported")
    scope = READ.one_of(adapter["scope"], f"{path}.scope", ("generic", "repository"))
    # A repository reviewer was read at a commit; the generic reviewer at none.
    if "source" in adapter:
        source = READ.one_of(adapter["source"], f"{path}.source", tuple(ADAPTER_SOURCES))
        if ADAPTER_SOURCES[source] != scope or (adapter["source_commit"] is None) != (scope == "generic"):
            raise RecordError(f"Review adapter source is invalid for a {scope} reviewer")
    for field in ("name", "reviewer"):
        READ.string(adapter[field], f"{path}.{field}")
    READ.one_of(adapter["status"], f"{path}.status", ("complete", "partial", "failed"))
    READ.optional(READ.hex_digest, adapter["source_commit"], f"{path}.source_commit", 40, 64)
    _validate_source_hashes(adapter["source_hashes"])
    READ.optional(READ.json_object, adapter["usage"], f"{path}.usage")


def _validate_finding(value: object, path: str) -> None:
    # Records written before findings carried titles or analyzer coverage remain valid.
    finding = READ.json_object(value, path, required=FINDING_FIELDS | {"id"}, optional=RECORD_OPTIONAL_FINDING_FIELDS)
    if "location" in finding:
        read_location(READ, finding["location"], f"{path}.location")
    READ.one_of(finding["severity"], f"{path}.severity", SEVERITIES)
    if "title" in finding:
        read_title(READ, finding["title"], f"{path}.title")
    if "analyzer" in finding:
        read_analyzer(READ, finding["analyzer"], f"{path}.analyzer")
    for field in ("candidate_key", "category", "path", "body", "evidence", "source"):
        READ.string(finding[field], f"{path}.{field}")
    if not safe_repository_path(finding["path"]):
        raise RecordError(f"Review finding {finding['id']}.path is unsafe")
    READ.integer(finding["line"], f"{path}.line", minimum=1)


def _validate_findings(value: object) -> list[dict[str, Any]]:
    """The findings, numbered F001 on in order, each well formed."""
    findings = READ.array(value, "record.findings", READ.json_object)
    expected_ids = [f"F{index:03d}" for index in range(1, len(findings) + 1)]
    if [item.get("id") for item in findings] != expected_ids:
        raise RecordError("Review finding IDs are not stable and contiguous")
    for index, finding in enumerate(findings):
        _validate_finding(finding, f"record.findings[{index}]")
    return findings


def _validate_counts(value: object, findings: list[dict[str, Any]]) -> None:
    counts = READ.json_object(value, "record.review.counts", required=SEVERITIES)
    for severity in sorted(SEVERITIES):
        READ.integer(counts[severity], f"record.review.counts.{severity}", minimum=0)
    expected_counts = {
        severity: sum(item["severity"] == severity for item in findings) for severity in sorted(SEVERITIES)
    }
    if counts != expected_counts:
        raise RecordError("Review finding counts do not match findings")


def _validate_prior_dispositions(dispositions: object) -> None:
    """At most one well-formed disposition per prior finding; the ledger checks which findings they judge."""
    disposition_ids: set[str] = set()
    for index, disposition in enumerate(READ.array(dispositions, "record.prior_dispositions")):
        path = f"record.prior_dispositions[{index}]"
        READ.json_object(disposition, path, required={"finding_id", "disposition", "rationale"})
        finding_id = READ.string(disposition["finding_id"], f"{path}.finding_id")
        if finding_id in disposition_ids:
            raise RecordError("Review prior disposition IDs must be unique")
        disposition_ids.add(finding_id)
        READ.one_of(disposition["disposition"], f"{path}.disposition", DISPOSITIONS)
        READ.string(disposition["rationale"], f"{path}.rationale")


def _validate_artifacts(value: object) -> None:
    """The hashes of the record's payload and its Markdown report, when the pair has been written."""
    if value is None:
        return
    artifacts = READ.json_object(value, "record.artifacts", required={"payload_sha256", "markdown_sha256"})
    for field in ("payload_sha256", "markdown_sha256"):
        READ.hex_digest(artifacts[field], f"record.artifacts.{field}", 64)


def validate_record(value: object) -> ReviewRecord:
    """A review record, checked part by part in a fixed order; the first fault found is the one raised."""
    record = READ.json_object(value, "record", optional=RECORD_FIELDS)
    version = READ.integer(record.get("schema_version"), "record.schema_version")
    if version not in READABLE_RECORD_VERSIONS:
        raise RecordError("Unsupported or malformed review record")
    _validate_comments(record)
    validate_repository_identity(READ.string(record.get("repository"), "record.repository"))
    pull = READ.json_object(record.get("pull_request"), "record.pull_request")
    review = READ.json_object(record.get("review"), "record.review")
    _validate_pull_request(pull)
    _validate_review(review, version)
    _validate_adapter(review["adapter"])
    findings = _validate_findings(record.get("findings"))
    _validate_counts(review["counts"], findings)
    _validate_prior_dispositions(record.get("prior_dispositions"))
    _validate_ledger(record, review["version"], review["mode"])
    _validate_artifacts(record.get("artifacts"))
    # Every field the shape gives a type was read above.
    return cast(ReviewRecord, record)


def payload_hash(record: ReviewRecord) -> str:
    payload = {key: value for key, value in record.items() if key != "artifacts"}
    return sha256_text(canonical_json(payload))


DISPOSITION_PHRASES: dict[Disposition, str] = {
    "still_present": "Still present",
    "partially_addressed": "Partially addressed",
    "addressed": "Addressed",
    "superseded": "Superseded",
    "unable_to_verify": "Unable to verify",
}


def _code(text: str) -> str:
    """Inline code span that survives backticks and line breaks in the text."""
    text = flat_text(text)
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _cell(text: str) -> str:
    """Single-line Markdown table cell content."""
    return flat_text(text).replace("|", "\\|")


def _code_cell(text: str) -> str:
    """HTML code avoids Markdown table escape parity when a label contains both backslashes and pipes."""
    if "\\" in text and "|" in text:
        return f"<code>{_text(text)}</code>"
    return _cell(_code(text))


def _prose(text: str, *, table: bool = False) -> str:
    """Literal prose with paragraphs and explicit line breaks, also inside a single table cell."""
    lines = [_literal(line) if line.strip() else "" for line in text.splitlines()]
    if table:
        return "<br>".join(lines)
    return "\n".join(
        line + ("<br>" if line and index + 1 < len(lines) and lines[index + 1] else "")
        for index, line in enumerate(lines)
    )


def _quote(text: str) -> list[str]:
    return [f"> {line}".rstrip() for line in _prose(text).splitlines()]


def _comment_link(comment: Comment) -> str:
    target = _navigation_url(comment["url"])
    return f"[{comment['id']}]({target})" if target else _text(f"{comment['id']} ({comment['url']})")


def _evidence(text: str) -> str:
    return "<br>".join(_code(line) if line else "" for line in text.splitlines())


def _label(value: str) -> str:
    return value.replace("_", " ").upper()


def _analyzer_note(analyzer: Analyzer) -> str:
    """One sentence saying how an analyzer could catch a finding."""
    tool, rule = _code(analyzer["tool"]), _code(analyzer["rule"])
    if analyzer["coverage"] == "available":
        return f"{rule} in {tool}, which the repository already has, would catch this if enforced."
    if analyzer["coverage"] == "known":
        return f"{rule} in {tool}, which the repository does not use, would catch this."
    return f"No existing rule catches this; it is a candidate for a custom {tool} rule ({rule})."


def _reviewed_at(value: str) -> str:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%d-%b-%Y %H:%M UTC")


def _description_row(pull: PullRequest) -> list[str]:
    """The report's row saying how much of the pull request's description its reviewers were given; none for a record
    written before reviewers were given it."""
    if "body_characters" not in pull:
        return []
    characters, given = pull["body_characters"], pull["body_given"]
    if characters == 0:
        stated = "none"
    elif given == characters:
        stated = f"{characters:,} characters, given to reviewers whole"
    else:
        stated = f"{characters:,} characters, of which reviewers were given the first {given:,}"
    return [f"| **Description** | {stated} |"]


def _finding_block(
    finding: Finding, label: str, *, repeats: str | None = None, status: Iterable[str] = ()
) -> list[str]:
    """One finding's collapsible block, headed by its display ID. `repeats` names the finding it repeats, and
    `status` holds the lines saying what this review found about it."""
    headline = (
        _text(finding["title"])
        if "title" in finding
        else f"<code>{_text(PurePosixPath(finding['path']).name)}:{finding['line']}</code>"
    )
    # A specialist's evidence is the added line itself, so it joins the line number instead of
    # repeating the location on a line of its own; other evidence stays after the body.
    added_prefix = f"{finding['path']}:{finding['line']} adds: "
    # A line of a changed document's extracted text also says where in the document it is.
    place = f" ({_text(finding['location'])})" if "location" in finding else ""
    if finding["evidence"].startswith(added_prefix):
        location = [
            f"> **Line {finding['line']}{place}:** {_evidence(finding['evidence'][len(added_prefix) :])} "
            f"| **Source:** {_text(finding['source'])}"
        ]
        evidence = []
    else:
        location = [f"> **Line:** {finding['line']}{place} | **Source:** {_text(finding['source'])}"]
        evidence = [">", f"> **Evidence:** {_evidence(finding['evidence'])}"]
    if "analyzer" in finding:
        evidence.extend([">", f"> **Analyzer:** {_analyzer_note(finding['analyzer'])}"])
    # Each status line but the last ends in a Markdown hard break, so the lines don't run together.
    status = [
        f"> {part}" + ("  " if part and not part.endswith("<br>") else "")
        for line in status
        for part in line.splitlines()
    ]
    if status:
        status[-1] = status[-1].rstrip()
    prefix = f"Repeats {repeats}: " if repeats else ""
    return [
        "<details open>",
        f"<summary>{label}. {prefix}[{_text(finding['category'])}] {headline}</summary>",
        "",
        *status,
        *([">"] if status else []),
        f"> **File:** {_code(finding['path'])}  ",
        *location,
        ">",
        *_quote(finding["body"]),
        *evidence,
        "",
        "</details>",
        "",
    ]


class _Ledger:
    """What a report needs from a pull request's records: its ledger at the rendered version, each finding by version
    and ID, each disposition's rationale by version and ledger ID, and the flags on each entry."""

    def __init__(
        self, record: ReviewRecord, prior_records: Iterable[ReviewRecord], flags: Iterable[dict[str, Any]]
    ) -> None:
        self.version = record["review"]["version"]
        records = [*(item for item in prior_records if item["review"]["version"] < self.version), record]
        self.entries = record["ledger"] if "ledger" in record else ledger_history(records)[self.version]
        self.findings = {
            (item["review"]["version"], finding["id"]): finding for item in records for finding in item["findings"]
        }
        self.rationales = {
            (item["review"]["version"], disposition["finding_id"]): disposition["rationale"]
            for item in records
            for disposition in item.get("prior_dispositions", [])
        }
        initial = [item["review"]["version"] for item in records if item["review"]["mode"] == "initial"]
        self.start = max(initial) if initial else records[0]["review"]["version"]
        self.flags = flagged_entries(self.entries, flags, record["repository"], record["pull_request"]["number"])

    def finding(self, version: int, identifier: str) -> Finding:
        finding = self.findings.get((version, identifier))
        if finding is None:
            raise RecordError(f"Review v{version} with finding {identifier} is missing from the archive")
        return finding

    def raised(self, entry: LedgerEntry) -> Finding:
        return self.finding(entry["version"], entry["id"])

    def order(self, entry: LedgerEntry) -> tuple[int, str, int, tuple[int, int]]:
        finding = self.raised(entry)
        return -SEVERITY_RANK[entry["severity"]], finding["path"].casefold(), finding["line"], _entry_key(entry)

    def judgment(self, entry: LedgerEntry) -> str:
        """The latest disposition's phrase, version, and rationale, such as `Addressed in v2: Fixed.`"""
        latest = entry["dispositions"][-1]
        rationale = self.rationales.get((latest["version"], ledger_id(entry["version"], entry["id"])))
        text = f"{DISPOSITION_PHRASES[latest['disposition']]} in v{latest['version']}"
        return f"{text}: {_prose(rationale)}" if rationale else f"{text}."

    def status(self, entry: LedgerEntry) -> list[str]:
        """An open or unverified entry's status line in a re-review, then a line for each flag on it."""
        if entry["version"] == self.version:
            lines = [f"**New in v{self.version}.**"]
        else:
            head = (
                f"**Unverified, raised in v{entry['version']}.**"
                if entry["state"] == "unverified"
                else f"**Open since v{entry['version']}.**"
            )
            judged = entry["dispositions"] and entry["dispositions"][-1]["version"] == self.version
            lines = [f"{head} {self.judgment(entry)}" if judged else f"{head} Last judged in v{entry['judged_in']}."]
        return lines + self.flag_lines(entry)

    def flag_lines(self, entry: LedgerEntry) -> list[str]:
        return [
            f"**Flagged:** {_text(flag['id'])} ({_text(flag['category'])}): {_prose(flag['body'])}"
            for flag in self.flags.get(ledger_id(entry["version"], entry["id"]), [])
        ]

    def counts(self) -> str:
        """Open entries, the version the oldest dates from, how many are flagged, and the entries addressed and
        unverified, such as `2 open since v1 (1 flagged), 1 addressed`."""
        opened = [entry for entry in self.entries if entry["state"] == "open"]
        if opened:
            since = min(entry["version"] for entry in opened)
            text = f"{len(opened)} open" + (f" since v{since}" if since < self.version else "")
            flagged = sum(ledger_id(entry["version"], entry["id"]) in self.flags for entry in opened)
            parts = [text + (f" ({flagged} flagged)" if flagged else "")]
        else:
            parts = ["none open"]
        addressed = sum(
            entry["state"] == "closed" and entry["dispositions"][-1]["disposition"] == "addressed"
            for entry in self.entries
        )
        unverified = sum(entry["state"] == "unverified" for entry in self.entries)
        parts.extend([f"{addressed} addressed"] * bool(addressed) + [f"{unverified} unverified"] * bool(unverified))
        return ", ".join(parts)


def render_markdown(
    record: ReviewRecord,
    *,
    record_payload_hash: str,
    prior_records: Iterable[ReviewRecord] = (),
    model_names: dict[str, str] | None = None,
    flags: Iterable[dict[str, Any]] = (),
) -> str:
    """The Markdown report. Its findings are the ledger's open and unverified entries, each shown from the record
    that raised it, so a re-review needs the pull request's earlier records. `model_names` maps a reviewer's model
    identifier to the name the Reviewers table shows, and `flags` are the flag store's flags."""
    pull = record["pull_request"]
    review = record["review"]
    adapter = review["adapter"]
    ledger = _Ledger(record, prior_records, flags)
    re_review = review["mode"] == "re-review"
    heading = f"# Code Review — {record['repository']}#{pull['number']}"
    if re_review:
        heading += f" (re-review v{review['version']})"
    lines = [
        heading,
        "",
        "| | |",
        "|---|---|",
        f"| **Title** | {_text(pull['title'])} |",
        (
            f"| **Branch** | {_code_cell(pull['head_ref'])} → {_code_cell(pull['base_ref'])} |"
            if pull.get("head_ref")
            else f"| **Base** | {_code_cell(pull['base_ref'])} |"
        ),
        f"| **URL** | {_navigation_url(pull['url']) or _text(pull['url'])} |",
        *_description_row(pull),
        f"| **Reviewed** | {_reviewed_at(review['reviewed_at'])} |",
        f"| **Verdict** | {_label(review['verdict'])}{', ' + ledger.counts() if re_review else ''} |",
        *([f"| **Scope** | {_prose(describe_scope(review['scope']), table=True)} |"] if "scope" in review else []),
        "",
    ]
    fallback = review.get("snapshot", {}).get("diff")
    if fallback is not None:
        lines.extend(
            [
                f"> **Large change:** GitHub's diff endpoint refuses a diff over {_text(fallback['limit'])}, so this "
                f"review read the diff from {DIFF_SOURCES[fallback['source']]}.",
                "",
            ]
        )
    coverage: Coverage = review.get("coverage") or {"unavailable_sources": []}
    unavailable = coverage["unavailable_sources"]
    if unavailable:
        lines.extend(
            [
                "> **Not reviewed in full:** these changed files were excluded by the repository's `snapshot_exclude`, "
                "were too large, could not be represented safely, or were documents whose text could not be extracted, "
                "so reviewers saw only their diff: " + ", ".join(_code(path) for path in unavailable) + ".",
                "",
            ]
        )
    unreviewed = coverage.get("unreviewed_files", {})
    if unreviewed:
        lines.extend(
            [
                "> **Not read:** these changed files are binary files of a type the review does not read, so reviewers "
                "saw only that they changed, and they do not make the review incomplete: "
                + ", ".join(f"{_code(path)} ({_code(suffix)})" for path, suffix in unreviewed.items())
                + ".",
                "",
            ]
        )
    uncovered = coverage.get("uncovered_files", [])
    if uncovered:
        lines.extend(
            [
                "> **Not reviewed:** no specialist covers these changed files, and the reviewer manifest sets "
                "`uncovered` to `ignore`, so no reviewer saw them: "
                + ", ".join(_code(path) for path in uncovered)
                + ".",
                "",
            ]
        )
    lines.extend(["---", "", "## Summary", "", _prose(review["summary"]), "", "## Findings", ""])
    lines.extend(_findings_section(ledger, re_review))
    comments = {comment["id"]: comment for comment in record.get("github_comments", [])}
    if comments:
        lines.extend(
            [
                "## Review Comments",
                "",
                "| # | Comment | Status | Rationale |",
                "|---|---------|--------|-----------|",
            ]
        )
        for disposition in record["comment_dispositions"]:
            comment = comments[disposition["comment_id"]]
            location = comment["path"] + (f":{comment['line']}" if comment["line"] else "")
            excerpt = flat_text(comment["body"])
            excerpt = excerpt if len(excerpt) <= 120 else excerpt[:117].rstrip() + "..."
            outdated = " (outdated)" if comment["outdated"] else ""
            lines.append(
                f"| {_comment_link(comment)} | {_text('@' + comment['author'])} on "
                f"{_code_cell(location)}{outdated}: {_text(excerpt)} | {_label(disposition['disposition'])} "
                f"| {_prose(disposition['rationale'], table=True)} |"
            )
        lines.extend(["", _addressed(record["comment_dispositions"]), ""])
    names = model_names or {}
    mapped: list[str] = []
    if review.get("reviewers"):
        lines.extend(
            [
                "## Reviewers",
                "",
                "| Reviewer | Focus | Model | Files | Findings | Retries | Time | Files read |",
                "|----------|-------|-------|-------|----------|---------|------|------------|",
            ]
        )
        for reviewer in review["reviewers"]:
            focus = reviewer["category"] + (" (dispositions only)" if reviewer["dispositions_only"] else "")
            model = reviewer.get("model", "-")
            if model in names and model not in mapped:
                mapped.append(model)
            lines.append(
                f"| {_code_cell(reviewer['id'])} | {_text(focus)} "
                f"| {_text(names.get(model, model)) if 'model' in reviewer else '-'} "
                f"| {reviewer['files']} "
                f"| {reviewer['findings']} | {reviewer['retries']} | {_duration(reviewer.get('seconds'))} "
                f"| {_files_read(reviewer)} |"
            )
        lines.append("")
    lines.extend(
        [
            "---",
            "",
            "<details>",
            "<summary><strong>Review Details</strong></summary>",
            "",
            "| | |",
            "|---|---|",
            f"| **Mode** | {review['mode']} v{review['version']} |",
            *([f"| **Dispatch** | {review['dispatch']} |"] if "dispatch" in review else []),
            *([f"| **Snapshot** | {describe_snapshot(review['snapshot'])} |"] if "snapshot" in review else []),
            f"| **Adapter** | {_code_cell(adapter['name'])} ({adapter['scope']}) |",
            *([f"| **Reviewer source** | {_cell(describe_reviewer_source(adapter))} |"] if "source" in adapter else []),
            f"| **Reviewer** | {_text(adapter['reviewer'])} ({adapter['status']}) |",
            # The Reviewers table shows a configured name in place of each mapped model identifier, kept here.
            *(
                [
                    "| **Reviewer models** | "
                    + "; ".join(f"{_text(names[model])}: {_code_cell(model)}" for model in mapped)
                    + " |"
                ]
                if mapped
                else []
            ),
            f"| **Base SHA** | `{pull['base_sha']}` |",
            f"| **Reviewed HEAD** | `{pull['head_sha']}` |",
            f"| **Record payload SHA-256** | `{record_payload_hash}` |",
            "",
            "</details>",
            "",
            f"<!-- reviewed_head_sha: {pull['head_sha']} -->",
            "",
        ]
    )
    return "\n".join(lines)


def _findings_section(ledger: _Ledger, re_review: bool) -> list[str]:
    """Every open and unverified entry, grouped by severity, each shown from the finding that raised it with this
    review's repeats of it nested inside; then, in a re-review, the entries closed since the latest initial review.
    Only a re-review says what changed on each entry."""
    shown = [entry for entry in ledger.entries if entry["state"] != "closed"]
    if not shown:
        return ["No open findings." if re_review else "No findings.", ""]
    lines: list[str] = []
    for severity, title in SEVERITY_SECTIONS:
        group = sorted((entry for entry in shown if entry["severity"] == severity), key=ledger.order)
        if not group:
            continue
        lines.extend(["<details open>", f"<summary><strong>{title} ({len(group)})</strong></summary>", ""])
        for entry in group:
            label = display_id(entry["version"], entry["id"])
            block = _finding_block(
                ledger.raised(entry), label, status=ledger.status(entry) if re_review else ledger.flag_lines(entry)
            )
            for repeat in entry["repeats"]:
                if repeat["version"] == ledger.version:
                    block[-2:-2] = _finding_block(
                        ledger.finding(repeat["version"], repeat["id"]),
                        display_id(repeat["version"], repeat["id"]),
                        repeats=label,
                    )
            lines.extend(block)
        lines.extend(["</details>", ""])
    closed = sorted((entry for entry in ledger.entries if entry["state"] == "closed"), key=ledger.order)
    if re_review and closed:
        lines.extend(["<details>", f"<summary><strong>Addressed since v{ledger.start}</strong></summary>", ""])
        for entry in closed:
            finding = ledger.raised(entry)
            title = f" {_text(finding['title'])}," if "title" in finding else ""
            location = _code(f"{finding['path']}:{finding['line']}")
            item = (
                f"- **{display_id(entry['version'], entry['id'])}.** {_label(entry['severity'])} "
                f"[{_text(entry['category'])}]{title} {location}. {ledger.judgment(entry)}"
            )
            lines.extend(part if index == 0 else f"  {part}" for index, part in enumerate(item.splitlines()))
        lines.extend(["", "</details>", ""])
    return lines


def _addressed(dispositions: list[CommentDisposition]) -> str:
    addressed = sum(1 for disposition in dispositions if disposition["disposition"] == "addressed")
    return f"**{addressed}/{len(dispositions)} addressed**"


def write_record_pair(
    json_path: Path,
    markdown_path: Path,
    record: ReviewRecord,
    *,
    prior_records: Iterable[ReviewRecord] = (),
    model_names: dict[str, str] | None = None,
    flags: Iterable[dict[str, Any]] = (),
) -> ReviewRecord:
    validate_record(record)
    payload_sha = payload_hash(record)
    markdown = render_markdown(
        record, record_payload_hash=payload_sha, prior_records=prior_records, model_names=model_names, flags=flags
    )
    markdown_sha = sha256_text(markdown)
    persisted = copy.deepcopy(record)
    persisted["artifacts"] = {
        "payload_sha256": payload_sha,
        "markdown_sha256": markdown_sha,
    }
    validate_record(persisted)
    atomic_write_text(markdown_path, markdown)
    try:
        atomic_write_json(json_path, persisted, validator=validate_record)
    except BaseException:
        markdown_path.unlink(missing_ok=True)
        raise
    return persisted


def record_artifacts(record: ReviewRecord) -> Artifacts | None:
    """A validated record's artifact hashes, or None when it has none: a null `artifacts` reads as absent."""
    return record.get("artifacts")


def artifact_hashes(record: ReviewRecord) -> Artifacts:
    """A validated record pair's artifact hashes, which a record that has none fails to be."""
    artifacts = record_artifacts(record)
    if artifacts is None:
        raise RecordError("Review record has no artifact hashes")
    return artifacts


def validate_record_pair(json_path: Path, markdown_path: Path) -> ReviewRecord:
    record = validate_record(read_json(json_path))
    artifacts = artifact_hashes(record)
    markdown = markdown_path.read_text(encoding="utf-8")
    if payload_hash(record) != artifacts["payload_sha256"]:
        raise RecordError("Review JSON payload hash mismatch")
    if sha256_text(markdown) != artifacts["markdown_sha256"]:
        raise RecordError("Review Markdown hash mismatch")
    expected_marker = f"Record payload SHA-256** | `{artifacts['payload_sha256']}`"
    if expected_marker not in markdown:
        raise RecordError("Review Markdown does not reference the JSON payload hash")
    return record

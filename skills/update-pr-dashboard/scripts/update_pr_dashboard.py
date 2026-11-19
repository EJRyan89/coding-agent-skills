"""Render the owned section of a pull-request tracker from normalized JSON input."""

from __future__ import annotations

import html
import re
import stat
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from file_presence import is_occupied
from markdown_literal import markdown_line, navigation_url
from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from path_containment import resolved_path
from pr_change import CHANGED, UNCHANGED, UNKNOWN, ChangeDetector, ComparisonError, Query
from review_config import (
    COMPUTED_DASHBOARD_STATES,
    DASHBOARD_AI_REVIEW_CURRENT,
    DASHBOARD_AI_REVIEW_INCOMPLETE,
    DASHBOARD_AI_REVIEW_MISSING,
    DASHBOARD_AI_REVIEW_STALE,
    DASHBOARD_AI_REVIEW_STATES,
    DASHBOARD_END_MARKER,
    DASHBOARD_SECTION_AWAITING,
    DASHBOARD_SECTION_DRAFTS,
    DASHBOARD_SECTION_MINE,
    DASHBOARD_SECTION_TO_REVIEW,
    DASHBOARD_START_MARKER,
    PULL_NUMBER,
    REPOSITORY,
    STATUS_OVERRIDE_KEY_PATTERN,
    ConfigurationError,
    dashboard_status_heading,
    dashboard_status_key,
    normalize_author_names,
    validate_repository_identity,
)
from review_github import GitHubClient
from review_io import PersistenceError, atomic_write_json, atomic_write_text, read_json
from review_vocabularies import SEVERITIES as SEVERITY_KEYS
from typed_fields import FieldError, FieldReader

REQUIRED_FIELDS = {
    "repository",
    "number",
    "url",
    "title",
    "author",
    "requested_reviewers",
    "draft",
    "base_ref",
    "head_sha",
    "updated_at",
    "reviewed_head_sha",
    "user_review_state",
    "user_review_sha",
}
USER_REVIEW_STATES = {None, "APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"}
ACTIVE_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}
# The states of the user's latest deciding review: one that decided, or whose decision was dismissed. A later
# comment-only review leaves it standing.
DECIDING_REVIEW_STATES = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
USER_REVIEW_DECISIONS = {None, *DECIDING_REVIEW_STATES}
SECTION_TO_REVIEW = DASHBOARD_SECTION_TO_REVIEW
SECTION_AWAITING = DASHBOARD_SECTION_AWAITING
SECTION_MINE = DASHBOARD_SECTION_MINE
SECTION_DRAFTS = DASHBOARD_SECTION_DRAFTS
OPTIONAL_FIELDS = {
    "reviewed_incomplete",
    "reviewed_base_sha",
    "base_sha",
    "author_name",
    "review_decision",
    "user_review_decision",
    "ai_review",
}
LEDGER_KEYS = {"ledger", "flagged", "since_review"}
REVIEW_DECISIONS = {None, "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}
AI_VERDICTS = {"APPROVED": "Approved", "CHANGES_REQUESTED": "Changes Requested", "INCOMPLETE": "Incomplete"}
# A row's Status: a decision on the pull request, GitHub's for one the user authored and the user's own for any other.
DECISION_STATUSES = {"APPROVED": "Approved", "CHANGES_REQUESTED": "Changes Requested"}
STATUS_DRAFT = "Draft"
STATUS_OPEN = "Open"
STATUS_COMMENTED = "Reviewed with Comments"
SECTION_SUMMARIES = {
    SECTION_TO_REVIEW: "PRs you are asked to review or are reviewing where action is needed — new PR, "
    "or the author has responded to your feedback. Status reflects your own latest review.",
    SECTION_AWAITING: "PRs where you have left a review; waiting for the author to respond or push changes. Status "
    "reflects your own latest review.",
    SECTION_DRAFTS: "Draft PRs you are asked to review or are reviewing; no action needed until the author "
    "marks them ready.",
    SECTION_MINE: "PRs you authored. Status reflects GitHub's review decision on the PR.",
}
OPEN_SECTIONS = {SECTION_TO_REVIEW, SECTION_AWAITING, SECTION_MINE}
FINDINGS_LEGEND = (
    "Findings: M must fix, H should fix, S suggestion; flagged means you flagged it with flag-review-finding."
)
# The tracker record's version: 2 adds each shown row's AI review, and 3 the base each of the user's own reviews is
# compared from; a version 1 or 2 record, still read, lacks what later versions add.
TRACKER_SCHEMA_VERSION = 3
TRACKER_SCHEMA_VERSIONS = (1, 2, TRACKER_SCHEMA_VERSION)
# The fields each version's record holds.
TRACKER_RECORD_FIELDS = {
    1: {"schema_version", "pulls"},
    2: {"schema_version", "pulls", "reviews"},
    3: {"schema_version", "pulls", "reviews", "user_reviews"},
}
# The AI review states a row is offered for review in, unless a status override pins it.
CANDIDATE_STATES = frozenset({DASHBOARD_AI_REVIEW_MISSING, DASHBOARD_AI_REVIEW_STALE})
# A new file the tracker writes is readable by its owner alone; a file it replaces keeps the mode it had.
NEW_FILE_MODE = 0o600
# A pull request's number is review_config's PULL_NUMBER, the bound its status override keys are held to.
PULL_KEY_PATTERN = re.compile(rf"({REPOSITORY})#({PULL_NUMBER})")


class TrackerError(ValueError):
    pass


class ComparisonFailures(TrackerError):
    """Pull requests whose comparison failed, in row order. Nothing is rendered, so the dashboard keeps every row as it
    was rather than showing a review stale on evidence GitHub never gave."""

    def __init__(self, failures: list[ComparisonError], related: int) -> None:
        super().__init__(
            f"{len(failures)} of {related} pull requests could not be compared; the dashboard keeps its previous rows"
        )
        self.failures = failures


class TrackerFieldError(FieldError, TrackerError):
    """A tracker input or status override field of the wrong kind."""


READ = FieldReader(TrackerFieldError)


class Detector(Protocol):
    def detect(
        self,
        repository: str,
        number: int,
        base_ref: str,
        since_sha: str,
        head_sha: str,
        since_base: str | None = None,
    ) -> str:
        """CHANGED, UNCHANGED, or UNKNOWN; raises `ComparisonError` when a GitHub call the comparison needs failed.
        `since_base` is the base `since_sha` was reviewed on, the base branch as it is now when None."""


class BatchDetector(Detector, Protocol):
    def prefetch(self, queries: Iterable[Query]) -> None:
        """Read ahead, concurrently, what detecting each query needs."""


@dataclass(frozen=True)
class Row:
    item: dict[str, Any]
    relationship: str
    section: str
    ai_review: str
    overridden: bool

    @property
    def key(self) -> str:
        return f"{self.item['repository']}#{self.item['number']}"


def _validate_ledger(ledger: Any, key: str, path: str) -> None:
    """The latest review's finding-ledger summary. A migrated legacy review's has no version and cannot say what was
    addressed, so both are None; it is None itself when the legacy report is unreadable. The repeats the latest review
    reported of open entries are absent from a legacy review's summary and from an input collected before they were
    counted."""
    required = {"open", "addressed", "since", "version"}
    if READ.optional(READ.json_object, ledger, path, required=required, optional={"repeats"}) is None:
        return
    opened = READ.json_object(ledger["open"], f"{path}.open", required=SEVERITY_KEYS)
    for severity in sorted(SEVERITY_KEYS):
        READ.integer(opened[severity], f"{path}.open.{severity}", minimum=0)
    repeats = READ.integer(ledger["repeats"], f"{path}.repeats", minimum=0) if "repeats" in ledger else 0
    if repeats and not any(opened.values()):
        raise TrackerError(f"Tracker item {key}.ai_review.ledger is invalid")
    version = READ.optional(READ.integer, ledger["version"], f"{path}.version", minimum=1)
    addressed = READ.optional(READ.integer, ledger["addressed"], f"{path}.addressed", minimum=0)
    since = READ.optional(READ.integer, ledger["since"], f"{path}.since", minimum=0)
    if version is None:
        consistent = addressed is None and since is None
    elif not any(opened.values()):
        consistent = addressed is not None and since is None
    else:
        consistent = addressed is not None and since is not None and 1 <= since <= version
    if not consistent:
        raise TrackerError(f"Tracker item {key}.ai_review.ledger is invalid")


def _validate_ledger_reading(review: dict[str, Any], key: str, path: str) -> None:
    """The open findings flags name and what moved since the user's last review, both read from a ledger: flagged
    counts open findings, and since_review is null when the user has not reviewed or the review commit could not be
    placed, else the baseline version (0 when every review came after it) with the entries new and addressed since."""
    if "flagged" not in review and "since_review" not in review:
        return
    invalid = TrackerError(f"Tracker item {key}.ai_review.flagged or since_review is invalid")
    ledger = review.get("ledger")
    if ledger is None or ledger["version"] is None or "flagged" not in review or "since_review" not in review:
        raise invalid
    if READ.integer(review["flagged"], f"{path}.flagged", minimum=0) > sum(ledger["open"].values()):
        raise invalid
    since = READ.optional(
        READ.json_object, review["since_review"], f"{path}.since_review", required={"version", "new", "addressed"}
    )
    if since is None:
        return
    for field in ("version", "new", "addressed"):
        READ.integer(since[field], f"{path}.since_review.{field}", minimum=0)
    if since["version"] > ledger["version"]:
        raise invalid


def _validate_presentation(item: dict[str, Any], path: str) -> None:
    key = f"{item.get('repository')}#{item.get('number')}"
    READ.optional(READ.string, item["author_name"], f"{path}.author_name")
    READ.optional(READ.one_of, item["review_decision"], f"{path}.review_decision", REVIEW_DECISIONS - {None})
    READ.optional(
        READ.one_of, item["user_review_decision"], f"{path}.user_review_decision", USER_REVIEW_DECISIONS - {None}
    )
    # An input collected before reviews kept a finding ledger has no ledger key, and one collected before the tracker
    # read flags and the user's last review has no flagged or since_review keys.
    path = f"{path}.ai_review"
    review = READ.optional(
        READ.json_object, item["ai_review"], path, required={"verdict", "counts", "report"}, optional=LEDGER_KEYS
    )
    if review is None:
        return
    _validate_ledger(review.get("ledger"), key, f"{path}.ledger")
    _validate_ledger_reading(review, key, path)
    READ.one_of(review["verdict"], f"{path}.verdict", tuple(AI_VERDICTS))
    counts = READ.optional(READ.json_object, review["counts"], f"{path}.counts", required=SEVERITY_KEYS)
    if counts is not None:
        for severity in sorted(SEVERITY_KEYS):
            READ.integer(counts[severity], f"{path}.counts.{severity}", minimum=0)
    READ.optional(READ.string, review["report"], f"{path}.report")


def validate_items(value: Any) -> list[dict[str, Any]]:
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, given in enumerate(READ.array(value, "tracker input")):
        path = f"tracker input[{index}]"
        item = _with_defaults(given, path)
        _validate_presentation(item, path)
        _validate_incomplete(item, path)
        repository, key = _item_key(item, path, seen)
        _validate_fields(item, key, path)
        _validate_review_state(item, key, path)
        replacement = dict(item)
        replacement["repository"] = repository
        normalized.append(replacement)
    return normalized


def _with_defaults(item: Any, path: str) -> dict[str, Any]:
    """The item with its optional fields defaulted ahead of its own, once its fields match the contract. An input
    collected before the user's deciding review was read has no `user_review_decision`, which is then the latest
    review's state when that review decided, as it must be then, and null otherwise."""
    READ.json_object(item, path, required=REQUIRED_FIELDS, optional=OPTIONAL_FIELDS)
    latest = item["user_review_state"]
    return {
        "reviewed_incomplete": False,
        "reviewed_base_sha": None,
        "base_sha": None,
        "author_name": None,
        "review_decision": None,
        "user_review_decision": latest if latest in DECIDING_REVIEW_STATES else None,
        "ai_review": None,
        **item,
    }


def _validate_incomplete(item: dict[str, Any], path: str) -> None:
    if READ.boolean(item["reviewed_incomplete"], f"{path}.reviewed_incomplete") and item["reviewed_head_sha"] is None:
        raise TrackerError("Tracker item reviewed_incomplete requires reviewed_head_sha")


def _item_key(item: dict[str, Any], path: str, seen: set[str]) -> tuple[str, str]:
    """The item's normalized repository and its owner/name#number key, which it adds to seen once it is new."""
    repository = validate_repository_identity(READ.string(item["repository"], f"{path}.repository"))
    key = f"{repository}#{READ.integer(item['number'], f'{path}.number', minimum=1)}"
    if key in seen:
        raise TrackerError(f"Duplicate tracker item: {key}")
    seen.add(key)
    return repository, key


def _validate_fields(item: dict[str, Any], key: str, path: str) -> None:
    for field in ("url", "title", "author", "base_ref", "head_sha", "updated_at"):
        READ.string(item[field], f"{path}.{field}")
    READ.array(item["requested_reviewers"], f"{path}.requested_reviewers", READ.string)
    READ.boolean(item["draft"], f"{path}.draft")
    for field in ("reviewed_head_sha", "reviewed_base_sha", "base_sha", "user_review_sha"):
        READ.optional(READ.string, item[field], f"{path}.{field}")
    if item["reviewed_base_sha"] is not None and item["reviewed_head_sha"] is None:
        raise TrackerError(f"Tracker item {key}.reviewed_base_sha requires reviewed_head_sha")


def _validate_review_state(item: dict[str, Any], key: str, path: str) -> None:
    review_state = READ.optional(
        READ.one_of, item["user_review_state"], f"{path}.user_review_state", USER_REVIEW_STATES - {None}
    )
    if review_state is None and item["user_review_sha"] is not None:
        raise TrackerError(f"Tracker item {key}.user_review_sha requires a review state")
    if review_state in ACTIVE_REVIEW_STATES and item["user_review_sha"] is None:
        raise TrackerError(f"Tracker item {key}.user_review_sha is required for {review_state}")
    # The latest review is the latest deciding one when it decided, and there is no deciding review without a review.
    decision = item["user_review_decision"]
    if review_state is None and decision is not None:
        raise TrackerError(f"Tracker item {key}.user_review_decision requires a review state")
    if review_state in DECIDING_REVIEW_STATES and decision != review_state:
        raise TrackerError(f"Tracker item {key}.user_review_decision must be {review_state}, its latest review's")


INT_PARSE_ALLOWED = {
    "normalize_pull_keys": "the pattern takes at most ten ASCII digits",
}


def normalize_pull_keys(values: set[str] | list[str], field: str) -> set[str]:
    normalized: set[str] = set()
    for value in values:
        match = PULL_KEY_PATTERN.fullmatch(READ.string(value, f"a {field} pull-request identity"))
        if match is None:
            raise TrackerError(f"Invalid {field} pull-request identity: {value!r}")
        key = f"{validate_repository_identity(match.group(1))}#{int(match.group(2))}"
        if key in normalized:
            raise TrackerError(f"Duplicate {field} pull-request identity: {key}")
        normalized.add(key)
    return normalized


def override_key(value: str) -> str:
    """A status override key read by the configuration's own rule, lowercase as the tracker matches it, so every key
    the configuration loads can be listed and cleared. One whose repository is an identity is the pull request
    `normalize_pull_keys` reads; one only the configuration's looser rule takes matches no pull request (see
    `is_pull_key`)."""
    key = READ.string(value, "an override pull-request identity")
    if PULL_KEY_PATTERN.fullmatch(key):
        return next(iter(normalize_pull_keys([key], "override")))
    if not STATUS_OVERRIDE_KEY_PATTERN.fullmatch(key):
        raise TrackerError(f"Invalid override pull-request identity: {value!r}")
    return key.lower()


def is_pull_key(key: str) -> bool:
    """Whether a status override key names a pull request the tracker can match: its repository is an identity."""
    return PULL_KEY_PATTERN.fullmatch(key) is not None


def read_overrides(value: dict[str, str] | None) -> tuple[dict[str, str], dict[str, list[tuple[str, str]]]]:
    """The configured status overrides, each key read by `override_key`, and apart from them each key two or more
    configured keys share, since they differ only in letter case, with those keys as configured and their statuses, in
    key order. Such a key pins nothing: which status is meant cannot be told, and the configuration loads them all."""
    if value is None:
        return {}, {}
    read: dict[str, list[tuple[str, str]]] = {}
    for raw_key, raw_status in READ.json_object(value, "status overrides").items():
        key = override_key(READ.string(raw_key, "a key of status overrides"))
        status = READ.string(raw_status, f"status overrides.{raw_key}", empty=True).strip()
        if not status:
            raise TrackerError(f"Status override {key} must be non-empty")
        if dashboard_status_key(status) in COMPUTED_DASHBOARD_STATES:
            raise TrackerError(f"Status override {key} duplicates a computed tracker state")
        read.setdefault(key, []).append((raw_key, status))
    pins = {key: entries[0][1] for key, entries in read.items() if len(entries) == 1}
    duplicates = {key: sorted(entries) for key, entries in sorted(read.items()) if len(entries) > 1}
    return pins, duplicates


def normalize_overrides(value: dict[str, str] | None) -> dict[str, str]:
    """The status overrides that pin a row: each configured key read by `override_key`, but one two configured keys
    share (see `read_overrides`)."""
    return read_overrides(value)[0]


def tracked_pulls(items: list[dict[str, Any]], login: str) -> list[str]:
    """The pull requests the tracker holds, sorted: every collected item the user authored, is asked to review, or
    has reviewed, whether or not this run shows its row, so a removed or approved one can still be pinned."""
    login_key = login.casefold()
    return sorted(f"{item['repository']}#{item['number']}" for item in items if _relationship(item, login_key))


def validate_tracker_record(value: Any) -> dict[str, Any]:
    """The tracker record `update` keeps beside the configuration: the pull requests its last run held, keyed as a
    status override is, so `override` can refuse one naming none of them, the AI review each shown row had, so the
    next update can name a review that went stale since, and the base each of the user's own reviews is compared from
    (see `user_review_bases`). A version 1 record holds no reviews and a version 2 record no user reviews, and each
    reads as holding none."""
    version = READ.one_of(
        READ.json_object(value, "tracker").get("schema_version"), "tracker.schema_version", TRACKER_SCHEMA_VERSIONS
    )
    record = READ.json_object(value, "tracker", required=TRACKER_RECORD_FIELDS[version])
    pulls = normalize_pull_keys(READ.array(record["pulls"], "tracker.pulls", READ.string), "tracker")
    reviews = _validate_record_reviews(record.get("reviews", {}), pulls)
    user_reviews = _validate_record_user_reviews(record.get("user_reviews", {}), pulls)
    return {
        "schema_version": TRACKER_SCHEMA_VERSION,
        "pulls": sorted(pulls),
        "reviews": reviews,
        "user_reviews": user_reviews,
    }


def _validate_record_reviews(value: Any, pulls: set[str]) -> dict[str, dict[str, Any]]:
    """Each recorded row's AI review state and the head its review covered, which only a missing review lacks, keyed
    as `pulls` is and naming only pull requests it holds."""
    reviews: dict[str, dict[str, Any]] = {}
    for raw_key, raw_review in READ.json_object(value, "tracker.reviews").items():
        key = next(iter(normalize_pull_keys([raw_key], "tracker review")))
        if key not in pulls or key in reviews:
            raise TrackerError(f"Tracker review {key} names no pull request the record holds, or is named twice")
        path = f"tracker.reviews.{raw_key}"
        review = READ.json_object(raw_review, path, required={"state", "reviewed_head_sha"})
        state = READ.one_of(review["state"], f"{path}.state", DASHBOARD_AI_REVIEW_STATES)
        head = READ.optional(READ.string, review["reviewed_head_sha"], f"{path}.reviewed_head_sha")
        if (head is None) != (state == DASHBOARD_AI_REVIEW_MISSING):
            raise TrackerError(f"Tracker review {key} must name a reviewed head exactly when its review is not missing")
        reviews[key] = {"state": state, "reviewed_head_sha": head}
    return dict(sorted(reviews.items()))


def _validate_record_user_reviews(value: Any, pulls: set[str]) -> dict[str, dict[str, str]]:
    """The commit of each of the user's own reviews and the base it is compared from, keyed as `pulls` is and naming
    only pull requests it holds."""
    user_reviews: dict[str, dict[str, str]] = {}
    for raw_key, raw_review in READ.json_object(value, "tracker.user_reviews").items():
        key = next(iter(normalize_pull_keys([raw_key], "tracker user review")))
        if key not in pulls or key in user_reviews:
            raise TrackerError(f"Tracker user review {key} names no pull request the record holds, or is named twice")
        path = f"tracker.user_reviews.{raw_key}"
        review = READ.json_object(raw_review, path, required={"user_review_sha", "base_sha"})
        user_reviews[key] = {field: READ.string(review[field], f"{path}.{field}") for field in sorted(review)}
    return dict(sorted(user_reviews.items()))


def user_review_bases(
    items: list[dict[str, Any]], login: str, previous: dict[str, Any] | None
) -> dict[str, dict[str, str]]:
    """The base each of the user's own reviews is compared from, by pull request, for the tracker record: for each item
    the user did not author whose latest review is one `_section` compares, the base `previous` recorded for that
    review's commit, else the tip of the base branch this collection read, or none when it read none.

    GitHub keeps no base for a user's review, so the base is the one the update saw the first time it saw the review's
    commit, kept by each later update that collects the pull request while the review's commit stays the same. Comparing
    the review's commit with that base rather than the base branch as it is now keeps a pull request a rewrite of a
    lower branch rebased, with its own changes as reviewed, from reading as changed. Limits: a base rewritten before the
    first update after the review was recorded after the rewrite, and an update that does not collect the pull
    request, one scoped to other repositories or run while it is closed, forgets its base, as it forgets the record's
    other entries for it, so the next update that collects it records the tip then. Either way that review reads as it
    did before bases were recorded."""
    login_key = login.casefold()
    recorded = previous["user_reviews"] if previous else {}
    bases: dict[str, dict[str, str]] = {}
    for item in items:
        key = f"{item['repository']}#{item['number']}"
        relationship = _relationship(item, login_key)
        if relationship in {None, "authored"} or item["user_review_state"] not in ACTIVE_REVIEW_STATES:
            continue
        before = recorded.get(key)
        if before is not None and before["user_review_sha"] == item["user_review_sha"]:
            bases[key] = before
        elif item["base_sha"] is not None:
            bases[key] = {"user_review_sha": item["user_review_sha"], "base_sha": item["base_sha"]}
    return dict(sorted(bases.items()))


def stale_since(previous: dict[str, Any] | None, rows: list[Row]) -> list[str]:
    """The rows whose AI review went stale since the update that wrote `previous`, in row order: one that update held
    as current or incomplete, or whose review was written since, so it covers another head, and is now stale. A row a
    status override pins is never offered for review, so it is left out, as is one `previous` did not show."""
    if previous is None:
        return []
    went: list[str] = []
    for row in rows:
        before = previous["reviews"].get(row.key)
        if row.ai_review != DASHBOARD_AI_REVIEW_STALE or row.overridden or before is None:
            continue
        held = before["state"] in {DASHBOARD_AI_REVIEW_CURRENT, DASHBOARD_AI_REVIEW_INCOMPLETE}
        if held or before["reviewed_head_sha"] != row.item["reviewed_head_sha"]:
            went.append(row.key)
    return went


def _record_reviews(rows: list[Row]) -> dict[str, dict[str, Any]]:
    return {
        row.key: {"state": row.ai_review, "reviewed_head_sha": row.item["reviewed_head_sha"]}
        for row in sorted(rows, key=lambda row: row.key)
    }


def replacement(path: Path, writer: str = "the update") -> tuple[Path, int]:
    """Where a replacement of `path` is written and the mode it takes: the file the path leads to, through any
    symbolic link, so a link stays a link and its target is what changes, with the mode that file has, or
    `NEW_FILE_MODE` for a file not yet written. A link to nothing reads as a file not yet written, and its replacement
    takes the link's own place, so no write creates the file the link leads to. A file with another hard link is
    refused, since a replacement would
    leave that name with the old content, and so is a read-only file, which ejryan89-skill-core's `atomic_file` would
    refuse only once GitHub had been asked; the refusal names `writer`, the command that would replace it."""
    target = resolved_path(path)
    try:
        status = target.stat()
    except FileNotFoundError:
        try:
            linked = is_occupied(path)
        except OSError as exc:
            raise TrackerError(f"Cannot examine {path}: {exc.strerror or exc}") from exc
        return (path if linked else target), NEW_FILE_MODE
    except OSError as exc:
        raise TrackerError(f"Cannot examine {path}: {exc.strerror or exc}") from exc
    if status.st_nlink > 1:
        raise TrackerError(
            f"{path} has another hard link, which a replacement would leave with the old content; give it one name"
        )
    if not status.st_mode & stat.S_IWUSR:
        raise TrackerError(f"{path} is read-only; make it writable for {writer} to replace it")
    return target, stat.S_IMODE(status.st_mode)


def apply_author_names(items: list[dict[str, Any]], author_names: dict[str, str] | None) -> list[dict[str, Any]]:
    """Replace each author's GitHub profile name with its configured display name, matching logins ignoring case."""
    try:
        configured = normalize_author_names(author_names or {})
    except ConfigurationError as exc:
        raise TrackerError(str(exc)) from exc
    names = {login.casefold(): name for login, name in configured.items()}
    return [{**item, "author_name": names.get(item["author"].casefold(), item["author_name"])} for item in items]


def _relationship(item: dict[str, Any], login_key: str) -> str | None:
    """How the user relates to the item, or None when it is not theirs to track: they authored it, are asked to review
    it, or have a review on it in any state, which a review comment alone also leaves. Pushing commits to it or
    commenting on its conversation, which make the user one of GitHub's participants, is not reviewing it."""
    if item["author"].casefold() == login_key:
        return "authored"
    if any(value.casefold() == login_key for value in item["requested_reviewers"]):
        return "review requested"
    return "reviewing" if item["user_review_state"] is not None else None


def _changed_since(item: dict[str, Any], since_sha: str, detector: Detector, since_base: str | None = None) -> str:
    return detector.detect(
        item["repository"], item["number"], item["base_ref"], since_sha, item["head_sha"], since_base
    )


def _query(item: dict[str, Any], since_sha: str, since_base: str | None = None) -> Query:
    return Query(item["repository"], item["base_ref"], since_sha, item["head_sha"], since_base)


def _ai_review(item: dict[str, Any], detector: Detector) -> str:
    """The row's AI review state, always one of `DASHBOARD_AI_REVIEW_STATES`, which a status override may not take."""
    if item["reviewed_head_sha"] is None:
        return DASHBOARD_AI_REVIEW_MISSING
    # The reviewed head is compared with the base its review was made on, when the record names it; an item that
    # names none, as one built before the base was collected, is compared with the base branch as before.
    change = _changed_since(item, item["reviewed_head_sha"], detector, item.get("reviewed_base_sha"))
    if change != UNCHANGED:
        return DASHBOARD_AI_REVIEW_STALE
    # Re-reviewing an unchanged head would hit the same coverage gap, so it is not re-offered.
    return DASHBOARD_AI_REVIEW_INCOMPLETE if item.get("reviewed_incomplete") else DASHBOARD_AI_REVIEW_CURRENT


def _compares_user_review(item: dict[str, Any], relationship: str) -> bool:
    """Whether the item's section depends on a change since the user's own review."""
    return relationship != "authored" and not item["draft"] and item["user_review_state"] in ACTIVE_REVIEW_STATES


def _section(item: dict[str, Any], relationship: str, detector: Detector, base: str | None) -> str | None:
    """The item's section. The user's own review is compared from `base`, the base `user_review_bases` recorded for it,
    when there is one, and from the base branch as it is now when there is none or GitHub can no longer compare that
    base, so a base the update recorded never answers less than the base branch does."""
    if relationship == "authored":
        return SECTION_MINE
    if item["draft"]:
        return SECTION_DRAFTS
    if not _compares_user_review(item, relationship):
        return SECTION_TO_REVIEW
    state = item["user_review_state"]
    change = _changed_since(item, item["user_review_sha"], detector, base)
    if change == UNKNOWN and base is not None:
        change = _changed_since(item, item["user_review_sha"], detector)
    if state == "APPROVED":
        return None if change == UNCHANGED else SECTION_TO_REVIEW
    return SECTION_TO_REVIEW if change == CHANGED else SECTION_AWAITING


def evaluate(
    items: list[dict[str, Any]],
    login: str,
    detector: BatchDetector,
    *,
    overrides: dict[str, str] | None = None,
    removals: set[str] | None = None,
    review_bases: dict[str, str] | None = None,
) -> list[Row]:
    """The rows to render. The detector reads ahead twice: the comparisons sections need, then the ones the AI review
    column needs for the rows that remain, so an approved pull request left out reads nothing more. `review_bases`
    holds, by pull request, the base the user's own review is compared from (see `user_review_bases`).

    A pull request whose comparison fails reads nothing more either, and once every other row is evaluated,
    `ComparisonFailures` names each that failed, so a call that failed never makes a review look stale."""
    if not login:
        raise TrackerError("GitHub login is required")
    login_key = login.casefold()
    excluded = removals or set()
    pinned = overrides or {}
    related: list[tuple[dict[str, Any], str, str]] = []
    for item in items:
        key = f"{item['repository']}#{item['number']}"
        relationship = _relationship(item, login_key)
        if key not in excluded and relationship is not None:
            related.append((item, key, relationship))
    failures: dict[tuple[str, int], ComparisonError] = {}
    placed = _place(related, pinned, detector, failures, review_bases or {})
    detector.prefetch(
        _query(item, item["reviewed_head_sha"], item["reviewed_base_sha"])
        for item, *_ in placed
        if item["reviewed_head_sha"]
    )
    rows: list[Row] = []
    for item, relationship, section, overridden in placed:
        try:
            rows.append(Row(item, relationship, section, _ai_review(item, detector), overridden))
        except ComparisonError as exc:
            failures[(item["repository"], item["number"])] = exc
    if failures:
        raise ComparisonFailures([failures[pull] for pull in sorted(failures)], len(related))
    return sorted(rows, key=lambda row: (row.item["repository"], row.item["number"]))


def _place(
    related: list[tuple[dict[str, Any], str, str]],
    pinned: dict[str, str],
    detector: BatchDetector,
    failures: dict[tuple[str, int], ComparisonError],
    bases: dict[str, str],
) -> list[tuple[dict[str, Any], str, str, bool]]:
    """Each related item that is shown, with its relationship, its section, and whether an override pinned it, after
    reading ahead the comparisons the sections need, each user review from its base in `bases`. An item whose
    comparison fails is added to `failures` instead."""
    detector.prefetch(
        _query(item, item["user_review_sha"], bases.get(key))
        for item, key, relationship in related
        if key not in pinned and _compares_user_review(item, relationship)
    )
    placed: list[tuple[dict[str, Any], str, str, bool]] = []
    for item, key, relationship in related:
        try:
            section = pinned[key] if key in pinned else _section(item, relationship, detector, bases.get(key))
        except ComparisonError as exc:
            failures[(item["repository"], item["number"])] = exc
            continue
        if section is not None:
            placed.append((item, relationship, section, key in pinned))
    return placed


def review_candidates(rows: list[Row]) -> list[dict[str, Any]]:
    return [
        {
            "repository": row.item["repository"],
            "number": row.item["number"],
            "url": row.item["url"],
            "status": row.ai_review,
        }
        for row in rows
        if row.ai_review in CANDIDATE_STATES and not row.overridden
    ]


def _findings(counts: dict[str, int]) -> str:
    return " ".join(
        f"{counts[key]}{letter}"
        for key, letter in (("MUST_FIX", "M"), ("SHOULD_FIX", "H"), ("SUGGESTION", "S"))
        if counts[key]
    )


def _ledger_findings(review: dict[str, Any] | None) -> str:
    """The remaining work, then the progress: open findings by severity, the repeats of them the review reported at
    other places, and how many are flagged, then what moved since the user's last review, such as
    `1M 1S open (2 repeats, 1 flagged) · 1 new, 1 addressed since your review` or `… · unchanged since your review`.
    Without that review, how many findings were addressed, such as `1H 3S open · 2 addressed`, or the head alone
    when none were or the review cannot say (a legacy review)."""
    if review is None or not review.get("ledger"):
        return "-"
    ledger = review["ledger"]
    opened = _findings(ledger["open"])
    repeats = ledger.get("repeats", 0)
    notes = [
        *([f"{repeats} repeat{'s' * (repeats != 1)}"] if repeats else []),
        *([f"{review['flagged']} flagged"] if review.get("flagged") else []),
    ]
    noted = f" ({', '.join(notes)})" if notes else ""
    head = f"{opened} open{noted}" if opened else "none open"
    since = review.get("since_review")
    if since is not None:
        moved = [f"{since[key]} {key}" for key in ("new", "addressed") if since[key]]
        return f"{head} · {', '.join(moved) if moved else 'unchanged'} since your review"
    return f"{head} · {ledger['addressed']} addressed" if ledger["addressed"] else head


def _ai_cells(row: Row) -> list[str]:
    """AI Result, Findings, and AI Review cells; stale or incomplete reviews are marked in the last cell."""
    review = row.item["ai_review"]
    if row.item["reviewed_head_sha"] is None:
        return ["-", "-", "-"]
    verdict = AI_VERDICTS[review["verdict"]] if review else "-"
    findings = _ledger_findings(review)
    link = ""
    if review and review["report"]:
        link = f"[AI Review]({_file_url(review['report'])})"
    marker = {DASHBOARD_AI_REVIEW_STALE: "(stale)", DASHBOARD_AI_REVIEW_INCOMPLETE: "(incomplete)"}.get(
        row.ai_review, ""
    )
    return [verdict, findings, " ".join(part for part in (link, marker) if part) or "-"]


def _file_url(path: str) -> str:
    return f"vscode://file/{quote(path.replace(chr(92), '/'), safe='/:')}"


def _pull_cells(row: Row) -> list[str]:
    """The Repository and PR cells: the repository's identity, and the pull request's number and title linking to it.
    Both are literal text whatever the repository is named or the author wrote, so neither can add a marker, open a
    comment, or add a cell; a URL that is not absolute HTTPS leaves the number and title unlinked. The owner and name
    are literal on each side of the slash, which stays readable since it is never Markdown on its own."""
    repository = "/".join(markdown_line(part) for part in row.item["repository"].split("/"))
    text = f"#{row.item['number']} {markdown_line(row.item['title'])}"
    url = navigation_url(row.item["url"])
    return [repository, f"[{text}]({url})" if url else text]


def _status(row: Row) -> str:
    """The row's Status: Draft, else the decision standing on the pull request, which is GitHub's review decision on
    one the user authored and the user's latest deciding review on any other. A user whose reviews decided nothing,
    or whose decision was dismissed, reviewed it with comments; with no review it is Open."""
    item = row.item
    if item["draft"]:
        return STATUS_DRAFT
    if row.relationship == "authored":
        return DECISION_STATUSES.get(item["review_decision"], STATUS_OPEN)
    if item["user_review_decision"] in DECISION_STATUSES:
        return DECISION_STATUSES[item["user_review_decision"]]
    return STATUS_OPEN if item["user_review_state"] is None else STATUS_COMMENTED


def _table_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _section_lines(section: str, members: list[Row], summary: str) -> list[str]:
    lines = [
        f"### {markdown_line(section)} ({len(members)})",
        "",
        "<details open>" if section in OPEN_SECTIONS else "<details>",
        f"<summary>{summary}</summary>",
        "",
    ]
    columns = ["Repository", "PR", "Status", "AI Result", "Findings", "AI Review"]
    if section == SECTION_MINE:
        lines += [_table_row(columns), _table_row([":---"] * len(columns))]
        for row in sorted(members, key=lambda r: (not r.item["draft"], r.item["repository"], r.item["number"])):
            lines.append(_table_row([*_pull_cells(row), _status(row), *_ai_cells(row)]))
    else:
        lines += [_table_row(["Requestor", *columns]), _table_row([":---"] * (len(columns) + 1))]

        def name(r: Row) -> str:
            author: str = r.item["author_name"] or r.item["author"]
            return author.strip()

        previous = None
        for row in sorted(members, key=lambda r: (name(r).casefold(), r.item["repository"], r.item["number"])):
            requestor = markdown_line(name(row)) if name(row).casefold() != previous else ""
            previous = name(row).casefold()
            lines.append(_table_row([requestor, *_pull_cells(row), _status(row), *_ai_cells(row)]))
    return [*lines, "", "</details>", ""]


def render(
    rows: list[Row],
    *,
    start_marker: str = DASHBOARD_START_MARKER,
    end_marker: str = DASHBOARD_END_MARKER,
    config_path: str | None = None,
) -> str:
    """Render the owned section: collapsible sections, rows grouped by requestor, AI verdict and findings. Pinned rows
    share a section when their statuses have one `dashboard_status_key`, that is when their headings are the same
    ignoring case, headed as `dashboard_status_heading` writes the first of them in row order."""
    lines = [start_marker, ""]
    if rows:
        lines.extend([FINDINGS_LEGEND, ""])
    pinned_groups: dict[str, list[Row]] = {}
    for row in rows:
        if row.overridden:
            pinned_groups.setdefault(dashboard_status_key(row.section), []).append(row)
    # Summaries sit in an HTML block (a line starting with <summary> opens one), so this one is written in HTML.
    config_link = (
        f'<a href="{html.escape(_file_url(config_path))}">the code-review configuration</a>'
        if config_path
        else "the code-review configuration"
    )
    pinned_summary = (
        f"Pinned by a status override in {config_link}. Set or clear one through update-pr-dashboard, which runs "
        "<code>dashboard_pipeline.py override</code>; do not edit the file by hand."
    )
    pinned = [
        (dashboard_status_heading(members[0].section), members, pinned_summary)
        for _, members in sorted(pinned_groups.items())
    ]
    for heading, members, summary in [
        *(_computed_section(section, rows) for section in (SECTION_TO_REVIEW, SECTION_AWAITING, SECTION_DRAFTS)),
        *pinned,
        _computed_section(SECTION_MINE, rows),
    ]:
        if members:
            lines += _section_lines(heading, members, summary)
    if not rows:
        lines.extend(["_No matching open pull requests_", ""])
    lines.append(end_marker)
    return "\n".join(lines)


def _computed_section(section: str, rows: list[Row]) -> tuple[str, list[Row], str]:
    return section, [row for row in rows if row.section == section and not row.overridden], SECTION_SUMMARIES[section]


def _owned_span(document: str, start_marker: str, end_marker: str) -> tuple[int, int]:
    if document.count(start_marker) != 1 or document.count(end_marker) != 1:
        raise TrackerError("Dashboard must contain exactly one marker pair")
    start = document.index(start_marker)
    try:
        end = document.index(end_marker, start) + len(end_marker)
    except ValueError as exc:
        raise TrackerError("Dashboard markers are malformed") from exc
    if end <= start:
        raise TrackerError("Dashboard markers are malformed")
    return start, end


def splice(
    document: str,
    owned_section: str,
    *,
    start_marker: str = DASHBOARD_START_MARKER,
    end_marker: str = DASHBOARD_END_MARKER,
) -> str:
    """The document with its owned section replaced, written with the document's dominant line ending."""
    start, end = _owned_span(document, start_marker, end_marker)
    crlf = document.count("\r\n")
    if crlf > document.count("\n") - crlf:
        owned_section = owned_section.replace("\n", "\r\n")
    return document[:start] + owned_section + document[end:]


def _read_dashboard(dashboard: Path) -> str:
    """The dashboard exactly as stored: decoded without newline translation, so its line endings survive."""
    try:
        return dashboard.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise TrackerError(str(exc)) from exc


def update_dashboard_rows(
    input_path: Path,
    dashboard: Path,
    login: str | Callable[[], str],
    *,
    detector: BatchDetector | None = None,
    start_marker: str = DASHBOARD_START_MARKER,
    end_marker: str = DASHBOARD_END_MARKER,
    overrides: dict[str, str] | None = None,
    author_names: dict[str, str] | None = None,
    removals: set[str] | list[str] | None = None,
    config_path: str | None = None,
    record: Path | None = None,
    previous: dict[str, Any] | None = None,
) -> list[Row]:
    """Update the owned section and return the rendered rows, then, given a `record` path, write there the pull
    requests the tracker holds, each shown row's AI review, and the base each of the user's own reviews is compared
    from, keeping the one `previous`, the validated record that path held, names for a review commit that has not
    changed (see `user_review_bases`). `login` is the configured user, or what finds it, called
    only once the dashboard and record are known to be writable. A dashboard or record `replacement` refuses is refused
    before GitHub is asked anything, and a removal naming no pull request the tracker holds before anything but that
    call asks it. Each write replaces the file its path leads to, through any symbolic link, and keeps that file's
    mode."""
    try:
        document = read_json(input_path)
    except PersistenceError as exc:
        raise TrackerError(str(exc)) from exc
    items = validate_items(document)
    if not start_marker or not end_marker or start_marker == end_marker:
        raise TrackerError("Dashboard markers must be distinct non-empty strings")
    # Refuse a dashboard without its markers, or one a write would not reach, before asking GitHub anything.
    _owned_span(_read_dashboard(dashboard), start_marker, end_marker)
    dashboard_file, dashboard_mode = replacement(dashboard)
    record_file, record_mode = (None, NEW_FILE_MODE) if record is None else replacement(record)
    if callable(login):
        login = login()
    if not login:
        raise TrackerError("GitHub login is required")
    excluded = normalize_pull_keys(removals or [], "removal")
    tracked = tracked_pulls(items, login)
    unmatched = sorted(excluded - set(tracked))
    if unmatched:
        raise TrackerError(
            f"Removal {unmatched[0]} names no collected pull request the configured user authored, is asked to "
            "review, or has reviewed; check the number, or leave it out if the pull request has closed"
        )
    bases = user_review_bases(items, login, previous)
    rows = evaluate(
        apply_author_names(items, author_names),
        login,
        detector or ChangeDetector(GitHubClient()),
        overrides=normalize_overrides(overrides),
        removals=excluded,
        review_bases={key: base["base_sha"] for key, base in bases.items()},
    )
    owned = render(
        rows,
        start_marker=start_marker,
        end_marker=end_marker,
        config_path=config_path,
    )
    # The GitHub calls above take seconds; read the dashboard again so edits saved meanwhile outside the owned
    # section are kept, since the owned section is rendered from the input alone.
    current = _read_dashboard(dashboard)
    spliced = splice(current, owned, start_marker=start_marker, end_marker=end_marker)
    atomic_write_text(dashboard_file, spliced, mode=dashboard_mode)
    if record_file is not None:
        held = {
            "schema_version": TRACKER_SCHEMA_VERSION,
            "pulls": tracked,
            "reviews": _record_reviews(rows),
            "user_reviews": bases,
        }
        atomic_write_json(record_file, held, validator=validate_tracker_record, mode=record_mode)
    return rows

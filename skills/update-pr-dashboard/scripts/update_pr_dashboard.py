"""Render the owned section of a pull-request tracker from normalized JSON input."""

from __future__ import annotations

import html
import json
import re
import stat
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from markdown_literal import markdown_line, navigation_url
from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pr_change import CHANGED, UNCHANGED, ChangeDetector, ComparisonError, Query
from review_config import (
    COMPUTED_DASHBOARD_STATES,
    DASHBOARD_AI_REVIEW_CURRENT,
    DASHBOARD_AI_REVIEW_INCOMPLETE,
    DASHBOARD_AI_REVIEW_MISSING,
    DASHBOARD_AI_REVIEW_STALE,
    DASHBOARD_AI_REVIEW_STATES,
    DASHBOARD_SECTION_AWAITING,
    DASHBOARD_SECTION_DRAFTS,
    DASHBOARD_SECTION_MINE,
    DASHBOARD_SECTION_TO_REVIEW,
    ConfigurationError,
    normalize_author_names,
    validate_repository_identity,
)
from review_github import GitHubClient
from review_io import atomic_write_json, atomic_write_text
from review_vocabularies import SEVERITIES as SEVERITY_KEYS
from typed_fields import FieldError, FieldReader

START_MARKER = "<!-- code-review-pr-tracker:start -->"
END_MARKER = "<!-- code-review-pr-tracker:end -->"
REQUIRED_FIELDS = {
    "repository",
    "number",
    "url",
    "title",
    "author",
    "requested_reviewers",
    "participants",
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
SECTION_TO_REVIEW = DASHBOARD_SECTION_TO_REVIEW
SECTION_AWAITING = DASHBOARD_SECTION_AWAITING
SECTION_MINE = DASHBOARD_SECTION_MINE
SECTION_DRAFTS = DASHBOARD_SECTION_DRAFTS
OPTIONAL_FIELDS = {"reviewed_incomplete", "author_name", "review_decision", "ai_review"}
LEDGER_KEYS = {"ledger", "flagged", "since_review"}
REVIEW_DECISIONS = {None, "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}
AI_VERDICTS = {"APPROVED": "Approved", "CHANGES_REQUESTED": "Changes Requested", "INCOMPLETE": "Incomplete"}
SECTION_SUMMARIES = {
    SECTION_TO_REVIEW: "PRs you are asked to review or are reviewing where action is needed — new PR, "
    "or the author has responded to your feedback.",
    SECTION_AWAITING: "PRs where you have left a review; waiting for the author to respond or push changes.",
    SECTION_DRAFTS: "Draft PRs you are asked to review or are reviewing; no action needed until the author "
    "marks them ready.",
    SECTION_MINE: "PRs you authored. Status reflects GitHub's review decision on the PR.",
}
OPEN_SECTIONS = {SECTION_TO_REVIEW, SECTION_AWAITING, SECTION_MINE}
FINDINGS_LEGEND = (
    "Findings: M must fix, H should fix, S suggestion; flagged means you flagged it with flag-review-finding."
)
# The tracker record's version: 2 adds each shown row's AI review, which a version 1 record, still read, lacks.
TRACKER_SCHEMA_VERSION = 2
TRACKER_SCHEMA_VERSIONS = (1, TRACKER_SCHEMA_VERSION)
# The AI review states a row is offered for review in, unless a status override pins it.
CANDIDATE_STATES = frozenset({DASHBOARD_AI_REVIEW_MISSING, DASHBOARD_AI_REVIEW_STALE})
# A new file the tracker writes is readable by its owner alone; a file it replaces keeps the mode it had.
NEW_FILE_MODE = 0o600
PULL_KEY_PATTERN = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*)#([1-9][0-9]*)")


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
    def detect(self, repository: str, number: int, base_ref: str, since_sha: str, head_sha: str) -> str:
        """CHANGED, UNCHANGED, or UNKNOWN; raises `ComparisonError` when a GitHub call the comparison needs failed."""


class BatchDetector(Detector, Protocol):
    def prefetch(self, queries: Iterable[Query]) -> None:
        """Read ahead, concurrently, what detecting each (repository, base_ref, since_sha, head_sha) query needs."""


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
    addressed, so both are None; it is None itself when the legacy report is unreadable."""
    if READ.optional(READ.json_object, ledger, path, required={"open", "addressed", "since", "version"}) is None:
        return
    opened = READ.json_object(ledger["open"], f"{path}.open", required=SEVERITY_KEYS)
    for severity in sorted(SEVERITY_KEYS):
        READ.integer(opened[severity], f"{path}.open.{severity}", minimum=0)
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
    """The item with its optional fields defaulted ahead of its own, once its fields match the contract."""
    READ.json_object(item, path, required=REQUIRED_FIELDS, optional=OPTIONAL_FIELDS)
    return {"reviewed_incomplete": False, "author_name": None, "review_decision": None, "ai_review": None, **item}


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
    for field in ("requested_reviewers", "participants"):
        READ.array(item[field], f"{path}.{field}", READ.string)
    READ.boolean(item["draft"], f"{path}.draft")
    for field in ("reviewed_head_sha", "user_review_sha"):
        READ.optional(READ.string, item[field], f"{path}.{field}")


def _validate_review_state(item: dict[str, Any], key: str, path: str) -> None:
    review_state = READ.optional(
        READ.one_of, item["user_review_state"], f"{path}.user_review_state", USER_REVIEW_STATES - {None}
    )
    if review_state is None and item["user_review_sha"] is not None:
        raise TrackerError(f"Tracker item {key}.user_review_sha requires a review state")
    if review_state in ACTIVE_REVIEW_STATES and item["user_review_sha"] is None:
        raise TrackerError(f"Tracker item {key}.user_review_sha is required for {review_state}")


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


def normalize_overrides(value: dict[str, str] | None) -> dict[str, str]:
    if value is None:
        return {}
    normalized: dict[str, str] = {}
    for raw_key, raw_status in READ.json_object(value, "status overrides").items():
        key = next(iter(normalize_pull_keys([READ.string(raw_key, "a key of status overrides")], "override")))
        status = READ.string(raw_status, f"status overrides.{raw_key}", empty=True).strip()
        if not status:
            raise TrackerError(f"Status override {key} must be non-empty")
        if status.casefold() in COMPUTED_DASHBOARD_STATES:
            raise TrackerError(f"Status override {key} duplicates a computed tracker state")
        if key in normalized:
            raise TrackerError(f"Duplicate status override after normalization: {key}")
        normalized[key] = status
    return normalized


def tracked_pulls(items: list[dict[str, Any]], login: str) -> list[str]:
    """The pull requests the tracker holds, sorted: every collected item the user authored, is asked to review, or
    takes part in, whether or not this run shows its row, so a removed or approved one can still be pinned."""
    login_key = login.casefold()
    return sorted(f"{item['repository']}#{item['number']}" for item in items if _relationship(item, login_key))


def validate_tracker_record(value: Any) -> dict[str, Any]:
    """The tracker record `update` keeps beside the configuration: the pull requests its last run held, keyed as a
    status override is, so `override` can refuse one naming none of them, and the AI review each shown row had, so the
    next update can name a review that went stale since. A version 1 record holds no reviews and reads as holding
    none."""
    version = READ.one_of(
        READ.json_object(value, "tracker").get("schema_version"), "tracker.schema_version", TRACKER_SCHEMA_VERSIONS
    )
    fields = {"schema_version", "pulls"} | ({"reviews"} if version == TRACKER_SCHEMA_VERSION else set())
    record = READ.json_object(value, "tracker", required=fields)
    pulls = normalize_pull_keys(READ.array(record["pulls"], "tracker.pulls", READ.string), "tracker")
    reviews = _validate_record_reviews(record.get("reviews", {}), pulls)
    return {"schema_version": TRACKER_SCHEMA_VERSION, "pulls": sorted(pulls), "reviews": reviews}


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


def replacement(path: Path) -> tuple[Path, int]:
    """Where a replacement of `path` is written and the mode it takes: the file the path leads to, through any
    symbolic link, so a link stays a link and its target is what changes, with the mode that file has, or
    `NEW_FILE_MODE` for a file not yet written. A file with another hard link is refused, since a replacement would
    leave that name with the old content."""
    target = path.resolve()
    try:
        status = target.stat()
    except FileNotFoundError:
        return target, NEW_FILE_MODE
    except OSError as exc:
        raise TrackerError(f"Cannot examine {path}: {exc.strerror or exc}") from exc
    if status.st_nlink > 1:
        raise TrackerError(
            f"{path} has another hard link, which a replacement would leave with the old content; give it one name"
        )
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
    authored = item["author"].casefold() == login_key
    requested = any(value.casefold() == login_key for value in item["requested_reviewers"])
    participating = any(value.casefold() == login_key for value in item["participants"])
    if not (authored or requested or participating):
        return None
    return "authored" if authored else "review requested" if requested else "participating"


def _changed_since(item: dict[str, Any], since_sha: str, detector: Detector) -> str:
    return detector.detect(item["repository"], item["number"], item["base_ref"], since_sha, item["head_sha"])


def _query(item: dict[str, Any], since_sha: str) -> Query:
    return (item["repository"], item["base_ref"], since_sha, item["head_sha"])


def _ai_review(item: dict[str, Any], detector: Detector) -> str:
    """The row's AI review state, always one of `DASHBOARD_AI_REVIEW_STATES`, which a status override may not take."""
    if item["reviewed_head_sha"] is None:
        return DASHBOARD_AI_REVIEW_MISSING
    change = _changed_since(item, item["reviewed_head_sha"], detector)
    if change != UNCHANGED:
        return DASHBOARD_AI_REVIEW_STALE
    # Re-reviewing an unchanged head would hit the same coverage gap, so it is not re-offered.
    return DASHBOARD_AI_REVIEW_INCOMPLETE if item.get("reviewed_incomplete") else DASHBOARD_AI_REVIEW_CURRENT


def _compares_user_review(item: dict[str, Any], relationship: str) -> bool:
    """Whether the item's section depends on a change since the user's own review."""
    return relationship != "authored" and not item["draft"] and item["user_review_state"] in ACTIVE_REVIEW_STATES


def _section(item: dict[str, Any], relationship: str, detector: Detector) -> str | None:
    if relationship == "authored":
        return SECTION_MINE
    if item["draft"]:
        return SECTION_DRAFTS
    if not _compares_user_review(item, relationship):
        return SECTION_TO_REVIEW
    state = item["user_review_state"]
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
) -> list[Row]:
    """The rows to render. The detector reads ahead twice: the comparisons sections need, then the ones the AI review
    column needs for the rows that remain, so an approved pull request left out reads nothing more.

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
    placed = _place(related, pinned, detector, failures)
    detector.prefetch(_query(item, item["reviewed_head_sha"]) for item, *_ in placed if item["reviewed_head_sha"])
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
) -> list[tuple[dict[str, Any], str, str, bool]]:
    """Each related item that is shown, with its relationship, its section, and whether an override pinned it, after
    reading ahead the comparisons the sections need. An item whose comparison fails is added to `failures` instead."""
    detector.prefetch(
        _query(item, item["user_review_sha"])
        for item, key, relationship in related
        if key not in pinned and _compares_user_review(item, relationship)
    )
    placed: list[tuple[dict[str, Any], str, str, bool]] = []
    for item, key, relationship in related:
        try:
            section = pinned[key] if key in pinned else _section(item, relationship, detector)
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
    """The remaining work, then the progress: open findings by severity and how many are flagged, then what moved
    since the user's last review, such as `1M 1S open (1 flagged) · 1 new, 1 addressed since your review` or
    `… · unchanged since your review`. Without that review, how many findings were addressed, such as
    `1H 3S open · 2 addressed`, or the head alone when none were or the review cannot say (a legacy review)."""
    if review is None or not review.get("ledger"):
        return "-"
    ledger = review["ledger"]
    opened = _findings(ledger["open"])
    flagged = f" ({review['flagged']} flagged)" if review.get("flagged") else ""
    head = f"{opened} open{flagged}" if opened else "none open"
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


def _pull_link(row: Row, home_repositories: set[str]) -> str:
    """The pull request's link, its title literal text whatever the author wrote, so it cannot add a marker, open a
    comment, or add a cell; a URL that is not absolute HTTPS leaves the text unlinked."""
    repository = row.item["repository"]
    prefix = "" if repository in home_repositories else repository.split("/", 1)[1]
    text = f"{prefix}#{row.item['number']} {markdown_line(row.item['title'])}"
    url = navigation_url(row.item["url"])
    return f"[{text}]({url})" if url else text


def _my_status(item: dict[str, Any]) -> str:
    if item["draft"]:
        return "Draft"
    return {"APPROVED": "Approved", "CHANGES_REQUESTED": "Changes Requested"}.get(item["review_decision"], "Open")


def _section_lines(section: str, members: list[Row], summary: str, home: set[str]) -> list[str]:
    lines = [
        f"### {markdown_line(section)} ({len(members)})",
        "",
        "<details open>" if section in OPEN_SECTIONS else "<details>",
        f"<summary>{summary}</summary>",
        "",
    ]
    if section == SECTION_MINE:
        lines += ["| PR | Status | AI Result | Findings | AI Review |", "| :--- | :--- | :--- | :--- | :--- |"]
        for row in sorted(members, key=lambda r: (not r.item["draft"], r.item["repository"], r.item["number"])):
            lines.append("| " + " | ".join([_pull_link(row, home), _my_status(row.item), *_ai_cells(row)]) + " |")
    else:
        lines += ["| Requestor | PR | AI Result | Findings | AI Review |", "| :--- | :--- | :--- | :--- | :--- |"]

        def name(r: Row) -> str:
            return (r.item["author_name"] or r.item["author"]).strip()

        previous = None
        for row in sorted(members, key=lambda r: (name(r).casefold(), r.item["repository"], r.item["number"])):
            requestor = markdown_line(name(row)) if name(row).casefold() != previous else ""
            previous = name(row).casefold()
            lines.append(f"| {requestor} | " + " | ".join([_pull_link(row, home), *_ai_cells(row)]) + " |")
    return [*lines, "", "</details>", ""]


def render(
    rows: list[Row],
    *,
    start_marker: str = START_MARKER,
    end_marker: str = END_MARKER,
    home_repositories: set[str] | None = None,
    config_path: str | None = None,
) -> str:
    """Render the owned section: collapsible sections, rows grouped by requestor, AI verdict and findings."""
    home = {repository.lower() for repository in (home_repositories or set())}
    lines = [start_marker, ""]
    if rows:
        lines.extend([FINDINGS_LEGEND, ""])
    pinned_sections = sorted({row.section for row in rows if row.overridden}, key=str.casefold)
    # Summaries sit in an HTML block (a line starting with <summary> opens one), so this one is written in HTML.
    config_link = (
        f'<a href="{html.escape(_file_url(config_path))}">the code-review configuration</a>'
        if config_path
        else "the code-review configuration"
    )
    order = [SECTION_TO_REVIEW, SECTION_AWAITING, SECTION_DRAFTS, *pinned_sections, SECTION_MINE]
    for section in order:
        pinned = section in pinned_sections
        members = [row for row in rows if row.section == section and row.overridden == pinned]
        if not members:
            continue
        summary = (
            f"Pinned by a status override in {config_link}. Set or clear one through update-pr-dashboard, which runs "
            "<code>dashboard_pipeline.py override</code>; do not edit the file by hand."
            if pinned
            else SECTION_SUMMARIES[section]
        )
        heading = " ".join(word[:1].upper() + word[1:] for word in section.split()) if pinned else section
        lines += _section_lines(heading, members, summary, home)
    if not rows:
        lines.extend(["_No matching open pull requests_", ""])
    lines.append(end_marker)
    return "\n".join(lines)


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
    start_marker: str = START_MARKER,
    end_marker: str = END_MARKER,
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
    login: str,
    *,
    detector: BatchDetector | None = None,
    start_marker: str = START_MARKER,
    end_marker: str = END_MARKER,
    overrides: dict[str, str] | None = None,
    author_names: dict[str, str] | None = None,
    removals: set[str] | list[str] | None = None,
    home_repositories: set[str] | list[str] | None = None,
    config_path: str | None = None,
    record: Path | None = None,
) -> list[Row]:
    """Update the owned section and return the rendered rows, then, given a `record` path, write there the pull
    requests the tracker holds and each shown row's AI review. A removal naming no pull request the tracker holds, and
    a dashboard or record `replacement` refuses, are refused before GitHub is asked anything. Each write replaces the
    file its path leads to, through any symbolic link, and keeps that file's mode."""
    try:
        items = validate_items(json.loads(input_path.read_text(encoding="utf-8-sig")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TrackerError(str(exc)) from exc
    if not start_marker or not end_marker or start_marker == end_marker:
        raise TrackerError("Dashboard markers must be distinct non-empty strings")
    # Refuse a dashboard without its markers, or one a write would not reach, before asking GitHub anything.
    _owned_span(_read_dashboard(dashboard), start_marker, end_marker)
    dashboard_file, dashboard_mode = replacement(dashboard)
    record_file, record_mode = (None, NEW_FILE_MODE) if record is None else replacement(record)
    if not login:
        raise TrackerError("GitHub login is required")
    excluded = normalize_pull_keys(removals or [], "removal")
    tracked = tracked_pulls(items, login)
    unmatched = sorted(excluded - set(tracked))
    if unmatched:
        raise TrackerError(
            f"Removal {unmatched[0]} names no collected pull request the configured user authored, is asked to "
            "review, or takes part in; check the number, or leave it out if the pull request has closed"
        )
    rows = evaluate(
        apply_author_names(items, author_names),
        login,
        detector or ChangeDetector(GitHubClient()),
        overrides=normalize_overrides(overrides),
        removals=excluded,
    )
    owned = render(
        rows,
        start_marker=start_marker,
        end_marker=end_marker,
        home_repositories=set(home_repositories or []),
        config_path=config_path,
    )
    # The GitHub calls above take seconds; read the dashboard again so edits saved meanwhile outside the owned
    # section are kept, since the owned section is rendered from the input alone.
    current = _read_dashboard(dashboard)
    spliced = splice(current, owned, start_marker=start_marker, end_marker=end_marker)
    atomic_write_text(dashboard_file, spliced, mode=dashboard_mode)
    if record_file is not None:
        held = {"schema_version": TRACKER_SCHEMA_VERSION, "pulls": tracked, "reviews": _record_reviews(rows)}
        atomic_write_json(record_file, held, validator=validate_tracker_record, mode=record_mode)
    return rows

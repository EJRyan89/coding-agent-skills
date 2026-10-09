"""Render the owned section of a pull-request tracker from normalized JSON input."""

from __future__ import annotations

import html
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "code-review-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pr_change import CHANGED, UNCHANGED, ChangeDetector, Query
from review_config import (
    COMPUTED_DASHBOARD_STATES,
    ConfigurationError,
    normalize_author_names,
    validate_repository_identity,
)
from review_github import GitHubClient
from review_io import atomic_write_text

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
SECTION_TO_REVIEW = "To Review"
SECTION_AWAITING = "Awaiting Response"
SECTION_MINE = "My PRs"
SECTION_DRAFTS = "Drafts"
COMPUTED_SECTIONS = [SECTION_TO_REVIEW, SECTION_AWAITING, SECTION_MINE, SECTION_DRAFTS]
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
PULL_KEY_PATTERN = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*)#([1-9][0-9]*)")


class TrackerError(ValueError):
    pass


class Detector(Protocol):
    def detect(self, repository: str, number: int, base_ref: str, since_sha: str, head_sha: str) -> str: ...


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


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_ledger(ledger: Any) -> bool:
    """The latest review's finding-ledger summary. A migrated legacy review's has no version and cannot say what was
    addressed, so both are None; it is None itself when the legacy report is unreadable."""
    if ledger is None:
        return True
    if not isinstance(ledger, dict) or set(ledger) != {"open", "addressed", "since", "version"}:
        return False
    opened, since, version = ledger["open"], ledger["since"], ledger["version"]
    if (
        not isinstance(opened, dict)
        or set(opened) != {"MUST_FIX", "SHOULD_FIX", "SUGGESTION"}
        or not all(_count(value) for value in opened.values())
    ):
        return False
    if version is None:
        return ledger["addressed"] is None and since is None
    if not _count(ledger["addressed"]) or not _count(version) or version < 1:
        return False
    if not any(opened.values()):
        return since is None
    return _count(since) and 1 <= since <= version


def _valid_ledger_reading(review: dict[str, Any]) -> bool:
    """The open findings flags name and what moved since the user's last review, both read from a ledger: flagged
    counts open findings, and since_review is null when the user has not reviewed or the review commit could not be
    placed, else the baseline version (0 when every review came after it) with the entries new and addressed since."""
    if "flagged" not in review and "since_review" not in review:
        return True
    ledger = review.get("ledger")
    if ledger is None or ledger["version"] is None or "flagged" not in review or "since_review" not in review:
        return False
    if not _count(review["flagged"]) or review["flagged"] > sum(ledger["open"].values()):
        return False
    since = review["since_review"]
    if since is None:
        return True
    return (
        isinstance(since, dict)
        and set(since) == {"version", "new", "addressed"}
        and all(_count(value) for value in since.values())
        and since["version"] <= ledger["version"]
    )


def _validate_presentation(item: dict[str, Any]) -> None:
    key = f"{item.get('repository')}#{item.get('number')}"
    name = item["author_name"]
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise TrackerError(f"Tracker item {key}.author_name must be null or a non-empty string")
    if item["review_decision"] not in REVIEW_DECISIONS:
        raise TrackerError(f"Tracker item {key}.review_decision is invalid")
    review = item["ai_review"]
    if review is None:
        return
    # An input collected before reviews kept a finding ledger has no ledger key, and one collected before the tracker
    # read flags and the user's last review has no flagged or since_review keys.
    if not isinstance(review, dict) or set(review) - LEDGER_KEYS != {"verdict", "counts", "report"}:
        raise TrackerError(f"Tracker item {key}.ai_review must have verdict, counts, and report")
    if not _valid_ledger(review.get("ledger")):
        raise TrackerError(f"Tracker item {key}.ai_review.ledger is invalid")
    if not _valid_ledger_reading(review):
        raise TrackerError(f"Tracker item {key}.ai_review.flagged or since_review is invalid")
    if review["verdict"] not in AI_VERDICTS:
        raise TrackerError(f"Tracker item {key}.ai_review.verdict is invalid")
    counts = review["counts"]
    if counts is not None and (
        not isinstance(counts, dict)
        or set(counts) != {"MUST_FIX", "SHOULD_FIX", "SUGGESTION"}
        or any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in counts.values())
    ):
        raise TrackerError(f"Tracker item {key}.ai_review.counts is invalid")
    if review["report"] is not None and (not isinstance(review["report"], str) or not review["report"]):
        raise TrackerError(f"Tracker item {key}.ai_review.report must be null or a path")


def validate_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise TrackerError("Tracker input must be an array")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in value:
        item = _with_defaults(item)
        _validate_presentation(item)
        _validate_incomplete(item)
        repository, key = _item_key(item, seen)
        _validate_fields(item, key)
        _validate_review_state(item, key)
        replacement = dict(item)
        replacement["repository"] = repository
        normalized.append(replacement)
    return normalized


def _with_defaults(item: Any) -> dict[str, Any]:
    """The item with its optional fields defaulted ahead of its own, once its fields match the contract."""
    if not isinstance(item, dict) or not REQUIRED_FIELDS <= set(item) <= REQUIRED_FIELDS | OPTIONAL_FIELDS:
        raise TrackerError("Tracker item fields do not match the contract")
    return {"reviewed_incomplete": False, "author_name": None, "review_decision": None, "ai_review": None, **item}


def _validate_incomplete(item: dict[str, Any]) -> None:
    if not isinstance(item["reviewed_incomplete"], bool):
        raise TrackerError("Tracker item reviewed_incomplete must be Boolean")
    if item["reviewed_incomplete"] and item["reviewed_head_sha"] is None:
        raise TrackerError("Tracker item reviewed_incomplete requires reviewed_head_sha")


def _item_key(item: dict[str, Any], seen: set[str]) -> tuple[str, str]:
    """The item's normalized repository and its owner/name#number key, which it adds to seen once it is new."""
    repository = validate_repository_identity(item["repository"])
    number = item["number"]
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise TrackerError("Tracker pull number must be positive")
    key = f"{repository}#{number}"
    if key in seen:
        raise TrackerError(f"Duplicate tracker item: {key}")
    seen.add(key)
    return repository, key


def _validate_fields(item: dict[str, Any], key: str) -> None:
    for field in ("url", "title", "author", "base_ref", "head_sha", "updated_at"):
        if not isinstance(item[field], str) or not item[field]:
            raise TrackerError(f"Tracker item {key}.{field} is required")
    for field in ("requested_reviewers", "participants"):
        if not isinstance(item[field], list) or any(not isinstance(login, str) or not login for login in item[field]):
            raise TrackerError(f"Tracker item {key}.{field} must be a string array")
    if not isinstance(item["draft"], bool):
        raise TrackerError(f"Tracker item {key}.draft must be Boolean")
    for field in ("reviewed_head_sha", "user_review_sha"):
        sha = item[field]
        if sha is not None and (not isinstance(sha, str) or not sha):
            raise TrackerError(f"Tracker item {key}.{field} is invalid")


def _validate_review_state(item: dict[str, Any], key: str) -> None:
    review_state = item["user_review_state"]
    if review_state not in USER_REVIEW_STATES:
        raise TrackerError(f"Tracker item {key}.user_review_state is invalid")
    if review_state is None and item["user_review_sha"] is not None:
        raise TrackerError(f"Tracker item {key}.user_review_sha requires a review state")
    if review_state in ACTIVE_REVIEW_STATES and item["user_review_sha"] is None:
        raise TrackerError(f"Tracker item {key}.user_review_sha is required for {review_state}")


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def normalize_pull_keys(values: set[str] | list[str], field: str) -> set[str]:
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise TrackerError(f"{field} entries must be pull-request identities")
        match = PULL_KEY_PATTERN.fullmatch(value)
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
    if not isinstance(value, dict):
        raise TrackerError("Status overrides must be a string-to-string object")
    normalized: dict[str, str] = {}
    for raw_key, raw_status in value.items():
        if not isinstance(raw_key, str) or not isinstance(raw_status, str):
            raise TrackerError("Status overrides must be a string-to-string object")
        key = next(iter(normalize_pull_keys([raw_key], "override")))
        status = raw_status.strip()
        if not status:
            raise TrackerError(f"Status override {key} must be non-empty")
        if status.casefold() in COMPUTED_DASHBOARD_STATES:
            raise TrackerError(f"Status override {key} duplicates a computed tracker state")
        if key in normalized:
            raise TrackerError(f"Duplicate status override after normalization: {key}")
        normalized[key] = status
    return normalized


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
    if item["reviewed_head_sha"] is None:
        return "missing"
    change = _changed_since(item, item["reviewed_head_sha"], detector)
    if change != UNCHANGED:
        return "stale"
    # Re-reviewing an unchanged head would hit the same coverage gap, so it is not re-offered.
    return "incomplete" if item.get("reviewed_incomplete") else "current"


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
    column needs for the rows that remain, so an approved pull request left out reads nothing more."""
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
    detector.prefetch(
        _query(item, item["user_review_sha"])
        for item, key, relationship in related
        if key not in pinned and _compares_user_review(item, relationship)
    )
    placed: list[tuple[dict[str, Any], str, str, bool]] = []
    for item, key, relationship in related:
        section = pinned[key] if key in pinned else _section(item, relationship, detector)
        if section is not None:
            placed.append((item, relationship, section, key in pinned))
    detector.prefetch(_query(item, item["reviewed_head_sha"]) for item, *_ in placed if item["reviewed_head_sha"])
    rows = [
        Row(item, relationship, section, _ai_review(item, detector), overridden)
        for item, relationship, section, overridden in placed
    ]
    return sorted(rows, key=lambda row: (row.item["repository"], row.item["number"]))


def review_candidates(rows: list[Row]) -> list[dict[str, Any]]:
    return [
        {
            "repository": row.item["repository"],
            "number": row.item["number"],
            "url": row.item["url"],
            "status": row.ai_review,
        }
        for row in rows
        if row.ai_review in {"missing", "stale"} and not row.overridden
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
    marker = {"stale": "(stale)", "incomplete": "(incomplete)"}.get(row.ai_review, "")
    return [verdict, findings, " ".join(part for part in (link, marker) if part) or "-"]


def _file_url(path: str) -> str:
    return f"vscode://file/{quote(path.replace(chr(92), '/'), safe='/:')}"


def _pull_link(row: Row, home_repositories: set[str]) -> str:
    repository = row.item["repository"]
    prefix = "" if repository in home_repositories else repository.split("/", 1)[1]
    return f"[{prefix}#{row.item['number']} {_escape(row.item['title'])}]({row.item['url']})"


def _my_status(item: dict[str, Any]) -> str:
    if item["draft"]:
        return "Draft"
    return {"APPROVED": "Approved", "CHANGES_REQUESTED": "Changes Requested"}.get(item["review_decision"], "Open")


def _section_lines(section: str, members: list[Row], summary: str, home: set[str]) -> list[str]:
    lines = [
        f"### {_escape(section)} ({len(members)})",
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
            requestor = _escape(name(row)) if name(row).casefold() != previous else ""
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
            "Manually managed — edit <code>dashboard.status_overrides</code> in "
            f"{config_link} to add or remove entries."
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
) -> list[Row]:
    """Update the owned section and return the rendered rows."""
    try:
        items = validate_items(json.loads(input_path.read_text(encoding="utf-8-sig")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TrackerError(str(exc)) from exc
    if not start_marker or not end_marker or start_marker == end_marker:
        raise TrackerError("Dashboard markers must be distinct non-empty strings")
    # Refuse a dashboard without its markers before asking GitHub anything.
    _owned_span(_read_dashboard(dashboard), start_marker, end_marker)
    rows = evaluate(
        apply_author_names(items, author_names),
        login,
        detector or ChangeDetector(GitHubClient()),
        overrides=normalize_overrides(overrides),
        removals=normalize_pull_keys(removals or [], "removal"),
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
    atomic_write_text(dashboard, splice(current, owned, start_marker=start_marker, end_marker=end_marker))
    return rows

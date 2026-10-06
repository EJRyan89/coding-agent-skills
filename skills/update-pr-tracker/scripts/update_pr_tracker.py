"""Render the owned section of a pull-request tracker from normalized JSON input."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

CORE_SCRIPTS = Path(__file__).resolve().parents[2] / "code-review-core" / "scripts"
sys.path.insert(0, str(CORE_SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pr_change import CHANGED, UNCHANGED, ChangeDetector
from review_config import (
    COMPUTED_DASHBOARD_STATES,
    ConfigurationError,
    normalize_author_names,
    validate_repository_identity,
)
from review_github import GitHubClient, GitHubError
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
PULL_KEY_PATTERN = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*)#([1-9][0-9]*)"
)


class TrackerError(ValueError):
    pass


class Detector(Protocol):
    def detect(
        self, repository: str, number: int, base_ref: str, since_sha: str, head_sha: str
    ) -> str: ...


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
    """The latest review's finding-ledger summary, or None for a review recorded before ledgers."""
    if ledger is None:
        return True
    if not isinstance(ledger, dict) or set(ledger) != {"open", "addressed", "since", "version"}:
        return False
    opened, since, version = ledger["open"], ledger["since"], ledger["version"]
    if not isinstance(opened, dict) or set(opened) != {"MUST_FIX", "SHOULD_FIX", "SUGGESTION"} \
            or not all(_count(value) for value in opened.values()):
        return False
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
    if ledger is None or "flagged" not in review or "since_review" not in review:
        return False
    if not _count(review["flagged"]) or review["flagged"] > sum(ledger["open"].values()):
        return False
    since = review["since_review"]
    if since is None:
        return True
    return (isinstance(since, dict) and set(since) == {"version", "new", "addressed"}
            and all(_count(value) for value in since.values()) and since["version"] <= ledger["version"])


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
        if not isinstance(item, dict) or not REQUIRED_FIELDS <= set(item) <= REQUIRED_FIELDS | OPTIONAL_FIELDS:
            raise TrackerError("Tracker item fields do not match the contract")
        item = {"reviewed_incomplete": False, "author_name": None, "review_decision": None, "ai_review": None, **item}
        _validate_presentation(item)
        if not isinstance(item["reviewed_incomplete"], bool):
            raise TrackerError("Tracker item reviewed_incomplete must be Boolean")
        if item["reviewed_incomplete"] and item["reviewed_head_sha"] is None:
            raise TrackerError("Tracker item reviewed_incomplete requires reviewed_head_sha")
        repository = validate_repository_identity(item["repository"])
        number = item["number"]
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise TrackerError("Tracker pull number must be positive")
        key = f"{repository}#{number}"
        if key in seen:
            raise TrackerError(f"Duplicate tracker item: {key}")
        seen.add(key)
        for field in ("url", "title", "author", "base_ref", "head_sha", "updated_at"):
            if not isinstance(item[field], str) or not item[field]:
                raise TrackerError(f"Tracker item {key}.{field} is required")
        for field in ("requested_reviewers", "participants"):
            if not isinstance(item[field], list) or any(
                not isinstance(login, str) or not login for login in item[field]
            ):
                raise TrackerError(f"Tracker item {key}.{field} must be a string array")
        if not isinstance(item["draft"], bool):
            raise TrackerError(f"Tracker item {key}.draft must be Boolean")
        for field in ("reviewed_head_sha", "user_review_sha"):
            sha = item[field]
            if sha is not None and (not isinstance(sha, str) or not sha):
                raise TrackerError(f"Tracker item {key}.{field} is invalid")
        review_state = item["user_review_state"]
        if review_state not in USER_REVIEW_STATES:
            raise TrackerError(f"Tracker item {key}.user_review_state is invalid")
        if review_state is None and item["user_review_sha"] is not None:
            raise TrackerError(f"Tracker item {key}.user_review_sha requires a review state")
        if review_state in ACTIVE_REVIEW_STATES and item["user_review_sha"] is None:
            raise TrackerError(
                f"Tracker item {key}.user_review_sha is required for {review_state}"
            )
        replacement = dict(item)
        replacement["repository"] = repository
        normalized.append(replacement)
    return normalized


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
            raise TrackerError(
                f"Status override {key} duplicates a computed tracker state"
            )
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
    return detector.detect(
        item["repository"], item["number"], item["base_ref"], since_sha, item["head_sha"]
    )


def _ai_review(item: dict[str, Any], detector: Detector) -> str:
    if item["reviewed_head_sha"] is None:
        return "missing"
    change = _changed_since(item, item["reviewed_head_sha"], detector)
    if change != UNCHANGED:
        return "stale"
    # Re-reviewing an unchanged head would hit the same coverage gap, so it is not re-offered.
    return "incomplete" if item.get("reviewed_incomplete") else "current"


def _section(item: dict[str, Any], relationship: str, detector: Detector) -> str | None:
    if relationship == "authored":
        return SECTION_MINE
    if item["draft"]:
        return SECTION_DRAFTS
    state = item["user_review_state"]
    if state not in ACTIVE_REVIEW_STATES:
        return SECTION_TO_REVIEW
    change = _changed_since(item, item["user_review_sha"], detector)
    if state == "APPROVED":
        return None if change == UNCHANGED else SECTION_TO_REVIEW
    return SECTION_TO_REVIEW if change == CHANGED else SECTION_AWAITING


def evaluate(
    items: list[dict[str, Any]],
    login: str,
    detector: Detector,
    *,
    overrides: dict[str, str] | None = None,
    removals: set[str] | None = None,
) -> list[Row]:
    if not login:
        raise TrackerError("GitHub login is required")
    login_key = login.casefold()
    excluded = removals or set()
    pinned = overrides or {}
    rows: list[Row] = []
    for item in items:
        key = f"{item['repository']}#{item['number']}"
        if key in excluded:
            continue
        relationship = _relationship(item, login_key)
        if relationship is None:
            continue
        if key in pinned:
            section: str | None = pinned[key]
        else:
            section = _section(item, relationship, detector)
        if section is None:
            continue
        rows.append(
            Row(item, relationship, section, _ai_review(item, detector), key in pinned)
        )
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
        if row.ai_review in {"missing", "stale"}
        and not row.overridden
    ]


def _findings(counts: dict[str, int] | None) -> str:
    if not counts:
        return "-"
    parts = [f"{counts[key]}{letter}" for key, letter in (("MUST_FIX", "M"), ("SHOULD_FIX", "H"), ("SUGGESTION", "S"))
             if counts[key]]
    return " ".join(parts) or "-"


def _ledger_findings(review: dict[str, Any]) -> str:
    """Open findings by severity and how many are flagged, then what moved since the user's last review, such as
    `1M 1S open (1 flagged) · 1 new, 1 addressed since your review`; without that review, the versions from the oldest
    open finding to the latest review, such as `1M 1S open · v1–v3`."""
    ledger = review["ledger"]
    opened = _findings(ledger["open"])
    flagged = f" ({review['flagged']} flagged)" if review.get("flagged") else ""
    head = "none open" if opened == "-" else f"{opened} open{flagged}"
    since = review.get("since_review")
    if since is None:
        first = ledger["since"] if ledger["since"] is not None else ledger["version"]
        span = f"v{first}–v{ledger['version']}" if first != ledger["version"] else f"v{ledger['version']}"
        return f"{head} · {span}"
    moved = [f"{since[key]} {key}" for key in ("new", "addressed") if since[key]]
    return f"{head} · {', '.join(moved) if moved else 'nothing new'} since your review"


def _ai_cells(row: Row) -> list[str]:
    """AI Result, Findings, and AI Review cells; stale or incomplete reviews are marked in the last cell."""
    review = row.item["ai_review"]
    if row.item["reviewed_head_sha"] is None:
        return ["-", "-", "-"]
    verdict = AI_VERDICTS[review["verdict"]] if review else "-"
    if review and review.get("ledger"):
        findings = _ledger_findings(review)
    else:
        findings = _findings(review["counts"]) if review else "-"
    link = ""
    if review and review["report"]:
        link = f"[AI Review](vscode://file/{quote(review['report'].replace(chr(92), '/'), safe='/:')})"
    marker = {"stale": "(stale)", "incomplete": "(incomplete)"}.get(row.ai_review, "")
    return [verdict, findings, " ".join(part for part in (link, marker) if part) or "-"]


def _pull_link(row: Row, home_repositories: set[str]) -> str:
    repository = row.item["repository"]
    prefix = "" if repository in home_repositories else repository.split("/", 1)[1]
    return f"[{prefix}#{row.item['number']} {_escape(row.item['title'])}]({row.item['url']})"


def _my_status(item: dict[str, Any]) -> str:
    if item["draft"]:
        return "Draft"
    return {"APPROVED": "Approved", "CHANGES_REQUESTED": "Changes Requested"}.get(item["review_decision"], "Open")


def _section_lines(section: str, members: list[Row], summary: str, home: set[str]) -> list[str]:
    lines = [f"### {_escape(section)} ({len(members)})", "",
             "<details open>" if section in OPEN_SECTIONS else "<details>", f"<summary>{summary}</summary>", ""]
    if section == SECTION_MINE:
        lines += ["| PR | Status | AI Result | Findings | AI Review |", "| :--- | :--- | :--- | :--- | :--- |"]
        for row in sorted(members, key=lambda r: (not r.item["draft"], r.item["repository"], r.item["number"])):
            lines.append("| " + " | ".join([_pull_link(row, home), _my_status(row.item), *_ai_cells(row)]) + " |")
    else:
        lines += ["| Requestor | PR | AI Result | Findings | AI Review |", "| :--- | :--- | :--- | :--- | :--- |"]
        name = lambda r: (r.item["author_name"] or r.item["author"]).strip()
        previous = None
        for row in sorted(members, key=lambda r: (name(r).casefold(), r.item["repository"], r.item["number"])):
            requestor = _escape(name(row)) if name(row).casefold() != previous else ""
            previous = name(row).casefold()
            lines.append(f"| {requestor} | " + " | ".join([_pull_link(row, home), *_ai_cells(row)]) + " |")
    return lines + ["", "</details>", ""]


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
    pinned_sections = sorted({row.section for row in rows if row.overridden}, key=str.casefold)
    config_link = (f"[the code-review configuration](vscode://file/{quote(config_path.replace(chr(92), '/'), safe='/:')})"
                   if config_path else "the code-review configuration")
    order = [SECTION_TO_REVIEW, SECTION_AWAITING, SECTION_DRAFTS, *pinned_sections, SECTION_MINE]
    for section in order:
        pinned = section in pinned_sections
        members = [row for row in rows if row.section == section and row.overridden == pinned]
        if not members:
            continue
        summary = (f"Manually managed — edit `dashboard.status_overrides` in {config_link} to add or remove entries."
                   if pinned else SECTION_SUMMARIES[section])
        heading = " ".join(word[:1].upper() + word[1:] for word in section.split()) if pinned else section
        lines += _section_lines(heading, members, summary, home)
    if not rows:
        lines.extend(["_No matching open pull requests_", ""])
    lines.append(end_marker)
    return "\n".join(lines)


def splice(
    document: str,
    owned_section: str,
    *,
    start_marker: str = START_MARKER,
    end_marker: str = END_MARKER,
) -> str:
    if document.count(start_marker) != 1 or document.count(end_marker) != 1:
        raise TrackerError("Dashboard must contain exactly one marker pair")
    start = document.index(start_marker)
    try:
        end = document.index(end_marker, start) + len(end_marker)
    except ValueError as exc:
        raise TrackerError("Dashboard markers are malformed") from exc
    if end <= start:
        raise TrackerError("Dashboard markers are malformed")
    return document[:start] + owned_section + document[end:]


def update_dashboard(input_path: Path, dashboard: Path, login: str, **options: Any) -> list[dict[str, Any]]:
    """Update the owned section and return the relevant pull requests whose AI review is missing or stale."""
    return review_candidates(update_dashboard_rows(input_path, dashboard, login, **options))


def update_dashboard_rows(
    input_path: Path,
    dashboard: Path,
    login: str,
    *,
    detector: Detector | None = None,
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
        current = dashboard.read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TrackerError(str(exc)) from exc
    if not start_marker or not end_marker or start_marker == end_marker:
        raise TrackerError("Dashboard markers must be distinct non-empty strings")
    rows = evaluate(
        apply_author_names(items, author_names),
        login,
        detector or ChangeDetector(GitHubClient()),
        overrides=normalize_overrides(overrides),
        removals=normalize_pull_keys(removals or [], "removal"),
    )
    owned = render(rows, start_marker=start_marker, end_marker=end_marker,
                   home_repositories=set(home_repositories or []), config_path=config_path)
    atomic_write_text(
        dashboard,
        splice(current, owned, start_marker=start_marker, end_marker=end_marker),
    )
    return rows


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--dashboard", type=Path, required=True)
    parser.add_argument("--login", required=True)
    parser.add_argument("--start-marker", default=START_MARKER)
    parser.add_argument("--end-marker", default=END_MARKER)
    parser.add_argument("--overrides", type=Path)
    parser.add_argument("--remove", action="append", default=[])
    parser.add_argument("--print-review-candidates", action="store_true")
    parser.add_argument("--home-repository", action="append", default=[],
                        help="owner/repo whose pull requests render as #N instead of name#N; repeatable")
    parser.add_argument("--config-path", help="code-review configuration file linked from pinned sections")
    args = parser.parse_args(arguments)
    try:
        overrides = None
        if args.overrides:
            overrides = json.loads(args.overrides.read_text(encoding="utf-8-sig"))
        candidates = update_dashboard(
            args.input,
            args.dashboard,
            args.login,
            start_marker=args.start_marker,
            end_marker=args.end_marker,
            overrides=overrides,
            removals=args.remove,
            home_repositories=[validate_repository_identity(r) for r in args.home_repository],
            config_path=args.config_path,
        )
        if args.print_review_candidates:
            print(json.dumps(candidates, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (TrackerError, GitHubError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

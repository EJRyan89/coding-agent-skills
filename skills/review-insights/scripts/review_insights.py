"""Create reproducible insights from validated structured review records, and record decisions on them.

    report  analyze the configured archive for a date range and repository scope; write insights.json and .md
    decide  record one recommendation's decision in a report; an accepted one resolves its linked flags

Every command prints machine-readable lines and exits 0 on success. Expected failures print
`FAILED <reason>` on stderr and exit 2.

There are two kinds of recommendation. A category recommendation covers every finding in one finding category. An
analyzer recommendation covers the findings reviewers said one diagnostic analyzer rule could catch: a rule in an
analyzer the repository already has but does not enforce (available), a rule in an established analyzer it does not
use (known), or a pattern no rule covers yet (custom-candidate). Analyzer recommendations are ranked in that order,
because enforcing a rule the repository already has is the cheapest way to stop a recurring finding. A finding with
analyzer coverage belongs to its category recommendation and to its analyzer recommendation.

A flag is linked to a recommendation when the flag is open, names a repository, pull request, review version,
and finding, and that analyzed review has that finding among the recommendation's findings. Finding IDs
restart in every review, so a flag is never matched against another review's finding. Links are computed when
the report is written, so `decide` resolves exactly the flags it listed.

Each recommendation also counts its findings by the reviewer that raised them and the model that reviewer ran on,
read from the review record: a finding raised by several reviewers counts for each, and a reviewer whose record
names no model counts under `unknown`.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

CORE_SCRIPTS = Path(__file__).resolve().parents[2] / "code-review-core" / "scripts"
sys.path.insert(0, str(CORE_SCRIPTS))

from review_config import (
    ConfigurationError,
    load_config,
    resolve_repositories,
    selected_repository_set,
    validate_repository_identity,
)
from review_flags import FlagError, default_flags_path, load_store, resolve_flag
from review_io import PersistenceError, atomic_write_json, atomic_write_text, read_json
from review_records import ANALYZER_COVERAGES, RecordError, valid_analyzer, validate_record_pair

# Version 2 adds each recommendation's decision_history and linked_flags. Version 3 links a flag only to the
# finding in the review version it names; earlier reports linked it to whatever finding had its ID in the latest
# review, so their links are dropped when read, and a decision on one resolves no flag until it is regenerated.
# Version 4 adds each recommendation's reviewers breakdown; an earlier report has none until it is regenerated.
# Version 5 adds each recommendation's kind and the analyzer recommendations; every earlier one is a category one.
SCHEMA_VERSION = 5
READABLE_SCHEMA_VERSIONS = {1, 2, 3, 4, 5}
UNKNOWN_MODEL = "unknown"
REVIEWER_FIELDS = {"reviewer", "model", "findings", "flagged_findings"}
EVIDENCE_FIELDS = {"repository", "pull_number", "review_version", "finding_id", "path", "line", "title"}
DECISIONS = ("accepted", "rejected", "deferred")
KINDS = ("category", "analyzer")
SET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
REC_ID = re.compile(r"REC-(\d{3,})")
EXAMPLES_PRINTED = 3
EVIDENCE_RENDERED = 10

ReviewKey = tuple[str, int, int]
FindingKey = tuple[str, int, int, str]
Pair = tuple[dict[str, Any], dict[str, Any]]  # (record, finding)


class InsightError(ValueError):
    pass


EXPECTED_ERRORS = (InsightError, ConfigurationError, FlagError, PersistenceError, OSError)


@dataclass
class Services:
    """External effects, replaceable in tests."""

    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    flags_path: Callable[[], Path] = default_flags_path


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InsightError(f"Invalid ISO date: {value}") from exc


def collect_records(
    archive_root: Path, repositories: list[str], start: date, end: date
) -> list[tuple[Path, dict[str, Any]]]:
    if start > end:
        raise InsightError("Start date must not be after end date")
    normalized = [validate_repository_identity(value) for value in repositories]
    if not normalized or len(set(normalized)) != len(normalized):
        raise InsightError("Repositories must be a non-empty unique list")
    records: list[tuple[Path, dict[str, Any]]] = []
    for repository in normalized:
        owner, name = repository.split("/", 1)
        base = archive_root / owner / name / "pulls"
        if not base.exists():
            continue
        for json_path in base.glob("*/review*.json"):
            markdown_path = json_path.with_suffix(".md")
            try:
                record = validate_record_pair(json_path, markdown_path)
                reviewed = date.fromisoformat(record["review"]["reviewed_at"][:10])
            except (KeyError, ValueError, OSError, RecordError) as exc:
                raise InsightError(f"Invalid review pair {json_path}: {exc}") from exc
            if start <= reviewed <= end:
                records.append((json_path, record))
    return sorted(records, key=lambda item: str(item[0]).casefold())


def _review_key(record: dict[str, Any]) -> ReviewKey:
    return record["repository"].lower(), record["pull_request"]["number"], record["review"]["version"]


def _finding_key(record: dict[str, Any], finding: dict[str, Any]) -> FindingKey:
    return (*_review_key(record), finding["id"])


def flags_by_finding(records: list[tuple[Path, dict[str, Any]]], flags: list[dict[str, Any]]) -> dict[FindingKey, list[str]]:
    """Open flag IDs by the analyzed finding each names, following each flag to the review version it names."""
    reviews = {_review_key(record): record for _, record in records}
    linked: dict[FindingKey, list[str]] = {}
    for flag in flags:
        key = (flag["repository"], flag["pull_number"], flag["review_version"], flag["finding_id"])
        if flag["status"] != "open" or None in key:
            continue
        review = (flag["repository"].lower(), flag["pull_number"], flag["review_version"])
        record = reviews.get(review)
        for finding in record["findings"] if record else []:
            if finding["id"] == flag["finding_id"]:
                linked.setdefault((*review, finding["id"]), []).append(flag["id"])
    return linked


def raised_by(record: dict[str, Any], finding: dict[str, Any]) -> list[tuple[str, str]]:
    """The reviewers that raised a finding, each with the model it ran on.

    A record's sole reviewer raised every finding (a repository entrypoint reviewer's source is its own free text);
    otherwise a finding merged from several reviewers names each in its source, as `finalize` counts them. A record
    written before reviewers were recorded has only the source, so its parts are the reviewers.
    """
    reviewers = {item["id"]: item.get("model", UNKNOWN_MODEL) for item in record["review"].get("reviewers", [])}
    if len(reviewers) == 1:
        return list(reviewers.items())
    parts = list(dict.fromkeys(part.strip() for part in finding["source"].split(" + ") if part.strip()))
    named = [(part, reviewers[part]) for part in parts if part in reviewers]
    return named or [(part, UNKNOWN_MODEL) for part in parts]


def reviewer_breakdown(pairs: list[Pair], flagged: set[FindingKey]) -> list[dict[str, Any]]:
    """How many of a recommendation's findings each reviewer raised on each model, and how many of those an open
    flag names. A finding raised by several reviewers counts for each, so the counts can sum past the findings."""
    counts: Counter[tuple[str, str]] = Counter()
    flagged_counts: Counter[tuple[str, str]] = Counter()
    for record, finding in pairs:
        is_flagged = _finding_key(record, finding) in flagged
        for pair in raised_by(record, finding):
            counts[pair] += 1
            flagged_counts[pair] += int(is_flagged)
    return [
        {"reviewer": reviewer, "model": model, "findings": count, "flagged_findings": flagged_counts[(reviewer, model)]}
        for (reviewer, model), count in sorted(
            counts.items(), key=lambda item: (-item[1], item[0][0].casefold(), item[0][1].casefold())
        )
    ]


def subject_key(item: dict[str, Any]) -> tuple[str, ...]:
    """What a recommendation is about, so a regenerated report keeps its ID, decision, and history. Analyzer names
    are matched without regard to case, as reviewers are."""
    if item.get("kind", "category") == "category":
        return ("category", item["category"])
    return ("analyzer", item["coverage"], item["tool"].casefold(), item["rule"].casefold())


def describe(item: dict[str, Any]) -> str:
    if item["kind"] == "category":
        return item["category"]
    return f"{item['coverage']} {item['tool']} {item['rule']}"


def _repositories_text(repositories: list[str]) -> str:
    return repositories[0] if len(repositories) == 1 else ", ".join(repositories[:-1]) + " and " + repositories[-1]


def _analyzer_recommendation(coverage: str, tool: str, rule: str, repositories: list[str]) -> str:
    where = _repositories_text(repositories)
    if coverage == "available":
        return (f"{tool}, which {where} already has, provides {rule}, but the rule is not enforced. Enable it or raise "
                "its severity in the analyzer's configuration so the build reports it instead of a reviewer.")
    if coverage == "known":
        return (f"{tool} provides {rule}, and {where} does not use {tool}. Consider adopting it for this rule, after "
                "checking its license, cost, and telemetry.")
    return (f"No existing analyzer rule catches the {rule} pattern found in {where}. Consider writing a custom {tool} "
            "rule for it.")


def _evidence(record: dict[str, Any], finding: dict[str, Any]) -> dict[str, Any]:
    return {"repository": record["repository"].lower(), "pull_number": record["pull_request"]["number"],
            "review_version": record["review"]["version"], "finding_id": finding["id"], "path": finding["path"],
            "line": finding["line"], "title": finding.get("title")}


def analyze(
    records: list[tuple[Path, dict[str, Any]]],
    *,
    flags: list[dict[str, Any]] | None = None,
    previous: dict[tuple[str, ...], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Recommendations by category, then by analyzer rule; a subject already in `previous` keeps its decision and
    history."""
    category_counts: Counter[str] = Counter()
    severity_counts: Counter[str] = Counter()
    groups: dict[tuple[str, ...], list[Pair]] = {}
    spellings: dict[tuple[str, ...], dict[str, str]] = {}
    for _, record in records:
        for finding in record["findings"]:
            category_counts[finding["category"]] += 1
            severity_counts[finding["severity"]] += 1
            groups.setdefault(("category", finding["category"]), []).append((record, finding))
            analyzer = finding.get("analyzer")
            if analyzer is not None and valid_analyzer(analyzer):
                # The first spelling in record order names the rule, so a report reads the same when regenerated.
                key = subject_key({"kind": "analyzer", **analyzer})
                groups.setdefault(key, []).append((record, finding))
                spellings.setdefault(key, analyzer)
    linked = flags_by_finding(records, flags or [])
    flagged = set(linked)
    earlier = previous or {}
    # A subject keeps the ID it had when the report was regenerated, so an ID the user was shown never comes to
    # name another subject; a new subject gets a number no earlier recommendation used.
    used = [int(match.group(1)) for item in earlier.values() if (match := REC_ID.fullmatch(item.get("id", "")))]
    next_number = max(used, default=0) + 1
    categories = sorted((key for key in groups if key[0] == "category"),
                        key=lambda key: (-len(groups[key]), key[1].casefold()))
    analyzers = sorted((key for key in groups if key[0] == "analyzer"),
                       key=lambda key: (ANALYZER_COVERAGES.index(key[1]), -len(groups[key]), key[2], key[3]))
    recommendations = []
    for key in [*categories, *analyzers]:
        pairs = groups[key]
        prior = earlier.get(key, {})
        identifier = prior.get("id")
        if not isinstance(identifier, str) or not REC_ID.fullmatch(identifier):
            identifier = f"REC-{next_number:03d}"
            next_number += 1
        links = sorted({flag for record, finding in pairs for flag in linked.get(_finding_key(record, finding), [])})
        common = {
            "finding_count": len(pairs),
            "decision": prior.get("decision", "deferred"),
            "decision_history": list(prior.get("decision_history", [])),
            "linked_flags": links,
            "reviewers": reviewer_breakdown(pairs, flagged),
        }
        if key[0] == "category":
            category = key[1]
            recommendations.append({
                "id": identifier, "kind": "category", "category": category,
                "recommendation": f"Review recurring {category} findings and decide whether guidance or reviewer rules should change.",
                **common,
            })
            continue
        analyzer = spellings[key]
        repositories = sorted({record["repository"].lower() for record, _ in pairs})
        recommendations.append({
            "id": identifier, "kind": "analyzer", "coverage": analyzer["coverage"], "tool": analyzer["tool"],
            "rule": analyzer["rule"], "repositories": repositories,
            "recommendation": _analyzer_recommendation(analyzer["coverage"], analyzer["tool"], analyzer["rule"],
                                                       repositories),
            **common,
            "evidence": [_evidence(record, finding) for record, finding in pairs],
        })
    return {
        "record_count": len(records),
        "finding_count": sum(category_counts.values()),
        "severity_counts": dict(sorted(severity_counts.items())),
        "category_counts": dict(sorted(category_counts.items())),
        "recommendations": recommendations,
    }


def _render_recommendation(item: dict[str, Any], heading: str) -> list[str]:
    lines = [f"### {item['id']} — {heading}", "", f"Finding count: {item['finding_count']}"]
    if item["kind"] == "analyzer":
        lines.append(f"Repositories: {', '.join(item['repositories'])}")
    lines.extend([
        f"Decision: {item['decision']}",
        f"Linked flags: {', '.join(item['linked_flags']) or 'none'}",
        "",
        item["recommendation"],
        "",
    ])
    if item["reviewers"]:
        lines.extend(["| Reviewer | Model | Findings | Flagged |", "| --- | --- | --- | --- |"])
        for row in item["reviewers"]:
            lines.append(f"| {_cell(row['reviewer'])} | {_cell(row['model'])} | {row['findings']} | "
                         f"{row['flagged_findings']} |")
        lines.append("")
    if item["kind"] == "analyzer":
        lines.extend(["Findings:", ""])
        for entry in item["evidence"][:EVIDENCE_RENDERED]:
            lines.append(f"- {_example(entry)}")
        if len(item["evidence"]) > EVIDENCE_RENDERED:
            lines.append(f"- and {len(item['evidence']) - EVIDENCE_RENDERED} more")
        lines.append("")
    if item["decision_history"]:
        lines.extend(["Decision history:", ""])
        for entry in item["decision_history"]:
            note = f" — {entry['note']}" if entry["note"] else ""
            resolved = f" (resolved {', '.join(entry['resolved_flags'])})" if entry["resolved_flags"] else ""
            lines.append(f"- {entry['decided_at']}: {entry['decision']}{note}{resolved}")
        lines.append("")
    return lines


def _example(entry: dict[str, Any]) -> str:
    """One finding an analyzer recommendation covers: where it is, then its headline when the record has one."""
    location = f"{entry['repository']}#{entry['pull_number']} v{entry['review_version']} {entry['finding_id']}"
    return f"{location} {entry['title'] or entry['path'] + ':' + str(entry['line'])}"


def render(report: dict[str, Any]) -> str:
    lines = [
        "# Code-review insights",
        "",
        f"Repository set: {report['repository_set']} ({', '.join(report['repositories'])})",
        f"Range: {report['start_date']} to {report['end_date']}",
        f"Records analyzed: {report['record_count']}",
        f"Findings analyzed: {report['finding_count']}",
        "",
        "## Recommendations",
        "",
    ]
    categories = [item for item in report["recommendations"] if item["kind"] == "category"]
    analyzers = [item for item in report["recommendations"] if item["kind"] == "analyzer"]
    if not categories:
        lines.extend(["No findings in the selected range.", ""])
    for item in categories:
        lines.extend(_render_recommendation(item, item["category"]))
    if analyzers:
        lines.extend([
            "## Analyzer opportunities",
            "",
            "Findings reviewers said a diagnostic analyzer could catch, cheapest first: rules in analyzers the "
            "repositories already have, then rules in analyzers they do not use, then patterns that would need a "
            "custom rule.",
            "",
        ])
    for item in analyzers:
        lines.extend(_render_recommendation(item, f"{item['coverage']}: {item['rule']} ({item['tool']})"))
    lines.extend(["## Evidence", ""])
    for record in report["records"]:
        lines.append(
            f"- `{record['path']}` — payload `{record['payload_sha256']}`"
        )
    lines.append("")
    return "\n".join(lines)


def _cell(value: str) -> str:
    """A table cell: a reviewer ID from an older record's source is free text and may hold a pipe."""
    return value.replace("\\", "\\\\").replace("|", "\\|")


def _validate_reviewers(value: Any, identifier: str) -> None:
    if not isinstance(value, list) or any(
        not isinstance(row, dict)
        or set(row) != REVIEWER_FIELDS
        or not all(isinstance(row[field], str) and row[field] for field in ("reviewer", "model"))
        or not all(type(row[field]) is int and row[field] >= 0 for field in ("findings", "flagged_findings"))
        for row in value
    ):
        raise InsightError(f"{identifier}.reviewers must be a list of reviewer counts")


def _validate_history(value: Any, identifier: str) -> None:
    if not isinstance(value, list):
        raise InsightError(f"{identifier}.decision_history must be a list")
    for entry in value:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"decision", "decided_at", "note", "resolved_flags"}
            or entry["decision"] not in DECISIONS
            or not isinstance(entry["decided_at"], str)
            or (entry["note"] is not None and not isinstance(entry["note"], str))
            or not isinstance(entry["resolved_flags"], list)
            or any(not isinstance(flag, str) for flag in entry["resolved_flags"])
        ):
            raise InsightError(f"{identifier}.decision_history has an invalid entry")


def _validate_subject(item: dict[str, Any], path: Path) -> None:
    if item.get("kind") not in KINDS:
        raise InsightError(f"{item['id']} has an invalid kind")
    if item["kind"] == "category":
        if not isinstance(item.get("category"), str):
            raise InsightError(f"{path} has an invalid recommendation")
        return
    analyzer = {field: item.get(field) for field in ("coverage", "tool", "rule")}
    repositories = item.get("repositories")
    if not valid_analyzer(analyzer) or not isinstance(repositories, list) or not repositories or any(
        not isinstance(repository, str) for repository in repositories
    ):
        raise InsightError(f"{item['id']} has an invalid analyzer subject")
    evidence = item.get("evidence")
    if not isinstance(evidence, list) or any(
        not isinstance(entry, dict) or set(entry) != EVIDENCE_FIELDS for entry in evidence
    ):
        raise InsightError(f"{item['id']}.evidence must be a list of findings")


def load_report(path: Path) -> dict[str, Any]:
    """A report in the current schema; a version 1 report gains an empty history, a version 1 or 2 report's links
    are dropped because they may name a finding from another review, a report before version 4 gains an empty
    reviewers breakdown, and every recommendation in a report before version 5 is a category one."""
    try:
        report = read_json(path)
    except PersistenceError as exc:
        raise InsightError(f"Cannot read insights report {path}: {exc}") from exc
    if not isinstance(report, dict) or report.get("schema_version") not in READABLE_SCHEMA_VERSIONS:
        raise InsightError(f"{path} is not a supported insights report")
    recommendations = report.get("recommendations")
    if not isinstance(recommendations, list) or not isinstance(report.get("records"), list):
        raise InsightError(f"{path} is not a supported insights report")
    upgrade = report["schema_version"] == 1
    for item in recommendations:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise InsightError(f"{path} has an invalid recommendation")
        if report["schema_version"] < 5:
            item.setdefault("kind", "category")
        _validate_subject(item, path)
        if item.get("decision") not in DECISIONS:
            raise InsightError(f"{item['id']} has an invalid decision")
        if upgrade:
            item.setdefault("decision_history", [])
            item.setdefault("linked_flags", [])
        _validate_history(item.get("decision_history"), item["id"])
        if not isinstance(item.get("linked_flags"), list) or any(
            not isinstance(flag, str) for flag in item["linked_flags"]
        ):
            raise InsightError(f"{item['id']}.linked_flags must be a list of flag IDs")
        if report["schema_version"] < 3:
            item["linked_flags"] = []
        if report["schema_version"] < 4:
            item.setdefault("reviewers", [])
        _validate_reviewers(item.get("reviewers"), item["id"])
    report["schema_version"] = SCHEMA_VERSION
    return report


def write_report(json_path: Path, report: dict[str, Any]) -> Path:
    markdown_path = json_path.with_suffix(".md")
    atomic_write_json(json_path, report)
    atomic_write_text(markdown_path, render(report))
    return markdown_path


def create_report(
    *,
    archive_root: Path,
    summary_root: Path,
    repository_set: str,
    repositories: list[str],
    start: date,
    end: date,
    flags: list[dict[str, Any]] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Write insights for the range. Regenerating keeps each subject's decision and decision history."""
    if not SET_NAME.fullmatch(repository_set):
        raise InsightError("Repository-set name is invalid")
    records = collect_records(archive_root, repositories, start, end)
    json_path = summary_root / repository_set / f"{start.isoformat()}--{end.isoformat()}" / "insights.json"
    previous = (
        {subject_key(item): item for item in load_report(json_path)["recommendations"]}
        if json_path.exists() else {}
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "repository_set": repository_set,
        "repositories": sorted(validate_repository_identity(value) for value in repositories),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        **analyze(records, flags=flags, previous=previous),
        "records": [
            {
                "path": str(path),
                "repository": record["repository"],
                "pull_number": record["pull_request"]["number"],
                "review_version": record["review"]["version"],
                "payload_sha256": record["artifacts"]["payload_sha256"],
            }
            for path, record in records
        ],
    }
    return json_path, write_report(json_path, report), report


def report_from_config(
    *,
    start: date,
    end: date,
    repositories: list[str] | None = None,
    repository_set: str | None = None,
    config_path: Path | None = None,
    services: Services | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    services = services or Services()
    config = load_config(config_path)
    selected = resolve_repositories(
        config, explicit=repositories, repository_set=repository_set, operation="review-insights"
    )
    # An explicit repository list is summarized under a stable digest of the list, so it never shares a set's
    # directory and decision history.
    name = (
        "repositories-" + hashlib.sha256(",".join(sorted(selected)).encode("utf-8")).hexdigest()[:12]
        if repositories is not None
        else selected_repository_set(config, repository_set=repository_set, operation="review-insights")
    )
    return create_report(
        archive_root=Path(config["archive_root"]),
        summary_root=Path(config["summary_root"]),
        repository_set=name,
        repositories=selected,
        start=start,
        end=end,
        flags=load_store(services.flags_path())["flags"],
    )


def decide(
    report_path: Path,
    recommendation_id: str,
    category: str | None,
    flags: list[str],
    decision: str,
    *,
    analyzer: tuple[str, str, str] | None = None,
    note: str | None = None,
    services: Services | None = None,
) -> tuple[list[str], list[str]]:
    """Append a decision to one recommendation's history. Returns the flags resolved and those already resolved.

    The subject (a category, or an analyzer's coverage, tool, and rule) and linked flags the user was shown must still
    match, so a decision never lands on a different recommendation or resolves a flag linked after the user saw it (a
    report regenerated in between keeps the ID but can gain flags). Only an accepted recommendation resolves flags.
    """
    services = services or Services()
    if decision not in DECISIONS:
        raise InsightError(f"Decision must be one of: {', '.join(DECISIONS)}")
    if (category is None) == (analyzer is None):
        raise InsightError("Name exactly one subject: a category or an analyzer")
    if note is not None and not note.strip():
        raise InsightError("A decision note must not be blank")
    report = load_report(report_path)
    matching = [item for item in report["recommendations"] if item["id"] == recommendation_id]
    if len(matching) != 1:
        raise InsightError(f"Unknown recommendation: {recommendation_id}")
    item = matching[0]
    if category is not None:
        shown_kind, shown, wanted = "category", category, ("category", category)
    else:
        coverage, tool, rule = analyzer  # type: ignore[misc]
        shown_kind, shown = "analyzer", f"{coverage} {tool} {rule}"
        wanted = subject_key({"kind": "analyzer", "coverage": coverage, "tool": tool, "rule": rule})
    if subject_key(item) != wanted:
        raise InsightError(
            f"{recommendation_id} is now {item['kind']} {describe(item)!r}, not {shown_kind} {shown!r}; run report "
            "again and confirm the decision with the user"
        )
    if sorted(item["linked_flags"]) != sorted(set(flags)):
        shown_flags = ",".join(sorted(set(flags))) or "none"
        current = ",".join(sorted(item["linked_flags"])) or "none"
        raise InsightError(
            f"{recommendation_id} now links flags {current}, not {shown_flags}; run report again and confirm the "
            "decision with the user"
        )
    resolved: list[str] = []
    skipped: list[str] = []
    if decision == "accepted" and item["linked_flags"]:
        flags_path = services.flags_path()
        status = {flag["id"]: flag["status"] for flag in load_store(flags_path)["flags"]}
        missing = [flag for flag in item["linked_flags"] if flag not in status]
        if missing:
            raise InsightError(f"Linked flags are not in the flag store {flags_path}: {', '.join(missing)}")
        resolution = f"Accepted review-insights recommendation {item['id']} ({describe(item)}) in {report_path}"
        if note:
            resolution += f": {note}"
        for flag_id in item["linked_flags"]:
            if status[flag_id] != "open":
                skipped.append(flag_id)
                continue
            resolve_flag(flags_path, flag_id, resolution)
            resolved.append(flag_id)
    item["decision"] = decision
    item["decision_history"].append({
        "decision": decision,
        "decided_at": services.now().isoformat(),
        "note": note,
        "resolved_flags": resolved,
    })
    write_report(report_path, report)
    return resolved, skipped


def _print_report(report: dict[str, Any]) -> None:
    for item in report["recommendations"]:
        flags = ",".join(item["linked_flags"]) or "none"
        if item["kind"] == "category":
            print(f"RECOMMENDATION {item['id']} {item['category']} findings={item['finding_count']} "
                  f"decision={item['decision']} flags={flags}")
        else:
            print(f"ANALYZER {item['id']} coverage={item['coverage']} tool={item['tool']} rule={item['rule']} "
                  f"findings={item['finding_count']} repositories={','.join(item['repositories'])} "
                  f"decision={item['decision']} flags={flags}")
        for row in item["reviewers"]:
            print(f"REVIEWER {item['id']} {row['reviewer']} model={row['model']} "
                  f"findings={row['findings']} flagged={row['flagged_findings']}")
        for entry in item.get("evidence", [])[:EXAMPLES_PRINTED]:
            print(f"EXAMPLE {item['id']} {_example(entry)}")


def main(arguments: list[str] | None = None, services: Services | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="defaults to CODE_REVIEW_CONFIG or the standard config path")
    commands = parser.add_subparsers(dest="command", required=True)
    report_parser = commands.add_parser("report")
    report_parser.add_argument("--start", required=True, help="inclusive ISO date")
    report_parser.add_argument("--end", required=True, help="inclusive ISO date")
    scope = report_parser.add_mutually_exclusive_group()
    scope.add_argument("--repository", action="append", dest="repositories")
    scope.add_argument("--repository-set")
    decide_parser = commands.add_parser("decide")
    decide_parser.add_argument("--report", required=True, type=Path, help="the insights.json a report printed")
    decide_parser.add_argument("recommendation")
    subject = decide_parser.add_mutually_exclusive_group(required=True)
    subject.add_argument("--category", help="the category a RECOMMENDATION line named")
    subject.add_argument("--analyzer", nargs=3, metavar=("COVERAGE", "TOOL", "RULE"),
                         help="the coverage, tool, and rule an ANALYZER line named")
    decide_parser.add_argument("--flags", required=True,
                               help="the flags= value the line printed: comma-separated IDs or none")
    decide_parser.add_argument("decision", choices=DECISIONS)
    decide_parser.add_argument("--note")
    args = parser.parse_args(arguments)
    try:
        if args.command == "report":
            json_path, markdown_path, report = report_from_config(
                start=parse_date(args.start), end=parse_date(args.end), repositories=args.repositories,
                repository_set=args.repository_set, config_path=args.config, services=services,
            )
            print(f"REPORT {json_path}")
            print(f"MARKDOWN {markdown_path}")
            _print_report(report)
            return 0
        shown_flags = [] if args.flags == "none" else [flag for flag in args.flags.split(",") if flag]
        resolved, skipped = decide(args.report, args.recommendation, args.category, shown_flags, args.decision,
                                   analyzer=tuple(args.analyzer) if args.analyzer else None, note=args.note,
                                   services=services)
        for flag_id in resolved:
            print(f"FLAG_RESOLVED {flag_id}")
        for flag_id in skipped:
            print(f"FLAG_ALREADY_RESOLVED {flag_id}")
        print(f"DECIDED {args.recommendation} {args.decision}")
        return 0
    except EXPECTED_ERRORS as exc:
        print(f"FAILED {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # Output quotes finding categories and configured paths; a Windows pipe's legacy code page cannot encode them.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())

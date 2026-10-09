"""Create reproducible insights from validated structured review records, and record decisions on them.

    scope       name the configured repository the working directory is in, and the default repository set
    report      analyze the configured archive for a date range and repository scope; write insights.json and .md,
                and the synthesis input, context, and prompt
    synthesize  check or record an analyst agent's synthesis result in its report
    decide      record one recommendation's decision in a report; an accepted one resolves its linked flags

Every command prints machine-readable lines and exits 0 on success. Expected failures print
`FAILED <reason>` as the last line and exit 1; only a usage error, such as a malformed date, exits 2.

There are two kinds of recommendation. A category recommendation covers every finding in one finding category. An
analyzer recommendation covers the findings reviewers said one diagnostic analyzer rule could catch: a rule in an
analyzer the repository already has but does not enforce (available), a rule in an established analyzer it does not
use (known), or a pattern no rule covers yet (custom-candidate). Analyzer recommendations are ranked in that order,
because enforcing a rule the repository already has is the cheapest way to stop a recurring finding. A finding with
analyzer coverage belongs to its category recommendation and to its analyzer recommendation.

A finding is counted once across the reviews that carry it, by its ledger entry: a finding a review linked as a
repeat counts with the finding it repeats, from the earliest review in range that carries either.

A flag is linked to a recommendation when the flag is open, names a repository, pull request, review version,
and finding, and that finding, or the finding it repeats or one that repeats it, is among the recommendation's
findings. Finding IDs restart in every review, so a flag is never matched against another review's finding. Links
are computed when the report is written, so `decide` resolves exactly the flags it listed.

Each recommendation also counts its findings by the reviewer that raised them and the model that reviewer ran on,
read from the review record: a finding raised by several reviewers counts for each, and a reviewer whose record
names no model counts under `unknown`. Beside the flags, each count says how many of those findings a later review
judged addressed and how many still present, read from the pull request's finding ledger whatever the range, so
acceptance shows without anyone flagging.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "code-review-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import review_synthesis as synthesis_stage
from console import use_utf8_output
from git_client import GitClient, GitError
from review_archive import list_versions, pull_directory, record_files, record_paths
from review_config import (
    ConfigurationError,
    load_config,
    resolve_repositories,
    selected_repository_set,
    validate_repository_identity,
)
from review_flags import FlagError, default_flags_path, load_store, resolve_flag
from review_io import PersistenceError, atomic_write_json, atomic_write_text, read_json
from review_records import ANALYZER_COVERAGES, RecordError, ledger_history, valid_analyzer, validate_record_pair

# Version 2 adds each recommendation's decision_history and linked_flags. Version 3 links a flag only to the
# finding in the review version it names; earlier reports linked it to whatever finding had its ID in the latest
# review, so their links are dropped when read, and a decision on one resolves no flag until it is regenerated.
# Version 4 adds each recommendation's reviewers breakdown; an earlier report has none until it is regenerated.
# Version 5 adds each recommendation's kind and the analyzer recommendations; every earlier one is a category one.
# Version 6 adds each reviewer row's addressed and still_present counts; an earlier report's rows have null for both
# until it is regenerated.
# Version 7 adds the report's synthesis and its synthesized recommendations; an earlier report has no synthesis until
# it is regenerated.
SCHEMA_VERSION = 7
READABLE_SCHEMA_VERSIONS = {1, 2, 3, 4, 5, 6, 7}
UNKNOWN_MODEL = "unknown"
REVIEWER_FIELDS = {"reviewer", "model", "findings", "flagged_findings", "addressed", "still_present"}
OUTCOMES = ("addressed", "still_present")
EVIDENCE_FIELDS = {"repository", "pull_number", "review_version", "finding_id", "path", "line", "title"}
DECISIONS = ("accepted", "rejected", "deferred")
KINDS = ("category", "analyzer", "synthesized")
SET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
REC_ID = re.compile(r"REC-(\d{3,})")
EXAMPLES_PRINTED = 3
EVIDENCE_RENDERED = 10

ReviewKey = tuple[str, int, int]
FindingKey = tuple[str, int, int, str]
Pair = tuple[dict[str, Any], dict[str, Any], FindingKey]  # (record, finding, its ledger entry)


class InsightError(ValueError):
    pass


EXPECTED_ERRORS = (InsightError, ConfigurationError, FlagError, PersistenceError, OSError)
SCRIPT = Path(__file__).resolve()


@dataclass
class Ledgers:
    """Each finding's ledger entry, keyed like a finding by the version and ID where the entry first appeared, and
    each entry's outcome (see read_ledgers). A finding with no entry is its own."""

    entries: dict[FindingKey, FindingKey]
    outcomes: dict[FindingKey, str]

    def entry(self, key: FindingKey) -> FindingKey:
        return self.entries.get(key, key)


@dataclass
class Services:
    """External effects, replaceable in tests."""

    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    flags_path: Callable[[], Path] = default_flags_path
    cwd: Callable[[], Path] = Path.cwd
    git_common_dir: Callable[[Path], Path | None] = lambda directory: _git_common_dir(directory)


def _git_common_dir(directory: Path) -> Path | None:
    """The Git directory a checkout or worktree shares with its main checkout, or None outside a repository."""
    try:
        completed = GitClient().run(["rev-parse", "--path-format=absolute", "--git-common-dir"], directory=directory)
    except GitError:
        return None
    value = completed.stdout.strip()
    return Path(value) if completed.returncode == 0 and value else None


def _within(path: Path, root: Path) -> bool:
    child, parent = os.path.normcase(str(path.resolve())), os.path.normcase(str(root.resolve()))
    return child == parent or child.startswith(parent.rstrip("\\/") + os.sep)


def current_repository(config: dict[str, Any], services: Services) -> str | None:
    """The configured repository whose `checkout_path` holds the working directory, or whose main checkout a worktree
    there belongs to; the innermost when checkouts nest."""
    directory = services.cwd()
    candidates = [directory]
    common = services.git_common_dir(directory)
    if common is not None and common.name.casefold() == ".git":
        candidates.append(common.parent)
    matches = [
        (len(str(Path(entry["checkout_path"]).resolve())), identity)
        for identity, entry in config["repositories"].items()
        if entry.get("checkout_path") and any(_within(item, Path(entry["checkout_path"])) for item in candidates)
    ]
    return max(matches)[1] if matches else None


def working_scope(config_path: Path | None, services: Services) -> list[str]:
    """`scope`: the repository the working directory suggests, and the set a report uses when none is named."""
    config = load_config(config_path)
    name = selected_repository_set(config, repository_set=None, operation="review-insights")
    members = resolve_repositories(config, explicit=None, repository_set=None, operation="review-insights")
    current = current_repository(config, services)
    default = f"DEFAULT_SET {name} {','.join(members)}"
    if current is None:
        return ["NO_CURRENT_REPOSITORY", default]
    # A set of just this repository keeps its reports, and their decisions, where that set's reports already are;
    # an explicit repository list is summarized apart from every set.
    alone = sorted(
        (set_name != name, set_name)
        for set_name, repositories in config["repository_sets"].items()
        if [item.lower() for item in repositories] == [current.lower()]
    )
    return [f"CURRENT_REPOSITORY {current} set={alone[0][1] if alone else 'none'}", default]


def parse_date(value: str) -> date:
    """An argparse type: a malformed date is a usage error, rejected before any work starts."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date: {value!r}") from exc


class RecordPairs:
    """The review record pairs a report reads, each validated once by its JSON path however many stages read it."""

    def __init__(self) -> None:
        self.validated: dict[Path, dict[str, Any]] = {}

    def read(self, json_path: Path) -> dict[str, Any]:
        if json_path not in self.validated:
            self.validated[json_path] = validate_record_pair(json_path, json_path.with_suffix(".md"))
        return self.validated[json_path]


def collect_records(
    archive_root: Path, repositories: list[str], start: date, end: date, pairs: RecordPairs
) -> list[tuple[Path, dict[str, Any]]]:
    """The records reviewed in the range. Every pair of the repositories is validated, in range or not, so `pairs`
    holds them all for the stages after this one."""
    if start > end:
        raise InsightError("Start date must not be after end date")
    normalized = [validate_repository_identity(value) for value in repositories]
    if not normalized or len(set(normalized)) != len(normalized):
        raise InsightError("Repositories must be a non-empty unique list")
    records: list[tuple[Path, dict[str, Any]]] = []
    for repository in normalized:
        for json_path in record_files(archive_root, repository):
            try:
                record = pairs.read(json_path)
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


def flags_by_entry(flags: list[dict[str, Any]], ledgers: Ledgers) -> dict[FindingKey, list[str]]:
    """Open flag IDs by the ledger entry of the finding each names, in the review version it names."""
    linked: dict[FindingKey, list[str]] = {}
    for flag in flags:
        named = (flag["repository"], flag["pull_number"], flag["review_version"], flag["finding_id"])
        if flag["status"] != "open" or None in named:
            continue
        key = (flag["repository"].lower(), flag["pull_number"], flag["review_version"], flag["finding_id"])
        linked.setdefault(ledgers.entry(key), []).append(flag["id"])
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


def read_ledgers(archive_root: Path, records: list[tuple[Path, dict[str, Any]]], pairs: RecordPairs) -> Ledgers:
    """The ledger entry of every finding of each analyzed pull request, and each entry's outcome, read from every
    review of it, in range or not. An entry is read from the ledger that ends its chain: the last review before the
    next initial review, which starts a new ledger. Its outcome is the latest disposition a review made after the
    last review that raised or repeated it; an entry no later review judged has none."""
    ledgers = Ledgers({}, {})
    pulls = sorted({(record["repository"].lower(), record["pull_request"]["number"]) for _, record in records})
    for repository, number in pulls:
        try:
            directory = pull_directory(archive_root, repository, number)
            history = [pairs.read(record_paths(directory, version)[0]) for version in list_versions(directory)]
        except (KeyError, ValueError, OSError, RecordError) as exc:
            raise InsightError(f"Invalid review history for {repository}#{number}: {exc}") from exc
        by_version = ledger_history(history)
        versions = sorted(by_version)
        # A record pair beside the version chain, such as one converted from another tool, has no ledger.
        if not versions:
            continue
        initial = {record["review"]["version"] for record in history if record["review"]["mode"] == "initial"}
        ends = [
            version
            for version, after in zip(versions, [*versions[1:], None], strict=True)
            if after is None or after in initial
        ]
        for end in ends:
            for entry in by_version[end]:
                key = (repository, number, entry["version"], entry["id"])
                raised = [{"version": entry["version"], "id": entry["id"]}, *entry["repeats"]]
                for occurrence in raised:
                    ledgers.entries[(repository, number, occurrence["version"], occurrence["id"])] = key
                last_raised = max(occurrence["version"] for occurrence in raised)
                later = [item for item in entry["dispositions"] if item["version"] > last_raised]
                if later:
                    ledgers.outcomes[key] = later[-1]["disposition"]
    return ledgers


def reviewer_breakdown(
    pairs: list[Pair], flagged: set[FindingKey], outcomes: dict[FindingKey, str]
) -> list[dict[str, Any]]:
    """How many of a recommendation's findings each reviewer raised on each model, how many of those an open flag
    names, and how many a later review judged addressed or still present. A finding raised by several reviewers
    counts for each, so the counts can sum past the findings."""
    counts: Counter[tuple[str, str]] = Counter()
    flagged_counts: Counter[tuple[str, str]] = Counter()
    outcome_counts: dict[str, Counter[tuple[str, str]]] = {outcome: Counter() for outcome in OUTCOMES}
    for record, finding, key in pairs:
        outcome = outcomes.get(key)
        for pair in raised_by(record, finding):
            counts[pair] += 1
            flagged_counts[pair] += int(key in flagged)
            if outcome in outcome_counts:
                outcome_counts[outcome][pair] += 1
    return [
        {
            "reviewer": reviewer,
            "model": model,
            "findings": count,
            "flagged_findings": flagged_counts[(reviewer, model)],
            **{outcome: outcome_counts[outcome][(reviewer, model)] for outcome in OUTCOMES},
        }
        for (reviewer, model), count in sorted(
            counts.items(), key=lambda item: (-item[1], item[0][0].casefold(), item[0][1].casefold())
        )
    ]


def subject_key(item: dict[str, Any]) -> tuple[str, ...]:
    """What a recommendation is about, so a regenerated report keeps its ID, decision, and history. Analyzer names
    are matched without regard to case, as reviewers are."""
    if item.get("kind", "category") == "category":
        return ("category", item["category"])
    if item["kind"] == "synthesized":
        return ("synthesized", item["title"].casefold())
    return ("analyzer", item["coverage"], item["tool"].casefold(), item["rule"].casefold())


def describe(item: dict[str, Any]) -> str:
    if item["kind"] == "category":
        return item["category"]
    if item["kind"] == "synthesized":
        return item["title"]
    return f"{item['coverage']} {item['tool']} {item['rule']}"


def _repositories_text(repositories: list[str]) -> str:
    return repositories[0] if len(repositories) == 1 else ", ".join(repositories[:-1]) + " and " + repositories[-1]


def _analyzer_recommendation(coverage: str, tool: str, rule: str, repositories: list[str]) -> str:
    where = _repositories_text(repositories)
    if coverage == "available":
        return (
            f"{tool}, which {where} already has, provides {rule}, but the rule is not enforced. Enable it or raise "
            "its severity in the analyzer's configuration so the build reports it instead of a reviewer."
        )
    if coverage == "known":
        return (
            f"{tool} provides {rule}, and {where} does not use {tool}. Consider adopting it for this rule, after "
            "checking its license, cost, and telemetry."
        )
    return (
        f"No existing analyzer rule catches the {rule} pattern found in {where}. Consider writing a custom {tool} "
        "rule for it."
    )


def _evidence(record: dict[str, Any], finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "repository": record["repository"].lower(),
        "pull_number": record["pull_request"]["number"],
        "review_version": record["review"]["version"],
        "finding_id": finding["id"],
        "path": finding["path"],
        "line": finding["line"],
        "title": finding.get("title"),
    }


def next_rec_number(items: Iterable[dict[str, Any]]) -> int:
    """The first `REC-` number after every one these recommendations use, so no ID is ever given twice."""
    used = [int(match.group(1)) for item in items if (match := REC_ID.fullmatch(str(item.get("id", ""))))]
    return max(used, default=0) + 1


def counted_pairs(records: list[tuple[Path, dict[str, Any]]], ledgers: Ledgers) -> list[Pair]:
    """Each analyzed finding once with its ledger entry: a finding and its repeats count once, from the earliest
    review in range that carries one."""
    counted: set[FindingKey] = set()
    pairs: list[Pair] = []
    for _, record in sorted(records, key=lambda item: _review_key(item[1])):
        for finding in record["findings"]:
            entry = ledgers.entry(_finding_key(record, finding))
            if entry not in counted:
                counted.add(entry)
                pairs.append((record, finding, entry))
    return pairs


def analyze(
    records: list[tuple[Path, dict[str, Any]]],
    *,
    flags: list[dict[str, Any]] | None = None,
    previous: dict[tuple[str, ...], dict[str, Any]] | None = None,
    ledgers: Ledgers | None = None,
) -> dict[str, Any]:
    """Recommendations by category, then by analyzer rule; a subject already in `previous` keeps its decision and
    history. `ledgers` gives each finding's ledger entry, so a finding and its repeats count once, from the earliest
    review in range that carries one, and each entry's outcome (see read_ledgers)."""
    ledgers = ledgers or Ledgers({}, {})
    category_counts: Counter[str] = Counter()
    severity_counts: Counter[str] = Counter()
    groups: dict[tuple[str, ...], list[Pair]] = {}
    spellings: dict[tuple[str, ...], dict[str, str]] = {}
    for record, finding, entry in counted_pairs(records, ledgers):
        category_counts[finding["category"]] += 1
        severity_counts[finding["severity"]] += 1
        groups.setdefault(("category", finding["category"]), []).append((record, finding, entry))
        analyzer = finding.get("analyzer")
        if analyzer is not None and valid_analyzer(analyzer):
            # The first spelling in review order names the rule, so a report reads the same when regenerated.
            key = subject_key({"kind": "analyzer", **analyzer})
            groups.setdefault(key, []).append((record, finding, entry))
            spellings.setdefault(key, analyzer)
    linked = flags_by_entry(flags or [], ledgers)
    flagged = set(linked)
    earlier = previous or {}
    # A subject keeps the ID it had when the report was regenerated, so an ID the user was shown never comes to
    # name another subject; a new subject gets a number no earlier recommendation used.
    next_number = next_rec_number(earlier.values())
    categories = sorted(
        (key for key in groups if key[0] == "category"), key=lambda key: (-len(groups[key]), key[1].casefold())
    )
    analyzers = sorted(
        (key for key in groups if key[0] == "analyzer"),
        key=lambda key: (ANALYZER_COVERAGES.index(key[1]), -len(groups[key]), key[2], key[3]),
    )
    recommendations = []
    for key in [*categories, *analyzers]:
        pairs = groups[key]
        prior = earlier.get(key, {})
        identifier = prior.get("id")
        if not isinstance(identifier, str) or not REC_ID.fullmatch(identifier):
            identifier = f"REC-{next_number:03d}"
            next_number += 1
        links = sorted({flag for _, _, entry in pairs for flag in linked.get(entry, [])})
        common = {
            "finding_count": len(pairs),
            "decision": prior.get("decision", "deferred"),
            "decision_history": list(prior.get("decision_history", [])),
            "linked_flags": links,
            "reviewers": reviewer_breakdown(pairs, flagged, ledgers.outcomes),
        }
        if key[0] == "category":
            category = key[1]
            recommendations.append(
                {
                    "id": identifier,
                    "kind": "category",
                    "category": category,
                    "recommendation": f"Review recurring {category} findings and decide "
                    "whether guidance or reviewer rules should change.",
                    **common,
                }
            )
            continue
        analyzer = spellings[key]
        repositories = sorted({record["repository"].lower() for record, _, _ in pairs})
        recommendations.append(
            {
                "id": identifier,
                "kind": "analyzer",
                "coverage": analyzer["coverage"],
                "tool": analyzer["tool"],
                "rule": analyzer["rule"],
                "repositories": repositories,
                "recommendation": _analyzer_recommendation(
                    analyzer["coverage"], analyzer["tool"], analyzer["rule"], repositories
                ),
                **common,
                "evidence": [_evidence(record, finding) for record, finding, _ in pairs],
            }
        )
    return {
        "record_count": len(records),
        "finding_count": sum(category_counts.values()),
        "severity_counts": dict(sorted(severity_counts.items())),
        "category_counts": dict(sorted(category_counts.items())),
        "recommendations": recommendations,
    }


def _render_recommendation(item: dict[str, Any], heading: str, report: dict[str, Any]) -> list[str]:
    lines = [f"### {item['id']} — {heading}", "", f"Finding count: {item['finding_count']}"]
    if item["kind"] == "analyzer":
        lines.append(f"Repositories: {', '.join(item['repositories'])}")
    lines.extend([f"Decision: {item['decision']}", f"Linked flags: {', '.join(item['linked_flags']) or 'none'}", ""])
    # A recorded synthesis's topics replace the generic sentence of a category recommendation.
    entry = synthesis_stage.category_entry(report, item["category"]) if item["kind"] == "category" else None
    lines.extend(synthesis_stage.category_markdown(report, entry) if entry else [item["recommendation"], ""])
    if item["reviewers"]:
        lines.extend(
            [
                "| Reviewer | Model | Findings | Flagged | Addressed | Still present |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for row in item["reviewers"]:
            lines.append(
                f"| {_cell(row['reviewer'])} | {_cell(row['model'])} | {row['findings']} | "
                f"{row['flagged_findings']} | {_outcome(row['addressed'])} | "
                f"{_outcome(row['still_present'])} |"
            )
        lines.append("")
    if item["kind"] == "analyzer":
        lines.extend(["Findings:", ""])
        for entry in item["evidence"][:EVIDENCE_RENDERED]:
            lines.append(f"- {_example(entry)}")
        if len(item["evidence"]) > EVIDENCE_RENDERED:
            lines.append(f"- and {len(item['evidence']) - EVIDENCE_RENDERED} more")
        lines.append("")
    return lines + _decision_history(item)


def _decision_history(item: dict[str, Any]) -> list[str]:
    if not item["decision_history"]:
        return []
    lines = ["Decision history:", ""]
    for entry in item["decision_history"]:
        note = f" — {entry['note']}" if entry["note"] else ""
        resolved = f" (resolved {', '.join(entry['resolved_flags'])})" if entry["resolved_flags"] else ""
        lines.append(f"- {entry['decided_at']}: {entry['decision']}{note}{resolved}")
    return [*lines, ""]


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
        *synthesis_stage.render(report, _decision_history),
        "## Recommendations",
        "",
    ]
    categories = [item for item in report["recommendations"] if item["kind"] == "category"]
    analyzers = [item for item in report["recommendations"] if item["kind"] == "analyzer"]
    if not categories:
        lines.extend(["No findings in the selected range.", ""])
    for item in categories:
        lines.extend(_render_recommendation(item, item["category"], report))
    if analyzers:
        lines.extend(
            [
                "## Analyzer opportunities",
                "",
                "Findings reviewers said a diagnostic analyzer could catch, cheapest first: rules in analyzers the "
                "repositories already have, then rules in analyzers they do not use, then patterns that would need a "
                "custom rule.",
                "",
            ]
        )
    for item in analyzers:
        lines.extend(_render_recommendation(item, f"{item['coverage']}: {item['rule']} ({item['tool']})", report))
    lines.extend(["## Evidence", ""])
    for record in report["records"]:
        lines.append(f"- `{record['path']}` — payload `{record['payload_sha256']}`")
    lines.append("")
    return "\n".join(lines)


def _cell(value: str) -> str:
    """A table cell: a reviewer ID from an older record's source is free text and may hold a pipe."""
    return value.replace("\\", "\\\\").replace("|", "\\|")


def _outcome(count: int | None) -> str:
    """An outcome count, or `-` in a report written before outcomes were counted."""
    return "-" if count is None else str(count)


def _validate_reviewers(value: Any, identifier: str) -> None:
    if not isinstance(value, list) or any(
        not isinstance(row, dict)
        or set(row) != REVIEWER_FIELDS
        or not all(isinstance(row[field], str) and row[field] for field in ("reviewer", "model"))
        or not all(type(row[field]) is int and row[field] >= 0 for field in ("findings", "flagged_findings"))
        or not all(row[field] is None or (type(row[field]) is int and row[field] >= 0) for field in OUTCOMES)
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
    if item["kind"] == "synthesized":
        if not synthesis_stage.valid_synthesized(item):
            raise InsightError(f"{item['id']} has an invalid synthesized recommendation")
        return
    analyzer = {field: item.get(field) for field in ("coverage", "tool", "rule")}
    repositories = item.get("repositories")
    if (
        not valid_analyzer(analyzer)
        or not isinstance(repositories, list)
        or not repositories
        or any(not isinstance(repository, str) for repository in repositories)
    ):
        raise InsightError(f"{item['id']} has an invalid analyzer subject")
    evidence = item.get("evidence")
    if not isinstance(evidence, list) or any(
        not isinstance(entry, dict) or set(entry) != EVIDENCE_FIELDS for entry in evidence
    ):
        raise InsightError(f"{item['id']}.evidence must be a list of findings")


def _upgrade_recommendation(item: Any, version: int, path: Path) -> None:
    """One recommendation of a report written in schema `version`, upgraded in place and checked."""
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        raise InsightError(f"{path} has an invalid recommendation")
    if version < 5:
        item.setdefault("kind", "category")
    _validate_subject(item, path)
    if item.get("decision") not in DECISIONS:
        raise InsightError(f"{item['id']} has an invalid decision")
    if version == 1:
        item.setdefault("decision_history", [])
        item.setdefault("linked_flags", [])
    _validate_history(item.get("decision_history"), item["id"])
    if not isinstance(item.get("linked_flags"), list) or any(
        not isinstance(flag, str) for flag in item["linked_flags"]
    ):
        raise InsightError(f"{item['id']}.linked_flags must be a list of flag IDs")
    if version < 3:
        item["linked_flags"] = []
    if version < 4:
        item.setdefault("reviewers", [])
    if version < 6 and isinstance(item.get("reviewers"), list):
        for row in item["reviewers"]:
            if isinstance(row, dict):
                row.update({outcome: row.get(outcome) for outcome in OUTCOMES})
    _validate_reviewers(item.get("reviewers"), item["id"])


def load_report(path: Path) -> dict[str, Any]:
    """A report in the current schema; a version 1 report gains an empty history, a version 1 or 2 report's links
    are dropped because they may name a finding from another review, a report before version 4 gains an empty
    reviewers breakdown, every recommendation in a report before version 5 is a category one, and a report before
    version 7 has no synthesis."""
    try:
        report = read_json(path)
    except PersistenceError as exc:
        raise InsightError(f"Cannot read insights report {path}: {exc}") from exc
    if not isinstance(report, dict) or report.get("schema_version") not in READABLE_SCHEMA_VERSIONS:
        raise InsightError(f"{path} is not a supported insights report")
    recommendations = report.get("recommendations")
    if not isinstance(recommendations, list) or not isinstance(report.get("records"), list):
        raise InsightError(f"{path} is not a supported insights report")
    for item in recommendations:
        _upgrade_recommendation(item, report["schema_version"], path)
    if report["schema_version"] < 7:
        report["synthesis"] = None
    # A synthesis recorded before category commentary was required has none.
    if isinstance(report.get("synthesis"), dict):
        report["synthesis"].setdefault("categories", [])
        report["synthesis"].setdefault("custom_rule_patterns", [])
    if "synthesis" not in report or (
        report["synthesis"] is not None and not synthesis_stage.valid_synthesis(report["synthesis"])
    ):
        raise InsightError(f"{path} has an invalid synthesis")
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
    """Write insights for the range and the input of its synthesis. Regenerating keeps each subject's decision and
    decision history, and a recorded synthesis while its input is unchanged; a changed input supersedes it."""
    if not SET_NAME.fullmatch(repository_set):
        raise InsightError("Repository-set name is invalid")
    pairs = RecordPairs()
    records = collect_records(archive_root, repositories, start, end, pairs)
    set_root = summary_root / repository_set
    json_path = set_root / f"{start.isoformat()}--{end.isoformat()}" / "insights.json"
    earlier = load_report(json_path) if json_path.exists() else None
    previous = {subject_key(item): item for item in earlier["recommendations"]} if earlier else {}
    # A superseded recommendation keeps its ID, so no later subject is given it.
    prior_synthesis = earlier["synthesis"] if earlier else None
    for run in prior_synthesis["superseded"] if prior_synthesis else []:
        previous.update({("superseded", item["id"]): item for item in run["recommendations"]})
    ledgers = read_ledgers(archive_root, records, pairs)
    report = {
        "schema_version": SCHEMA_VERSION,
        "repository_set": repository_set,
        "repositories": sorted(validate_repository_identity(value) for value in repositories),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        **analyze(records, flags=flags, previous=previous, ledgers=ledgers),
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
    json_path.parent.mkdir(parents=True, exist_ok=True)
    analyzed = synthesis_stage.Analyzed(
        report=report,
        records=[record for _, record in records],
        pairs=counted_pairs(records, ledgers),
        outcomes=ledgers.outcomes,
        flagged=set(flags_by_entry(flags or [], ledgers)),
    )
    fresh = synthesis_stage.prepare(
        json_path.parent,
        analyzed,
        flags=flags or [],
        guidance=synthesis_stage.guidance_files(report["repositories"], pairs.validated),
        previous=synthesis_stage.previous_period(set_root, start, load_report),
        script=SCRIPT,
    )
    _carry_synthesis(report, fresh, earlier)
    return json_path, write_report(json_path, report), report


def _carry_synthesis(report: dict[str, Any], fresh: dict[str, Any], earlier: dict[str, Any] | None) -> None:
    """Keep a recorded synthesis and its recommendations while the input is unchanged; otherwise start the fresh one,
    keeping a recorded synthesis's recommendations, with their decisions, among its superseded runs."""
    prior = earlier.get("synthesis") if earlier else None
    report["synthesis"] = fresh
    if prior is None or earlier is None:
        return
    recorded = [item for item in earlier["recommendations"] if item["kind"] == "synthesized"]
    if prior["status"] == "complete" and prior["input_sha256"] == fresh["input_sha256"]:
        report["synthesis"] = prior
        report["recommendations"].extend(recorded)
        return
    fresh["superseded"] = list(prior["superseded"])
    if prior["status"] == "complete":
        fresh["superseded"].append(
            {"recorded_at": prior["recorded_at"], "input_sha256": prior["input_sha256"], "recommendations": recorded}
        )


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


def synthesize(
    report_path: Path, result_path: Path, *, check: bool = False, services: Services | None = None
) -> list[dict[str, Any]]:
    """Record a synthesis result in its report, or with `check` only test that it could be recorded. Returns the
    recommendations it adds. A result is refused, and the report left unchanged, unless it is the result file the
    report named, its input is still what the report sealed, and it passes every check."""
    services = services or Services()
    report = load_report(report_path)
    synthesis = report["synthesis"]
    if synthesis is None or synthesis["status"] == "skipped":
        raise InsightError(f"{report_path} has no synthesis to record; run report again")
    if synthesis["status"] == "complete" and not check:
        raise InsightError(f"{report_path} already has a recorded synthesis; it is replaced when its input changes")
    if result_path.resolve() != Path(synthesis["result"]).resolve():
        raise InsightError(f"The synthesis result must be {synthesis['result']}")
    try:
        context = synthesis_stage.load_context(synthesis)
        result = synthesis_stage.check_result(read_json(result_path), context, synthesis["input_sha256"])
    except PersistenceError as exc:
        raise InsightError(f"Cannot read the synthesis result: {exc}") from exc
    if check:
        return []
    taken = [*report["recommendations"], *(item for run in synthesis["superseded"] for item in run["recommendations"])]
    added = [
        synthesis_stage.recorded_recommendation(item, f"REC-{number:03d}")
        for number, item in enumerate(result["recommendations"], start=next_rec_number(taken))
    ]
    report["recommendations"].extend(added)
    synthesis.update(
        {
            "status": "complete",
            "recorded_at": services.now().isoformat(),
            **{key: result[key] for key in ("themes", "mistakes", "persistent_patterns")},
            "reviewer_effectiveness": result["reviewer_effectiveness"],
            "comparison": result["comparison"],
            "categories": result["categories"],
            "custom_rule_patterns": result["custom_rule_patterns"],
        }
    )
    write_report(report_path, report)
    return added


def decide(
    report_path: Path,
    recommendation_id: str,
    category: str | None,
    flags: list[str],
    decision: str,
    *,
    analyzer: tuple[str, str, str] | None = None,
    synthesized: str | None = None,
    note: str | None = None,
    services: Services | None = None,
) -> tuple[list[str], list[str]]:
    """Append a decision to one recommendation's history. Returns the flags resolved and those already resolved.

    The subject (a category, an analyzer's coverage, tool, and rule, or a synthesized recommendation's title) and
    linked flags the user was shown must still match, so a decision never lands on a different recommendation or
    resolves a flag linked after the user saw it (a report regenerated in between keeps the ID but can gain flags).
    The subject matches as the report's line printed it, screened, or as recorded. Only an accepted recommendation
    resolves flags.
    """
    services = services or Services()
    if decision not in DECISIONS:
        raise InsightError(f"Decision must be one of: {', '.join(DECISIONS)}")
    if [category, analyzer, synthesized].count(None) != 2:
        raise InsightError("Name exactly one subject: a category, an analyzer, or a synthesized title")
    if note is not None and not note.strip():
        raise InsightError("A decision note must not be blank")
    report = load_report(report_path)
    matching = [item for item in report["recommendations"] if item["id"] == recommendation_id]
    if len(matching) != 1:
        raise InsightError(f"Unknown recommendation: {recommendation_id}")
    item = matching[0]
    shown_kind, shown, wanted = _shown_subject(category, analyzer, synthesized)
    if _as_printed(subject_key(item)) != _as_printed(wanted):
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
    resolution = f"Accepted review-insights recommendation {item['id']} ({describe(item)}) in {report_path}"
    resolved, skipped = (
        _resolve_flags(item["linked_flags"], resolution, note, services) if decision == "accepted" else ([], [])
    )
    _record_decision(item, decision, note, resolved, services)
    write_report(report_path, report)
    return resolved, skipped


def _resolve_flags(
    flag_ids: list[str], resolution: str, note: str | None, services: Services
) -> tuple[list[str], list[str]]:
    """Resolve the flags still open; returns those resolved and those already resolved."""
    if not flag_ids:
        return [], []
    flags_path = services.flags_path()
    status = {flag["id"]: flag["status"] for flag in load_store(flags_path)["flags"]}
    missing = [flag for flag in flag_ids if flag not in status]
    if missing:
        raise InsightError(f"Linked flags are not in the flag store {flags_path}: {', '.join(missing)}")
    text = f"{resolution}: {note}" if note else resolution
    resolved: list[str] = []
    skipped: list[str] = []
    for flag_id in flag_ids:
        if status[flag_id] != "open":
            skipped.append(flag_id)
            continue
        resolve_flag(flags_path, flag_id, text)
        resolved.append(flag_id)
    return resolved, skipped


def _record_decision(
    item: dict[str, Any], decision: str, note: str | None, resolved: list[str], services: Services
) -> None:
    item["decision"] = decision
    item["decision_history"].append(
        {"decision": decision, "decided_at": services.now().isoformat(), "note": note, "resolved_flags": resolved}
    )


def decide_custom(
    report_path: Path, flags: list[str], decision: str, *, note: str | None = None, services: Services | None = None
) -> tuple[list[str], list[str], int]:
    """Record one decision for every custom-candidate analyzer recommendation together, as the user was asked about
    them in one question. Returns the flags resolved, those already resolved, and how many were decided. The flags
    the user was shown, the union of theirs, must still match."""
    services = services or Services()
    if decision not in DECISIONS:
        raise InsightError(f"Decision must be one of: {', '.join(DECISIONS)}")
    if note is not None and not note.strip():
        raise InsightError("A decision note must not be blank")
    report = load_report(report_path)
    items = custom_candidates(report)
    if not items:
        raise InsightError(f"{report_path} has no custom-candidate recommendations")
    linked = sorted({flag for item in items for flag in item["linked_flags"]})
    if linked != sorted(set(flags)):
        raise InsightError(
            f"The custom-candidate recommendations now link flags {','.join(linked) or 'none'}, not "
            f"{','.join(sorted(set(flags))) or 'none'}; run report again and confirm the decision with the user"
        )
    resolution = f"Accepted the custom-candidate analyzer recommendations in {report_path}"
    resolved, skipped = _resolve_flags(linked, resolution, note, services) if decision == "accepted" else ([], [])
    for item in items:
        _record_decision(item, decision, note, [flag for flag in item["linked_flags"] if flag in resolved], services)
    write_report(report_path, report)
    return resolved, skipped, len(items)


def _as_printed(key: tuple[str, ...]) -> tuple[str, ...]:
    """A subject key as the report's lines print it, so a decision naming the subject as printed matches it. Two
    subjects that print alike never share an ID, which `decide` matches first."""
    return tuple(synthesis_stage.screened(part) for part in key)


def _shown_subject(
    category: str | None, analyzer: tuple[str, str, str] | None, synthesized: str | None
) -> tuple[str, str, tuple[str, ...]]:
    """The subject the user was shown: its kind, how to name it, and its subject key."""
    if category is not None:
        return "category", category, ("category", category)
    if analyzer is not None:
        coverage, tool, rule = analyzer
        key = subject_key({"kind": "analyzer", "coverage": coverage, "tool": tool, "rule": rule})
        return "analyzer", f"{coverage} {tool} {rule}", key
    if synthesized is not None:
        return "synthesized", synthesized, subject_key({"kind": "synthesized", "title": synthesized})
    raise InsightError("Name exactly one subject: a category, an analyzer, or a synthesized title")


def _print_synthesis(report: dict[str, Any]) -> None:
    synthesis = report["synthesis"]
    if synthesis is None or synthesis["status"] == "skipped":
        print("SYNTHESIS skipped")
        return
    if synthesis["status"] == "pending":
        print(f"SYNTHESIS_PROMPT {synthesis['prompt']}")
        print(f"SYNTHESIS_RESULT {synthesis['result']}")
        return
    print(f"SYNTHESIS recorded {synthesis['recorded_at']}")
    items = [item for item in report["recommendations"] if item["kind"] == "synthesized"]
    for item in sorted(items, key=lambda entry: synthesis_stage.PRIORITIES.index(entry["priority"])):
        _print_synthesized(item)


def _print_synthesized(item: dict[str, Any]) -> None:
    flags = ",".join(item["linked_flags"]) or "none"
    target = synthesis_stage.describe_target(item["target"])
    print(
        f"SYNTHESIZED {item['id']} type={item['type']} priority={item['priority']} decision={item['decision']} "
        f"flags={flags}"
    )
    print(f"TITLE {item['id']} {item['title']}")
    print(f"TARGET {item['id']} {target}")
    print(f"CHANGE {item['id']} {_flat(item['change'])}")
    print(f"RATIONALE {item['id']} {_flat(item['rationale'])}")
    for ref in item["evidence"][:EXAMPLES_PRINTED]:
        print(f"EXAMPLE {item['id']} {ref}")


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def custom_candidates(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in report["recommendations"] if item.get("coverage") == "custom-candidate"]


def _print_custom_candidates(report: dict[str, Any]) -> None:
    """One summary line for every custom-candidate rule, decided together, then the synthesis's patterns of them."""
    items = custom_candidates(report)
    if not items:
        return
    decisions = {item["decision"] for item in items}
    flags = ",".join(sorted({flag for item in items for flag in item["linked_flags"]})) or "none"
    print(
        f"CUSTOM_CANDIDATES rules={len(items)} findings={sum(item['finding_count'] for item in items)} "
        f"decision={decisions.pop() if len(decisions) == 1 else 'mixed'} flags={flags}"
    )
    for fact in synthesis_stage.custom_pattern_facts(report):
        print(fact)


def _print_report(report: dict[str, Any]) -> None:
    _print_synthesis(report)
    for item in report["recommendations"]:
        flags = ",".join(item["linked_flags"]) or "none"
        if item["kind"] == "synthesized" or item.get("coverage") == "custom-candidate":
            continue
        if item["kind"] == "category":
            print(
                f"RECOMMENDATION {item['id']} {synthesis_stage.screened(item['category'])} "
                f"findings={item['finding_count']} "
                f"decision={item['decision']} flags={flags}"
            )
            entry = synthesis_stage.category_entry(report, item["category"])
            for fact in synthesis_stage.category_facts(report, item["id"], entry) if entry else []:
                print(fact)
        else:
            print(
                f"ANALYZER {item['id']} coverage={item['coverage']} tool={synthesis_stage.screened(item['tool'])} "
                f"rule={synthesis_stage.screened(item['rule'])} "
                f"findings={item['finding_count']} repositories={','.join(item['repositories'])} "
                f"decision={item['decision']} flags={flags}"
            )
        for row in item["reviewers"]:
            print(
                f"REVIEWER {item['id']} {row['reviewer']} model={row['model']} "
                f"findings={row['findings']} flagged={row['flagged_findings']} "
                f"addressed={_outcome(row['addressed'])} still_present={_outcome(row['still_present'])}"
            )
        for entry in item.get("evidence", [])[:EXAMPLES_PRINTED]:
            print(f"EXAMPLE {item['id']} {_example(entry)}")
    _print_custom_candidates(report)


def _run_decide_custom(args: argparse.Namespace, services: Services | None) -> int:
    shown = [] if args.flags == "none" else [flag for flag in args.flags.split(",") if flag]
    resolved, skipped, count = decide_custom(args.report, shown, args.decision, note=args.note, services=services)
    for flag_id in resolved:
        print(f"FLAG_RESOLVED {flag_id}")
    for flag_id in skipped:
        print(f"FLAG_ALREADY_RESOLVED {flag_id}")
    print(f"DECIDED custom-candidates rules={count} {args.decision}")
    return 0


def _run_synthesize(args: argparse.Namespace, services: Services | None) -> int:
    """`synthesize`: a refused result prints each problem, so the agent can fix them all before checking again."""
    try:
        added = synthesize(args.report, args.result, check=args.check, services=services)
    except synthesis_stage.SynthesisError as exc:
        for problem in exc.problems[: synthesis_stage.PROBLEMS_SHOWN]:
            print(f"PROBLEM {_flat(problem)}")
        print(f"FAILED {exc}")
        return 1
    if args.check:
        print(f"VALID {args.result}")
        return 0
    report = load_report(args.report)
    print(f"MARKDOWN {args.report.with_suffix('.md')}")
    _print_synthesis(report)
    print(f"SYNTHESIZED_COUNT {len(added)}")
    return 0


def main(arguments: list[str] | None = None, services: Services | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="defaults to CODE_REVIEW_CONFIG or the standard config path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("scope")
    report_parser = commands.add_parser("report")
    report_parser.add_argument("--start", required=True, type=parse_date, help="inclusive ISO date")
    report_parser.add_argument("--end", required=True, type=parse_date, help="inclusive ISO date")
    scope = report_parser.add_mutually_exclusive_group()
    scope.add_argument("--repository", action="append", dest="repositories")
    scope.add_argument("--repository-set")
    decide_parser = commands.add_parser("decide")
    decide_parser.add_argument("--report", required=True, type=Path, help="the insights.json a report printed")
    decide_parser.add_argument("recommendation")
    subject = decide_parser.add_mutually_exclusive_group(required=True)
    subject.add_argument("--category", help="the category a RECOMMENDATION line named")
    subject.add_argument(
        "--analyzer",
        nargs=3,
        metavar=("COVERAGE", "TOOL", "RULE"),
        help="the coverage, tool, and rule an ANALYZER line named",
    )
    subject.add_argument("--synthesized", metavar="TITLE", help="the title a TITLE line named")
    custom_parser = commands.add_parser("decide-custom")
    custom_parser.add_argument("--report", required=True, type=Path, help="the insights.json a report printed")
    custom_parser.add_argument("--flags", required=True, help="the CUSTOM_CANDIDATES line's flags= value")
    custom_parser.add_argument("decision", choices=DECISIONS)
    custom_parser.add_argument("--note")
    synthesize_parser = commands.add_parser("synthesize")
    synthesize_parser.add_argument("--report", required=True, type=Path, help="the insights.json a report printed")
    synthesize_parser.add_argument("--result", required=True, type=Path, help="the SYNTHESIS_RESULT file")
    synthesize_parser.add_argument("--check", action="store_true", help="only check the result; record nothing")
    decide_parser.add_argument(
        "--flags", required=True, help="the flags= value the line printed: comma-separated IDs or none"
    )
    decide_parser.add_argument("decision", choices=DECISIONS)
    decide_parser.add_argument("--note")
    args = parser.parse_args(arguments)
    try:
        if args.command == "report":
            json_path, markdown_path, report = report_from_config(
                start=args.start,
                end=args.end,
                repositories=args.repositories,
                repository_set=args.repository_set,
                config_path=args.config,
                services=services,
            )
            print(f"REPORT {json_path}")
            print(f"MARKDOWN {markdown_path}")
            _print_report(report)
            return 0
        if args.command == "synthesize":
            return _run_synthesize(args, services)
        if args.command == "decide-custom":
            return _run_decide_custom(args, services)
        if args.command == "scope":
            print("\n".join(working_scope(args.config, services or Services())))
            return 0
        shown_flags = [] if args.flags == "none" else [flag for flag in args.flags.split(",") if flag]
        resolved, skipped = decide(
            args.report,
            args.recommendation,
            args.category,
            shown_flags,
            args.decision,
            analyzer=tuple(args.analyzer) if args.analyzer else None,
            synthesized=args.synthesized,
            note=args.note,
            services=services,
        )
        for flag_id in resolved:
            print(f"FLAG_RESOLVED {flag_id}")
        for flag_id in skipped:
            print(f"FLAG_ALREADY_RESOLVED {flag_id}")
        print(f"DECIDED {args.recommendation} {args.decision}")
        return 0
    except EXPECTED_ERRORS as exc:
        print(f"FAILED {exc}")
        return 1


if __name__ == "__main__":
    use_utf8_output()
    raise SystemExit(main())

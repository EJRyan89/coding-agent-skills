"""Review adapter validation, verdict calculation, and paired rendering."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from flat_text import flat_text
from review_config import validate_repository_identity
from review_io import atomic_write_json, atomic_write_text, read_json

RECORD_SCHEMA_VERSION = 1
# How a review's reviewer roles were worked, as its record's `review.dispatch` says: as native subagents (or by a Claude
# Code Workflow), by the bounded Copilot CLI host, or inline by the orchestrating session itself, one role at a time.
DISPATCH_MODES = ("subagents", "copilot-host", "inline")
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
SEVERITIES = {"MUST_FIX", "SHOULD_FIX", "SUGGESTION"}
DISPOSITIONS = {
    "addressed",
    "partially_addressed",
    "still_present",
    "superseded",
    "unable_to_verify",
}
FINDING_FIELDS = frozenset({"candidate_key", "severity", "category", "path", "line", "body", "evidence", "source"})
OPTIONAL_FINDING_FIELDS = frozenset({"title", "analyzer", "repeats"})
# The finding ledger: one entry per problem raised on a pull request since its latest initial review, identified by
# the version and finding ID where it first appeared. A repeat is linked to the finding it repeats, never counted
# twice, so a repeat needs a target at least as severe. A disposition of a prior finding judges its entry.
SEVERITY_RANK = {"SUGGESTION": 1, "SHOULD_FIX": 2, "MUST_FIX": 3}
FINDING_ID = re.compile(r"F[0-9]{3,}")
LEDGER_ID = re.compile(r"v([1-9][0-9]*):(F[0-9]{3,})")
LEDGER_FIELDS = frozenset({"version", "id", "severity", "category", "state", "judged_in", "dispositions", "repeats"})
OPEN_DISPOSITIONS = frozenset({"still_present", "partially_addressed"})
# A ledger entry's key: the version and finding ID where the finding first appeared.
LedgerKey = tuple[int, str]
LEDGER_STATES = {
    "still_present": "open",
    "partially_addressed": "open",
    "addressed": "closed",
    "superseded": "closed",
    "unable_to_verify": "unverified",
}
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
# How many head paths the snapshot left out, by the reason its manifest records; absent from older records.
OPTIONAL_SNAPSHOT_FIELDS = frozenset({"excluded"})
EXCLUSION_REASON = re.compile(r"[a-z][a-z-]{0,39}")
SNAPSHOT_PHASES = ("fetch", "materialize", "prompts")
TITLE_MAXIMUM_LENGTH = 120
TITLE_RULE = f"must be a single non-blank line of at most {TITLE_MAXIMUM_LENGTH} characters"
MODEL_MAXIMUM_LENGTH = 200
MODEL_RULE = f"must be a single non-blank line of at most {MODEL_MAXIMUM_LENGTH} characters"
# How a diagnostic analyzer could catch a finding instead of a reviewer: a rule in an analyzer the repository already
# has but does not enforce, a rule in an established analyzer it does not use, or a pattern no rule covers yet.
ANALYZER_COVERAGES = ("available", "known", "custom-candidate")
ANALYZER_FIELDS = frozenset({"coverage", "tool", "rule"})
ANALYZER_NAME = re.compile(r"[^\s`|<>]{1,100}")
CUSTOM_RULE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
CUSTOM_RULE_MAXIMUM_LENGTH = 60
ANALYZER_RULE = (
    f"must be an object with exactly coverage ({', '.join(ANALYZER_COVERAGES)}), tool, and rule; tool and rule "
    "are at most 100 characters with no whitespace, backticks, pipes, or angle brackets, and a custom-candidate "
    f"rule is a lowercase kebab-case pattern name of at most {CUSTOM_RULE_MAXIMUM_LENGTH} characters"
)
# A re-review asks for one of these scopes and runs as `full` or `incremental`. Its record keeps what was asked,
# what ran, why, and how much changed since the version it compared with (null when it could not compare).
RE_REVIEW_SCOPES = ("auto", "full", "incremental")
SCOPE_FIELDS = frozenset(
    {"requested", "used", "reason", "since_version", "files_changed", "files_total", "lines_changed", "lines_total"}
)
PATCH_FIELDS = frozenset({"sha256", "lines"})


def _one_line(value: Any, maximum: int) -> bool:
    return (
        isinstance(value, str) and value == value.strip() and 0 < len(value) <= maximum and len(value.splitlines()) == 1
    )


def valid_title(value: Any) -> bool:
    """A finding headline: one trimmed, non-blank line short enough for a report summary row."""
    return _one_line(value, TITLE_MAXIMUM_LENGTH)


def valid_model(value: Any) -> bool:
    """The model a reviewer reports it ran on: one trimmed, non-blank line."""
    return _one_line(value, MODEL_MAXIMUM_LENGTH)


def valid_analyzer(value: Any) -> bool:
    """A finding's analyzer coverage. Tool and rule are single tokens, so a report line can name them unquoted."""
    if not isinstance(value, dict) or set(value) != ANALYZER_FIELDS or value["coverage"] not in ANALYZER_COVERAGES:
        return False
    if not all(isinstance(value[field], str) and ANALYZER_NAME.fullmatch(value[field]) for field in ("tool", "rule")):
        return False
    return value["coverage"] != "custom-candidate" or (
        len(value["rule"]) <= CUSTOM_RULE_MAXIMUM_LENGTH and CUSTOM_RULE.fullmatch(value["rule"]) is not None
    )


class RecordError(ValueError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _one_of(value: Any, allowed: set[str] | frozenset[str]) -> bool:
    """A string in `allowed`. Checking the type first keeps an unhashable value from crashing set membership."""
    return isinstance(value, str) and value in allowed


def _validate_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise RecordError(f"{field} must be a lowercase Git or SHA-256 hash")
    return value


DISPOSITION_SUBJECTS = {"Prior": "prior finding", "Comment": "review comment"}


def validate_dispositions(dispositions: Any, key: str, expected: set[str], label: str) -> None:
    """Exactly one well-formed disposition for each expected ID (a prior finding or a review comment)."""
    subject = DISPOSITION_SUBJECTS[label]
    if not isinstance(dispositions, list):
        raise RecordError(f"{label.lower()}_dispositions must be an array")
    actual: set[str] = set()
    for disposition in dispositions:
        if not isinstance(disposition, dict) or set(disposition) != {key, "disposition", "rationale"}:
            raise RecordError(f"{label} disposition fields do not match the protocol")
        identifier = disposition[key]
        if not isinstance(identifier, str) or identifier in actual:
            raise RecordError(f"{label} disposition IDs must be unique strings")
        actual.add(identifier)
        if not _one_of(disposition["disposition"], DISPOSITIONS):
            raise RecordError(f"Invalid disposition for {subject} {identifier}")
        if not isinstance(disposition["rationale"], str) or not disposition["rationale"].strip():
            raise RecordError(f"{subject[0].upper()}{subject[1:]} {identifier} requires a rationale")
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
    value: Any,
    *,
    expected_repository: str,
    expected_number: int,
    expected_head_sha: str,
    prior_ids: Iterable[str] = (),
    comment_ids: Iterable[str] = (),
    require_comment_dispositions: bool = True,
    prior_severities: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate a reviewer result against its request.

    Every prior finding needs exactly one disposition. So does every open review comment the request listed,
    except that a repository reviewer that predates comment dispositions (require_comment_dispositions=False)
    may omit them all; when it gives any, they must cover every comment exactly once. A finding's `repeats`
    names another finding's candidate key or a prior finding ID (whose severity `prior_severities` gives).
    """
    prior_ids = list(prior_ids)
    _validate_result_envelope(value)
    _validate_result_request(value, expected_repository, expected_number, expected_head_sha)
    _validate_result_metadata(value)
    findings = _validate_result_findings(value.get("findings"))
    _validate_result_dispositions(value, prior_ids, comment_ids, require_comment_dispositions)
    _validate_result_repeats(findings, value.get("prior_dispositions", []), prior_severities or {}, set(prior_ids))
    usage = value.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise RecordError("usage must be an object or null")
    return value


def _validate_result_envelope(value: Any) -> None:
    """An object of known fields, in the protocol version this module reads."""
    if not isinstance(value, dict):
        raise RecordError("Adapter result must be an object")
    unknown = sorted(set(value) - ADAPTER_RESULT_FIELDS)
    if unknown:
        raise RecordError("Adapter result contains unknown fields: " + ", ".join(unknown))
    if value.get("protocol_version") != ADAPTER_PROTOCOL_VERSION:
        raise RecordError("Unsupported adapter protocol version")


def _validate_result_request(
    value: dict[str, Any], expected_repository: str, expected_number: int, expected_head_sha: str
) -> None:
    """The result answers the request: the same repository, pull request, and head."""
    if validate_repository_identity(value.get("repository")) != expected_repository.lower():
        raise RecordError("Adapter result repository does not match request")
    if value.get("pull_number") != expected_number:
        raise RecordError("Adapter result pull number does not match request")
    if value.get("head_sha") != expected_head_sha:
        raise RecordError("Adapter result head SHA does not match request")


def _validate_result_metadata(value: dict[str, Any]) -> None:
    if not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise RecordError("Adapter result summary is required")
    if not isinstance(value.get("reviewer"), str) or not value["reviewer"].strip():
        raise RecordError("Adapter result reviewer is required")
    if not _one_of(value.get("status"), {"complete", "partial", "failed"}):
        raise RecordError("Adapter result status is invalid")


def _validate_result_findings(findings: Any) -> list[dict[str, Any]]:
    """The findings, each checked in order; the first fault found is the one raised."""
    if not isinstance(findings, list):
        raise RecordError("Adapter findings must be an array")
    seen_keys: set[str] = set()
    for finding in findings:
        _validate_result_finding(finding, seen_keys)
    return findings


def _validate_result_finding(finding: Any, seen_keys: set[str]) -> None:
    """One finding, whose candidate key must not be in `seen_keys`; the key is added to it."""
    if not isinstance(finding, dict):
        raise RecordError("Every adapter finding must be an object")
    if not FINDING_FIELDS <= set(finding) <= FINDING_FIELDS | OPTIONAL_FINDING_FIELDS:
        raise RecordError("Adapter finding fields do not match the protocol")
    key = finding["candidate_key"]
    if not isinstance(key, str) or not key or key in seen_keys:
        raise RecordError("Adapter candidate keys must be unique non-empty strings")
    seen_keys.add(key)
    if "title" in finding and not valid_title(finding["title"]):
        raise RecordError(f"Finding {key}.title {TITLE_RULE}")
    if "analyzer" in finding and not valid_analyzer(finding["analyzer"]):
        raise RecordError(f"Finding {key}.analyzer {ANALYZER_RULE}")
    if not _one_of(finding["severity"], SEVERITIES):
        raise RecordError(f"Invalid finding severity for {key}")
    for field in ("category", "path", "body", "evidence", "source"):
        if not isinstance(finding[field], str) or not finding[field].strip():
            raise RecordError(f"Finding {key}.{field} must be non-empty")
    path = finding["path"]
    if path.startswith(("/", "\\")) or ".." in Path(path).parts or "\\" in path:
        raise RecordError(f"Finding {key}.path must be a safe repository-relative path")
    line = finding["line"]
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        raise RecordError(f"Finding {key}.line must be a positive integer")


def _validate_result_dispositions(
    value: dict[str, Any], prior_ids: list[str], comment_ids: Iterable[str], require_comment_dispositions: bool
) -> None:
    """One disposition per prior finding, then the comment dispositions when given or required."""
    validate_dispositions(value.get("prior_dispositions", []), "finding_id", set(prior_ids), "Prior")
    expected_comments = set(comment_ids)
    if "comment_dispositions" in value or (expected_comments and require_comment_dispositions):
        validate_dispositions(value.get("comment_dispositions", []), "comment_id", expected_comments, "Comment")


def _validate_result_repeats(
    findings: list[dict[str, Any]],
    dispositions: list[dict[str, Any]],
    prior_severities: dict[str, str],
    prior_ids: set[str],
) -> None:
    """Each `repeats` names one finding by its candidate key or prior finding ID, as validate_repeat requires."""
    keys = {finding["candidate_key"]: finding for finding in findings}
    judged = {disposition["finding_id"]: disposition["disposition"] for disposition in dispositions}
    for finding in findings:
        if "repeats" not in finding:
            continue
        key, target = finding["candidate_key"], finding["repeats"]
        if not isinstance(target, str) or not target:
            raise RecordError(f"Finding {key}.repeats must be a candidate key or prior finding ID")
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


def assign_finding_ids(findings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        (copy.deepcopy(item) for item in findings),
        key=lambda item: (item["path"].casefold(), item["line"], item["candidate_key"]),
    )
    result: list[dict[str, Any]] = []
    for index, finding in enumerate(ordered, start=1):
        finding["id"] = f"F{index:03d}"
        result.append(finding)
    return result


def ledger_id(version: int, identifier: str) -> str:
    """A ledger entry's ID, `v<version>:<finding ID>`: the prior-finding ID a re-review disposes."""
    return f"v{version}:{identifier}"


def _entry_key(entry: dict[str, Any]) -> tuple[int, int]:
    return entry["version"], int(entry["id"][1:])


def entry_state(entry: dict[str, Any]) -> tuple[str, int]:
    """An entry's state and the version that last judged it. Raising or repeating the finding judges it open;
    otherwise its latest disposition decides."""
    raised = [entry["version"], *(repeat["version"] for repeat in entry["repeats"])]
    latest = max([*raised, *(item["version"] for item in entry["dispositions"])])
    if latest in raised:
        return "open", latest
    return LEDGER_STATES[entry["dispositions"][-1]["disposition"]], latest


def extend_ledger(
    prior_ledger: Iterable[dict[str, Any]],
    *,
    version: int,
    findings: Iterable[dict[str, Any]],
    prior_dispositions: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The ledger after a review: each disposition judges its entry, each finding without `repeats` opens an
    entry, and each repeat joins the entry of the finding it repeats."""
    ledger = copy.deepcopy(list(prior_ledger))
    entries = {ledger_id(entry["version"], entry["id"]): entry for entry in ledger}
    for disposition in prior_dispositions:
        entry = entries.get(disposition["finding_id"])
        if entry is None:
            raise RecordError(f"Prior disposition {disposition['finding_id']} names no ledger entry")
        entry["dispositions"].append({"version": version, "disposition": disposition["disposition"]})
    findings = list(findings)
    for finding in findings:
        if "repeats" not in finding:
            entry = {
                "version": version,
                "id": finding["id"],
                "severity": finding["severity"],
                "category": finding["category"],
                "state": "open",
                "judged_in": version,
                "dispositions": [],
                "repeats": [],
            }
            ledger.append(entry)
            entries[ledger_id(version, finding["id"])] = entry
    for finding in findings:
        if "repeats" in finding:
            entry = entries.get(ledger_id(finding["repeats"]["version"], finding["repeats"]["id"]))
            if entry is None:
                raise RecordError(f"Finding {finding['id']} repeats a finding with no ledger entry")
            entry["repeats"].append({"version": version, "id": finding["id"]})
    for entry in ledger:
        entry["state"], entry["judged_in"] = entry_state(entry)
    return sorted(ledger, key=_entry_key)


def _legacy_ledger(base: list[dict[str, Any]], record: dict[str, Any], compared: int) -> list[dict[str, Any]]:
    """The ledger of a re-review recorded before ledgers. Its dispositions name bare finding IDs of the version it
    compared with; an ID that names no entry there is skipped. Its findings carry no links."""
    holders: dict[tuple[int, str], str] = {}
    for entry in base:
        for occurrence in ({"version": entry["version"], "id": entry["id"]}, *entry["repeats"]):
            holders[(occurrence["version"], occurrence["id"])] = ledger_id(entry["version"], entry["id"])
    dispositions: dict[str, dict[str, Any]] = {}
    for disposition in record["prior_dispositions"]:
        holder = holders.get((compared, disposition["finding_id"]))
        if holder is not None:
            dispositions.setdefault(holder, {"finding_id": holder, "disposition": disposition["disposition"]})
    findings = [{key: value for key, value in finding.items() if key != "repeats"} for finding in record["findings"]]
    return extend_ledger(
        base, version=record["review"]["version"], findings=findings, prior_dispositions=dispositions.values()
    )


def ledger_history(records: Iterable[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """Each version's ledger for one pull request's validated records: the ledger a record stores, or for a record
    written before ledgers, one computed from its findings and dispositions. An initial review starts afresh."""
    history: dict[int, list[dict[str, Any]]] = {}
    previous: int | None = None
    for record in sorted(records, key=lambda item: item["review"]["version"]):
        review = record["review"]
        if "ledger" in record:
            ledger = copy.deepcopy(record["ledger"])
        elif review["mode"] == "initial" or previous is None:
            ledger = _legacy_ledger([], record, 0)
        else:
            ledger = _legacy_ledger(
                history[previous], record, (review.get("scope") or {}).get("since_version", previous)
            )
        history[review["version"]] = ledger
        previous = review["version"]
    return history


def carried_findings(records: Iterable[dict[str, Any]], flags: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """The prior findings a re-review disposes: every open or unverified entry of the latest ledger, where it was
    last reported. A finding a review only judged stays here until a review closes it. An entry with flags in the
    flag store (`flags`) carries each one's ID, category, and rationale, so the reviewer can weigh it."""
    records = list(records)
    if not records:
        return []
    history = ledger_history(records)
    by_version = {record["review"]["version"]: record for record in records}
    latest = by_version[max(history)]
    flagged = flagged_entries(history[max(history)], flags, latest["repository"], latest["pull_request"]["number"])
    carried = []
    for entry in history[max(history)]:
        if entry["state"] == "closed":
            continue
        where = entry["repeats"][-1] if entry["repeats"] else entry
        finding = next(
            (
                item
                for item in (by_version.get(where["version"]) or {}).get("findings", [])
                if item["id"] == where["id"]
            ),
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


def ledger_summary(ledger: Iterable[dict[str, Any]], version: int) -> dict[str, Any]:
    """Open entries by severity, entries addressed, the earliest version an open entry dates from, and the version
    whose ledger this is. For a record written before ledgers, pass the ledger `ledger_history` computes: such a
    re-review's counts cover only the findings it raised, not a prior one it judged still present."""
    opened = {severity: 0 for severity in sorted(SEVERITIES)}
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
    ledger: Iterable[dict[str, Any]], flags: Iterable[dict[str, Any]], repository: str, number: int
) -> dict[str, list[dict[str, Any]]]:
    """The flags naming each ledger entry's findings, keyed by ledger ID: a flag names the finding where the entry
    first appeared or one of its repeats, by review version and finding ID. A resolved flag still counts, because
    resolving one records that the improvement was handled, not that the finding stood."""
    holders: dict[tuple[int, str], str] = {}
    for entry in ledger:
        for occurrence in ({"version": entry["version"], "id": entry["id"]}, *entry["repeats"]):
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
    ledger: Iterable[dict[str, Any]], policy: dict[str, Any], unavailable_sources: Iterable[str] = ()
) -> str:
    """CHANGES_REQUESTED when the open ledger entries require it; else INCOMPLETE when changed source could not be
    reviewed in full; else APPROVED. A blocking finding is never hidden by a coverage gap. A finding carried from an
    earlier review counts until a review closes it, and a linked repeat counts once, as its entry."""
    severities = [entry["severity"] for entry in ledger if entry["state"] == "open"]
    request_for = set(policy.get("request_changes_for", ["MUST_FIX"]))
    if any(severity in request_for for severity in severities):
        return "CHANGES_REQUESTED"
    threshold = policy.get("should_fix_threshold", 3)
    if severities.count("SHOULD_FIX") >= threshold:
        return "CHANGES_REQUESTED"
    if list(unavailable_sources):
        return "INCOMPLETE"
    return "APPROVED"


def build_record(
    request: dict[str, Any],
    adapter_result: dict[str, Any],
    *,
    version: int,
    policy: dict[str, Any],
    reviewed_at: str | None = None,
    prior_ledger: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A review record. A re-review passes the latest record's ledger as `prior_ledger`, which this review's
    dispositions judge and its findings extend; an initial review starts a fresh ledger."""
    findings = assign_finding_ids(adapter_result["findings"])
    identifiers = {finding["candidate_key"]: finding["id"] for finding in findings}
    for finding in findings:
        if "repeats" not in finding:
            continue
        target = finding["repeats"]
        prior = LEDGER_ID.fullmatch(target) if isinstance(target, str) else None
        if target in identifiers:
            finding["repeats"] = {"version": version, "id": identifiers[target]}
        elif prior is not None:
            finding["repeats"] = {"version": int(prior.group(1)), "id": prior.group(2)}
        else:
            raise RecordError(f"Finding {finding['candidate_key']} repeats an unknown finding: {target}")
    ledger = extend_ledger(
        prior_ledger or [],
        version=version,
        findings=findings,
        prior_dispositions=adapter_result.get("prior_dispositions", []),
    )
    counts = {severity: sum(item["severity"] == severity for item in findings) for severity in sorted(SEVERITIES)}
    unavailable = sorted(request.get("unavailable_sources", []))
    record = {
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
            "verdict": calculate_verdict(ledger, policy, unavailable),
            "counts": counts,
            "adapter": {
                "name": request["adapter"]["name"],
                "protocol_version": ADAPTER_PROTOCOL_VERSION,
                "scope": request["adapter"]["scope"],
                # Absent from runs prepared before the source was recorded.
                **({"source": request["adapter"]["source"]} if "source" in request["adapter"] else {}),
                "source_commit": request["adapter"].get("source_commit"),
                "source_hashes": request["adapter"].get("source_hashes", {}),
                "reviewer": adapter_result["reviewer"],
                "status": adapter_result["status"],
                "usage": adapter_result.get("usage"),
            },
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
    uncovered = sorted(request.get("uncovered_files", []))
    if unavailable or uncovered:
        record["review"]["coverage"] = {"unavailable_sources": unavailable}
    if uncovered:
        # Files no specialist covers, which the reviewer manifest left unreviewed. A deliberate opt-out, so they do
        # not make the review INCOMPLETE.
        record["review"]["coverage"]["uncovered_files"] = uncovered
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
        record["github_comments"] = [
            {key: comment[key] for key in COMMENT_FIELDS} for comment in request.get("github_comments", [])
        ]
        record["comment_dispositions"] = copy.deepcopy(adapter_result["comment_dispositions"])
    return record


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _seconds(value: Any) -> bool:
    """A non-negative, finite number of seconds, whole or not."""
    return isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value < float("inf")


def _size(count: int) -> str:
    """A byte count as `512 B`, `3.4 KiB`, or `204.0 MiB`."""
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f} KiB"
    return f"{count / 2**20:.1f} MiB"


def _files_read(reviewer: dict[str, Any]) -> str:
    """A reviewer's distinct snapshot files read and their size, `unknown` when no guard counted them, or `-` in a
    record written before reads were counted."""
    if "files_read" not in reviewer:
        return "-"
    if reviewer["files_read"] is None:
        return "unknown"
    return f"{reviewer['files_read']} ({_size(reviewer['bytes_read'])})"


def snapshot_seconds(seconds: dict[str, float]) -> dict[str, float]:
    """Each phase's seconds as a review records them: to a tenth of a second, and never below zero."""
    return {phase: round(max(seconds[phase], 0.0), 1) for phase in SNAPSHOT_PHASES}


def describe_snapshot(snapshot: dict[str, Any]) -> str:
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


def describe_reviewer_source(adapter: dict[str, Any]) -> str:
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


def _path_set(paths: Any) -> bool:
    """A list of distinct non-empty paths."""
    return (
        isinstance(paths, list)
        and all(isinstance(path, str) and path for path in paths)
        and len(set(paths)) == len(paths)
    )


def _validate_timestamp(value: Any) -> None:
    """An ISO 8601 timestamp string, as a review's reviewed_at must be."""
    if not isinstance(value, str):
        raise RecordError("Review timestamp is invalid")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise RecordError("Review timestamp is invalid") from exc


def _validate_reviewers(reviewers: Any) -> None:
    """The reviewers that ran: which files each covered, what it found, how often it was retried, and how
    long it took."""
    if not isinstance(reviewers, list) or not reviewers:
        raise RecordError("Review reviewers must be a non-empty array")
    seen: set[str] = set()
    for reviewer in reviewers:
        if (
            not isinstance(reviewer, dict)
            or not REVIEWER_FIELDS <= set(reviewer) <= REVIEWER_FIELDS | OPTIONAL_REVIEWER_FIELDS
        ):
            raise RecordError("Review reviewer fields are malformed")
        if not isinstance(reviewer["id"], str) or not reviewer["id"] or reviewer["id"] in seen:
            raise RecordError("Review reviewer IDs must be unique non-empty strings")
        seen.add(reviewer["id"])
        if not isinstance(reviewer["category"], str) or not reviewer["category"].strip():
            raise RecordError(f"Review reviewer {reviewer['id']} needs a category")
        if not all(_count(reviewer[field]) for field in ("files", "findings", "retries")):
            raise RecordError(f"Review reviewer {reviewer['id']} counts must be non-negative integers")
        if not isinstance(reviewer["dispositions_only"], bool):
            raise RecordError(f"Review reviewer {reviewer['id']}.dispositions_only must be a boolean")
        if "seconds" in reviewer and not _count(reviewer["seconds"]):
            raise RecordError(f"Review reviewer {reviewer['id']}.seconds must be a non-negative integer")
        if "model" in reviewer and not valid_model(reviewer["model"]):
            raise RecordError(f"Review reviewer {reviewer['id']}.model {MODEL_RULE}")
        _validate_read_counts(reviewer)


def _validate_read_counts(reviewer: dict[str, Any]) -> None:
    """A reviewer's read counts: both absent, both null (not counted), or both non-negative integers."""
    present = [field for field in READ_COUNT_FIELDS if field in reviewer]
    if not present:
        return
    values = [reviewer.get(field) for field in READ_COUNT_FIELDS]
    if len(present) != len(READ_COUNT_FIELDS) or not (
        all(value is None for value in values) or all(_count(value) for value in values)
    ):
        raise RecordError(
            f"Review reviewer {reviewer['id']} files_read and bytes_read must both be non-negative integers "
            "or both null"
        )


def _validate_snapshot(snapshot: Any) -> None:
    """The source snapshot's origin, file count, byte total, exclusions by reason, and the seconds of each phase of
    prepare."""
    if (
        not isinstance(snapshot, dict)
        or not SNAPSHOT_FIELDS <= set(snapshot) <= SNAPSHOT_FIELDS | OPTIONAL_SNAPSHOT_FIELDS
    ):
        raise RecordError("Review snapshot fields are malformed")
    excluded = snapshot.get("excluded", {})
    if not isinstance(excluded, dict) or not all(
        isinstance(reason, str) and EXCLUSION_REASON.fullmatch(reason) and _count(count) and count > 0
        for reason, count in excluded.items()
    ):
        raise RecordError("Review snapshot excluded must give a positive count for each exclusion reason")
    if not _one_of(snapshot["source"], set(SNAPSHOT_SOURCES)):
        raise RecordError("Review snapshot source is invalid")
    if not _count(snapshot["files"]) or not _count(snapshot["bytes"]):
        raise RecordError("Review snapshot files and bytes must be non-negative integers")
    seconds = snapshot["seconds"]
    if (
        not isinstance(seconds, dict)
        or set(seconds) != set(SNAPSHOT_PHASES)
        or not all(_seconds(value) for value in seconds.values())
    ):
        raise RecordError(f"Review snapshot seconds must give {', '.join(SNAPSHOT_PHASES)} as non-negative numbers")


def _safe_relative(path: Any) -> bool:
    return (
        isinstance(path, str)
        and bool(path)
        and not path.startswith(("/", "\\"))
        and "\\" not in path
        and ".." not in Path(path).parts
    )


def _validate_patches(patches: Any) -> None:
    """Each changed file's patch fingerprint, so the next re-review can tell which files changed since."""
    if not isinstance(patches, dict) or not patches:
        raise RecordError("Review patches must be a non-empty object")
    for path, patch in patches.items():
        if not _safe_relative(path):
            raise RecordError("Review patch path is unsafe")
        if (
            not isinstance(patch, dict)
            or set(patch) != PATCH_FIELDS
            or not isinstance(patch["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", patch["sha256"])
            or not _count(patch["lines"])
        ):
            raise RecordError(f"Review patch for {path} is malformed")


def _validate_scope(scope: Any, review: dict[str, Any]) -> None:
    if review["mode"] != "re-review":
        raise RecordError("Only a re-review has a scope")
    if not isinstance(scope, dict) or set(scope) != SCOPE_FIELDS:
        raise RecordError("Review scope fields are malformed")
    if scope["requested"] not in RE_REVIEW_SCOPES or scope["used"] not in ("full", "incremental"):
        raise RecordError("Review scope is invalid")
    if not isinstance(scope["reason"], str) or not scope["reason"].strip():
        raise RecordError("Review scope needs a reason")
    since = scope["since_version"]
    if not _count(since) or not 1 <= since < review["version"]:
        raise RecordError("Review scope.since_version must be an earlier version")
    if not _count(scope["files_total"]) or not _count(scope["lines_total"]):
        raise RecordError("Review scope totals must be non-negative integers")
    compared = scope["files_changed"] is not None
    if compared != (scope["lines_changed"] is not None) or (
        compared
        and not (
            _count(scope["files_changed"])
            and scope["files_changed"] <= scope["files_total"]
            and _count(scope["lines_changed"])
            and scope["lines_changed"] <= scope["lines_total"]
        )
    ):
        raise RecordError("Review scope changed counts must both be null or both be within their totals")
    if scope["used"] == "incremental" and not compared:
        raise RecordError("An incremental re-review must have compared with an earlier version")


def describe_scope(scope: dict[str, Any]) -> str:
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
    if not isinstance(comments, list) or not comments:
        raise RecordError("Review comment dispositions need the comments they answer")
    ids: set[str] = set()
    for comment in comments:
        if not isinstance(comment, dict) or set(comment) != set(COMMENT_FIELDS):
            raise RecordError("Review comment fields are malformed")
        if (
            not isinstance(comment["id"], str)
            or not re.fullmatch(r"C[1-9][0-9]*", comment["id"])
            or comment["id"] in ids
        ):
            raise RecordError("Review comment IDs must be unique C<n> values")
        ids.add(comment["id"])
        for field in ("author", "path", "body", "url"):
            if not isinstance(comment[field], str):
                raise RecordError(f"Review comment {comment['id']}.{field} must be a string")
        if comment["line"] is not None and (not _count(comment["line"]) or comment["line"] < 1):
            raise RecordError(f"Review comment {comment['id']}.line must be a positive integer or null")
        if not isinstance(comment["outdated"], bool):
            raise RecordError(f"Review comment {comment['id']}.outdated must be a boolean")
    validate_dispositions(dispositions, "comment_id", ids, "Comment")


def _reference(value: Any, latest: int) -> bool:
    """A `{version, id}` reference to a finding in this review or an earlier one."""
    return (
        isinstance(value, dict)
        and set(value) == {"version", "id"}
        and _count(value["version"])
        and 1 <= value["version"] <= latest
        and isinstance(value["id"], str)
        and FINDING_ID.fullmatch(value["id"]) is not None
    )


def _validate_ledger_entry(entry: Any, version: int) -> None:
    if (
        not isinstance(entry, dict)
        or set(entry) != LEDGER_FIELDS
        or not _count(entry["version"])
        or entry["version"] < 1
        or not isinstance(entry["id"], str)
        or not FINDING_ID.fullmatch(entry["id"])
    ):
        raise RecordError("Review ledger entry fields are malformed")
    label = ledger_id(entry["version"], entry["id"])
    if entry["version"] > version:
        raise RecordError(f"Review ledger entry {label} is after this review")
    if (
        not _one_of(entry["severity"], SEVERITIES)
        or not isinstance(entry["category"], str)
        or not entry["category"].strip()
    ):
        raise RecordError(f"Review ledger entry {label} severity or category is invalid")
    dispositions = entry["dispositions"]
    if not isinstance(dispositions, list) or any(
        not isinstance(item, dict)
        or set(item) != {"version", "disposition"}
        or not _count(item["version"])
        or not _one_of(item["disposition"], DISPOSITIONS)
        for item in dispositions
    ):
        raise RecordError(f"Review ledger entry {label} dispositions are malformed")
    versions = [item["version"] for item in dispositions]
    if versions != sorted(set(versions)) or any(not entry["version"] < item <= version for item in versions):
        raise RecordError(f"Review ledger entry {label} dispositions are malformed")
    repeats = entry["repeats"]
    if (
        not isinstance(repeats, list)
        or any(not _reference(item, version) or item["version"] < entry["version"] for item in repeats)
        or len({(item["version"], item["id"]) for item in repeats}) != len(repeats)
    ):
        raise RecordError(f"Review ledger entry {label} repeats are malformed")
    state, judged_in = entry_state(entry)
    if entry["state"] != state:
        raise RecordError(f"Review ledger entry {label} state must be {state}")
    if entry["judged_in"] != judged_in:
        raise RecordError(f"Review ledger entry {label} judged_in must be {judged_in}")


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


def _ledger_entries(ledger: Any, version: int, mode: str) -> dict[LedgerKey, dict[str, Any]]:
    """The ledger's entries by key: each well formed and unique, in order, and all of this version in an initial
    review."""
    if not isinstance(ledger, list):
        raise RecordError("Review ledger must be an array")
    entries: dict[LedgerKey, dict[str, Any]] = {}
    for entry in ledger:
        _validate_ledger_entry(entry, version)
        if (entry["version"], entry["id"]) in entries:
            raise RecordError("Review ledger entries must be unique")
        entries[(entry["version"], entry["id"])] = entry
    if [_entry_key(entry) for entry in ledger] != sorted(_entry_key(entry) for entry in ledger):
        raise RecordError("Review ledger entries must be ordered by version and finding ID")
    if mode == "initial" and any(entry["version"] != version for entry in ledger):
        raise RecordError("An initial review starts a fresh ledger")
    return entries


def _validate_opened_entries(
    entries: dict[LedgerKey, dict[str, Any]], findings: dict[str, dict[str, Any]], version: int
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
    ledger: list[dict[str, Any]], prior_dispositions: list[dict[str, Any]], version: int
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
    entries: dict[LedgerKey, dict[str, Any]],
    given: dict[str, str],
    version: int,
) -> LedgerKey:
    """A finding's `repeats` names a finding at least as severe, whose ledger entry lists the repeat. Returns that
    entry's key."""
    target = finding["repeats"]
    if not _reference(target, version):
        raise RecordError(f"Review finding {identifier}.repeats is malformed")
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


def _same_review_target_severity(identifier: str, target_id: str, findings: dict[str, dict[str, Any]]) -> str:
    """The severity of the finding of this review that `identifier` repeats: another finding, not itself a repeat."""
    if target_id == identifier:
        raise RecordError(f"Review finding {identifier} cannot repeat itself")
    other = findings.get(target_id)
    if other is None:
        raise RecordError(f"Review finding {identifier} repeats an unknown finding: {target_id}")
    if "repeats" in other:
        raise RecordError(f"Review finding {identifier} repeats a repeat: {target_id}")
    return other["severity"]


def _earlier_target_severity(
    identifier: str, key: LedgerKey, entries: dict[LedgerKey, dict[str, Any]], given: dict[str, str]
) -> str:
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


def _validate_listed_repeats(ledger: list[dict[str, Any]], linked: list[tuple[str, LedgerKey]], version: int) -> None:
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
    body = {"body_characters", "body_given"}
    if not pull_fields <= set(pull) or set(pull) - pull_fields not in ({"head_ref"} | body, body, {"head_ref"}, set()):
        raise RecordError("Review pull-request fields are malformed")
    if "head_ref" in pull and (not isinstance(pull["head_ref"], str) or not pull["head_ref"].strip()):
        raise RecordError("Review pull_request.head_ref is invalid")
    characters, given = pull.get("body_characters", 0), pull.get("body_given", 0)
    if not all(isinstance(count, int) and not isinstance(count, bool) and count >= 0 for count in (characters, given)):
        raise RecordError("Review pull_request.body_characters or body_given is invalid")
    if given > characters:
        raise RecordError("Review pull_request.body_given is more than body_characters")
    if not isinstance(pull.get("number"), int) or isinstance(pull["number"], bool) or pull["number"] < 1:
        raise RecordError("Review pull number is invalid")
    for field in ("url", "title", "base_ref"):
        if not isinstance(pull[field], str) or not pull[field].strip():
            raise RecordError(f"Review pull_request.{field} is invalid")
    _validate_sha(pull.get("base_sha"), "pull_request.base_sha")
    _validate_sha(pull.get("head_sha"), "pull_request.head_sha")


def _validate_coverage(review: dict[str, Any]) -> None:
    """The sources a review could not reach and the files no reviewer covered; an INCOMPLETE review names a source."""
    coverage = review.get("coverage", {"unavailable_sources": []})
    unavailable = coverage.get("unavailable_sources") if isinstance(coverage, dict) else None
    # Records written before uncovered files were recorded omit them.
    if (
        not isinstance(coverage, dict)
        or not {"unavailable_sources"} <= set(coverage) <= {"unavailable_sources", "uncovered_files"}
        or not _path_set(unavailable)
        or not _path_set(coverage.get("uncovered_files", []))
    ):
        raise RecordError("Review coverage is malformed")
    if review["verdict"] == "INCOMPLETE" and not unavailable:
        raise RecordError("An INCOMPLETE review must list its unavailable sources")


def _validate_review(review: dict[str, Any]) -> None:
    """The review's own metadata, up to its adapter."""
    review_fields = {"version", "mode", "reviewed_at", "summary", "verdict", "counts", "adapter"}
    # Records written before patches, scopes, dispatch modes, or snapshots were recorded omit them.
    optional = {"coverage", "reviewers", "patches", "scope", "dispatch", "snapshot"}
    if not review_fields <= set(review) <= review_fields | optional:
        raise RecordError("Review metadata fields are malformed")
    if "dispatch" in review and not _one_of(review["dispatch"], set(DISPATCH_MODES)):
        raise RecordError("Review dispatch is invalid")
    if "snapshot" in review:
        _validate_snapshot(review["snapshot"])
    if "reviewers" in review:
        _validate_reviewers(review["reviewers"])
    if "patches" in review:
        _validate_patches(review["patches"])
    if not _one_of(review.get("verdict"), {"APPROVED", "CHANGES_REQUESTED", "INCOMPLETE"}):
        raise RecordError("Review verdict is invalid")
    _validate_coverage(review)
    if not isinstance(review.get("version"), int) or isinstance(review["version"], bool) or review["version"] < 1:
        raise RecordError("Review version is invalid")
    if not _one_of(review.get("mode"), {"initial", "re-review"}):
        raise RecordError("Review mode is invalid")
    if "scope" in review:
        _validate_scope(review["scope"], review)
    _validate_timestamp(review.get("reviewed_at"))
    if not isinstance(review.get("summary"), str) or not review["summary"].strip():
        raise RecordError("Review summary is invalid")


def _validate_source_hashes(source_hashes: Any) -> None:
    """The adapter's source files by safe relative path, each with its SHA-256 hash."""
    if not isinstance(source_hashes, dict):
        raise RecordError("Review adapter source_hashes is invalid")
    for path, hash_value in source_hashes.items():
        if not _safe_relative(path):
            raise RecordError("Review adapter source hash path is unsafe")
        if not isinstance(hash_value, str) or not re.fullmatch(r"[0-9a-f]{64}", hash_value):
            raise RecordError(f"Review adapter source hash is invalid: {path}")


def _validate_adapter(adapter: Any) -> None:
    """Which adapter produced the review, at which source, and how it ended. Records written before the source was
    recorded have no `source`."""
    fields = {"name", "protocol_version", "scope", "source_commit", "source_hashes", "reviewer", "status", "usage"}
    if not isinstance(adapter, dict) or not fields <= set(adapter) <= fields | {"source"}:
        raise RecordError("Review adapter metadata is malformed")
    if adapter["protocol_version"] != ADAPTER_PROTOCOL_VERSION:
        raise RecordError("Review adapter protocol is unsupported")
    if not _one_of(adapter["scope"], {"generic", "repository"}):
        raise RecordError("Review adapter scope is invalid")
    # A repository reviewer was read at a commit; the generic reviewer at none.
    if "source" in adapter and (
        not _one_of(adapter["source"], set(ADAPTER_SOURCES))
        or ADAPTER_SOURCES[adapter["source"]] != adapter["scope"]
        or (adapter["source_commit"] is None) != (adapter["scope"] == "generic")
    ):
        raise RecordError(f"Review adapter source is invalid for a {adapter['scope']} reviewer")
    for field in ("name", "reviewer"):
        if not isinstance(adapter[field], str) or not adapter[field].strip():
            raise RecordError(f"Review adapter {field} is invalid")
    if not _one_of(adapter["status"], {"complete", "partial", "failed"}):
        raise RecordError("Review adapter status is invalid")
    if adapter["source_commit"] is not None:
        _validate_sha(adapter["source_commit"], "review.adapter.source_commit")
    _validate_source_hashes(adapter["source_hashes"])
    if adapter["usage"] is not None and not isinstance(adapter["usage"], dict):
        raise RecordError("Review adapter usage is invalid")


def _validate_finding(finding: Any) -> None:
    # Records written before findings carried titles or analyzer coverage remain valid.
    if not isinstance(finding, dict) or not (
        FINDING_FIELDS | {"id"} <= set(finding) <= FINDING_FIELDS | {"id"} | OPTIONAL_FINDING_FIELDS
    ):
        raise RecordError("Review finding fields are malformed")
    if not _one_of(finding["severity"], SEVERITIES):
        raise RecordError(f"Review finding {finding['id']} severity is invalid")
    if "title" in finding and not valid_title(finding["title"]):
        raise RecordError(f"Review finding {finding['id']}.title {TITLE_RULE}")
    if "analyzer" in finding and not valid_analyzer(finding["analyzer"]):
        raise RecordError(f"Review finding {finding['id']}.analyzer {ANALYZER_RULE}")
    for field in ("candidate_key", "category", "path", "body", "evidence", "source"):
        if not isinstance(finding[field], str) or not finding[field].strip():
            raise RecordError(f"Review finding {finding['id']}.{field} is invalid")
    if not _safe_relative(finding["path"]):
        raise RecordError(f"Review finding {finding['id']}.path is unsafe")
    if not isinstance(finding["line"], int) or isinstance(finding["line"], bool) or finding["line"] < 1:
        raise RecordError(f"Review finding {finding['id']}.line is invalid")


def _validate_findings(findings: Any) -> list[dict[str, Any]]:
    """The findings, numbered F001 on in order, each well formed."""
    if not isinstance(findings, list):
        raise RecordError("Review findings must be an array")
    expected_ids = [f"F{index:03d}" for index in range(1, len(findings) + 1)]
    if [item.get("id") for item in findings if isinstance(item, dict)] != expected_ids:
        raise RecordError("Review finding IDs are not stable and contiguous")
    for finding in findings:
        _validate_finding(finding)
    return findings


def _validate_counts(counts: Any, findings: list[dict[str, Any]]) -> None:
    expected_counts = {
        severity: sum(item["severity"] == severity for item in findings) for severity in sorted(SEVERITIES)
    }
    if counts != expected_counts:
        raise RecordError("Review finding counts do not match findings")


def _validate_prior_dispositions(dispositions: Any) -> None:
    """At most one well-formed disposition per prior finding; the ledger checks which findings they judge."""
    if not isinstance(dispositions, list):
        raise RecordError("Review prior dispositions must be an array")
    disposition_ids: set[str] = set()
    for disposition in dispositions:
        if not isinstance(disposition, dict) or set(disposition) != {
            "finding_id",
            "disposition",
            "rationale",
        }:
            raise RecordError("Review prior disposition fields are malformed")
        finding_id = disposition["finding_id"]
        if not isinstance(finding_id, str) or not finding_id or finding_id in disposition_ids:
            raise RecordError("Review prior disposition IDs must be unique")
        disposition_ids.add(finding_id)
        if not _one_of(disposition["disposition"], DISPOSITIONS):
            raise RecordError(f"Review prior disposition is invalid: {finding_id}")
        if not isinstance(disposition["rationale"], str) or not disposition["rationale"].strip():
            raise RecordError(f"Review prior disposition rationale is invalid: {finding_id}")


def _validate_artifacts(artifacts: Any) -> None:
    """The hashes of the record's payload and its Markdown report, when the pair has been written."""
    if artifacts is None:
        return
    if not isinstance(artifacts, dict) or set(artifacts) != {"payload_sha256", "markdown_sha256"}:
        raise RecordError("Review artifact hashes are malformed")
    _validate_sha(artifacts["payload_sha256"], "artifacts.payload_sha256")
    _validate_sha(artifacts["markdown_sha256"], "artifacts.markdown_sha256")


def validate_record(value: Any) -> dict[str, Any]:
    """A review record, checked part by part in a fixed order; the first fault found is the one raised."""
    if (
        not isinstance(value, dict)
        or set(value) - RECORD_FIELDS
        or value.get("schema_version") != RECORD_SCHEMA_VERSION
    ):
        raise RecordError("Unsupported or malformed review record")
    _validate_comments(value)
    validate_repository_identity(value.get("repository"))
    pull = value.get("pull_request")
    review = value.get("review")
    if not isinstance(pull, dict) or not isinstance(review, dict):
        raise RecordError("Review record metadata is malformed")
    _validate_pull_request(pull)
    _validate_review(review)
    _validate_adapter(review.get("adapter"))
    findings = _validate_findings(value.get("findings"))
    _validate_counts(review.get("counts"), findings)
    _validate_prior_dispositions(value.get("prior_dispositions"))
    _validate_ledger(value, review["version"], review["mode"])
    _validate_artifacts(value.get("artifacts"))
    return value


def payload_hash(record: dict[str, Any]) -> str:
    payload = {key: value for key, value in record.items() if key != "artifacts"}
    return sha256_text(canonical_json(payload))


SEVERITY_SECTIONS = (("MUST_FIX", "MUST FIX"), ("SHOULD_FIX", "SHOULD FIX"), ("SUGGESTION", "SUGGESTIONS"))
DISPOSITION_PHRASES = {
    "still_present": "Still present",
    "partially_addressed": "Partially addressed",
    "addressed": "Addressed",
    "superseded": "Superseded",
    "unable_to_verify": "Unable to verify",
}


def _code(text: str) -> str:
    """Inline code span that survives backticks and line breaks in the text."""
    text = " ".join(text.split())
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _cell(text: str) -> str:
    """Single-line Markdown table cell content."""
    return " ".join(text.split()).replace("|", "\\|")


# The characters Markdown, GitHub's math, or HTML reads as markup within a line; a backslash makes each literal.
MARKUP = re.compile(r"([\\`*_\[\]<>&|~$])")


def _text(text: str) -> str:
    """Single-line Markdown table cell content that renders as written: untrusted text, such as a pull request's title
    or a review comment, never becomes a link, an image, emphasis, or an HTML tag."""
    return MARKUP.sub(r"\\\1", " ".join(text.split()))


def _html(text: str) -> str:
    return " ".join(text.split()).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _quote(text: str) -> list[str]:
    return [f"> {line}".rstrip() for line in text.strip().splitlines()]


def _label(value: str) -> str:
    return value.replace("_", " ").upper()


def _analyzer_note(analyzer: dict[str, str]) -> str:
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


def _description_row(pull: dict[str, Any]) -> list[str]:
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


def _display_id(version: int, identifier: str) -> str:
    """A finding's ID as reports show it, `v1 F001`, since finding IDs restart in every review."""
    return f"v{version} {identifier}"


def _finding_block(
    finding: dict[str, Any], label: str, *, repeats: str | None = None, status: Iterable[str] = ()
) -> list[str]:
    """One finding's collapsible block, headed by its display ID. `repeats` names the finding it repeats, and
    `status` holds the lines saying what this review found about it."""
    headline = (
        _html(finding["title"])
        if "title" in finding
        else f"<code>{_html(PurePosixPath(finding['path']).name)}:{finding['line']}</code>"
    )
    # A specialist's evidence is the added line itself, so it joins the line number instead of
    # repeating the location on a line of its own; other evidence stays after the body.
    added_prefix = f"{finding['path']}:{finding['line']} adds: "
    if finding["evidence"].startswith(added_prefix):
        location = [
            f"> **Line {finding['line']}:** {_code(finding['evidence'][len(added_prefix) :])} "
            f"| **Source:** {_html(finding['source'])}"
        ]
        evidence = []
    else:
        location = [f"> **Line:** {finding['line']} | **Source:** {_html(finding['source'])}"]
        evidence = [">", f"> **Evidence:** {_code(finding['evidence'])}"]
    if "analyzer" in finding:
        evidence.extend([">", f"> **Analyzer:** {_analyzer_note(finding['analyzer'])}"])
    # Each status line but the last ends in a Markdown hard break, so the lines don't run together.
    status = [f"> {line}  " for line in status]
    if status:
        status[-1] = status[-1].rstrip()
    prefix = f"Repeats {repeats}: " if repeats else ""
    return [
        "<details open>",
        f"<summary>{label}. {prefix}[{_html(finding['category'])}] {headline}</summary>",
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
        self, record: dict[str, Any], prior_records: Iterable[dict[str, Any]], flags: Iterable[dict[str, Any]]
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

    def finding(self, version: int, identifier: str) -> dict[str, Any]:
        finding = self.findings.get((version, identifier))
        if finding is None:
            raise RecordError(f"Review v{version} with finding {identifier} is missing from the archive")
        return finding

    def raised(self, entry: dict[str, Any]) -> dict[str, Any]:
        return self.finding(entry["version"], entry["id"])

    def order(self, entry: dict[str, Any]) -> tuple[int, str, int, tuple[int, int]]:
        finding = self.raised(entry)
        return -SEVERITY_RANK[entry["severity"]], finding["path"].casefold(), finding["line"], _entry_key(entry)

    def judgment(self, entry: dict[str, Any]) -> str:
        """The latest disposition's phrase, version, and rationale, such as `Addressed in v2: Fixed.`"""
        latest = entry["dispositions"][-1]
        rationale = self.rationales.get((latest["version"], ledger_id(entry["version"], entry["id"])))
        text = f"{DISPOSITION_PHRASES[latest['disposition']]} in v{latest['version']}"
        return f"{text}: {flat_text(rationale)}" if rationale else f"{text}."

    def status(self, entry: dict[str, Any]) -> list[str]:
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

    def flag_lines(self, entry: dict[str, Any]) -> list[str]:
        return [
            f"**Flagged:** {flag['id']} ({flat_text(flag['category'])}): {flat_text(flag['body'])}"
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
    record: dict[str, Any],
    *,
    record_payload_hash: str,
    prior_records: Iterable[dict[str, Any]] = (),
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
            f"| **Branch** | {_cell(_code(pull['head_ref']))} → {_cell(_code(pull['base_ref']))} |"
            if pull.get("head_ref")
            else f"| **Base** | {_cell(_code(pull['base_ref']))} |"
        ),
        f"| **URL** | {_cell(pull['url'])} |",
        *_description_row(pull),
        f"| **Reviewed** | {_reviewed_at(review['reviewed_at'])} |",
        f"| **Verdict** | {_label(review['verdict'])}{', ' + ledger.counts() if re_review else ''} |",
        *([f"| **Scope** | {_cell(describe_scope(review['scope']))} |"] if "scope" in review else []),
        "",
    ]
    unavailable = (review.get("coverage") or {}).get("unavailable_sources", [])
    if unavailable:
        lines.extend(
            [
                "> **Not reviewed in full:** these changed files were too large or could not be represented safely, "
                "so reviewers saw only their diff: " + ", ".join(_code(path) for path in unavailable) + ".",
                "",
            ]
        )
    uncovered = (review.get("coverage") or {}).get("uncovered_files", [])
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
    lines.extend(["---", "", "## Summary", "", review["summary"].strip(), "", "## Findings", ""])
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
            excerpt = " ".join(comment["body"].split())
            excerpt = excerpt if len(excerpt) <= 120 else excerpt[:117].rstrip() + "..."
            outdated = " (outdated)" if comment["outdated"] else ""
            lines.append(
                f"| [{comment['id']}]({comment['url']}) | @{_cell(comment['author'])} on "
                f"{_cell(_code(location))}{outdated}: {_text(excerpt)} | {_label(disposition['disposition'])} "
                f"| {_cell(disposition['rationale'])} |"
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
                f"| {_cell(_code(reviewer['id']))} | {_cell(focus)} | {_cell(names.get(model, model))} "
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
            f"| **Adapter** | {_cell(_code(adapter['name']))} ({adapter['scope']}) |",
            *([f"| **Reviewer source** | {_cell(describe_reviewer_source(adapter))} |"] if "source" in adapter else []),
            f"| **Reviewer** | {_cell(adapter['reviewer'])} ({adapter['status']}) |",
            # The Reviewers table shows a configured name in place of each mapped model identifier, kept here.
            *(
                [
                    "| **Reviewer models** | "
                    + "; ".join(f"{_cell(names[model])}: {_cell(_code(model))}" for model in mapped)
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
            label = _display_id(entry["version"], entry["id"])
            block = _finding_block(
                ledger.raised(entry), label, status=ledger.status(entry) if re_review else ledger.flag_lines(entry)
            )
            for repeat in entry["repeats"]:
                if repeat["version"] == ledger.version:
                    block[-2:-2] = _finding_block(
                        ledger.finding(repeat["version"], repeat["id"]),
                        _display_id(repeat["version"], repeat["id"]),
                        repeats=label,
                    )
            lines.extend(block)
        lines.extend(["</details>", ""])
    closed = sorted((entry for entry in ledger.entries if entry["state"] == "closed"), key=ledger.order)
    if re_review and closed:
        lines.extend(["<details>", f"<summary><strong>Addressed since v{ledger.start}</strong></summary>", ""])
        for entry in closed:
            finding = ledger.raised(entry)
            title = f" {_html(finding['title'])}," if "title" in finding else ""
            location = _code(f"{finding['path']}:{finding['line']}")
            lines.append(
                f"- **{_display_id(entry['version'], entry['id'])}.** {_label(entry['severity'])} "
                f"[{_html(entry['category'])}]{title} {location}. {ledger.judgment(entry)}"
            )
        lines.extend(["", "</details>", ""])
    return lines


def _addressed(dispositions: list[dict[str, Any]]) -> str:
    addressed = sum(1 for disposition in dispositions if disposition["disposition"] == "addressed")
    return f"**{addressed}/{len(dispositions)} addressed**"


def write_record_pair(
    json_path: Path,
    markdown_path: Path,
    record: dict[str, Any],
    *,
    prior_records: Iterable[dict[str, Any]] = (),
    model_names: dict[str, str] | None = None,
    flags: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
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


def record_artifacts(record: dict[str, Any]) -> dict[str, str] | None:
    """A validated record's artifact hashes, or None when it has none: a null `artifacts` reads as absent."""
    artifacts: dict[str, str] | None = record.get("artifacts")
    return artifacts


def validate_record_pair(json_path: Path, markdown_path: Path) -> dict[str, Any]:
    record = validate_record(read_json(json_path))
    artifacts = record_artifacts(record)
    if artifacts is None:
        raise RecordError("Review record has no artifact hashes")
    markdown = markdown_path.read_text(encoding="utf-8")
    if payload_hash(record) != artifacts["payload_sha256"]:
        raise RecordError("Review JSON payload hash mismatch")
    if sha256_text(markdown) != artifacts["markdown_sha256"]:
        raise RecordError("Review Markdown hash mismatch")
    expected_marker = f"Record payload SHA-256** | `{artifacts['payload_sha256']}`"
    if expected_marker not in markdown:
        raise RecordError("Review Markdown does not reference the JSON payload hash")
    return record

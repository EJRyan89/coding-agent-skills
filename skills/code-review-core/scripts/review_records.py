"""Review adapter validation, verdict calculation, and paired rendering."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from review_config import validate_repository_identity
from review_io import atomic_write_json, atomic_write_text, read_json


RECORD_SCHEMA_VERSION = 1
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
LEDGER_STATES = {
    "still_present": "open",
    "partially_addressed": "open",
    "addressed": "closed",
    "superseded": "closed",
    "unable_to_verify": "unverified",
}
COMMENT_FIELDS = ("id", "author", "path", "line", "outdated", "body", "url")
REVIEWER_FIELDS = frozenset({"id", "category", "files", "findings", "retries", "dispositions_only"})
# Records written before reviewer timing or model reporting existed, or by a reviewer that does not report
# them (a repository entrypoint reviewer has no model field in its protocol), omit them.
OPTIONAL_REVIEWER_FIELDS = frozenset({"seconds", "model"})
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
SCOPE_FIELDS = frozenset({"requested", "used", "reason", "since_version", "files_changed", "files_total",
                          "lines_changed", "lines_total"})
PATCH_FIELDS = frozenset({"sha256", "lines"})


def _one_line(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and value == value.strip() and 0 < len(value) <= maximum \
        and len(value.splitlines()) == 1


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
    if not isinstance(value, dict):
        raise RecordError("Adapter result must be an object")
    allowed = {
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
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise RecordError("Adapter result contains unknown fields: " + ", ".join(unknown))
    if value.get("protocol_version") != ADAPTER_PROTOCOL_VERSION:
        raise RecordError("Unsupported adapter protocol version")
    if validate_repository_identity(value.get("repository")) != expected_repository.lower():
        raise RecordError("Adapter result repository does not match request")
    if value.get("pull_number") != expected_number:
        raise RecordError("Adapter result pull number does not match request")
    if value.get("head_sha") != expected_head_sha:
        raise RecordError("Adapter result head SHA does not match request")
    if not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise RecordError("Adapter result summary is required")
    if not isinstance(value.get("reviewer"), str) or not value["reviewer"].strip():
        raise RecordError("Adapter result reviewer is required")
    if not _one_of(value.get("status"), {"complete", "partial", "failed"}):
        raise RecordError("Adapter result status is invalid")
    findings = value.get("findings")
    if not isinstance(findings, list):
        raise RecordError("Adapter findings must be an array")
    seen_keys: set[str] = set()
    for finding in findings:
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

    validate_dispositions(value.get("prior_dispositions", []), "finding_id", set(prior_ids), "Prior")
    expected_comments = set(comment_ids)
    if "comment_dispositions" in value or (expected_comments and require_comment_dispositions):
        validate_dispositions(value.get("comment_dispositions", []), "comment_id", expected_comments, "Comment")
    _validate_result_repeats(findings, value.get("prior_dispositions", []), prior_severities or {}, set(prior_ids))
    usage = value.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise RecordError("usage must be an object or null")
    return value


def _validate_result_repeats(findings: list[dict[str, Any]], dispositions: list[dict[str, Any]],
                             prior_severities: dict[str, str], prior_ids: set[str]) -> None:
    """Each `repeats` names exactly one finding that is at least as severe and not itself a repeat. A prior finding
    it repeats must have been judged still present, at least in part."""
    keys = {finding["candidate_key"]: finding for finding in findings}
    judged = {disposition["finding_id"]: disposition["disposition"] for disposition in dispositions}
    for finding in findings:
        if "repeats" not in finding:
            continue
        key, target = finding["candidate_key"], finding["repeats"]
        if not isinstance(target, str) or not target:
            raise RecordError(f"Finding {key}.repeats must be a candidate key or prior finding ID")
        if target == key:
            raise RecordError(f"Finding {key} cannot repeat itself")
        if target in keys and target in prior_ids:
            raise RecordError(f"Finding {key}.repeats is ambiguous: {target} is a candidate key and a prior finding ID")
        if target in keys:
            if "repeats" in keys[target]:
                raise RecordError(f"Finding {key} repeats a repeat: link it to what {target} repeats instead")
            severity = keys[target]["severity"]
        elif target in prior_ids:
            if judged.get(target) not in OPEN_DISPOSITIONS:
                raise RecordError(f"Finding {key} repeats prior finding {target}, so that finding's disposition must "
                                  "be still_present or partially_addressed")
            severity = prior_severities.get(target)
            if severity not in SEVERITY_RANK:
                raise RecordError(f"Finding {key} repeats prior finding {target}, whose severity is unknown")
        else:
            raise RecordError(f"Finding {key} repeats an unknown finding: {target}")
        if SEVERITY_RANK[severity] < SEVERITY_RANK[finding["severity"]]:
            raise RecordError(f"Finding {key} repeats a less severe finding: {target}")


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
            entry = {"version": version, "id": finding["id"], "severity": finding["severity"],
                     "category": finding["category"], "state": "open", "judged_in": version, "dispositions": [],
                     "repeats": []}
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
    return extend_ledger(base, version=record["review"]["version"], findings=findings,
                         prior_dispositions=dispositions.values())


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
            ledger = _legacy_ledger(history[previous], record, (review.get("scope") or {}).get("since_version",
                                                                                               previous))
        history[review["version"]] = ledger
        previous = review["version"]
    return history


def carried_findings(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The prior findings a re-review disposes: every open or unverified entry of the latest ledger, where it was
    last reported. A finding a review only judged stays here until a review closes it."""
    records = list(records)
    if not records:
        return []
    history = ledger_history(records)
    by_version = {record["review"]["version"]: record for record in records}
    carried = []
    for entry in history[max(history)]:
        if entry["state"] == "closed":
            continue
        where = entry["repeats"][-1] if entry["repeats"] else entry
        finding = next((item for item in (by_version.get(where["version"]) or {}).get("findings", [])
                        if item["id"] == where["id"]), None)
        if finding is None:
            raise RecordError(f"Review v{where['version']} with finding {where['id']} is missing from the archive")
        carried.append({"id": ledger_id(entry["version"], entry["id"]), "severity": entry["severity"],
                        "category": entry["category"], "path": finding["path"], "line": finding["line"],
                        **({"title": finding["title"]} if "title" in finding else {}), "body": finding["body"]})
    return carried


def ledger_summary(record: dict[str, Any]) -> dict[str, Any] | None:
    """Open entries by severity, entries addressed, the earliest version an open entry dates from, and the record's
    version; None for a record written before ledgers, which is read as having no history."""
    if "ledger" not in record:
        return None
    opened = {severity: 0 for severity in sorted(SEVERITIES)}
    addressed = 0
    since: int | None = None
    for entry in record["ledger"]:
        if entry["state"] == "open":
            opened[entry["severity"]] += 1
            since = entry["version"] if since is None else min(since, entry["version"])
        elif entry["state"] == "closed" and entry["dispositions"][-1]["disposition"] == "addressed":
            addressed += 1
    return {"open": opened, "addressed": addressed, "since": since, "version": record["review"]["version"]}


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
    ledger = extend_ledger(prior_ledger or [], version=version, findings=findings,
                           prior_dispositions=adapter_result.get("prior_dispositions", []))
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
            "reviewed_at": reviewed_at or datetime.now(timezone.utc).isoformat(),
            "summary": adapter_result["summary"],
            "verdict": calculate_verdict(ledger, policy, unavailable),
            "counts": counts,
            "adapter": {
                "name": request["adapter"]["name"],
                "protocol_version": ADAPTER_PROTOCOL_VERSION,
                "scope": request["adapter"]["scope"],
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
    if adapter_result.get("comment_dispositions"):
        record["github_comments"] = [
            {key: comment[key] for key in COMMENT_FIELDS} for comment in request.get("github_comments", [])
        ]
        record["comment_dispositions"] = copy.deepcopy(adapter_result["comment_dispositions"])
    return record


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _duration(seconds: int | None) -> str:
    """A reviewer's time as `4m 05s`, or `-` when it was not timed."""
    if seconds is None:
        return "-"
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}m {remainder:02d}s" if minutes else f"{remainder}s"


def _path_set(paths: Any) -> bool:
    """A list of distinct non-empty paths."""
    return (isinstance(paths, list) and all(isinstance(path, str) and path for path in paths)
            and len(set(paths)) == len(paths))


def _validate_reviewers(reviewers: Any) -> None:
    """The reviewers that ran: which files each covered, what it found, how often it was retried, and how long it took."""
    if not isinstance(reviewers, list) or not reviewers:
        raise RecordError("Review reviewers must be a non-empty array")
    seen: set[str] = set()
    for reviewer in reviewers:
        if not isinstance(reviewer, dict) \
                or not REVIEWER_FIELDS <= set(reviewer) <= REVIEWER_FIELDS | OPTIONAL_REVIEWER_FIELDS:
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


def _safe_relative(path: Any) -> bool:
    return (isinstance(path, str) and bool(path) and not path.startswith(("/", "\\")) and "\\" not in path
            and ".." not in Path(path).parts)


def _validate_patches(patches: Any) -> None:
    """Each changed file's patch fingerprint, so the next re-review can tell which files changed since."""
    if not isinstance(patches, dict) or not patches:
        raise RecordError("Review patches must be a non-empty object")
    for path, patch in patches.items():
        if not _safe_relative(path):
            raise RecordError("Review patch path is unsafe")
        if not isinstance(patch, dict) or set(patch) != PATCH_FIELDS \
                or not isinstance(patch["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", patch["sha256"]) \
                or not _count(patch["lines"]):
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
    if compared != (scope["lines_changed"] is not None) or compared and not (
            _count(scope["files_changed"]) and scope["files_changed"] <= scope["files_total"]
            and _count(scope["lines_changed"]) and scope["lines_changed"] <= scope["lines_total"]):
        raise RecordError("Review scope changed counts must both be null or both be within their totals")
    if scope["used"] == "incremental" and not compared:
        raise RecordError("An incremental re-review must have compared with an earlier version")


def describe_scope(scope: dict[str, Any]) -> str:
    """One line on how much of a pull request a re-review covered, and why."""
    if scope["files_changed"] is None:
        compared = f"could not compare with v{scope['since_version']}"
    else:
        compared = (f"{scope['files_changed']} of {scope['files_total']} files and {scope['lines_changed']} of "
                    f"{scope['lines_total']} changed lines differ from v{scope['since_version']}")
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
        if not isinstance(comment["id"], str) or not re.fullmatch(r"C[1-9][0-9]*", comment["id"]) \
                or comment["id"] in ids:
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
    return (isinstance(value, dict) and set(value) == {"version", "id"} and _count(value["version"])
            and 1 <= value["version"] <= latest and isinstance(value["id"], str)
            and FINDING_ID.fullmatch(value["id"]) is not None)


def _validate_ledger_entry(entry: Any, version: int) -> None:
    if not isinstance(entry, dict) or set(entry) != LEDGER_FIELDS or not _count(entry["version"]) \
            or entry["version"] < 1 or not isinstance(entry["id"], str) or not FINDING_ID.fullmatch(entry["id"]):
        raise RecordError("Review ledger entry fields are malformed")
    label = ledger_id(entry["version"], entry["id"])
    if entry["version"] > version:
        raise RecordError(f"Review ledger entry {label} is after this review")
    if not _one_of(entry["severity"], SEVERITIES) or not isinstance(entry["category"], str) \
            or not entry["category"].strip():
        raise RecordError(f"Review ledger entry {label} severity or category is invalid")
    dispositions = entry["dispositions"]
    if not isinstance(dispositions, list) or any(
            not isinstance(item, dict) or set(item) != {"version", "disposition"} or not _count(item["version"])
            or not _one_of(item["disposition"], DISPOSITIONS) for item in dispositions):
        raise RecordError(f"Review ledger entry {label} dispositions are malformed")
    versions = [item["version"] for item in dispositions]
    if versions != sorted(set(versions)) or any(not entry["version"] < item <= version for item in versions):
        raise RecordError(f"Review ledger entry {label} dispositions are malformed")
    repeats = entry["repeats"]
    if not isinstance(repeats, list) or any(not _reference(item, version) or item["version"] < entry["version"]
                                            for item in repeats) \
            or len({(item["version"], item["id"]) for item in repeats}) != len(repeats):
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
    if not isinstance(ledger, list):
        raise RecordError("Review ledger must be an array")
    entries: dict[tuple[int, str], dict[str, Any]] = {}
    for entry in ledger:
        _validate_ledger_entry(entry, version)
        if (entry["version"], entry["id"]) in entries:
            raise RecordError("Review ledger entries must be unique")
        entries[(entry["version"], entry["id"])] = entry
    if [_entry_key(entry) for entry in ledger] != sorted(_entry_key(entry) for entry in ledger):
        raise RecordError("Review ledger entries must be ordered by version and finding ID")
    if mode == "initial" and any(entry["version"] != version for entry in ledger):
        raise RecordError("An initial review starts a fresh ledger")
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
    judged = {ledger_id(entry["version"], entry["id"]): item["disposition"]
              for entry in ledger for item in entry["dispositions"] if item["version"] == version}
    given = {item["finding_id"]: item["disposition"] for item in record["prior_dispositions"]}
    if judged != given:
        raise RecordError("Review prior dispositions do not match the ledger")
    linked: list[tuple[str, tuple[int, str]]] = []
    for identifier, finding in findings.items():
        if "repeats" not in finding:
            continue
        target = finding["repeats"]
        if not _reference(target, version):
            raise RecordError(f"Review finding {identifier}.repeats is malformed")
        key = (target["version"], target["id"])
        if target["version"] == version:
            if target["id"] == identifier:
                raise RecordError(f"Review finding {identifier} cannot repeat itself")
            other = findings.get(target["id"])
            if other is None:
                raise RecordError(f"Review finding {identifier} repeats an unknown finding: {target['id']}")
            if "repeats" in other:
                raise RecordError(f"Review finding {identifier} repeats a repeat: {target['id']}")
            severity = other["severity"]
        else:
            entry = entries.get(key)
            if entry is None:
                raise RecordError(f"Review finding {identifier} repeats an unknown finding: {ledger_id(*key)}")
            if given.get(ledger_id(*key)) not in OPEN_DISPOSITIONS:
                raise RecordError(f"Review finding {identifier} repeats {ledger_id(*key)}, so its disposition must "
                                  "be still_present or partially_addressed")
            severity = entry["severity"]
        if SEVERITY_RANK[severity] < SEVERITY_RANK[finding["severity"]]:
            raise RecordError(f"Review finding {identifier} repeats a less severe finding")
        if {"version": version, "id": identifier} not in entries[key]["repeats"]:
            raise RecordError(f"Review finding {identifier} is not in its target's ledger entry")
        linked.append((identifier, key))
    listed = [(item["id"], (entry["version"], entry["id"]))
              for entry in ledger for item in entry["repeats"] if item["version"] == version]
    if sorted(listed) != sorted(linked):
        raise RecordError("Review ledger repeats of this version must be its findings with repeats")


def validate_record(value: Any) -> dict[str, Any]:
    allowed_top = {
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
    if (
        not isinstance(value, dict)
        or set(value) - allowed_top
        or value.get("schema_version") != RECORD_SCHEMA_VERSION
    ):
        raise RecordError("Unsupported or malformed review record")
    _validate_comments(value)
    validate_repository_identity(value.get("repository"))
    pull = value.get("pull_request")
    review = value.get("review")
    if not isinstance(pull, dict) or not isinstance(review, dict):
        raise RecordError("Review record metadata is malformed")
    pull_fields = {"number", "url", "title", "base_ref", "base_sha", "head_sha"}
    if set(pull) not in (pull_fields, pull_fields | {"head_ref"}):
        raise RecordError("Review pull-request fields are malformed")
    if "head_ref" in pull and (not isinstance(pull["head_ref"], str) or not pull["head_ref"].strip()):
        raise RecordError("Review pull_request.head_ref is invalid")
    if (
        not isinstance(pull.get("number"), int)
        or isinstance(pull["number"], bool)
        or pull["number"] < 1
    ):
        raise RecordError("Review pull number is invalid")
    for field in ("url", "title", "base_ref"):
        if not isinstance(pull[field], str) or not pull[field].strip():
            raise RecordError(f"Review pull_request.{field} is invalid")
    _validate_sha(pull.get("base_sha"), "pull_request.base_sha")
    _validate_sha(pull.get("head_sha"), "pull_request.head_sha")
    review_fields = {"version", "mode", "reviewed_at", "summary", "verdict", "counts", "adapter"}
    # Records written before patches or scopes were recorded omit them.
    if not review_fields <= set(review) <= review_fields | {"coverage", "reviewers", "patches", "scope"}:
        raise RecordError("Review metadata fields are malformed")
    if "reviewers" in review:
        _validate_reviewers(review["reviewers"])
    if "patches" in review:
        _validate_patches(review["patches"])
    if not _one_of(review.get("verdict"), {"APPROVED", "CHANGES_REQUESTED", "INCOMPLETE"}):
        raise RecordError("Review verdict is invalid")
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
    if (
        not isinstance(review.get("version"), int)
        or isinstance(review["version"], bool)
        or review["version"] < 1
    ):
        raise RecordError("Review version is invalid")
    if not _one_of(review.get("mode"), {"initial", "re-review"}):
        raise RecordError("Review mode is invalid")
    if "scope" in review:
        _validate_scope(review["scope"], review)
    try:
        datetime.fromisoformat(review.get("reviewed_at"))
    except (TypeError, ValueError) as exc:
        raise RecordError("Review timestamp is invalid") from exc
    if not isinstance(review.get("summary"), str) or not review["summary"].strip():
        raise RecordError("Review summary is invalid")
    adapter = review.get("adapter")
    if not isinstance(adapter, dict) or set(adapter) != {
        "name",
        "protocol_version",
        "scope",
        "source_commit",
        "source_hashes",
        "reviewer",
        "status",
        "usage",
    }:
        raise RecordError("Review adapter metadata is malformed")
    if adapter["protocol_version"] != ADAPTER_PROTOCOL_VERSION:
        raise RecordError("Review adapter protocol is unsupported")
    if not _one_of(adapter["scope"], {"generic", "repository"}):
        raise RecordError("Review adapter scope is invalid")
    for field in ("name", "reviewer"):
        if not isinstance(adapter[field], str) or not adapter[field].strip():
            raise RecordError(f"Review adapter {field} is invalid")
    if not _one_of(adapter["status"], {"complete", "partial", "failed"}):
        raise RecordError("Review adapter status is invalid")
    if adapter["source_commit"] is not None:
        _validate_sha(adapter["source_commit"], "review.adapter.source_commit")
    source_hashes = adapter["source_hashes"]
    if not isinstance(source_hashes, dict):
        raise RecordError("Review adapter source_hashes is invalid")
    for path, hash_value in source_hashes.items():
        if (
            not isinstance(path, str)
            or not path
            or path.startswith(("/", "\\"))
            or "\\" in path
            or ".." in Path(path).parts
        ):
            raise RecordError("Review adapter source hash path is unsafe")
        if not isinstance(hash_value, str) or not re.fullmatch(r"[0-9a-f]{64}", hash_value):
            raise RecordError(f"Review adapter source hash is invalid: {path}")
    if adapter["usage"] is not None and not isinstance(adapter["usage"], dict):
        raise RecordError("Review adapter usage is invalid")
    findings = value.get("findings")
    if not isinstance(findings, list):
        raise RecordError("Review findings must be an array")
    expected_ids = [f"F{index:03d}" for index in range(1, len(findings) + 1)]
    if [item.get("id") for item in findings if isinstance(item, dict)] != expected_ids:
        raise RecordError("Review finding IDs are not stable and contiguous")
    for finding in findings:
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
        if (
            finding["path"].startswith(("/", "\\"))
            or "\\" in finding["path"]
            or ".." in Path(finding["path"]).parts
        ):
            raise RecordError(f"Review finding {finding['id']}.path is unsafe")
        if (
            not isinstance(finding["line"], int)
            or isinstance(finding["line"], bool)
            or finding["line"] < 1
        ):
            raise RecordError(f"Review finding {finding['id']}.line is invalid")
    counts = review.get("counts")
    expected_counts = {
        severity: sum(item["severity"] == severity for item in findings)
        for severity in sorted(SEVERITIES)
    }
    if counts != expected_counts:
        raise RecordError("Review finding counts do not match findings")
    dispositions = value.get("prior_dispositions")
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
    _validate_ledger(value, review["version"], review["mode"])
    artifacts = value.get("artifacts")
    if artifacts is not None:
        if not isinstance(artifacts, dict) or set(artifacts) != {"payload_sha256", "markdown_sha256"}:
            raise RecordError("Review artifact hashes are malformed")
        _validate_sha(artifacts["payload_sha256"], "artifacts.payload_sha256")
        _validate_sha(artifacts["markdown_sha256"], "artifacts.markdown_sha256")
    return value


def payload_hash(record: dict[str, Any]) -> str:
    payload = {key: value for key, value in record.items() if key != "artifacts"}
    return sha256_text(canonical_json(payload))


SEVERITY_SECTIONS = (("MUST_FIX", "MUST FIX"), ("SHOULD_FIX", "SHOULD FIX"), ("SUGGESTION", "SUGGESTIONS"))


def _code(text: str) -> str:
    """Inline code span that survives backticks and line breaks in the text."""
    text = " ".join(text.split())
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _cell(text: str) -> str:
    """Single-line Markdown table cell content."""
    return " ".join(text.split()).replace("|", "\\|")


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
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%d-%b-%Y %H:%M UTC")


def _finding_block(finding: dict[str, Any], *, repeats: str | None = None) -> list[str]:
    """One finding's collapsible block; `repeats` names the finding in this review it repeats."""
    headline = (
        _html(finding["title"]) if "title" in finding
        else f"<code>{_html(PurePosixPath(finding['path']).name)}:{finding['line']}</code>"
    )
    # A specialist's evidence is the added line itself, so it joins the line number instead of
    # repeating the location on a line of its own; other evidence stays after the body.
    added_prefix = f"{finding['path']}:{finding['line']} adds: "
    if finding["evidence"].startswith(added_prefix):
        location = [f"> **Line {finding['line']}:** {_code(finding['evidence'][len(added_prefix):])} "
                    f"| **Source:** {_html(finding['source'])}"]
        evidence = []
    else:
        location = [f"> **Line:** {finding['line']} | **Source:** {_html(finding['source'])}"]
        evidence = [">", f"> **Evidence:** {_code(finding['evidence'])}"]
    if "analyzer" in finding:
        evidence.extend([">", f"> **Analyzer:** {_analyzer_note(finding['analyzer'])}"])
    if "repeats" in finding and repeats is None:
        evidence.extend([">", f"> **Repeats:** v{finding['repeats']['version']} {finding['repeats']['id']}, "
                              "an earlier finding this review found still present"])
    prefix = f"Repeats {repeats}: " if repeats else ""
    return [
        "<details open>",
        f"<summary>{finding['id']}. {prefix}[{_html(finding['category'])}] {headline}</summary>",
        "",
        f"> **File:** {_code(finding['path'])}  ",
        *location,
        ">",
        *_quote(finding["body"]),
        *evidence,
        "",
        "</details>",
        "",
    ]


def render_markdown(record: dict[str, Any], *, record_payload_hash: str) -> str:
    pull = record["pull_request"]
    review = record["review"]
    adapter = review["adapter"]
    heading = f"# Code Review — {record['repository']}#{pull['number']}"
    if review["mode"] == "re-review":
        heading += f" (re-review v{review['version']})"
    lines = [
        heading,
        "",
        "| | |",
        "|---|---|",
        f"| **Title** | {_cell(pull['title'])} |",
        (f"| **Branch** | {_cell(_code(pull['head_ref']))} → {_cell(_code(pull['base_ref']))} |"
         if pull.get("head_ref") else f"| **Base** | {_cell(_code(pull['base_ref']))} |"),
        f"| **URL** | {_cell(pull['url'])} |",
        f"| **Reviewed** | {_reviewed_at(review['reviewed_at'])} |",
        f"| **Verdict** | {_label(review['verdict'])} |",
        *([f"| **Scope** | {_cell(describe_scope(review['scope']))} |"] if "scope" in review else []),
        "",
    ]
    unavailable = (review.get("coverage") or {}).get("unavailable_sources", [])
    if unavailable:
        lines.extend([
            "> **Not reviewed in full:** these changed files were too large or could not be represented safely, "
            "so reviewers saw only their diff: " + ", ".join(_code(path) for path in unavailable) + ".",
            "",
        ])
    uncovered = (review.get("coverage") or {}).get("uncovered_files", [])
    if uncovered:
        lines.extend([
            "> **Not reviewed:** no specialist covers these changed files, and the reviewer manifest sets "
            "`uncovered` to `ignore`, so no reviewer saw them: " + ", ".join(_code(path) for path in uncovered) + ".",
            "",
        ])
    lines.extend(["---", "", "## Summary", "", review["summary"].strip(), "", "## Findings", ""])
    if not record["findings"]:
        lines.extend(["No findings.", ""])
    # A repeat of a finding in this review is shown inside the finding it repeats, not counted in its own section.
    nested: dict[str, list[dict[str, Any]]] = {}
    for finding in record["findings"]:
        if (finding.get("repeats") or {}).get("version") == review["version"]:
            nested.setdefault(finding["repeats"]["id"], []).append(finding)
    for severity, title in SEVERITY_SECTIONS:
        group = [finding for finding in record["findings"] if finding["severity"] == severity
                 and (finding.get("repeats") or {}).get("version") != review["version"]]
        if not group:
            continue
        lines.extend(["<details open>", f"<summary><strong>{title} ({len(group)})</strong></summary>", ""])
        for finding in group:
            block = _finding_block(finding)
            for repeat in nested.get(finding["id"], []):
                block[-2:-2] = _finding_block(repeat, repeats=finding["id"])
            lines.extend(block)
        lines.extend(["</details>", ""])
    carried = [entry for entry in record.get("ledger", [])
               if entry["version"] < review["version"] and entry["state"] != "closed"]
    if carried:
        lines.extend([
            "## Open Findings", "",
            "Findings from earlier reviews that no review has closed. Open ones count toward the verdict.", "",
            "| Entry | Severity | Category | Last judged | State |",
            "|-------|----------|----------|-------------|-------|",
        ])
        for entry in carried:
            lines.append(f"| v{entry['version']} {entry['id']} | {_label(entry['severity'])} "
                         f"| {_cell(entry['category'])} | v{entry['judged_in']} | {_label(entry['state'])} |")
        lines.append("")
    comments = {comment["id"]: comment for comment in record.get("github_comments", [])}
    if record.get("prior_dispositions") or comments:
        lines.extend(["## Prior Findings Status", ""])
    if record.get("prior_dispositions"):
        if comments:
            lines.extend(["### From the previous AI review", ""])
        lines.extend(["| # | Status | Rationale |", "|---|--------|-----------|"])
        for disposition in record["prior_dispositions"]:
            lines.append(
                f"| {_cell(disposition['finding_id'])} | {_label(disposition['disposition'])} "
                f"| {_cell(disposition['rationale'])} |"
            )
        lines.extend(["", _addressed(record["prior_dispositions"]), ""])
    if comments:
        lines.extend([
            "### From GitHub PR comments", "",
            "| # | Comment | Status | Rationale |", "|---|---------|--------|-----------|",
        ])
        for disposition in record["comment_dispositions"]:
            comment = comments[disposition["comment_id"]]
            location = comment["path"] + (f":{comment['line']}" if comment["line"] else "")
            excerpt = " ".join(comment["body"].split())
            excerpt = excerpt if len(excerpt) <= 120 else excerpt[:117].rstrip() + "..."
            outdated = " (outdated)" if comment["outdated"] else ""
            lines.append(
                f"| [{comment['id']}]({comment['url']}) | @{_cell(comment['author'])} on "
                f"{_cell(_code(location))}{outdated}: {_cell(excerpt)} | {_label(disposition['disposition'])} "
                f"| {_cell(disposition['rationale'])} |"
            )
        lines.extend(["", _addressed(record["comment_dispositions"]), ""])
    if review.get("reviewers"):
        lines.extend([
            "## Reviewers", "",
            "| Reviewer | Focus | Model | Files | Findings | Retries | Time |",
            "|----------|-------|-------|-------|----------|---------|------|",
        ])
        for reviewer in review["reviewers"]:
            focus = reviewer["category"] + (" (dispositions only)" if reviewer["dispositions_only"] else "")
            lines.append(
                f"| {_cell(_code(reviewer['id']))} | {_cell(focus)} | {_cell(reviewer.get('model', '-'))} "
                f"| {reviewer['files']} "
                f"| {reviewer['findings']} | {reviewer['retries']} | {_duration(reviewer.get('seconds'))} |"
            )
        lines.append("")
    lines.extend([
        "---",
        "",
        "<details>",
        "<summary><strong>Review Details</strong></summary>",
        "",
        "| | |",
        "|---|---|",
        f"| **Mode** | {review['mode']} v{review['version']} |",
        f"| **Adapter** | {_cell(_code(adapter['name']))} ({adapter['scope']}) |",
        f"| **Reviewer** | {_cell(adapter['reviewer'])} ({adapter['status']}) |",
        f"| **Base SHA** | `{pull['base_sha']}` |",
        f"| **Reviewed HEAD** | `{pull['head_sha']}` |",
        f"| **Record payload SHA-256** | `{record_payload_hash}` |",
        "",
        "</details>",
        "",
        f"<!-- reviewed_head_sha: {pull['head_sha']} -->",
        "",
    ])
    return "\n".join(lines)


def _addressed(dispositions: list[dict[str, Any]]) -> str:
    addressed = sum(1 for disposition in dispositions if disposition["disposition"] == "addressed")
    return f"**{addressed}/{len(dispositions)} addressed**"


def write_record_pair(json_path: Path, markdown_path: Path, record: dict[str, Any]) -> dict[str, Any]:
    validate_record(record)
    payload_sha = payload_hash(record)
    markdown = render_markdown(record, record_payload_hash=payload_sha)
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


def validate_record_pair(json_path: Path, markdown_path: Path) -> dict[str, Any]:
    record = validate_record(read_json(json_path))
    markdown = markdown_path.read_text(encoding="utf-8")
    if payload_hash(record) != record["artifacts"]["payload_sha256"]:
        raise RecordError("Review JSON payload hash mismatch")
    if sha256_text(markdown) != record["artifacts"]["markdown_sha256"]:
        raise RecordError("Review Markdown hash mismatch")
    expected_marker = f"Record payload SHA-256** | `{record['artifacts']['payload_sha256']}`"
    if expected_marker not in markdown:
        raise RecordError("Review Markdown does not reference the JSON payload hash")
    return record

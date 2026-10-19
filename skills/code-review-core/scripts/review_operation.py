"""Pure producer workflow helpers shared by initial review and re-review skills."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from review_archive import (
    archive_head,
    commit_record,
    current_ledger,
    latest_record,
    ledger_digest,
    list_versions,
    pull_directory,
    record_paths,
)
from review_config import validate_repository_identity
from review_io import PersistenceError, read_json
from review_records import (
    LedgerEntry,
    ReviewRecord,
    build_record,
    ledger_history,
    ledger_summary,
    payload_hash,
    validate_adapter_result,
)
from review_state import utc_date, watermark_date
from typed_fields import FieldError, FieldReader


class ReviewOperationError(ValueError):
    pass


class ReviewOperationFieldError(FieldError, ReviewOperationError):
    """A pull request, request, run, or legacy index field of the wrong kind."""


READ = FieldReader(ReviewOperationFieldError)


PULL_FIELDS = {
    "number",
    "title",
    "url",
    "state",
    "isDraft",
    "baseRefName",
    "baseRefOid",
    "headRefOid",
    "headRefName",
    "mergedAt",
}


def parse_pull_selector(value: str) -> tuple[str, int]:
    match = re.fullmatch(r"([^#]+)#([1-9][0-9]*)", READ.string(value, "pull selector"))
    if match is None:
        raise ReviewOperationError("Pull selector must be owner/repository#number")
    repository = validate_repository_identity(match.group(1))
    return repository, int(match.group(2))


def validate_canary_pull(value: Any, *, repository: str, number: int) -> dict[str, Any]:
    validate_repository_identity(repository)
    pull = validate_pull(value)
    if pull["number"] != number:
        raise ReviewOperationError("Canary pull metadata does not match the selector")
    return pull


def validate_pull(value: Any) -> dict[str, Any]:
    """A pull request's metadata; the one pull request a review reads also carries its description, `body`."""
    pull = READ.json_object(value, "pull", required=PULL_FIELDS, optional={"body"})
    READ.string(pull.get("body", ""), "pull.body", empty=True)
    READ.integer(pull["number"], "pull.number", minimum=1)
    state = READ.one_of(pull["state"], "pull.state", ("OPEN", "MERGED"))
    READ.boolean(pull["isDraft"], "pull.isDraft")
    for field in ("title", "url", "baseRefName", "baseRefOid", "headRefOid", "headRefName"):
        READ.string(pull[field], f"pull.{field}")
    if state == "MERGED":
        READ.string(pull["mergedAt"], "pull.mergedAt")
    return pull


def select_eligible_pulls(
    pulls: Iterable[dict[str, Any]],
    *,
    merged_since: date,
    reviewed_heads: Callable[[list[int]], dict[int, str]],
    force: bool = False,
) -> list[dict[str, Any]]:
    """The non-draft open pull requests and those merged on or after `merged_since` whose head is not yet reviewed.

    A merged pull request is judged by its merge date alone, so one merged earlier and updated since stays out.
    `reviewed_heads` reads the archive, so it is asked only about the pull requests the state, draft, and watermark
    filters keep, after every pull is validated, and not at all under `force`.
    """
    window = []
    seen: set[int] = set()
    for raw in pulls:
        pull = validate_pull(raw)
        number = pull["number"]
        if number in seen:
            raise ReviewOperationError(f"Duplicate pull metadata: {number}")
        seen.add(number)
        if pull["isDraft"]:
            continue
        if pull["state"] == "MERGED" and merged_date(pull) < merged_since:
            continue
        window.append(pull)
    window.sort(key=lambda pull: pull["number"])
    if force:
        return window
    heads = reviewed_heads([pull["number"] for pull in window])
    return [pull for pull in window if heads.get(pull["number"]) != pull["headRefOid"]]


def split_batch(eligible: list[dict[str, Any]], size: int | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The eligible pull requests one batch run reviews, in their given order, and those it defers to the next run.

    The run takes the oldest first the way the watermark advances: merged pull requests by merge date, which is what
    holds the watermark back, then open ones by number. With no size, it takes every one."""
    if size is None or len(eligible) <= size:
        return list(eligible), []

    def age(pull: dict[str, Any]) -> tuple[bool, date, int]:
        merged = pull["state"] == "MERGED"
        return (not merged, merged_date(pull) if merged else date.min, pull["number"])

    taken = {pull["number"] for pull in sorted(eligible, key=age)[:size]}
    return (
        [pull for pull in eligible if pull["number"] in taken],
        [pull for pull in eligible if pull["number"] not in taken],
    )


def merged_date(pull: dict[str, Any]) -> date:
    try:
        return utc_date(pull["mergedAt"])
    except (TypeError, ValueError) as exc:
        raise ReviewOperationError(f"Pull {pull['number']} has invalid mergedAt") from exc


def safe_watermark(
    *,
    previous: date,
    today: date,
    eligible_merged: Iterable[dict[str, Any]],
    completed_numbers: set[int],
) -> date:
    pending_dates = []
    for raw in eligible_merged:
        pull = validate_pull(raw)
        if pull["number"] not in completed_numbers:
            pending_dates.append(merged_date(pull))
    candidate = today if not pending_dates else min(pending_dates) - timedelta(days=1)
    return max(previous, candidate)


def request_to_record_input(
    request: dict[str, Any],
    adapter: dict[str, Any],
    reviewers: list[dict[str, Any]] | None = None,
    *,
    patches: dict[str, Any] | None = None,
    scope: dict[str, Any] | None = None,
    uncovered_files: list[str] | None = None,
    dispatch: str | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pull = READ.json_object(request.get("pull_request"), "request.pull_request")
    return {
        "reviewers": reviewers or [],
        "patches": patches,
        "scope": scope,
        "dispatch": dispatch,
        "snapshot": snapshot,
        "github_comments": list(request.get("github_comments") or []),
        "head_ref": pull.get("head_ref"),
        "body_characters": pull.get("body_characters"),
        "body_given": pull.get("body_given"),
        "repository": request["repository"],
        "pull_number": request["pull_number"],
        "pull_url": pull["url"],
        "title": pull["title"],
        "base_ref": pull["base_ref"],
        "base_sha": pull["base_sha"],
        "head_sha": pull["head_sha"],
        "mode": request["mode"],
        "adapter": adapter,
        "unavailable_sources": list((request.get("coverage") or {}).get("unavailable_sources", [])),
        # Absent from runs prepared before unreviewed files were recorded.
        "unreviewed_files": dict((request.get("coverage") or {}).get("unreviewed_files", {})),
        "uncovered_files": list(uncovered_files or []),
    }


def prior_severities(request: dict[str, Any]) -> dict[str, str]:
    """Each prior finding's severity, by ID, so a result's `repeats` of it can be checked.

    A finding without a string severity is left out, which its readers treat alike: as an unknown severity.
    """
    severities: dict[str, str] = {}
    for item in request.get("prior_findings") or []:
        try:
            finding = READ.json_object(item, "request.prior_findings[]")
            identifier = READ.string(finding.get("id"), "request.prior_findings[].id")
            severities[identifier] = READ.string(finding.get("severity"), "request.prior_findings[].severity")
        except FieldError:
            continue
    return severities


def _ids(items: Any, path: str) -> list[str]:
    """The ID of each item of the request's array at `path`."""
    return READ.array(
        items, path, lambda item, item_path: READ.string(READ.json_object(item, item_path).get("id"), f"{item_path}.id")
    )


def archive_base(current: int | None, ledger: list[LedgerEntry]) -> dict[str, Any]:
    """The archive state a review is judged against, as prepare records it in run.json: its latest version (None
    without one) and the SHA-256 of that version's ledger."""
    return {"version": current, "ledger_sha256": ledger_digest(ledger)}


def _version_name(version: Any) -> str:
    return "no review" if version is None else f"version {version}"


def check_archive_base(base: object, current: int | None, ledger: list[LedgerEntry]) -> None:
    """Fail unless the archive still holds the version and ledger a review was judged against.

    A review's dispositions are judged against the version prepare saw, so recording them on top of a later one
    could reopen entries it closed or record an older head as the latest review. commit_record then holds the same
    version under the pull request's lock while it writes.
    """
    try:
        recorded = READ.json_object(base, "run.base", required={"version", "ledger_sha256"})
    except FieldError as exc:
        raise ReviewOperationError(
            "The run does not record the archive version it was prepared against; nothing was recorded. "
            "Prepare the review again"
        ) from exc
    version = READ.optional(READ.integer, recorded["version"], "run.base.version", minimum=1)
    if version != current:
        raise ReviewOperationError(
            f"The archive moved since prepare: the review was judged against {_version_name(version)}, "
            f"and the archive now holds {_version_name(current)}; nothing was recorded. Prepare the review again"
        )
    if READ.hex_digest(recorded["ledger_sha256"], "run.base.ledger_sha256", 64) != ledger_digest(ledger):
        raise ReviewOperationError(
            f"The ledger of {_version_name(current)} changed since prepare; nothing was recorded. "
            "Prepare the review again"
        )


def commit_adapter_result(
    *,
    request_path: Path,
    result_path: Path,
    archive_root: Path,
    policy: dict[str, Any],
    adapter: dict[str, Any],
    base: Any,
    local_mirror_root: Path | None = None,
    reviewers: list[dict[str, Any]] | None = None,
    require_comment_dispositions: bool = True,
    patches: dict[str, Any] | None = None,
    scope: dict[str, Any] | None = None,
    uncovered_files: list[str] | None = None,
    model_names: dict[str, str] | None = None,
    flags: list[dict[str, Any]] | None = None,
    dispatch: str | None = None,
    snapshot: dict[str, Any] | None = None,
    documents: dict[str, list[str]] | None = None,
) -> tuple[Path, Path, ReviewRecord]:
    """Archive a validated reviewer result as the pull request's next review record. `documents` holds, for each
    changed document whose extracted text the reviewers read, each line's place in the document, which a finding on
    that line records as its `location`."""
    request = read_json(request_path)
    result_value = read_json(result_path)
    prior_ids = _ids(request.get("prior_findings", []), "request.prior_findings")
    result = validate_adapter_result(
        result_value,
        expected_repository=request["repository"],
        expected_number=request["pull_number"],
        expected_head_sha=request["pull_request"]["head_sha"],
        prior_ids=prior_ids,
        comment_ids=_ids(request.get("github_comments", []), "request.github_comments"),
        require_comment_dispositions=require_comment_dispositions,
        prior_severities=prior_severities(request),
    )
    if result["status"] != "complete":
        raise ReviewOperationError(f"Reviewer result status is {result['status']}; incomplete results are not archived")
    for finding in result["findings"]:
        places = (documents or {}).get(finding["path"], [])
        if finding["line"] <= len(places) and places[finding["line"] - 1]:
            finding["location"] = places[finding["line"] - 1]
    repository = validate_repository_identity(request["repository"])
    number = request["pull_number"]
    current, ledger = archive_head(archive_root, repository, number)
    check_archive_base(base, current, ledger)
    version = 1 if current is None else current + 1
    record_input = request_to_record_input(
        request,
        adapter,
        reviewers,
        patches=patches,
        scope=scope,
        uncovered_files=uncovered_files,
        dispatch=dispatch,
        snapshot=snapshot,
    )
    # A re-review judges and extends the archive's latest ledger; an initial review starts a fresh one.
    prior_ledger = ledger if request["mode"] == "re-review" else []
    record = build_record(record_input, result, version=version, policy=policy, prior_ledger=prior_ledger)
    if local_mirror_root is not None:
        local_versions = list_versions(pull_directory(local_mirror_root, repository, number))
        local_current = local_versions[-1] if local_versions else None
        if local_current == version:
            pending = latest_record(local_mirror_root, repository, number)
            if pending is None:
                raise ReviewOperationError("Local mirror reports a version without a valid record")
            candidate = build_record(
                record_input,
                result,
                version=version,
                policy=policy,
                reviewed_at=pending["review"]["reviewed_at"],
                prior_ledger=prior_ledger,
            )
            if payload_hash(candidate) != payload_hash(pending):
                raise ReviewOperationError(
                    "Local mirror contains a different pending review; reconcile before retrying"
                )
            record = pending
        elif local_current != current:
            raise ReviewOperationError("Local mirror and archive versions differ; reconcile before committing")
        else:
            commit_record(
                local_mirror_root,
                repository,
                number,
                record,
                expected_latest_version=local_current,
                model_names=model_names,
                flags=flags or [],
            )
    return commit_record(
        archive_root,
        repository,
        number,
        record,
        expected_latest_version=current,
        model_names=model_names,
        flags=flags or [],
    )


LEGACY_INDEX_KEYS = {
    "schema_version",
    "kind",
    "repository",
    "pull_number",
    "reviewed_at",
    "reviewed_head_sha",
    "verdict",
    "source_sha256",
    "source_path",
    "source_file_sha256",
}


def legacy_index(archive_root: Path, repository: str, number: int) -> dict[str, Any] | None:
    """The migrated legacy review index for a pull request, or None when absent or invalid.

    An invalid index is treated as unreviewed so the pull request is offered for review again.
    """
    repository = validate_repository_identity(repository)
    path = pull_directory(archive_root, repository, number) / "legacy-review.json"
    if not path.is_file():
        return None
    try:
        index = READ.json_object(read_json(path), "legacy-index", required=LEGACY_INDEX_KEYS)
        version = READ.integer(index["schema_version"], "legacy-index.schema_version")
        READ.one_of(index["kind"], "legacy-index.kind", ("legacy-review-index",))
        indexed = READ.string(index["repository"], "legacy-index.repository")
        pull_number = READ.integer(index["pull_number"], "legacy-index.pull_number")
        READ.hex_digest(index["reviewed_head_sha"], "legacy-index.reviewed_head_sha", 40, 64)
        READ.string(index["verdict"], "legacy-index.verdict", empty=True)
    except (PersistenceError, FieldError):
        return None
    if version != 1 or indexed.lower() != repository or pull_number != number:
        return None
    return index


LEGACY_COUNT = re.compile(r"<summary><strong>(MUST FIX|SHOULD FIX|SUGGESTIONS) \((\d+)\)</strong></summary>")


def legacy_counts(report: Path) -> dict[str, int] | None:
    """Finding counts from a migrated legacy report's collapsible section headings, or None if unreadable."""
    try:
        text = report.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None
    counts = {"MUST_FIX": 0, "SHOULD_FIX": 0, "SUGGESTION": 0}
    for label, value in LEGACY_COUNT.findall(text):
        counts[{"MUST FIX": "MUST_FIX", "SHOULD FIX": "SHOULD_FIX", "SUGGESTIONS": "SUGGESTION"}[label]] += int(value)
    return counts


def reviewed_head(
    archive_root: Path, repository: str, number: int, *, records: list[ReviewRecord] | None = None
) -> dict[str, Any] | None:
    """The latest reviewed head: a validated review record, else a migrated legacy review.

    Also reports the review's verdict, finding counts, ledger summary, and report path for dashboards. A record
    written before ledgers is summarized from the ledger its pull request's records compute. A legacy review's
    findings were never converted, so its summary counts its report's findings as open and cannot say how many
    were addressed (None); it is None when the report is unreadable. A caller that already holds the pull request's
    validated records, oldest first as `pull_records` returns them, passes them as `records` so none is read again.
    """
    directory = pull_directory(archive_root, repository, number)
    record = records[-1] if records else None
    if records is None:
        record = latest_record(archive_root, repository, number)
    if record is not None:
        review = record["review"]
        coverage = review.get("coverage")
        if "ledger" in record:
            ledger = record["ledger"]
        elif records is None:
            ledger = current_ledger(archive_root, repository, number)
        else:
            ledger = ledger_history(records)[review["version"]]
        _, markdown = record_paths(directory, review["version"])
        return {
            "head_sha": record["pull_request"]["head_sha"],
            "source": "record",
            "version": review["version"],
            "incomplete": bool(coverage and coverage["unavailable_sources"]),
            "verdict": review["verdict"],
            "counts": dict(review["counts"]),
            "ledger": ledger_summary(ledger, review["version"]),
            "report": str(markdown),
        }
    legacy = legacy_index(archive_root, repository, number)
    if legacy is not None:
        report = directory / "legacy-review.md"
        counts = legacy_counts(report)
        summary = None if counts is None else {"open": counts, "addressed": None, "since": None, "version": None}
        return {
            "head_sha": legacy["reviewed_head_sha"],
            "source": "legacy",
            "version": None,
            "incomplete": False,
            "verdict": legacy["verdict"].replace(" ", "_"),
            "counts": counts,
            "ledger": summary,
            "report": str(report) if report.is_file() else None,
        }
    return None


def latest_reviewed_heads(archive_root: Path, repository: str, numbers: Iterable[int]) -> dict[int, str]:
    heads: dict[int, str] = {}
    for number in numbers:
        reviewed = reviewed_head(archive_root, repository, number)
        if reviewed is not None:
            heads[number] = reviewed["head_sha"]
    return heads


def recorded_watermark(state: dict[str, Any], repository: str) -> date | None:
    """A repository's recorded merged-pull watermark, or None before its first batch run advances one."""
    identity = validate_repository_identity(repository)
    value = state.get("repositories", {}).get(identity, {}).get("merged_since")
    return None if value is None else watermark_date(identity, value)

"""Pure producer workflow helpers shared by initial review and re-review skills."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable

from review_archive import commit_record, latest_record, list_versions, pull_directory, record_paths
from review_config import validate_repository_identity
from review_io import read_json
from review_records import build_record, payload_hash, validate_adapter_result


class ReviewOperationError(ValueError):
    pass


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
    if not isinstance(value, str):
        raise ReviewOperationError("Pull selector must be owner/repository#number")
    match = re.fullmatch(r"([^#]+)#([1-9][0-9]*)", value)
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
    if not isinstance(value, dict) or set(value) != PULL_FIELDS:
        raise ReviewOperationError("Pull metadata fields do not match the contract")
    if not isinstance(value["number"], int) or isinstance(value["number"], bool) or value["number"] < 1:
        raise ReviewOperationError("Pull number must be positive")
    if value["state"] not in {"OPEN", "MERGED"} or not isinstance(value["isDraft"], bool):
        raise ReviewOperationError("Pull state or draft flag is invalid")
    for field in ("title", "url", "baseRefName", "baseRefOid", "headRefOid", "headRefName"):
        if not isinstance(value[field], str) or not value[field]:
            raise ReviewOperationError(f"Pull {field} is required")
    if value["state"] == "MERGED" and not isinstance(value["mergedAt"], str):
        raise ReviewOperationError("Merged pull requires mergedAt")
    return value


def select_eligible_pulls(
    pulls: Iterable[dict[str, Any]],
    *,
    merged_since: date,
    reviewed_heads: dict[int, str],
    force: bool = False,
) -> list[dict[str, Any]]:
    eligible = []
    seen: set[int] = set()
    for raw in pulls:
        pull = validate_pull(raw)
        number = pull["number"]
        if number in seen:
            raise ReviewOperationError(f"Duplicate pull metadata: {number}")
        seen.add(number)
        if pull["isDraft"]:
            continue
        if pull["state"] == "MERGED":
            try:
                merged_date = date.fromisoformat(pull["mergedAt"][:10])
            except (TypeError, ValueError) as exc:
                raise ReviewOperationError(f"Pull {number} has invalid mergedAt") from exc
            if merged_date < merged_since:
                continue
        if not force and reviewed_heads.get(number) == pull["headRefOid"]:
            continue
        eligible.append(pull)
    return sorted(eligible, key=lambda pull: pull["number"])


def safe_watermark(
    *,
    previous: date,
    today: date,
    eligible_merged: Iterable[dict[str, Any]],
    completed_numbers: set[int],
    enumeration_complete: bool,
) -> date:
    if not enumeration_complete:
        return previous
    pending_dates = []
    for raw in eligible_merged:
        pull = validate_pull(raw)
        if pull["state"] != "MERGED":
            continue
        if pull["number"] not in completed_numbers:
            pending_dates.append(date.fromisoformat(pull["mergedAt"][:10]))
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
) -> dict[str, Any]:
    pull = request.get("pull_request")
    if not isinstance(pull, dict):
        raise ReviewOperationError("Adapter request pull_request is invalid")
    return {
        "reviewers": reviewers or [],
        "patches": patches,
        "scope": scope,
        "github_comments": list(request.get("github_comments") or []),
        "head_ref": pull.get("head_ref"),
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
        "uncovered_files": list(uncovered_files or []),
    }


def _ids(items: Any, what: str) -> list[str]:
    if not isinstance(items, list):
        raise ReviewOperationError(f"Adapter request {what}s are invalid")
    ids = [item.get("id") for item in items if isinstance(item, dict)]
    if len(ids) != len(items) or any(not isinstance(value, str) for value in ids):
        raise ReviewOperationError(f"Every {what} requires an ID")
    return ids


def commit_adapter_result(
    *,
    request_path: Path,
    result_path: Path,
    archive_root: Path,
    policy: dict[str, Any],
    adapter: dict[str, Any],
    local_mirror_root: Path | None = None,
    reviewers: list[dict[str, Any]] | None = None,
    require_comment_dispositions: bool = True,
    patches: dict[str, Any] | None = None,
    scope: dict[str, Any] | None = None,
    uncovered_files: list[str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    request = read_json(request_path)
    result_value = read_json(result_path)
    prior_ids = _ids(request.get("prior_findings", []), "prior finding")
    result = validate_adapter_result(
        result_value,
        expected_repository=request["repository"],
        expected_number=request["pull_number"],
        expected_head_sha=request["pull_request"]["head_sha"],
        prior_ids=prior_ids,
        comment_ids=_ids(request.get("github_comments", []), "review comment"),
        require_comment_dispositions=require_comment_dispositions,
    )
    if result["status"] != "complete":
        raise ReviewOperationError(
            f"Reviewer result status is {result['status']}; incomplete results are not archived"
        )
    repository = validate_repository_identity(request["repository"])
    number = request["pull_number"]
    archive_versions = list_versions(pull_directory(archive_root, repository, number))
    current = archive_versions[-1] if archive_versions else None
    version = 1 if current is None else current + 1
    record_input = request_to_record_input(request, adapter, reviewers, patches=patches, scope=scope,
                                           uncovered_files=uncovered_files)
    record = build_record(record_input, result, version=version, policy=policy)
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
            )
            if payload_hash(candidate) != payload_hash(pending):
                raise ReviewOperationError(
                    "Local mirror contains a different pending review; reconcile before retrying"
                )
            record = pending
        elif local_current != current:
            raise ReviewOperationError(
                "Local mirror and archive versions differ; reconcile before committing"
            )
        else:
            commit_record(
                local_mirror_root,
                repository,
                number,
                record,
                expected_latest_version=local_current,
            )
    return commit_record(
        archive_root,
        repository,
        number,
        record,
        expected_latest_version=current,
    )


LEGACY_INDEX_KEYS = {
    "schema_version", "kind", "repository", "pull_number", "reviewed_at", "reviewed_head_sha",
    "verdict", "source_sha256", "source_path", "source_file_sha256",
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
        value = read_json(path)
    except Exception:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != LEGACY_INDEX_KEYS
        or value["schema_version"] != 1
        or value["kind"] != "legacy-review-index"
        or not isinstance(value["repository"], str)
        or value["repository"].lower() != repository
        or value["pull_number"] != number
        or not isinstance(value["reviewed_head_sha"], str)
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value["reviewed_head_sha"])
    ):
        return None
    return value


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


def reviewed_head(archive_root: Path, repository: str, number: int) -> dict[str, Any] | None:
    """The latest reviewed head: a validated review record, else a migrated legacy review.

    Also reports the review's verdict, finding counts, and report path for dashboards.
    """
    directory = pull_directory(archive_root, repository, number)
    record = latest_record(archive_root, repository, number)
    if record is not None:
        review = record["review"]
        coverage = review.get("coverage") or {}
        _, markdown = record_paths(directory, review["version"])
        return {"head_sha": record["pull_request"]["head_sha"], "source": "record", "version": review["version"],
                "incomplete": bool(coverage.get("unavailable_sources")), "verdict": review["verdict"],
                "counts": dict(review["counts"]), "report": str(markdown)}
    legacy = legacy_index(archive_root, repository, number)
    if legacy is not None:
        report = directory / "legacy-review.md"
        return {"head_sha": legacy["reviewed_head_sha"], "source": "legacy", "version": None, "incomplete": False,
                "verdict": legacy["verdict"].replace(" ", "_"), "counts": legacy_counts(report),
                "report": str(report) if report.is_file() else None}
    return None


def latest_reviewed_heads(archive_root: Path, repository: str, numbers: Iterable[int]) -> dict[int, str]:
    heads: dict[int, str] = {}
    for number in numbers:
        reviewed = reviewed_head(archive_root, repository, number)
        if reviewed is not None:
            heads[number] = reviewed["head_sha"]
    return heads


def repository_watermark(state: dict[str, Any], repository: str, today: date) -> date:
    """A repository's merged-pull watermark; a repository without one starts today, so a first batch
    run reviews only open pull requests instead of every merged pull request in its history."""
    entry = state.get("repositories", {}).get(validate_repository_identity(repository), {})
    value = entry.get("merged_since")
    return date.fromisoformat(value[:10]) if isinstance(value, str) else today


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report reviewed heads for pull requests.")
    commands = parser.add_subparsers(dest="command", required=True)
    heads = commands.add_parser("reviewed-heads")
    heads.add_argument("--repository", required=True)
    heads.add_argument("--archive-root", type=Path, help="defaults to the configured archive_root")
    heads.add_argument("numbers", nargs="+", type=int)
    args = parser.parse_args(arguments)
    archive_root = args.archive_root
    if archive_root is None:
        from review_config import load_config
        archive_root = Path(load_config()["archive_root"])
    result = {str(n): reviewed_head(archive_root, args.repository, n) for n in args.numbers}
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

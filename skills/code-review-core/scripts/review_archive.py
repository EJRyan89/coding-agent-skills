"""Collision-safe review archive paths and version allocation."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from review_config import validate_repository_identity
from review_io import PersistenceError, ResourceLock
from review_records import canonical_json, ledger_history, sha256_text, validate_record_pair, write_record_pair


class ArchiveError(RuntimeError):
    pass


def pulls_directory(root: Path, repository: str) -> Path:
    owner, name = validate_repository_identity(repository).split("/", 1)
    return root / owner / name / "pulls"


def pull_directory(root: Path, repository: str, pull_number: int) -> Path:
    pulls = pulls_directory(root, repository)
    if not isinstance(pull_number, int) or isinstance(pull_number, bool) or pull_number < 1:
        raise ArchiveError("Pull number must be a positive integer")
    return pulls / str(pull_number)


def record_files(root: Path, repository: str) -> list[Path]:
    """The JSON file of every review record pair of a repository's pull requests, sorted: each pull request's version
    chain and any pair beside it, such as one converted from another tool, which list_versions leaves out."""
    pulls = pulls_directory(root, repository)
    return sorted(pulls.glob("*/review*.json")) if pulls.exists() else []


def _version_from_name(path: Path) -> int | None:
    if path.name == "review.json":
        return 1
    match = re.fullmatch(r"review-v([1-9][0-9]*)\.json", path.name)
    if not match:
        return None
    version = int(match.group(1))
    return version if version >= 2 else None


def list_versions(directory: Path) -> list[int]:
    if not directory.exists():
        return []
    versions = [version for path in directory.glob("review*.json") if (version := _version_from_name(path))]
    return sorted(set(versions))


def record_paths(directory: Path, version: int) -> tuple[Path, Path]:
    if version == 1:
        return directory / "review.json", directory / "review.md"
    if version < 2:
        raise ArchiveError("Review version must be positive")
    return directory / f"review-v{version}.json", directory / f"review-v{version}.md"


def latest_record(root: Path, repository: str, pull_number: int) -> dict[str, Any] | None:
    directory = pull_directory(root, repository, pull_number)
    versions = list_versions(directory)
    if not versions:
        return None
    json_path, markdown_path = record_paths(directory, versions[-1])
    return validate_record_pair(json_path, markdown_path)


def pull_records(root: Path, repository: str, pull_number: int) -> list[dict[str, Any]]:
    """Every validated review record of a pull request, oldest first."""
    directory = pull_directory(root, repository, pull_number)
    return [validate_record_pair(*record_paths(directory, version)) for version in list_versions(directory)]


def archive_head(root: Path, repository: str, pull_number: int) -> tuple[int | None, list[dict[str, Any]]]:
    """A pull request's latest review version (None without one) and that version's finding ledger, from one read.

    The ledger is computed from the earlier records when the latest was written before ledgers, and is empty when the
    pull request has no review.
    """
    directory = pull_directory(root, repository, pull_number)
    versions = list_versions(directory)
    history = ledger_history(validate_record_pair(*record_paths(directory, version)) for version in versions)
    return (versions[-1] if versions else None), (history[max(history)] if history else [])


def current_ledger(root: Path, repository: str, pull_number: int) -> list[dict[str, Any]]:
    """The latest record's finding ledger; empty when the pull request has no review."""
    return archive_head(root, repository, pull_number)[1]


def ledger_digest(ledger: list[dict[str, Any]]) -> str:
    """The SHA-256 of a ledger's canonical JSON, which prepare records so finalize can tell that it changed."""
    return sha256_text(canonical_json(ledger))


def commit_record(
    root: Path,
    repository: str,
    pull_number: int,
    record: dict[str, Any],
    *,
    expected_latest_version: int | None,
    model_names: dict[str, str] | None = None,
    flags: Iterable[dict[str, Any]] = (),
) -> tuple[Path, Path, dict[str, Any]]:
    """Archive the next review version. A re-review's report shows findings raised by earlier versions, so it is
    rendered from the records already archived here."""
    directory = pull_directory(root, repository, pull_number)
    lock_key = f"{repository.replace('/', '__')}#{pull_number}.lock"
    lock = root / ".locks" / lock_key
    with ResourceLock(lock):
        versions = list_versions(directory)
        current = versions[-1] if versions else None
        if current != expected_latest_version:
            raise ArchiveError(
                f"Review version changed concurrently: expected {expected_latest_version}, found {current}"
            )
        version = 1 if current is None else current + 1
        if record.get("review", {}).get("version") != version:
            raise ArchiveError(f"Record version must be {version}")
        json_path, markdown_path = record_paths(directory, version)
        if json_path.exists() or markdown_path.exists():
            raise PersistenceError("Review destination already exists")
        prior_records = (
            [validate_record_pair(*record_paths(directory, item)) for item in versions]
            if record["review"]["mode"] == "re-review"
            else []
        )
        directory.mkdir(parents=True, exist_ok=True)
        persisted = write_record_pair(
            json_path, markdown_path, record, prior_records=prior_records, model_names=model_names, flags=flags
        )
        return json_path, markdown_path, persisted

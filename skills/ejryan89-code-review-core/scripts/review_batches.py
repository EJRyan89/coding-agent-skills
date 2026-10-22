"""Batch runs: enumerating the pull requests to review, the merged-pull watermarks, and run lists."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from github_client import GitHubError
from review_archive import ArchiveError
from review_config import load_config, resolve_repositories
from review_io import PersistenceError, atomic_write_json, map_in_order, read_json
from review_operation import (
    ReviewOperationError,
    latest_reviewed_heads,
    recorded_watermark,
    safe_watermark,
    select_eligible_pulls,
    split_batch,
    validate_pull,
)
from review_records import RE_REVIEW_SCOPES, RecordError
from review_run import EXPECTED_ERRORS, READ, PipelineError, Services, load_run, run_dispatch
from review_runtime import RUNTIME_CAPABILITIES
from review_state import StateError, default_state_path, load_state, update_state
from skill_roots import skills_directory_holding

# Version 2 records each repository's deferred pull requests, which hold its watermark back.
BATCH_SCHEMA_VERSION = 2


RUN_LIST_SCHEMA_VERSION = 1


# The run list prepare writes in a new temporary directory: the pull requests or fixtures it has still to prepare,
# and every run it published, which every later command takes as --runs.
RUN_LIST_FILE = "runs.json"


RUN_LIST_PREFIX = "review-prs-runs-"


# A listed run is published by prepare, valid once check accepts it, failed once check or finalize fails it, and
# recorded once finalize records it.
RUN_STATUSES = ("published", "valid", "failed", "recorded")


# What a run list has still to prepare: a pull request to review or re-review, or a fixture directory.
PENDING_KINDS = ("pull", "re-review", "fixture")


# Pull requests or fixtures one prepare call prepares, the rest staying in its run list: it bounds the call's duration
# and the reviewers started together.
MAX_PREPARE_PULLS = 4


def enumerate_batch(
    output: Path,
    *,
    repositories: list[str] | None = None,
    repository_set: str | None = None,
    force: bool = False,
    batch_size: int | None = None,
    config_path: Path | None = None,
    services: Services | None = None,
) -> dict[str, Any]:
    """The open non-draft and newly merged pull requests each repository still needs reviewed.

    A repository's configured `batch_size`, or `batch_size` when given, bounds how many the batch takes; the rest are
    recorded as deferred, so `advance` keeps the watermark short of them and the next run takes them."""
    services = services or Services()
    config = load_config(config_path)
    selected = resolve_repositories(
        config, explicit=repositories, repository_set=repository_set, operation="review-prs"
    )
    archive_root = Path(config["archive_root"])
    state = load_state()
    today = services.today()
    batch: dict[str, Any] = {"schema_version": BATCH_SCHEMA_VERSION, "today": today.isoformat(), "repositories": {}}

    def listing(repository: str) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "previous_watermark": None,  # until the recorded watermark is read
            "complete": False,
            "error": None,
            "eligible": [],
            "deferred": [],
            "listing": None,
        }
        read: list[int] = []

        def reviewed_heads(numbers: list[int]) -> dict[int, str]:
            read.extend(numbers)
            return latest_reviewed_heads(archive_root, repository, numbers)

        configured: dict[str, Any] | None = config["repositories"].get(repository)
        if configured is None:
            # An explicit --repository need not be configured, but prepare refuses one that is not, so it fails here.
            entry["error"] = f"{repository} is not a configured repository"
            return entry
        try:
            recorded = recorded_watermark(state, repository)
            # A repository without a watermark starts today in UTC, so its first batch run reviews the open pull
            # requests and those merged today instead of every merged pull request in its history.
            watermark = today if recorded is None else recorded
            entry["previous_watermark"] = watermark.isoformat()
            if recorded is None:
                # No watermark yet: list the whole history once; `advance` then records one.
                listings = [services.github.list_pulls(repository, state="all")]
            else:
                listings = [
                    services.github.list_pulls(repository, state="open"),
                    services.github.list_closed_pulls_since(repository, watermark),
                ]
            # A pull request merged between the two listings is in both; the later listing is the current one.
            pulls = {pull["number"]: pull for found in listings for pull in found.pulls}
            eligible = select_eligible_pulls(
                pulls.values(), merged_since=watermark, reviewed_heads=reviewed_heads, force=force
            )
            size = configured["batch_size"] if batch_size is None else batch_size
            entry["eligible"], entry["deferred"] = split_batch(eligible, size)
            entry["listing"] = {
                "scan": "full" if recorded is None else "watermark",
                "pages": sum(found.pages for found in listings),
                "pulls": len(pulls),
                "read": len(read),
            }
            entry["complete"] = True
        except (GitHubError, ReviewOperationError, ArchiveError, PersistenceError, RecordError, StateError) as exc:
            entry["error"] = str(exc)
        return entry

    for repository, (entry, _) in zip(selected, map_in_order(listing, selected), strict=True):
        batch["repositories"][repository] = entry
    atomic_write_json(output, batch)
    return batch


def advance_watermarks(
    batch_path: Path, *, config_path: Path | None = None, services: Services | None = None
) -> dict[str, tuple[str | None, str | None]]:
    """Advance each fully enumerated repository past the merged pull requests whose heads are now reviewed.

    A repository whose enumeration failed keeps its watermark (None in the result), so no work is skipped; its
    previous watermark is None when that watermark could not be read. The batch file is input, so a batch dated after
    the pipeline's own today, or one that would move a watermark past it, is refused with the state unchanged: such a
    watermark is one no merged pull request ever passes again.
    """
    batch = _load_batch(batch_path)
    config = load_config(config_path)
    archive_root = Path(config["archive_root"])
    today = date.fromisoformat(batch["today"])
    now = (services or Services()).today()
    if today > now:
        raise PipelineError(f"{batch_path} is dated {today.isoformat()}, after today, {now.isoformat()}")
    changes: dict[str, tuple[str | None, str | None]] = {}
    for repository, entry in batch["repositories"].items():
        if not entry["complete"]:
            changes[repository] = (entry["previous_watermark"], None)
            continue
        previous = date.fromisoformat(entry["previous_watermark"])
        # A deferred merged pull request is pending like an unreviewed one, so a bounded batch skips nothing.
        eligible = [validate_pull(pull) for pull in (*entry["eligible"], *entry["deferred"])]
        merged = [pull for pull in eligible if pull["state"] == "MERGED"]
        heads = latest_reviewed_heads(archive_root, repository, [pull["number"] for pull in merged])
        completed = {pull["number"] for pull in merged if heads.get(pull["number"]) == pull["headRefOid"]}
        candidate = safe_watermark(previous=previous, today=today, eligible_merged=merged, completed_numbers=completed)
        if candidate > now:
            raise PipelineError(
                f"{batch_path} would move the {repository} watermark to {candidate.isoformat()}, "
                f"after today, {now.isoformat()}"
            )
        changes[repository] = (previous.isoformat(), candidate.isoformat())

    def update(current: dict[str, Any]) -> dict[str, Any]:
        # Only ever move forward, so a concurrent run's later watermark is never undone.
        for repository, (_, new) in changes.items():
            if new is None:
                continue
            recorded = current["repositories"].setdefault(repository, {})
            existing = recorded.get("merged_since")
            if existing is None or new > existing[:10]:
                recorded["merged_since"] = new
                recorded["updated_at"] = today.isoformat()
        return current

    if any(new is not None for _, new in changes.values()):
        update_state(default_state_path(), update)
    return changes


def _load_batch(batch_path: Path) -> dict[str, Any]:
    batch = READ.json_object(read_json(batch_path), "batch")
    if READ.integer(batch.get("schema_version"), "batch.schema_version") != BATCH_SCHEMA_VERSION:
        raise PipelineError(f"{batch_path} is not a review batch file")
    return batch


def batch_selectors(batch_path: Path) -> list[str]:
    """Each pull request a batch file lists for review, in the order enumerate printed its PULL lines."""
    batch = _load_batch(batch_path)
    selectors = []
    for repository, entry in READ.json_object(batch.get("repositories"), "batch.repositories").items():
        path = f"batch.repositories.{repository}"
        pulls = READ.array(READ.json_object(entry, path).get("eligible"), f"{path}.eligible")
        for index, pull in enumerate(pulls):
            pull = READ.json_object(pull, f"{path}.eligible[{index}]")
            selectors.append(f"{repository}#{READ.integer(pull.get('number'), f'{path}.eligible[{index}].number')}")
    return selectors


def new_run_list(pending: list[dict[str, str]], options: dict[str, Any]) -> dict[str, Any]:
    """A run list with nothing prepared yet. `pending` holds each pull request or fixture in the order given, as its
    kind and its selector or directory; `options` holds the rest of prepare's arguments, which every group takes."""
    return {"schema_version": RUN_LIST_SCHEMA_VERSION, "options": options, "pending": pending, "runs": []}


RUN_LIST_OPTIONS = ("config", "host", "inline", "force", "canary", "scope", "prior")


def _read_run_list_options(value: Any, path: str) -> dict[str, Any]:
    options = READ.json_object(value, path, required=RUN_LIST_OPTIONS)
    for name in ("config", "prior"):
        READ.optional(READ.string, options[name], f"{path}.{name}")
    READ.optional(READ.one_of, options["host"], f"{path}.host", sorted(RUNTIME_CAPABILITIES))
    READ.optional(READ.one_of, options["scope"], f"{path}.scope", RE_REVIEW_SCOPES)
    for name in ("inline", "force", "canary"):
        READ.boolean(options[name], f"{path}.{name}")
    return options


def _read_pending(value: Any, path: str) -> dict[str, str]:
    item = READ.json_object(value, path, required=("kind", "target"))
    READ.one_of(item["kind"], f"{path}.kind", PENDING_KINDS)
    READ.string(item["target"], f"{path}.target", single_line=True)
    return item


def _read_listed_run(value: Any, path: str) -> dict[str, str]:
    item = READ.json_object(value, path, required=("run", "selector", "status"))
    READ.string(item["run"], f"{path}.run", single_line=True)
    READ.string(item["selector"], f"{path}.selector", single_line=True)
    READ.one_of(item["status"], f"{path}.status", RUN_STATUSES)
    return item


def load_run_list(path: Path) -> dict[str, Any]:
    """The run list at `path`, read whole and checked, since it is input like any other path a command is given.

    Each listed run is still read as a prepared run by the command that uses it, so a list naming a directory that is
    not one fails that run alone. A list inside a skills directory is refused, because commands write it back."""
    root = skills_directory_holding(path)
    if root is not None:
        raise PipelineError(f"{path} is inside the skills directory {root}; pass the run list prepare printed")
    value = READ.json_object(read_json(path), "run list")
    if READ.integer(value.get("schema_version"), "run list.schema_version") != RUN_LIST_SCHEMA_VERSION:
        raise PipelineError(f"{path} is not a review run list")
    READ.json_object(value, "run list", required=("schema_version", "options", "pending", "runs"))
    _read_run_list_options(value["options"], "run list.options")
    READ.array(value["pending"], "run list.pending", _read_pending)
    READ.array(value["runs"], "run list.runs", _read_listed_run)
    return value


def listed_runs(runs: dict[str, Any], *statuses: str) -> list[dict[str, str]]:
    """The listed runs in any of `statuses`, in the order prepare published them."""
    return [entry for entry in runs["runs"] if entry["status"] in statuses]


def worked_by_subagents(run: Path) -> bool:
    """Whether a run's roles start as subagents, as workflow and wait-reviewers serve them, rather than on the Copilot
    CLI host or inline. A run that cannot be read counts, so the command it was listed for reports it."""
    try:
        state = load_run(run.resolve())
    except EXPECTED_ERRORS:
        return True
    return run_dispatch(state) not in {"inline", "copilot-host"}

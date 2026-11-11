"""Batch runs: enumerating the pull requests to review, the merged-pull watermarks, and run lists."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any, Literal, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import calendar_date
from github_client import GitHubError
from review_archive import ArchiveError
from review_config import load_config, resolve_repositories, validate_repository_identity
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
from typed_fields import describe

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
        # An OSError is an archive folder it cannot stat or list, which fails this repository's entry, never the batch.
        except (
            GitHubError,
            ReviewOperationError,
            ArchiveError,
            PersistenceError,
            RecordError,
            StateError,
            OSError,
        ) as exc:
            entry["error"] = str(exc)
        return entry

    for repository, (entry, _) in zip(selected, map_in_order(listing, selected), strict=True):
        batch["repositories"][repository] = entry
    atomic_write_json(output, batch)
    return batch


class WatermarkChange(NamedTuple):
    """What `advance` did to one repository's watermark, as the state holds it once the update is written.

    `moved` from `previous`, the state's date before (the batch's starting watermark when the state held none), to
    `current`; `held` at `current`, which the state already held at or past the batch's candidate, as when a concurrent
    run moved it further; or `failed`, its enumeration incomplete, keeping the batch's `previous`, None when that
    watermark could not be read, and no `current`.
    """

    outcome: Literal["moved", "held", "failed"]
    previous: str | None
    current: str | None


def advance_watermarks(
    batch_path: Path, *, config_path: Path | None = None, services: Services | None = None
) -> dict[str, WatermarkChange]:
    """Advance each fully enumerated repository past the merged pull requests whose heads are now reviewed.

    A repository whose enumeration failed keeps its watermark, so no work is skipped. The batch file is input, so it
    is read whole and checked before anything else, and a batch dated after the pipeline's own today, or one that would
    move a watermark past it, is refused with the state unchanged: such a watermark is one no merged pull request ever
    passes again. Each change is what the state holds once the update is written, never the batch's candidate alone.
    """
    batch = load_batch(batch_path)
    config = load_config(config_path)
    archive_root = Path(config["archive_root"])
    today = date.fromisoformat(batch["today"])
    now = (services or Services()).today()
    if today > now:
        raise PipelineError(f"{batch_path} is dated {today.isoformat()}, after today, {now.isoformat()}")
    candidates: dict[str, WatermarkChange] = {}
    for repository, entry in batch["repositories"].items():
        if not entry["complete"]:
            candidates[repository] = WatermarkChange("failed", entry["previous_watermark"], None)
            continue
        previous = date.fromisoformat(entry["previous_watermark"])
        # A deferred merged pull request is pending like an unreviewed one, so a bounded batch skips nothing.
        merged = [pull for pull in (*entry["eligible"], *entry["deferred"]) if pull["state"] == "MERGED"]
        heads = latest_reviewed_heads(archive_root, repository, [pull["number"] for pull in merged])
        completed = {pull["number"] for pull in merged if heads.get(pull["number"]) == pull["headRefOid"]}
        candidate = safe_watermark(previous=previous, today=today, eligible_merged=merged, completed_numbers=completed)
        if candidate > now:
            raise PipelineError(
                f"{batch_path} would move the {repository} watermark to {candidate.isoformat()}, "
                f"after today, {now.isoformat()}"
            )
        candidates[repository] = WatermarkChange("moved", previous.isoformat(), candidate.isoformat())
    changes = dict(candidates)

    def update(current: dict[str, Any]) -> dict[str, Any]:
        # Only ever move forward, so a concurrent run's later watermark is never undone, and report what the state
        # then holds: a candidate at or behind it moves nothing.
        for repository, (_, previous, new) in candidates.items():
            if new is None:  # enumeration failed
                continue
            recorded = current["repositories"].setdefault(repository, {})
            existing = recorded.get("merged_since")
            if existing is None or new > existing[:10]:
                recorded["merged_since"] = new
                recorded["updated_at"] = today.isoformat()
                changes[repository] = WatermarkChange("moved", previous if existing is None else existing[:10], new)
            else:
                changes[repository] = WatermarkChange("held", existing[:10], existing[:10])
        return current

    if any(change.outcome != "failed" for change in candidates.values()):
        update_state(default_state_path(), update)
    return changes


BATCH_FIELDS = ("schema_version", "today", "repositories")


BATCH_ENTRY_FIELDS = ("previous_watermark", "complete", "error", "eligible", "deferred", "listing")


LISTING_FIELDS = ("scan", "pages", "pulls", "read")


LISTING_SCANS = ("full", "watermark")


def _batch_date(value: object, path: str) -> str:
    """A YYYY-MM-DD date as enumerate writes one, and nothing else `date.fromisoformat` would also take."""
    text = READ.string(value, path)
    if calendar_date.parse(text) is None:
        raise READ.fail(path, "a YYYY-MM-DD date", describe(text))
    return text


def _batch_listing(value: object, path: str) -> dict[str, Any]:
    listing = READ.json_object(value, path, required=LISTING_FIELDS)
    READ.one_of(listing["scan"], f"{path}.scan", LISTING_SCANS)
    for name in ("pages", "pulls", "read"):
        READ.integer(listing[name], f"{path}.{name}", minimum=0)
    return listing


def _batch_entry(value: object, path: str) -> dict[str, Any]:
    """One repository's entry: a complete one names the watermark it started from and no error, and an incomplete
    one the error that stopped it."""
    entry = READ.json_object(value, path, required=BATCH_ENTRY_FIELDS)
    complete = READ.boolean(entry["complete"], f"{path}.complete")
    previous = READ.optional(_batch_date, entry["previous_watermark"], f"{path}.previous_watermark")
    error = READ.optional(READ.string, entry["error"], f"{path}.error", empty=True)
    for name in ("eligible", "deferred"):
        for index, pull in enumerate(READ.array(entry[name], f"{path}.{name}")):
            validate_pull(READ.json_object(pull, f"{path}.{name}[{index}]"))
    READ.optional(_batch_listing, entry["listing"], f"{path}.listing")
    if complete and previous is None:
        raise READ.fail(f"{path}.previous_watermark", "a YYYY-MM-DD date in a complete entry", "null")
    if complete and error is not None:
        raise READ.fail(f"{path}.error", "null in a complete entry", describe(error))
    if not complete and error is None:
        raise READ.fail(f"{path}.error", "a string in an incomplete entry", "null")
    return entry


def load_batch(batch_path: Path) -> dict[str, Any]:
    """The batch file enumerate wrote, read whole and checked, since like any path a command is given it is input:
    a field of another kind is a `PipelineFieldError` naming it, and its repositories are keyed by their lowercase
    identities, two spellings of one refused. Each pull request is one `validate_pull` accepts."""
    batch = READ.json_object(read_json(batch_path), "batch")
    if READ.integer(batch.get("schema_version"), "batch.schema_version") != BATCH_SCHEMA_VERSION:
        raise PipelineError(f"{batch_path} is not a review batch file")
    READ.json_object(batch, "batch", required=BATCH_FIELDS)
    _batch_date(batch["today"], "batch.today")
    repositories: dict[str, Any] = {}
    for identity, entry in READ.json_object(batch["repositories"], "batch.repositories").items():
        key = validate_repository_identity(identity, "a key of batch.repositories")
        if key in repositories:
            raise PipelineError(f"{batch_path} lists {key} under more than one spelling")
        repositories[key] = _batch_entry(entry, f"batch.repositories.{identity}")
    return {**batch, "repositories": repositories}


def batch_selectors(batch_path: Path) -> list[str]:
    """Each pull request a batch file lists for review, in the order enumerate printed its PULL lines."""
    batch = load_batch(batch_path)
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

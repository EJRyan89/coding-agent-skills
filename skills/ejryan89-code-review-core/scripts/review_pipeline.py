"""Deterministic review steps, so an orchestrating agent only dispatches reviewers.

    enumerate  list the pull requests a batch run should review, to a batch file (by default in a new
               temporary directory)
    prepare    fetch pull requests, snapshot each head, load its reviewer, write the request and prompts; or, as a
               fixture canary, read each pull request from a local fixture directory and nothing from GitHub
    dispatch   start the Copilot CLI host for a prepared run, detached, and return (copilot-cli runtime only)
    next-role  hand the orchestrating session the next role of an inline run to work itself, once the run's
               sealed files are unchanged
    wait       wait a bounded time for that host: its result, its failure, or how long it has run
    workflow   write a Claude Code Workflow script that starts every role of several runs at once
    wait-reviewers  wait a bounded time for the Workflow's reviewers: which roles are ready, running, or overdue
    check      validate reviewer results; set aside invalid ones and say which roles to rerun
    validate-result  tell a reviewer whether check would accept its result, changing nothing
    finalize   assemble each result and commit its review record (or a canary pair)
    unfinalized  list the prepared runs that never reached finalize, each its pull request's failure
    advance    move each fully enumerated repository's merged-pull watermark after a batch

    inspect-reviewer   say whether a repository's review skill runs as one reviewer or needs a manifest
    validate-reviewer  prove a repository reviewer's files, patterns, and routing without running a review

Every command prints machine-readable lines on stdout. It exits 0 with its result, including a state to poll
again (`RUNNING`), and 1 with findings to act on (`RETRY`, `INVALID`, `UNFINALIZED`) or an expected failure,
printed as a `FAILED <reason>` line on stdout. Exit 2 is only argparse's usage error, before any work.
`prepare` takes any number of pull requests, or a batch file, prepares them four at a time, and keeps the runs it
publishes in a run list it names in a `RUNS <file> pending=<n>` line; `prepare --runs <file>` prepares the next four.
`workflow`, `wait-reviewers`, `check`, `finalize`, and `unfinalized` take that list as `--runs <file>` in place of
one `--run` per run directory, so the orchestrator never carries run directories from one command to the next. Each
pull request or run succeeds or fails on its own, and every line names its pull request.
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from console import run_main
from review_archive import archive_head, commit_record, pull_records
from review_batches import (
    MAX_PREPARE_PULLS,
    RUN_LIST_FILE,
    RUN_LIST_PREFIX,
    RUN_STATUSES,
    advance_watermarks,
    batch_selectors,
    enumerate_batch,
    listed_runs,
    load_run_list,
    new_run_list,
    worked_by_subagents,
)
from review_canary import fixture_change, read_fixture_pull, validate_prior_record
from review_config import default_config_path, load_config
from review_dispatch import (
    MAX_WAIT_SECONDS,
    SCRIPT_BEGIN,
    SCRIPT_END,
    check_run,
    count_reads,
    dispatch_copilot,
    next_role,
    reviewer_seconds,
    role_errors,
    run_host,
    unfinalized_selector,
    unguarded_roles,
    validate_result,
    wait_for_host,
    wait_for_reviewers,
    workflow_script,
)
from review_flags import default_flags_path, load_store
from review_guard import RUN_PREFIX
from review_io import (
    PersistenceError,
    atomic_write_json,
    atomic_write_text,
    map_in_order,
    read_diff,
    read_json,
    working_path,
)
from review_operation import (
    archive_base,
    commit_adapter_result,
    parse_pull_selector,
    reviewed_head,
    validate_canary_pull,
)
from review_records import (
    RE_REVIEW_SCOPES,
    ReviewRecord,
    Scope,
    carried_findings,
    describe_scope,
    ledger_history,
    ledger_summary,
    snapshot_seconds,
    validate_record,
)
from review_reporting import _print_ready, _print_settings, reviewer_summaries, stats_lines
from review_reviewer_setup import (
    GENERIC_REVIEWER,
    _configured_entry,
    _materialize_reviewer,
    fixture_reviewer,
    inspect_reviewer,
    validate_reviewer,
)
from review_run import (
    EXPECTED_ERRORS,
    RECORDED_FILE,
    RUN_FILE,
    RUN_SCHEMA_VERSION,
    SOURCE_REPOSITORY,
    PipelineError,
    RunRole,
    RunState,
    Services,
    _inside,
    _sha256,
    _write_run,
    command_script,
    load_run,
    mark_dispatched,
    remove_run,
    run_dispatch,
    self_check_command,
    verify_seal,
)
from review_runtime import (
    PULL_REQUEST_BODY_FILE,
    RUNTIME_CAPABILITIES,
    SNAPSHOT_FETCHABLE,
    PrepareGitRunner,
    build_adapter_request,
    given_body,
    stamp_source_snapshot,
    write_adapter_request,
)
from review_snapshot import (
    ChangeRead,
    PullDiff,
    _base_reader,
    _document_diffs,
    _fetch_diff,
    _retake_snapshot,
    _snapshot,
    _snapshot_reads,
    _write_diff,
)
from review_source import source_commands
from review_specialists import (
    assemble,
    build_plan,
    parse_unified_diff,
    renamed_from,
    render_entrypoint_prompt,
    reviewer_models,
    role_summaries,
)

# Seconds between finalize's attempts to remove a recorded run whose files another process, such as antivirus or the
# exiting Copilot CLI host, still holds open.
RUN_REMOVAL_DELAYS = (0.25, 0.5, 1.0, 2.0)


def choose_scope(
    requested: str,
    previous: ReviewRecord,
    patches: dict[str, dict[str, Any]],
    *,
    thresholds: dict[str, Any],
    entrypoint: bool,
) -> tuple[Scope, set[str] | None]:
    """How much of a pull request a re-review covers: the record's scope, and the files to review in full for
    an incremental pass (None for a full one).

    A file counts as changed when its patch fingerprint differs from the previous review's, or it was not in
    that review. A previous review recorded without fingerprints cannot be compared, so it gets a full pass.
    An incremental pass also reviews in full each file of the pull request the previous record left unreviewed,
    changed or not: no reviewer has seen it, so a reviewer that covers it on this head reviews it, and one that
    does not leaves it in this record's `uncovered_files` too.
    """
    earlier = previous["review"].get("patches")
    changed = (
        None
        if earlier is None
        else {
            path for path, patch in patches.items() if path not in earlier or earlier[path]["sha256"] != patch["sha256"]
        }
    )
    lines_total = sum(patch["lines"] for patch in patches.values())
    lines_changed = None if changed is None else sum(patches[path]["lines"] for path in changed)
    if requested == "full":
        used, reason = "full", "a full re-review was requested"
    elif changed is None:
        used, reason = "full", "the earlier review recorded no patches to compare with"
    elif entrypoint:
        used, reason = "full", "the repository's reviewer runs as one entrypoint, which always reviews everything"
    elif requested == "incremental":
        used, reason = "incremental", "an incremental re-review was requested"
    else:
        share = lines_changed / lines_total if lines_total else len(changed) / len(patches)
        if share >= thresholds["full_share"]:
            used, reason = "full", f"at least {thresholds['full_share']:.0%} of the changed lines differ"
        elif lines_changed >= thresholds["full_lines"]:
            used, reason = "full", f"at least {thresholds['full_lines']} changed lines differ"
        else:
            used, reason = "incremental", "the change since then is under the full-review thresholds"
    scope: Scope = {
        "requested": requested,
        "since_version": previous["review"]["version"],
        "files_changed": None if changed is None else len(changed),
        "files_total": len(patches),
        "lines_changed": lines_changed,
        "lines_total": lines_total,
        "used": used,
        "reason": reason,
    }
    if used != "incremental" or changed is None:
        return scope, None
    coverage = previous["review"].get("coverage")
    unseen = coverage.get("uncovered_files", []) if coverage else []
    return scope, changed | {path for path in unseen if path in patches}


def _skip(selector: str, reason: str) -> dict[str, Any]:
    return {"status": "skip", "selector": selector, "reason": reason}


def _review_history(
    archive_root: Path, repository: str, number: int, head: str, *, re_review: bool, force: bool, notes: list[str]
) -> tuple[str, ReviewRecord | None, list[dict[str, Any]]] | None:
    """What the archive says a review of `head` starts from: its mode, the previous record, and the prior findings.
    None when that head is already reviewed and the review is not forced."""
    selector = f"{repository}#{number}"
    reviewed = reviewed_head(archive_root, repository, number)
    if re_review and reviewed is None:
        raise PipelineError(f"{selector} has no review yet; run review-prs --pull {selector}")
    if reviewed is not None and reviewed["head_sha"] == head and not force:
        return None
    if re_review and reviewed is not None and reviewed["source"] == "legacy":
        notes.append("This initial review supersedes the migrated legacy review.")
        return "initial", None, []
    if re_review:
        # Every finding no review has closed, not only the latest review's: one a review only judged still
        # present would otherwise never be offered again.
        records = pull_records(archive_root, repository, number)
        return "re-review", records[-1], carried_findings(records, load_store(default_flags_path())["flags"])
    return "initial", None, []


def _write_request(
    request_path: Path,
    *,
    mode: str,
    repository: str,
    number: int,
    pull: dict[str, Any],
    diff_path: Path,
    source: Path,
    manifest: dict[str, Any],
    prior: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    gaps: list[str],
    unreviewed_suffixes: tuple[str, ...] = (),
) -> None:
    """The adapter request, whose unavailable sources include every changed path whose change no reviewer could be
    given whole, which is then no unreviewed file, and beside it the file of the pull request's description, which is
    never prompt text."""
    body = pull.get("body", "")
    body_path = request_path.parent / PULL_REQUEST_BODY_FILE
    atomic_write_text(body_path, given_body(body))
    request = build_adapter_request(
        mode=mode,
        repository=repository,
        pull_number=number,
        base_ref=pull["baseRefName"],
        head_ref=pull["headRefName"],
        base_sha=pull["baseRefOid"],
        head_sha=pull["headRefOid"],
        title=pull["title"],
        url=pull["url"],
        diff_path=diff_path,
        source_snapshot_root=source,
        prior_findings=prior,
        github_comments=comments,
        snapshot=manifest,  # prepare materialized and verified it, in this call
        body_file=body_path,
        body_characters=len(body),
        unreviewed_suffixes=unreviewed_suffixes,
    )
    coverage = request["coverage"]
    coverage["unavailable_sources"] = sorted({*coverage["unavailable_sources"], *gaps})
    coverage["unreviewed_files"] = {
        path: suffix for path, suffix in coverage["unreviewed_files"].items() if path not in gaps
    }
    write_adapter_request(request_path, request)


def _source_commands(run: Path, role: str, script: str, lazy: bool) -> tuple[str | None, str]:
    """A role's `source-file` command, only for a lazy snapshot, which leaves files to fetch, and its `source-search`,
    for every snapshot, so a reviewer searches the code where the runtime gives it no Grep or Glob."""
    fetch, search = source_commands(run, role, script=script)
    return (fetch if lazy else None), search


def _write_roles(
    kind: str,
    run: Path,
    request_path: Path,
    result_path: Path,
    *,
    adapter: dict[str, Any],
    reviewer_root: Path | None,
    entrypoint: str | None,
    links: dict[str, tuple[int, str] | None],
    checkout: Path | None,
    manifest: dict[str, Any],
    review_files: set[str] | None,
    notes: list[str],
    lazy: bool = False,
    document_stage: str | None = None,
) -> tuple[list[RunRole], list[str]]:
    """Each reviewer role with its prompt written: an entrypoint's one, or the specialists plan's. Returns the roles
    and the changed files the plan leaves unreviewed. Every prompt names the `source-search` command, and a `lazy`
    snapshot's its `source-file` command too; a `document_stage` reaches the design-review specialist's."""
    script = command_script("review_source.py")
    if kind == "entrypoint":
        if reviewer_root is None or entrypoint is None:
            raise PipelineError("an entrypoint reviewer was prepared without its materialized entrypoint")
        prompt_path = run / "reviewer.prompt.md"
        atomic_write_text(
            prompt_path,
            render_entrypoint_prompt(
                read_json(request_path),
                request_path=request_path,
                reviewer=adapter["name"],
                reviewer_root=reviewer_root,
                entrypoint=entrypoint,
                result_file=result_path,
                self_check=self_check_command(run, adapter["name"]),
                links=links,
                local_checkout=checkout,
                source_commands=_source_commands(run, adapter["name"], script, lazy),
            ),
        )
        return [{"id": adapter["name"], "prompt_file": str(prompt_path), "result_file": str(result_path)}], []
    plan = build_plan(
        request_path,
        reviewer_root,
        run / "work",
        self_check=lambda identity: self_check_command(run, identity),
        snapshot=manifest,
        local_checkout=checkout,
        review_files=review_files,
        source_commands=lambda identity: _source_commands(run, identity, script, lazy),
        document_stage=document_stage,
    )
    roles: list[RunRole] = [
        {
            "id": role["id"],
            "prompt_file": role["prompt_file"],
            "result_file": role["result_file"],
            "model": role["model"],
            "effort": role["effort"],
        }
        for role in plan["roles"]
    ]
    notes.extend(plan["notes"])
    return roles, plan["uncovered_files"]


@dataclass
class _Review:
    """What a run prepares to review, read from GitHub or from a fixture."""

    selector: str
    repository: str
    number: int
    pull: dict[str, Any]  # validated by validate_canary_pull
    mode: str
    previous: ReviewRecord | None  # the record a re-review starts from
    prior: list[dict[str, Any]]  # the findings it carries
    base: dict[str, Any]  # the archive state finalize must still find
    reviewer: dict[str, Any]
    checkout: Path | None  # the configured checkout, which reviewers are kept out of
    source: Path | None  # the repository the head is snapshotted from; None for GitHub's tarball
    # Writes the diff to the path it is given and returns what prepare reads of the change.
    read_diff: Callable[[Path], ChangeRead]
    canary: bool
    fixture: dict[str, Any] | None = None  # run.json's record of a fixture canary
    prior_record: ReviewRecord | None = None  # a fixture re-review's prior record, which finalize archives first
    # Whether `source` is a throwaway repository prepare moves into the run, as a fixture's is, so that a lazy
    # snapshot's reviewers can fetch from it until finalize removes the run.
    adopt_source: bool = False
    # The repository's configured snapshot_exclude globs; a fixture has none.
    snapshot_exclude: tuple[str, ...] = ()
    # The repository's own suffixes of binary file types the review does not read; a fixture has none.
    unreviewed_suffixes: tuple[str, ...] = ()
    # The stage of a fixture's design document, which the design-review specialist's prompt names; None for a pull
    # request read from GitHub.
    document_stage: str | None = None


FIXTURE_PRIOR_FILE = "prior.json"


def prepare(
    selector: str,
    *,
    re_review: bool = False,
    scope: str | None = None,
    force: bool = False,
    canary: bool = False,
    host: str | None = None,
    inline: bool = False,
    config_path: Path | None = None,
    run_directory: Path | None = None,
    services: Services | None = None,
) -> dict[str, Any]:
    """Everything before semantic review. Returns a skip, or a ready run whose roles need reviewers.

    A re-review names its `scope` (`auto`, `full`, or `incremental`); nothing picks one for it. `host` is the
    runtime the orchestrating session says it runs in, which decides an `auto` runtime. `inline` has the orchestrating
    session work every role itself, as it does on a runtime whose reviewers do not start as subagents.
    """
    services = services or Services()
    config_path = (config_path or default_config_path()).resolve()
    config = load_config(config_path)
    repository, number = parse_pull_selector(selector)
    selector = f"{repository}#{number}"
    entry = _configured_entry(config, repository, re_review=re_review, scope=scope, force=force, canary=canary)
    pull = validate_canary_pull(services.github.get_pull(repository, number), repository=repository, number=number)
    head = pull["headRefOid"]
    mode = "re-review" if re_review else "initial"
    prior: list[dict[str, Any]] = []
    previous: ReviewRecord | None = None
    notes: list[str] = []
    # A canary records under a new, empty archive root.
    base = archive_base(None, [])
    if not canary:
        # Read before the history the review starts from: a version recorded in between then fails finalize rather
        # than slipping under the review.
        base = archive_base(*archive_head(Path(config["archive_root"]), repository, number))
        history = _review_history(
            Path(config["archive_root"]), repository, number, head, re_review=re_review, force=force, notes=notes
        )
        if history is None:
            return _skip(selector, f"head {head[:12]} is already reviewed")
        mode, previous, prior = history
    checkout = Path(entry["checkout_path"]) if entry["checkout_path"] else None
    review = _Review(
        selector=selector,
        repository=repository,
        number=number,
        pull=pull,
        mode=mode,
        previous=previous,
        prior=prior,
        base=base,
        reviewer=entry["reviewer"],
        checkout=checkout,
        source=checkout,
        read_diff=lambda diff_path: _fetch_diff(repository, number, pull, checkout, diff_path, services, notes),
        canary=canary,
        snapshot_exclude=tuple(entry["snapshot_exclude"]),
        unreviewed_suffixes=tuple(entry["unreviewed_suffixes"]),
    )
    return _prepare_run(
        review,
        scope=scope,
        host=host,
        inline=inline,
        config=config,
        config_path=config_path,
        run_directory=run_directory,
        services=services,
        notes=notes,
    )


def prepare_fixture(
    directory: Path,
    *,
    prior: Path | None = None,
    host: str | None = None,
    inline: bool = False,
    config_path: Path | None = None,
    run_directory: Path | None = None,
    services: Services | None = None,
) -> dict[str, Any]:
    """A canary of the pull request a fixture directory holds (see review_canary.py), reading nothing from GitHub.

    With `prior`, a review record of the same pull request at an earlier head, it is a re-review that carries that
    record's ledger and covers the full scope; finalize archives the prior record in the canary root first, so the
    re-review is recorded as a real one is. The suite's generic reviewer reviews a fixture, unless its pull.json names
    a `manifest_path`, which is read from the fixture's base commit as a configured repository's is.
    """
    services = services or Services()
    config_path = (config_path or default_config_path()).resolve()
    config = load_config(config_path)
    prior_value = None if prior is None else read_json(prior.resolve())
    notes: list[str] = []
    # pull.json and the prior record are validated before the throwaway repository is written.
    fixture_pull = read_fixture_pull(directory)
    record = None
    if prior_value is not None:
        record = validate_prior_record(
            prior_value, repository=fixture_pull["repository"], number=fixture_pull["number"]
        )
    with fixture_change(directory, services.git, fixture_pull) as change:
        repository, number, pull = change.name, change.pull["number"], change.pull
        selector = f"{repository}#{number}"
        review = _Review(
            selector=selector,
            repository=repository,
            number=number,
            pull=validate_canary_pull(pull, repository=repository, number=number),
            mode="initial" if record is None else "re-review",
            previous=record,
            prior=[] if record is None else carried_findings([record]),  # a fixture reads no flag store
            base=archive_base(None, []) if record is None else archive_base(1, ledger_history([record])[1]),
            reviewer=GENERIC_REVIEWER if change.manifest_path is None else fixture_reviewer(change.manifest_path),
            checkout=None,
            source=change.repository,
            read_diff=lambda diff_path: ChangeRead(
                *_write_diff(selector, PullDiff(change.diff, change.undecodable), diff_path, notes),
                change.comments,
                None,
            ),
            canary=True,
            fixture={"directory": str(directory.resolve()), "prior": None if prior is None else str(prior.resolve())},
            prior_record=record,
            adopt_source=True,
            document_stage=change.document_stage,
        )
        return _prepare_run(
            review,
            scope=None if record is None else "full",
            host=host,
            inline=inline,
            config=config,
            config_path=config_path,
            run_directory=run_directory,
            services=services,
            notes=notes,
        )


def _prepare_run(
    review: _Review,
    *,
    scope: str | None,
    host: str | None,
    inline: bool,
    config: dict[str, Any],
    config_path: Path,
    run_directory: Path | None,
    services: Services,
    notes: list[str],
) -> dict[str, Any]:
    """Write the run of a review: its diff, snapshot, reviewer, request, prompts, and run.json.

    Its git runs through a PrepareGitRunner of its own, so an identical query it repeats is asked once until a command
    that is not a read, and nothing it was told outlives it."""
    services = replace(services, git=PrepareGitRunner(services.git))
    repository, number, pull, checkout = review.repository, review.number, review.pull, review.checkout
    if checkout is not None and _inside(Path.cwd(), checkout):
        notes.append(
            f"This session runs inside {checkout}, so its CLAUDE.md files and project memory load into every "
            "reviewer on every turn; start review sessions from a directory outside the checkout."
        )
    runtime = services.resolve_runtime(config["runtime"], host)
    created = run_directory is None
    run = (Path(tempfile.mkdtemp(prefix=RUN_PREFIX)) if run_directory is None else run_directory).resolve()
    if run.exists() and any(run.iterdir()):
        raise PipelineError(f"Run directory must be empty: {run}")
    try:
        run.mkdir(parents=True, exist_ok=True)
        diff_path = run / "diff.patch"
        parsed, changed, gaps, patches, comments, fallback = review.read_diff(diff_path)
        source = run / "source"
        head = pull["headRefOid"]
        repository_path = review.source
        if repository_path is not None and review.adopt_source:
            repository_path = Path(shutil.move(repository_path, run / SOURCE_REPOSITORY))
        links, snapshot, manifest = _snapshot(
            repository_path,
            repository,
            number,
            head,
            source,
            parsed,
            changed,
            services,
            notes,
            review.snapshot_exclude,
            review.unreviewed_suffixes,
            fallback,
        )
        kind, adapter, reviewer_root, entrypoint, dispatch = _materialize_reviewer(
            review.reviewer,
            # The repository the head is snapshotted from: the configured checkout, or a fixture's repository, which
            # holds the base commit a fixture's reviewer manifest is read from.
            checkout=repository_path,
            pull=pull,
            mode=review.mode,
            runtime=runtime,
            inline=inline,
            run=run,
            config_path=config_path,
            repository=repository,
            services=services,
            notes=notes,
        )
        reads = _snapshot_reads(manifest, dispatch, kind, reviewer_root, list(parsed), renamed_from(parsed))
        if repository_path is not None and reads != []:
            manifest = _retake_snapshot(
                repository_path,
                repository,
                head,
                source,
                changed,
                services,
                snapshot,
                review.snapshot_exclude,
                reads,
                review.unreviewed_suffixes,
            )
        documents, patches = _document_diffs(
            diff_path, parsed, manifest, source, _base_reader(repository_path, repository, pull, services), notes
        ) or ({}, patches)
        lazy = SNAPSHOT_FETCHABLE in manifest
        # The Copilot CLI host starts in a process of its own, so it checks the snapshot against this stamp.
        stamp = (
            stamp_source_snapshot(source, expected_repository=repository, expected_commit=head)
            if dispatch == "copilot-host"
            else None
        )
        review_files: set[str] | None = None
        scope_record: Scope | None = None
        if review.previous is not None and scope is not None:  # a re-review, which always names its scope
            scope_record, review_files = choose_scope(
                scope, review.previous, patches, thresholds=config["re_review_scope"], entrypoint=kind == "entrypoint"
            )
            notes.append(f"Scope {describe_scope(scope_record)}.")
        request_path = run / "request.json"
        writing = services.timer()
        _write_request(
            request_path,
            mode=review.mode,
            repository=repository,
            number=number,
            pull=pull,
            diff_path=diff_path,
            source=source,
            manifest=manifest,
            prior=review.prior,
            comments=comments,
            gaps=gaps,
            unreviewed_suffixes=review.unreviewed_suffixes,
        )
        fixture = review.fixture
        if fixture is not None:
            fixture = {**fixture, "prior_record": None}
            if review.prior_record is not None:
                fixture["prior_record"] = str(run / FIXTURE_PRIOR_FILE)
                atomic_write_json(run / FIXTURE_PRIOR_FILE, review.prior_record)
        result_path = run / "result.json"
        roles, uncovered_files = _write_roles(
            kind,
            run,
            request_path,
            result_path,
            adapter=adapter,
            reviewer_root=reviewer_root,
            entrypoint=entrypoint,
            links=links,
            checkout=checkout,
            manifest=manifest,
            review_files=review_files,
            notes=notes,
            lazy=lazy,
            document_stage=review.document_stage,
        )
        snapshot["seconds"] = snapshot_seconds({**snapshot["seconds"], "prompts": services.timer() - writing})
        state: RunState = {
            "schema_version": RUN_SCHEMA_VERSION,
            "selector": review.selector,
            "mode": review.mode,
            "canary": review.canary,
            # A fixture canary's directory, the prior record it was given, and the run's copy of that record.
            "fixture": fixture,
            "config_path": str(config_path),
            "host": host,
            "runtime": runtime,
            "dispatch": dispatch,
            "kind": kind,
            "request_path": str(request_path),
            "reviewer_root": str(reviewer_root) if reviewer_root else None,
            "result_path": str(result_path),
            "adapter": adapter,
            "roles": roles,
            "attempts": {role["id"]: 0 for role in roles},
            # An inline role is handed out, and timed, by next-role, one at a time.
            "dispatched_at": {} if dispatch == "inline" else {role["id"]: time.time() for role in roles},
            "snapshot": snapshot,
            # The repository a lazy snapshot's reviewers fetch files from, by blob id; None for a whole snapshot.
            "source_repository": str(repository_path) if lazy and repository_path is not None else None,
            # The snapshot's stamp, taken before anything untrusted ran, for the Copilot CLI host; None otherwise.
            "snapshot_stamp": stamp,
            # Each role's distinct snapshot files read and their bytes, as check reduces the guard's read log.
            "reads": {},
            "notes": notes,
            "patches": patches,
            "scope": scope_record,
            "uncovered_files": uncovered_files,
            "archive_base": review.base,
            # Each line's place in each changed document whose extracted text the reviewers read.
            "documents": documents,
        }
        _write_run(run, state)
    except BaseException:
        if created:
            remove_run(run, ignore_errors=True)
        raise
    return {"status": "ready", "run": run, **state}


def wait_seconds(value: str) -> int:
    seconds = int(value)
    if not 1 <= seconds <= MAX_WAIT_SECONDS:
        raise argparse.ArgumentTypeError(f"must be from 1 to {MAX_WAIT_SECONDS} seconds")
    return seconds


def positive_integer(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def remove_recorded_run(run: Path, recorded: dict[str, str], sleep: Callable[[float], None]) -> str | None:
    """Remove a run finalize recorded, retrying while another process holds one of its files.

    The run is marked recorded first, so a run folder left by a crash or a held file is never reported unfinalized,
    and marked again if removal deleted the mark but not the folder. Returns a note when the folder survives.
    """
    with contextlib.suppress(PersistenceError):
        atomic_write_json(run / RECORDED_FILE, recorded)
    error: OSError | None = None
    for delay in (0.0, *RUN_REMOVAL_DELAYS):
        if delay:
            sleep(delay)
        try:
            remove_run(run)
        except FileNotFoundError:
            return None
        except OSError as exc:
            error = exc
        else:
            return None
    try:
        atomic_write_json(run / RECORDED_FILE, recorded)
    except PersistenceError as exc:
        return f"The recorded run {run} could not be removed ({error}) or marked recorded ({exc}); delete it."
    return f"The recorded run {run} could not be removed ({error}); it is marked recorded, so delete it later."


def finalize(run: Path, services: Services | None = None) -> dict[str, Any]:
    """Commit a validated review. Nothing is archived unless every reviewer result is valid and the archive still
    holds the version prepare judged the review against."""
    services = services or Services()
    run = run.resolve()
    if (run / RECORDED_FILE).is_file():
        raise PipelineError(f"{run} is already recorded")
    state = load_run(run)
    if run_dispatch(state) == "inline":
        verify_seal(run, state)
    errors = role_errors(run, state)
    errors.update(unguarded_roles(run, state, [role["id"] for role in state["roles"] if role["id"] not in errors]))
    if errors:
        raise PipelineError("Reviewer results are invalid: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
    seconds = reviewer_seconds(state)
    if count_reads(run, state, [role["id"] for role in state["roles"]]):  # any log check did not reach
        atomic_write_json(run / RUN_FILE, state)
    request_path = Path(state["request_path"])
    result_path = Path(state["result_path"])
    if state["kind"] != "entrypoint":
        plan = read_json(run / "work" / "plan.json", maximum_bytes=64 * 1024 * 1024)
        result = assemble(plan, read_json(request_path))
        if result["status"] != "complete":
            raise PipelineError(result["summary"])
        atomic_write_json(result_path, result)
        roles = plan["roles"]
        models = reviewer_models(plan)
        summaries = role_summaries(plan)
    else:
        result = read_json(result_path)
        request = read_json(request_path)
        roles = [
            {
                "id": state["adapter"]["name"],
                "category": "Repository reviewer",
                "dispositions_only": False,
                "files": list(parse_unified_diff(read_diff(Path(request["diff_path"])))),
            }
        ]
        models = None  # the repository entrypoint protocol has no model field
        summaries = {}
    reviewers = reviewer_summaries(
        roles,
        result["findings"],
        state["attempts"],
        single=state["kind"] == "entrypoint",
        seconds=seconds,
        models=models,
        own_summaries=summaries,
        reads=state.get("reads"),  # absent from runs prepared before reads were counted
    )
    config = load_config(Path(state["config_path"]))
    canary_root = Path(tempfile.mkdtemp(prefix="code-review-canary-")).resolve() if state["canary"] else None
    prior_record = (state.get("fixture") or {}).get("prior_record")  # absent from runs prepared before fixtures
    if canary_root is not None and prior_record:
        # A fixture re-review's canary root first holds the review it starts from, as a real archive would.
        prior = validate_record(read_json(Path(prior_record)))
        commit_record(
            canary_root,
            prior["repository"],
            prior["pull_request"]["number"],
            prior,
            expected_latest_version=None,
            model_names=config["model_names"],
        )
    json_path, markdown_path, record = commit_adapter_result(
        request_path=request_path,
        result_path=result_path,
        archive_root=canary_root or Path(config["archive_root"]),
        local_mirror_root=None
        if canary_root
        else (Path(config["local_mirror_root"]) if config["local_mirror_root"] else None),
        policy=config["verdict_policy"],
        adapter=state["adapter"],
        base=state.get("archive_base"),  # a run prepared before it was recorded fails rather than guess
        reviewers=reviewers,
        require_comment_dispositions=state["kind"] != "entrypoint",
        patches=state.get("patches"),  # absent from runs prepared before patches were recorded
        scope=state.get("scope"),
        uncovered_files=state.get("uncovered_files"),  # absent from runs prepared before they were recorded
        model_names=config["model_names"],
        flags=[] if canary_root else load_store(default_flags_path())["flags"],  # a canary reads no flag store
        dispatch=run_dispatch(state),
        snapshot=state.get("snapshot"),  # absent from runs prepared before snapshots were measured
        documents=state.get("documents"),  # absent from runs prepared before documents were extracted
    )
    ledger = ledger_summary(record["ledger"], record["review"]["version"])
    notes = list(state["notes"])
    left = remove_recorded_run(
        run, {"selector": state["selector"], "json": str(json_path), "markdown": str(markdown_path)}, services.sleep
    )
    if left is not None:
        notes.append(left)
    return {
        "selector": state["selector"],
        "json": json_path,
        "markdown": markdown_path,
        "verdict": record["review"]["verdict"],
        # The findings this review raised, and the ledger's open and addressed entries after it, as its report's
        # verdict counts them: an incremental re-review seeks new findings only in the files that changed.
        "new": len(record["findings"]),
        "open": sum(ledger["open"].values()),
        "addressed": ledger["addressed"],
        "canary_root": canary_root,
        "hashes": {json_path: _sha256(json_path), markdown_path: _sha256(markdown_path)} if canary_root else {},
        "stats": stats_lines(record["review"]) if canary_root else [],
        "notes": notes,
    }


def _first_duplicate(selectors: list[str]) -> str | None:
    """The first pull request named twice; a malformed selector is left for prepare to report."""
    seen: set[tuple[str, int]] = set()
    for selector in selectors:
        try:
            key = parse_pull_selector(selector)
        except EXPECTED_ERRORS:
            continue
        if key in seen:
            return selector
        seen.add(key)
    return None


def _add_runs(parser: argparse.ArgumentParser) -> None:
    """The runs a command works on: run directories, or the run list prepare printed, never both."""
    runs = parser.add_mutually_exclusive_group(required=True)
    runs.add_argument("--run", action="append", type=Path, dest="runs", help="prepared run directory; repeatable")
    runs.add_argument("--runs", type=Path, dest="run_list", help="the run list prepare printed")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="defaults to CODE_REVIEW_CONFIG or the standard config path")
    commands = parser.add_subparsers(dest="command", required=True)
    enumerate_parser = commands.add_parser("enumerate")
    scope = enumerate_parser.add_mutually_exclusive_group()
    scope.add_argument("--repository", action="append", dest="repositories")
    scope.add_argument("--repository-set")
    enumerate_parser.add_argument("--force", action="store_true")
    enumerate_parser.add_argument(
        "--batch-size",
        type=positive_integer,
        help="the most pull requests each repository's batch takes; overrides its configured batch_size",
    )
    enumerate_parser.add_argument("--output", type=Path, help="batch file; defaults to a new temporary directory")
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument(
        "--pull", action="append", default=[], dest="pulls", help="owner/repo#number to review; repeatable"
    )
    prepare_parser.add_argument(
        "--re-review",
        action="append",
        nargs="?",
        const="",
        default=[],
        dest="re_reviews",
        help="owner/repo#number to re-review; repeatable. Bare, with --fixture: re-review the fixture against --prior",
    )
    prepare_parser.add_argument(
        "--scope", choices=RE_REVIEW_SCOPES, help="how much each --re-review covers; required with --re-review"
    )
    prepare_parser.add_argument("--force", action="store_true")
    prepare_parser.add_argument(
        "--canary",
        action="append",
        nargs="?",
        const="",
        default=[],
        dest="canaries",
        help="owner/repo#number to review in isolation; repeatable. Bare: each --pull or --fixture is a canary",
    )
    prepare_parser.add_argument(
        "--fixture",
        action="append",
        type=Path,
        default=[],
        dest="fixtures",
        help="with --canary: a fixture directory to review in place of a pull request; repeatable",
    )
    prepare_parser.add_argument(
        "--prior", type=Path, help="with --fixture and --re-review: the review record the re-review starts from"
    )
    prepare_parser.add_argument(
        "--batch", type=Path, help="review every pull request of the batch file enumerate wrote"
    )
    prepare_parser.add_argument(
        "--runs", type=Path, dest="run_list", help="prepare the next group of the run list an earlier prepare printed"
    )
    prepare_parser.add_argument(
        "--host",
        choices=sorted(RUNTIME_CAPABILITIES),
        help="the runtime this session runs in; decides an auto runtime before PATH does",
    )
    prepare_parser.add_argument(
        "--inline", action="store_true", help="have this session work every reviewer role itself, one at a time"
    )
    commands.add_parser("dispatch").add_argument("--run", required=True, type=Path)
    commands.add_parser("next-role").add_argument("--run", required=True, type=Path)
    wait_parser = commands.add_parser("wait")
    wait_parser.add_argument("--run", required=True, type=Path)
    wait_parser.add_argument(
        "--timeout", required=True, type=wait_seconds, help=f"seconds to wait at most, from 1 to {MAX_WAIT_SECONDS}"
    )
    # Run only by dispatch, as the detached host process; never by an orchestrator.
    host_parser = commands.add_parser("host", help=argparse.SUPPRESS)
    host_parser.add_argument("--run", required=True, type=Path)
    host_parser.add_argument("--token", required=True)
    _add_runs(commands.add_parser("workflow"))
    wait_reviewers_parser = commands.add_parser("wait-reviewers")
    _add_runs(wait_reviewers_parser)
    wait_reviewers_parser.add_argument(
        "--timeout", required=True, type=wait_seconds, help=f"seconds to wait at most, from 1 to {MAX_WAIT_SECONDS}"
    )
    validate_result_parser = commands.add_parser("validate-result")
    validate_result_parser.add_argument("--run", required=True, type=Path)
    validate_result_parser.add_argument("--role", required=True)
    for name in ("check", "finalize", "unfinalized"):
        _add_runs(commands.add_parser(name))
    commands.add_parser("advance").add_argument("--batch", required=True, type=Path)
    inspect_parser = commands.add_parser("inspect-reviewer")
    inspect_parser.add_argument("--repository", required=True)
    inspect_parser.add_argument(
        "--ref", help="commit to read the reviewer from; defaults to the trusted ref or origin's default branch"
    )
    validate_parser = commands.add_parser("validate-reviewer")
    validate_parser.add_argument("--repository", required=True)
    validate_parser.add_argument(
        "--pull", action="append", type=int, default=[], dest="pulls", help="pull request number to route; repeatable"
    )
    validate_parser.add_argument("--ref", help="commit to read the reviewer from when no --pull is given")
    validate_parser.add_argument(
        "--host",
        choices=sorted(RUNTIME_CAPABILITIES),
        help="the runtime prepare would run in; decides an auto runtime before PATH does, as prepare's --host does",
    )
    return parser


def _check_prepare(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Refuse a prepare call argparse cannot judge alone, as a usage error that exits 2 before any work."""
    given = {
        "--pull": bool(args.pulls),
        "--re-review": bool(args.re_reviews),
        "--scope": args.scope is not None,
        "--force": args.force,
        "--canary": bool(args.canaries),
        "--fixture": bool(args.fixtures),
        "--prior": args.prior is not None,
        "--batch": args.batch is not None,
        "--host": args.host is not None,
        "--inline": args.inline,
    }
    if args.run_list is not None:
        # The run list holds the arguments the first call was given; every group takes the same.
        if any(given.values()):
            parser.error("--runs takes no other option; the run list holds them")
        return
    if args.fixtures:
        _check_prepare_fixtures(parser, args)
        return
    if args.prior is not None or "" in args.re_reviews:
        parser.error("--prior and a bare --re-review are taken only with --fixture")
    canary_selectors = [selector for selector in args.canaries if selector]
    if args.batch is not None:
        if args.pulls or args.re_reviews or args.scope or args.canaries:
            parser.error("--batch takes no other selector, no --scope, and no --canary")
        return
    selectors = [*args.pulls, *args.re_reviews, *canary_selectors]
    if not selectors:
        parser.error("prepare takes at least one --pull, --re-review, --canary selector, --batch, or --runs")
    # A canary is an initial review of each pull request, written under its own temporary root.
    if args.canaries and (args.re_reviews or args.force):
        parser.error("--canary takes only --pull selectors and no --force")
    # The person running the review chooses the scope; nothing picks one for them.
    if bool(args.re_reviews) != (args.scope is not None):
        parser.error("--scope is required with --re-review and taken only with it")
    # Two runs of one pull request would race to record the same review version.
    duplicate = _first_duplicate(selectors)
    if duplicate is not None:
        parser.error(f"{duplicate} is named more than once")


def _check_prepare_fixtures(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """A fixture is one canary, an initial review or, with its prior record, a full re-review; several fixtures are
    initial reviews."""
    selectors = [selector for selector in args.canaries if selector]
    if not args.canaries or selectors or args.pulls or args.scope or args.force or args.batch or any(args.re_reviews):
        parser.error("--fixture takes --canary, and a bare --re-review with --prior, and no other selector")
    if args.re_reviews not in ([], [""]) or bool(args.re_reviews) != (args.prior is not None):
        parser.error("--prior takes one bare --re-review, and a bare --re-review takes --prior")
    if args.prior is not None and len(args.fixtures) > 1:
        parser.error("--prior takes one --fixture")


def _run_inspect_reviewer(args: argparse.Namespace, services: Services | None) -> int:
    print("\n".join(inspect_reviewer(args.repository, ref=args.ref, config_path=args.config, services=services)))
    return 0


def _run_validate_reviewer(args: argparse.Namespace, services: Services | None) -> int:
    lines = validate_reviewer(
        args.repository, pulls=args.pulls, ref=args.ref, host=args.host, config_path=args.config, services=services
    )
    print("\n".join(lines))
    return 0


def _run_enumerate(args: argparse.Namespace, services: Services | None) -> int:
    output = working_path(args.output, "review-prs-batch-", "batch.json")
    batch = enumerate_batch(
        output,
        repositories=args.repositories,
        repository_set=args.repository_set,
        force=args.force,
        batch_size=args.batch_size,
        config_path=args.config,
        services=services,
    )
    failed = False
    for repository, entry in batch["repositories"].items():
        if not entry["complete"]:
            print(f"REPOSITORY_FAILED {repository} {entry['error']}")
            failed = True
        else:
            found = entry["listing"]
            print(
                f"LISTED {repository} scan={found['scan']} pages={found['pages']} pulls={found['pulls']} "
                f"read={found['read']} candidates={len(entry['eligible'])} deferred={len(entry['deferred'])}"
            )
        for pull in entry["eligible"]:
            print(f"PULL {repository}#{pull['number']}")
    print(f"BATCH {output}")
    # A repository that failed to list is a batch item's failure; the others are still listed and reviewable.
    return 1 if failed else 0


def _publish_prepared(result: dict[str, Any], failure_label: str | Path) -> bool:
    """Publish only after marking succeeds; a failed unpublished run has no reviewer work to keep."""
    if result["dispatch"] != "inline":
        try:
            # Other pulls may have taken longer; every reviewer starts from publication, not preparation.
            mark_dispatched(result["run"])
        except EXPECTED_ERRORS as exc:
            reason = str(exc)
            try:
                remove_run(result["run"])
            except OSError as cleanup_error:
                reason += (
                    f"; unpublished run {result['run']} could not be removed ({cleanup_error}); inspect or delete it."
                )
            print(f"FAILED {failure_label} {reason}")
            return False
    _print_ready(result)
    return True


def _first_run_list(args: argparse.Namespace) -> dict[str, Any]:
    """The run list of a first prepare call: every pull request or fixture it names, in order, and its options."""
    if args.fixtures:
        pending = [{"kind": "fixture", "target": str(directory)} for directory in args.fixtures]
    elif args.batch is not None:
        pending = [{"kind": "pull", "target": selector} for selector in batch_selectors(args.batch)]
    else:
        pending = [
            *({"kind": "pull", "target": selector} for selector in args.pulls),
            *({"kind": "pull", "target": selector} for selector in args.canaries if selector),
            *({"kind": "re-review", "target": selector} for selector in args.re_reviews),
        ]
    options = {
        "config": None if args.config is None else str(args.config),
        "host": args.host,
        "inline": args.inline,
        "force": args.force,
        "canary": bool(args.canaries),
        "scope": args.scope,
        "prior": None if args.prior is None else str(args.prior),
    }
    return new_run_list(pending, options)


def _prepare_item(item: dict[str, str], options: dict[str, Any], services: Services | None) -> dict[str, Any]:
    config_path = None if options["config"] is None else Path(options["config"])
    if item["kind"] == "fixture":
        return prepare_fixture(
            Path(item["target"]),
            prior=None if options["prior"] is None else Path(options["prior"]),
            host=options["host"],
            inline=options["inline"],
            config_path=config_path,
            services=services,
        )
    re_review = item["kind"] == "re-review"
    return prepare(
        item["target"],
        re_review=re_review,
        scope=options["scope"] if re_review else None,
        force=options["force"],
        canary=options["canary"],
        host=options["host"],
        inline=options["inline"],
        config_path=config_path,
        services=services,
    )


def _run_prepare(args: argparse.Namespace, services: Services | None) -> int:
    """Prepare the next group of a run list, a new one from the arguments or the one --runs names, and record every
    run it publishes there. The list is written before the group is prepared, so it never names a group twice."""
    if args.run_list is None:
        runs = _first_run_list(args)
        path = working_path(None, RUN_LIST_PREFIX, RUN_LIST_FILE)
    else:
        path = args.run_list
        runs = load_run_list(path)
    group, runs["pending"] = runs["pending"][:MAX_PREPARE_PULLS], runs["pending"][MAX_PREPARE_PULLS:]
    atomic_write_json(path, runs)
    failed = False
    outcomes = map_in_order(lambda item: _prepare_item(item, runs["options"], services), group, catch=EXPECTED_ERRORS)
    for item, (result, error) in zip(group, outcomes, strict=True):
        if error is not None or result is None:  # prepare returns a result whenever it raises nothing
            print(f"FAILED {item['target']} {error}")
            failed = True
        elif result["status"] == "skip":
            print(f"SKIP {result['selector']} {result['reason']}")
        elif _publish_prepared(result, item["target"]):
            runs["runs"].append({"run": str(result["run"]), "selector": result["selector"], "status": "published"})
        else:
            failed = True
    atomic_write_json(path, runs)
    print(f"RUNS {path} pending={len(runs['pending'])}")
    return 1 if failed else 0


def _run_next_role(args: argparse.Namespace, services: Services | None) -> int:
    selector, role = next_role(args.run, services or Services())
    print(f"INLINE_ROLE {selector} {role['id']} {role['prompt_file']}" if role else f"INLINE_DONE {selector}")
    return 0


def _run_validate_result(args: argparse.Namespace, services: Services | None) -> int:
    invalid = validate_result(args.run, args.role)
    print(f"INVALID {invalid}" if invalid else "VALID")
    return 1 if invalid else 0


def _run_entries(
    args: argparse.Namespace, *statuses: str
) -> tuple[dict[str, Any] | None, list[tuple[Path, dict[str, str]]]]:
    """The runs a command works on, each with its run list entry: every --run, or the listed runs in `statuses`. A
    --run has an entry of its own that nothing keeps, so a command sets a status the same way for both."""
    if args.run_list is None:
        return None, [(run, {"status": "published"}) for run in args.runs]
    runs = load_run_list(args.run_list)
    return runs, [(Path(entry["run"]), entry) for entry in listed_runs(runs, *statuses)]


def _subagent_runs(args: argparse.Namespace) -> list[Path]:
    """The --run directories, or the published listed runs whose roles start as subagents."""
    _, entries = _run_entries(args, "published")
    if args.run_list is None:
        return [run for run, _ in entries]
    return [run for run, _ in entries if worked_by_subagents(run)]


def _save_run_list(args: argparse.Namespace, runs: dict[str, Any] | None) -> None:
    if runs is not None:
        atomic_write_json(args.run_list, runs)


def _run_workflow(args: argparse.Namespace, services: Services | None) -> int:
    runs = _subagent_runs(args)
    if not runs:
        # Every listed run is on the Copilot CLI host, inline, or already checked: no Workflow to start.
        print("WORKFLOW none")
        return 0
    script, text, count = workflow_script(runs)
    print(f"WORKFLOW {script} roles={count}")
    print(SCRIPT_BEGIN)
    print(text, end="")
    print(SCRIPT_END)
    return 0


def _run_wait_reviewers(args: argparse.Namespace, services: Services | None) -> int:
    progress, failures = wait_for_reviewers(_subagent_runs(args), args.timeout, services or Services())
    for run, reason in failures.items():
        print(f"FAILED {run} {reason}")
    for selector, roles in progress:
        if all(status == "ready" for status, _ in roles.values()):
            print(f"READY {selector}")
        for identity, (status, elapsed) in roles.items():
            if status != "ready":
                print(f"{status.upper()} {selector} {identity} {elapsed}s")
    # RUNNING and OVERDUE are states the skill acts on by polling again or checking, not failures.
    return 1 if failures else 0


def _run_unfinalized(args: argparse.Namespace, services: Services | None) -> int:
    pending = failed = False
    runs, entries = _run_entries(args, *RUN_STATUSES)
    for run, _ in entries:
        try:
            selector = unfinalized_selector(run)
        except EXPECTED_ERRORS as exc:
            print(f"FAILED {run} {exc}")
            failed = True
            continue
        if selector is not None:
            print(f"UNFINALIZED {selector} {run.resolve()}")
            pending = True
    # A pull request or fixture no prepare reached was never reviewed: its failure as much as an unfinalized run.
    for item in [] if runs is None else runs["pending"]:
        print(f"UNPREPARED {item['target']}")
        pending = True
    if not pending and not failed:
        print("ALL_FINALIZED")
    return 1 if pending or failed else 0


def _run_dispatch(args: argparse.Namespace, services: Services | None) -> int:
    print(f"STARTED {dispatch_copilot(args.run, services or Services())}")
    return 0


def _run_host(args: argparse.Namespace, services: Services | None) -> int:
    print(f"OUTCOME {run_host(args.run, args.token, services or Services())}")
    return 0


def _run_wait(args: argparse.Namespace, services: Services | None) -> int:
    dispatched, elapsed = wait_for_host(args.run, args.timeout, services or Services())
    if dispatched is None:
        print(f"RUNNING {elapsed}s")  # a state the skill polls again
        return 0
    print(f"DISPATCHED {dispatched}")
    return 0


def _run_check(args: argparse.Namespace, services: Services | None) -> int:
    retry = failed = False
    # A listed run check accepted or failed is not checked again; one with a RETRY or RUNNING line is.
    runs, entries = _run_entries(args, "published")
    for run, entry in entries:
        try:
            outcome = check_run(run, services)
        except EXPECTED_ERRORS as exc:
            print(f"FAILED {run} {exc}")
            entry["status"] = "failed"
            failed = True
            continue
        selector = outcome["selector"]
        for role in outcome["retry"]:
            print(f"RETRY {selector} {role['id']} {role['prompt_file']} {outcome['errors'][role['id']]}")
            _print_settings(selector, role, outcome["runtime"])
        for identity, error in outcome["failed"].items():
            print(f"FAILED {selector} {identity} {error}")
        for identity, elapsed in outcome["running"].items():
            print(f"RUNNING {selector} {identity} {elapsed}s")
        if outcome["failed"]:
            entry["status"] = "failed"
        elif not outcome["errors"] and not outcome["running"]:
            print(f"ALL_VALID {selector}")
            entry["status"] = "valid"
        retry = retry or bool(outcome["retry"])
        failed = failed or bool(outcome["failed"])
    _save_run_list(args, runs)
    # A RETRY or FAILED line is for the skill to act on; RUNNING alone is a state it polls again.
    return 1 if failed or retry else 0


def _run_finalize(args: argparse.Namespace, services: Services | None) -> int:
    failed = False
    # From a run list, only the runs check accepted.
    runs, entries = _run_entries(args, "valid")
    for run, entry in entries:
        try:
            result = finalize(run, services)
        except EXPECTED_ERRORS as exc:
            print(f"FAILED {run} {exc}")
            entry["status"] = "failed"
            failed = True
            continue
        entry["status"] = "recorded"
        for note in result["notes"]:
            print(f"NOTE {result['selector']} {note}")
        if result["canary_root"]:
            print(f"CANARY {result['selector']} {result['canary_root']}")
            for path, digest in result["hashes"].items():
                print(f"SHA256 {digest} {path}")
            for line in result["stats"]:
                print(f"STATS {result['selector']} {line}")
        print(
            f"RECORDED {result['selector']} verdict={result['verdict']} new={result['new']} open={result['open']} "
            f"addressed={result['addressed']} {result['markdown']}"
        )
    _save_run_list(args, runs)
    return 1 if failed else 0


def _run_advance(args: argparse.Namespace, services: Services | None) -> int:
    for repository, (old, new) in advance_watermarks(args.batch, config_path=args.config, services=services).items():
        print(
            f"WATERMARK {repository} {old} -> {new}" if new else f"WATERMARK {repository} unchanged: enumeration failed"
        )
    return 0


# One handler per subcommand. Each prints its lines and returns the exit code; an expected error it does not report
# itself reaches _translate_errors.
COMMANDS: dict[str, Callable[[argparse.Namespace, Services | None], int]] = {
    "enumerate": _run_enumerate,
    "prepare": _run_prepare,
    "dispatch": _run_dispatch,
    "next-role": _run_next_role,
    "wait": _run_wait,
    "host": _run_host,
    "workflow": _run_workflow,
    "wait-reviewers": _run_wait_reviewers,
    "validate-result": _run_validate_result,
    "check": _run_check,
    "finalize": _run_finalize,
    "unfinalized": _run_unfinalized,
    "advance": _run_advance,
    "inspect-reviewer": _run_inspect_reviewer,
    "validate-reviewer": _run_validate_reviewer,
}


def _translate_errors(
    handler: Callable[[argparse.Namespace, Services | None], int], args: argparse.Namespace, services: Services | None
) -> int:
    """Run a handler, ending an expected error as one FAILED line and exit 1; any other error is a bug and escapes."""
    try:
        return handler(args, services)
    except EXPECTED_ERRORS as exc:
        print(f"FAILED {exc}")
        return 1


def main(arguments: list[str] | None = None, services: Services | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(arguments)
    if args.command == "prepare":
        _check_prepare(parser, args)
    return _translate_errors(COMMANDS[args.command], args, services)


if __name__ == "__main__":
    run_main(main)

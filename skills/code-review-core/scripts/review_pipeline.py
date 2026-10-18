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
import functools
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from console import run_main
from executable_locator import PYTHON
from git_client import Runner, subprocess_runner
from process_identity import ProcessStatus, process_status
from review_analyzers import reads_settings
from review_archive import ArchiveError, archive_head, commit_record, pull_records
from review_canary import FixtureError, fixture_change, validate_prior_record
from review_config import (
    ConfigurationError,
    default_config_path,
    load_config,
    resolve_repositories,
    validate_repository_identity,
)
from review_documents import (
    EXTRACTED_WHEN_TEXT,
    ExtractionRefused,
    document_diff,
    document_format,
    extract,
    locations,
)
from review_flags import FlagError, default_flags_path, load_store
from review_github import GitHubClient, GitHubError
from review_guard import RUN_PREFIX, read_log
from review_hosts import (
    HostSuperseded,
    claim_holds,
    host_lock,
    host_log_path,
    host_state,
    new_claim,
    record_host_process,
    run_copilot,
    staging_path,
    write_outcome,
)
from review_hosts import Runner as CopilotRunner
from review_hosts import subprocess_runner as copilot_subprocess_runner
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
    ReviewOperationError,
    archive_base,
    commit_adapter_result,
    latest_reviewed_heads,
    parse_pull_selector,
    prior_severities,
    recorded_watermark,
    reviewed_head,
    safe_watermark,
    select_eligible_pulls,
    split_batch,
    validate_canary_pull,
    validate_pull,
)
from review_process import start_detached
from review_records import (
    RE_REVIEW_SCOPES,
    RecordError,
    carried_findings,
    describe_scope,
    ledger_history,
    snapshot_seconds,
    source_parts,
    validate_adapter_result,
    validate_record,
)
from review_reviewers import (
    MissingSkillError,
    ResolvedReviewer,
    inspect_configured_skill,
    manifest_location,
    repository_files,
    resolve_reviewer,
)
from review_runtime import (
    MAX_CHANGED_FILE_BYTES,
    MAX_SOURCE_SNAPSHOT_BYTES,
    MODEL_ALIAS_RUNTIMES,
    PULL_REQUEST_BODY_CHARACTERS,
    PULL_REQUEST_BODY_FILE,
    RUNTIME_CAPABILITIES,
    SNAPSHOT_EXTRACTED,
    SNAPSHOT_FETCHABLE,
    SOURCE_SNAPSHOT_MANIFEST,
    PrepareGitRunner,
    RuntimeContractError,
    blob_matches,
    build_adapter_request,
    choose_dispatch,
    declared_reviewer_files,
    git_in,
    github_tarball_fetcher,
    given_body,
    glob_matcher,
    held_path,
    materialize_reviewer,
    materialize_source_snapshot,
    materialize_source_snapshot_from_github,
    measure_source_snapshot,
    negotiate_capabilities,
    read_blob,
    resolve_reviewer_commit,
    resolve_runtime,
    snapshot_bytes,
    stamp_source_snapshot,
    verify_checkout_remote,
    write_adapter_request,
)
from review_source import source_commands
from review_specialists import (
    SpecialistError,
    assemble,
    build_plan,
    check,
    condition_reads,
    diff_block_sides,
    evaluate_condition,
    load_materialized_manifest,
    parse_unified_diff,
    patch_fingerprints,
    renamed_from,
    render_entrypoint_prompt,
    reviewer_models,
    route,
    specialist_model,
    split_unified_diff,
    symbolic_links,
    uncovered,
    unreviewed,
)
from review_state import StateError, default_state_path, load_state, update_state, utc_today
from skill_roots import skills_directory_holding
from typed_fields import FieldError, FieldReader

RUN_SCHEMA_VERSION = 1
# Version 2 records each repository's deferred pull requests, which hold its watermark back.
BATCH_SCHEMA_VERSION = 2
RUN_LIST_SCHEMA_VERSION = 1
RUN_FILE = "run.json"
# The run list prepare writes in a new temporary directory: the pull requests or fixtures it has still to prepare,
# and every run it published, which every later command takes as --runs.
RUN_LIST_FILE = "runs.json"
RUN_LIST_PREFIX = "review-prs-runs-"
# A listed run is published by prepare, valid once check accepts it, failed once check or finalize fails it, and
# recorded once finalize records it.
RUN_STATUSES = ("published", "valid", "failed", "recorded")
# What a run list has still to prepare: a pull request to review or re-review, or a fixture directory.
PENDING_KINDS = ("pull", "re-review", "fixture")
# A fixture's throwaway repository, moved into its run, which a lazy snapshot's reviewers fetch from.
SOURCE_REPOSITORY = "repository"
# Written in a run once finalize records it, so a run folder that survives its removal is not reported unfinalized.
RECORDED_FILE = "recorded.json"
# Seconds between finalize's attempts to remove a recorded run whose files another process, such as antivirus or the
# exiting Copilot CLI host, still holds open.
RUN_REMOVAL_DELAYS = (0.25, 0.5, 1.0, 2.0)
# Pull requests or fixtures one prepare call prepares, the rest staying in its run list: it bounds the call's duration
# and the reviewers started together.
MAX_PREPARE_PULLS = 4
MAX_RETRIES = 1
# A reviewer runs this on its own result before replying; check stays authoritative. Python is named as
# review-prs's rendered grant names it, by the platform's command.
SELF_CHECK_COMMAND = PYTHON + ' -B "{script}" validate-result --run "{run}" --role "{role}"'


def remove_run(run: Path, *, ignore_errors: bool = False) -> None:
    """Remove a run folder, its fixture repository included, whose object files git leaves read-only, which Windows
    will not delete until they are writable again."""
    with contextlib.suppress(OSError):
        for path in (run / SOURCE_REPOSITORY).rglob("*"):
            if path.is_file() and not path.is_symlink():
                path.chmod(stat.S_IREAD | stat.S_IWRITE)
    shutil.rmtree(run, ignore_errors=ignore_errors)


def command_script(name: str) -> str:
    """The path a role's prompt gives for this folder's script `name`, spelled as this process was started.

    `review-prs` grants its pipeline commands as text, `${CLAUDE_SKILL_DIR}/../code-review-core/scripts/` with the
    skill's directory filled in, and an inline role runs its prompt's commands in that same session, so a command
    spelled as the session started `prepare` matches the grant where the resolved path, with no `..`, never does.
    The reviewer guard resolves either spelling to the same script. A start the prompt cannot reuse falls back to the
    resolved path: a relative path, a path to another file, or one holding a character a quoted command cannot."""
    pipeline = Path(__file__).resolve()
    resolved = str(pipeline.parent / name)
    started = sys.argv[0] if sys.argv else ""
    if not started.endswith(pipeline.name) or not Path(started).is_absolute() or set(started) & set('"`$\r\n'):
        return resolved
    if os.path.normcase(str(Path(started).resolve())) != os.path.normcase(str(pipeline)):
        return resolved
    return started.removesuffix(pipeline.name) + name


def self_check_command(run: Path, role: str) -> str:
    return SELF_CHECK_COMMAND.format(script=command_script("review_pipeline.py"), run=run, role=role)


class PipelineError(ValueError):
    pass


class PipelineFieldError(FieldError, PipelineError):
    """A run or batch file field of the wrong kind."""


READ = FieldReader(PipelineFieldError)


EXPECTED_ERRORS = (
    PipelineError,
    ConfigurationError,
    GitHubError,
    RuntimeContractError,
    SpecialistError,
    ReviewOperationError,
    RecordError,
    ArchiveError,
    PersistenceError,
    StateError,
    FlagError,
    FixtureError,
    OSError,
    UnicodeError,  # a decoding fault the boundary did not absorb still ends as FAILED, never a traceback
)


@dataclass
class Services:
    """External effects, replaceable in tests."""

    github: GitHubClient = field(default_factory=GitHubClient)
    git: Runner = subprocess_runner
    fetch_tarball: Callable[[str, str, Path], None] = github_tarball_fetcher
    resolve_runtime: Callable[[str, str | None], str] = resolve_runtime
    today: Callable[[], date] = utc_today
    # The detached Copilot CLI host: how it starts, how its process is probed, and the clock wait and check use.
    launch: Callable[[Sequence[str], Path, Path], int] = start_detached
    probe: Callable[[int], ProcessStatus] = process_status
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    # The clock prepare times its phases with: fetching the head, materializing the snapshot, and writing prompts.
    timer: Callable[[], float] = time.monotonic
    copilot_runner: CopilotRunner = copilot_subprocess_runner
    copilot_executable: str | None = None


def _has_commit(checkout: Path, commit: str, git: Runner) -> bool:
    return git_in(checkout, git, "cat-file", "-e", f"{commit}^{{commit}}").returncode == 0


_FETCH_LOCKS: dict[str, threading.Lock] = {}
_FETCH_LOCKS_GUARD = threading.Lock()


def _fetch_lock(checkout: Path) -> threading.Lock:
    """One lock per checkout: concurrent fetches into one repository contend for FETCH_HEAD and ref locks."""
    key = os.path.normcase(str(checkout.resolve()))
    with _FETCH_LOCKS_GUARD:
        return _FETCH_LOCKS.setdefault(key, threading.Lock())


def ensure_local_commit(checkout: Path, commit: str, refspec: str, git: Runner) -> None:
    """Use the commit from the local clone when it is already there; fetch only what is missing."""
    if _has_commit(checkout, commit, git):
        return
    with _fetch_lock(checkout):
        if _has_commit(checkout, commit, git):  # another pull request's fetch may have brought it
            return
        result = git_in(checkout, git, "fetch", "--no-tags", "--quiet", "origin", refspec)
    if result.returncode != 0:
        raise PipelineError(f"Cannot fetch {refspec}: {result.stderr.strip() or 'git fetch failed'}")
    if not _has_commit(checkout, commit, git):
        raise PipelineError(f"Commit {commit} is not available after fetching {refspec}")


def choose_scope(
    requested: str,
    previous: dict[str, Any],
    patches: dict[str, dict[str, Any]],
    *,
    thresholds: dict[str, Any],
    entrypoint: bool,
) -> tuple[dict[str, Any], set[str] | None]:
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
        else {path for path, patch in patches.items() if (earlier.get(path) or {}).get("sha256") != patch["sha256"]}
    )
    lines_total = sum(patch["lines"] for patch in patches.values())
    lines_changed = None if changed is None else sum(patches[path]["lines"] for path in changed)
    scope = {
        "requested": requested,
        "since_version": previous["review"]["version"],
        "files_changed": None if changed is None else len(changed),
        "files_total": len(patches),
        "lines_changed": lines_changed,
        "lines_total": lines_total,
    }
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
    if used != "incremental" or changed is None:
        return {**scope, "used": used, "reason": reason}, None
    unseen = (previous["review"].get("coverage") or {}).get("uncovered_files", [])
    return {**scope, "used": used, "reason": reason}, changed | {path for path in unseen if path in patches}


def _skip(selector: str, reason: str) -> dict[str, Any]:
    return {"status": "skip", "selector": selector, "reason": reason}


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _review_history(
    archive_root: Path,
    repository: str,
    number: int,
    head: str,
    *,
    re_review: bool,
    force: bool,
    notes: list[str],
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]] | None:
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


def _fetch_diff(
    repository: str, number: int, pull: dict[str, Any], diff_path: Path, services: Services, notes: list[str]
) -> tuple[dict[str, dict[str, Any]], list[str], list[str], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Write the pull request's diff to `diff_path` once the pull is confirmed unmoved. Returns what `_write_diff`
    returns and the open review comments."""
    selector = f"{repository}#{number}"
    head = pull["headRefOid"]
    diff, undecodable = services.github.get_pull_diff(repository, number)
    # GitHub serves the diff by pull number, which follows pushes. Confirm the pull did not move after its
    # head was read, so a new diff is never archived under the old head SHA.
    current = validate_canary_pull(services.github.get_pull(repository, number), repository=repository, number=number)
    if (current["headRefOid"], current["baseRefOid"]) != (head, pull["baseRefOid"]):
        raise PipelineError(
            f"{selector} changed while it was being prepared (head {head[:12]} is now "
            f"{current['headRefOid'][:12]}); run prepare again"
        )
    parsed, changed, unsafe, patches = _write_diff(selector, diff, undecodable, diff_path, notes)
    comments = services.github.list_open_review_threads(repository, number)
    return parsed, changed, unsafe, patches, comments


def _write_diff(
    selector: str, diff: str, undecodable: int, diff_path: Path, notes: list[str]
) -> tuple[dict[str, dict[str, Any]], list[str], list[str], dict[str, dict[str, Any]]]:
    """Write a pull request's diff to `diff_path`. Returns the parsed diff, its changed paths, the changed paths no
    reviewer can be given (each noted), and their patch fingerprints."""
    atomic_write_text(diff_path, diff)
    if undecodable:
        notes.append(f"{undecodable} undecodable bytes replaced in the diff")
    parsed, unsafe = split_unified_diff(diff)
    changed = list(parsed)
    patches = patch_fingerprints(parsed)
    # Escaped, because each note is a line the orchestrator reads, and a raw path could add a line of its own.
    named = ", ".join(json.dumps(path) for path in unsafe)
    if not changed:
        raise PipelineError(
            f"{selector} changes no files" + (f" a reviewer can be given safely: {named}" if unsafe else "")
        )
    if unsafe:
        notes.append(
            f"Not reviewed: {len(unsafe)} changed file{'s' if len(unsafe) != 1 else ''} whose path a reviewer prompt "
            f"cannot carry safely (a control character, a backslash, or an absolute, empty, '.', or '..' segment), "
            f"recorded as unavailable sources: {named}."
        )
    return parsed, changed, unsafe, patches


def _snapshot(
    checkout: Path | None,
    repository: str,
    number: int,
    head: str,
    source: Path,
    parsed: dict[str, dict[str, Any]],
    changed: list[str],
    services: Services,
    notes: list[str],
    exclude: Sequence[str] = (),
) -> tuple[dict[str, tuple[int, str] | None], dict[str, Any], dict[str, Any]]:
    """Snapshot the head at `source`, from the checkout or else GitHub's tarball, leaving out what the repository's
    `exclude` globs match, and noting each changed document whose text it could not extract. Returns the symbolic
    links the snapshot leaves out that the pull request changes, each one noted, the snapshot's statistics: its
    source, files, bytes, and exclusions by reason, and the seconds spent fetching the head and materializing it, and
    the manifest materializing it verified, which the request and the plan take in place of verifying the snapshot
    again.

    A checkout's snapshot is lazy: it holds the changed files and the analyzer settings, and its reviewers fetch the
    rest through review_source.py. `_retake_snapshot` makes it whole where they cannot, and adds the paths the
    run's condition scripts declare they read."""
    fetched = 0.0
    refused: dict[str, str] = {}
    started = services.timer()
    if checkout is not None:
        verify_checkout_remote(checkout, repository, services.git)
        ensure_local_commit(checkout, head, f"refs/pull/{number}/head", services.git)
        fetched = services.timer() - started
        snapshot = materialize_source_snapshot(
            checkout,
            repository,
            head,
            source,
            runner=services.git,
            changed_paths=changed,
            upfront=lazy_upfront(()),
            exclude=exclude,
            refused=refused,
        )
    else:

        def fetcher(repository: str, commit: str, target: Path) -> None:
            nonlocal fetched
            began = services.timer()
            services.fetch_tarball(repository, commit, target)
            fetched += services.timer() - began

        snapshot = materialize_source_snapshot_from_github(
            repository, head, source, fetcher=fetcher, changed_paths=changed, exclude=exclude, refused=refused
        )
    materialized = services.timer() - started - fetched
    # Only the links this pull request adds or changes: a repository that keeps links is not told on every review.
    links = symbolic_links(parsed, snapshot["excluded_paths"])
    notes.extend(f"snapshot excludes symbolic link {path}" for path in links)
    # A snapshot taken again for the same change refuses the same documents, so these notes stand for it too.
    notes.extend(
        f"Not reviewed in full: the text of document {json.dumps(path)} could not be extracted ({reason}), so it "
        "is recorded as an unavailable source."
        for path, reason in sorted(refused.items())
    )
    stats = {
        "source": "tarball" if checkout is None else "checkout-lazy",
        "files": len(snapshot["source_hashes"]),
        "bytes": snapshot_bytes(source, snapshot),
        "excluded": exclusion_counts(snapshot["excluded_paths"]),
        "seconds": {"fetch": fetched, "materialize": materialized},
    }
    return links, stats, snapshot


def exclusion_counts(excluded: dict[str, str]) -> dict[str, int]:
    """How many paths a snapshot leaves out for each reason, by reason."""
    counts = Counter(excluded.values())
    return {reason: counts[reason] for reason in sorted(counts)}


def lazy_upfront(reads: Sequence[str]) -> Callable[[str], bool]:
    """The paths a lazy snapshot holds from the start beside the changed files: the analyzer settings, and those the
    `reads` glob patterns of its condition scripts match."""
    matchers = [glob_matcher(pattern) for pattern in reads]
    return lambda path: reads_settings(path) or any(matches(path) for matches in matchers)


def _snapshot_reads(
    snapshot: dict[str, Any],
    dispatch: str,
    kind: str,
    reviewer_root: Path | None,
    changed: list[str],
    renamed: Mapping[str, Sequence[str]],
) -> list[str] | None:
    """The glob patterns of the snapshot paths a run's condition scripts read, which its lazy snapshot must hold from
    the start; empty when it runs none or the snapshot is already whole. None when its reviewers need every file from
    the start: the Copilot CLI host runs no command, so it cannot fetch one, and a condition that declares no `reads`
    reads the snapshot as it chooses."""
    if SNAPSHOT_FETCHABLE not in snapshot:
        return []
    manifest = None
    if kind == "specialists" and reviewer_root is not None:
        try:
            manifest = load_materialized_manifest(reviewer_root)[0]
        except SpecialistError as exc:
            raise PipelineError(str(exc)) from exc
    return _route_reads(dispatch, manifest, changed, renamed)


def _route_reads(
    dispatch: str | None,
    manifest: dict[str, Any] | None,
    changed: list[str],
    renamed: Mapping[str, Sequence[str]],
) -> list[str] | None:
    """The route a checkout's snapshot takes for a run dispatched as `dispatch` (None when it is not the Copilot CLI
    host) whose reviewer's `manifest` (None for the generic reviewer) sees these changed files, with the earlier paths
    `renamed` gives them (see `review_specialists.renamed_from`): the glob patterns of
    the paths its lazy snapshot holds from the start, or None when it is written whole, as `_snapshot_reads` says.
    prepare and validate-reviewer both decide the route here, so they measure the same snapshot."""
    if dispatch == "copilot-host":
        return None
    if manifest is None or manifest.get("kind") != "specialists":
        return []
    return condition_reads(manifest, changed, renamed)


def _retake_snapshot(
    checkout: Path,
    repository: str,
    head: str,
    source: Path,
    changed: list[str],
    services: Services,
    stats: dict[str, Any],
    exclude: Sequence[str] = (),
    reads: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Replace a lazy snapshot, adding the time it takes to `stats`: with the whole one, now of a `checkout` snapshot,
    when `reads` is None, or else with a lazy one that also holds the paths its glob patterns match. Returns the
    manifest materializing it verified."""
    started = services.timer()
    shutil.rmtree(source)
    snapshot = materialize_source_snapshot(
        checkout,
        repository,
        head,
        source,
        runner=services.git,
        changed_paths=changed,
        upfront=None if reads is None else lazy_upfront(reads),
        exclude=exclude,
    )
    stats["source"] = "checkout" if reads is None else "checkout-lazy"
    stats["files"] = len(snapshot["source_hashes"])
    stats["bytes"] = snapshot_bytes(source, snapshot)
    stats["excluded"] = exclusion_counts(snapshot["excluded_paths"])
    stats["seconds"]["materialize"] += services.timer() - started
    return snapshot


# Reads a changed document's base version: its bytes, or None for one over the changed-file limit.
BaseReader = Callable[[str, str], bytes | None]
DIFF_BLOCK = re.compile(r"^diff --git ", re.MULTILINE)
INDEX_LINE = re.compile(r"index ([0-9a-f]+)\.\.([0-9a-f]+)")


def _base_reader(checkout: Path | None, repository: str, pull: dict[str, Any], services: Services) -> BaseReader:
    """How a review reads a changed document's base version, by the old path and abbreviated blob id its diff names:
    from the checkout's object store, or else from GitHub at the merge base the diff was taken against, where the
    blob GitHub names must start with that id and hash to its own."""
    if checkout is not None:

        def from_checkout(_path: str, abbreviation: str) -> bytes | None:
            return read_blob(checkout, abbreviation, services.git, maximum=MAX_CHANGED_FILE_BYTES)

        return from_checkout
    merge_base = functools.cache(
        lambda: services.github.get_merge_base(repository, pull["baseRefOid"], pull["headRefOid"])
    )

    def from_github(path: str, abbreviation: str) -> bytes | None:
        blob, content = services.github.get_file_blob(repository, merge_base(), path, maximum=MAX_CHANGED_FILE_BYTES)
        if not blob.startswith(abbreviation) or (content is not None and not blob_matches(blob, content)):
            raise RuntimeContractError(f"GitHub did not return blob {abbreviation}")
        return content

    return from_github


def _document_block(block: str, form: str) -> tuple[list[str], str | None] | None:
    """A changed document's diff block's header lines and the abbreviated base blob id its index line names (None for
    an added file), or None for a block that is not one file's binary block, or, for a format extracted whether or not
    it is binary, one file's text block."""
    header: list[str] = []
    old: str | None = None
    for raw in block.rstrip("\n").split("\n"):
        line = raw.removesuffix("\r")
        if header and line.startswith("diff --git "):
            return None
        if line.startswith(("Binary files ", "GIT binary patch")):
            return header, old
        if line.startswith("--- ") and form in EXTRACTED_WHEN_TEXT:
            return header, old
        if line.startswith(("@@", "--- ", "+++ ")):
            return None
        index = INDEX_LINE.match(line)
        if index is not None:
            old = index.group(1) if index.group(1).strip("0") else None
        header.append(line)
    return None


def _extracted_base(form: str, old: str | None, blob: str | None, read_base: BaseReader) -> tuple[str, str | None]:
    """A changed document's base version as extracted text, and why it could not be extracted, if it could not."""
    if old is None:
        return "", None
    if blob is None:
        return "", "its diff names no base blob"
    try:
        content = read_base(old, blob)
        if content is None:
            return "", "it is over the changed-file size limit"
        return extract(form, content).decode("utf-8"), None
    except ExtractionRefused as exc:
        return "", str(exc)
    except (GitHubError, RuntimeContractError):
        return "", "it could not be read"


def _document_diffs(
    diff_path: Path,
    parsed: dict[str, dict[str, Any]],
    snapshot: dict[str, Any],
    source: Path,
    read_base: BaseReader,
    notes: list[str],
) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]] | None:
    """Give each changed document the snapshot holds as extracted text, or the pull request deletes, a diff of its
    base and head text in place of its binary block in `diff_path`. Returns, for each document the snapshot holds,
    each line's place in the document, which a finding on that line records, and the patch fingerprints of the diff
    as rewritten; None when it changes nothing.

    The head text is the snapshot's own file, so a hunk's line numbers are that file's and a finding anchors to it.
    """
    extracted = snapshot.get(SNAPSHOT_EXTRACTED, {})
    rendered: dict[str, str] = {}
    places: dict[str, list[str]] = {}
    for path, entry in parsed.items():
        form = document_format(path)
        binary = None if form is None else _document_block(entry["block"], form)
        if form is None or binary is None:
            continue
        old, new = diff_block_sides(entry["block"])
        held = held_path(path, form)
        if path in extracted:
            head = source.joinpath(*Path(held).parts).read_text(encoding="utf-8")
            places[path] = locations(head)
        elif new is None:
            head = ""
        else:
            continue
        base, refused = _extracted_base(form, old, binary[1], read_base)
        if refused is not None:
            notes.append(f"The base version of document {json.dumps(path)} could not be extracted ({refused}).")
        rendered[path] = document_diff(binary[0], old, new, base, head, base_refused=refused, form=form, held=held)
    if not rendered:
        return None
    text = read_diff(diff_path)
    starts = [match.start() for match in DIFF_BLOCK.finditer(text)]
    pieces = [text[: starts[0]] if starts else text]
    for start, stop in zip(starts, [*starts[1:], len(text)], strict=True):
        block = text[start:stop]
        files = list(parse_unified_diff(block))
        pieces.append(rendered.get(files[0], block) if len(files) == 1 else block)
    atomic_write_text(diff_path, "".join(pieces))
    return places, patch_fingerprints(parse_unified_diff("".join(pieces)))


def _generic_reviewer(runtime: str, inline: bool, source: str) -> tuple[str, dict[str, Any], None, None, str]:
    """The suite's generic reviewer, as `_materialize_reviewer` returns it: `source` is `generic` when the repository
    is configured with it, and `generic-fallback` when it reviews in place of a repository reviewer."""
    dispatch = choose_dispatch(runtime, "generic", inline=inline)
    # The suite's own role needs agent delegation only when a subagent works it.
    negotiate_capabilities(runtime, ["agent-delegation"] if dispatch == "subagents" else [], dispatch=dispatch)
    adapter: dict[str, Any] = {
        "name": "generic",
        "scope": "generic",
        "source": source,
        "source_commit": None,
        "source_hashes": {},
    }
    return "generic", adapter, None, None, dispatch


def _default_branch_tip(checkout: Path, services: Services) -> tuple[str, str]:
    """origin's default branch and the commit at its tip, as origin reports them now.

    The checkout's origin is the configured repository, so its default branch holds only what the repository merged.
    """
    result = git_in(checkout, services.git, "ls-remote", "--symref", "origin", "HEAD")
    if result.returncode != 0:
        reason = result.stderr.strip() or "git ls-remote failed"
        raise PipelineError(f"Cannot read origin's default branch: {reason}")
    branch, tip = "", ""
    for line in result.stdout.splitlines():
        value, _, ref = line.partition("\t")
        if ref != "HEAD":
            continue
        if value.startswith("ref: refs/heads/"):
            branch = value.removeprefix("ref: refs/heads/")
        elif re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
            tip = value.lower()
    if not tip:
        raise PipelineError("Cannot read origin's default branch: origin names no default branch")
    return branch, tip


def _resolve_reviewer_source(
    reviewer: dict[str, Any],
    *,
    checkout: Path,
    pull: dict[str, Any],
    config_path: Path,
    repository: str,
    services: Services,
    notes: list[str],
) -> tuple[str, str, ResolvedReviewer] | None:
    """Where a repository reviewer is read from: `trusted-ref` or `base`, the commit, and what it resolved to there.

    A configured skill the base predates, with no trusted ref, is read from the default branch's tip instead
    (`default-branch`). None, with a note, when the tip lacks it too or is the pull request's head: the suite's
    generic reviewer then reviews the pull request, since the head never supplies its own reviewer.
    """
    head, base = pull["headRefOid"].lower(), pull["baseRefOid"]
    source = "trusted-ref" if reviewer["trusted_ref"] else "base"
    commit = resolve_reviewer_commit(checkout, reviewer["trusted_ref"] or base, head_sha=head, runner=services.git)
    resolve = functools.partial(
        resolve_reviewer,
        reviewer,
        checkout=checkout,
        config_path=config_path,
        repository=repository,
        runner=services.git,
    )
    try:
        return source, commit, resolve(commit=commit)
    except MissingSkillError:
        if source != "base":
            raise
    skill = reviewer["skill"]
    name, tip = _default_branch_tip(checkout, services)
    branch = name or "origin's default branch"
    if tip == head:
        notes.append(
            f"The base {commit[:12]} predates the review skill {skill}, and {branch}'s tip is this pull request's "
            "head, which never supplies its own reviewer, so the suite's generic reviewer reviews this pull request."
        )
        return None
    if tip != commit:
        ensure_local_commit(checkout, tip, f"refs/heads/{name}" if name else "HEAD", services.git)
        try:
            resolved = resolve(commit=tip)
        except MissingSkillError:
            pass
        else:
            notes.append(
                f"The base {commit[:12]} predates the review skill {skill}, so the reviewer was read from {branch}'s "
                f"tip {tip[:12]}."
            )
            return "default-branch", tip, resolved
    notes.append(
        f"Neither the base {commit[:12]} nor {branch}'s tip {tip[:12]} has the review skill {skill}, so the suite's "
        "generic reviewer reviews this pull request."
    )
    return None


def _materialize_reviewer(
    reviewer: dict[str, Any],
    *,
    checkout: Path | None,
    pull: dict[str, Any],
    mode: str,
    runtime: str,
    inline: bool,
    run: Path,
    config_path: Path,
    repository: str,
    services: Services,
    notes: list[str],
) -> tuple[str, dict[str, Any], Path | None, str | None, str]:
    """The reviewer's kind, the adapter the record names, the root its trusted files were materialized under, its
    entrypoint, and how its roles are dispatched. The suite's generic reviewer has no root and no entrypoint, and
    neither has a specialists manifest."""
    if reviewer["scope"] == "generic":
        return _generic_reviewer(runtime, inline, "generic")
    if checkout is None:  # validate_config requires a checkout for a repository reviewer
        raise PipelineError(f"{repository} has a repository reviewer but no checkout_path")
    ensure_local_commit(checkout, pull["baseRefOid"], f"refs/heads/{pull['baseRefName']}", services.git)
    found = _resolve_reviewer_source(
        reviewer,
        checkout=checkout,
        pull=pull,
        config_path=config_path,
        repository=repository,
        services=services,
        notes=notes,
    )
    if found is None:
        return _generic_reviewer(runtime, inline, "generic-fallback")
    source, reviewer_commit, resolved = found
    manifest = resolved.manifest
    if resolved.inspection is not None and resolved.source == "skill" and resolved.inspection.delegates == "unknown":
        notes.append(
            f"{resolved.inspection.skill} may start subagents ({resolved.inspection.reason}); "
            "if its review fails, give it a specialists manifest."
        )
    if mode not in manifest["supports"]:
        raise PipelineError(f"Reviewer {manifest['id']} does not support {mode} reviews")
    kind = "specialists" if manifest.get("kind") == "specialists" else "entrypoint"
    dispatch = choose_dispatch(runtime, kind, inline=inline)
    # A specialists manifest that lists agent-delegation keeps its specialists off an inline review.
    negotiate_capabilities(runtime, manifest["required_capabilities"], dispatch=dispatch)
    reviewer_root = run / "reviewer"
    hashes = materialize_reviewer(
        checkout,
        reviewer_commit,
        manifest,
        reviewer_root,
        runner=services.git,
        guideline_commit=pull["baseRefOid"],
        local_root=resolved.local_root,
    )
    adapter = {
        "name": manifest["id"],
        "scope": "repository",
        "source": source,
        "source_commit": reviewer_commit,
        "source_hashes": hashes,
    }
    return kind, adapter, reviewer_root, manifest["entrypoint"] if kind == "entrypoint" else None, dispatch


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
    unsafe: list[str],
    unreviewed_suffixes: tuple[str, ...] = (),
) -> None:
    """The adapter request, whose unavailable sources include every changed path no reviewer could be given, which
    is then no unreviewed file, and beside it the file of the pull request's description, which is never prompt
    text."""
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
    coverage["unavailable_sources"] = sorted({*coverage["unavailable_sources"], *unsafe})
    coverage["unreviewed_files"] = {
        path: suffix for path, suffix in coverage["unreviewed_files"].items() if path not in unsafe
    }
    write_adapter_request(request_path, request)


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
) -> tuple[list[dict[str, Any]], list[str]]:
    """Each reviewer role with its prompt written: an entrypoint's one, or the specialists plan's. Returns the roles
    and the changed files the plan leaves unreviewed. A `lazy` snapshot's prompts name its source commands, and a
    `document_stage` reaches the design-review specialist's."""
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
                source_commands=source_commands(run, adapter["name"], script=script) if lazy else None,
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
        source_commands=(lambda identity: source_commands(run, identity, script=script)) if lazy else None,
        document_stage=document_stage,
    )
    roles = [
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


def _write_run(run: Path, state: dict[str, Any]) -> None:
    """Write a prepared run's run.json. A run that cannot start a role on the model it asks for first notes that model
    in place of its `MODEL` line, so the role runs on the session's model; an inline run then seals every file
    prepare wrote."""
    reason = _unapplied_model_reason(state["runtime"], state["dispatch"])
    if reason is not None:
        for role in state["roles"]:
            if role.get("model"):
                state["notes"].append(f"{role['id']} asks for model {role['model']}; {reason}")
                role["model"] = None
    if state["dispatch"] == "inline":
        state["seal"] = {"files": sealed_files(run, state), "results": {}}
    atomic_write_json(run / RUN_FILE, state)


def _unapplied_model_reason(runtime: str, dispatch: str) -> str | None:
    """Why no role of a run dispatched this way on this runtime starts on a model alias, or None when every role can."""
    if dispatch == "inline":
        # The orchestrating session works every role on its own model; no subagent can be started on another.
        return "an inline review works every role on this session's model"
    if runtime not in MODEL_ALIAS_RUNTIMES:
        return f"{runtime} starts every reviewer on this session's model"
    return None


@dataclass
class _Review:
    """What a run prepares to review, read from GitHub or from a fixture."""

    selector: str
    repository: str
    number: int
    pull: dict[str, Any]  # validated by validate_canary_pull
    mode: str
    previous: dict[str, Any] | None  # the record a re-review starts from
    prior: list[dict[str, Any]]  # the findings it carries
    base: dict[str, Any]  # the archive state finalize must still find
    reviewer: dict[str, Any]
    checkout: Path | None  # the configured checkout, which reviewers are kept out of
    source: Path | None  # the repository the head is snapshotted from; None for GitHub's tarball
    # Writes the diff to the path it is given; returns _write_diff's values and the open review comments.
    read_diff: Callable[
        [Path], tuple[dict[str, dict[str, Any]], list[str], list[str], dict[str, dict[str, Any]], list[dict[str, Any]]]
    ]
    canary: bool
    fixture: dict[str, Any] | None = None  # run.json's record of a fixture canary
    prior_record: dict[str, Any] | None = None  # a fixture re-review's prior record, which finalize archives first
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


# The reviewer of a fixture whose pull.json names no reviewer manifest.
GENERIC_REVIEWER = {
    "id": "generic",
    "protocol_version": 1,
    "trusted_ref": None,
    "scope": "generic",
    "manifest_path": None,
}


def fixture_reviewer(manifest_path: str) -> dict[str, Any]:
    """The reviewer of a fixture whose pull.json names a manifest: a repository reviewer with no trusted ref, so its
    manifest and files are read from the fixture's base commit, as a configured repository's would be."""
    return {
        "id": "fixture",
        "protocol_version": 1,
        "trusted_ref": None,
        "scope": "repository",
        "manifest_path": manifest_path,
        "skill": None,
    }


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
    session work every role itself, as it does wherever the runtime cannot start subagents.
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
    previous: dict[str, Any] | None = None
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
        read_diff=lambda diff_path: _fetch_diff(repository, number, pull, diff_path, services, notes),
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
    with fixture_change(directory, services.git) as change:
        repository, number, pull = change.name, change.pull["number"], change.pull
        selector = f"{repository}#{number}"
        record = None
        if prior_value is not None:
            record = validate_prior_record(prior_value, repository=repository, number=number)
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
            read_diff=lambda diff_path: (
                *_write_diff(selector, change.diff, change.undecodable, diff_path, notes),
                change.comments,
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
        parsed, changed, unsafe, patches, comments = review.read_diff(diff_path)
        source = run / "source"
        head = pull["headRefOid"]
        repository_path = review.source
        if repository_path is not None and review.adopt_source:
            repository_path = Path(shutil.move(repository_path, run / SOURCE_REPOSITORY))
        links, snapshot, manifest = _snapshot(
            repository_path, repository, number, head, source, parsed, changed, services, notes, review.snapshot_exclude
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
                repository_path, repository, head, source, changed, services, snapshot, review.snapshot_exclude, reads
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
        scope_record: dict[str, Any] | None = None
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
            unsafe=unsafe,
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
        state = {
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


def _configured_entry(
    config: dict[str, Any], repository: str, *, re_review: bool, scope: str | None, force: bool, canary: bool
) -> dict[str, Any]:
    """The repository's configuration entry, once the request's options fit together: a canary is an unforced initial
    review, and a re-review, and only a re-review, names a known scope."""
    if canary and (force or re_review):
        raise PipelineError("A canary is an initial review and cannot be forced")
    if re_review != (scope is not None) or scope not in (None, *RE_REVIEW_SCOPES):
        raise PipelineError(f"A re-review, and only a re-review, takes a scope: {', '.join(RE_REVIEW_SCOPES)}")
    entry = config["repositories"].get(repository)
    if entry is None:
        raise PipelineError(f"{repository} is not a configured repository")
    return entry


def _repository_reviewer(
    repository: str, config_path: Path | None, services: Services
) -> tuple[Path, str, dict[str, Any], Path, str]:
    """The configuration file, the repository's identity, its configured entry, its checkout, and the configured
    runtime, once the entry names a repository reviewer and the checkout's origin is the repository."""
    config_path = (config_path or default_config_path()).resolve()
    config = load_config(config_path)
    repository = validate_repository_identity(repository)
    entry = config["repositories"].get(repository)
    if entry is None:
        raise PipelineError(f"{repository} is not a configured repository")
    if entry["reviewer"]["scope"] != "repository":
        raise PipelineError(f"{repository} uses the suite's generic reviewer; it has no repository reviewer")
    checkout = Path(entry["checkout_path"])
    verify_checkout_remote(checkout, repository, services.git)
    return config_path, repository, entry, checkout, config["runtime"]


def _reviewer_commit(checkout: Path, reviewer: dict[str, Any], ref: str | None, services: Services) -> str:
    """The commit a reviewer is read from without a pull request: --ref, the trusted ref, or origin's default."""
    candidate = ref or reviewer["trusted_ref"] or "refs/remotes/origin/HEAD"
    try:
        return resolve_reviewer_commit(checkout, candidate, head_sha="", runner=services.git)
    except RuntimeContractError as exc:
        raise PipelineError(f"Cannot resolve {candidate} in {checkout}; pass --ref: {exc}") from exc


def inspect_reviewer(
    repository: str, *, ref: str | None = None, config_path: Path | None = None, services: Services | None = None
) -> list[str]:
    """Whether a repository's review skill can run as one entrypoint reviewer or needs a specialists manifest."""
    services = services or Services()
    config_path, repository, entry, checkout, _ = _repository_reviewer(repository, config_path, services)
    reviewer = entry["reviewer"]
    commit = _reviewer_commit(checkout, reviewer, ref, services)
    lines = [f"REVIEWER {reviewer['id']} {repository} commit={commit}"]
    if reviewer["manifest_path"]:
        return [*lines, f"MANIFEST repository {reviewer['manifest_path']}", "VERDICT manifest-configured"]
    inspection = inspect_configured_skill(checkout, commit, reviewer["skill"], services.git)
    lines.append(f"SKILL {inspection.skill}")
    lines.append("TOOLS " + (", ".join(inspection.tools) if inspection.tools is not None else "inherited (all)"))
    lines.append(f"DELEGATES {inspection.delegates} {inspection.reason}")
    lines.extend(f"EVIDENCE {number} {text}" for number, text in inspection.evidence)
    lines.extend(f"REFERENCES {path}" for path in inspection.references)
    local = manifest_location(reviewer, config_path, repository)
    if local is not None:
        lines.append(f"MANIFEST local {local} {'present' if local.is_file() else 'missing'}")
        verdict = "manifest-configured"
    else:
        verdict = {"yes": "manifest-required", "no": "entrypoint-ok", "unknown": "undetermined"}[inspection.delegates]
    lines.append(f"VERDICT {verdict}")
    return lines


def _unmatched_patterns(manifest: dict[str, Any], files: set[str]) -> list[str]:
    """Specialist include patterns that match no file in the repository: usually a typo or a renamed folder."""
    unmatched = []
    for specialist in manifest.get("specialists", []):
        excludes = [re.compile(pattern) for pattern in specialist["exclude"]]
        for pattern in specialist["include"]:
            compiled = re.compile(pattern)
            if not any(compiled.search(path) and not any(e.search(path) for e in excludes) for path in files):
                unmatched.append(f"UNMATCHED {specialist['id']} {pattern}")
    return unmatched


def run_source_example() -> Path:
    """A path as long as the source folder of a run prepare creates, which tempfile names with RUN_PREFIX and eight
    random characters, so a measurement leaves each path the room prepare's snapshot will."""
    return Path(tempfile.gettempdir()).resolve() / f"{RUN_PREFIX}{'x' * 8}" / "source"


def _excluded_reviewer_files(manifest: dict[str, Any], exclude: Sequence[str]) -> list[str]:
    """Each file the reviewer declares that a snapshot_exclude glob matches, as `<pattern> <path>`."""
    return [
        f"{pattern} {path}"
        for pattern in exclude
        for path in declared_reviewer_files(manifest)
        if glob_matcher(pattern)(path)
    ]


def _snapshot_line(
    checkout: Path,
    commit: str,
    changed: list[str],
    services: Services,
    exclude: Sequence[str] = (),
    reads: Sequence[str] | None = (),
) -> str:
    """The source snapshot prepare would write for this commit on the route `_route_reads` gave, or the reason prepare
    would refuse it: lazy, writing the changed files, the analyzer settings, and the paths the `reads` patterns
    match, and listing the rest as fetchable, or whole when `reads` is None."""
    size = measure_source_snapshot(
        checkout,
        commit,
        destination=run_source_example(),
        runner=services.git,
        changed_paths=changed,
        exclude=exclude,
        upfront=None if reads is None else lazy_upfront(reads),
    )
    source = "checkout" if reads is None else "checkout-lazy"
    error = size.limit_error()
    if error:
        raise PipelineError(f"The {source} source snapshot of {commit[:12]} cannot be prepared: {error}")
    excluded = ",".join(f"{reason}:{count}" for reason, count in sorted(size.excluded.items())) or "none"
    return (
        f"SNAPSHOT {commit[:12]} source={source} files={size.files} fetchable={size.fetchable} bytes={size.bytes} "
        f"limit={MAX_SOURCE_SNAPSHOT_BYTES} excluded={excluded}"
    )


def _route_condition(
    name: str,
    *,
    checkout: Path,
    repository: str,
    head: str,
    changed: list[str],
    renamed: Mapping[str, Sequence[str]],
    reviewer_root: Path,
    manifest: dict[str, Any],
    source: Path,
    work: Path,
    diff_file: Path,
    results: dict[str, bool],
    services: Services,
    exclude: Sequence[str] = (),
) -> bool:
    """Evaluate one routing condition on the pull request's source snapshot, taken on first use as a review takes it,
    and record it: lazy, holding the paths the conditions routing runs declare they read, when each declares them.
    The script is given the pull request's diff, `diff_file`, and a file to write, as a review gives them."""
    if not source.exists():
        reads = condition_reads(manifest, changed, renamed)
        materialize_source_snapshot(
            checkout,
            repository,
            head,
            source,
            runner=services.git,
            changed_paths=changed,
            upfront=None if reads is None else lazy_upfront(reads),
            exclude=exclude,
        )
    work.mkdir(exist_ok=True)
    output = work / f"{name}.txt"
    script = manifest["conditions"][name]["script"]
    results[name] = evaluate_condition(reviewer_root, script, source, work, diff_file=diff_file, output=output)
    return results[name]


def validate_reviewer(
    repository: str,
    *,
    pulls: list[int] | None = None,
    ref: str | None = None,
    host: str | None = None,
    config_path: Path | None = None,
    services: Services | None = None,
) -> list[str]:
    """Prove a repository reviewer works without starting a reviewer or writing anything.

    Checks the manifest's structure, that every file it names exists (profiles at the trusted commit, condition
    scripts beside a local manifest), that no snapshot_exclude glob matches one of them, which include patterns
    match nothing, and, for each pull request, where prepare would read its reviewer, the snapshot prepare would
    write on the route it would take, and which specialists its changes would start, with each condition script's
    real result against the pull's head. `host` decides an auto runtime as prepare's does; only an entrypoint
    reviewer's route depends on the runtime.
    """
    services = services or Services()
    config_path, repository, entry, checkout, runtime = _repository_reviewer(repository, config_path, services)
    reviewer, exclude = entry["reviewer"], entry["snapshot_exclude"]
    default_effort = load_config(config_path)["reviewer_effort"]
    targets = _validation_targets(repository, checkout, reviewer, pulls, ref, config_path, services)
    lines: list[str] = []
    checked: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="code-review-validate-") as temporary:
        scratch = Path(temporary)
        for index, target in enumerate(targets):
            pull = target.pull
            diff_file = scratch / f"diff-{index}.patch"
            changed, renamed, pull_lines = _pull_changes(repository, target, diff_file, services)
            if target.commit is None or target.resolved is None:
                # The suite's generic reviewer stands in, on the lazy snapshot prepare gives it.
                lines.extend(pull_lines)
                if pull is not None:
                    lines.append(_snapshot_line(checkout, pull["headRefOid"], changed, services, exclude))
                lines.append(f"GENERIC files={len(changed)} (the suite's generic reviewer reviews it)")
                continue
            commit, manifest = target.commit, target.resolved.manifest
            root = scratch / f"reviewer-{index}"
            hashes = materialize_reviewer(
                checkout,
                commit,
                manifest,
                root,
                runner=services.git,
                guideline_commit=pull["baseRefOid"] if pull else None,
                local_root=target.resolved.local_root,
            )
            kind = "specialists" if manifest.get("kind") == "specialists" else "entrypoint"
            excluded = _excluded_reviewer_files(manifest, exclude)
            if excluded:
                raise PipelineError(
                    f"snapshot_exclude of {repository} matches a file its reviewer declares, which a review must be "
                    "able to read; narrow the pattern: " + ", ".join(excluded)
                )
            if commit not in checked:
                checked.add(commit)
                lines.append(
                    f"REVIEWER {manifest['id']} {kind} source={target.resolved.source} {target.resolved.location} "
                    f"commit={commit}"
                )
                lines.append(f"FILES {len(hashes)} found")
                lines.extend(_unmatched_patterns(manifest, repository_files(checkout, commit, services.git)))
            # Only an entrypoint reviewer can run on the Copilot CLI host, whose snapshot is whole.
            dispatch = choose_dispatch(services.resolve_runtime(runtime, host), kind) if kind == "entrypoint" else None
            reads = _route_reads(dispatch, manifest, changed, renamed)
            lines.extend(pull_lines)
            if pull is None:
                lines.append(_snapshot_line(checkout, commit, [], services, exclude, reads))
                continue
            lines.append(_snapshot_line(checkout, pull["headRefOid"], changed, services, exclude, reads))
            if kind == "entrypoint":
                lines.append(f"ENTRYPOINT {manifest['id']} files={len(changed)}")
                continue
            routing = _Routing(
                checkout,
                repository,
                pull["headRefOid"],
                root,
                scratch,
                index,
                diff_file,
                services,
                exclude,
                default_effort,
            )
            lines.extend(_routing_lines(manifest, changed, renamed, routing))
    lines.append("VALID")
    return lines


def _pull_changes(
    repository: str, target: _ValidationTarget, diff_file: Path, services: Services
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """The changed paths of the pull request a validation target reviews, the earlier paths its renames give them,
    and its PULL, BODY, and NOTE lines, with its diff written to `diff_file`; none of them for a target without one."""
    pull = target.pull
    if pull is None:
        return [], {}, []
    diff_text = services.github.get_pull_diff(repository, pull["number"])[0]
    parsed = parse_unified_diff(diff_text)
    atomic_write_text(diff_file, diff_text)
    lines = [
        f"PULL {repository}#{pull['number']} base={pull['baseRefOid'][:12]} "
        f"head={pull['headRefOid'][:12]} files={len(parsed)} reviewer={target.source}",
        _body_line(pull.get("body", "")),
        *(f"NOTE {note}" for note in target.notes),
    ]
    return list(parsed), renamed_from(parsed), lines


def _body_line(body: str) -> str:
    """How much of the pull request's description `prepare` would give its reviewers."""
    return f"BODY characters={len(body)} given={min(len(body), PULL_REQUEST_BODY_CHARACTERS)}"


class _Routing(NamedTuple):
    """Where validate-reviewer evaluates one pull request's routing conditions, as a review would."""

    checkout: Path
    repository: str
    head: str
    reviewer_root: Path
    scratch: Path
    target: int  # the target's position, which names its scratch folders
    diff_file: Path  # the pull request's diff, which a condition script is given
    services: Services
    exclude: Sequence[str]
    default_effort: str | None  # the configuration's reviewer_effort, which a specialist without its own effort gets


def _routing_lines(
    manifest: dict[str, Any], changed: list[str], renamed: Mapping[str, Sequence[str]], routing: _Routing
) -> list[str]:
    """What a specialists manifest does with a pull request's changes, a renamed file matched by each of its paths:
    each condition's result, each route with its settings, and the changed files no specialist matches."""
    results: dict[str, bool] = {}
    condition = functools.partial(
        _route_condition,
        checkout=routing.checkout,
        repository=routing.repository,
        head=routing.head,
        changed=changed,
        renamed=renamed,
        reviewer_root=routing.reviewer_root,
        manifest=manifest,
        source=routing.scratch / f"source-{routing.target}",
        work=routing.scratch / f"conditions-{routing.target}",
        diff_file=routing.diff_file,
        results=results,
        services=routing.services,
        exclude=routing.exclude,
    )
    routes = route(manifest, changed, condition, renamed)
    lines = [f"CONDITION {name} {'open' if value else 'closed'}" for name, value in results.items()]
    specialists = {specialist["id"]: specialist for specialist in manifest["specialists"]}
    for identity, files in routes.items():
        model, note = specialist_model(specialists[identity], routing.reviewer_root)
        # The effort a Workflow agent would start on, as `workflow` resolves it; null leaves the session's.
        effort = specialists[identity].get("effort") or routing.default_effort
        lines.append(
            f"ROUTE {identity} files={len(files)}"
            + (f" model={model}" if model else "")
            + (f" effort={effort}" if effort else "")
        )
        lines.extend([f"NOTE {note}"] if note else [])
    outside = uncovered(manifest, changed, renamed)
    if len(outside) == len(changed):
        lines.append(f"GENERIC files={len(changed)} (no specialist matched; the generic reviewer reviews it)")
        return lines
    lines.extend(f"UNCOVERED {path}" for path in outside)
    if outside and manifest.get("uncovered", "review") == "ignore":
        left = unreviewed(manifest, outside, routes)
        if left:
            lines.append(f"UNREVIEWED files={len(left)} (the manifest sets uncovered to ignore; the record lists them)")
    elif outside:
        lines.append(f"GENERIC files={len(outside)} (no specialist covers them; the generic reviewer reviews them)")
    return lines


class _ValidationTarget(NamedTuple):
    """One reviewer validate-reviewer checks: the commit it is read from and what it resolved to there, the pull
    request it reviews, the review.adapter.source prepare would record, and prepare's notes on a fallback. `commit`
    and `resolved` are None where the suite's generic reviewer stands in for the repository's."""

    commit: str | None
    resolved: ResolvedReviewer | None
    pull: dict[str, Any] | None
    source: str | None
    notes: list[str]


def _validation_targets(
    repository: str,
    checkout: Path,
    reviewer: dict[str, Any],
    pulls: list[int] | None,
    ref: str | None,
    config_path: Path,
    services: Services,
) -> list[_ValidationTarget]:
    """Each reviewer to validate, resolved before any is materialized: for each pull request, once both of its
    commits are local, the one prepare would read (the trusted ref or the base, else the default branch's tip, else
    the generic reviewer); with none, the reviewer at --ref, the trusted ref, or origin's default."""
    targets: list[_ValidationTarget] = []
    for number in pulls or []:
        fetched = validate_canary_pull(
            services.github.get_pull(repository, number), repository=repository, number=number
        )
        ensure_local_commit(checkout, fetched["headRefOid"], f"refs/pull/{number}/head", services.git)
        ensure_local_commit(checkout, fetched["baseRefOid"], f"refs/heads/{fetched['baseRefName']}", services.git)
        notes: list[str] = []
        found = _resolve_reviewer_source(
            reviewer,
            checkout=checkout,
            pull=fetched,
            config_path=config_path,
            repository=repository,
            services=services,
            notes=notes,
        )
        if found is None:
            targets.append(_ValidationTarget(None, None, fetched, "generic-fallback", notes))
        else:
            source, commit, resolved = found
            targets.append(_ValidationTarget(commit, resolved, fetched, source, notes))
    if not targets:
        commit = _reviewer_commit(checkout, reviewer, ref, services)
        resolved = resolve_reviewer(
            reviewer,
            checkout=checkout,
            commit=commit,
            config_path=config_path,
            repository=repository,
            runner=services.git,
        )
        targets.append(_ValidationTarget(commit, resolved, None, None, []))
    return targets


def load_run(run: Path) -> dict[str, Any]:
    state = READ.json_object(read_json(run / RUN_FILE), "run")
    if READ.integer(state.get("schema_version"), "run.schema_version") != RUN_SCHEMA_VERSION:
        raise PipelineError(f"{run} is not a prepared review run")
    return state


def role_errors(run: Path, state: dict[str, Any]) -> dict[str, str]:
    """Each role whose result is missing or invalid, with the reason."""
    if state["kind"] != "entrypoint":
        return check(read_json(run / "work" / "plan.json", maximum_bytes=64 * 1024 * 1024))
    role = state["roles"][0]
    request = read_json(Path(state["request_path"]))
    try:
        result = validate_adapter_result(
            read_json(Path(role["result_file"])),
            expected_repository=request["repository"],
            expected_number=request["pull_number"],
            expected_head_sha=request["pull_request"]["head_sha"],
            prior_ids=[finding["id"] for finding in request["prior_findings"]],
            prior_severities=prior_severities(request),
            comment_ids=[comment["id"] for comment in request["github_comments"]],
            # A repository entrypoint reviewer may predate comment dispositions; if it gives any, it gives all.
            require_comment_dispositions=False,
        )
    except (PersistenceError, RecordError, ConfigurationError) as exc:
        return {role["id"]: str(exc)}
    if result["status"] != "complete":
        return {role["id"]: f"result status is {result['status']}"}
    # As a specialist's finding must sit in its own files, an entrypoint's must sit in a file the diff changes.
    changed = parse_unified_diff(read_diff(Path(request["diff_path"])))
    for finding in result["findings"]:
        if finding["path"] not in changed:
            return {
                role["id"]: f"Finding {finding['candidate_key']} at {finding['path']}:{finding['line']} is not on a "
                "file the pull request changes; take the path from a `diff --git` header in DIFF_FILE"
            }
    return {}


def mark_dispatched(run: Path) -> None:
    """Time every role of a run from now: its reviewers are about to start.

    A reviewer's time runs from here to its accepted result, so a rerun counts toward it and is never restarted.
    """
    state = load_run(run)
    now = time.time()
    state["dispatched_at"] = {role["id"]: now for role in state["roles"]}
    atomic_write_json(run / RUN_FILE, state)


def count_reads(run: Path, state: dict[str, Any], roles: Sequence[str]) -> bool:
    """Reduce each named role's read log, which the reviewer guard writes, to the distinct snapshot files it names and
    their bytes, add them to the role's counts in run.json's `reads`, and delete the log, so no path outlives this
    step. Returns whether any log was counted. A role without a log had no guard and stays uncounted, and a run
    prepared before reads were counted has no `reads` and counts nothing. The snapshot's own manifest is not a file
    of the snapshot, as `review.snapshot.files` leaves it out, so reading it counts for nothing."""
    if "reads" not in state:
        return False
    source = run / "source"
    manifest = os.path.normcase(os.path.normpath(source / SOURCE_SNAPSHOT_MANIFEST))
    counted = False
    for role in roles:
        log = read_log(run, role)
        if not log.is_file():
            continue
        sizes: dict[str, int] = {}
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            with contextlib.suppress(ValueError):
                relative = json.loads(line)
                target = source.joinpath(relative) if isinstance(relative, str) else source
                key = os.path.normcase(os.path.normpath(target))
                if key not in sizes and key != manifest and _inside(target, source) and target.is_file():
                    sizes[key] = target.stat().st_size
        total = state["reads"].get(role, {"files": 0, "bytes": 0})
        state["reads"][role] = {"files": total["files"] + len(sizes), "bytes": total["bytes"] + sum(sizes.values())}
        log.unlink()
        counted = True
    return counted


def reviewer_seconds(state: dict[str, Any]) -> dict[str, int]:
    """Whole seconds from each role's dispatch to the last write of its result; untimed roles are left out.

    Windows dates a file from a coarser clock than the one dispatch reads, so a result written at once can carry an
    mtime a few milliseconds early: under a second early counts as zero. A second or more early is a result written
    before the role was handed out, which is not timed.
    """
    seconds = {}
    for role in state["roles"]:
        started = state.get("dispatched_at", {}).get(role["id"])
        if started is None:
            continue
        elapsed = Path(role["result_file"]).stat().st_mtime - started
        if elapsed > -1:
            seconds[role["id"]] = max(0, round(elapsed))
    return seconds


def run_dispatch(state: dict[str, Any]) -> str:
    """How a run's roles are worked. A run prepared before the dispatch was recorded was dispatched as its runtime
    and reviewer kind decided then: a Copilot CLI entrypoint on its host, everything else as subagents."""
    if "dispatch" in state:
        return str(state["dispatch"])
    return "copilot-host" if state["runtime"] == "copilot-cli" and state["kind"] == "entrypoint" else "subagents"


def _is_copilot_host_run(state: dict[str, Any]) -> bool:
    return run_dispatch(state) == "copilot-host"


def _inline_only(state: dict[str, Any]) -> PipelineError:
    return PipelineError(f"{state['selector']} is reviewed inline; work its roles with next-role")


def sealed_files(run: Path, state: dict[str, Any]) -> dict[str, str]:
    """The SHA-256 of each file of an inline run its reviewers must not change, by path relative to the run.

    That is every file prepare wrote but run.json, which the pipeline itself updates; the source snapshot, which its
    own manifest hashes and which is too large to hash again before every role, and a fixture's repository, which a
    lazy snapshot's files are fetched from and checked against by blob id; and the reviewers' results, which
    next-role seals one by one as it moves past them.
    """
    results = {Path(role["result_file"]) for role in state["roles"]} | {Path(state["result_path"])}
    skipped = {(result.parent, result.name) for result in results}
    rejected = {(result.parent, f"{result.name}.rejected-") for result in results}
    digests = {}
    for path in sorted(run.rglob("*")):
        relative = path.relative_to(run)
        if relative.parts[0] in {"source", SOURCE_REPOSITORY, RUN_FILE, RECORDED_FILE} or not path.is_file():
            continue
        if (path.parent, path.name) in skipped or any(
            path.parent == parent and path.name.startswith(prefix) for parent, prefix in rejected
        ):
            continue
        digests[relative.as_posix()] = _sha256(path)
    return digests


def verify_seal(run: Path, state: dict[str, Any]) -> None:
    """Fail an inline run if a file it sealed changed or went missing.

    An inline reviewer is the orchestrating session, which no hook confines to its role, so this is what keeps a later
    role from rewriting an earlier role's result, the request's coverage or prior findings, the plan, or the next
    role's instructions: the pull request fails with nothing recorded rather than being retried.
    """
    seal = state["seal"]
    results = {role["id"]: Path(role["result_file"]) for role in state["roles"]}
    expected = [(run.joinpath(*relative.split("/")), digest) for relative, digest in seal["files"].items()]
    expected.extend((results[identity], digest) for identity, digest in seal["results"].items())
    changed = [
        path.relative_to(run).as_posix() for path, digest in expected if not path.is_file() or _sha256(path) != digest
    ]
    if changed:
        raise PipelineError(f"{state['selector']}: run files changed during the inline review: {', '.join(changed)}")


def next_role(run: Path, services: Services) -> tuple[str, dict[str, Any] | None]:
    """An inline run's selector and the next role its orchestrating session works, or None once every role has a
    result.

    The run's sealed files must be unchanged. Every result written so far is sealed before the next role is handed
    out, so the roles after it cannot change it. A role is timed from its first hand-out, so a rerun counts toward it.
    """
    run = run.resolve()
    state = load_run(run)
    if run_dispatch(state) != "inline":
        raise PipelineError(f"{state['selector']} is not reviewed inline; dispatch its roles as prepare printed them")
    verify_seal(run, state)
    following = None
    for role in state["roles"]:
        result = Path(role["result_file"])
        if result.is_file():
            state["seal"]["results"].setdefault(role["id"], _sha256(result))
        elif following is None:
            following = role
    if following is not None:
        state["dispatched_at"].setdefault(following["id"], services.clock())
    atomic_write_json(run / RUN_FILE, state)
    return state["selector"], following


def check_run(run: Path, services: Services | None = None) -> dict[str, Any]:
    """Validate results. An invalid result is set aside so a fresh reviewer can rerun that role, once.

    A Copilot CLI host still starting or running is not ready: its role is reported as running and left alone.
    Setting the role aside raises its attempt count, which a late host finds and so never promotes its result.
    """
    run = run.resolve()
    state = load_run(run)
    if run_dispatch(state) == "inline":
        verify_seal(run, state)
    if not _is_copilot_host_run(state):
        return _check_roles(run, state)
    services = services or Services()
    with host_lock(run):
        state = load_run(run)  # dispatch and the host change it under this lock
        host = host_state(run, probe=services.probe, now=services.clock())
        if host.status in {"starting", "running"}:
            return {
                "selector": state["selector"],
                "errors": {},
                "retry": [],
                "failed": {},
                "running": {state["roles"][0]["id"]: host.elapsed},
            }
        return _check_roles(run, state)


def _check_roles(run: Path, state: dict[str, Any]) -> dict[str, Any]:
    errors = role_errors(run, state)
    retry: list[dict[str, Any]] = []
    failed: dict[str, str] = {}
    seal = state.get("seal")  # an inline run's
    # An accepted role's reads are counted now; a role set aside keeps its log, which its rerun adds to.
    count_reads(run, state, [role["id"] for role in state["roles"] if role["id"] not in errors])
    for role in state["roles"]:
        identity = role["id"]
        result = Path(role["result_file"])
        if identity not in errors:
            if seal is not None:
                seal["results"].setdefault(identity, _sha256(result))
            continue
        if seal is not None:
            seal["results"].pop(identity, None)  # set aside, so next-role hands the role out again
        if result.exists():
            result.replace(result.with_name(f"{result.name}.rejected-{state['attempts'][identity] + 1}"))
        if state["attempts"][identity] < MAX_RETRIES:
            state["attempts"][identity] += 1
            retry.append(role)
        else:
            failed[identity] = errors[identity]
    atomic_write_json(run / RUN_FILE, state)
    return {"selector": state["selector"], "errors": errors, "retry": retry, "failed": failed, "running": {}}


def validate_result(run: Path, role: str) -> str | None:
    """Why check would reject this role's result, or None. It only reads: no rename, no retry counted."""
    run = run.resolve()
    state = load_run(run)
    if role not in {entry["id"] for entry in state["roles"]}:
        raise PipelineError(f"{role} is not a reviewer role of {run}")
    return role_errors(run, state).get(role)


# The exact task each reviewer receives, on the native-subagent path and in a Workflow alike.
REVIEWER_TASK = "Read {prompt} and follow it exactly. It is your complete task."
# The subagent type that runs each role, deployed from agents/ with code-review-core. Its hook runs review_guard.py.
REVIEWER_AGENT = "code-review-reviewer"
WORKFLOW_SCRIPT = """export const meta = {{
  name: 'review-prs-reviewers',
  description: 'Run every prepared reviewer role of a review-prs batch',
  phases: [{{ title: 'Review' }}],
}}
// Generated by review_pipeline.py workflow. Each role is one reviewer; check and finalize run afterwards.
// Each run: [pull request, run folder, [[role, prompt relative to the run folder, model or null, effort or null]]]
const RUNS = [
{runs}
]
const [BEFORE, AFTER, SEP] = {task_parts}
const ROLES = RUNS.flatMap(([pull, run, roles]) => roles.map(([id, prompt, model, effort]) =>
  ({{ label: pull + ' ' + id, task: BEFORE + run + SEP + prompt + AFTER, model, effort }})))
const start = (role, agentType) =>
  agent(role.task, {{ label: role.label, phase: 'Review', agentType,
    ...(role.model ? {{ model: role.model }} : {{}}), ...(role.effort ? {{ effort: role.effort }} : {{}}) }})
// A role whose guarded reviewer fails is left unfinished, never rerun unguarded: check retries it.
const replies = await parallel(ROLES.map(role => () => start(role, '{reviewer_agent}').catch(() => null)))
return {{ roles: ROLES.length, unfinished: ROLES.filter((role, index) => !replies[index]).map(role => role.label) }}
"""
# The script is passed to the Workflow tool inline: it refuses a script path in a run folder under the system temp
# directory, which is neither one it returned nor under the session's working directory.
SCRIPT_BEGIN = "BEGIN_WORKFLOW_SCRIPT"
SCRIPT_END = "END_WORKFLOW_SCRIPT"


def workflow_script(runs: list[Path]) -> tuple[Path, str, int]:
    """Write and return a Workflow script that starts every role of these runs at once, with no barrier between
    pulls. It is compact, because the orchestrator passes it to the Workflow tool as text: each role is its id,
    its prompt's path relative to the run folder, and its model, and the script builds each task from them.

    A Workflow can only start agents, so prepare, check, retries, and finalize stay with the orchestrator, and
    check remains the authority on every result.
    """
    entries: list[str] = []
    count = 0
    states = [(run.resolve(), load_run(run.resolve())) for run in runs]
    for _, state in states:
        if run_dispatch(state) == "inline":
            raise _inline_only(state)
        if state["runtime"] == "copilot-cli":
            raise PipelineError(f"{state['selector']} runs on the Copilot CLI host; dispatch it instead")
    for run, state in states:
        # Every prepared run waited for the whole batch to be prepared; its reviewers start with this script.
        mark_dispatched(run)
        # A specialist's own effort from the manifest wins over the configured default for every reviewer.
        default_effort = load_config(Path(state["config_path"])).get("reviewer_effort")
        roles = [
            [
                role["id"],
                str(Path(role["prompt_file"]).relative_to(run)),
                role.get("model"),
                role.get("effort") or default_effort,
            ]
            for role in state["roles"]
        ]
        count += len(roles)
        entries.append(json.dumps([state["selector"], str(run), roles], ensure_ascii=False))
    if not count:
        raise PipelineError("No reviewer roles to run")
    before, after = REVIEWER_TASK.split("{prompt}")
    text = WORKFLOW_SCRIPT.format(
        runs=",\n".join(entries), reviewer_agent=REVIEWER_AGENT, task_parts=json.dumps([before, after, os.sep])
    )
    # Kept with the first run for inspection; finalize removes it with the run.
    script = runs[0].resolve() / "reviewers.workflow.js"
    atomic_write_text(script, text)
    return script, text, count


WAIT_POLL_SECONDS = 2
MAX_WAIT_SECONDS = 300


def _copilot_run(run: Path) -> tuple[Path, dict[str, Any], str]:
    """A Copilot CLI host run, its state, and its one reviewer, whose name every host failure carries."""
    run = run.resolve()
    state = load_run(run)
    if run_dispatch(state) == "inline":
        raise _inline_only(state)
    if not _is_copilot_host_run(state):
        raise PipelineError("dispatch runs only a Copilot CLI entrypoint reviewer; delegate the ROLE prompts instead")
    return run, state, state["roles"][0]["id"]


def dispatch_copilot(run: Path, services: Services) -> Path:
    """Start the Copilot CLI host detached and return at once; refuse while an earlier host is still going."""
    run, _, reviewer = _copilot_run(run)
    with host_lock(run):
        host = host_state(run, probe=services.probe, now=services.clock())
        if host.status == "running":
            raise PipelineError(f"{reviewer}: the Copilot CLI host is still running (PID {host.pid})")
        if host.status == "starting":
            raise PipelineError(f"{reviewer}: the Copilot CLI host is still starting")
        claim = new_claim(run, generation=load_run(run)["attempts"][reviewer], now=services.clock())
        if claim["attempt"] == 1:
            mark_dispatched(run)
    pid = services.launch(
        [sys.executable, "-B", str(Path(__file__).resolve()), "host", "--run", str(run), "--token", claim["token"]],
        run,
        host_log_path(run, claim["attempt"]),
    )
    with host_lock(run):
        record_host_process(run, claim["token"], pid, services.probe(pid).start_time)
    return run


def run_host(run: Path, token: str, services: Services) -> str:
    """The detached host dispatch starts: run Copilot, promote its result only while the claim holds, and record
    the outcome. Returns the outcome, or `superseded` without running when the claim no longer holds."""
    run, state, reviewer = _copilot_run(run)

    def generation() -> int:
        return load_run(run)["attempts"][reviewer]

    with host_lock(run):
        claim = claim_holds(run, token, generation())
        if claim is None:
            return "superseded"
        # Dispatch records the host; this covers a dispatch stopped between starting it and recording it.
        record_host_process(run, token, os.getpid(), services.probe(os.getpid()).start_time)

    def promote(staging: Path, result: Path) -> bool:
        with host_lock(run):
            if claim_holds(run, token, generation()) is None:
                return False
            staging.replace(result)
            return True

    attempt = claim["attempt"]
    try:
        run_copilot(
            run_directory=run,
            materialized_root=Path(state["reviewer_root"]),
            request_path=Path(state["request_path"]),
            result_path=Path(state["result_path"]),
            staging_path=staging_path(run, attempt),
            snapshot_stamp=state.get("snapshot_stamp"),  # a run prepared before stamps has none
            promote=promote,
            diagnostic_path=run / f"copilot-diagnostic-{attempt}.jsonl",
            isolation_root=run / f"copilot-isolation-{attempt}",
            runner=services.copilot_runner,
            executable=services.copilot_executable,
        )
        status, reason = "dispatched", None
    except HostSuperseded as exc:
        status, reason = "superseded", str(exc)
    except EXPECTED_ERRORS as exc:
        status, reason = "failed", str(exc)
    write_outcome(run, claim, status, reason)
    return status


def wait_for_host(run: Path, timeout: int, services: Services) -> tuple[Path | None, int]:
    """Wait at most `timeout` seconds for the host: its result file when it is done, or None and the seconds it
    has run so far. A host that failed, ended without a result, or was never dispatched is a PipelineError naming
    the reviewer."""
    run, state, reviewer = _copilot_run(run)
    deadline = services.clock() + timeout
    while True:
        host = host_state(run, probe=services.probe, now=services.clock())
        if host.status == "done" and host.outcome == "dispatched":
            return Path(state["result_path"]), host.elapsed
        if host.status not in {"starting", "running"}:
            raise PipelineError(f"{reviewer}: {host.reason}")
        remaining = deadline - services.clock()
        if remaining <= 0:
            return None, host.elapsed
        services.sleep(min(WAIT_POLL_SECONDS, remaining))


# A Workflow role without a valid result this long after it was handed out is no longer waited for, so the wait ends
# even when the Workflow's completion never reaches the session. Longer than the Copilot host's 30 minutes, because a
# role's clock starts when workflow writes the script and a role queued behind the tool's concurrency limit starts late.
REVIEWER_LIMIT_SECONDS = 3600


def reviewer_progress(run: Path, state: dict[str, Any], now: float) -> dict[str, tuple[str, int]]:
    """Each role's state, `ready`, `running`, or `overdue`, and its whole seconds since it was handed out.

    It only reads, as validate-result does. An invalid result may be one its reviewer is still fixing, so it counts as
    running and is left alone: check stays the only step that sets a result aside and counts a retry.
    """
    errors = role_errors(run, state)
    started = state.get("dispatched_at", {})
    progress = {}
    for role in state["roles"]:
        elapsed = max(0, round(now - started.get(role["id"], now)))
        if role["id"] not in errors:
            status = "ready"
        elif elapsed >= REVIEWER_LIMIT_SECONDS:
            status = "overdue"
        else:
            status = "running"
        progress[role["id"]] = (status, elapsed)
    return progress


def wait_for_reviewers(
    runs: list[Path], timeout: int, services: Services
) -> tuple[list[tuple[str, dict[str, tuple[str, int]]]], dict[Path, str]]:
    """Wait at most `timeout` seconds until no role of these runs is running.

    The orchestrator runs this while the Workflow's reviewers work, so it never has to end its turn to wait: the
    skill's tool grants last only for the turn that invoked it, and check and finalize must run in that turn.
    Returns each run's selector and role progress, in the given order, and the reason for each run that cannot be read.
    """
    loaded: list[tuple[Path, dict[str, Any]]] = []
    failures: dict[Path, str] = {}
    for run in runs:
        try:
            resolved = run.resolve()
            state = load_run(resolved)
            if run_dispatch(state) == "inline":
                raise _inline_only(state)
            if _is_copilot_host_run(state):
                raise PipelineError(f"{state['selector']} runs on the Copilot CLI host; wait for it with wait")
            loaded.append((resolved, state))
        except EXPECTED_ERRORS as exc:
            failures[run] = str(exc)
    deadline = services.clock() + timeout
    while True:
        now = services.clock()
        progress = []
        for run, state in loaded:
            try:
                progress.append((run, state["selector"], reviewer_progress(run, state, now)))
            except EXPECTED_ERRORS as exc:
                failures[run] = str(exc)
        loaded = [(run, state) for run, state in loaded if run not in failures]
        running = any(status == "running" for _, _, roles in progress for status, _ in roles.values())
        remaining = deadline - services.clock()
        if not running or remaining <= 0:
            return [(selector, roles) for _, selector, roles in progress], failures
        services.sleep(min(WAIT_POLL_SECONDS, remaining))


def unfinalized_selector(run: Path) -> str | None:
    """The pull request a run directory still holds unfinalized, or None once finalize has removed the run.

    finalize is the only step that removes a published run, and prepare never prints RUN for a directory it removed,
    so a run directory that is gone was recorded, and so is one finalize marked recorded because it could not remove
    it. One that remains otherwise, after a failure or with nobody finalizing it, is its pull request's failure.
    """
    run = run.resolve()
    if not run.exists() or (run / RECORDED_FILE).is_file():
        return None
    return load_run(run)["selector"]


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reviewer_summaries(
    roles: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    attempts: dict[str, int],
    *,
    single: bool,
    seconds: dict[str, int] | None = None,
    models: dict[str, str] | None = None,
    reads: dict[str, dict[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """Which reviewers ran, the files each covered, the findings it raised, how often it was rerun, its time, and the
    snapshot files it read.

    A finding merged from several specialists counts for each of them. With `reads`, a reviewer it does not count, as
    no guard held it, reads null for both counts: unknown, not zero.
    """
    summaries = []
    for role in roles:
        raised = (
            len(findings) if single else sum(1 for finding in findings if role["id"] in source_parts(finding["source"]))
        )
        summaries.append(
            {
                "id": role["id"],
                "category": role["category"],
                "files": len(role["files"]),
                "findings": raised,
                "retries": attempts.get(role["id"], 0),
                "dispositions_only": role["dispositions_only"],
            }
        )
        if seconds and role["id"] in seconds:
            summaries[-1]["seconds"] = seconds[role["id"]]
        if models and role["id"] in models:
            summaries[-1]["model"] = models[role["id"]]
        if reads is not None:
            counted = reads.get(role["id"])
            summaries[-1]["files_read"] = None if counted is None else counted["files"]
            summaries[-1]["bytes_read"] = None if counted is None else counted["bytes"]
    return summaries


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
    reviewers = reviewer_summaries(
        roles,
        result["findings"],
        state["attempts"],
        single=state["kind"] == "entrypoint",
        seconds=seconds,
        models=models,
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
    notes = list(state["notes"])
    left = remove_recorded_run(
        run,
        {"selector": state["selector"], "json": str(json_path), "markdown": str(markdown_path)},
        services.sleep,
    )
    if left is not None:
        notes.append(left)
    return {
        "selector": state["selector"],
        "json": json_path,
        "markdown": markdown_path,
        "verdict": record["review"]["verdict"],
        "findings": len(record["findings"]),
        "canary_root": canary_root,
        "hashes": {json_path: _sha256(json_path), markdown_path: _sha256(markdown_path)} if canary_root else {},
        "stats": stats_lines(record["review"]) if canary_root else [],
        "notes": notes,
    }


def _count_or_unknown(value: int | None) -> str:
    return "unknown" if value is None else str(value)


def stats_lines(review: dict[str, Any]) -> list[str]:
    """A canary's snapshot and read counts, as finalize prints them after `STATS <selector> `: the numbers its record
    holds, and none for a field the record leaves out."""
    lines = []
    snapshot = review.get("snapshot")
    if snapshot is not None:
        seconds = " ".join(f"{phase}={value:.1f}s" for phase, value in snapshot["seconds"].items())
        # Absent from records written before exclusions were counted.
        excluded = (
            ""
            if "excluded" not in snapshot
            else " excluded="
            + (",".join(f"{reason}:{count}" for reason, count in snapshot["excluded"].items()) or "none")
        )
        lines.append(
            f"snapshot source={snapshot['source']} files={snapshot['files']} bytes={snapshot['bytes']}{excluded} "
            f"{seconds}"
        )
    for reviewer in review.get("reviewers", []):
        if "files_read" in reviewer:
            lines.append(
                f"reviewer {reviewer['id']} files_read={_count_or_unknown(reviewer['files_read'])} "
                f"bytes_read={_count_or_unknown(reviewer['bytes_read'])}"
            )
    return lines


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
            size = config["repositories"][repository]["batch_size"] if batch_size is None else batch_size
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
        candidate = safe_watermark(
            previous=previous,
            today=today,
            eligible_merged=merged,
            completed_numbers=completed,
        )
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


def _print_ready(result: dict[str, Any]) -> None:
    print(f"RUN {result['selector']} {result['run']}")
    for note in result["notes"]:
        print(f"NOTE {result['selector']} {note}")
    if result["dispatch"] == "copilot-host":
        print(f"HOST copilot-cli {result['run']}")
        return
    if result["dispatch"] == "inline":
        print(f"INLINE {result['run']}")
        return
    for role in result["roles"]:
        print(f"ROLE {role['id']} {role['prompt_file']}")
        _print_model(result["selector"], role)


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


def _print_model(selector: str, role: dict[str, Any]) -> None:
    """The model a role's trusted profile asks for; the orchestrator starts that role's subagent with it."""
    if role.get("model"):
        print(f"MODEL {selector} {role['id']} {role['model']}")


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
            _print_model(selector, role)
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
            f"RECORDED {result['selector']} verdict={result['verdict']} findings={result['findings']} "
            f"{result['markdown']}"
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

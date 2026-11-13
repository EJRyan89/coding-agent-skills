"""Working a run's roles: checking results, handing out inline roles, Workflow scripts, and the Copilot CLI host."""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import file_presence
import json_reader
import json_writer
from path_keys import path_key
from review_config import ConfigurationError, load_config
from review_guard import PLUGIN, UNGUARDED, claimed, is_search_entry, read_log, reviewer_agent_type
from review_hosts import (
    HostSuperseded,
    claim_holds,
    host_lock,
    host_log_path,
    host_state,
    materialized_reviewer_entrypoint,
    new_claim,
    record_host_process,
    run_copilot,
    staging_path,
    write_outcome,
)
from review_io import CONTENT_ERRORS, PersistenceError, atomic_write_json, atomic_write_text, read_diff, read_json
from review_operation import prior_severities
from review_records import RecordError, validate_adapter_result
from review_run import (
    EXPECTED_ERRORS,
    MAX_RETRIES,
    PIPELINE_SCRIPT,
    RECORDED_FILE,
    RUN_FILE,
    PipelineError,
    ReadCount,
    RunRole,
    RunState,
    Services,
    _inline_only,
    _inside,
    _is_copilot_host_run,
    _sha256,
    load_run,
    mark_dispatched,
    run_dispatch,
    verify_seal,
)
from review_runtime import GENERIC_SPECIALIST, SOURCE_SNAPSHOT_MANIFEST, RuntimeContractError, _snapshot_files
from review_specialists import (
    GENERIC_INSTRUCTIONS_KEY,
    SpecialistError,
    check,
    load_materialized_manifest,
    parse_unified_diff,
    verify_generic_instructions,
)

# The record of what prepare wrote under a reviewer root, which lists every other file there.
MATERIALIZATION_FILE = "materialization.json"
# What the reviewer's checks raise when a file is not what prepare materialized, a refusal they word through
# json_writer, as of a name a lone surrogate holds, included.
REVIEWER_REFUSALS: tuple[type[Exception], ...] = (
    SpecialistError,
    RuntimeContractError,
    PersistenceError,
    OSError,
    *json_writer.CONTENT_ERRORS,
)


def verify_reviewer(state: RunState) -> None:
    """Fail a run whose reviewer no longer holds what prepare materialized, in every dispatch mode.

    Reviewers follow the files under the run's reviewer root, a specialist's suite profile where the suite keeps it,
    and the generic reviewer's instructions where the suite keeps them, when they review, while the record names the
    hashes prepare took. A reviewer's prompt makes every trusted file under the root its instructions, so a file added
    there reaches it as surely as one changed. So every file is hashed again: an entrypoint reviewer's by the check the
    Copilot CLI host runs when it starts, and a specialists reviewer's by the one prepare ran, which covers its suite
    profiles too; the root must hold only the files its materialization lists; and the generic reviewer's instructions,
    whenever a generic role is planned, alone or beside specialists, must still have the hash prepare recorded. The
    hashes the record will name must then be the materialization's and that one. A file changed, added, or gone since
    prepare fails the pull request with nothing recorded, as an inline run's seal does, naming a changed or missing
    file, and an added one for a specialists reviewer; an entrypoint reviewer's check names no added file. A run
    prepared before the hashes were recorded, whose record names none, is held to its files alone.
    """
    selector = state["selector"]
    recorded = state["adapter"].get("source_hashes")
    generic = GENERIC_SPECIALIST in {role["id"] for role in state["roles"]}
    try:
        if generic and recorded is not None:
            verify_generic_instructions(recorded)
        materialized = _materialized_hashes(state)
    except REVIEWER_REFUSALS as exc:
        raise PipelineError(f"{selector}: the reviewer is not what prepare materialized: {exc}") from exc
    if recorded is None:  # a run prepared before the hashes were recorded, whose record names none
        return
    if generic:  # just checked against the file the role follows
        materialized[GENERIC_INSTRUCTIONS_KEY] = recorded[GENERIC_INSTRUCTIONS_KEY]
    differing = sorted(
        path for path in materialized.keys() | recorded.keys() if materialized.get(path) != recorded.get(path)
    )
    if differing:
        raise PipelineError(
            f"{selector}: the reviewer hashes the record would name are not the materialized reviewer's: "
            + ", ".join(differing)
        )


def _materialized_hashes(state: RunState) -> dict[str, str]:
    """The hashes of the files under the run's reviewer root and of its suite profiles, each checked against the file
    again, refusing a file the root holds that its materialization does not list; none without a reviewer root."""
    if state["reviewer_root"] is None:
        return {}
    root = Path(state["reviewer_root"])
    if state["kind"] == "entrypoint":
        materialized_reviewer_entrypoint(root)  # which also refuses a file it does not list
    else:
        load_materialized_manifest(root)
    metadata = read_json(root / MATERIALIZATION_FILE)
    added = sorted(_snapshot_files(root) - {*metadata["source_hashes"], MATERIALIZATION_FILE})
    if added:
        raise SpecialistError(f"files it does not list: {', '.join(added)}")
    return {**metadata["source_hashes"], **metadata.get("suite_profiles", {})}


def role_errors(run: Path, state: RunState) -> dict[str, str]:
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


def count_reads(run: Path, state: RunState, roles: Sequence[str]) -> bool:
    """Reduce each named role's read log, which the reviewer guard writes, to the distinct snapshot files it names and
    their bytes and the searches it holds, add them to the role's counts in run.json's `reads`, and delete the log, so
    no path outlives this step. Returns whether any log was counted. A role counted before searches were keeps no
    `searches`, since its earlier searches are unknown. A role without a log is not counted: no guard ran for it, and on
    Claude Code `unguarded_roles` refuses its result. A run prepared before reads were counted has no `reads` and
    counts nothing. The snapshot's own manifest is not a file of the snapshot, as `review.snapshot.files` leaves it
    out, so reading it counts for nothing. A logged path where nothing is, or a folder, which a search names, is not a
    file read and counts for nothing; one it cannot stat raises the stat's OSError before any log is counted or
    deleted, so the step fails naming it and a rerun counts every log whole, never a count short of a file the reviewer
    read."""
    if "reads" not in state:
        return False
    read = [
        (role, log, *_read_log(log, run / "source"))
        for role in roles
        if file_presence.is_present(log := read_log(run, role))
    ]
    for role, log, sizes, searches in read:
        total: ReadCount = state["reads"].get(role, {"files": 0, "bytes": 0, "searches": 0})
        count: ReadCount = {"files": total["files"] + len(sizes), "bytes": total["bytes"] + sum(sizes.values())}
        if "searches" in total:
            count["searches"] = total["searches"] + searches
        state["reads"][role] = count
        log.unlink()
    return bool(read)


def _read_log(log: Path, source: Path) -> tuple[dict[str, int], int]:
    """A read log's distinct snapshot files with their bytes, keyed as the file system compares paths, and the count
    of its searches."""
    manifest = path_key(source / SOURCE_SNAPSHOT_MANIFEST, textual=True)
    sizes: dict[str, int] = {}
    searches = 0
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        with contextlib.suppress(*CONTENT_ERRORS):
            relative = json_reader.loads(line)
            searches += is_search_entry(relative)
            # A path holding a NUL names no file, which file_presence refuses as an OSError: count it as nothing.
            target = source.joinpath(relative) if isinstance(relative, str) and "\0" not in relative else source
            key = path_key(target, textual=True)
            if (
                key not in sizes
                and key != manifest
                and _inside(target, source)
                and file_presence.is_present(target, other_is_absent=True)
            ):
                sizes[key] = target.stat().st_size
    return sizes, searches


# What a refused role's reviewer does instead of the agent: work the review inline, where the seal holds it.
UNGUARDED_REMEDY = "prepare the review again with --inline"


def guard_runs(runtime: str, dispatch: str) -> bool:
    """Whether the reviewer guard holds a run's roles: only on Claude Code, to a role dispatched as a native subagent,
    one by one or by a Workflow. An inline role, the Copilot CLI host, and a Codex subagent run no guard."""
    return runtime == "claude-code" and dispatch == "subagents"


def unguarded_roles(run: Path, state: RunState, roles: Sequence[str]) -> dict[str, str]:
    """Each of these roles that its guard should have claimed and never did, with the reason its result is refused.

    On Claude Code a role dispatched as a native subagent, whether started one by one or by a Workflow, runs as the
    code-review-reviewer agent, whose hook, or the plugin's, runs the guard; the guard's claim creates the role's read
    log, and check counts the log into run.json's `reads` and deletes it. A role with neither ran with no hook, bound
    by its prompt alone. An inline role, the Copilot CLI host, and a Codex subagent run no guard, so they are never
    held to a claim.
    """
    if not guard_runs(state["runtime"], run_dispatch(state)):
        return {}
    counted = state.get("reads", {})
    reason = UNGUARDED.format(remedy=UNGUARDED_REMEDY)
    return {role: reason for role in roles if role not in counted and not claimed(run, role)}


def reviewer_seconds(state: RunState) -> dict[str, int]:
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


def next_role(run: Path, services: Services) -> tuple[str, RunRole | None]:
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
    verify_reviewer(state)  # a suite profile lies outside the seal
    following = None
    for role in state["roles"]:
        result = Path(role["result_file"])
        # A result it cannot stat is the stat's OSError, never a role without one to hand out again unsealed.
        if file_presence.is_present(result):
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
                "runtime": state["runtime"],
            }
        return _check_roles(run, state)


def _check_roles(run: Path, state: RunState) -> dict[str, Any]:
    verify_reviewer(state)  # before any result counts
    errors = role_errors(run, state)
    errors.update(unguarded_roles(run, state, [role["id"] for role in state["roles"] if role["id"] not in errors]))
    retry: list[RunRole] = []
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
        if file_presence.is_occupied(result):  # a result it cannot stat raises, never stays to pass as the retry's
            result.replace(result.with_name(f"{result.name}.rejected-{state['attempts'][identity] + 1}"))
        if state["attempts"][identity] < MAX_RETRIES:
            state["attempts"][identity] += 1
            retry.append(role)
        else:
            failed[identity] = errors[identity]
    atomic_write_json(run / RUN_FILE, state)
    return {
        "selector": state["selector"],
        "errors": errors,
        "retry": retry,
        "failed": failed,
        "running": {},
        "runtime": state["runtime"],
    }


def validate_result(run: Path, role: str) -> str | None:
    """Why check would reject this role's result for what it holds, or None. It only reads: no rename, no retry
    counted. It is the reviewer's self-check, so it leaves the guard's claim to check: a reviewer is never told how to
    show one."""
    run = run.resolve()
    state = load_run(run)
    if role not in {entry["id"] for entry in state["roles"]}:
        raise PipelineError(f"{role} is not a reviewer role of {run}")
    return role_errors(run, state).get(role)


# The exact task each reviewer receives, on the native-subagent path and in a Workflow alike.
REVIEWER_TASK = "Read {prompt} and follow it exactly. It is your complete task."


# The subagent type that runs each role, deployed from agents/ with ejryan89-code-review-core, or namespaced by the
# plugin these scripts run from. Its hook, or the plugin's, runs review_guard.py.
REVIEWER_AGENT = reviewer_agent_type(PLUGIN)


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
    # Every configuration is read before any run is marked, so one that cannot be read leaves every run as it was.
    efforts = [load_config(Path(state["config_path"])).get("reviewer_effort") for _, state in states]
    for (run, state), default_effort in zip(states, efforts, strict=True):
        # Every prepared run waited for the whole batch to be prepared; its reviewers start with this script.
        mark_dispatched(run)
        # A specialist's own effort from the manifest wins over the configured default for every reviewer.
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
        entries.append(json_writer.dumps([state["selector"], str(run), roles], ensure_ascii=False))
    if not count:
        raise PipelineError("No reviewer roles to run")
    before, after = REVIEWER_TASK.split("{prompt}")
    text = WORKFLOW_SCRIPT.format(
        runs=",\n".join(entries), reviewer_agent=REVIEWER_AGENT, task_parts=json_writer.dumps([before, after, os.sep])
    )
    # Kept with the first run for inspection; finalize removes it with the run.
    script = runs[0].resolve() / "reviewers.workflow.js"
    atomic_write_text(script, text)
    return script, text, count


WAIT_POLL_SECONDS = 2


MAX_WAIT_SECONDS = 300


def _copilot_run(run: Path) -> tuple[Path, RunState, str]:
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
        [sys.executable, "-B", str(PIPELINE_SCRIPT), "host", "--run", str(run), "--token", claim["token"]],
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
            # A host run's reviewer is an entrypoint, which prepare materialized, so its root is recorded.
            materialized_root=Path(cast(str, state["reviewer_root"])),
            request_path=Path(state["request_path"]),
            result_path=Path(state["result_path"]),
            staging_path=staging_path(run, attempt),
            snapshot_stamp=state.get("snapshot_stamp"),  # a run prepared before stamps has none
            effort=state["roles"][0].get("effort"),  # a run prepared before efforts reached the host has none
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


def reviewer_progress(run: Path, state: RunState, now: float) -> dict[str, tuple[str, int]]:
    """Each role's state, `ready`, `running`, or `overdue`, and its whole seconds since it was handed out.

    It only reads, as validate-result does. An invalid result may be one its reviewer is still fixing, so it counts as
    running and is left alone: check stays the only step that sets a result aside and counts a retry, and the only
    one that holds a role to its guard's claim, so a valid result the guard never claimed is ready for check to refuse.
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
    loaded: list[tuple[Path, RunState]] = []
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
    it. One that remains otherwise, after a failure or with nobody finalizing it, is its pull request's failure, and
    one it cannot stat is the OSError file_presence raises, never a recorded run.
    """
    run = run.resolve()
    if not file_presence.is_directory(run) or file_presence.is_present(run / RECORDED_FILE):
        return None
    return load_run(run)["selector"]

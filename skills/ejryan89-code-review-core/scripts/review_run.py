"""A prepared review run: its errors, its external services, run.json, its dispatch, and its seal."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import stat
import sys
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any, NotRequired, TypedDict, TypeVar, cast, get_origin, get_type_hints

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from executable_locator import PYTHON
from git_client import Runner, subprocess_runner
from github_client import GitHubError
from link_probe import is_plain_file
from process_identity import ProcessStatus, process_status
from review_archive import ArchiveError
from review_canary import FixtureError
from review_config import ConfigurationError, load_config
from review_flags import FlagError
from review_github import GitHubClient
from review_hosts import Runner as CopilotRunner
from review_hosts import subprocess_runner as copilot_subprocess_runner
from review_io import PersistenceError, atomic_write_json, read_json
from review_operation import ReviewOperationError
from review_process import start_detached
from review_records import ADAPTER_SOURCES, RE_REVIEW_SCOPES, SCOPE_FIELDS, RecordError, Scope, read_repository_path
from review_runtime import (
    MODEL_ALIAS_RUNTIMES,
    NATIVE_EFFORT_RUNTIMES,
    RuntimeContractError,
    github_tarball_fetcher,
    resolve_runtime,
)
from review_specialists import SpecialistError
from review_state import StateError, utc_today
from review_vocabularies import DISPATCH_MODES, DispatchMode
from typed_fields import FieldError, FieldReader, describe

T = TypeVar("T")

# The pipeline's command line, which a role's prompt names and the Copilot CLI host is started as.
PIPELINE_SCRIPT = Path(__file__).resolve().parent / "review_pipeline.py"
RUN_SCHEMA_VERSION = 1


RUN_FILE = "run.json"


# A fixture's throwaway repository, moved into its run, which a lazy snapshot's reviewers fetch from.
SOURCE_REPOSITORY = "repository"


# Written in a run once finalize records it, so a run folder that survives its removal is not reported unfinalized.
RECORDED_FILE = "recorded.json"


MAX_RETRIES = 1


# A reviewer runs this on its own result before replying; check stays authoritative. Python is named as
# review-prs's rendered grant names it, by the platform's command.
SELF_CHECK_COMMAND = PYTHON + ' -B "{script}" validate-result --run "{run}" --role "{role}"'


def remove_run(run: Path, *, ignore_errors: bool = False) -> None:
    """Remove a run folder, its fixture repository included, whose object files git leaves read-only, which Windows
    will not delete until they are writable again."""
    with contextlib.suppress(OSError):
        for path in (run / SOURCE_REPOSITORY).rglob("*"):
            if is_plain_file(path):
                path.chmod(stat.S_IREAD | stat.S_IWRITE)
    shutil.rmtree(run, ignore_errors=ignore_errors)


def command_script(name: str) -> str:
    """The path a role's prompt gives for this folder's script `name`, spelled as this process was started.

    `review-prs` grants its pipeline commands as text, `${CLAUDE_SKILL_DIR}/../ejryan89-code-review-core/scripts/` with
    the skill's directory filled in, and an inline role runs its prompt's commands in that same session, so a command
    spelled as the session started `prepare` matches the grant where the resolved path, with no `..`, never does.
    The reviewer guard resolves either spelling to the same script. A start the prompt cannot reuse falls back to the
    resolved path: a relative path, a path to another file, or one holding a character a quoted command cannot."""
    pipeline = PIPELINE_SCRIPT
    resolved = str(pipeline.parent / name)
    started = sys.argv[0] if sys.argv else ""
    if not started.endswith(pipeline.name) or not Path(started).is_absolute() or set(started) & set('"`$\r\n'):
        return resolved
    if os.path.normcase(str(Path(started).resolve())) != os.path.normcase(str(pipeline)):
        return resolved
    return started.removesuffix(pipeline.name) + name


def self_check_command(run: Path, role: str) -> str:
    return SELF_CHECK_COMMAND.format(script=command_script("review_pipeline.py"), run=run, role=role)


class RunRole(TypedDict):
    """One reviewer role of a run: its prompt, where its result goes, and, for a specialist, the model and effort it
    starts on, either of which prepare may set to None."""

    id: str
    prompt_file: str
    result_file: str
    model: NotRequired[str | None]
    effort: NotRequired[str | None]


class ReadCount(TypedDict):
    """The distinct snapshot files a role read, and their bytes."""

    files: int
    bytes: int


class Seal(TypedDict):
    """An inline run's sealed files by path relative to the run, and each result next-role sealed by its role."""

    files: dict[str, str]
    results: dict[str, str]


class RunState(TypedDict):
    """A prepared run's run.json, as prepare writes it and every later step reads and updates it.

    A run prepared before a field was recorded lacks it: its dispatch, the roles' dispatch times, the snapshot, its
    repository and stamp, the read counts, the archive base, which finalize then refuses to record without, the
    changed documents, and an inline run's seal. The reviewer adapter and a fixture canary's paths are read field by
    field, though typed as the mappings the steps that use them take; the patches, the archive base, and the snapshot
    stay open mappings, which those steps read.
    """

    schema_version: int
    selector: str
    mode: str
    canary: bool
    fixture: dict[str, Any] | None
    config_path: str
    host: str | None
    runtime: str
    dispatch: NotRequired[DispatchMode]
    kind: str
    request_path: str
    reviewer_root: str | None
    result_path: str
    adapter: dict[str, Any]
    roles: list[RunRole]
    attempts: dict[str, int]
    dispatched_at: NotRequired[dict[str, float]]
    snapshot: NotRequired[dict[str, Any]]
    source_repository: NotRequired[str | None]
    snapshot_stamp: NotRequired[str | None]
    reads: NotRequired[dict[str, ReadCount]]
    notes: list[str]
    patches: dict[str, dict[str, Any]]
    scope: Scope | None
    uncovered_files: list[str]
    archive_base: NotRequired[dict[str, Any]]
    documents: NotRequired[dict[str, list[str]]]
    seal: NotRequired[Seal]


def _fields(shape: type) -> tuple[frozenset[str], frozenset[str]]:
    """A TypedDict's required and optional fields, read from its annotations, which this module's postponed
    evaluation leaves as text its `__optional_keys__` does not see."""
    hints = get_type_hints(shape, include_extras=True)
    optional = frozenset(name for name, hint in hints.items() if get_origin(hint) is NotRequired)
    return frozenset(hints) - optional, optional


RUN_FIELDS, RUN_OPTIONAL_FIELDS = _fields(RunState)
ROLE_FIELDS, ROLE_OPTIONAL_FIELDS = _fields(RunRole)
# The reviewer adapter the record will name. A run prepared before its source, commit, and hashes were recorded lacks
# them, and the record then takes no source, no commit, and no hashes.
ADAPTER_FIELDS = frozenset({"name", "scope"})
ADAPTER_OPTIONAL_FIELDS = frozenset({"source", "source_commit", "source_hashes"})
# A fixture canary's directory, the prior record it was given, and the run's copy of that record, which a run
# prepared before the copy was kept lacks.
FIXTURE_FIELDS = frozenset({"directory", "prior"})
FIXTURE_OPTIONAL_FIELDS = frozenset({"prior_record"})


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


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _write_run(run: Path, state: RunState) -> None:
    """Write a prepared run's run.json. A run that cannot start a role on the model it asks for first notes that model
    in place of its `MODEL` line, so the role runs on the session's model; a run whose reviewers start at an effort
    they are given records each role's, its specialist's else the configuration's; an inline run then seals every
    file prepare wrote."""
    reason = _unapplied_model_reason(state["runtime"], state["dispatch"])
    if reason is not None:
        for role in state["roles"]:
            if role.get("model"):
                state["notes"].append(f"{role['id']} asks for model {role['model']}; {reason}")
                role["model"] = None
    if _starts_at_given_effort(state["runtime"], state["dispatch"]):
        default_effort = load_config(Path(state["config_path"])).get("reviewer_effort")
        for role in state["roles"]:
            role["effort"] = role.get("effort") or default_effort
    if state["dispatch"] == "inline":
        state["seal"] = {"files": sealed_files(run, state), "results": {}}
    atomic_write_json(run / RUN_FILE, state)


def _unapplied_model_reason(runtime: str, dispatch: DispatchMode) -> str | None:
    """Why no role of a run dispatched this way on this runtime starts on a model alias, or None when every role can."""
    if dispatch == "inline":
        # The orchestrating session works every role on its own model; no subagent can be started on another.
        return "an inline review works every role on this session's model"
    if runtime not in MODEL_ALIAS_RUNTIMES:
        return f"{runtime} starts every reviewer on this session's model"
    return None


def _starts_at_given_effort(runtime: str, dispatch: DispatchMode) -> bool:
    """Whether a run's reviewers start at a reasoning effort the pipeline gives them: a native subagent of a runtime in
    NATIVE_EFFORT_RUNTIMES, from its `EFFORT` line, or the Copilot CLI host, from its command. The Workflow tool reads
    the configured effort when its script is written, and an inline role works at the session's."""
    return dispatch == "copilot-host" or (dispatch == "subagents" and runtime in NATIVE_EFFORT_RUNTIMES)


def load_run(run: Path) -> RunState:
    """A prepared run's state, read field by field once its schema version is one this pipeline prepares: a field
    missing, unknown, or of the wrong kind or shape fails with a PipelineFieldError naming the first such field.

    A run prepared before an optional field was recorded loads without it, as each step that reads the field expects.
    """
    state = READ.json_object(read_json(run / RUN_FILE), "run")
    if READ.integer(state.get("schema_version"), "run.schema_version") != RUN_SCHEMA_VERSION:
        raise PipelineError(f"{run} is not a prepared review run")
    return _read_run(READ.json_object(state, "run", required=RUN_FIELDS, optional=RUN_OPTIONAL_FIELDS))


def _read_run(state: dict[str, Any]) -> RunState:
    roles = READ.array(state["roles"], "run.roles", _read_role)
    identities: set[str] = set()
    for index, role in enumerate(roles):
        if role["id"] in identities:
            raise READ.fail(f"run.roles[{index}].id", "a role id no earlier role has", describe(role["id"]))
        identities.add(role["id"])
    read: RunState = {
        "schema_version": state["schema_version"],
        "selector": READ.string(state["selector"], "run.selector", single_line=True),
        "mode": READ.one_of(state["mode"], "run.mode", ("initial", "re-review")),
        "canary": READ.boolean(state["canary"], "run.canary"),
        "fixture": READ.optional(_read_fixture, state["fixture"], "run.fixture"),
        "config_path": READ.string(state["config_path"], "run.config_path"),
        "host": READ.optional(READ.string, state["host"], "run.host"),
        "runtime": READ.string(state["runtime"], "run.runtime"),
        "kind": READ.one_of(state["kind"], "run.kind", ("entrypoint", "generic", "specialists")),
        "request_path": READ.string(state["request_path"], "run.request_path"),
        "reviewer_root": READ.optional(READ.string, state["reviewer_root"], "run.reviewer_root"),
        "result_path": READ.string(state["result_path"], "run.result_path"),
        "adapter": _read_adapter(state["adapter"], "run.adapter"),
        "roles": roles,
        "attempts": _by_role(state["attempts"], "run.attempts", identities, _attempt_count, every=True),
        "notes": READ.array(state["notes"], "run.notes", READ.string),
        "patches": _object_of(state["patches"], "run.patches", READ.json_object),
        "scope": READ.optional(_read_scope, state["scope"], "run.scope"),
        "uncovered_files": READ.array(
            state["uncovered_files"], "run.uncovered_files", partial(read_repository_path, READ)
        ),
    }
    _read_optional(state, read, identities)
    return read


def _read_optional(state: dict[str, Any], read: RunState, identities: set[str]) -> None:
    """Add to `read` each field a run prepared before it was recorded may lack, read as its kind when it is there."""
    if "dispatch" in state:
        read["dispatch"] = READ.one_of(state["dispatch"], "run.dispatch", DISPATCH_MODES)
    if "dispatched_at" in state:
        read["dispatched_at"] = _by_role(state["dispatched_at"], "run.dispatched_at", identities, READ.number)
    if "snapshot" in state:
        read["snapshot"] = READ.json_object(state["snapshot"], "run.snapshot")
    if "source_repository" in state:
        read["source_repository"] = READ.optional(READ.string, state["source_repository"], "run.source_repository")
    if "snapshot_stamp" in state:
        read["snapshot_stamp"] = READ.optional(READ.string, state["snapshot_stamp"], "run.snapshot_stamp")
    if "reads" in state:
        read["reads"] = _by_role(state["reads"], "run.reads", identities, _read_count)
    if "archive_base" in state:
        read["archive_base"] = READ.json_object(state["archive_base"], "run.archive_base")
    if "documents" in state:
        read["documents"] = _object_of(state["documents"], "run.documents", _document_places)
    if "seal" in state:
        read["seal"] = _read_seal(state["seal"], "run.seal", identities)


def _read_adapter(value: object, path: str) -> dict[str, Any]:
    adapter = READ.json_object(value, path, required=ADAPTER_FIELDS, optional=ADAPTER_OPTIONAL_FIELDS)
    read: dict[str, Any] = {
        "name": READ.string(adapter["name"], f"{path}.name"),
        "scope": READ.one_of(adapter["scope"], f"{path}.scope", ("generic", "repository")),
    }
    if "source" in adapter:
        read["source"] = READ.one_of(adapter["source"], f"{path}.source", tuple(ADAPTER_SOURCES))
    if "source_commit" in adapter:
        read["source_commit"] = READ.optional(
            READ.hex_digest, adapter["source_commit"], f"{path}.source_commit", 40, 64
        )
    if "source_hashes" in adapter:
        read["source_hashes"] = _object_of(adapter["source_hashes"], f"{path}.source_hashes", _sha256_digest)
    return read


def _read_fixture(value: object, path: str) -> dict[str, Any]:
    fixture = READ.json_object(value, path, required=FIXTURE_FIELDS, optional=FIXTURE_OPTIONAL_FIELDS)
    read: dict[str, Any] = {
        "directory": READ.string(fixture["directory"], f"{path}.directory"),
        "prior": READ.optional(READ.string, fixture["prior"], f"{path}.prior"),
    }
    if "prior_record" in fixture:
        read["prior_record"] = READ.optional(READ.string, fixture["prior_record"], f"{path}.prior_record")
    return read


def _read_role(value: object, path: str) -> RunRole:
    entry = READ.json_object(value, path, required=ROLE_FIELDS, optional=ROLE_OPTIONAL_FIELDS)
    role: RunRole = {
        "id": READ.string(entry["id"], f"{path}.id", single_line=True, trimmed=True),
        "prompt_file": READ.string(entry["prompt_file"], f"{path}.prompt_file"),
        "result_file": READ.string(entry["result_file"], f"{path}.result_file"),
    }
    if "model" in entry:
        role["model"] = READ.optional(READ.string, entry["model"], f"{path}.model")
    if "effort" in entry:
        role["effort"] = READ.optional(READ.string, entry["effort"], f"{path}.effort")
    return role


def _object_of(value: object, path: str, read: Callable[[object, str], T]) -> dict[str, T]:
    """An object whose every value `read` reads at `path.<key>`."""
    return {key: read(entry, f"{path}.{key}") for key, entry in READ.json_object(value, path).items()}


def _by_role(
    value: object, path: str, roles: Collection[str], read: Callable[[object, str], T], *, every: bool = False
) -> dict[str, T]:
    """An object keyed by the run's roles, every one of them when `every`, whose every value `read` reads."""
    return _object_of(READ.json_object(value, path, required=roles if every else (), optional=roles), path, read)


def _attempt_count(value: object, path: str) -> int:
    return READ.integer(value, path, minimum=0)


def _read_count(value: object, path: str) -> ReadCount:
    count = READ.json_object(value, path, required=("files", "bytes"))
    return {
        "files": READ.integer(count["files"], f"{path}.files", minimum=0),
        "bytes": READ.integer(count["bytes"], f"{path}.bytes", minimum=0),
    }


def _document_places(value: object, path: str) -> list[str]:
    """A changed document's place of each line, blank for a line that has none."""
    return READ.array(value, path, lambda place, at: READ.string(place, at, empty=True))


def _sha256_digest(value: object, path: str) -> str:
    return READ.hex_digest(value, path, 64)


def _read_seal(value: object, path: str, roles: Collection[str]) -> Seal:
    seal = READ.json_object(value, path, required=("files", "results"))
    return {
        "files": _object_of(seal["files"], f"{path}.files", _sha256_digest),
        "results": _by_role(seal["results"], f"{path}.results", roles, _sha256_digest),
    }


def _read_scope(value: object, path: str) -> Scope:
    """A re-review's scope as its record will hold it; the record's own validation checks the counts agree."""
    scope = READ.json_object(value, path, required=SCOPE_FIELDS)
    return {
        "requested": READ.one_of(scope["requested"], f"{path}.requested", RE_REVIEW_SCOPES),
        "used": READ.one_of(scope["used"], f"{path}.used", ("full", "incremental")),
        "reason": READ.string(scope["reason"], f"{path}.reason"),
        "since_version": READ.integer(scope["since_version"], f"{path}.since_version"),
        "files_changed": READ.optional(READ.integer, scope["files_changed"], f"{path}.files_changed", minimum=0),
        "files_total": READ.integer(scope["files_total"], f"{path}.files_total", minimum=0),
        "lines_changed": READ.optional(READ.integer, scope["lines_changed"], f"{path}.lines_changed", minimum=0),
        "lines_total": READ.integer(scope["lines_total"], f"{path}.lines_total", minimum=0),
    }


def mark_dispatched(run: Path) -> None:
    """Time every role of a run from now: its reviewers are about to start.

    A reviewer's time runs from here to its accepted result, so a rerun counts toward it and is never restarted.
    """
    state = load_run(run)
    now = time.time()
    state["dispatched_at"] = {role["id"]: now for role in state["roles"]}
    atomic_write_json(run / RUN_FILE, state)


def run_dispatch(state: Mapping[str, object]) -> DispatchMode:
    """How a run's roles are worked. A run prepared before the dispatch was recorded was dispatched as its runtime
    and reviewer kind decided then: a Copilot CLI entrypoint on its host, everything else as subagents."""
    if "dispatch" in state:
        return cast(DispatchMode, str(state["dispatch"]))  # prepare wrote it, and load_run reads it, as one
    return "copilot-host" if state["runtime"] == "copilot-cli" and state["kind"] == "entrypoint" else "subagents"


def _is_copilot_host_run(state: RunState) -> bool:
    return run_dispatch(state) == "copilot-host"


def _inline_only(state: RunState) -> PipelineError:
    return PipelineError(f"{state['selector']} is reviewed inline; work its roles with next-role")


def sealed_files(run: Path, state: RunState) -> dict[str, str]:
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


def verify_seal(run: Path, state: RunState) -> None:
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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

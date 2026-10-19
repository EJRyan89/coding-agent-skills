"""A prepared review run: its errors, its external services, run.json, its dispatch, and its seal."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import stat
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from executable_locator import PYTHON
from git_client import Runner, subprocess_runner
from process_identity import ProcessStatus, process_status
from review_archive import ArchiveError
from review_canary import FixtureError
from review_config import ConfigurationError, load_config
from review_flags import FlagError
from review_github import GitHubClient, GitHubError
from review_hosts import Runner as CopilotRunner
from review_hosts import subprocess_runner as copilot_subprocess_runner
from review_io import PersistenceError, atomic_write_json, read_json
from review_operation import ReviewOperationError
from review_process import start_detached
from review_records import RecordError
from review_runtime import (
    MODEL_ALIAS_RUNTIMES,
    NATIVE_EFFORT_RUNTIMES,
    RuntimeContractError,
    github_tarball_fetcher,
    resolve_runtime,
)
from review_specialists import SpecialistError
from review_state import StateError, utc_today
from typed_fields import FieldError, FieldReader

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


def _write_run(run: Path, state: dict[str, Any]) -> None:
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


def _unapplied_model_reason(runtime: str, dispatch: str) -> str | None:
    """Why no role of a run dispatched this way on this runtime starts on a model alias, or None when every role can."""
    if dispatch == "inline":
        # The orchestrating session works every role on its own model; no subagent can be started on another.
        return "an inline review works every role on this session's model"
    if runtime not in MODEL_ALIAS_RUNTIMES:
        return f"{runtime} starts every reviewer on this session's model"
    return None


def _starts_at_given_effort(runtime: str, dispatch: str) -> bool:
    """Whether a run's reviewers start at a reasoning effort the pipeline gives them: a native subagent of a runtime in
    NATIVE_EFFORT_RUNTIMES, from its `EFFORT` line, or the Copilot CLI host, from its command. The Workflow tool reads
    the configured effort when its script is written, and an inline role works at the session's."""
    return dispatch == "copilot-host" or (dispatch == "subagents" and runtime in NATIVE_EFFORT_RUNTIMES)


def load_run(run: Path) -> dict[str, Any]:
    state = READ.json_object(read_json(run / RUN_FILE), "run")
    if READ.integer(state.get("schema_version"), "run.schema_version") != RUN_SCHEMA_VERSION:
        raise PipelineError(f"{run} is not a prepared review run")
    return state


def mark_dispatched(run: Path) -> None:
    """Time every role of a run from now: its reviewers are about to start.

    A reviewer's time runs from here to its accepted result, so a rerun counts toward it and is never restarted.
    """
    state = load_run(run)
    now = time.time()
    state["dispatched_at"] = {role["id"]: now for role in state["roles"]}
    atomic_write_json(run / RUN_FILE, state)


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

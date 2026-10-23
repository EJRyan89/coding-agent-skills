"""Closed runtime-host interface; native hosts delegate, Copilot CLI has a bounded driver.

The Copilot CLI host runs detached from the command that starts it. Its run directory holds:

    copilot-host.json            the current claim: attempt, token, generation, and the host's PID and start time
    copilot-host-N.log           the detached host's own output
    copilot-result-N.json        the result Copilot writes; the host renames it into place only while its claim holds
    copilot-outcome-N.json       the host's verdict: dispatched, failed, or superseded, with the reason
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from bounded_process import run_bounded, without_repository_variables
from executable_locator import find_executables
from job_tree import JobRefused, process_tree
from process_identity import ProcessStatus
from review_config import REVIEWER_EFFORTS
from review_io import (
    CONTENT_ERRORS,
    ResourceLock,
    atomic_write_json,
    atomic_write_text,
    parse_json,
    read_diff,
    read_json,
)
from review_process import same_process, shell_script
from review_runtime import (
    READ,
    SOURCE_SNAPSHOT_MANIFEST,
    RuntimeContractError,
    _require_safe_snapshot_path,
    _safe_relative_path,
    _snapshot_files,
    negotiate_capabilities,
    verify_stamped_snapshot,
)
from review_specialists import parse_unified_diff, render_entrypoint_prompt, symbolic_links


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


# A command's arguments, working directory, environment, and the text it reads on stdin, which is all it reads there.
Runner = Callable[[Sequence[str], Path, Mapping[str, str], str], ProcessResult]
Promote = Callable[[Path, Path], bool]

MINIMUM_COPILOT_CLI_VERSION = (1, 0, 88)
# The first Copilot CLI that refuses --no-remote and --no-remote-export and turns the session's export off with
# --sync-session=off instead.
COPILOT_SYNC_SESSION_VERSION = (1, 0, 103)
COPILOT_TIMEOUT_SECONDS = 1800
# Bytes of each of Copilot CLI's output streams kept for its diagnostic; the rest is cut and the cut named.
COPILOT_OUTPUT_BYTES = 1024 * 1024
HOST_CLAIM = "copilot-host.json"
HOST_LOCK = "copilot-host.lock"
# A claimed host that has not recorded its process this long after dispatch never started.
HOST_START_GRACE_SECONDS = 60
# How long past the Copilot limit a host may take to record its outcome before it counts as gone.
HOST_EXIT_GRACE_SECONDS = 300


class HostSuperseded(RuntimeContractError):
    """The host's claim was replaced or its role set aside, so its result is not promoted."""


@dataclass(frozen=True)
class HostState:
    """Where a run's Copilot CLI host stands: none, starting, running, done, or gone."""

    status: str
    elapsed: int = 0
    pid: int | None = None
    outcome: str | None = None  # done only: dispatched, failed, or superseded
    reason: str | None = None


# The functions that start a command whose program tests/run_validation.py cannot read here, with the reason.
COMMANDS_ALLOWED = {
    "subprocess_runner": "the default Copilot CLI runner, which this module's calls reach through _run_bounded, "
    "where the policy reads the program each passes",
}


def subprocess_runner(arguments: Sequence[str], cwd: Path, environment: Mapping[str, str], stdin: str) -> ProcessResult:
    """Run Copilot CLI with `stdin` as all it reads, without a window, keeping at most COPILOT_OUTPUT_BYTES of each
    output stream with the cut named; at COPILOT_TIMEOUT_SECONDS it is killed and subprocess.TimeoutExpired raised.
    Every process it started is ended with it either way, held in this platform's process tree, and one the tree
    cannot hold is ended before it runs and refused with JobRefused."""
    finished = run_bounded(
        arguments,
        COPILOT_TIMEOUT_SECONDS,
        cwd=cwd,
        env=environment,
        input_bytes=stdin.encode("utf-8"),
        output_limit=COPILOT_OUTPUT_BYTES,
        tree=process_tree(hidden_window=True),
    )
    return ProcessResult(finished.returncode, _text(finished.stdout), _text(finished.stderr))


def _text(output: bytes) -> str:
    """Copilot CLI's output as text, with Windows line endings read as newlines."""
    return output.decode("utf-8", "replace").replace("\r\n", "\n")


def staging_path(run: Path, attempt: int) -> Path:
    return run / f"copilot-result-{attempt}.json"


def host_log_path(run: Path, attempt: int) -> Path:
    return run / f"copilot-host-{attempt}.log"


def _outcome_path(run: Path, attempt: int) -> Path:
    return run / f"copilot-outcome-{attempt}.json"


def host_lock(run: Path) -> ResourceLock:
    """Held around every read-decide-write of a run's claim, its promotion, and check's set-aside."""
    return ResourceLock(run / HOST_LOCK)


def read_claim(run: Path) -> dict[str, Any] | None:
    path = run / HOST_CLAIM
    return read_json(path) if path.exists() else None


def new_claim(run: Path, *, generation: int, now: float) -> dict[str, Any]:
    """Claim the run for a new host attempt; a host with any other token, or a stale generation, is superseded.

    The generation is the role's retry count when the claim is made: check raises it when it sets the role aside.
    """
    previous = read_claim(run) or {}
    earlier = sum(1 for _ in run.glob("copilot-isolation-*"))
    claim = {
        "attempt": max(previous.get("attempt", 0), earlier) + 1,
        "token": secrets.token_hex(16),
        "generation": generation,
        "claimed_at": now,
        "pid": None,
        "start_time": None,
    }
    atomic_write_json(run / HOST_CLAIM, claim)
    return claim


def claim_holds(run: Path, token: str, generation: int) -> dict[str, Any] | None:
    """The claim, when it is still this host's and its role has not been set aside since."""
    claim = read_claim(run)
    if claim is None or claim["token"] != token or claim["generation"] != generation:
        return None
    return claim


def record_host_process(run: Path, token: str, pid: int, start_time: int | None) -> None:
    """Record the host's process on its claim, once: by dispatch, or by the host if dispatch was stopped first."""
    claim = read_claim(run)
    if claim is None or claim["token"] != token or claim["pid"] is not None:
        return
    claim.update(pid=pid, start_time=start_time)
    atomic_write_json(run / HOST_CLAIM, claim)


def write_outcome(run: Path, claim: dict[str, Any], status: str, reason: str | None) -> None:
    atomic_write_json(
        _outcome_path(run, claim["attempt"]), {"token": claim["token"], "status": status, "reason": reason}
    )


def host_state(run: Path, *, probe: Callable[[int], ProcessStatus], now: float) -> HostState:
    claim = read_claim(run)
    if claim is None:
        return HostState("none", reason="no Copilot CLI host was dispatched for this run")
    elapsed = max(0, round(now - claim["claimed_at"]))
    pid = claim["pid"]
    log = host_log_path(run, claim["attempt"])
    identity = None if pid is None else same_process(probe(pid), claim["start_time"])
    # Read after the probe, so a host that recorded its outcome and then exited is done, not gone.
    outcome_path = _outcome_path(run, claim["attempt"])
    outcome = read_json(outcome_path) if outcome_path.exists() else None
    if outcome is not None and outcome.get("token") == claim["token"]:
        return HostState("done", elapsed, pid, outcome["status"], outcome["reason"])
    if pid is None:
        if elapsed < HOST_START_GRACE_SECONDS:
            return HostState("starting", elapsed)
        return HostState("gone", elapsed, reason=f"the Copilot CLI host never started; see {log}")
    if identity is False:
        return HostState(
            "gone", elapsed, pid, reason=f"the Copilot CLI host (PID {pid}) ended without a result; see {log}"
        )
    if elapsed > COPILOT_TIMEOUT_SECONDS + HOST_EXIT_GRACE_SECONDS:
        return HostState(
            "gone",
            elapsed,
            pid,
            reason=f"the Copilot CLI host (PID {pid}) ran past its {COPILOT_TIMEOUT_SECONDS}s limit",
        )
    return HostState("running", elapsed, pid)


def _write_diagnostic(diagnostic_path: Path, result: ProcessResult) -> None:
    diagnostic = result.stdout
    if result.stderr:
        diagnostic += "\nSTDERR:\n" + result.stderr
    atomic_write_text(diagnostic_path, diagnostic)


def _run_bounded(
    runner: Runner,
    arguments: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    diagnostic_path: Path,
    stdin: str = "",
) -> ProcessResult:
    """Run Copilot; a timeout keeps its partial output and becomes a contract error, and so does a Copilot that no
    process tree could hold, which was ended before it ran."""
    try:
        return runner(arguments, cwd, environment, stdin)
    except JobRefused as exc:
        raise RuntimeContractError(f"GitHub Copilot CLI could not run: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        # The partial output is bytes or text depending on the platform, or absent.
        stdout, stderr = (
            part.decode("utf-8", "replace") if isinstance(part, bytes) else part or ""
            for part in (exc.output, exc.stderr)
        )
        _write_diagnostic(diagnostic_path, ProcessResult(-1, stdout, stderr))
        raise RuntimeContractError(
            f"GitHub Copilot CLI timed out after {exc.timeout:g}s; see {diagnostic_path}"
        ) from exc


def find_copilot() -> str:
    """Copilot CLI's own program: the first ejryan89-skill-core's locator finds, on PATH and then in WinGet's Links
    folder, that is no shell script. An npm install puts a `copilot.cmd` shim on PATH, which Windows would start through
    cmd.exe, so the lookup passes over every shell script, wherever it stands on PATH, and never returns one."""
    found = find_executables("copilot")
    program = next((path for path in found if not shell_script(path)), None)
    if program is not None:
        return program
    if found:
        raise RuntimeContractError(
            "GitHub Copilot CLI on PATH is a shell script, which Windows starts through a shell: "
            f"{', '.join(found)}; install the program from WinGet (winget install GitHub.Copilot) or the GitHub release"
        )
    raise RuntimeContractError("GitHub Copilot CLI is not available")


def parse_copilot_version(output: str) -> tuple[int, int, int]:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", output)
    if match is None:
        raise RuntimeContractError("Cannot parse GitHub Copilot CLI version")
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
    )


def isolated_copilot_environment(
    isolation_root: Path,
    base_environment: Mapping[str, str] | None = None,
) -> tuple[Path, dict[str, str]]:
    """Make the isolation root's home, Copilot home, and workspace, and return the workspace and Copilot CLI's
    environment: `base_environment`, or this process's, without git's repository-selecting variables, so neither
    Copilot nor a git it runs reads the repository a shell's `GIT_DIR` names, with both homes in the isolation root
    and Copilot's auto-update off, so the Copilot CLI whose version the host checks is the one that reviews, not a
    newer package it has downloaded."""
    if not isolation_root.is_absolute():
        raise RuntimeContractError("Copilot isolation root must be absolute")
    if isolation_root.exists() and not isolation_root.is_dir():
        raise RuntimeContractError("Copilot isolation root must be a directory")
    if isolation_root.exists() and any(isolation_root.iterdir()):
        raise RuntimeContractError("Copilot isolation root must be empty")
    home = isolation_root / "home"
    copilot_home = isolation_root / "copilot-home"
    workspace = isolation_root / "workspace"
    for directory in (home, copilot_home, workspace):
        directory.mkdir(parents=True, exist_ok=True)
    environment = without_repository_variables(os.environ if base_environment is None else base_environment)
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    environment["COPILOT_HOME"] = str(copilot_home)
    environment["COPILOT_AUTO_UPDATE"] = "false"
    return workspace, environment


def materialized_reviewer_entrypoint(materialized_root: Path) -> Path:
    metadata_path = materialized_root / "materialization.json"
    try:
        metadata = parse_json(metadata_path.read_text(encoding="utf-8-sig"))
    except (OSError, *CONTENT_ERRORS) as exc:
        raise RuntimeContractError(f"Copilot reviewer materialization metadata is invalid: {exc}") from exc
    READ.json_object(
        metadata,
        "materialization",
        required={"schema_version", "adapter_id", "entrypoint", "source_commit", "source_hashes"},
    )
    if READ.integer(metadata["schema_version"], "materialization.schema_version") != 1:
        raise RuntimeContractError("Copilot reviewer materialization schema version is unsupported")
    READ.string(metadata["adapter_id"], "materialization.adapter_id")
    READ.hex_digest(metadata["source_commit"], "materialization.source_commit", 40, 64)
    relative_value = _safe_relative_path(metadata["entrypoint"], "materialization.entrypoint")
    relative = PurePosixPath(relative_value)
    materialized_resolved = materialized_root.resolve(strict=True)
    source_hashes = READ.json_object(metadata["source_hashes"], "materialization.source_hashes")
    if relative_value not in source_hashes:
        raise RuntimeContractError("Copilot reviewer materialization source hashes are invalid")
    expected_files = {"materialization.json"}
    for declared_path, expected_hash in source_hashes.items():
        normalized = _safe_relative_path(declared_path, "a key of materialization.source_hashes")
        if normalized != declared_path or declared_path == "materialization.json":
            raise RuntimeContractError(f"Copilot reviewer materialization path is invalid: {declared_path!r}")
        READ.hex_digest(expected_hash, f"materialization.source_hashes.{declared_path}", 64)
        target = materialized_root.joinpath(*PurePosixPath(declared_path).parts)
        _require_safe_snapshot_path(materialized_root, target)
        if not target.is_file() or not target.resolve(strict=True).is_relative_to(materialized_resolved):
            raise RuntimeContractError(f"Copilot reviewer materialization file is invalid: {declared_path}")
        actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise RuntimeContractError(f"Copilot reviewer materialization hash does not match: {declared_path}")
        expected_files.add(declared_path)
    actual_files = _snapshot_files(materialized_root)
    if actual_files != expected_files:
        raise RuntimeContractError("Copilot reviewer materialization file set does not match its hashes")
    return materialized_root.joinpath(*relative.parts).resolve(strict=True)


def copilot_local_session(version: tuple[int, ...]) -> list[str]:
    """The flags that keep a Copilot CLI session of `version` local: not exported to the account's history on GitHub
    web and mobile, and not controlled from there. Before COPILOT_SYNC_SESSION_VERSION, --no-remote-export turns off
    the export and, with it, remote control, and --no-remote turns off remote control on its own. From that version
    Copilot refuses both and names --sync-session=off as the export's replacement; remote control there starts only
    from the /remote command in an interactive session or a hosting option such as --connect, and the session is given
    neither."""
    if version >= COPILOT_SYNC_SESSION_VERSION:
        return ["--sync-session=off"]
    return ["--no-remote", "--no-remote-export"]


def copilot_command(
    executable: str,
    *,
    version: tuple[int, ...],
    run_directory: Path,
    materialized_root: Path,
    result_path: Path,
    effort: str | None = None,
) -> list[str]:
    """The Copilot CLI command that reviews one run on the Copilot CLI `version` names, at `effort` when one is given,
    else at the session's default."""
    negotiate_capabilities("copilot-cli", ["isolated-added-root", "read-diff", "write-result"])
    if effort is not None and effort not in REVIEWER_EFFORTS:
        raise RuntimeContractError(f"Copilot reasoning effort must be one of {', '.join(sorted(REVIEWER_EFFORTS))}")
    if not materialized_root.is_absolute() or not materialized_root.is_dir():
        raise RuntimeContractError("Copilot materialized root must be an existing absolute directory")
    if not run_directory.is_absolute() or not run_directory.is_dir():
        raise RuntimeContractError("Copilot run directory must be an existing absolute directory")
    if not result_path.is_absolute():
        raise RuntimeContractError("Copilot result path must be absolute")
    run_directory_resolved = run_directory.resolve(strict=True)
    result_path_resolved = result_path.resolve(strict=False)
    if not result_path_resolved.is_relative_to(run_directory_resolved):
        raise RuntimeContractError("Copilot result path must be inside the run directory")
    # No prompt argument: the prompt, which carries review-comment bodies, prior findings, and diff paths, goes on
    # stdin, which Copilot CLI reads as its prompt in non-interactive mode when no -p is given. A command line holds
    # none of that text, so no program's parsing of one can turn it into a command.
    return [
        executable,
        f"--add-dir={run_directory}",
        f"--add-dir={materialized_root}",
        "--no-custom-instructions",
        "--no-ask-user",
        *copilot_local_session(version),
        "--disable-builtin-mcps",
        "--disallow-temp-dir",
        "--stream=off",
        "--output-format=json",
        "--available-tools",
        "view",
        "grep",
        "glob",
        "edit",
        "create",
        "--allow-tool=read",
        f"--allow-tool=write({result_path.as_posix()})",
        "--deny-tool=shell",
        "--deny-tool=url",
        "--deny-tool=memory",
        "--deny-tool=ask_user",
        # Copilot CLI's --reasoning-effort takes every value REVIEWER_EFFORTS names; one the model does not offer it
        # reports and leaves unapplied.
        *([f"--reasoning-effort={effort}"] if effort is not None else []),
    ]


def run_copilot(
    *,
    run_directory: Path,
    materialized_root: Path,
    request_path: Path,
    result_path: Path,
    diagnostic_path: Path,
    isolation_root: Path,
    promote: Promote,
    staging_path: Path | None = None,
    snapshot_stamp: str | None = None,
    effort: str | None = None,
    runner: Runner = subprocess_runner,
    executable: str | None = None,
    base_environment: Mapping[str, str] | None = None,
) -> None:
    """Run one Copilot CLI review. Copilot writes only the staging file; once it is a JSON object, the required
    `promote` callback checks the caller's claim and renames it to the result path, so a reader sees the whole
    result or none. A refused promotion is HostSuperseded. `snapshot_stamp` is the stamp `prepare` took of the
    source snapshot, so the snapshot is re-read
    before Copilot starts only as far as something can have changed since (see `verify_stamped_snapshot`). `effort`,
    when given, is the reasoning effort Copilot reviews at."""
    staging_path = staging_path or result_path.with_name(f"{result_path.stem}.staging{result_path.suffix}")
    materialized_resolved, entrypoint_resolved = _copilot_reviewer(
        run_directory, materialized_root, request_path, result_path, staging_path, diagnostic_path
    )
    request = _read_copilot_request(request_path)
    source_resolved = _verified_copilot_source(run_directory, request, snapshot_stamp)
    execution_directory, environment = isolated_copilot_environment(isolation_root, base_environment)
    executable = executable or find_copilot()
    version = _copilot_version(runner, executable, execution_directory, environment, diagnostic_path)
    prompt = _copilot_prompt(
        request.value, request_path, entrypoint_resolved, materialized_resolved, source_resolved, staging_path
    )
    result = _run_bounded(
        runner,
        copilot_command(
            executable,
            version=version,
            run_directory=run_directory,
            materialized_root=materialized_root,
            result_path=staging_path,
            effort=effort,
        ),
        execution_directory,
        environment,
        diagnostic_path,
        stdin=prompt,
    )
    _write_diagnostic(diagnostic_path, result)
    _require_copilot_result(result, staging_path, diagnostic_path)
    if not promote(staging_path, result_path):
        raise HostSuperseded(
            f"the Copilot CLI host's result came after its role was set aside; it stays in {staging_path}"
        )


def _copilot_reviewer(
    run_directory: Path,
    materialized_root: Path,
    request_path: Path,
    result_path: Path,
    staging_path: Path,
    diagnostic_path: Path,
) -> tuple[Path, Path]:
    """The resolved materialized root and trusted entrypoint, once the inputs exist and every path is absolute."""
    for path, label in (
        (run_directory, "run directory"),
        (materialized_root, "materialized root"),
        (request_path, "request"),
    ):
        if not path.is_absolute() or not path.exists():
            raise RuntimeContractError(f"Copilot {label} must exist and be absolute")
    materialized_resolved = materialized_root.resolve(strict=True)
    entrypoint_resolved = materialized_reviewer_entrypoint(materialized_root)
    for path, label in ((result_path, "result"), (staging_path, "staging"), (diagnostic_path, "diagnostic")):
        if not path.is_absolute():
            raise RuntimeContractError(f"Copilot {label} path must be absolute")
    return materialized_resolved, entrypoint_resolved


@dataclass(frozen=True)
class _CopilotRequest:
    """The parts of a review request the Copilot CLI host checks before it starts Copilot."""

    value: dict[str, Any]
    source_snapshot: dict[str, Any]
    source_root: Path
    source_manifest: Path
    diff_path: Path
    repository: str
    head_sha: str


def _read_copilot_request(request_path: Path) -> _CopilotRequest:
    try:
        request = parse_json(request_path.read_text(encoding="utf-8-sig"))
        pull_request = request["pull_request"]
        source_snapshot = request["source_snapshot"]
        source_root = Path(source_snapshot["root"])
        source_manifest = Path(source_snapshot["manifest_path"])
        diff_path = Path(request["diff_path"])
        repository = request["repository"]
        head_sha = pull_request["head_sha"]
    except (OSError, *CONTENT_ERRORS, KeyError, TypeError) as exc:
        raise RuntimeContractError(f"Copilot request source snapshot is invalid: {exc}") from exc
    return _CopilotRequest(request, source_snapshot, source_root, source_manifest, diff_path, repository, head_sha)


def _verified_copilot_source(run_directory: Path, request: _CopilotRequest, stamp: str | None) -> Path:
    """The resolved source snapshot root, once the diff and the snapshot are inside the run directory and the snapshot
    verifies against the request's repository and head, and against `stamp` as far as it still holds."""
    run_resolved = run_directory.resolve(strict=True)
    try:
        source_resolved = request.source_root.resolve(strict=True)
        diff_resolved = request.diff_path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeContractError(f"Copilot request artifacts are invalid: {exc}") from exc
    if not diff_resolved.is_file() or not diff_resolved.is_relative_to(run_resolved):
        raise RuntimeContractError("Copilot request diff must be a file inside the run directory")
    if not source_resolved.is_relative_to(run_resolved):
        raise RuntimeContractError("Copilot source snapshot must be inside the run directory")
    if request.source_manifest != request.source_root / SOURCE_SNAPSHOT_MANIFEST:
        raise RuntimeContractError("Copilot source snapshot manifest path is invalid")
    if request.source_snapshot.get("source_commit") != request.head_sha:
        raise RuntimeContractError("Copilot source snapshot commit is invalid")
    verify_stamped_snapshot(
        request.source_root,
        expected_repository=request.repository,
        expected_commit=request.head_sha,
        stamp=stamp,
    )
    return source_resolved


def _copilot_version(
    runner: Runner, executable: str, execution_directory: Path, environment: Mapping[str, str], diagnostic_path: Path
) -> tuple[int, int, int]:
    """The version the bounded `--version` run names, once the run succeeded and the version is at least the minimum."""
    version_result = _run_bounded(runner, [executable, "--version"], execution_directory, environment, diagnostic_path)
    if version_result.returncode != 0:
        raise RuntimeContractError("Cannot determine GitHub Copilot CLI version")
    version = parse_copilot_version(version_result.stdout)
    if version < MINIMUM_COPILOT_CLI_VERSION:
        minimum = ".".join(str(part) for part in MINIMUM_COPILOT_CLI_VERSION)
        raise RuntimeContractError(f"GitHub Copilot CLI {minimum} or newer is required")
    return version


def _copilot_prompt(
    request: dict[str, Any],
    request_path: Path,
    entrypoint_resolved: Path,
    materialized_resolved: Path,
    source_resolved: Path,
    staging_path: Path,
) -> str:
    """The prepared entrypoint's contracts, with the host's staging destination and no shell-based self-check
    or source commands. The caller has verified the reviewer and source snapshot before rendering."""
    request = {**request, "source_snapshot": {**request["source_snapshot"], "root": str(source_resolved)}}
    metadata = read_json(materialized_resolved / "materialization.json")
    snapshot = read_json(source_resolved / SOURCE_SNAPSHOT_MANIFEST)
    links = symbolic_links(parse_unified_diff(read_diff(Path(request["diff_path"]))), snapshot["excluded_paths"])
    try:
        prompt = render_entrypoint_prompt(
            request,
            request_path=request_path,
            reviewer=metadata["adapter_id"],
            reviewer_root=materialized_resolved,
            entrypoint=entrypoint_resolved.relative_to(materialized_resolved).as_posix(),
            result_file=staging_path,
            self_check="",
            links=links,
        )
    except (KeyError, TypeError) as exc:
        raise RuntimeContractError(f"Copilot request prompt inputs are invalid: {exc}") from exc
    return prompt + (
        "\nHost restrictions: Do not ask questions, run shell commands, use network tools, "
        "or modify any file other than RESULT_FILE."
    )


def _require_copilot_result(result: ProcessResult, staging_path: Path, diagnostic_path: Path) -> None:
    """Copilot exited cleanly and left a JSON object in the staging file."""
    if result.returncode != 0:
        raise RuntimeContractError(
            f"GitHub Copilot CLI failed with exit code {result.returncode}; see {diagnostic_path}"
        )
    if not staging_path.is_file():
        raise RuntimeContractError("GitHub Copilot CLI did not produce the result file")
    try:
        parsed = parse_json(staging_path.read_text(encoding="utf-8-sig"))
    except (OSError, *CONTENT_ERRORS) as exc:
        raise RuntimeContractError(f"GitHub Copilot CLI result is not valid JSON: {exc}") from exc
    READ.json_object(parsed, "adapter-result")

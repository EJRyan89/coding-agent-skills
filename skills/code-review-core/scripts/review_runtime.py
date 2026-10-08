"""Trusted reviewer materialization and runtime-host capability negotiation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import threading
from collections import Counter
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from git_client import GitClient, GitError, GitResult, GitStream, Runner, subprocess_runner
from github_client import GitHubClient, GitHubError, replace_undecodable
from review_config import REVIEWER_EFFORTS, validate_repository_identity
from review_io import PersistenceError, atomic_write_json, read_diff

ADAPTER_PROTOCOL_VERSION = 1
SOURCE_SNAPSHOT_SCHEMA_VERSION = 1
SOURCE_SNAPSHOT_MANIFEST = "source-snapshot.json"
MAX_SOURCE_SNAPSHOT_FILES = 50_000
MAX_SOURCE_SNAPSHOT_BYTES = 256 * 1024 * 1024
MAX_SOURCE_FILE_BYTES = 1024 * 1024
BINARY_PROBE_BYTES = 8000
SNAPSHOT_WRITE_WORKERS = 8
GIT_WRITER_SECONDS = 10  # how long the id writer gets to finish once git is gone
MAX_CHANGED_FILE_BYTES = 16 * 1024 * 1024
SNAPSHOT_EXCLUSION_REASONS = {
    "agent-instruction",
    "binary",
    "file-size-limit",
    "unsafe-path",
    "symbolic-link",
    "non-regular",
}
WINDOWS_UNSAFE = re.compile(r'[:<>"|?*\x00-\x1f]')
# Names Windows opens as a device rather than a file, alone or before any extension: `nul.txt` and `COM1.tar.gz` too.
RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"{port}{digit}" for port in ("com", "lpt") for digit in "0123456789¹²³"}
)
MAX_SEGMENT_UNITS = 255  # NTFS, ReFS, exFAT, and FAT32 all stop a name at 255 UTF-16 code units
MAX_PATH = 260  # a Windows path without long-path support, its terminating NUL included
LONG_PATH_UNITS = 32_767  # an NT path with long-path support, counted with the volume's device name for the drive
GIT_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
RUNTIME_CAPABILITIES = {
    "claude-code": {"agent-delegation", "read-diff", "write-result"},
    "codex": {"agent-delegation", "read-diff", "write-result"},
    "copilot-cli": {"isolated-added-root", "read-diff", "write-result"},
}
# An inline reviewer is the orchestrating session: it reads the run and writes its result, and it starts no agent
# and has no isolated host, so a manifest that needs either cannot run inline.
INLINE_CAPABILITIES = frozenset({"read-diff", "write-result"})
MANIFEST_KEYS = {
    "schema_version",
    "id",
    "protocol_version",
    "supports",
    "required_capabilities",
    "entrypoint",
    "resources",
    "agent_profiles",
}
SPECIALIST_MANIFEST_KEYS = {
    "schema_version",
    "id",
    "protocol_version",
    "kind",
    "supports",
    "required_capabilities",
    "resources",
    "specialists",
    "conditions",
}
# `uncovered` says what happens to changed files no specialist matches when others route: `review` (the default) gives
# them to the generic reviewer, `ignore` leaves them unreviewed and lists them in the record.
OPTIONAL_SPECIALIST_MANIFEST_KEYS = {"uncovered"}
UNCOVERED_POLICIES = ("review", "ignore")
SPECIALIST_KEYS = {"id", "category", "profile", "include", "exclude", "resources", "when"}
# Optional per-specialist settings a manifest may give. `model` takes the aliases every way of starting a Claude
# subagent accepts (the Agent tool takes no other value; on Bedrock a bare model name can be silently ignored), or
# `inherit` to use the session's model whatever the profile says. `effort` reaches reviewers the Workflow tool starts.
OPTIONAL_SPECIALIST_KEYS = {"model", "effort"}
MODEL_ALIASES = frozenset({"sonnet", "opus", "haiku", "fable"})
GENERIC_SPECIALIST = "generic-review"
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class RuntimeContractError(ValueError):
    """Raised when a runtime or repository reviewer violates the trust contract."""


# Reads blobs by id from a checkout's object store, yielding each one's exact bytes in the order asked.
BlobReader = Callable[[Path, Sequence[str]], Iterable[bytes]]
# A snapshot path with its content bytes when the snapshot keeps it, or the reason it leaves the path out.
SnapshotMember = tuple[str, bytes | str]


def output_bytes(text: str) -> bytes:
    """The exact bytes a runner decoded to `text`."""
    return text.encode("utf-8", "surrogateescape")


def has_undecodable(text: str) -> bool:
    """Whether a runner's output holds a byte that is not UTF-8, which surrogateescape keeps as a lone surrogate."""
    return any("\udc80" <= character <= "\udcff" for character in text)


def _safe_relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RuntimeContractError(f"{field} must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} or WINDOWS_UNSAFE.search(part) for part in path.parts):
        raise RuntimeContractError(f"{field} is unsafe: {value!r}")
    return path.as_posix()


@dataclass(frozen=True)
class PathRoom:
    """How long a path below a snapshot root may be, in UTF-16 code units: a file's, and the folder it is in."""

    file: int
    folder: int


def path_room(destination: Path) -> PathRoom:
    """The room a snapshot written at `destination` leaves each path below it, once the root and the separator after
    it are counted against what this process can write."""
    used = _utf16_units(str(destination.absolute())) + 1
    file_limit, folder_limit = _path_limits()
    return PathRoom(file_limit - used, folder_limit - used)


def _path_limits() -> tuple[int, int]:
    """The longest whole path, in UTF-16 code units, at which this process can write a file and create a folder.

    Without long-path support, MAX_PATH holds a path and its terminating NUL, and CreateDirectory keeps 12 more of it
    for an 8.3 file name. With it, an NT path holds 32,767 units counted with the volume's device name, such as
    `\\Device\\HarddiskVolume3`, in place of the drive, so MAX_PATH of them are left for that name.
    """
    if _long_paths_enabled():
        return LONG_PATH_UNITS - MAX_PATH, LONG_PATH_UNITS - MAX_PATH
    return MAX_PATH - 1, MAX_PATH - 12 - 1


def _long_paths_enabled() -> bool:
    """Whether Windows lets this process use paths over MAX_PATH. The system setting and the interpreter's manifest
    must both allow it, and ntdll answers for the two; without the answer the shorter limits hold."""
    import ctypes

    try:
        query = ctypes.WinDLL("ntdll").RtlAreLongPathsEnabled
    except (AttributeError, OSError):
        return False
    query.restype = ctypes.c_ubyte
    return bool(query())


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _segments(relative: str) -> list[str]:
    return relative.split("/")


# Every reason the snapshot cannot hold a head path as it is named, in the order they are checked. A path one applies
# to is an `unsafe-path` exclusion: recorded, never written, and a coverage gap only for a pull request that changes it,
# so every other file is still reviewed. The first rule comes first because its path has no UTF-16 length.
UNSAFE_PATH_RULES: tuple[tuple[str, Callable[[str, PathRoom], bool]], ...] = (
    (
        "bytes that are not UTF-8: the path has no exact name to write, so it is recorded with U+FFFD for each byte",
        lambda relative, room: has_undecodable(relative),
    ),
    (
        'a reserved character (< > : " | ? *) or a control character in a segment: Windows refuses it in a name',
        lambda relative, room: any(WINDOWS_UNSAFE.search(segment) for segment in _segments(relative)),
    ),
    (
        "a backslash in a segment: Windows reads it as a folder separator, so the file would land in another folder",
        lambda relative, room: "\\" in relative,
    ),
    (
        "a segment ending in a dot or a space: Windows drops it, so the file would be written under another name",
        lambda relative, room: any(segment.endswith((".", " ")) for segment in _segments(relative)),
    ),
    (
        "a reserved device name as a segment, with or without an extension: Windows opens the device, not a file",
        lambda relative, room: any(
            segment.split(".", 1)[0].rstrip(" ").casefold() in RESERVED_DEVICE_NAMES for segment in _segments(relative)
        ),
    ),
    (
        f"a segment over {MAX_SEGMENT_UNITS} UTF-16 code units: no Windows file system holds a longer name",
        lambda relative, room: any(_utf16_units(segment) > MAX_SEGMENT_UNITS for segment in _segments(relative)),
    ),
    (
        "a path longer than the room the snapshot root leaves a file",
        lambda relative, room: _utf16_units(relative) > room.file,
    ),
    (
        "a folder longer than the room the snapshot root leaves a folder",
        lambda relative, room: _utf16_units(relative.rpartition("/")[0]) > room.folder,
    ),
)


def unsafe_path_reason(relative: str, room: PathRoom) -> str | None:
    """Why the snapshot cannot hold this head path as named, from UNSAFE_PATH_RULES, or None when it can."""
    return next((reason for reason, applies in UNSAFE_PATH_RULES if applies(relative, room)), None)


def _path_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise RuntimeContractError(f"{field} must be an array")
    return [_safe_relative_path(item, f"{field}[{index}]") for index, item in enumerate(value)]


def _regex_list(value: Any, field: str, *, required: bool) -> list[str]:
    if not isinstance(value, list) or (required and not value):
        raise RuntimeContractError(f"{field} must be a{' non-empty' if required else 'n'} array")
    for index, pattern in enumerate(value):
        if not isinstance(pattern, str) or not pattern:
            raise RuntimeContractError(f"{field}[{index}] must be a non-empty regular expression")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise RuntimeContractError(f"{field}[{index}] is not a valid regular expression: {exc}") from exc
    return list(value)


def _validate_specialists(value: dict[str, Any]) -> dict[str, Any]:
    conditions = value["conditions"]
    if not isinstance(conditions, dict):
        raise RuntimeContractError("Adapter conditions must be an object")
    normalized_conditions: dict[str, dict[str, str]] = {}
    for name, condition in conditions.items():
        if not isinstance(name, str) or not SLUG.fullmatch(name):
            raise RuntimeContractError(f"Adapter condition name is invalid: {name!r}")
        if not isinstance(condition, dict) or set(condition) != {"script"}:
            raise RuntimeContractError(f"Adapter condition {name} must declare only a script")
        normalized_conditions[name] = {"script": _safe_relative_path(condition["script"], f"conditions.{name}.script")}
    specialists = value["specialists"]
    if not isinstance(specialists, list) or not specialists:
        raise RuntimeContractError("Adapter specialists must be a non-empty array")
    normalized_specialists = []
    seen: set[str] = set()
    for index, specialist in enumerate(specialists):
        field = f"specialists[{index}]"
        if not isinstance(specialist, dict) or not SPECIALIST_KEYS <= set(specialist) <= (
            SPECIALIST_KEYS | OPTIONAL_SPECIALIST_KEYS
        ):
            raise RuntimeContractError(f"{field} fields do not match the protocol")
        model, effort = specialist.get("model"), specialist.get("effort")
        if "model" in specialist and model not in MODEL_ALIASES | {"inherit"}:
            raise RuntimeContractError(f"{field}.model must be inherit or one of {', '.join(sorted(MODEL_ALIASES))}")
        if "effort" in specialist and effort not in REVIEWER_EFFORTS:
            raise RuntimeContractError(f"{field}.effort must be one of {', '.join(sorted(REVIEWER_EFFORTS))}")
        identity = specialist["id"]
        if not isinstance(identity, str) or not SLUG.fullmatch(identity) or identity in seen | {GENERIC_SPECIALIST}:
            raise RuntimeContractError(f"{field}.id is invalid or duplicated")
        seen.add(identity)
        if not isinstance(specialist["category"], str) or not specialist["category"].strip():
            raise RuntimeContractError(f"{field}.category is required")
        when = specialist["when"]
        if when is not None and when not in normalized_conditions:
            raise RuntimeContractError(f"{field}.when names an undeclared condition")
        normalized_specialists.append(
            {
                "id": identity,
                "category": specialist["category"].strip(),
                "profile": _safe_relative_path(specialist["profile"], f"{field}.profile"),
                "include": _regex_list(specialist["include"], f"{field}.include", required=True),
                "exclude": _regex_list(specialist["exclude"], f"{field}.exclude", required=False),
                "resources": _path_list(specialist["resources"], f"{field}.resources"),
                "when": when,
                **({"model": model} if model is not None else {}),
                **({"effort": effort} if effort is not None else {}),
            }
        )
    return {
        "conditions": normalized_conditions,
        "specialists": normalized_specialists,
    }


def declared_reviewer_files(manifest: dict[str, Any]) -> list[str]:
    """Every trusted file a validated manifest declares, without duplicates."""
    if manifest.get("kind") == "specialists":
        files = list(manifest["resources"])
        for specialist in manifest["specialists"]:
            for path in (specialist["profile"], *specialist["resources"]):
                if path not in files:
                    files.append(path)
        for condition in manifest["conditions"].values():
            if condition["script"] not in files:
                files.append(condition["script"])
        return files
    return [manifest["entrypoint"], *manifest["resources"], *manifest["agent_profiles"]]


def validate_adapter_manifest(value: Any) -> dict[str, Any]:
    version = _manifest_version(value)
    _validate_supports(value["supports"])
    _validate_capabilities(value["required_capabilities"])
    if version == 2:
        return _normalized_specialists_manifest(value)
    return _normalized_entrypoint_manifest(value)


def _manifest_version(value: Any) -> Any:
    """The schema version of a manifest whose fields match it and whose protocol version and id are valid."""
    if not isinstance(value, dict):
        raise RuntimeContractError("Adapter manifest fields do not match the protocol")
    version = value.get("schema_version")
    expected_keys = SPECIALIST_MANIFEST_KEYS if version == 2 else MANIFEST_KEYS
    optional_keys = OPTIONAL_SPECIALIST_MANIFEST_KEYS if version == 2 else set()
    if not expected_keys <= set(value) <= expected_keys | optional_keys:
        raise RuntimeContractError("Adapter manifest fields do not match the protocol")
    if version not in {1, 2} or value["protocol_version"] != ADAPTER_PROTOCOL_VERSION:
        raise RuntimeContractError("Adapter manifest protocol version is unsupported")
    if not isinstance(value["id"], str) or not SLUG.fullmatch(value["id"]):
        raise RuntimeContractError("Adapter manifest id is invalid")
    return version


def _validate_supports(supports: Any) -> None:
    if (
        not isinstance(supports, list)
        or not supports
        or any(not isinstance(item, str) or item not in {"initial", "re-review"} for item in supports)
        or len(set(supports)) != len(supports)
    ):
        raise RuntimeContractError("Adapter supports must contain unique supported modes")


def _validate_capabilities(capabilities: Any) -> None:
    if (
        not isinstance(capabilities, list)
        or len(set(capabilities)) != len(capabilities)
        or any(not isinstance(item, str) or not item for item in capabilities)
    ):
        raise RuntimeContractError("Adapter required_capabilities is invalid")


def _normalized_specialists_manifest(value: dict[str, Any]) -> dict[str, Any]:
    """A specialists manifest (schema 2) with its paths normalized, once its kind, uncovered policy, specialists, and
    declared files are valid. Whether it lists agent-delegation is negotiated when a review starts: listing it keeps
    its specialists off an inline review."""
    if value["kind"] != "specialists":
        raise RuntimeContractError("Adapter manifest kind is unsupported")
    if "uncovered" in value and value["uncovered"] not in UNCOVERED_POLICIES:
        raise RuntimeContractError("Adapter uncovered must be review or ignore")
    normalized = dict(value)
    normalized["resources"] = _path_list(value["resources"], "resources")
    normalized.update(_validate_specialists(value))
    declared = [*normalized["resources"]]
    for specialist in normalized["specialists"]:
        declared.extend([specialist["profile"], *specialist["resources"]])
    declared.extend(condition["script"] for condition in normalized["conditions"].values())
    if len(set(normalized["resources"])) != len(normalized["resources"]):
        raise RuntimeContractError("Adapter declares a file more than once")
    _refuse_reserved_paths(declared)
    return normalized


def _refuse_reserved_paths(declared: list[str]) -> None:
    """Refuse a declared file at the path of the materialization record, in any case: on a file system that ignores
    case, the record would replace it after its hash is taken."""
    if any(path.casefold() == "materialization.json" for path in declared):
        raise RuntimeContractError("Adapter declares a reserved path")


def _normalized_entrypoint_manifest(value: dict[str, Any]) -> dict[str, Any]:
    """An entrypoint manifest (schema 1) with its paths normalized, once each is safe, none is declared twice, and
    none is the reserved materialization record."""
    entrypoint = _safe_relative_path(value["entrypoint"], "entrypoint")
    resources = value["resources"]
    agents = value["agent_profiles"]
    if not isinstance(resources, list) or not isinstance(agents, list):
        raise RuntimeContractError("Adapter resources and agent_profiles must be arrays")
    normalized_resources = [_safe_relative_path(item, f"resources[{index}]") for index, item in enumerate(resources)]
    normalized_agents = [_safe_relative_path(item, f"agent_profiles[{index}]") for index, item in enumerate(agents)]
    declared = [entrypoint, *normalized_resources, *normalized_agents]
    if len(set(declared)) != len(declared):
        raise RuntimeContractError("Adapter declares a file more than once")
    _refuse_reserved_paths(declared)
    normalized = dict(value)
    normalized["entrypoint"] = entrypoint
    normalized["resources"] = normalized_resources
    normalized["agent_profiles"] = normalized_agents
    return normalized


def negotiate_capabilities(runtime: str, required: Iterable[str], *, dispatch: str = "subagents") -> set[str]:
    """The capabilities a review dispatched this way on this runtime offers, once it offers every required one.

    An inline review offers only what its runtime and INLINE_CAPABILITIES both do. The runtime's own lack is named
    first, so a runtime that cannot delegate says so whichever way its review is dispatched.
    """
    available = RUNTIME_CAPABILITIES.get(runtime)
    if available is None:
        raise RuntimeContractError(f"Unknown runtime host: {runtime}")
    wanted = set(required)
    missing = sorted(wanted - available)
    if missing:
        raise RuntimeContractError(f"Runtime {runtime} lacks required capabilities: {', '.join(missing)}")
    if dispatch != "inline":
        return available
    missing = sorted(wanted - INLINE_CAPABILITIES)
    if missing:
        raise RuntimeContractError(f"Inline review lacks required capabilities: {', '.join(missing)}")
    return available & INLINE_CAPABILITIES


def choose_dispatch(runtime: str, kind: str, *, inline: bool = False) -> str:
    """How a run's roles are worked: inline when asked, or when the runtime cannot start the subagents the suite's
    own roles need; a Copilot CLI entrypoint reviewer on its bounded host; otherwise as subagents."""
    if inline:
        return "inline"
    if runtime == "copilot-cli" and kind == "entrypoint":
        return "copilot-host"
    if "agent-delegation" not in RUNTIME_CAPABILITIES[runtime]:
        return "inline"
    return "subagents"


def resolve_runtime(configured: str, host: str | None = None) -> str:
    """The configured runtime, else the host the orchestrating session states, else the first CLI on PATH.

    PATH says which CLIs are installed, not which one is orchestrating, so it is only the fallback.
    """
    if configured != "auto":
        negotiate_capabilities(configured, ())
        return configured
    if host is not None:
        negotiate_capabilities(host, ())
        return host
    for runtime, command in (
        ("claude-code", "claude"),
        ("codex", "codex"),
        ("copilot-cli", "copilot"),
    ):
        if shutil.which(command):
            return runtime
    winget_copilot = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "copilot.exe"
    if winget_copilot.is_file():
        return "copilot-cli"
    raise RuntimeContractError("No supported local runtime host is available")


def _git(checkout: Path, runner: Runner, *arguments: str) -> GitResult:
    """Run git in the checkout through skill-core's client, whose `runner` tests replace.

    git reads no stdin, shows no prompt, and stops at the client's time limit; git that cannot run or finish breaks
    the contract like a failed command.
    """
    try:
        return GitClient(runner).run(arguments, directory=checkout)
    except GitError as exc:
        raise RuntimeContractError(str(exc)) from exc


def _run_git(checkout: Path, runner: Runner, *arguments: str) -> str:
    result = _git(checkout, runner, *arguments)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise RuntimeContractError(output_bytes(detail).decode("utf-8", "replace"))
    return result.stdout.strip()


def _normalize_remote(value: str) -> str:
    value = value.strip().removesuffix(".git")
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+)$", value, re.IGNORECASE)
    if not match:
        raise RuntimeContractError("Checkout origin is not a recognizable GitHub remote")
    return f"{match.group(1)}/{match.group(2)}".lower()


def verify_checkout_remote(checkout: Path, repository: str, runner: Runner = subprocess_runner) -> None:
    expected = validate_repository_identity(repository)
    actual = _normalize_remote(_run_git(checkout, runner, "remote", "get-url", "origin"))
    if actual != expected:
        raise RuntimeContractError(f"Checkout origin mismatch: expected {expected}, found {actual}")


def resolve_reviewer_commit(
    checkout: Path,
    trusted_ref: str,
    *,
    head_sha: str,
    runner: Runner = subprocess_runner,
) -> str:
    if not trusted_ref or trusted_ref.startswith("refs/pull/"):
        raise RuntimeContractError("Pull-request refs cannot be trusted reviewer sources")
    commit = _run_git(checkout, runner, "rev-parse", "--verify", f"{trusted_ref}^{{commit}}")
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
        raise RuntimeContractError("Trusted ref did not resolve to a commit")
    if commit.lower() == head_sha.lower():
        raise RuntimeContractError("Trusted reviewer ref resolves to the pull-request head")
    return commit.lower()


def _read_git_file(checkout: Path, commit: str, relative: str, runner: Runner) -> bytes:
    """The committed blob's exact bytes; a caller that needs text decodes them inside its own UnicodeError handler."""
    mode_line = _run_git(checkout, runner, "ls-tree", commit, "--", relative)
    if not mode_line:
        raise RuntimeContractError(f"Declared reviewer file is missing: {relative}")
    fields = mode_line.split(None, 3)
    if len(fields) != 4 or fields[1] != "blob" or fields[0] == "120000":
        raise RuntimeContractError(f"Declared reviewer file is not a regular file: {relative}")
    result = _git(checkout, runner, "show", f"{commit}:{relative}")
    if result.returncode != 0:
        raise RuntimeContractError(f"Cannot read declared reviewer file: {relative}")
    return output_bytes(result.stdout)


def load_manifest_from_commit(
    checkout: Path,
    commit: str,
    manifest_path: str,
    *,
    runner: Runner = subprocess_runner,
) -> dict[str, Any]:
    relative = _safe_relative_path(manifest_path, "manifest_path")
    try:
        content = _read_git_file(checkout, commit, relative, runner).decode("utf-8-sig")
        return validate_adapter_manifest(json.loads(content))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeContractError(f"Adapter manifest is not valid UTF-8 JSON: {exc}") from exc


MAX_LOCAL_MANIFEST_BYTES = 1024 * 1024


def load_manifest_from_file(path: Path) -> dict[str, Any]:
    """A reviewer manifest kept outside the reviewed repository, beside the condition scripts it names.

    Profiles and resources it names are still read from the repository's trusted commit; only its condition
    scripts come from this folder, so each must be a regular file there and not also a repository file.
    """
    if not path.is_absolute():
        raise RuntimeContractError(f"Local reviewer manifest path must be absolute: {path}")
    try:
        if not path.is_file() or _is_reparse_point(path):
            raise RuntimeContractError(f"Local reviewer manifest is not a regular file: {path}")
        if path.stat().st_size > MAX_LOCAL_MANIFEST_BYTES:
            raise RuntimeContractError(f"Local reviewer manifest is too large: {path}")
        manifest = validate_adapter_manifest(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeContractError(f"Local reviewer manifest is not valid UTF-8 JSON: {path}: {exc}") from exc
    for relative in local_reviewer_files(manifest):
        target = path.parent.joinpath(*PurePosixPath(relative).parts)
        if not target.is_file():
            raise RuntimeContractError(f"Local condition script is missing beside the manifest: {relative}")
        _require_safe_snapshot_path(path.parent, target)
    return manifest


def local_reviewer_files(manifest: dict[str, Any]) -> list[str]:
    """Files a local manifest supplies itself: its condition scripts. Everything else comes from the repository."""
    if manifest.get("kind") != "specialists":
        return []
    scripts = {condition["script"] for condition in manifest["conditions"].values()}
    repository = set(manifest["resources"]) | {
        path for specialist in manifest["specialists"] for path in (specialist["profile"], *specialist["resources"])
    }
    clashes = sorted(scripts & repository)
    if clashes:
        raise RuntimeContractError(
            "A local condition script cannot also be a repository profile or resource: " + ", ".join(clashes)
        )
    return sorted(scripts)


def specialist_guidelines(manifest: dict[str, Any]) -> set[str]:
    """Documents only specialists declare as their own resources (the rules a review applies)."""
    if manifest.get("kind") != "specialists":
        return set()
    guidelines = {path for specialist in manifest["specialists"] for path in specialist["resources"]}
    instructions = set(manifest["resources"])
    instructions.update(specialist["profile"] for specialist in manifest["specialists"])
    instructions.update(condition["script"] for condition in manifest["conditions"].values())
    return guidelines - instructions


def materialize_reviewer(
    checkout: Path,
    commit: str,
    manifest: dict[str, Any],
    destination: Path,
    *,
    runner: Runner = subprocess_runner,
    guideline_commit: str | None = None,
    local_root: Path | None = None,
) -> dict[str, str]:
    """Materialize declared reviewer files from the trusted commit.

    For a specialist manifest, guideline_commit (the pull request's base commit) supplies each specialist
    guideline document it contains, so a pull request into a release branch is reviewed against that
    branch's rules. Profiles and shared resources always come from the trusted commit. Condition scripts do
    too, unless the manifest is kept outside the repository: then local_root (the manifest's folder)
    supplies them, and the metadata names them under local_files. Never the pull-request head.
    """
    normalized = validate_adapter_manifest(manifest)
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeContractError("Reviewer materialization destination must be empty")
    if guideline_commit is not None:
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", guideline_commit):
            raise RuntimeContractError("Guideline commit is invalid")
        resolved = _run_git(checkout, runner, "rev-parse", "--verify", f"{guideline_commit}^{{commit}}").lower()
        if resolved != guideline_commit:
            raise RuntimeContractError("Guideline commit did not resolve exactly")
    guidelines = specialist_guidelines(normalized) if guideline_commit is not None else set()
    local = set(local_reviewer_files(normalized)) if local_root is not None else set()
    destination.mkdir(parents=True, exist_ok=True)
    declared = declared_reviewer_files(normalized)
    hashes: dict[str, str] = {}
    guideline_sources: dict[str, str] = {}
    try:
        for relative in declared:
            # local is empty without local_root, and guidelines without guideline_commit.
            if local_root is not None and relative in local:
                source_file = local_root.joinpath(*PurePosixPath(relative).parts)
                _require_safe_snapshot_path(local_root, source_file)
                content = source_file.read_bytes()
            else:
                source = commit
                if guideline_commit is not None and relative in guidelines:
                    if _run_git(checkout, runner, "ls-tree", guideline_commit, "--", relative):
                        source = guideline_commit
                    guideline_sources[relative] = source
                content = _read_git_file(checkout, source, relative, runner)
            target = destination.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            hashes[relative] = hashlib.sha256(content).hexdigest()
        metadata: dict[str, Any] = {
            "schema_version": 1,
            "adapter_id": normalized["id"],
            "entrypoint": normalized.get("entrypoint"),
            "source_commit": commit,
            "source_hashes": hashes,
        }
        if normalized.get("kind") == "specialists":
            metadata["manifest"] = normalized
            if guideline_sources:
                metadata["guideline_sources"] = guideline_sources
            if local:
                metadata["local_files"] = sorted(local)
        atomic_write_json(destination / "materialization.json", metadata)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return hashes


def _is_agent_instruction_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    parts = tuple(part.casefold() for part in path.parts)
    if any(part in {".agents", ".claude", ".codex", ".cursor", ".windsurf"} for part in parts):
        return True
    if path.name.casefold() in {
        "agents.md",
        "agents.override.md",
        "claude.md",
        "claude.local.md",
        "copilot-instructions.md",
        "gemini.md",
    }:
        return True
    if parts and parts[0] == ".github":
        if len(parts) > 1 and parts[1] in {"agents", "instructions", "prompts", "skills"}:
            return True
        if path.name.casefold().endswith(".instructions.md"):
            return True
    return False


def _is_reparse_point(path: Path) -> bool:
    metadata = os.lstat(path)
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _require_safe_snapshot_path(root: Path, target: Path) -> None:
    resolved_root = root.resolve(strict=True)
    current = target
    while True:
        if _is_reparse_point(current):
            raise RuntimeContractError(f"Source snapshot path contains a reparse point: {target}")
        if current == root:
            break
        if current.parent == current:
            raise RuntimeContractError(f"Source snapshot path escapes its root: {target}")
        current = current.parent
    resolved = target.resolve(strict=True)
    if not resolved.is_relative_to(resolved_root):
        raise RuntimeContractError(f"Source snapshot path escapes its root: {target}")


def _snapshot_files(root: Path) -> set[str]:
    files: set[str] = set()
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            child = current_path / directory
            if _is_reparse_point(child):
                raise RuntimeContractError(f"Source snapshot contains a reparse-point directory: {child}")
        for name in names:
            child = current_path / name
            if _is_reparse_point(child) or not child.is_file():
                raise RuntimeContractError(f"Source snapshot contains a non-regular file: {child}")
            files.add(child.relative_to(root).as_posix())
    return files


def verify_source_snapshot(
    root: Path,
    *,
    expected_repository: str,
    expected_commit: str,
    contents: bool = True,
) -> dict[str, Any]:
    """Verify a snapshot against its manifest and return the manifest.

    The structure is always checked: the manifest, the exact file set, sizes against the limits, and that no
    path is a reparse point or escapes the root. `contents=False` skips re-reading and re-hashing every file,
    for a step that runs in the same process as the write with nothing untrusted in between. Under real-time
    antivirus each file read costs milliseconds, so a full pass over a large repository takes minutes.
    """
    if not root.is_absolute() or not root.is_dir() or _is_reparse_point(root):
        raise RuntimeContractError("Source snapshot root must be an existing absolute non-reparse directory")
    metadata = _read_snapshot_metadata(root)
    _require_snapshot_source(metadata, expected_repository, expected_commit)
    hashes, excluded = _snapshot_maps(metadata)
    expected_files = _verify_snapshot_files(root, hashes, contents=contents)
    for relative, reason in excluded.items():
        _validate_snapshot_exclusion(relative, reason, hashes)
    _require_snapshot_file_set(root, expected_files)
    return metadata


def _read_snapshot_metadata(root: Path) -> Any:
    """The snapshot's manifest, once it holds exactly the contract's fields at a supported schema version."""
    metadata_path = root / SOURCE_SNAPSHOT_MANIFEST
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeContractError(f"Source snapshot metadata is invalid: {exc}") from exc
    if not isinstance(metadata, dict) or set(metadata) != {
        "schema_version",
        "repository",
        "source_commit",
        "source_hashes",
        "excluded_paths",
    }:
        raise RuntimeContractError("Source snapshot metadata fields do not match the contract")
    if metadata["schema_version"] != SOURCE_SNAPSHOT_SCHEMA_VERSION:
        raise RuntimeContractError("Source snapshot schema version is unsupported")
    return metadata


def _require_snapshot_source(metadata: dict[str, Any], expected_repository: str, expected_commit: str) -> None:
    """The snapshot is of the requested repository at the requested head."""
    repository = validate_repository_identity(metadata["repository"])
    if repository != validate_repository_identity(expected_repository):
        raise RuntimeContractError("Source snapshot repository does not match the request")
    commit = metadata["source_commit"]
    if commit != expected_commit or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise RuntimeContractError("Source snapshot commit does not match the request head")


def _snapshot_maps(metadata: dict[str, Any]) -> tuple[Any, Any]:
    """The source hashes and the exclusions, once both are objects and the hashes are within the file-count limit."""
    hashes = metadata["source_hashes"]
    excluded = metadata["excluded_paths"]
    if not isinstance(hashes, dict) or not isinstance(excluded, dict):
        raise RuntimeContractError("Source snapshot hashes and exclusions must be objects")
    if len(hashes) > MAX_SOURCE_SNAPSHOT_FILES:
        raise RuntimeContractError("Source snapshot exceeds the file-count limit")
    return hashes, excluded


def _verify_snapshot_files(root: Path, hashes: dict[str, Any], *, contents: bool) -> set[str]:
    """The paths the snapshot holds, its manifest included, once every listed file checks out and their total size
    is within the limit. `contents` also compares each file's hash."""
    expected_files = {SOURCE_SNAPSHOT_MANIFEST}
    total_bytes = 0
    for relative, expected_hash in hashes.items():
        target, size = _snapshot_file(root, relative, expected_hash)
        total_bytes += size
        if total_bytes > MAX_SOURCE_SNAPSHOT_BYTES:
            raise RuntimeContractError("Source snapshot exceeds the size limit")
        if contents and hashlib.sha256(target.read_bytes()).hexdigest() != expected_hash:
            raise RuntimeContractError(f"Source snapshot hash mismatch: {relative}")
        expected_files.add(relative)
    return expected_files


def _snapshot_file(root: Path, relative: str, expected_hash: Any) -> tuple[Path, int]:
    """A listed file's path and size, once its name, its hash's format, its place under the root, and its size check
    out."""
    normalized = _safe_relative_path(relative, "source_hashes path")
    if normalized != relative or relative == SOURCE_SNAPSHOT_MANIFEST:
        raise RuntimeContractError(f"Source snapshot path is invalid: {relative!r}")
    if _is_agent_instruction_path(relative):
        raise RuntimeContractError(f"Source snapshot includes an agent-instruction path: {relative}")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise RuntimeContractError(f"Source snapshot hash is invalid: {relative}")
    target = root.joinpath(*PurePosixPath(relative).parts)
    try:
        _require_safe_snapshot_path(root, target)
        size = target.stat().st_size
    except FileNotFoundError as exc:
        raise RuntimeContractError(f"Source snapshot file is missing: {relative}") from exc
    if size > MAX_CHANGED_FILE_BYTES:
        raise RuntimeContractError(f"Source snapshot file exceeds the size limit: {relative}")
    return target, size


def _validate_snapshot_exclusion(relative: Any, reason: Any, hashes: dict[str, Any]) -> None:
    """One exclusion: an unsafe name the snapshot does not also hold, or a safe path it does not hold, excluded for a
    known reason."""
    if reason == "unsafe-path":
        # Recorded, never written: the name itself is what could not be represented safely.
        if not isinstance(relative, str) or not relative or relative in hashes:
            raise RuntimeContractError(f"Source snapshot exclusion is invalid: {relative!r}")
        return
    normalized = _safe_relative_path(relative, "excluded_paths path")
    if (
        normalized != relative
        or relative in hashes
        or relative == SOURCE_SNAPSHOT_MANIFEST
        or not isinstance(reason, str)
        or reason not in SNAPSHOT_EXCLUSION_REASONS
    ):
        raise RuntimeContractError(f"Source snapshot exclusion is invalid: {relative!r}")


def _require_snapshot_file_set(root: Path, expected_files: set[str]) -> None:
    """The snapshot holds exactly the files its manifest lists, none of them a reparse point or other non-regular
    file."""
    actual_files = _snapshot_files(root)
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        raise RuntimeContractError(f"Source snapshot file set mismatch; missing={missing}, extra={extra}")


def _entry_exclusion(
    name: str, kind: str, size: int, changed_paths: frozenset[str], room: PathRoom
) -> tuple[str, str | None]:
    """A head path's name in the manifest, and why the snapshot leaves it out, or None when its bytes are to be read.

    `kind` is "file", "symbolic-link", or "non-regular". A path one of UNSAFE_PATH_RULES applies to, in `room`, is an
    unsafe path, so a changed file with such a name is a coverage gap and every other pull request is unaffected. A
    name that is not UTF-8 is named with one U+FFFD per undecodable byte, the form the GitHub client gives it in the
    diff. A symbolic link, and any other entry that is not a regular file, is excluded without being read, written,
    or followed; reviewers see a link only as diff text. Raises for a name that would leave the snapshot or name its
    root (an absolute path, or an empty, `.`, or `..` segment), which no tree git accepts holds, and for the reserved
    manifest path.
    """
    if name.startswith("/") or any(segment in {"", ".", ".."} for segment in _segments(name)):
        raise RuntimeContractError(f"source snapshot member is unsafe: {name!r}")
    if unsafe_path_reason(name, room) is not None:
        return replace_undecodable(name)[0], "unsafe-path"
    relative = _safe_relative_path(name, "source snapshot member")
    if relative == SOURCE_SNAPSHOT_MANIFEST:
        raise RuntimeContractError(f"Repository contains reserved snapshot path: {relative}")
    if kind != "file":
        return relative, kind
    if _is_agent_instruction_path(relative):
        return relative, "agent-instruction"
    limit = MAX_CHANGED_FILE_BYTES if relative in changed_paths else MAX_SOURCE_FILE_BYTES
    if size > limit:
        return relative, "file-size-limit"
    return relative, None


def _content_exclusion(relative: str, content: bytes, written: dict[str, str]) -> str | None:
    """The binary exclusion for a file with a NUL byte near its start, else None once `written` records the file.

    Raises for two kept paths a case-insensitive filesystem would merge.
    """
    if b"\0" in content[:BINARY_PROBE_BYTES]:
        return "binary"
    key = relative.casefold()  # a name ending in a dot or a space, which Windows would also merge, is never kept
    if key in written:
        raise RuntimeContractError(
            f"Source paths collide on a case-insensitive filesystem: {written[key]} and {relative}"
        )
    written[key] = relative
    return None


def _write_object_ids(stdin: IO[bytes], blobs: Sequence[str]) -> None:
    """Feed `git cat-file --batch` its requests, then close its input so it exits once it has answered them.

    A broken pipe means the reader stopped and ended git; the reader reports why, so the error is not repeated here.
    """
    with suppress(OSError), stdin:
        for blob in blobs:
            stdin.write(f"{blob}\n".encode("ascii"))


def _read_batch_blob(stdout: GitStream, blob: str) -> bytes:
    """One `git cat-file --batch` answer: a `<id> blob <size>` line, exactly that many bytes, and a newline."""
    header = stdout.readline()
    fields = header.split()
    if len(fields) == 2 and fields[1] == b"missing":
        raise RuntimeContractError(f"Blob {blob} is missing from the checkout's object store")
    if len(fields) != 3 or fields[0] != blob.encode("ascii") or fields[1] != b"blob" or not fields[2].isdigit():
        raise RuntimeContractError(f"git cat-file answered {blob} unexpectedly: {header[:200]!r}")
    size = int(fields[2])
    content = stdout.read(size)
    if len(content) != size or stdout.read(1) != b"\n":
        raise RuntimeContractError(f"git cat-file cut blob {blob} short")
    return content


def git_blob_reader(checkout: Path, blobs: Sequence[str]) -> Generator[bytes, None, None]:
    """The exact bytes of each blob, in order, streamed from one `git cat-file --batch`.

    cat-file applies no attribute, filter, or line-ending conversion. It runs through skill-core's client, with no
    prompt, and each wait for its output is bounded, so a large tree that keeps arriving is never cut off while a
    stalled git is. The ids are written from a thread, so a large answer never waits on a full input pipe. Stopping
    early closes git's output, so its next write fails and it exits by itself; it is killed only if it has not
    exited a minute later. A killed process can hold its working directory, the checkout, for a moment after it is
    reported gone, which made removing the checkout fail.
    """
    for blob in blobs:
        if not GIT_OBJECT_ID.fullmatch(blob):
            raise RuntimeContractError(f"Not a git object id: {blob!r}")
    writer: threading.Thread | None = None
    try:
        with GitClient().stream(["cat-file", "--batch"], directory=checkout) as stream:
            writer = threading.Thread(target=_write_object_ids, args=(stream.stdin, blobs), daemon=True)
            writer.start()
            for blob in blobs:
                yield _read_batch_blob(stream, blob)
            if stream.read(1):
                raise RuntimeContractError("git cat-file printed more than it was asked for")
            if stream.wait() != 0:
                raise RuntimeContractError(stream.stderr().strip() or "git cat-file failed")
    except GitError as exc:
        raise RuntimeContractError(str(exc)) from exc
    finally:
        if writer is not None:
            writer.join(GIT_WRITER_SECONDS)  # git is gone, so a write it was blocked on has failed


def _commit_members(
    checkout: Path,
    commit: str,
    runner: Runner,
    blob_reader: BlobReader,
    changed_paths: frozenset[str],
    room: PathRoom,
) -> Generator[SnapshotMember, None, None]:
    """Each path of the commit's tree, as (path, content bytes) when the snapshot keeps it or (path, reason) when not.

    The paths come from `git ls-tree` and the bytes from the object store, never from `git archive` or a working
    tree, so no .gitattributes entry (export-ignore, export-subst, eol, a filter) and no line-ending setting can leave
    a file out or change a byte. A submodule has no blob here and is skipped. The count and size limits are the
    caller's to apply.
    """
    # -z ends each entry with NUL and quotes no name, so stripping the listing's whitespace never touches a path.
    listing = _run_git(checkout, runner, "ls-tree", "-r", "-z", "-l", "--full-tree", commit)
    pending: list[tuple[str, str, int]] = []
    for record in listing.split("\0"):
        if not record:
            continue
        fields, separator, name = record.partition("\t")
        parts = fields.split()
        if not separator or len(parts) != 4:
            raise RuntimeContractError(f"git ls-tree printed an unexpected entry: {record!r}")
        mode, kind, blob, size = parts
        if kind == "commit":
            continue
        if kind != "blob" or not GIT_OBJECT_ID.fullmatch(blob) or not size.isdigit():
            raise RuntimeContractError(f"git ls-tree printed an unexpected entry: {record!r}")
        entry = "symbolic-link" if mode == "120000" else "file" if mode.startswith("100") else "non-regular"
        relative, reason = _entry_exclusion(name, entry, int(size), changed_paths, room)
        if reason is None:
            pending.append((relative, blob, int(size)))
        else:
            yield relative, reason
    written: dict[str, str] = {}
    blobs = iter(blob_reader(checkout, [blob for _, blob, _ in pending]))
    try:
        for relative, _, length in pending:
            content = next(blobs, None)
            if content is None:
                raise RuntimeContractError("git cat-file returned fewer blobs than the commit's tree lists")
            if len(content) != length:
                raise RuntimeContractError(f"Source blob size changed: {relative}")
            yield relative, _content_exclusion(relative, content, written) or content
    finally:
        close = getattr(blobs, "close", None)
        if close is not None:  # a generator reader ends its git process
            close()


def _tarball_members(
    archive: Path, changed_paths: frozenset[str], room: PathRoom
) -> Generator[SnapshotMember, None, None]:
    """Each entry of GitHub's tarball below its top folder, as (path, content bytes) when the snapshot keeps it or
    (path, reason) when not. The count and size limits are the caller's to apply."""
    written: dict[str, str] = {}
    with tarfile.open(archive, mode="r:gz") as source:
        for member in source:
            parts = PurePosixPath(member.name).parts[1:]
            if member.isdir() or not parts:
                continue
            kind = "file" if member.isfile() else "symbolic-link" if member.issym() else "non-regular"
            name = PurePosixPath(*parts).as_posix()
            relative, reason = _entry_exclusion(name, kind, member.size, changed_paths, room)
            if reason is not None:
                yield relative, reason
                continue
            extracted = source.extractfile(member)
            if extracted is None:
                raise RuntimeContractError(f"Cannot read source snapshot member: {relative}")
            content = extracted.read()
            if len(content) != member.size:
                raise RuntimeContractError(f"Source snapshot member size changed: {relative}")
            yield relative, _content_exclusion(relative, content, written) or content


def _populate_snapshot(
    members: Iterable[SnapshotMember], destination: Path, *, repository: str, commit: str
) -> dict[str, Any]:
    hashes: dict[str, str] = {}
    excluded: dict[str, str] = {}
    total_bytes = 0
    directories: set[Path] = set()
    pending: list[Future[int]] = []
    # Real-time antivirus scans each new file, which costs milliseconds of latency per file but little CPU, so
    # overlapping the writes hides most of it. Directories are created and checked once, before their files.
    with ThreadPoolExecutor(SNAPSHOT_WRITE_WORKERS) as pool:
        for relative, content in members:
            if isinstance(content, str):
                excluded[relative] = content
                continue
            if len(hashes) >= MAX_SOURCE_SNAPSHOT_FILES:
                raise RuntimeContractError("Source snapshot exceeds the file-count limit")
            total_bytes += len(content)
            if total_bytes > MAX_SOURCE_SNAPSHOT_BYTES:
                raise RuntimeContractError("Source snapshot exceeds the size limit")
            target = destination.joinpath(*PurePosixPath(relative).parts)
            if target.parent not in directories:
                target.parent.mkdir(parents=True, exist_ok=True)
                _require_safe_snapshot_path(destination, target.parent)
                directories.add(target.parent)
            hashes[relative] = hashlib.sha256(content).hexdigest()
            pending.append(pool.submit(target.write_bytes, content))
            if len(pending) >= SNAPSHOT_WRITE_WORKERS * 4:  # bound the file contents held in memory
                pending.pop(0).result()
        for write in pending:
            write.result()
    metadata = {
        "schema_version": SOURCE_SNAPSHOT_SCHEMA_VERSION,
        "repository": repository,
        "source_commit": commit,
        "source_hashes": hashes,
        "excluded_paths": excluded,
    }
    atomic_write_json(destination / SOURCE_SNAPSHOT_MANIFEST, metadata)
    # The hashes were computed from the bytes just written, in this process; re-reading them proves nothing more.
    return verify_source_snapshot(destination, expected_repository=repository, expected_commit=commit, contents=False)


def _prepare_destination(destination: Path) -> None:
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeContractError("Source snapshot destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)


def materialize_source_snapshot(
    checkout: Path,
    repository: str,
    commit: str,
    destination: Path,
    *,
    runner: Runner = subprocess_runner,
    changed_paths: Iterable[str] = (),
    blob_reader: BlobReader = git_blob_reader,
) -> dict[str, Any]:
    """Snapshot the exact commit from a local checkout's object store (no worktree or branch change)."""
    repository = validate_repository_identity(repository)
    if not GIT_OBJECT_ID.fullmatch(commit):
        raise RuntimeContractError("Source snapshot commit is invalid")
    verify_checkout_remote(checkout, repository, runner)
    resolved_commit = _run_git(checkout, runner, "rev-parse", "--verify", f"{commit}^{{commit}}").lower()
    if resolved_commit != commit:
        raise RuntimeContractError("Source snapshot commit did not resolve exactly")
    _prepare_destination(destination)
    try:
        room = path_room(destination)
        members = _commit_members(checkout, commit, runner, blob_reader, frozenset(changed_paths), room)
        with closing(members):
            return _populate_snapshot(members, destination, repository=repository, commit=commit)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


@dataclass(frozen=True)
class SnapshotSize:
    """What a source snapshot of one commit would hold, measured with the snapshot's own rules."""

    files: int
    bytes: int
    excluded: dict[str, int]  # exclusion reason -> file count
    directories: dict[str, int]  # top-level directory ("" for root files) -> bytes kept

    def limit_error(self) -> str | None:
        """Why prepare would refuse this snapshot, naming the largest directories, or None when it fits."""
        if self.files > MAX_SOURCE_SNAPSHOT_FILES:
            return f"it would hold {self.files} files, over the {MAX_SOURCE_SNAPSHOT_FILES}-file limit"
        if self.bytes <= MAX_SOURCE_SNAPSHOT_BYTES:
            return None
        largest = sorted(self.directories.items(), key=lambda item: (-item[1], item[0]))[:5]
        return (
            f"it would hold {self.bytes / 2**20:.1f} MiB, over the {MAX_SOURCE_SNAPSHOT_BYTES / 2**20:.0f} MiB limit; "
            "largest top-level directories: "
            + ", ".join(f"{name or '(root)'} {size / 2**20:.1f} MiB" for name, size in largest)
        )


def measure_source_snapshot(
    checkout: Path,
    commit: str,
    *,
    destination: Path,
    runner: Runner = subprocess_runner,
    changed_paths: Iterable[str] = (),
    blob_reader: BlobReader = git_blob_reader,
) -> SnapshotSize:
    """Measure the snapshot materialize_source_snapshot would write for a commit at `destination`, without writing it.

    It applies the same exclusions, the room `destination` leaves each path included, and fails on the same
    unrepresentable entries, but not on the count or size limits, so a commit over them reports how far over it is.
    """
    excluded: dict[str, int] = {}
    directories: dict[str, int] = {}
    files = 0
    room = path_room(destination)
    with closing(_commit_members(checkout, commit, runner, blob_reader, frozenset(changed_paths), room)) as members:
        for relative, content in members:
            if isinstance(content, str):
                excluded[content] = excluded.get(content, 0) + 1
                continue
            files += 1
            top = relative.split("/", 1)[0] if "/" in relative else ""
            directories[top] = directories.get(top, 0) + len(content)
    return SnapshotSize(files, sum(directories.values()), excluded, directories)


def _git_blob_id(algorithm: str, size: int, chunks: Iterable[bytes]) -> str:
    """The git object id of a blob of `size` bytes, hashed as git hashes it: a `blob <size>` header, NUL, content."""
    digest = hashlib.new(algorithm)
    digest.update(f"blob {size}\0".encode("ascii"))
    total = 0
    for chunk in chunks:
        digest.update(chunk)
        total += len(chunk)
    if total != size:
        raise RuntimeContractError("A tarball entry's size changed while it was read")
    return digest.hexdigest()


def _chunks(stream: IO[bytes]) -> Iterator[bytes]:
    while chunk := stream.read(1024 * 1024):
        yield chunk


def _listed_blobs(tree: Any) -> Counter[tuple[str, str, str]]:
    """Each blob GitHub's trees API lists, as (path, kind, id); folders and submodules have none."""
    if not isinstance(tree, dict) or not isinstance(tree.get("tree"), list):
        raise RuntimeContractError("the tree listing is malformed")
    if tree.get("truncated") is not False:
        raise RuntimeContractError("GitHub truncated the tree listing")
    blobs: Counter[tuple[str, str, str]] = Counter()
    for entry in tree["tree"]:
        if not isinstance(entry, dict) or entry.get("type") not in {"blob", "tree", "commit"}:
            raise RuntimeContractError("the tree listing is malformed")
        if entry["type"] != "blob":
            continue
        path, mode, blob = entry.get("path"), entry.get("mode"), entry.get("sha")
        if not isinstance(path, str) or not path or not isinstance(mode, str) or not isinstance(blob, str):
            raise RuntimeContractError("the tree listing is malformed")
        if not GIT_OBJECT_ID.fullmatch(blob):
            raise RuntimeContractError("the tree listing is malformed")
        blobs[(path, "symbolic-link" if mode == "120000" else "file", blob)] += 1
    return blobs


def _tarball_blobs(archive: Path, algorithm: str) -> Counter[tuple[str, str, str]]:
    """Each entry of GitHub's tarball below its top folder as (path, kind, git object id), a name that is not UTF-8
    with one U+FFFD per undecodable byte, as GitHub's listing gives it. An entry a tree cannot hold has no id."""
    blobs: Counter[tuple[str, str, str]] = Counter()
    with tarfile.open(archive, mode="r:gz") as source:
        for member in source:
            parts = PurePosixPath(member.name).parts[1:]
            if member.isdir() or not parts:
                continue
            name = replace_undecodable(PurePosixPath(*parts).as_posix())[0]
            if member.issym():
                target = member.linkname.encode("utf-8", "surrogateescape")
                blobs[(name, "symbolic-link", _git_blob_id(algorithm, len(target), [target]))] += 1
            elif member.isfile():
                extracted = source.extractfile(member)
                if extracted is None:
                    raise RuntimeContractError(f"Cannot read tarball entry {json.dumps(name)}")
                blobs[(name, "file", _git_blob_id(algorithm, member.size, _chunks(extracted)))] += 1
            else:
                blobs[(name, "non-regular", "")] += 1
    return blobs


def verify_github_tarball(archive: Path, tree: Any, *, repository: str, commit: str) -> None:
    """Fail unless GitHub's tarball holds exactly the blobs of the commit's tree, as GitHub's trees API lists them.

    GitHub builds the tarball with `git archive`, which honours the commit's own .gitattributes: export-ignore leaves
    a file out, and export-subst and eol rewrite one. A tarball that is not the exact tree is refused, never
    snapshotted, so reviewers never read bytes the commit does not hold or miss a file it does.
    """
    where = f"GitHub's tarball of {repository}@{commit[:12]}"
    fix = "set checkout_path for this repository to snapshot it from git objects"
    try:
        expected = _listed_blobs(tree)
        actual = _tarball_blobs(archive, "sha1" if len(commit) == 40 else "sha256")
    except (RuntimeContractError, tarfile.TarError, EOFError, OSError) as exc:
        raise RuntimeContractError(f"{where} cannot be checked: {exc}; {fix}") from exc
    differing = sorted({path for path, _, _ in (expected - actual) + (actual - expected)})
    if differing:
        named = ", ".join(json.dumps(path) for path in differing[:5])
        more = f" and {len(differing) - 5} more" if len(differing) > 5 else ""
        raise RuntimeContractError(
            f"{where} is not the commit's exact tree: {named}{more} differ. The repository's .gitattributes can leave "
            f"a file out of an archive or rewrite it (export-ignore, export-subst, eol); {fix}"
        )


def github_tarball_fetcher(repository: str, commit: str, target: Path, github: GitHubClient | None = None) -> None:
    """Download GitHub's tarball of the commit to `target`, checked against the commit's tree."""
    client = github or GitHubClient()
    try:
        client.download(["api", f"repos/{repository}/tarball/{commit}"], target)
        tree = client.json(["api", f"repos/{repository}/git/trees/{commit}?recursive=1"])
    except GitHubError as exc:
        raise RuntimeContractError(f"Cannot download {repository}@{commit}: {exc}") from exc
    verify_github_tarball(target, tree, repository=repository, commit=commit)


def materialize_source_snapshot_from_github(
    repository: str,
    commit: str,
    destination: Path,
    *,
    fetcher: Callable[[str, str, Path], None] = github_tarball_fetcher,
    changed_paths: Iterable[str] = (),
) -> dict[str, Any]:
    """Snapshot the exact commit from GitHub's tarball, for repositories without a configured checkout.

    The fetcher is responsible for the tarball being the exact commit; `github_tarball_fetcher` checks it against
    the commit's tree.
    """
    repository = validate_repository_identity(repository)
    if not GIT_OBJECT_ID.fullmatch(commit):
        raise RuntimeContractError("Source snapshot commit is invalid")
    _prepare_destination(destination)
    try:
        with tempfile.TemporaryDirectory(prefix="code-review-source-") as temporary:
            archive = Path(temporary) / "source.tar.gz"
            fetcher(repository, commit, archive)
            members = _tarball_members(archive, frozenset(changed_paths), path_room(destination))
            with closing(members):
                return _populate_snapshot(members, destination, repository=repository, commit=commit)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def build_adapter_request(
    *,
    mode: str,
    repository: str,
    pull_number: int,
    base_ref: str,
    base_sha: str,
    head_sha: str,
    title: str,
    url: str,
    diff_path: Path,
    source_snapshot_root: Path,
    prior_findings: list[dict[str, Any]] | None = None,
    github_comments: list[dict[str, Any]] | None = None,
    head_ref: str | None = None,
    verify_contents: bool = True,
) -> dict[str, Any]:
    """`verify_contents=False` is for a caller that materialized the snapshot itself, moments earlier."""
    if mode not in {"initial", "re-review"}:
        raise RuntimeContractError("Review request mode is invalid")
    repository = validate_repository_identity(repository)
    if not isinstance(pull_number, int) or isinstance(pull_number, bool) or pull_number < 1:
        raise RuntimeContractError("Pull number must be positive")
    for value, field in ((base_sha, "base_sha"), (head_sha, "head_sha")):
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
            raise RuntimeContractError(f"{field} is invalid")
    if not diff_path.is_absolute() or not diff_path.is_file():
        raise RuntimeContractError("diff_path must name an existing absolute file")
    snapshot = verify_source_snapshot(
        source_snapshot_root,
        expected_repository=repository,
        expected_commit=head_sha,
        contents=verify_contents,
    )
    return {
        "protocol_version": ADAPTER_PROTOCOL_VERSION,
        "mode": mode,
        "repository": repository,
        "pull_number": pull_number,
        "pull_request": {
            "title": title,
            "url": url,
            "base_ref": base_ref,
            "base_sha": base_sha,
            "head_sha": head_sha,
            **({"head_ref": head_ref} if head_ref else {}),
        },
        "diff_path": str(diff_path),
        "source_snapshot": {
            "root": str(source_snapshot_root),
            "manifest_path": str(source_snapshot_root / SOURCE_SNAPSHOT_MANIFEST),
            "source_commit": head_sha,
        },
        "prior_findings": prior_findings or [],
        "github_comments": github_comments or [],
        "coverage": {"unavailable_sources": unavailable_sources(diff_path, snapshot)},
    }


COVERAGE_GAP_REASONS = {"file-size-limit", "unsafe-path"}


def unavailable_sources(diff_path: Path, snapshot: dict[str, Any]) -> list[str]:
    """Changed files whose source the snapshot could not provide (too large, or an unsafe name).

    Binary, agent-instruction, symbolic-link, and non-regular exclusions are deliberate and reviewed from the diff
    alone.
    """
    from review_specialists import SpecialistError, parse_unified_diff

    try:
        changed = parse_unified_diff(read_diff(diff_path))
    except SpecialistError as exc:
        raise RuntimeContractError(f"Cannot read changed paths from the diff: {exc}") from exc
    gaps = {
        _undecodable_form(path) for path, reason in snapshot["excluded_paths"].items() if reason in COVERAGE_GAP_REASONS
    }
    return sorted(path for path in changed if _undecodable_form(path) in gaps)


REPLACEMENT_RUN = re.compile(f"{chr(0xFFFD)}+")


def _undecodable_form(path: str) -> str:
    """The path with each run of U+FFFD as one, so the forms of one undecodable name match: the snapshot and the
    GitHub client replace each byte that is not UTF-8, and the diff parser an octal-quoted name's whole invalid
    sequence. Two names this merges can only make a review INCOMPLETE that would not be, never hide a gap."""
    return REPLACEMENT_RUN.sub(chr(0xFFFD), path)


def write_adapter_request(path: Path, request: dict[str, Any]) -> None:
    try:
        atomic_write_json(path, request)
    except PersistenceError as exc:
        raise RuntimeContractError(str(exc)) from exc

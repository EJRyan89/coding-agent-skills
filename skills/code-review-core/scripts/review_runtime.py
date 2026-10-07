"""Trusted reviewer materialization and runtime-host capability negotiation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from github_client import CommandResult, GitHubClient, GitHubError
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
RUNTIME_CAPABILITIES = {
    "claude-code": {"agent-delegation", "read-diff", "write-result"},
    "codex": {"agent-delegation", "read-diff", "write-result"},
    "copilot-cli": {"isolated-added-root", "read-diff", "write-result"},
}
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


Runner = Callable[[Sequence[str]], CommandResult]


def subprocess_runner(arguments: Sequence[str]) -> CommandResult:
    """Run a command, capturing bytes and decoding them here, never in subprocess's reader threads.

    stdout is UTF-8 with surrogateescape and its line endings untouched, so `stdout.encode("utf-8",
    "surrogateescape")` is exactly what the command printed. stderr only feeds messages, so a bad byte becomes U+FFFD.
    """
    process = subprocess.run(list(arguments), capture_output=True, check=False)
    return CommandResult(
        process.returncode, process.stdout.decode("utf-8", "surrogateescape"), process.stderr.decode("utf-8", "replace")
    )


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
    supports = value["supports"]
    if (
        not isinstance(supports, list)
        or not supports
        or any(not isinstance(item, str) or item not in {"initial", "re-review"} for item in supports)
        or len(set(supports)) != len(supports)
    ):
        raise RuntimeContractError("Adapter supports must contain unique supported modes")
    capabilities = value["required_capabilities"]
    if (
        not isinstance(capabilities, list)
        or len(set(capabilities)) != len(capabilities)
        or any(not isinstance(item, str) or not item for item in capabilities)
    ):
        raise RuntimeContractError("Adapter required_capabilities is invalid")
    if version == 2:
        if value["kind"] != "specialists":
            raise RuntimeContractError("Adapter manifest kind is unsupported")
        if "agent-delegation" not in capabilities:
            raise RuntimeContractError("Specialist reviewers must require agent-delegation")
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
        for path in declared:
            if path == "materialization.json":
                raise RuntimeContractError("Adapter declares a reserved path")
        return normalized
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
    normalized = dict(value)
    normalized["entrypoint"] = entrypoint
    normalized["resources"] = normalized_resources
    normalized["agent_profiles"] = normalized_agents
    return normalized


def negotiate_capabilities(runtime: str, required: Iterable[str]) -> set[str]:
    available = RUNTIME_CAPABILITIES.get(runtime)
    if available is None:
        raise RuntimeContractError(f"Unknown runtime host: {runtime}")
    missing = sorted(set(required) - available)
    if missing:
        raise RuntimeContractError(f"Runtime {runtime} lacks required capabilities: {', '.join(missing)}")
    return available


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


def _run_git(checkout: Path, runner: Runner, *arguments: str) -> str:
    result = runner(["git", "-C", str(checkout), *arguments])
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
    result = runner(["git", "-C", str(checkout), "show", f"{commit}:{relative}"])
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


def _snapshot_members(
    source: tarfile.TarFile, *, strip_components: int, changed_paths: frozenset[str]
) -> Iterable[tuple[str, bytes | str]]:
    """Each archive file as (path, content bytes) when the snapshot keeps it, or (path, reason) when not.

    A symbolic link, and any other entry that is not a regular file or a directory, is excluded without being read,
    written, or followed; reviewers see a link only as diff text. Raises for an entry that fails the whole
    snapshot: the reserved manifest path, or two paths a case-insensitive filesystem would merge. The count and
    size limits are the caller's to apply.
    """
    written: dict[str, str] = {}
    for member in source:
        if member.isdir():
            continue
        parts = PurePosixPath(member.name).parts[strip_components:]
        if not parts:
            continue
        name = PurePosixPath(*parts).as_posix()
        if any(WINDOWS_UNSAFE.search(part) for part in parts):
            yield name, "unsafe-path"
            continue
        relative = _safe_relative_path(name, "source snapshot member")
        if relative == SOURCE_SNAPSHOT_MANIFEST:
            raise RuntimeContractError(f"Repository contains reserved snapshot path: {relative}")
        if member.issym():
            yield relative, "symbolic-link"
            continue
        if not member.isfile():  # a hard link, FIFO, or device entry
            yield relative, "non-regular"
            continue
        if _is_agent_instruction_path(relative):
            yield relative, "agent-instruction"
            continue
        limit = MAX_CHANGED_FILE_BYTES if relative in changed_paths else MAX_SOURCE_FILE_BYTES
        if member.size > limit:
            yield relative, "file-size-limit"
            continue
        extracted = source.extractfile(member)
        if extracted is None:
            raise RuntimeContractError(f"Cannot read source snapshot member: {relative}")
        content = extracted.read()
        if len(content) != member.size:
            raise RuntimeContractError(f"Source snapshot member size changed: {relative}")
        if b"\0" in content[:BINARY_PROBE_BYTES]:
            yield relative, "binary"
            continue
        key = "/".join(part.rstrip(". ").casefold() for part in PurePosixPath(relative).parts)
        if key in written:
            raise RuntimeContractError(
                f"Source paths collide on a case-insensitive filesystem: {written[key]} and {relative}"
            )
        written[key] = relative
        yield relative, content


def _populate_snapshot(
    archive: Path,
    destination: Path,
    *,
    repository: str,
    commit: str,
    mode: Literal["r:", "r:gz"],
    strip_components: int,
    changed_paths: frozenset[str],
) -> dict[str, Any]:
    hashes: dict[str, str] = {}
    excluded: dict[str, str] = {}
    total_bytes = 0
    directories: set[Path] = set()
    pending: list[Future[int]] = []
    # Real-time antivirus scans each new file, which costs milliseconds of latency per file but little CPU, so
    # overlapping the writes hides most of it. Directories are created and checked once, before their files.
    with tarfile.open(archive, mode=mode) as source, ThreadPoolExecutor(SNAPSHOT_WRITE_WORKERS) as pool:
        for relative, content in _snapshot_members(
            source, strip_components=strip_components, changed_paths=changed_paths
        ):
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
) -> dict[str, Any]:
    """Snapshot the exact commit from a local checkout's object store (no worktree or branch change)."""
    repository = validate_repository_identity(repository)
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise RuntimeContractError("Source snapshot commit is invalid")
    verify_checkout_remote(checkout, repository, runner)
    resolved_commit = _run_git(checkout, runner, "rev-parse", "--verify", f"{commit}^{{commit}}").lower()
    if resolved_commit != commit:
        raise RuntimeContractError("Source snapshot commit did not resolve exactly")
    _prepare_destination(destination)
    try:
        with tempfile.TemporaryDirectory(prefix="code-review-source-") as temporary:
            archive = Path(temporary) / "source.tar"
            _run_git(checkout, runner, "archive", "--format=tar", f"--output={archive}", commit)
            return _populate_snapshot(
                archive,
                destination,
                repository=repository,
                commit=commit,
                mode="r:",
                strip_components=0,
                changed_paths=frozenset(changed_paths),
            )
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
    runner: Runner = subprocess_runner,
    changed_paths: Iterable[str] = (),
) -> SnapshotSize:
    """Measure the snapshot materialize_source_snapshot would write for a commit, without writing it.

    It applies the same exclusions and fails on the same unrepresentable entries, but not on the count or size
    limits, so a commit over them reports how far over it is.
    """
    excluded: dict[str, int] = {}
    directories: dict[str, int] = {}
    files = 0
    with tempfile.TemporaryDirectory(prefix="code-review-measure-") as temporary:
        archive = Path(temporary) / "source.tar"
        _run_git(checkout, runner, "archive", "--format=tar", f"--output={archive}", commit)
        with tarfile.open(archive, mode="r:") as source:
            for relative, content in _snapshot_members(
                source, strip_components=0, changed_paths=frozenset(changed_paths)
            ):
                if isinstance(content, str):
                    excluded[content] = excluded.get(content, 0) + 1
                    continue
                files += 1
                top = relative.split("/", 1)[0] if "/" in relative else ""
                directories[top] = directories.get(top, 0) + len(content)
    return SnapshotSize(files, sum(directories.values()), excluded, directories)


def github_tarball_fetcher(repository: str, commit: str, target: Path, github: GitHubClient | None = None) -> None:
    try:
        (github or GitHubClient()).download(["api", f"repos/{repository}/tarball/{commit}"], target)
    except GitHubError as exc:
        raise RuntimeContractError(f"Cannot download {repository}@{commit}: {exc}") from exc


def materialize_source_snapshot_from_github(
    repository: str,
    commit: str,
    destination: Path,
    *,
    fetcher: Callable[[str, str, Path], None] = github_tarball_fetcher,
    changed_paths: Iterable[str] = (),
) -> dict[str, Any]:
    """Snapshot the exact commit from GitHub's tarball, for repositories without a configured checkout."""
    repository = validate_repository_identity(repository)
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise RuntimeContractError("Source snapshot commit is invalid")
    _prepare_destination(destination)
    try:
        with tempfile.TemporaryDirectory(prefix="code-review-source-") as temporary:
            archive = Path(temporary) / "source.tar.gz"
            fetcher(repository, commit, archive)
            return _populate_snapshot(
                archive,
                destination,
                repository=repository,
                commit=commit,
                mode="r:gz",
                strip_components=1,
                changed_paths=frozenset(changed_paths),
            )
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
    excluded = snapshot["excluded_paths"]
    return sorted(path for path in changed if excluded.get(path) in COVERAGE_GAP_REASONS)


def write_adapter_request(path: Path, request: dict[str, Any]) -> None:
    try:
        atomic_write_json(path, request)
    except PersistenceError as exc:
        raise RuntimeContractError(str(exc)) from exc

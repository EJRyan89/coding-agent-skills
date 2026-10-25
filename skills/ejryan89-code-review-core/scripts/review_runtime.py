"""Trusted reviewer materialization and runtime-host capability negotiation."""

from __future__ import annotations

import fnmatch
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
from contextlib import ExitStack, closing, contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from device_names import is_reserved_device_name
from executable_locator import find_executable
from git_client import GitClient, GitError, GitResult, GitStream, Runner, subprocess_runner
from github_client import GitHubClient, GitHubError, replace_undecodable
from github_remote import github_repository
from link_probe import is_link_or_junction
from path_containment import posix_within, within
from review_config import REVIEWER_EFFORTS, validate_glob_patterns, validate_repository_identity
from review_documents import DOCUMENT_FORMATS, EXTRACTED_WHEN_TEXT, ExtractionRefused, document_format, extract
from review_guard import ROLE_ID as SLUG
from review_io import CONTENT_ERRORS, PersistenceError, atomic_write_json, parse_json, read_diff
from review_records import SUITE_PROFILE_PREFIX, is_suite_profile
from review_vocabularies import DispatchMode
from typed_fields import FieldError, FieldReader

ADAPTER_PROTOCOL_VERSION = 1
SOURCE_SNAPSHOT_SCHEMA_VERSION = 1
SOURCE_SNAPSHOT_MANIFEST = "source-snapshot.json"
SOURCE_SNAPSHOT_FIELDS = frozenset({"schema_version", "repository", "source_commit", "source_hashes", "excluded_paths"})
# A lazy snapshot's manifest also lists, by blob id, each path it can hold that prepare did not write; review_source.py
# writes one when a reviewer asks for it.
SNAPSHOT_FETCHABLE = "fetchable"
# The changed documents the snapshot holds as extracted text rather than their committed bytes, with their format.
SNAPSHOT_EXTRACTED = "extracted"
# The folder at the snapshot's root that holds the text of a document whose own path a reviewer's file reader would
# parse as that format: Claude Code's Read takes any file named .pdf for a PDF and refuses text there. No path of
# the repository may be in it, as none may be the manifest.
HELD_TEXT_FOLDER = "source-snapshot-text"
# The formats whose text is held in HELD_TEXT_FOLDER, at the document's path with this suffix added, rather than at
# the document's own path.
HELD_BESIDE = {"pdf": ".txt"}
SNAPSHOT_OPTIONAL_FIELDS = frozenset({SNAPSHOT_FETCHABLE, SNAPSHOT_EXTRACTED})
MAX_SOURCE_SNAPSHOT_FILES = 50_000
MAX_SOURCE_SNAPSHOT_BYTES = 256 * 1024 * 1024
MAX_SOURCE_FILE_BYTES = 1024 * 1024
BINARY_PROBE_BYTES = 8000
MAX_SEARCH_MATCHES = 200
MAX_MATCH_CHARACTERS = 300
SNAPSHOT_WRITE_WORKERS = 8
GIT_WRITER_SECONDS = 10  # how long the id writer gets to finish once git is gone
MAX_CHANGED_FILE_BYTES = 16 * 1024 * 1024
# The options of a diff git computes as GitHub's diff endpoint does, so a user's configuration cannot change it: no
# color, external diff, or textconv, renames found, three lines of context, the a/ and b/ prefixes, and a submodule's
# change as its commits.
GIT_DIFF_OPTIONS = (
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--find-renames",
    "--unified=3",
    "--submodule=short",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)
SNAPSHOT_EXCLUSION_REASONS = {
    "agent-instruction",
    "binary",
    "configured",
    "file-size-limit",
    "unsafe-path",
    "symbolic-link",
    "non-regular",
    "unextractable",
}
WINDOWS_UNSAFE = re.compile(r'[:<>"|?*\x00-\x1f]')
MAX_SEGMENT_UNITS = 255  # NTFS, ReFS, exFAT, and FAT32 all stop a name at 255 UTF-16 code units
MAX_PATH = 260  # a Windows path without long-path support, its terminating NUL included
LONG_PATH_UNITS = 32_767  # an NT path with long-path support, counted with the volume's device name for the drive
GIT_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# What each runtime gives a review, which is not everything it can do: Copilot CLI starts subagents, but its hooks let a
# tool call through when they time out, so its reviewers stay inline under the seal rather than start as subagents
# whose guard holds only when it runs (the fail-closed decision in docs/design.md).
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
# `uncovered` says what happens to changed files no specialist matches when others match: `review` (the default) gives
# them to the generic reviewer, `ignore` leaves them unreviewed and lists them in the record.
# `finding_categories` names the kinds of problem a finding may be, such as Style or Security; when a manifest gives
# them, every specialist names one per finding instead of each finding taking its specialist's category.
# `fallback_finding_category`, one of them, is the one a reviewer uses when no other fits, such as Other.
OPTIONAL_SPECIALIST_MANIFEST_KEYS = {"uncovered", "finding_categories", "fallback_finding_category"}
FINDING_CATEGORY_MAXIMUM_LENGTH = 60
UNCOVERED_POLICIES = ("review", "ignore")
SPECIALIST_KEYS = {"id", "category", "profile", "include", "exclude", "resources", "when"}
# Optional per-specialist settings a manifest may give. `model` takes the aliases every way of starting a Claude
# subagent accepts (the Agent tool takes no other value; on Bedrock a bare model name can be silently ignored), or
# `inherit` to use the session's model whatever the profile says. `effort` reaches reviewers the Workflow tool starts
# and those a runtime in NATIVE_EFFORT_RUNTIMES starts.
# Either may be null, which validates to an absent key, so a maintainer can keep both keys in the manifest and set or
# clear them without removing them; the configuration's own `reviewer_effort` takes null the same way.
# `shares_files`, when true, has the specialist review its matched files beside whoever else reviews them: its matches
# take no file from the generic reviewer, skip none when its condition is closed, and own no prior finding or comment.
OPTIONAL_SPECIALIST_KEYS = {"model", "effort", "shares_files"}
MODEL_ALIASES = frozenset({"sonnet", "opus", "haiku", "fable"})
# The runtimes whose native delegation starts a subagent on one of MODEL_ALIASES. Codex's takes only its own model
# identifiers, so there a reviewer starts on the session's model.
MODEL_ALIAS_RUNTIMES = frozenset({"claude-code"})
# The runtimes whose native delegation starts a subagent at a reasoning effort it is given, in the values
# REVIEWER_EFFORTS names: Codex's spawn_agent takes `reasoning_effort`. Claude Code's reviewers take theirs through
# the Workflow tool, and the Copilot CLI host takes `--reasoning-effort`.
NATIVE_EFFORT_RUNTIMES = frozenset({"codex"})
GENERIC_SPECIALIST = "generic-review"
# Profiles the suite ships, which a specialist names as `suite:<name>` in place of a repository path. Each is read from
# this skill's references folder, never from the repository, and only the names here resolve, so a manifest can reach
# no other file through one.
SUITE_PROFILES = {"design-review": "design-reviewer.md"}
SUITE_REFERENCES = Path(__file__).resolve().parents[1] / "references"
# The specialist a fixture's document stage reaches.
DESIGN_REVIEW_PROFILE = f"{SUITE_PROFILE_PREFIX}design-review"
# The stage a fixture's design document is at, which its pull.json may name as `document_stage`, and the one fixed
# sentence prepare puts in the design-review specialist's prompt for it. The stage is one of these names and never
# text a user wrote, so nothing but the document reaches a reviewer from the person who asked for the review. Each
# sentence points at the bar the profile's "Document stage" section sets.
DOCUMENT_STAGES = {
    "draft": (
        "Document stage: draft. The author marks this document an early iteration, so hold it to the draft bar under "
        '"Document stage" in your instructions: a section it has not written yet, an open question without an owner, '
        "and a requirement it says it will state later are SUGGESTION, while what it has written is judged in full."
    ),
    "review": (
        "Document stage: review. The document is meant to be built from as written, so hold it to the review bar "
        'under "Document stage" in your instructions, the severities they give.'
    ),
    "final": (
        "Document stage: final. The document is about to be built or is already approved, so hold it to the final "
        'bar under "Document stage" in your instructions: a placeholder such as TBD, TODO, or an empty section, and an '
        "open question without an owner that decides what is built, are MUST_FIX."
    ),
}
DEFAULT_DOCUMENT_STAGE = "review"
# The snapshot paths a condition may declare it reads: glob patterns, bounded as a repository's snapshot_exclude is.
MAX_CONDITION_READS = 100
MAX_READ_PATTERN_LENGTH = 200


class RuntimeContractError(ValueError):
    """Raised when a runtime or repository reviewer violates the trust contract."""


class RuntimeContractFieldError(FieldError, RuntimeContractError):
    """A reviewer manifest, source snapshot manifest, or review request field of the wrong kind."""


READ = FieldReader(RuntimeContractFieldError)


# The schema version each reviewer manifest format has, by the name the contract's tables give it.
MANIFEST_FORMATS = {1: "entrypoint-manifest", 2: "specialists-manifest"}


# Reads blobs by id from a checkout's object store, yielding each one's exact bytes in the order asked.
BlobReader = Callable[[Path, Sequence[str]], Iterable[bytes]]
# Reads the first BINARY_PROBE_BYTES of one blob of a checkout's object store, given its id and its size, and no more.
BlobPrefixReader = Callable[[Path, str, int], bytes]
# A snapshot path with its content bytes when the snapshot keeps it, or the reason it leaves the path out.
SnapshotMember = tuple[str, bytes | str]


def output_bytes(text: str) -> bytes:
    """The exact bytes a runner decoded to `text`."""
    return text.encode("utf-8", "surrogateescape")


def has_undecodable(text: str) -> bool:
    """Whether a runner's output holds a byte that is not UTF-8, which surrogateescape keeps as a lone surrogate."""
    return any("\udc80" <= character <= "\udcff" for character in text)


def _safe_relative_path(value: Any, field: str, reader: FieldReader = READ) -> str:
    """A repository-relative POSIX path that stays inside the repository; `reader` reads it as the format holding it."""
    text = reader.string(value, field)
    if "\\" in text:
        raise RuntimeContractError(f"{field} must be a non-empty POSIX relative path")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} or WINDOWS_UNSAFE.search(part) for part in path.parts):
        raise RuntimeContractError(f"{field} is unsafe: {text!r}")
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
    must both allow it, and ntdll answers for the two; without the answer the shorter limits hold, as they do on every
    other platform, so a snapshot holds the paths a Windows reviewer could write wherever it is made."""
    if sys.platform != "win32":
        return False
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
        lambda relative, room: any(is_reserved_device_name(segment) for segment in _segments(relative)),
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


# A path the snapshot leaves out because the repository's configured snapshot_exclude matches it.
Exclusion = Callable[[str], bool]


def _never_excluded(relative: str) -> bool:
    return False


def glob_matcher(pattern: str) -> Exclusion:
    """Whether a snapshot-relative path matches one snapshot_exclude glob, ignoring case, as a whole path.

    `*`, `?`, and `[...]` match within one segment, as fnmatch does; a `**` segment matches any number of segments,
    none included. So `*.resx` matches only a root file, and `**/*.resx` one at any depth.
    """
    segments = [
        None if part == "**" else re.compile(fnmatch.translate(part), re.IGNORECASE) for part in pattern.split("/")
    ]

    def matches(relative: str) -> bool:
        parts = relative.split("/")
        reached = {0}  # how many of the path's segments the pattern's segments so far can consume
        for segment in segments:
            if segment is None:
                reached = set(range(min(reached), len(parts) + 1))
            else:
                reached = {index + 1 for index in reached if index < len(parts) and segment.match(parts[index])}
            if not reached:
                return False
        return len(parts) in reached

    return matches


def configured_exclusion(patterns: Sequence[str]) -> Exclusion:
    """Whether any of a repository's snapshot_exclude globs matches a snapshot-relative path."""
    matchers = [glob_matcher(pattern) for pattern in patterns]
    if not matchers:
        return _never_excluded
    return lambda relative: any(matches(relative) for matches in matchers)


def suite_profile_path(profile: str) -> Path:
    """The suite's file a `suite:<name>` profile names."""
    name = profile.removeprefix(SUITE_PROFILE_PREFIX)
    if not is_suite_profile(profile) or name not in SUITE_PROFILES:
        raise RuntimeContractError(f"No suite profile is named {profile!r}")
    return SUITE_REFERENCES / SUITE_PROFILES[name]


def suite_profiles(manifest: dict[str, Any]) -> list[str]:
    """The distinct suite profiles a validated manifest's specialists name, in order."""
    if manifest.get("kind") != "specialists":
        return []
    profiles = [specialist["profile"] for specialist in manifest["specialists"]]
    return list(dict.fromkeys(profile for profile in profiles if is_suite_profile(profile)))


def _profile(value: Any, field: str) -> str:
    """A specialist's profile: a repository path, or `suite:<name>` for one the suite ships."""
    profile = READ.string(value, field)
    if profile.startswith(SUITE_PROFILE_PREFIX):
        if profile.removeprefix(SUITE_PROFILE_PREFIX) not in SUITE_PROFILES:
            shipped = ", ".join(f"{SUITE_PROFILE_PREFIX}{name}" for name in sorted(SUITE_PROFILES))
            raise RuntimeContractError(f"{field} names no suite profile: {profile!r}; the suite ships {shipped}")
        return profile
    return _safe_relative_path(profile, field)


def _path_list(value: Any, field: str) -> list[str]:
    return READ.array(value, field, _safe_relative_path)


def _regex_list(value: Any, field: str, *, required: bool) -> list[str]:
    patterns = READ.array(value, field, READ.string, nonempty=required)
    for index, pattern in enumerate(patterns):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise RuntimeContractError(f"{field}[{index}] is not a valid regular expression: {exc}") from exc
    return patterns


def _validate_specialists(value: dict[str, Any]) -> dict[str, Any]:
    root = MANIFEST_FORMATS[2]
    conditions = READ.json_object(value["conditions"], f"{root}.conditions")
    normalized_conditions: dict[str, dict[str, Any]] = {}
    for key, condition in conditions.items():
        name = READ.string(key, f"a key of {root}.conditions")
        if not SLUG.fullmatch(name):
            raise RuntimeContractError(f"Adapter condition name is invalid: {name!r}")
        path = f"{root}.conditions.{name}"
        READ.json_object(condition, path, required={"script"}, optional={"reads"})
        normalized_conditions[name] = {"script": _safe_relative_path(condition["script"], f"{path}.script")}
        if "reads" in condition:
            # Matched as snapshot_exclude globs are (`glob_matcher`), so validated as they are.
            normalized_conditions[name]["reads"] = validate_glob_patterns(
                condition["reads"],
                f"{path}.reads",
                maximum_patterns=MAX_CONDITION_READS,
                maximum_length=MAX_READ_PATTERN_LENGTH,
                reader=READ,
            )
    specialists = READ.array(value["specialists"], f"{root}.specialists", nonempty=True)
    normalized_specialists = []
    seen: set[str] = set()
    for index, specialist in enumerate(specialists):
        path = f"{root}.specialists[{index}]"
        READ.json_object(specialist, path, required=SPECIALIST_KEYS, optional=OPTIONAL_SPECIALIST_KEYS)
        model = READ.optional(READ.one_of, specialist.get("model"), f"{path}.model", MODEL_ALIASES | {"inherit"})
        effort = READ.optional(READ.one_of, specialist.get("effort"), f"{path}.effort", REVIEWER_EFFORTS)
        shares = READ.optional(READ.boolean, specialist.get("shares_files"), f"{path}.shares_files")
        identity = READ.string(specialist["id"], f"{path}.id")
        if not SLUG.fullmatch(identity) or identity in seen | {GENERIC_SPECIALIST}:
            raise RuntimeContractError(f"{path}.id is invalid or duplicated")
        seen.add(identity)
        category = READ.string(specialist["category"], f"{path}.category")
        when = READ.optional(READ.string, specialist["when"], f"{path}.when")
        if when is not None and when not in normalized_conditions:
            raise RuntimeContractError(f"{path}.when names an undeclared condition")
        normalized_specialists.append(
            {
                "id": identity,
                "category": category.strip(),
                "profile": _profile(specialist["profile"], f"{path}.profile"),
                "include": _regex_list(specialist["include"], f"{path}.include", required=True),
                "exclude": _regex_list(specialist["exclude"], f"{path}.exclude", required=False),
                "resources": _path_list(specialist["resources"], f"{path}.resources"),
                "when": when,
                **({"model": model} if model is not None else {}),
                **({"effort": effort} if effort is not None else {}),
                **({"shares_files": True} if shares else {}),
            }
        )
    return {
        "conditions": normalized_conditions,
        "specialists": normalized_specialists,
    }


def declared_reviewer_files(manifest: dict[str, Any]) -> list[str]:
    """Every trusted repository file a validated manifest declares, once each, in order. A suite profile is not one."""
    if manifest.get("kind") == "specialists":
        files = list(manifest["resources"])
        for specialist in manifest["specialists"]:
            profile = specialist["profile"]
            files.extend([*([] if is_suite_profile(profile) else [profile]), *specialist["resources"]])
        files.extend(condition["script"] for condition in manifest["conditions"].values())
        return list(dict.fromkeys(files))
    return [manifest["entrypoint"], *manifest["resources"], *manifest["agent_profiles"]]


def _specialists_manifest_roles(manifest: dict[str, Any]) -> list[list[str]]:
    """A specialists manifest's repository files by role, each read from its own commit: the top-level resources,
    the specialists' profiles, their own resources (guidelines, read from the base when it has them), and the
    condition scripts (read from beside a local manifest when there is one). Several specialists may name one
    profile or guideline, and several conditions one script; a suite profile is no repository file."""
    specialists = manifest["specialists"]
    return [
        list(manifest["resources"]),
        [specialist["profile"] for specialist in specialists if not is_suite_profile(specialist["profile"])],
        [path for specialist in specialists for path in specialist["resources"]],
        [condition["script"] for condition in manifest["conditions"].values()],
    ]


def validate_adapter_manifest(value: Any) -> dict[str, Any]:
    version = _manifest_version(value)
    root = MANIFEST_FORMATS[version]
    _validate_supports(value["supports"], root)
    _validate_capabilities(value["required_capabilities"], root)
    if version == 2:
        return _normalized_specialists_manifest(value)
    return _normalized_entrypoint_manifest(value)


def _manifest_version(value: Any) -> int:
    """The schema version of a manifest whose fields match it and whose protocol version and id are valid."""
    manifest = READ.json_object(value, "manifest")
    version = READ.integer(manifest.get("schema_version"), "manifest.schema_version")
    root = MANIFEST_FORMATS.get(version, MANIFEST_FORMATS[1])
    expected_keys = SPECIALIST_MANIFEST_KEYS if version == 2 else MANIFEST_KEYS
    optional_keys = OPTIONAL_SPECIALIST_MANIFEST_KEYS if version == 2 else set()
    READ.json_object(manifest, root, required=expected_keys, optional=optional_keys)
    if (
        version not in MANIFEST_FORMATS
        or READ.integer(manifest["protocol_version"], f"{root}.protocol_version") != ADAPTER_PROTOCOL_VERSION
    ):
        raise RuntimeContractError("Adapter manifest protocol version is unsupported")
    if not SLUG.fullmatch(READ.string(manifest["id"], f"{root}.id")):
        raise RuntimeContractError("Adapter manifest id is invalid")
    return version


def _validate_supports(supports: Any, root: str) -> None:
    modes = READ.array(
        supports,
        f"{root}.supports",
        lambda item, path: READ.one_of(item, path, ("initial", "re-review")),
        nonempty=True,
    )
    if len(set(modes)) != len(modes):
        raise RuntimeContractError("Adapter supports must contain unique supported modes")


def _validate_capabilities(capabilities: Any, root: str) -> None:
    names = READ.array(capabilities, f"{root}.required_capabilities", READ.string)
    if len(set(names)) != len(names):
        raise RuntimeContractError("Adapter required_capabilities is invalid")


def _normalized_specialists_manifest(value: dict[str, Any]) -> dict[str, Any]:
    """A specialists manifest (schema 2) with its paths normalized, once its kind, uncovered policy, specialists, and
    declared files are valid. Whether it lists agent-delegation is negotiated when a review starts: listing it keeps
    its specialists off an inline review."""
    root = MANIFEST_FORMATS[2]
    READ.one_of(value["kind"], f"{root}.kind", ("specialists",))
    if "uncovered" in value:
        READ.one_of(value["uncovered"], f"{root}.uncovered", UNCOVERED_POLICIES)
    if "finding_categories" in value:
        _validate_finding_categories(value["finding_categories"])
    if "fallback_finding_category" in value and READ.string(
        value["fallback_finding_category"], f"{root}.fallback_finding_category"
    ) not in value.get("finding_categories", []):
        raise RuntimeContractError("Adapter fallback_finding_category must be one of its finding_categories")
    normalized = dict(value)
    normalized["resources"] = _path_list(value["resources"], f"{root}.resources")
    normalized.update(_validate_specialists(value))
    # A file has one role, so one purpose and one source: a guideline that is also a shared resource would be read
    # from the base for one and the trusted ref for the other. A list that names a file twice is a mistake.
    roles = [set(role) for role in _specialists_manifest_roles(normalized)]
    lists = [normalized["resources"], *(specialist["resources"] for specialist in normalized["specialists"])]
    if sum(map(len, roles)) != len(set().union(*roles)) or any(len(set(paths)) != len(paths) for paths in lists):
        raise RuntimeContractError("Adapter declares a file more than once")
    _refuse_reserved_paths(declared_reviewer_files(normalized))
    return normalized


def _validate_finding_categories(categories: Any) -> None:
    """A non-empty list of distinct category names, each one line of plain text a prompt can list."""
    names = READ.array(
        categories,
        f"{MANIFEST_FORMATS[2]}.finding_categories",
        lambda item, path: READ.string(item, path, trimmed=True, maximum=FINDING_CATEGORY_MAXIMUM_LENGTH),
        nonempty=True,
    )
    if any(re.search(r'[\r\n`"|]', name) for name in names) or len({name.casefold() for name in names}) != len(names):
        raise RuntimeContractError(
            "Adapter finding_categories must be a non-empty list of distinct one-line names of at most "
            f"{FINDING_CATEGORY_MAXIMUM_LENGTH} characters, without backticks, quotes, or pipes"
        )


def _refuse_reserved_paths(declared: list[str]) -> None:
    """Refuse a declared file at the path of the materialization record, in any case: on a file system that ignores
    case, the record would replace it after its hash is taken."""
    if any(path.casefold() == "materialization.json" for path in declared):
        raise RuntimeContractError("Adapter declares a reserved path")


def _normalized_entrypoint_manifest(value: dict[str, Any]) -> dict[str, Any]:
    """An entrypoint manifest (schema 1) with its paths normalized, once each is safe, none is declared twice, and
    none is the reserved materialization record."""
    root = MANIFEST_FORMATS[1]
    entrypoint = _safe_relative_path(value["entrypoint"], f"{root}.entrypoint")
    normalized_resources = _path_list(value["resources"], f"{root}.resources")
    normalized_agents = _path_list(value["agent_profiles"], f"{root}.agent_profiles")
    declared = [entrypoint, *normalized_resources, *normalized_agents]
    if len(set(declared)) != len(declared):
        raise RuntimeContractError("Adapter declares a file more than once")
    _refuse_reserved_paths(declared)
    normalized = dict(value)
    normalized["entrypoint"] = entrypoint
    normalized["resources"] = normalized_resources
    normalized["agent_profiles"] = normalized_agents
    return normalized


def negotiate_capabilities(runtime: str, required: Iterable[str], *, dispatch: DispatchMode = "subagents") -> set[str]:
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


def choose_dispatch(runtime: str, kind: str, *, inline: bool = False) -> DispatchMode:
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
    """The configured runtime, else the host the orchestrating session states, else the first CLI installed, as
    ejryan89-skill-core's locator finds it: on PATH, else in WinGet's Links folder.

    What is installed does not say which CLI is orchestrating, so it is only the fallback.
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
        if find_executable(command):
            return runtime
    raise RuntimeContractError("No supported local runtime host is available")


def git_in(checkout: Path, runner: Runner, *arguments: str) -> GitResult:
    """Run git in the checkout through ejryan89-skill-core's client, whose `runner` tests replace.

    git reads no stdin, shows no prompt, and stops at the client's time limit, so a fetch from a private remote with
    an expired credential fails instead of waiting on a credential prompt. git that cannot run or finish breaks the
    contract like a failed command; one that runs and fails returns its result for the caller to judge.
    """
    try:
        return GitClient(runner).run(arguments, directory=checkout)
    except GitError as exc:
        raise RuntimeContractError(f"git {arguments[0]} failed in {checkout}: {exc}") from exc


# A full object id, alone or peeled to a kind: what it names cannot change while the object exists.
FULL_OBJECT_NAME = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})(?:\^\{(?:commit|tree|blob)\})?")
# The git subcommands that only read: run through a PrepareGitRunner, they leave its kept answers in place.
READ_ONLY_GIT = frozenset({"cat-file", "ls-remote", "ls-tree", "rev-parse"})


def _reused_query(arguments: Sequence[str]) -> bool:
    """Whether one prepare may answer these git arguments from an earlier identical call: origin's URL, which only a
    command that is not a read can change, or a query of an object by its full id, whose answer never changes."""
    match list(arguments):
        case ["remote", "get-url", "origin"]:
            return True
        case ["rev-parse", "--verify", name] | ["cat-file", "-e", name]:
            return bool(FULL_OBJECT_NAME.fullmatch(name))
        case ["ls-tree", *rest]:
            treeish = next((argument for argument in rest if not argument.startswith("-")), "")
            return bool(FULL_OBJECT_NAME.fullmatch(treeish))
        case _:
            return False


def _read_only(arguments: Sequence[str]) -> bool:
    """Whether git only reads with these arguments; one this cannot tell is taken as one that may write."""
    match list(arguments):
        case [command, *_] if command in READ_ONLY_GIT:
            return True
        case ["remote", "get-url", name]:
            return not name.startswith("-")
        case _:
            return False


class PrepareGitRunner:
    """The git runner of one prepare, used by one thread: a query `_reused_query` accepts is answered from the first
    successful answer to the identical command, `git -C <checkout>` and every argument, so the same checkout's origin
    and the same full object id are asked once. A failed answer is never kept. Any command that is not a read, such
    as a fetch or `remote set-url`, drops every kept answer before and after it runs, so nothing a ref or the remote
    could have changed outlives it. Each prepare makes its own, so nothing outlives the prepare either.

    It keeps git's answers, never a verdict: verify_checkout_remote still compares a kept URL with the repository it
    is asked about, and a commit's resolution is still compared with the commit expected."""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self._answers: dict[tuple[str, ...], GitResult] = {}

    def __call__(self, command: Sequence[str], timeout: float) -> GitResult:
        key = tuple(command)
        named = len(key) >= 3 and key[0] == "git" and key[1] == "-C"  # git in a named checkout, as GitClient runs it
        arguments = key[3:] if named else ()
        if not _read_only(arguments):
            self._answers.clear()
            try:
                return self.runner(command, timeout)
            finally:
                self._answers.clear()
        if not _reused_query(arguments):
            return self.runner(command, timeout)
        kept = self._answers.get(key)
        if kept is not None:
            return kept
        result = self.runner(command, timeout)
        if result.returncode == 0:
            self._answers[key] = result
        return result


def _run_git(checkout: Path, runner: Runner, *arguments: str) -> str:
    result = git_in(checkout, runner, *arguments)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise RuntimeContractError(output_bytes(detail).decode("utf-8", "replace"))
    return result.stdout.strip()


def _normalize_remote(value: str) -> str:
    repository = github_repository(value)
    if repository is None:
        raise RuntimeContractError("Checkout origin is not a recognizable GitHub remote")
    return repository.lower()


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


# The path characters one `git ls-tree` call is given, well inside what a Windows command line holds.
LS_TREE_PATH_UNITS = 8_000


@dataclass(frozen=True)
class TreeEntry:
    """One entry of a commit's tree, as `git ls-tree` lists it."""

    mode: str
    kind: str
    object_id: str


def _ls_tree_calls(paths: Iterable[str]) -> list[list[str]]:
    """The paths split into as few `git ls-tree` calls as hold them, each within LS_TREE_PATH_UNITS, and none holding
    a path and a path under it: given both, ls-tree lists the folder's entries rather than the folder itself."""
    calls: list[list[str]] = []
    units: list[int] = []
    for path in dict.fromkeys(paths):
        size = _utf16_units(path) + 3  # its quotes and the space before it
        for index, call in enumerate(calls):
            nested = any(
                posix_within(path, other, strict=True) or posix_within(other, path, strict=True) for other in call
            )
            if not nested and units[index] + size <= LS_TREE_PATH_UNITS:
                call.append(path)
                units[index] += size
                break
        else:
            calls.append([path])
            units.append(size)
    return calls


def _tree_entries(checkout: Path, commit: str, paths: Iterable[str], runner: Runner) -> dict[str, TreeEntry]:
    """The entry the commit's tree holds at each path, from `git ls-tree`, which matches each path literally, with no
    wildcard; a path it does not hold is left out. A call per batch of paths, not per path."""
    entries: dict[str, TreeEntry] = {}
    for call in _ls_tree_calls(paths):
        # -z ends each entry with NUL and quotes no name, so stripping the listing's whitespace never touches a path.
        listing = _run_git(checkout, runner, "ls-tree", "-z", commit, "--", *call)
        for record in listing.split("\0"):
            if not record:
                continue
            fields, separator, name = record.partition("\t")
            parts = fields.split()
            if not separator or len(parts) != 3 or not GIT_OBJECT_ID.fullmatch(parts[2]):
                raise RuntimeContractError(f"git ls-tree printed an unexpected entry: {record!r}")
            if name not in call or name in entries:
                raise RuntimeContractError(f"git ls-tree listed a path it was not asked for: {name!r}")
            entries[name] = TreeEntry(*parts)
    return entries


def _regular_blob(entry: TreeEntry | None, relative: str) -> str:
    """The blob id of a declared reviewer file's tree entry, refusing a missing path, a symbolic link, a folder, and a
    submodule."""
    if entry is None:
        raise RuntimeContractError(f"Declared reviewer file is missing: {relative}")
    if entry.kind != "blob" or entry.mode == "120000":
        raise RuntimeContractError(f"Declared reviewer file is not a regular file: {relative}")
    return entry.object_id


def _read_reviewer_blobs(checkout: Path, blobs: dict[str, str], blob_reader: BlobReader) -> dict[str, bytes]:
    """Each declared reviewer file's exact bytes, read by the blob id its tree entry gave, never by its path again:
    each id once, and each content must hash to its id. A caller that needs text decodes them inside its own
    UnicodeError handler."""
    ids = list(dict.fromkeys(blobs.values()))
    if not ids:
        return {}
    paths = {blob: relative for relative, blob in reversed(blobs.items())}  # the first path naming each id
    contents: dict[str, bytes] = {}
    reader = iter(blob_reader(checkout, ids))
    try:
        for blob in ids:
            try:
                content = next(reader, None)
            except RuntimeContractError as exc:
                raise RuntimeContractError(f"Cannot read declared reviewer file: {paths[blob]}") from exc
            if content is None or not blob_matches(blob, content):
                raise RuntimeContractError(f"Cannot read declared reviewer file: {paths[blob]}")
            contents[blob] = content
    finally:
        close = getattr(reader, "close", None)
        if close is not None:  # a generator reader ends its git process
            close()
    return {relative: contents[blob] for relative, blob in blobs.items()}


def _runner_blob_reader(runner: Runner) -> BlobReader:
    """A reader of each blob by its id with `git cat-file blob` through `runner`, for a read of one file, where a
    batch saves no git start."""

    def read(checkout: Path, blobs: Sequence[str]) -> Iterator[bytes]:
        for blob in blobs:
            result = git_in(checkout, runner, "cat-file", "blob", blob)
            if result.returncode != 0:
                raise RuntimeContractError(f"The checkout did not return blob {blob}")
            yield result.output_bytes()

    return read


def _read_git_file(checkout: Path, commit: str, relative: str, runner: Runner) -> bytes:
    """The committed blob's exact bytes; a caller that needs text decodes them inside its own UnicodeError handler."""
    blob = _regular_blob(_tree_entries(checkout, commit, [relative], runner).get(relative), relative)
    return _read_reviewer_blobs(checkout, {relative: blob}, _runner_blob_reader(runner))[relative]


def load_manifest_from_commit(
    checkout: Path,
    commit: str,
    manifest_path: str,
    *,
    runner: Runner = subprocess_runner,
) -> dict[str, Any]:
    relative = _safe_relative_path(manifest_path, "manifest_path")
    content = _read_git_file(checkout, commit, relative, runner)  # its own refusals are RuntimeContractErrors already
    try:
        manifest = parse_json(content.decode("utf-8-sig"))
    except CONTENT_ERRORS as exc:
        raise RuntimeContractError(f"Adapter manifest is not valid UTF-8 JSON: {exc}") from exc
    return validate_adapter_manifest(manifest)


MAX_LOCAL_MANIFEST_BYTES = 1024 * 1024


def load_manifest_from_file(path: Path) -> dict[str, Any]:
    """A reviewer manifest kept outside the reviewed repository, beside the condition scripts it names.

    Profiles and resources it names are still read from the repository's trusted commit; only its condition
    scripts come from this folder, so each must be a regular file there and not also a repository file.
    """
    if not path.is_absolute():
        raise RuntimeContractError(f"Local reviewer manifest path must be absolute: {path}")
    try:
        if not path.is_file() or is_link_or_junction(path):
            raise RuntimeContractError(f"Local reviewer manifest is not a regular file: {path}")
        if path.stat().st_size > MAX_LOCAL_MANIFEST_BYTES:
            raise RuntimeContractError(f"Local reviewer manifest is too large: {path}")
        value = parse_json(path.read_text(encoding="utf-8-sig"))
    except RuntimeContractError:
        raise
    except (OSError, *CONTENT_ERRORS) as exc:
        raise RuntimeContractError(f"Local reviewer manifest is not valid UTF-8 JSON: {path}: {exc}") from exc
    manifest = validate_adapter_manifest(value)
    for relative in local_reviewer_files(manifest):
        target = path.parent.joinpath(*PurePosixPath(relative).parts)
        if not target.is_file():
            raise RuntimeContractError(f"Local condition script is missing beside the manifest: {relative}")
        _require_safe_snapshot_path(path.parent, target)
    return manifest


def local_reviewer_files(manifest: dict[str, Any]) -> list[str]:
    """Files a validated local manifest supplies itself: its condition scripts. Everything else comes from the
    repository, and validation has refused a script that is also a profile or resource, since no file is declared
    twice."""
    if manifest.get("kind") != "specialists":
        return []
    return sorted({condition["script"] for condition in manifest["conditions"].values()})


def specialist_guidelines(manifest: dict[str, Any]) -> set[str]:
    """Documents specialists declare as their own resources (the rules a review applies). Validation has refused one
    that is also a shared resource, a profile, or a condition script, which are read from the trusted commit."""
    if manifest.get("kind") != "specialists":
        return set()
    return {path for specialist in manifest["specialists"] for path in specialist["resources"]}


def _reviewer_contents(
    checkout: Path,
    commit: str,
    declared: list[str],
    *,
    runner: Runner,
    guideline_commit: str | None,
    guidelines: set[str],
    local_root: Path | None,
    local: set[str],
    blob_reader: BlobReader,
) -> tuple[dict[str, bytes], dict[str, str]]:
    """Each declared reviewer file's exact bytes, and the commit each guideline came from.

    local is empty without local_root, and guidelines without guideline_commit. A guideline the base holds in any form
    is read from the base, and refused there if it is not a regular file; one it lacks, from the trusted commit. Every
    refusal is raised in declared order before any blob is read.
    """
    committed = [relative for relative in declared if relative not in local]
    at_base: dict[str, TreeEntry] = {}
    sources: dict[str, str] = {}
    if guideline_commit is not None:
        at_base = _tree_entries(checkout, guideline_commit, [path for path in committed if path in guidelines], runner)
        sources = {path: guideline_commit if path in at_base else commit for path in committed if path in guidelines}
    trusted = _tree_entries(checkout, commit, [path for path in committed if path not in at_base], runner)
    entries = {**trusted, **at_base}
    contents: dict[str, bytes] = {}
    blobs: dict[str, str] = {}
    for relative in declared:
        if local_root is not None and relative in local:
            source_file = local_root.joinpath(*PurePosixPath(relative).parts)
            _require_safe_snapshot_path(local_root, source_file)
            contents[relative] = source_file.read_bytes()
        else:
            blobs[relative] = _regular_blob(entries.get(relative), relative)
    contents.update(_read_reviewer_blobs(checkout, blobs, blob_reader))
    return contents, sources


def materialize_reviewer(
    checkout: Path,
    commit: str,
    manifest: dict[str, Any],
    destination: Path,
    *,
    runner: Runner = subprocess_runner,
    guideline_commit: str | None = None,
    local_root: Path | None = None,
    blob_reader: BlobReader | None = None,
) -> dict[str, str]:
    """Materialize declared reviewer files from the trusted commit.

    Each commit's entries come from one `git ls-tree` for its files (see `_ls_tree_calls` for when it takes more),
    and every file's bytes from one `git cat-file --batch` (`blob_reader`, by default `git_blob_reader`), by the blob
    id its entry gave.

    For a specialist manifest, guideline_commit (the pull request's base commit) supplies each specialist
    guideline document it contains, so a pull request into a release branch is reviewed against that
    branch's rules. Profiles and shared resources always come from the trusted commit. Condition scripts do
    too, unless the manifest is kept outside the repository: then local_root (the manifest's folder)
    supplies them, and the metadata names them under local_files. Never the pull-request head.

    A suite profile is read where it is, from the suite's references, and never copied: the metadata keeps its
    hash under suite_profiles, and the hashes returned, which the record keeps, name it by its `suite:` reference.
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
    try:
        contents, guideline_sources = _reviewer_contents(
            checkout,
            commit,
            declared,
            runner=runner,
            guideline_commit=guideline_commit,
            guidelines=guidelines,
            local_root=local_root,
            local=local,
            blob_reader=git_blob_reader if blob_reader is None else blob_reader,
        )
        for relative in declared:
            content = contents[relative]
            target = destination.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            hashes[relative] = hashlib.sha256(content).hexdigest()
        suite = {
            profile: hashlib.sha256(suite_profile_path(profile).read_bytes()).hexdigest()
            for profile in suite_profiles(normalized)
        }
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
            if suite:
                metadata["suite_profiles"] = suite
        atomic_write_json(destination / "materialization.json", metadata)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return {**hashes, **suite}


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
    # A `.github` folder at any depth holds Copilot's agents, instructions, prompts, and skills.
    for index, part in enumerate(parts[:-1]):
        if part != ".github":
            continue
        if parts[index + 1] in {"agents", "instructions", "prompts", "skills"}:
            return True
        if path.name.casefold().endswith(".instructions.md"):
            return True
    return False


def _require_safe_snapshot_path(root: Path, target: Path) -> None:
    resolved_root = root.resolve(strict=True)
    current = target
    while True:
        if is_link_or_junction(current):
            raise RuntimeContractError(f"Source snapshot path contains a reparse point: {target}")
        if current == root:
            break
        if current.parent == current:
            raise RuntimeContractError(f"Source snapshot path escapes its root: {target}")
        current = current.parent
    resolved = target.resolve(strict=True)
    if not within(resolved, resolved_root):
        raise RuntimeContractError(f"Source snapshot path escapes its root: {target}")


@dataclass
class _SnapshotTree:
    """What one walk of a snapshot found: each regular file with its size and its modification time in nanoseconds, as
    its directory listing gives them, the entries it refused to enter or count (a reparse point, a folder that resolves
    outside the root, anything else that is neither a folder nor a regular file), and a fault for each refused entry,
    in the order the walk met them."""

    root: Path
    files: dict[str, int] = field(default_factory=dict)
    modified: dict[str, int] = field(default_factory=dict)
    reparse: set[str] = field(default_factory=set)
    escaping: set[str] = field(default_factory=set)
    irregular: set[str] = field(default_factory=set)
    faults: list[str] = field(default_factory=list)

    def size(self, relative: str) -> int:
        """A listed file's size, or the reason it is not a regular file inside the root: the first folder above it, or
        the file itself, that is a reparse point or escapes, else that it is not a regular file, else missing.

        Only a listed file the walk did not count reaches the disk again: the file and each folder above it are
        checked for a reparse point the walk could not see, such as one made after it, before it is called missing.
        """
        size = self.files.get(relative)
        if size is not None:
            return size
        target = self.root.joinpath(*PurePosixPath(relative).parts)
        segments = relative.split("/")
        for depth in range(1, len(segments) + 1):
            above = "/".join(segments[:depth])
            if above in self.reparse:
                raise RuntimeContractError(f"Source snapshot path contains a reparse point: {target}")
            if above in self.escaping:
                raise RuntimeContractError(f"Source snapshot path escapes its root: {target}")
        current = target
        while current != self.root and current.parent != current:
            with suppress(OSError):  # a path that cannot be read is no reparse point found; it is missing below
                if is_link_or_junction(current):
                    raise RuntimeContractError(f"Source snapshot path contains a reparse point: {target}")
            current = current.parent
        if relative in self.irregular:
            raise RuntimeContractError(f"Source snapshot contains a non-regular file: {target}")
        raise RuntimeContractError(f"Source snapshot file is missing: {relative}")

    def require_clean(self) -> None:
        """The walk met nothing it refused."""
        if self.faults:
            raise RuntimeContractError(self.faults[0])


def _walk_snapshot(root: Path) -> _SnapshotTree:
    """Walk the snapshot once, folder by folder, classifying each entry from its directory listing.

    The root is resolved once. Each folder below it is entered only once its listing entry shows no reparse point and
    it resolves inside the root, so the cost is a listing and a resolve per folder, never a call per file or per
    ancestor of a file. A folder that cannot be listed fails the walk.
    """
    tree = _SnapshotTree(root)
    resolved_root = root.resolve(strict=True)
    pending: list[tuple[Path, str]] = [(root, "")]
    while pending:
        folder, prefix = pending.pop()
        try:
            if prefix and not within(folder.resolve(strict=True), resolved_root):
                tree.escaping.add(prefix.removesuffix("/"))
                tree.faults.append(f"Source snapshot path escapes its root: {folder}")
                continue
            with os.scandir(folder) as listing:
                entries = [(entry.name, entry.is_dir(), entry.stat(follow_symlinks=False)) for entry in listing]
        except OSError as exc:
            raise RuntimeContractError(f"Source snapshot folder cannot be read: {folder}: {exc}") from exc
        subfolders: list[tuple[Path, str]] = []
        for name, is_folder, metadata in entries:
            if not is_folder:
                continue
            child, relative = folder / name, prefix + name
            if is_link_or_junction(child, metadata):
                tree.reparse.add(relative)
                tree.faults.append(f"Source snapshot contains a reparse-point directory: {child}")
            else:
                subfolders.append((child, relative + "/"))
        for name, is_folder, metadata in entries:
            if is_folder:
                continue
            child, relative = folder / name, prefix + name
            if is_link_or_junction(child, metadata):
                tree.reparse.add(relative)
            elif stat.S_ISREG(metadata.st_mode):
                tree.files[relative] = metadata.st_size
                tree.modified[relative] = metadata.st_mtime_ns
                continue
            else:
                tree.irregular.add(relative)
            tree.faults.append(f"Source snapshot contains a non-regular file: {child}")
        pending.extend(reversed(subfolders))  # depth first, in listing order
    return tree


def _snapshot_files(root: Path) -> set[str]:
    """The regular files under `root`, once no entry is a reparse point, escapes it, or is neither a folder nor a
    regular file."""
    tree = _walk_snapshot(root)
    tree.require_clean()
    return set(tree.files)


def verify_source_snapshot(
    root: Path,
    *,
    expected_repository: str,
    expected_commit: str,
    contents: bool = True,
) -> dict[str, Any]:
    """Verify a snapshot against its manifest and return the manifest.

    The structure is always checked: the manifest, the exact file set, sizes against the limits, and that no
    path is a reparse point or escapes the root. One walk of the snapshot supplies all of it (see
    `_walk_snapshot`); each listed file is then looked up in what the walk found, and anything the walk refused
    that no listed file explains fails after the manifest's own checks. `contents=False` skips re-reading and
    re-hashing every file, for a step that runs in the same process as the write with nothing untrusted in
    between. Under real-time antivirus each file read costs milliseconds, so a full pass over a large repository
    takes minutes, and it walks the snapshot again after the reads to check the file set against what is there then.

    A lazy snapshot (see `materialize_source_snapshot`) may also hold any of the files its manifest lists as
    fetchable, each within the per-file and total limits, and `contents` checks each one held against its blob id.
    """
    return _verify_snapshot(
        root, expected_repository=expected_repository, expected_commit=expected_commit, contents=contents
    )[0]


def stamp_source_snapshot(root: Path, *, expected_repository: str, expected_commit: str) -> str:
    """The snapshot's stamp, once its structure verifies, for `verify_stamped_snapshot` to tell that no file the
    snapshot was written with has changed since. `prepare` takes it, after writing the snapshot and before anything
    untrusted runs, for a reviewer that starts in a process of its own."""
    metadata, tree, _, manifest_digest = _verify_snapshot(
        root, expected_repository=expected_repository, expected_commit=expected_commit, contents=False
    )
    return _snapshot_stamp(metadata, tree, manifest_digest)


def verify_stamped_snapshot(
    root: Path, *, expected_repository: str, expected_commit: str, stamp: str | None
) -> dict[str, Any]:
    """Verify a snapshot a reviewer in another process is about to read, and return its manifest: the Copilot CLI
    host's check before it starts its reviewer.

    The structure is checked as `verify_source_snapshot` checks it, so a file added, removed, or replaced by a link
    fails here. When `stamp` still matches, every file the snapshot was written with keeps the size and modification
    time it had when `prepare` took the stamp, so the hashes `prepare` computed as it wrote them still hold, and only
    the files a time cannot vouch for are read: each one dated no earlier than the manifest, which `prepare` writes
    last, since a rewrite in the clock tick it was written in keeps its time, and each file a lazy snapshot fetched
    since, against its blob id. A stamp that no longer matches, or none (a run prepared before stamps), has every file
    read and hashed, as `verify_source_snapshot` does by default, so a file changed since `prepare` is refused.
    """
    metadata, tree, _, manifest_digest = _verify_snapshot(
        root, expected_repository=expected_repository, expected_commit=expected_commit, contents=False
    )
    if stamp != _snapshot_stamp(metadata, tree, manifest_digest):
        return verify_source_snapshot(
            root, expected_repository=expected_repository, expected_commit=expected_commit, contents=True
        )
    written = tree.modified[SOURCE_SNAPSHOT_MANIFEST]
    for relative, expected_hash in metadata["source_hashes"].items():
        if tree.modified[relative] < written:
            continue
        _require_hash(root, relative, expected_hash)
    for relative, blob in metadata.get(SNAPSHOT_FETCHABLE, {}).items():
        if relative in tree.files:
            _require_blob(root, relative, blob)
    return metadata


def _snapshot_stamp(metadata: dict[str, Any], tree: _SnapshotTree, manifest_digest: str) -> str:
    """One SHA-256 over the manifest's own SHA-256 and the path, size, and modification time, as the walk listed them,
    of the manifest and of each file it lists by hash. A lazy snapshot's fetched files are left out, since a fetch adds
    them."""
    digest = hashlib.sha256(manifest_digest.encode("ascii"))
    for relative in sorted([SOURCE_SNAPSHOT_MANIFEST, *metadata["source_hashes"]]):
        entry = [relative, tree.files[relative], tree.modified[relative]]
        digest.update(json.dumps(entry).encode("ascii") + b"\n")
    return digest.hexdigest()


def _snapshot_path(root: Path, relative: str) -> Path:
    return root.joinpath(*PurePosixPath(relative).parts)


def _require_hash(root: Path, relative: str, expected_hash: str) -> None:
    """A listed file's bytes hash to its SHA-256 hash."""
    if hashlib.sha256(_snapshot_path(root, relative).read_bytes()).hexdigest() != expected_hash:
        raise RuntimeContractError(f"Source snapshot hash mismatch: {relative}")


def _require_blob(root: Path, relative: str, blob: str) -> None:
    """A fetched file's bytes hash to its blob id."""
    if not blob_matches(blob, _snapshot_path(root, relative).read_bytes()):
        raise RuntimeContractError(f"Source snapshot file does not match its blob: {relative}")


def _verify_snapshot(
    root: Path, *, expected_repository: str, expected_commit: str, contents: bool
) -> tuple[dict[str, Any], _SnapshotTree, int, str]:
    """`verify_source_snapshot`'s manifest, the walk the file set was checked against, the bytes the files it holds
    total, and the SHA-256 of the manifest's bytes."""
    if not root.is_absolute() or not root.is_dir() or is_link_or_junction(root):
        raise RuntimeContractError("Source snapshot root must be an existing absolute non-reparse directory")
    metadata, manifest_digest = _read_snapshot_metadata(root)
    require_snapshot_source(metadata, expected_repository, expected_commit)
    hashes, excluded, fetchable = _snapshot_maps(metadata)
    tree = _walk_snapshot(root)
    expected_files, total_bytes = _verify_snapshot_files(tree, hashes, contents=contents)
    for relative, reason in excluded.items():
        _validate_snapshot_exclusion(relative, reason, hashes)
    _validate_extracted(metadata.get(SNAPSHOT_EXTRACTED, {}), hashes)
    total_bytes = _verify_fetched_files(tree, fetchable, metadata, total_bytes, contents=contents)
    if contents:  # the reads took time, so the file set is checked against a walk made after them
        tree = _walk_snapshot(root)
    _require_snapshot_file_set(tree, expected_files, set(fetchable))
    return metadata, tree, total_bytes, manifest_digest


def _read_snapshot_metadata(root: Path) -> tuple[Any, str]:
    """The snapshot's manifest, once it holds exactly the contract's fields, and a lazy one's fetchable paths, at a
    supported schema version, and the SHA-256 of the bytes it was read from."""
    metadata_path = root / SOURCE_SNAPSHOT_MANIFEST
    try:
        content = metadata_path.read_bytes()
        metadata = parse_json(content.decode("utf-8-sig"))
    except (OSError, *CONTENT_ERRORS) as exc:
        raise RuntimeContractError(f"Source snapshot metadata is invalid: {exc}") from exc
    READ.json_object(
        metadata, SOURCE_SNAPSHOT_MANIFEST, required=SOURCE_SNAPSHOT_FIELDS, optional=SNAPSHOT_OPTIONAL_FIELDS
    )
    version = READ.integer(metadata["schema_version"], f"{SOURCE_SNAPSHOT_MANIFEST}.schema_version")
    if version != SOURCE_SNAPSHOT_SCHEMA_VERSION:
        raise RuntimeContractError("Source snapshot schema version is unsupported")
    return metadata, hashlib.sha256(content).hexdigest()


def require_snapshot_source(metadata: dict[str, Any], expected_repository: str, expected_commit: str) -> None:
    """The snapshot is of the requested repository at the requested head."""
    repository = READ.string(metadata["repository"], f"{SOURCE_SNAPSHOT_MANIFEST}.repository")
    if validate_repository_identity(repository) != validate_repository_identity(expected_repository):
        raise RuntimeContractError("Source snapshot repository does not match the request")
    commit = READ.hex_digest(metadata["source_commit"], f"{SOURCE_SNAPSHOT_MANIFEST}.source_commit", 40, 64)
    if commit != expected_commit:
        raise RuntimeContractError("Source snapshot commit does not match the request head")


def _snapshot_maps(metadata: dict[str, Any]) -> tuple[Any, Any, Any]:
    """The source hashes, the exclusions, and a lazy snapshot's fetchable paths (empty for a full snapshot), once each
    is an object and the files it can hold are within the file-count limit."""
    hashes = READ.json_object(metadata["source_hashes"], f"{SOURCE_SNAPSHOT_MANIFEST}.source_hashes")
    excluded = READ.json_object(metadata["excluded_paths"], f"{SOURCE_SNAPSHOT_MANIFEST}.excluded_paths")
    fetchable = READ.json_object(
        metadata.get(SNAPSHOT_FETCHABLE, {}), f"{SOURCE_SNAPSHOT_MANIFEST}.{SNAPSHOT_FETCHABLE}"
    )
    if len(hashes) + len(fetchable) > MAX_SOURCE_SNAPSHOT_FILES:
        raise RuntimeContractError("Source snapshot exceeds the file-count limit")
    return hashes, excluded, fetchable


def _verify_snapshot_files(tree: _SnapshotTree, hashes: dict[str, Any], *, contents: bool) -> tuple[set[str], int]:
    """The paths the snapshot holds, its manifest included, and their total size, once every listed file checks out
    and the total is within the limit. `contents` also compares each file's hash."""
    expected_files = {SOURCE_SNAPSHOT_MANIFEST}
    total_bytes = 0
    for relative, expected_hash in hashes.items():
        total_bytes += _snapshot_file_size(tree, relative, expected_hash)
        if total_bytes > MAX_SOURCE_SNAPSHOT_BYTES:
            raise RuntimeContractError("Source snapshot exceeds the size limit")
        if contents:
            _require_hash(tree.root, relative, expected_hash)
        expected_files.add(relative)
    return expected_files, total_bytes


def _blob_algorithm(blob: str) -> str:
    """The hash a git object id of this length is: SHA-1 for 40 hexadecimal digits, SHA-256 for 64."""
    return "sha1" if len(blob) == 40 else "sha256"


def _verify_fetched_files(
    tree: _SnapshotTree, fetchable: dict[str, Any], metadata: dict[str, Any], total_bytes: int, *, contents: bool
) -> int:
    """The bytes the snapshot holds once the fetched files are added to `total_bytes`, once each fetchable path is a
    safe name the snapshot neither holds from the start nor excludes, with a blob id of the commit's hash, and each
    one fetched is within the per-file and total limits. `contents` also compares each fetched file's blob id."""
    hashes, excluded = metadata["source_hashes"], metadata["excluded_paths"]
    length = len(metadata["source_commit"])
    for relative, value in fetchable.items():
        normalized = _safe_relative_path(relative, f"a key of {SOURCE_SNAPSHOT_MANIFEST}.{SNAPSHOT_FETCHABLE}")
        blob = READ.hex_digest(value, f"{SOURCE_SNAPSHOT_MANIFEST}.{SNAPSHOT_FETCHABLE}.{relative}", length)
        if (
            normalized != relative
            or relative == SOURCE_SNAPSHOT_MANIFEST
            or _in_held_text_folder(relative)
            or relative in hashes
            or relative in excluded
            or _is_agent_instruction_path(relative)
        ):
            raise RuntimeContractError(f"Source snapshot fetchable path is invalid: {relative!r}")
        size = tree.files.get(relative)
        if size is None:
            continue
        total_bytes += size
        if size > MAX_SOURCE_FILE_BYTES or total_bytes > MAX_SOURCE_SNAPSHOT_BYTES:
            raise RuntimeContractError(f"Source snapshot file exceeds the size limit: {relative}")
        if contents:
            _require_blob(tree.root, relative, blob)
    return total_bytes


def _snapshot_file_size(tree: _SnapshotTree, relative: str, expected_hash: Any) -> int:
    """A listed file's size, once its name, its hash's format, its place under the root, and its size check out."""
    normalized = _safe_relative_path(relative, f"a key of {SOURCE_SNAPSHOT_MANIFEST}.source_hashes")
    if normalized != relative or relative == SOURCE_SNAPSHOT_MANIFEST:
        raise RuntimeContractError(f"Source snapshot path is invalid: {relative!r}")
    if _is_agent_instruction_path(relative):
        raise RuntimeContractError(f"Source snapshot includes an agent-instruction path: {relative}")
    READ.hex_digest(expected_hash, f"{SOURCE_SNAPSHOT_MANIFEST}.source_hashes.{relative}", 64)
    size = tree.size(relative)
    if size > MAX_CHANGED_FILE_BYTES:
        raise RuntimeContractError(f"Source snapshot file exceeds the size limit: {relative}")
    return size


def _validate_snapshot_exclusion(relative: Any, reason: Any, hashes: dict[str, Any]) -> None:
    """One exclusion: an unsafe name the snapshot does not also hold, or a safe path it does not hold, excluded for a
    known reason."""
    path = f"{SOURCE_SNAPSHOT_MANIFEST}.excluded_paths"
    if reason == "unsafe-path":
        # Recorded, never written: the name itself is what could not be represented safely.
        if READ.string(relative, f"a key of {path}", empty=True) in hashes or not relative:
            raise RuntimeContractError(f"Source snapshot exclusion is invalid: {relative!r}")
        return
    normalized = _safe_relative_path(relative, f"a key of {SOURCE_SNAPSHOT_MANIFEST}.excluded_paths")
    READ.one_of(reason, f"{path}.{relative}", SNAPSHOT_EXCLUSION_REASONS)
    if (
        normalized != relative
        or relative in hashes
        or relative == SOURCE_SNAPSHOT_MANIFEST
        or _in_held_text_folder(relative)
    ):
        raise RuntimeContractError(f"Source snapshot exclusion is invalid: {relative!r}")


def _validate_extracted(extracted: Any, hashes: dict[str, Any]) -> None:
    """The documents held as extracted text: each one in a format the snapshot extracts, with its text held where
    that format's text is, and, for a format held beside it, nothing held at its own path. Every file in
    HELD_TEXT_FOLDER is one of theirs."""
    path = f"{SOURCE_SNAPSHOT_MANIFEST}.{SNAPSHOT_EXTRACTED}"
    documents = READ.json_object(extracted, path)
    beside: set[str] = set()
    for key, value in documents.items():
        relative = READ.string(key, f"a key of {path}")
        form = READ.one_of(value, f"{path}.{relative}", tuple(DOCUMENT_FORMATS.values()))
        if _in_held_text_folder(relative):
            raise RuntimeContractError(f"Source snapshot extracted document is invalid: {relative!r}")
        held = held_path(relative, form)
        if held not in hashes or (held != relative and relative in hashes):
            raise RuntimeContractError(f"Source snapshot extracted document is invalid: {relative!r}")
        if held != relative:
            beside.add(held)
    stray = sorted(relative for relative in hashes if _in_held_text_folder(relative) and relative not in beside)
    if stray:
        raise RuntimeContractError(f"Source snapshot holds text of no extracted document: {stray[0]}")


def _require_snapshot_file_set(tree: _SnapshotTree, expected_files: set[str], fetchable: set[str]) -> None:
    """The snapshot holds exactly the files its manifest lists, and any of those it lists as fetchable, none of them
    a reparse point or other non-regular file."""
    tree.require_clean()
    actual_files = set(tree.files)
    missing = sorted(expected_files - actual_files)
    extra = sorted(actual_files - expected_files - fetchable)
    if missing or extra:
        raise RuntimeContractError(f"Source snapshot file set mismatch; missing={missing}, extra={extra}")


def _entry_exclusion(
    name: str, kind: str, size: int, changed_paths: frozenset[str], room: PathRoom, configured: Exclusion
) -> tuple[str, str | None]:
    """A head path's name in the manifest, and why the snapshot leaves it out, or None when its bytes are to be read.

    `kind` is "file", "symbolic-link", or "non-regular". A path one of UNSAFE_PATH_RULES applies to, in `room`, is an
    unsafe path, so a changed file with such a name is a coverage gap and every other pull request is unaffected. A
    name that is not UTF-8 is named with one U+FFFD per undecodable byte, the form the GitHub client gives it in the
    diff. A symbolic link, and any other entry that is not a regular file, is excluded without being read, written,
    or followed; reviewers see a link only as diff text. Raises for a name that would leave the snapshot or name its
    root (an absolute path, or an empty, `.`, or `..` segment), which no tree git accepts holds, and for the reserved
    manifest path and any path in HELD_TEXT_FOLDER. A regular file the repository's snapshot_exclude matches, changed
    or not, is a `configured` exclusion.
    """
    if name.startswith("/") or any(segment in {"", ".", ".."} for segment in _segments(name)):
        raise RuntimeContractError(f"source snapshot member is unsafe: {name!r}")
    if unsafe_path_reason(name, room) is not None:
        return replace_undecodable(name)[0], "unsafe-path"
    relative = _safe_relative_path(name, "source snapshot member")
    if relative == SOURCE_SNAPSHOT_MANIFEST or _in_held_text_folder(relative):
        raise RuntimeContractError(f"Repository contains reserved snapshot path: {relative}")
    if kind != "file":
        return relative, kind
    if _is_agent_instruction_path(relative):
        return relative, "agent-instruction"
    if configured(relative):
        return relative, "configured"
    limit = MAX_CHANGED_FILE_BYTES if relative in changed_paths else MAX_SOURCE_FILE_BYTES
    if size > limit:
        return relative, "file-size-limit"
    return relative, None


def held_path(relative: str, form: str) -> str:
    """Where the snapshot holds the extracted text of the document at `relative` in format `form`: its own path, or
    a path in HELD_TEXT_FOLDER for a format held beside it."""
    suffix = HELD_BESIDE.get(form)
    return relative if suffix is None else f"{HELD_TEXT_FOLDER}/{relative}{suffix}"


def _in_held_text_folder(relative: str) -> bool:
    """Whether a path is in HELD_TEXT_FOLDER, in any case, since a case-insensitive file system merges the spellings."""
    return relative.split("/", 1)[0].casefold() == HELD_TEXT_FOLDER


@dataclass
class _Documents:
    """The changed documents a snapshot reads: each one it holds as extracted text, with its format, and why each
    one it could not extract was refused. `room` is the room the snapshot leaves a path, which a document's text
    held beside it must fit too."""

    changed: frozenset[str]
    room: PathRoom
    extracted: dict[str, str] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)


def _kept_content(
    relative: str, content: bytes, written: dict[str, str], documents: _Documents
) -> tuple[str, bytes | str]:
    """The path the snapshot writes a file it reads at, once `written` records it, and its content, or the file's
    own path and why the snapshot leaves it out.

    A file with a NUL byte near its start is `binary`, unless the pull request changes it and it is a document whose
    text the snapshot extracts; a PDF the pull request changes is extracted whether or not it has one. The text is
    written in the document's place, or, for a format held beside it, at its path in HELD_TEXT_FOLDER; the document
    is `unextractable` when the extraction is refused. Raises for two kept paths a case-insensitive filesystem would
    merge.
    """
    form = document_format(relative) if relative in documents.changed else None
    binary = is_binary(content)
    if form is None or not (binary or form in EXTRACTED_WHEN_TEXT):
        if binary:
            return relative, "binary"
        _claim_name(relative, written)
        return relative, content
    held = held_path(relative, form)
    try:
        if unsafe_path_reason(held, documents.room) is not None:
            raise ExtractionRefused(f"the path its text would be held at, in {HELD_TEXT_FOLDER}, is too long")
        content = extract(form, content)
    except ExtractionRefused as exc:
        documents.refused[relative] = str(exc)
        return relative, "unextractable"
    documents.extracted[relative] = form
    _claim_name(held, written)
    return held, content


def is_binary(content: bytes) -> bool:
    """The snapshot's test of whether a file is binary: a NUL byte in its first BINARY_PROBE_BYTES bytes."""
    return b"\0" in content[:BINARY_PROBE_BYTES]


def _claim_name(relative: str, written: dict[str, str]) -> None:
    """Record a kept path in `written`, keyed as a case-insensitive file system names it, refusing a second path it
    would merge with the first."""
    key = relative.casefold()  # a name ending in a dot or a space, which Windows would also merge, is never kept
    if key in written:
        raise RuntimeContractError(
            f"Source paths collide on a case-insensitive filesystem: {written[key]} and {relative}"
        )
    written[key] = relative


def _write_object_ids(stdin: IO[bytes], blobs: Sequence[str]) -> None:
    """Feed `git cat-file --batch` its requests, then close its input so it exits once it has answered them.

    A broken pipe means the reader stopped and ended git; the reader reports why, so the error is not repeated here.
    """
    with suppress(OSError), stdin:
        for blob in blobs:
            stdin.write(f"{blob}\n".encode("ascii"))


INT_PARSE_ALLOWED = {
    "_batch_blob_size": "git cat-file writes the blob's size",
    "_commit_members": "git ls-tree writes each blob's size",
    "read_blob": "git cat-file -s writes the blob's size",
}


def _batch_blob_size(stdout: GitStream, blob: str) -> int:
    """The size a `git cat-file --batch` answer's `<id> blob <size>` line gives the blob it names."""
    header = stdout.readline()
    fields = header.split()
    if len(fields) == 2 and fields[1] == b"missing":
        raise RuntimeContractError(f"Blob {blob} is missing from the checkout's object store")
    if len(fields) != 3 or fields[0] != blob.encode("ascii") or fields[1] != b"blob" or not fields[2].isdigit():
        raise RuntimeContractError(f"git cat-file answered {blob} unexpectedly: {header[:200]!r}")
    return int(fields[2])


def _read_batch_blob(stdout: GitStream, blob: str) -> bytes:
    """One `git cat-file --batch` answer: a `<id> blob <size>` line, exactly that many bytes, and a newline."""
    size = _batch_blob_size(stdout, blob)
    content = stdout.read(size)
    if len(content) != size or stdout.read(1) != b"\n":
        raise RuntimeContractError(f"git cat-file cut blob {blob} short")
    return content


def git_blob_reader(checkout: Path, blobs: Sequence[str]) -> Generator[bytes, None, None]:
    """The exact bytes of each blob, in order, streamed from one `git cat-file --batch`.

    cat-file applies no attribute, filter, or line-ending conversion. It runs through ejryan89-skill-core's client, with
    no prompt, and each wait for its output is bounded, so a large tree that keeps arriving is never cut off while a
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


def git_blob_prefix(checkout: Path, blob: str, size: int) -> bytes:
    """The first BINARY_PROBE_BYTES of a blob of `size` bytes, from one `git cat-file --batch` that is stopped once
    they have arrived, so the rest of a large blob is never read whole.

    It runs through ejryan89-skill-core's client as `git_blob_reader` does, with no prompt and each wait for output
    bounded, and applies no attribute, filter, or line-ending conversion. Raises unless the object store answers with a
    blob of exactly `size` bytes.
    """
    if not GIT_OBJECT_ID.fullmatch(blob):
        raise RuntimeContractError(f"Not a git object id: {blob!r}")
    wanted = min(size, BINARY_PROBE_BYTES)
    try:
        with GitClient().stream(["cat-file", "--batch"], directory=checkout) as stream:
            stream.stdin.write(f"{blob}\n".encode("ascii"))
            stream.stdin.close()
            if _batch_blob_size(stream, blob) != size:
                raise RuntimeContractError(f"Source blob size changed: {blob}")
            prefix = stream.read(wanted)
    except GitError as exc:
        raise RuntimeContractError(str(exc)) from exc
    if len(prefix) != wanted:
        raise RuntimeContractError(f"git cat-file cut blob {blob} short")
    return prefix


def _size_limit_reason(prefix: bytes) -> str:
    """Why the snapshot leaves out a probed file over the size limit, from its first BINARY_PROBE_BYTES: `binary` when
    the snapshot's own test finds it binary, so coverage can name it as unreviewed, or else `file-size-limit`."""
    return "binary" if is_binary(prefix) else "file-size-limit"


def _commit_members(
    checkout: Path,
    commit: str,
    runner: Runner,
    blob_reader: BlobReader,
    changed_paths: frozenset[str],
    room: PathRoom,
    *,
    upfront: Callable[[str], bool] | None = None,
    fetchable: dict[str, str] | None = None,
    configured: Exclusion = _never_excluded,
    documents: _Documents | None = None,
    probed: Exclusion = _never_excluded,
    prefix_reader: BlobPrefixReader = git_blob_prefix,
) -> Generator[SnapshotMember, None, None]:
    """Each path of the commit's tree, as (path, content bytes) when the snapshot keeps it or (path, reason) when not.
    A changed document's content is its extracted text, which `documents` records (see `_kept_content`).

    The paths come from `git ls-tree` and the bytes from the object store, never from `git archive` or a working
    tree, so no .gitattributes entry (export-ignore, export-subst, eol, a filter) and no line-ending setting can leave
    a file out or change a byte. A submodule has no blob here and is skipped. The count and size limits are the
    caller's to apply.

    With `upfront`, the snapshot is lazy: only the changed paths and those it accepts are read, and every other path
    the snapshot would keep by its name, kind, and size goes into `fetchable` with its blob id before the first blob
    is yielded. Whether such a blob is binary is decided when it is fetched, so the names a case-insensitive file
    system would merge are refused among all of them here.

    A changed file over the size limit that `probed` accepts has only its first BINARY_PROBE_BYTES read, through
    `prefix_reader`, and is `binary` when they are (see `_size_limit_reason`).
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
        relative, reason = _entry_exclusion(name, entry, int(size), changed_paths, room, configured)
        if reason == "file-size-limit" and relative in changed_paths and probed(relative):
            reason = _size_limit_reason(prefix_reader(checkout, blob, int(size)))
        if reason is None:
            pending.append((relative, blob, int(size)))
        else:
            yield relative, reason
    written: dict[str, str] = {}
    documents = documents or _Documents(changed_paths, room)
    if upfront is not None and fetchable is not None:
        names: dict[str, str] = {}
        for relative, _, _ in pending:
            _claim_name(relative, names)
        fetchable.update(
            (relative, blob) for relative, blob, _ in pending if relative not in changed_paths and not upfront(relative)
        )
        pending = [entry for entry in pending if entry[0] not in fetchable]
    blobs = iter(blob_reader(checkout, [blob for _, blob, _ in pending]))
    try:
        for relative, _, length in pending:
            content = next(blobs, None)
            if content is None:
                raise RuntimeContractError("git cat-file returned fewer blobs than the commit's tree lists")
            if len(content) != length:
                raise RuntimeContractError(f"Source blob size changed: {relative}")
            yield _kept_content(relative, content, written, documents)
    finally:
        close = getattr(blobs, "close", None)
        if close is not None:  # a generator reader ends its git process
            close()


def _tarball_members(
    archive: Path,
    changed_paths: frozenset[str],
    room: PathRoom,
    configured: Exclusion = _never_excluded,
    documents: _Documents | None = None,
    probed: Exclusion = _never_excluded,
) -> Generator[SnapshotMember, None, None]:
    """Each entry of GitHub's tarball below its top folder, as (path, content bytes) when the snapshot keeps it or
    (path, reason) when not, a changed document's content being its extracted text, which `documents` records. A
    changed file over the size limit that `probed` accepts has only the first BINARY_PROBE_BYTES of its entry read
    (see `_size_limit_reason`). The count and size limits are the caller's to apply."""
    written: dict[str, str] = {}
    documents = documents or _Documents(changed_paths, room)
    with tarfile.open(archive, mode="r:gz") as source:
        for member in source:
            parts = PurePosixPath(member.name).parts[1:]
            if member.isdir() or not parts:
                continue
            kind = "file" if member.isfile() else "symbolic-link" if member.issym() else "non-regular"
            name = PurePosixPath(*parts).as_posix()
            relative, reason = _entry_exclusion(name, kind, member.size, changed_paths, room, configured)
            if reason == "file-size-limit" and relative in changed_paths and probed(relative):
                reason = _size_limit_reason(_member_bytes(source, member, relative, BINARY_PROBE_BYTES))
            if reason is not None:
                yield relative, reason
                continue
            content = _member_bytes(source, member, relative)
            if len(content) != member.size:
                raise RuntimeContractError(f"Source snapshot member size changed: {relative}")
            yield _kept_content(relative, content, written, documents)


def _member_bytes(source: tarfile.TarFile, member: tarfile.TarInfo, relative: str, limit: int = -1) -> bytes:
    """A tarball entry's bytes, or with `limit` only its first that many, the rest never read."""
    extracted = source.extractfile(member)
    if extracted is None:
        raise RuntimeContractError(f"Cannot read source snapshot member: {relative}")
    with extracted:
        return extracted.read(limit)


def _populate_snapshot(
    members: Iterable[SnapshotMember],
    destination: Path,
    *,
    repository: str,
    commit: str,
    fetchable: dict[str, str] | None = None,
    extracted: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Write the members and the manifest, which lists `fetchable` when it is given, once the members have filled it:
    the snapshot is then lazy. It lists `extracted` too, once the members have filled it, when it names a document."""
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
    metadata: dict[str, Any] = {
        "schema_version": SOURCE_SNAPSHOT_SCHEMA_VERSION,
        "repository": repository,
        "source_commit": commit,
        "source_hashes": hashes,
        "excluded_paths": excluded,
    }
    if fetchable is not None:
        if len(hashes) + len(fetchable) > MAX_SOURCE_SNAPSHOT_FILES:
            raise RuntimeContractError("Source snapshot exceeds the file-count limit")
        metadata[SNAPSHOT_FETCHABLE] = fetchable
    if extracted:
        metadata[SNAPSHOT_EXTRACTED] = dict(sorted(extracted.items()))
    atomic_write_json(destination / SOURCE_SNAPSHOT_MANIFEST, metadata)
    # The hashes were computed from the bytes just written, in this process; re-reading them proves nothing more.
    return verify_source_snapshot(destination, expected_repository=repository, expected_commit=commit, contents=False)


def snapshot_bytes(root: Path, metadata: dict[str, Any]) -> int:
    """The total size of the files a snapshot its manifest verified holds, its manifest left out."""
    return sum(root.joinpath(*PurePosixPath(relative).parts).stat().st_size for relative in metadata["source_hashes"])


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
    upfront: Callable[[str], bool] | None = None,
    exclude: Sequence[str] = (),
    refused: dict[str, str] | None = None,
    unreviewed_suffixes: Iterable[str] = (),
    prefix_reader: BlobPrefixReader = git_blob_prefix,
) -> dict[str, Any]:
    """Snapshot the exact commit from a local checkout's object store (no worktree or branch change).

    With `upfront`, the snapshot is lazy: it holds the changed files and the paths `upfront` accepts, and its
    manifest lists every other path it would hold, with its blob id, under `fetchable`, for `fetch_source_file`.
    A path one of the `exclude` globs matches is a `configured` exclusion, never written or fetchable. A changed
    document is held as its extracted text, which the manifest lists under `extracted`, and `refused` is given why
    each one whose extraction was refused is `unextractable`. A changed file over the size limit is read only as far
    as `probes` says, through `prefix_reader`, with the repository's `unreviewed_suffixes`.
    """
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
        fetchable: dict[str, str] | None = None if upfront is None else {}
        documents = _Documents(frozenset(changed_paths), room)
        members = _commit_members(
            checkout,
            commit,
            runner,
            blob_reader,
            frozenset(changed_paths),
            room,
            upfront=upfront,
            fetchable=fetchable,
            configured=configured_exclusion(exclude),
            documents=documents,
            probed=probes(unreviewed_suffixes),
            prefix_reader=prefix_reader,
        )
        with closing(members):
            snapshot = _populate_snapshot(
                members,
                destination,
                repository=repository,
                commit=commit,
                fetchable=fetchable,
                extracted=documents.extracted,
            )
        if refused is not None:
            refused.update(documents.refused)
        return snapshot
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


ABBREVIATED_OBJECT_ID = re.compile(r"[0-9a-f]{7,64}")


def blob_matches(blob: str, content: bytes) -> bool:
    """Whether `content` hashes to the full git blob id `blob`."""
    return bool(GIT_OBJECT_ID.fullmatch(blob)) and _git_blob_id(_blob_algorithm(blob), len(content), [content]) == blob


def read_blob(
    checkout: Path, abbreviation: str, runner: Runner = subprocess_runner, *, maximum: int | None = None
) -> bytes | None:
    """The exact bytes of the blob a diff's `index` line names by its abbreviated id, from the checkout's object store,
    once the id resolves to exactly one blob and the bytes hash to it.

    With `maximum`, a blob over that many bytes is None: its size is read first, by the full id it resolved to, and its
    content is never asked for. The size and the content are read by that one id, so they are of one object.
    """
    if not ABBREVIATED_OBJECT_ID.fullmatch(abbreviation):
        raise RuntimeContractError(f"Not an abbreviated blob id: {abbreviation!r}")
    blob = _run_git(checkout, runner, "rev-parse", "--verify", f"{abbreviation}^{{blob}}").lower()
    if not blob.startswith(abbreviation) or not GIT_OBJECT_ID.fullmatch(blob):
        raise RuntimeContractError(f"Blob {abbreviation} did not resolve exactly")
    if maximum is not None:
        size = git_in(checkout, runner, "cat-file", "-s", blob)
        if size.returncode != 0 or not re.fullmatch(r"[0-9]+", size.stdout.strip()):
            raise RuntimeContractError(f"The checkout did not return the size of blob {blob}")
        if int(size.stdout.strip()) > maximum:
            return None
    result = git_in(checkout, runner, "cat-file", "blob", blob)
    content = result.output_bytes()
    if result.returncode != 0 or not blob_matches(blob, content):
        raise RuntimeContractError(f"The checkout did not return blob {blob}")
    return content


def fetch_source_file(
    checkout: Path,
    root: Path,
    relative: str,
    *,
    repository: str,
    commit: str,
    staging: Path,
    blob_reader: BlobReader = git_blob_reader,
) -> Path | str:
    """A head file's path in the snapshot at `root`, written there from the commit when a lazy snapshot lists it as
    fetchable, or the reason the snapshot leaves it out (an exclusion's, or `binary`).

    Only a path the manifest names is looked up, so nothing a caller passes becomes a file name: a path the commit
    does not hold as a file, in any other spelling, raises with nothing written. The blob is read from `checkout`'s
    object store by the id the manifest lists, and its bytes must hash to that id. It is written through a temporary
    file in `staging`, which must be on the snapshot's volume, into a folder checked for reparse points and escape,
    and the snapshot's total size limit holds. A file already there is checked against its id instead. A document
    held as extracted text is returned as that text, wherever the snapshot holds it.
    """
    metadata, tree, held, _ = _verify_snapshot(
        root, expected_repository=repository, expected_commit=commit, contents=False
    )
    form = metadata.get(SNAPSHOT_EXTRACTED, {}).get(relative)
    if form is not None:
        return root.joinpath(*PurePosixPath(held_path(relative, form)).parts)
    target = root.joinpath(*PurePosixPath(relative).parts)
    if relative in metadata["source_hashes"]:
        return target
    if relative in metadata["excluded_paths"]:
        return str(metadata["excluded_paths"][relative])
    blob = metadata.get(SNAPSHOT_FETCHABLE, {}).get(relative)
    if blob is None:
        raise RuntimeContractError(f"The head commit has no file {json.dumps(relative)} the snapshot can hold")
    if relative in tree.files:
        _require_blob(root, relative, blob)
        return target
    blobs = iter(blob_reader(checkout, [blob]))
    try:
        content = next(blobs, None)
    finally:
        close = getattr(blobs, "close", None)
        if close is not None:  # a generator reader ends its git process
            close()
    if content is None or not blob_matches(blob, content):
        raise RuntimeContractError(f"The checkout did not return blob {blob} of {json.dumps(relative)}")
    if is_binary(content):
        return "binary"
    if held + len(content) > MAX_SOURCE_SNAPSHOT_BYTES:
        raise RuntimeContractError("Source snapshot would exceed the size limit")
    target.parent.mkdir(parents=True, exist_ok=True)
    _require_safe_snapshot_path(root, target.parent)
    handle, name = tempfile.mkstemp(prefix="fetch-", dir=staging)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
        try:
            temporary.replace(target)
        except PermissionError:  # another reviewer fetched it first and the file is open
            if not target.is_file() or target.read_bytes() != content:
                raise
    finally:
        temporary.unlink(missing_ok=True)
    return target


def search_source(
    checkout: Path | None, root: Path, pattern: str, *, repository: str, commit: str
) -> tuple[list[tuple[str, str, str]], bool]:
    """Matches of an extended regular expression in the head commit's files, as (path, line number, text), in the
    paths the snapshot at `root` can hold only, and whether more than MAX_SEARCH_MATCHES were found.

    `git grep` searches the commit's tree in `checkout`, so a lazy snapshot is searched whole. Without a checkout, the
    snapshot holds every file, and `git grep --no-index` searches its files as written, each one the manifest hashes.
    It treats every file as text, so no attribute the checkout sets decides what is binary; the snapshot's own test
    does. A file the snapshot
    holds passed it when it was written, and a file it lists to fetch later is read by its blob id from one
    `git cat-file --batch` and tested the first time it matches. No textconv applies. Each line is read only as far
    as a match can show, so a long line of a binary file is never held whole, and its text is read as UTF-8 with
    U+FFFD for any other byte, cut at MAX_MATCH_CHARACTERS. A path the snapshot excludes, such as an agent
    instruction file, is left out, and so is a document the snapshot holds as its extracted text, wherever it holds
    that text: the commit holds the document's own bytes, not the text a reviewer reads.
    """
    metadata = _verify_snapshot(root, expected_repository=repository, expected_commit=commit, contents=False)[0]
    fetchable: dict[str, str] = metadata.get(SNAPSHOT_FETCHABLE, {}) if checkout is not None else {}
    text = dict.fromkeys(metadata["source_hashes"], True)  # path -> whether the snapshot's test finds it text
    text.update(dict.fromkeys(metadata.get(SNAPSHOT_EXTRACTED, {}), False))
    prefix = f"{commit}:".encode("ascii") if checkout is not None else b""
    # The commit, the longest path, two NULs, a line number, and enough bytes of text to run past
    # MAX_MATCH_CHARACTERS however they decode.
    longest = max((len(path.encode("utf-8", "surrogateescape")) for path in [*text, *fetchable]), default=0)
    limit = len(prefix) + longest + 32 + 4 * (MAX_MATCH_CHARACTERS + 1)
    arguments = ["grep", "--null", "--line-number", "-a", "-E", "--no-color"]
    if checkout is None:
        arguments += ["--no-index", "-e", pattern]
    else:
        arguments += ["--full-name", "-e", pattern, commit]
    matches: list[tuple[str, str, str]] = []
    try:
        with ExitStack() as processes:
            stream = processes.enter_context(GitClient().stream([*arguments, "--"], directory=checkout or root))
            stream.stdin.close()
            binary: Callable[[str], bool] | None = None
            while line := stream.readline(limit):
                if not line.endswith(b"\n"):
                    _skip_line(stream)
                name, _, rest = line.rstrip(b"\r\n").partition(b"\0")
                number, cut, found = rest.partition(b"\0")
                path = name.removeprefix(prefix).decode("utf-8", "surrogateescape")
                if not cut or (path not in text and path not in fetchable):
                    continue  # a line cut before its text names a path longer than any the snapshot can hold
                if path not in text and checkout is not None:
                    binary = binary or processes.enter_context(_blob_binary_test(checkout))
                    text[path] = not binary(fetchable[path])
                if not text[path]:
                    continue
                if len(matches) == MAX_SEARCH_MATCHES:
                    return matches, True
                matches.append((path, number.decode("ascii", "replace"), _match_text(found)))
            status = stream.wait()
            if status not in {0, 1}:  # 1 is no match
                raise RuntimeContractError(stream.stderr().strip() or f"git grep failed with exit code {status}")
    except GitError as exc:
        raise RuntimeContractError(str(exc)) from exc
    return matches, False


def _skip_line(stream: GitStream) -> None:
    """Read past the rest of a line a limited `readline` cut, a mebibyte at a time."""
    while (piece := stream.readline(1024 * 1024)) and not piece.endswith(b"\n"):
        pass


@contextmanager
def _blob_binary_test(checkout: Path) -> Iterator[Callable[[str], bool]]:
    """The snapshot's binary test of a blob, read by its id from one `git cat-file --batch` that stays open."""
    with GitClient().stream(["cat-file", "--batch"], directory=checkout) as stream:

        def binary(blob: str) -> bool:
            if not GIT_OBJECT_ID.fullmatch(blob):
                raise RuntimeContractError(f"Not a git object id: {blob!r}")
            stream.stdin.write(f"{blob}\n".encode("ascii"))
            stream.stdin.flush()
            return is_binary(_read_batch_blob(stream, blob))

        yield binary
        stream.stdin.close()


def _match_text(text: bytes) -> str:
    """A matched line as one line of text, cut to MAX_MATCH_CHARACTERS."""
    decoded = text.decode("utf-8", "replace").replace("\r", " ").replace("\0", " ")
    return decoded if len(decoded) <= MAX_MATCH_CHARACTERS else decoded[:MAX_MATCH_CHARACTERS] + "..."


@dataclass(frozen=True)
class SnapshotSize:
    """What a source snapshot of one commit would hold, measured with the snapshot's own rules."""

    files: int
    bytes: int
    excluded: dict[str, int]  # exclusion reason -> file count
    directories: dict[str, int]  # top-level directory ("" for root files) -> bytes kept
    fetchable: int = 0  # the files a lazy snapshot lists to fetch later, which count toward the file-count limit only

    def limit_error(self) -> str | None:
        """Why prepare would refuse this snapshot, naming the largest directories, or None when it fits."""
        files = self.files + self.fetchable
        if files > MAX_SOURCE_SNAPSHOT_FILES:
            return f"it would hold {files} files, over the {MAX_SOURCE_SNAPSHOT_FILES}-file limit"
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
    exclude: Sequence[str] = (),
    upfront: Callable[[str], bool] | None = None,
    unreviewed_suffixes: Iterable[str] = (),
) -> SnapshotSize:
    """Measure the snapshot materialize_source_snapshot would write for a commit at `destination`, without writing it.

    It applies the same exclusions, the room `destination` leaves each path included, and fails on the same
    unrepresentable entries, but not on the count or size limits, so a commit over them reports how far over it is.
    With `upfront`, it measures the lazy snapshot: the files it would write, read and sized, and the files it would
    list as fetchable, counted without being read, as the snapshot counts them against its limits. A changed file
    over the size limit is probed as the snapshot probes it, with the repository's `unreviewed_suffixes`.
    """
    excluded: dict[str, int] = {}
    directories: dict[str, int] = {}
    files = 0
    room = path_room(destination)
    fetchable: dict[str, str] | None = None if upfront is None else {}
    members = _commit_members(
        checkout,
        commit,
        runner,
        blob_reader,
        frozenset(changed_paths),
        room,
        upfront=upfront,
        fetchable=fetchable,
        configured=configured_exclusion(exclude),
        probed=probes(unreviewed_suffixes),
    )
    with closing(members):
        for relative, content in members:
            if isinstance(content, str):
                excluded[content] = excluded.get(content, 0) + 1
                continue
            files += 1
            top = relative.split("/", 1)[0] if "/" in relative else ""
            directories[top] = directories.get(top, 0) + len(content)
    return SnapshotSize(files, sum(directories.values()), excluded, directories, len(fetchable or {}))


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
    listing = READ.json_object(tree, "tree listing")
    entries = READ.array(listing.get("tree"), "tree listing.tree")
    if listing.get("truncated") is not False:
        raise RuntimeContractError("GitHub truncated the tree listing")
    blobs: Counter[tuple[str, str, str]] = Counter()
    for index, value in enumerate(entries):
        path = f"tree listing.tree[{index}]"
        entry = READ.json_object(value, path)
        if READ.one_of(entry.get("type"), f"{path}.type", ("blob", "tree", "commit")) != "blob":
            continue
        name = READ.string(entry.get("path"), f"{path}.path")
        mode = READ.string(entry.get("mode"), f"{path}.mode", empty=True)
        blob = READ.hex_digest(entry.get("sha"), f"{path}.sha", 40, 64)
        blobs[(name, "symbolic-link" if mode == "120000" else "file", blob)] += 1
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
    exclude: Sequence[str] = (),
    refused: dict[str, str] | None = None,
    unreviewed_suffixes: Iterable[str] = (),
) -> dict[str, Any]:
    """Snapshot the exact commit from GitHub's tarball, for repositories without a configured checkout.

    The fetcher is responsible for the tarball being the exact commit; `github_tarball_fetcher` checks it against
    the commit's tree. A path one of the `exclude` globs matches is a `configured` exclusion, never written. Changed
    documents are extracted as `materialize_source_snapshot` extracts them, and `refused` is given the same reasons,
    and a changed file over the size limit is probed as it probes one, with the repository's `unreviewed_suffixes`.
    """
    repository = validate_repository_identity(repository)
    if not GIT_OBJECT_ID.fullmatch(commit):
        raise RuntimeContractError("Source snapshot commit is invalid")
    _prepare_destination(destination)
    try:
        with tempfile.TemporaryDirectory(prefix="code-review-source-") as temporary:
            archive = Path(temporary) / "source.tar.gz"
            fetcher(repository, commit, archive)
            documents = _Documents(frozenset(changed_paths), path_room(destination))
            members = _tarball_members(
                archive,
                documents.changed,
                documents.room,
                configured_exclusion(exclude),
                documents,
                probes(unreviewed_suffixes),
            )
            with closing(members):
                snapshot = _populate_snapshot(
                    members, destination, repository=repository, commit=commit, extracted=documents.extracted
                )
        if refused is not None:
            refused.update(documents.refused)
        return snapshot
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


# A pull request's description is written by its author and runs to GitHub's 65,536 characters; prepare gives
# reviewers this many, room for any description the pull request template asks for, so one cannot fill their context.
PULL_REQUEST_BODY_CHARACTERS = 24_000
# The run's file of the description reviewers are given.
PULL_REQUEST_BODY_FILE = "pull-request-body.md"
# A lone surrogate, which a JSON string can spell but no UTF-8 file can hold.
LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


def given_body(body: str) -> str:
    """The part of a pull request's description reviewers are given: its first PULL_REQUEST_BODY_CHARACTERS, with each
    lone surrogate replaced by U+FFFD."""
    return LONE_SURROGATE.sub("�", body[:PULL_REQUEST_BODY_CHARACTERS])


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
    snapshot: dict[str, Any] | None = None,
    body_file: Path | None = None,
    body_characters: int | None = None,
    unreviewed_suffixes: Iterable[str] = (),
) -> dict[str, Any]:
    """`snapshot` is for a caller that materialized the snapshot itself, moments earlier: the manifest its
    materialization verified, which is only checked to name this request's repository and head. Without it the
    snapshot is verified here, contents included. `body_file` and `body_characters`, given together, name the file
    holding the description reviewers are given and the description's full length. `unreviewed_suffixes` are the
    repository's own suffixes of binary file types the review does not read (see `review_coverage`)."""
    READ.one_of(mode, "request.mode", ("initial", "re-review"))
    repository = validate_repository_identity(repository)
    READ.integer(pull_number, "request.pull_number", minimum=1)
    for value, name in ((base_sha, "base_sha"), (head_sha, "head_sha")):
        READ.hex_digest(value, f"request.pull_request.{name}", 40, 64)
    if not diff_path.is_absolute() or not diff_path.is_file():
        raise RuntimeContractError("diff_path must name an existing absolute file")
    if (body_file is None) != (body_characters is None):
        raise RuntimeContractError("body_file and body_characters are given together")
    if body_file is not None and not (body_file.is_absolute() and body_file.is_file()):
        raise RuntimeContractError("body_file must name an existing absolute file")
    READ.optional(READ.integer, body_characters, "request.pull_request.body_characters", minimum=0)
    if snapshot is None:
        snapshot = verify_source_snapshot(
            source_snapshot_root, expected_repository=repository, expected_commit=head_sha, contents=True
        )
    else:
        require_snapshot_source(snapshot, repository, head_sha)
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
            **(
                {
                    "body_file": str(body_file),
                    "body_characters": body_characters,
                    "body_given": min(body_characters, PULL_REQUEST_BODY_CHARACTERS),
                }
                if body_file is not None and body_characters is not None
                else {}
            ),
        },
        "diff_path": str(diff_path),
        "source_snapshot": {
            "root": str(source_snapshot_root),
            "manifest_path": str(source_snapshot_root / SOURCE_SNAPSHOT_MANIFEST),
            "source_commit": head_sha,
        },
        "prior_findings": prior_findings or [],
        "github_comments": github_comments or [],
        "coverage": review_coverage(diff_path, snapshot, unreviewed_suffixes),
    }


COVERAGE_GAP_REASONS = {"binary", "configured", "file-size-limit", "unsafe-path", "unextractable"}
# The file-name suffixes of the binary file types the review knows it does not read: archives, installers and
# packages, images, fonts, compiled binaries and symbol files, and media. A changed file the snapshot finds binary
# whose name ends in one, ignoring case, is an unreviewed file rather than a coverage gap; a repository's
# unreviewed_suffixes adds its own. The contract document lists them, and a test holds the two to each other.
UNREVIEWED_SUFFIXES = frozenset(
    " ".join(
        (
            ".7z .bz2 .cab .gz .jar .rar .tar .tgz .xz .zip .zst",  # archives
            ".apk .appx .deb .dmg .msi .msix .msp .nupkg .pkg .rpm .snupkg .vsix .whl",  # installers and packages
            ".bmp .gif .icns .ico .jpeg .jpg .png .psd .tif .tiff .webp",  # images
            ".eot .otf .ttf .woff .woff2",  # fonts
            ".a .class .dll .dylib .exe .lib .o .obj .pdb .pyc .pyd .so .wasm",  # compiled binaries and symbol files
            ".aac .avi .flac .m4a .mkv .mov .mp3 .mp4 .ogg .wav .webm",  # media
        )
    ).split()
)


def unreviewed_suffix(path: str, suffixes: Iterable[str]) -> str | None:
    """The longest of `suffixes` the path's file name ends in, ignoring case, with a name before it, or None."""
    name = path.rpartition("/")[2].lower()
    matched = [suffix for suffix in suffixes if len(name) > len(suffix) and name.endswith(suffix)]
    return max(matched, key=len, default=None)


def probes(suffixes: Iterable[str] = ()) -> Exclusion:
    """Whether the snapshot reads the first BINARY_PROBE_BYTES of a changed file it leaves out as over the size limit,
    so a binary of a type the review does not read is found binary and named as unreviewed rather than left a gap.

    Only a name ending in one of UNREVIEWED_SUFFIXES or `suffixes` (the repository's own) is probed, and never a
    document the snapshot extracts, whose oversized bytes no extractor reads. Every other oversized file is never read.
    """
    known = UNREVIEWED_SUFFIXES | set(suffixes)
    return lambda relative: document_format(relative) is None and unreviewed_suffix(relative, known) is not None


def review_coverage(diff_path: Path, snapshot: dict[str, Any], suffixes: Iterable[str] = ()) -> dict[str, Any]:
    """The changed files reviewers see only as diff: `unavailable_sources`, whose source the snapshot could not
    provide, and `unreviewed_files`, each binary file of a type the review does not read, by its path, with the
    suffix it matched.

    A changed file is a coverage gap when the snapshot leaves it out as too large, for an unsafe name, as a document
    whose text could not be extracted, by the repository's snapshot_exclude, which the pull request's author cannot
    hide a change behind, or as binary. A binary one whose name ends in one of UNREVIEWED_SUFFIXES or `suffixes`
    (the repository's own) is unreviewed instead: the suffix decides only once the snapshot has found the file binary,
    so a text file is never unreviewed. Agent-instruction, symbolic-link, and non-regular exclusions are deliberate
    and reviewed from the diff alone.
    """
    from review_specialists import SpecialistError, parse_unified_diff

    try:
        changed = parse_unified_diff(read_diff(diff_path))
    except SpecialistError as exc:
        raise RuntimeContractError(f"Cannot read changed paths from the diff: {exc}") from exc
    reasons: dict[str, set[str]] = {}
    for path, reason in snapshot["excluded_paths"].items():
        reasons.setdefault(_undecodable_form(path), set()).add(reason)
    known = UNREVIEWED_SUFFIXES | set(suffixes)
    unavailable: list[str] = []
    unreviewed: dict[str, str] = {}
    for path in sorted(changed):
        # Every reason any spelling of the name has, so a name merged with a gap's never hides it.
        gaps = reasons.get(_undecodable_form(path), set()) & COVERAGE_GAP_REASONS
        suffix = unreviewed_suffix(path, known) if gaps == {"binary"} else None
        if suffix is not None:
            unreviewed[path] = suffix
        elif gaps:
            unavailable.append(path)
    return {"unavailable_sources": unavailable, "unreviewed_files": unreviewed}


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

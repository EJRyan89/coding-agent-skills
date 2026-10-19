"""Versioned code-review configuration and repository-scope resolution."""

from __future__ import annotations

import argparse
import json
import ntpath
import os
import posixpath
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from console import run_main
from github_logins import is_login
from review_io import PersistenceError, atomic_write_json, read_json
from review_vocabularies import SEVERITIES, Severity
from typed_fields import FieldError, FieldReader, describe

SCHEMA_VERSION = 1
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$")
RUNTIME_HOSTS = {"auto", "claude-code", "codex", "copilot-cli"}
TOP_LEVEL_KEYS = {
    "schema_version",
    "default_repository_set",
    "repository_sets",
    "repositories",
    "archive_root",
    "local_mirror_root",
    "summary_root",
    "dashboard_file",
    "github_login",
    "runtime",
    "verdict_policy",
    "dashboard",
    "operation_repository_sets",
    "reviewer_effort",
    "re_review_scope",
    "model_names",
}
# A model identifier reviewers report (up to the 200 characters a record allows) and the shorter name reports show.
MODEL_IDENTIFIER_MAXIMUM_LENGTH = 200
MODEL_NAME_MAXIMUM_LENGTH = 100
# Reasoning effort for reviewers the Workflow tool starts; null keeps the session's effort.
REVIEWER_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
# When an `auto` re-review reviews the whole pull request instead of only what changed since the last review:
# at least this share of its changed lines changed again, or at least this many lines did.
RE_REVIEW_SCOPE_DEFAULTS = {"full_share": 0.5, "full_lines": 1000}
OPERATIONS = {"review-prs", "update-pr-dashboard", "review-insights"}
# An operation a skill rename retired, with the name it has now: a configuration still naming it is refused with both
# names, as any unknown operation is refused, so the user renames the key rather than losing its set silently.
RENAMED_OPERATIONS = {"update-pr-tracker": "update-pr-dashboard"}
REPOSITORY_KEYS = {"reviewer", "checkout_path", "snapshot_exclude", "unreviewed_suffixes", "batch_size"}
# A repository's snapshot_exclude: glob patterns over snapshot-relative paths, matched as review_runtime's
# configured_exclusion matches them.
MAX_SNAPSHOT_EXCLUDE_PATTERNS = 100
MAX_SNAPSHOT_EXCLUDE_PATTERN_LENGTH = 200
# A repository's unreviewed_suffixes: the file-name suffixes of binary file types the review does not read, beside
# the ones review_runtime's UNREVIEWED_SUFFIXES ships. Each is lowercase, a dot and then one or more dot-separated
# parts, such as `.nupkg` or `.tar.lz`, and a file name ending in it, ignoring case, matches it.
MAX_UNREVIEWED_SUFFIXES = 100
MAX_UNREVIEWED_SUFFIX_LENGTH = 32
UNREVIEWED_SUFFIX = re.compile(r"(?:\.[a-z0-9][a-z0-9_+-]*)+")
REVIEWER_KEYS = {"id", "protocol_version", "trusted_ref", "scope", "manifest_path", "skill", "manifest"}
DASHBOARD_KEYS = {"start_marker", "end_marker", "status_overrides", "author_names"}
# The sections the pull-request tracker computes. update-pr-dashboard renders these names, and they live here, beside
# the validation that refuses a status override naming one, so the names refused and the names rendered cannot drift:
# an override named like a computed section rendered that section twice and dropped the rows placed in it.
DASHBOARD_SECTION_TO_REVIEW = "To Review"
DASHBOARD_SECTION_AWAITING = "Awaiting Response"
DASHBOARD_SECTION_MINE = "My PRs"
DASHBOARD_SECTION_DRAFTS = "Drafts"
DASHBOARD_SECTIONS = (
    DASHBOARD_SECTION_TO_REVIEW,
    DASHBOARD_SECTION_AWAITING,
    DASHBOARD_SECTION_MINE,
    DASHBOARD_SECTION_DRAFTS,
)
# The AI review states the tracker computes for each row, independently of its section: no review, a review of the
# current head, a review of an earlier head that has changed since, and a review of the current head that could not
# read every changed file in full. update-pr-dashboard returns only these, so a state it computes is refused as an
# override without a second list to keep in step.
DASHBOARD_AI_REVIEW_MISSING = "missing"
DASHBOARD_AI_REVIEW_CURRENT = "current"
DASHBOARD_AI_REVIEW_STALE = "stale"
DASHBOARD_AI_REVIEW_INCOMPLETE = "incomplete"
DASHBOARD_AI_REVIEW_STATES = (
    DASHBOARD_AI_REVIEW_MISSING,
    DASHBOARD_AI_REVIEW_CURRENT,
    DASHBOARD_AI_REVIEW_STALE,
    DASHBOARD_AI_REVIEW_INCOMPLETE,
)
# The values a status override may not take, compared trimmed and ignoring case: each computed section, the AI review
# states the tracker shows beside them, and the earlier name of the authored section.
COMPUTED_DASHBOARD_STATES = frozenset(
    {section.casefold() for section in DASHBOARD_SECTIONS} | {"my pull requests"} | set(DASHBOARD_AI_REVIEW_STATES)
)


class ConfigurationError(ValueError):
    """Raised when configuration is invalid or unsafe."""


class ConfigurationFieldError(FieldError, ConfigurationError):
    """A configuration field of the wrong kind."""


READ = FieldReader(ConfigurationFieldError)


def default_config_path() -> Path:
    override = os.environ.get("CODE_REVIEW_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".coding-agent-skills" / "code-review" / "config.json"


def validate_repository_identity(value: object, field: str = "repository") -> str:
    identity = READ.string(value, field)
    if not REPOSITORY_PATTERN.fullmatch(identity):
        raise ConfigurationError(f"Invalid repository identity: {value!r}")
    return identity.lower()


def validate_absolute_path(value: Any, field: str, *, nullable: bool = False) -> str | None:
    """An absolute path of this platform's form, normalized: a drive-letter path on Windows, and elsewhere a POSIX path
    from one leading slash, never a filesystem root."""
    reader_path = f"config.{field}"
    path = READ.optional(READ.string, value, reader_path) if nullable else READ.string(value, reader_path)
    if path is None:
        return None
    if sys.platform != "win32":
        return _posix_absolute_path(path, field)
    normalized = ntpath.normpath(path)
    drive, tail = ntpath.splitdrive(normalized)
    if not drive or not tail.startswith(("\\", "/")) or drive.startswith("\\"):
        raise ConfigurationError(f"{field} must be an absolute drive-letter path")
    if normalized in {f"{drive}\\", f"{drive}/"}:
        raise ConfigurationError(f"{field} must not be a filesystem root")
    return normalized


def _posix_absolute_path(path: str, field: str) -> str:
    # POSIX leaves a path that starts with two slashes to the system, so one slash it is.
    normalized = posixpath.normpath(path)
    if not normalized.startswith("/") or normalized.startswith("//"):
        raise ConfigurationError(f"{field} must be an absolute path")
    if normalized == "/":
        raise ConfigurationError(f"{field} must not be a filesystem root")
    return normalized


def safe_repository_path(value: str) -> bool:
    """A non-empty repository-relative POSIX path that stays inside the repository."""
    return (
        bool(value)
        and "\\" not in value
        and not value.startswith("/")
        and not re.match(r"^[A-Za-z]:", value)
        and ".." not in value.split("/")
    )


def default_manifest_path(config_path: Path, repository: str) -> Path:
    """Where `"manifest": true` looks: reviewers/<owner>/<repo>/manifest.json beside the configuration file."""
    owner, name = validate_repository_identity(repository).split("/", 1)
    return config_path.parent / "reviewers" / owner / name / "manifest.json"


def validate_config(value: Any) -> dict[str, Any]:
    config = READ.json_object(value, "config", optional=TOP_LEVEL_KEYS)
    _validate_schema_version(config.get("schema_version"))
    normalized_sets = _validate_repository_sets(config.get("repository_sets"))
    _validate_default_set(config.get("default_repository_set"), normalized_sets)
    normalized_repositories = _validate_repositories(config.get("repositories", {}))
    _check_configured_repositories(normalized_sets, normalized_repositories)
    runtime = READ.one_of(config.get("runtime", "auto"), "config.runtime", RUNTIME_HOSTS)
    effort = READ.optional(READ.one_of, config.get("reviewer_effort"), "config.reviewer_effort", REVIEWER_EFFORTS)
    scope = _validate_re_review_scope(config.get("re_review_scope", {}))
    verdict_policy = _validate_verdict_policy(config.get("verdict_policy", {}))
    operation_sets = _validate_operation_sets(config.get("operation_repository_sets", {}), normalized_sets)

    normalized = dict(config)
    normalized["operation_repository_sets"] = dict(operation_sets)
    normalized["repository_sets"] = normalized_sets
    normalized["repositories"] = normalized_repositories
    normalized["runtime"] = runtime
    normalized["reviewer_effort"] = effort
    normalized["re_review_scope"] = scope
    normalized["model_names"] = normalize_model_names(config.get("model_names", {}))
    normalized["archive_root"] = validate_absolute_path(config.get("archive_root"), "archive_root")
    normalized["local_mirror_root"] = validate_absolute_path(
        config.get("local_mirror_root"), "local_mirror_root", nullable=True
    )
    normalized["summary_root"] = validate_absolute_path(config.get("summary_root"), "summary_root")
    normalized["dashboard_file"] = validate_absolute_path(config.get("dashboard_file"), "dashboard_file")
    normalized["verdict_policy"] = verdict_policy
    READ.optional(READ.string, config.get("github_login"), "config.github_login")
    normalized["dashboard"] = _validate_dashboard(config.get("dashboard", {}))
    return normalized


def _validate_schema_version(value: Any) -> None:
    version = READ.integer(value, "config.schema_version")
    if version > SCHEMA_VERSION:
        raise ConfigurationError(f"Unsupported future config schema version: {version}")
    if version != SCHEMA_VERSION:
        raise ConfigurationError(f"Unsupported config schema version: {version}")


def _validate_repository_sets(value: Any) -> dict[str, list[str]]:
    """Each named set's members as lowercase identities, in the order given."""
    sets = READ.json_object(value, "config.repository_sets")
    if not sets:
        raise ConfigurationError("repository_sets must not be empty")
    normalized_sets: dict[str, list[str]] = {}
    for key, repositories in sets.items():
        name = READ.string(key, "a key of config.repository_sets")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ConfigurationError(f"Invalid repository-set name: {name!r}")
        members = READ.array(
            repositories, f"config.repository_sets.{name}", validate_repository_identity, nonempty=True
        )
        if len(set(members)) != len(members):
            raise ConfigurationError(f"Repository set '{name}' contains duplicates")
        normalized_sets[name] = members
    return normalized_sets


def _validate_default_set(default_set: Any, normalized_sets: dict[str, list[str]]) -> None:
    if READ.string(default_set, "config.default_repository_set") not in normalized_sets:
        raise ConfigurationError("default_repository_set must name an existing set")


def _validate_repositories(value: Any) -> dict[str, Any]:
    """Each repository's normalized entry keyed by its lowercase identity, checked one entry at a time."""
    repository_config = READ.json_object(value, "config.repositories")
    normalized_repositories: dict[str, Any] = {}
    for identity, entry_value in repository_config.items():
        normalized_identity = validate_repository_identity(identity, "a key of config.repositories")
        if normalized_identity in normalized_repositories:
            raise ConfigurationError(f"Duplicate repository configuration after normalization: {normalized_identity}")
        normalized_repositories[normalized_identity] = _validate_repository(identity, entry_value)
    return normalized_repositories


def _validate_repository(identity: str, entry_value: Any) -> dict[str, Any]:
    field = f"repositories.{identity}"
    entry = READ.json_object(entry_value, f"config.{field}", optional=REPOSITORY_KEYS)
    reviewer = _validate_reviewer(identity, entry.get("reviewer"))
    checkout = validate_absolute_path(entry.get("checkout_path"), f"{field}.checkout_path", nullable=True)
    if reviewer["scope"] == "repository" and checkout is None:
        raise ConfigurationError(f"{field}.checkout_path is required for a repository reviewer")
    exclude = validate_glob_patterns(
        entry.get("snapshot_exclude", []),
        f"config.{field}.snapshot_exclude",
        maximum_patterns=MAX_SNAPSHOT_EXCLUDE_PATTERNS,
        maximum_length=MAX_SNAPSHOT_EXCLUDE_PATTERN_LENGTH,
    )
    unreviewed = validate_unreviewed_suffixes(
        entry.get("unreviewed_suffixes", []), f"config.{field}.unreviewed_suffixes"
    )
    # The most pull requests one batch run reviews; null or absent, a run takes every eligible one.
    batch_size = READ.optional(READ.integer, entry.get("batch_size"), f"config.{field}.batch_size", minimum=1)
    return {
        "reviewer": reviewer,
        "checkout_path": checkout,
        "snapshot_exclude": exclude,
        "unreviewed_suffixes": unreviewed,
        "batch_size": batch_size,
    }


def is_unreviewed_suffix(value: str) -> bool:
    """Whether a string is a file-name suffix an unreviewed file type may be named by (see UNREVIEWED_SUFFIX)."""
    return len(value) <= MAX_UNREVIEWED_SUFFIX_LENGTH and UNREVIEWED_SUFFIX.fullmatch(value) is not None


def validate_unreviewed_suffixes(value: Any, field: str, *, reader: FieldReader = READ) -> list[str]:
    """Distinct file-name suffixes of binary file types the review does not read, each lowercase, as
    `is_unreviewed_suffix` reads one; `reader` is the reader of the format that holds them."""
    suffixes = reader.array(
        value,
        field,
        lambda suffix, path: reader.string(suffix, path, maximum=MAX_UNREVIEWED_SUFFIX_LENGTH),
        maximum=MAX_UNREVIEWED_SUFFIXES,
    )
    for index, suffix in enumerate(suffixes):
        if not is_unreviewed_suffix(suffix):
            raise reader.fail(
                f"{field}[{index}]",
                "a lowercase file-name suffix: a dot, then letters, digits, '_', '+', or '-', in parts each starting "
                "with a letter or digit and joined by dots",
                describe(suffix),
            )
    for index, suffix in enumerate(suffixes):
        if suffix in suffixes[:index]:
            raise reader.fail(field, "distinct file-name suffixes", f"{json.dumps(suffix)} twice")
    return suffixes


def validate_glob_patterns(
    value: Any,
    field: str,
    *,
    maximum_patterns: int,
    maximum_length: int,
    reader: FieldReader = READ,
) -> list[str]:
    """Distinct glob patterns, each a repository-relative POSIX path pattern that cannot name the snapshot's root or
    leave it: no backslash, leading slash, control character, or empty, `.`, or `..` segment. A repository's
    `snapshot_exclude` and a condition's `reads` are both such patterns; `reader` is the reader of the format that
    holds them, so a fault raises that format's error."""
    patterns = reader.array(
        value,
        field,
        lambda pattern, path: reader.string(pattern, path, maximum=maximum_length),
        maximum=maximum_patterns,
    )
    seen: set[str] = set()
    for index, pattern in enumerate(patterns):
        if (
            not safe_repository_path(pattern)
            or re.search(r"[\x00-\x1f\x7f]", pattern)
            or any(segment in {"", ".", ".."} for segment in pattern.split("/"))
        ):
            raise reader.fail(
                f"{field}[{index}]",
                "a repository-relative glob pattern without a backslash, a control character, or an empty, '.', or "
                "'..' segment",
                describe(pattern),
            )
        if pattern.casefold() in seen:
            raise reader.fail(field, "distinct glob patterns, ignoring case", f"{json.dumps(pattern)} twice")
        seen.add(pattern.casefold())
    return patterns


def _validate_reviewer(identity: str, reviewer_value: Any) -> dict[str, Any]:
    field = f"repositories.{identity}.reviewer"
    path = f"config.{field}"
    reviewer = READ.json_object(reviewer_value, path, optional=REVIEWER_KEYS)
    reviewer_id = READ.string(reviewer.get("id"), f"{path}.id")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", reviewer_id):
        raise ConfigurationError(f"{field}.id is invalid")
    if READ.integer(reviewer.get("protocol_version"), f"{path}.protocol_version") != 1:
        raise ConfigurationError(f"{field} protocol is unsupported")
    trusted_ref = READ.optional(READ.string, reviewer.get("trusted_ref"), f"{path}.trusted_ref")
    if trusted_ref is not None and trusted_ref.startswith("refs/pull/"):
        raise ConfigurationError(f"{field}.trusted_ref cannot be a pull ref")
    scope = READ.one_of(reviewer.get("scope", "repository"), f"{path}.scope", ("generic", "repository"))
    manifest_path = READ.optional(READ.string, reviewer.get("manifest_path"), f"{path}.manifest_path")
    skill = READ.optional(READ.string, reviewer.get("skill"), f"{path}.skill")
    manifest = reviewer.get("manifest")
    if manifest is not True:
        try:
            manifest = READ.optional(READ.string, manifest, f"{path}.manifest")
        except FieldError as exc:
            raise READ.fail(exc.path, "true, a non-empty string, or null", exc.found) from None
    if scope == "generic":
        if reviewer_id != "generic" or any(
            value is not None for value in (manifest_path, trusted_ref, skill, manifest)
        ):
            raise ConfigurationError(
                f"{field} generic scope must use id 'generic' without manifest_path, skill, manifest, or trusted_ref"
            )
    elif manifest_path is not None:
        # A manifest committed to the repository describes its reviewer on its own.
        if skill is not None or manifest is not None:
            raise ConfigurationError(f"{field} sets manifest_path, so it cannot also set skill or manifest")
        if not safe_repository_path(manifest_path):
            raise ConfigurationError(f"{field}.manifest_path is unsafe")
    else:
        # The repository's own review skill, optionally run through a manifest kept outside the repository.
        if skill is None:
            raise ConfigurationError(f"{field} needs skill (the repository's review skill) or manifest_path")
        if not safe_repository_path(skill):
            raise ConfigurationError(f"{field}.skill is unsafe")
        if manifest is not None and manifest is not True:
            manifest = validate_absolute_path(manifest, f"{field}.manifest")
    return {
        "id": reviewer_id,
        "protocol_version": 1,
        "trusted_ref": trusted_ref,
        "scope": scope,
        "manifest_path": manifest_path,
        "skill": skill,
        "manifest": manifest,
    }


def _check_configured_repositories(
    normalized_sets: dict[str, list[str]], normalized_repositories: dict[str, Any]
) -> None:
    """Every repository a set names has its own entry."""
    configured = {repo for repos in normalized_sets.values() for repo in repos}
    missing = sorted(configured - set(normalized_repositories))
    if missing:
        raise ConfigurationError("Missing per-repository configuration for: " + ", ".join(missing))


def _validate_re_review_scope(value: Any) -> dict[str, Any]:
    """The given thresholds over their defaults."""
    given = READ.json_object(value, "config.re_review_scope", optional=set(RE_REVIEW_SCOPE_DEFAULTS))
    scope = {**RE_REVIEW_SCOPE_DEFAULTS, **given}
    share = READ.number(scope["full_share"], "config.re_review_scope.full_share")
    if not 0 < share <= 1:
        raise ConfigurationError("re_review_scope.full_share must be a number above 0 and at most 1")
    READ.integer(scope["full_lines"], "config.re_review_scope.full_lines", minimum=1)
    return scope


@dataclass(frozen=True)
class VerdictPolicy:
    """When a review's open findings request changes: one open finding of a severity in `request_changes_for`, or
    `should_fix_threshold` open SHOULD_FIX findings. `verdict_policy` reads one, its defaults filled."""

    request_changes_for: frozenset[Severity]
    should_fix_threshold: int


def verdict_policy(value: Any) -> VerdictPolicy:
    """The policy a configuration's `verdict_policy` gives, read and checked as `validate_config` reads it."""
    policy = _validate_verdict_policy(value)
    return VerdictPolicy(frozenset(policy["request_changes_for"]), policy["should_fix_threshold"])


def _validate_verdict_policy(value: Any) -> dict[str, Any]:
    """Both policy fields, defaults filled; request_changes_for is the given list itself."""
    policy = READ.json_object(value, "config.verdict_policy", optional={"request_changes_for", "should_fix_threshold"})
    request_changes_for = policy.get("request_changes_for", ["MUST_FIX"])
    READ.array(
        request_changes_for,
        "config.verdict_policy.request_changes_for",
        lambda item, path: READ.one_of(item, path, SEVERITIES),
        nonempty=True,
    )
    threshold = READ.integer(
        policy.get("should_fix_threshold", 3), "config.verdict_policy.should_fix_threshold", minimum=1
    )
    return {
        "request_changes_for": request_changes_for,
        "should_fix_threshold": threshold,
    }


def _validate_operation_sets(value: Any, normalized_sets: dict[str, list[str]]) -> dict[str, Any]:
    for retired, current in RENAMED_OPERATIONS.items():
        if isinstance(value, dict) and retired in value:
            raise ConfigurationError(
                f"operation_repository_sets.{retired} names an operation renamed {current}; rename the key to {current}"
            )
    operation_sets = READ.json_object(value, "config.operation_repository_sets", optional=OPERATIONS)
    for operation, set_name in operation_sets.items():
        if READ.string(set_name, f"config.operation_repository_sets.{operation}") not in normalized_sets:
            raise ConfigurationError(f"operation_repository_sets.{operation} must name an existing set")
    return operation_sets


def _validate_dashboard(value: Any) -> dict[str, Any]:
    """The markers with their defaults, the status overrides as given, and the normalized author names."""
    dashboard = READ.json_object(value, "config.dashboard", optional=DASHBOARD_KEYS)
    start_marker = READ.string(
        dashboard.get("start_marker", "<!-- code-review-pr-tracker:start -->"),
        "config.dashboard.start_marker",
        single_line=True,
        trimmed=True,
    )
    end_marker = READ.string(
        dashboard.get("end_marker", "<!-- code-review-pr-tracker:end -->"),
        "config.dashboard.end_marker",
        single_line=True,
        trimmed=True,
    )
    if start_marker == end_marker:
        raise ConfigurationError("dashboard markers must be distinct")
    overrides = dashboard.get("status_overrides", {})
    _validate_status_overrides(overrides)
    return {
        "start_marker": start_marker,
        "end_marker": end_marker,
        "status_overrides": overrides,
        "author_names": normalize_author_names(dashboard.get("author_names", {})),
    }


def _validate_status_overrides(value: Any) -> None:
    overrides = READ.json_object(value, "config.dashboard.status_overrides")
    for raw_key, raw_status in overrides.items():
        key = READ.string(raw_key, "a key of config.dashboard.status_overrides")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*", key):
            raise ConfigurationError(f"Invalid dashboard status override key: {key!r}")
        status = READ.string(raw_status, f"config.dashboard.status_overrides.{key}")
        if status.strip().casefold() in COMPUTED_DASHBOARD_STATES:
            raise ConfigurationError(f"Dashboard status override {key} duplicates a computed tracker state")


def normalize_model_names(value: Any) -> dict[str, str]:
    """Display names keyed by the model identifier a reviewer reports, such as an inference-profile ARN."""
    names = READ.json_object(value, "config.model_names")
    normalized: dict[str, str] = {}
    for key, name in names.items():
        identifier = READ.string(
            key, "a key of config.model_names", single_line=True, trimmed=True, maximum=MODEL_IDENTIFIER_MAXIMUM_LENGTH
        )
        label = READ.string(name, f"config.model_names.{identifier}", single_line=True).strip()
        if len(label) > MODEL_NAME_MAXIMUM_LENGTH:
            raise ConfigurationError(
                f"model_names.{identifier} must be a name of at most {MODEL_NAME_MAXIMUM_LENGTH} characters"
            )
        normalized[identifier] = label
    return normalized


def normalize_author_names(value: Any) -> dict[str, str]:
    """Display names keyed by GitHub login; logins are case-insensitive, so they must be unique ignoring case."""
    names = READ.json_object(value, "config.dashboard.author_names")
    normalized: dict[str, str] = {}
    seen: set[str] = set()
    for key, name in names.items():
        login = READ.string(key, "a key of config.dashboard.author_names")
        if not is_login(login):
            raise ConfigurationError(f"Invalid dashboard author name login: {login!r}")
        if login.casefold() in seen:
            raise ConfigurationError(f"Duplicate dashboard author name login ignoring case: {login}")
        seen.add(login.casefold())
        normalized[login] = READ.string(name, f"config.dashboard.author_names.{login}", single_line=True).strip()
    return normalized


def load_config(path: Path | None = None) -> dict[str, Any]:
    selected_path = path or default_config_path()
    try:
        return validate_config(read_json(selected_path))
    except PersistenceError as exc:
        raise ConfigurationError(str(exc)) from exc


def write_config(value: Any, path: Path | None = None) -> dict[str, Any]:
    selected_path = path or default_config_path()
    normalized = validate_config(value)
    atomic_write_json(selected_path, normalized, validator=validate_config)
    return normalized


def selected_repository_set(
    config: dict[str, Any], *, repository_set: str | None = None, operation: str | None = None
) -> str:
    """The named set, else the operation's configured set, else the default set."""
    return (
        repository_set
        or config.get("operation_repository_sets", {}).get(operation or "")
        or config["default_repository_set"]
    )


def resolve_repositories(
    config: dict[str, Any],
    *,
    explicit: Iterable[str] | None = None,
    repository_set: str | None = None,
    operation: str | None = None,
) -> list[str]:
    """Explicit repositories, else the named set, else the operation's configured set, else the default set."""
    if explicit is not None and repository_set is not None:
        raise ConfigurationError("Specify repositories or a repository set, not both")
    if explicit is not None:
        values = list(explicit)
        if not values:
            raise ConfigurationError("Explicit repository selection must not be empty")
        normalized = [validate_repository_identity(item) for item in values]
    else:
        selected_set = selected_repository_set(config, repository_set=repository_set, operation=operation)
        if selected_set not in config["repository_sets"]:
            raise ConfigurationError(f"Unknown repository set: {selected_set}")
        normalized = list(config["repository_sets"][selected_set])
    if len(set(normalized)) != len(normalized):
        raise ConfigurationError("Repository selection contains duplicates")
    return normalized


def _main(arguments: list[str] | None = None) -> int:
    """Prints `VALID <path>` or `WROTE <path>` and exits 0, or a last line `FAILED <reason>` and exits 1."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("path", type=Path, nargs="?")
    write_parser = subparsers.add_parser("write")
    write_parser.add_argument("input", type=Path)
    write_parser.add_argument("--output", type=Path)
    args = parser.parse_args(arguments)
    try:
        if args.command == "validate":
            path = args.path or default_config_path()
            load_config(path)
            print(f"VALID {path}")
        else:
            path = args.output or default_config_path()
            value = json.loads(args.input.read_text(encoding="utf-8-sig"))
            write_config(value, path)
            print(f"WROTE {path}")
        return 0
    except (ConfigurationError, PersistenceError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"FAILED {exc}")
        return 1


if __name__ == "__main__":
    run_main(_main)

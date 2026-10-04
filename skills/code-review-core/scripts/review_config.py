"""Versioned code-review configuration and repository-scope resolution."""

from __future__ import annotations

import argparse
import json
import ntpath
import os
import re
from pathlib import Path
from typing import Any, Iterable

from review_io import PersistenceError, atomic_write_json, read_json


SCHEMA_VERSION = 1
REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
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
}
# Reasoning effort for reviewers the Workflow tool starts; null keeps the session's effort.
REVIEWER_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
# When an `auto` re-review reviews the whole pull request instead of only what changed since the last review:
# at least this share of its changed lines changed again, or at least this many lines did.
RE_REVIEW_SCOPE_DEFAULTS = {"full_share": 0.5, "full_lines": 1000}
OPERATIONS = {"review-prs", "update-pr-tracker", "review-insights"}
REPOSITORY_KEYS = {"reviewer", "checkout_path"}
REVIEWER_KEYS = {"id", "protocol_version", "trusted_ref", "scope", "manifest_path", "skill", "manifest"}
DASHBOARD_KEYS = {"start_marker", "end_marker", "status_overrides", "author_names"}
GITHUB_LOGIN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
COMPUTED_DASHBOARD_STATES = {
    "to review",
    "awaiting response",
    "my pull requests",
    "drafts",
    "missing",
    "current",
    "stale",
}


class ConfigurationError(ValueError):
    """Raised when configuration is invalid or unsafe."""


def default_config_path() -> Path:
    override = os.environ.get("CODE_REVIEW_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".coding-agent-skills" / "code-review" / "config.json"


def _expect_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{field} must be an object")
    return value


def _reject_unknown(value: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigurationError(f"{field} contains unknown field(s): {', '.join(unknown)}")


def validate_repository_identity(value: str) -> str:
    if not isinstance(value, str) or not REPOSITORY_PATTERN.fullmatch(value):
        raise ConfigurationError(f"Invalid repository identity: {value!r}")
    return value.lower()


def validate_windows_absolute_path(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{field} must be a non-empty absolute Windows path")
    normalized = ntpath.normpath(value)
    drive, tail = ntpath.splitdrive(normalized)
    if not drive or not tail.startswith(("\\", "/")) or drive.startswith("\\"):
        raise ConfigurationError(f"{field} must be an absolute drive-letter path")
    if normalized in {f"{drive}\\", f"{drive}/"}:
        raise ConfigurationError(f"{field} must not be a filesystem root")
    return normalized


def _safe_repository_path(value: Any) -> bool:
    """A non-empty repository-relative POSIX path that stays inside the repository."""
    return (
        isinstance(value, str)
        and bool(value)
        and "\\" not in value
        and not value.startswith("/")
        and ".." not in value.split("/")
    )


def default_manifest_path(config_path: Path, repository: str) -> Path:
    """Where `"manifest": true` looks: reviewers/<owner>/<repo>/manifest.json beside the configuration file."""
    owner, name = validate_repository_identity(repository).split("/", 1)
    return config_path.parent / "reviewers" / owner / name / "manifest.json"


def validate_config(value: Any) -> dict[str, Any]:
    config = _expect_object(value, "config")
    _reject_unknown(config, TOP_LEVEL_KEYS, "config")
    version = config.get("schema_version")
    if version != SCHEMA_VERSION:
        if isinstance(version, int) and version > SCHEMA_VERSION:
            raise ConfigurationError(f"Unsupported future config schema version: {version}")
        raise ConfigurationError(f"Unsupported config schema version: {version!r}")

    sets = _expect_object(config.get("repository_sets"), "repository_sets")
    if not sets:
        raise ConfigurationError("repository_sets must not be empty")
    normalized_sets: dict[str, list[str]] = {}
    for name, repositories in sets.items():
        if not isinstance(name, str) or not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ConfigurationError(f"Invalid repository-set name: {name!r}")
        if not isinstance(repositories, list) or not repositories:
            raise ConfigurationError(f"Repository set '{name}' must not be empty")
        normalized = [validate_repository_identity(item) for item in repositories]
        if len(set(normalized)) != len(normalized):
            raise ConfigurationError(f"Repository set '{name}' contains duplicates")
        normalized_sets[name] = normalized

    default_set = config.get("default_repository_set")
    if not isinstance(default_set, str) or default_set not in normalized_sets:
        raise ConfigurationError("default_repository_set must name an existing set")

    repository_config = _expect_object(config.get("repositories", {}), "repositories")
    normalized_repositories: dict[str, Any] = {}
    for identity, entry_value in repository_config.items():
        normalized_identity = validate_repository_identity(identity)
        if normalized_identity in normalized_repositories:
            raise ConfigurationError(
                f"Duplicate repository configuration after normalization: {normalized_identity}"
            )
        entry = _expect_object(entry_value, f"repositories.{identity}")
        _reject_unknown(entry, REPOSITORY_KEYS, f"repositories.{identity}")
        reviewer = _expect_object(entry.get("reviewer"), f"repositories.{identity}.reviewer")
        _reject_unknown(reviewer, REVIEWER_KEYS, f"repositories.{identity}.reviewer")
        reviewer_id = reviewer.get("id")
        if not isinstance(reviewer_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", reviewer_id):
            raise ConfigurationError(f"repositories.{identity}.reviewer.id is invalid")
        if reviewer.get("protocol_version") != 1:
            raise ConfigurationError(f"repositories.{identity}.reviewer protocol is unsupported")
        trusted_ref = reviewer.get("trusted_ref")
        if trusted_ref is not None and (not isinstance(trusted_ref, str) or not trusted_ref.strip()):
            raise ConfigurationError(f"repositories.{identity}.reviewer.trusted_ref is invalid")
        if isinstance(trusted_ref, str) and trusted_ref.startswith("refs/pull/"):
            raise ConfigurationError(f"repositories.{identity}.reviewer.trusted_ref cannot be a pull ref")
        scope = reviewer.get("scope", "repository")
        if scope not in {"generic", "repository"}:
            raise ConfigurationError(f"repositories.{identity}.reviewer.scope is invalid")
        manifest_path = reviewer.get("manifest_path")
        skill = reviewer.get("skill")
        manifest = reviewer.get("manifest")
        field = f"repositories.{identity}.reviewer"
        if scope == "generic":
            if reviewer_id != "generic" or any(
                value is not None for value in (manifest_path, trusted_ref, skill, manifest)
            ):
                raise ConfigurationError(
                    f"{field} generic scope must use id 'generic' without manifest_path, skill, manifest, "
                    "or trusted_ref"
                )
        elif manifest_path is not None:
            # A manifest committed to the repository describes its reviewer on its own.
            if skill is not None or manifest is not None:
                raise ConfigurationError(f"{field} sets manifest_path, so it cannot also set skill or manifest")
            if not _safe_repository_path(manifest_path):
                raise ConfigurationError(f"{field}.manifest_path is unsafe")
        else:
            # The repository's own review skill, optionally run through a manifest kept outside the repository.
            if skill is None:
                raise ConfigurationError(f"{field} needs skill (the repository's review skill) or manifest_path")
            if not _safe_repository_path(skill):
                raise ConfigurationError(f"{field}.skill is unsafe")
            if manifest is not None and manifest is not True:
                manifest = validate_windows_absolute_path(manifest, f"{field}.manifest")
        checkout = validate_windows_absolute_path(
            entry.get("checkout_path"), f"repositories.{identity}.checkout_path", nullable=True
        )
        if scope == "repository" and checkout is None:
            raise ConfigurationError(
                f"repositories.{identity}.checkout_path is required for a repository reviewer"
            )
        normalized_repositories[normalized_identity] = {
            "reviewer": {
                "id": reviewer_id,
                "protocol_version": 1,
                "trusted_ref": trusted_ref,
                "scope": scope,
                "manifest_path": manifest_path,
                "skill": skill,
                "manifest": manifest,
            },
            "checkout_path": checkout,
        }

    configured = {repo for repos in normalized_sets.values() for repo in repos}
    missing = sorted(configured - set(normalized_repositories))
    if missing:
        raise ConfigurationError(
            "Missing per-repository configuration for: " + ", ".join(missing)
        )

    runtime = config.get("runtime", "auto")
    if runtime not in RUNTIME_HOSTS:
        raise ConfigurationError(f"Unknown runtime host: {runtime!r}")
    effort = config.get("reviewer_effort")
    if effort is not None and effort not in REVIEWER_EFFORTS:
        raise ConfigurationError(
            f"reviewer_effort must be null or one of {', '.join(sorted(REVIEWER_EFFORTS))}: {effort!r}")

    scope = _expect_object(config.get("re_review_scope", {}), "re_review_scope")
    _reject_unknown(scope, set(RE_REVIEW_SCOPE_DEFAULTS), "re_review_scope")
    scope = {**RE_REVIEW_SCOPE_DEFAULTS, **scope}
    share = scope["full_share"]
    if not isinstance(share, (int, float)) or isinstance(share, bool) or not 0 < share <= 1:
        raise ConfigurationError("re_review_scope.full_share must be a number above 0 and at most 1")
    if not isinstance(scope["full_lines"], int) or isinstance(scope["full_lines"], bool) or scope["full_lines"] < 1:
        raise ConfigurationError("re_review_scope.full_lines must be a positive integer")

    policy = _expect_object(config.get("verdict_policy", {}), "verdict_policy")
    _reject_unknown(policy, {"request_changes_for", "should_fix_threshold"}, "verdict_policy")
    request_changes_for = policy.get("request_changes_for", ["MUST_FIX"])
    if not isinstance(request_changes_for, list) or not request_changes_for:
        raise ConfigurationError("verdict_policy.request_changes_for must be non-empty")
    allowed_severities = {"MUST_FIX", "SHOULD_FIX", "SUGGESTION"}
    if any(item not in allowed_severities for item in request_changes_for):
        raise ConfigurationError("verdict_policy.request_changes_for contains an invalid severity")
    threshold = policy.get("should_fix_threshold", 3)
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
        raise ConfigurationError("verdict_policy.should_fix_threshold must be a positive integer")

    operation_sets = config.get("operation_repository_sets", {})
    if not isinstance(operation_sets, dict):
        raise ConfigurationError("operation_repository_sets must be an object")
    for operation, set_name in operation_sets.items():
        if operation not in OPERATIONS:
            raise ConfigurationError(f"operation_repository_sets has an unknown operation: {operation!r}")
        if set_name not in normalized_sets:
            raise ConfigurationError(f"operation_repository_sets.{operation} must name an existing set")

    normalized = dict(config)
    normalized["operation_repository_sets"] = dict(operation_sets)
    normalized["repository_sets"] = normalized_sets
    normalized["repositories"] = normalized_repositories
    normalized["runtime"] = runtime
    normalized["reviewer_effort"] = effort
    normalized["re_review_scope"] = scope
    normalized["archive_root"] = validate_windows_absolute_path(
        config.get("archive_root"), "archive_root"
    )
    normalized["local_mirror_root"] = validate_windows_absolute_path(
        config.get("local_mirror_root"), "local_mirror_root", nullable=True
    )
    normalized["summary_root"] = validate_windows_absolute_path(
        config.get("summary_root"), "summary_root"
    )
    normalized["dashboard_file"] = validate_windows_absolute_path(
        config.get("dashboard_file"), "dashboard_file"
    )
    normalized["verdict_policy"] = {
        "request_changes_for": request_changes_for,
        "should_fix_threshold": threshold,
    }
    login = config.get("github_login")
    if login is not None and (not isinstance(login, str) or not login.strip()):
        raise ConfigurationError("github_login must be null or a non-empty string")
    dashboard = config.get("dashboard", {})
    if not isinstance(dashboard, dict):
        raise ConfigurationError("dashboard must be an object")
    _reject_unknown(dashboard, DASHBOARD_KEYS, "dashboard")
    start_marker = dashboard.get(
        "start_marker", "<!-- code-review-pr-tracker:start -->"
    )
    end_marker = dashboard.get("end_marker", "<!-- code-review-pr-tracker:end -->")
    if (
        not isinstance(start_marker, str)
        or not start_marker.strip()
        or start_marker != start_marker.strip()
        or "\n" in start_marker
        or "\r" in start_marker
        or not isinstance(end_marker, str)
        or not end_marker.strip()
        or end_marker != end_marker.strip()
        or "\n" in end_marker
        or "\r" in end_marker
        or start_marker == end_marker
    ):
        raise ConfigurationError(
            "dashboard markers must be distinct, trimmed, non-empty single-line strings"
        )
    overrides = dashboard.get("status_overrides", {})
    if not isinstance(overrides, dict):
        raise ConfigurationError("dashboard.status_overrides must be an object")
    for key, value in overrides.items():
        if not isinstance(key, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*", key
        ):
            raise ConfigurationError(f"Invalid dashboard status override key: {key!r}")
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationError(f"Dashboard status override {key} must be non-empty")
        if value.strip().casefold() in COMPUTED_DASHBOARD_STATES:
            raise ConfigurationError(
                f"Dashboard status override {key} duplicates a computed tracker state"
            )
    normalized["dashboard"] = {
        "start_marker": start_marker,
        "end_marker": end_marker,
        "status_overrides": overrides,
        "author_names": normalize_author_names(dashboard.get("author_names", {})),
    }
    return normalized


def normalize_author_names(value: Any) -> dict[str, str]:
    """Display names keyed by GitHub login; logins are case-insensitive, so they must be unique ignoring case."""
    if not isinstance(value, dict):
        raise ConfigurationError("dashboard.author_names must be an object")
    normalized: dict[str, str] = {}
    seen: set[str] = set()
    for login, name in value.items():
        if not isinstance(login, str) or not GITHUB_LOGIN_PATTERN.fullmatch(login):
            raise ConfigurationError(f"Invalid dashboard author name login: {login!r}")
        if login.casefold() in seen:
            raise ConfigurationError(f"Duplicate dashboard author name login ignoring case: {login}")
        seen.add(login.casefold())
        if not isinstance(name, str) or not name.strip() or "\n" in name or "\r" in name:
            raise ConfigurationError(f"Dashboard author name for {login} must be a non-empty single-line string")
        normalized[login] = name.strip()
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


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("path", type=Path, nargs="?")
    write_parser = subparsers.add_parser("write")
    write_parser.add_argument("input", type=Path)
    write_parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            load_config(args.path)
        else:
            value = json.loads(args.input.read_text(encoding="utf-8-sig"))
            write_config(value, args.output)
        return 0
    except (ConfigurationError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())

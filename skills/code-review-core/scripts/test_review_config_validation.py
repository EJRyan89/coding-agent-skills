"""validate_config, pinned: every configuration shape it accepts with the exact result it returns, every fault it
refuses with its exact error, and the order in which it detects faults. The configurations are literal, so a change to
which repositories, reviewers, paths, or policies the review pipeline accepts shows up here."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_config import ConfigurationError, validate_config

Mutation = Callable[[Any], Any]
Case = tuple[str, Mutation, type[Exception], str]

REPO = ("repositories", "owner/repo")
REVIEWER = (*REPO, "reviewer")
FIELD = "repositories.owner/repo"
GENERIC_MESSAGE = (
    f"{FIELD}.reviewer generic scope must use id 'generic' without manifest_path, skill, manifest, or trusted_ref"
)
MARKERS_MESSAGE = "dashboard markers must be distinct, trimmed, non-empty single-line strings"
EFFORT_MESSAGE = "reviewer_effort must be null or one of high, low, max, medium, xhigh: "
SHARE_MESSAGE = "re_review_scope.full_share must be a number above 0 and at most 1"
LINES_MESSAGE = "re_review_scope.full_lines must be a positive integer"
THRESHOLD_MESSAGE = "verdict_policy.should_fix_threshold must be a positive integer"
SEVERITY_MESSAGE = "verdict_policy.request_changes_for contains an invalid severity"
# The tracker's own states, which a status override may not repeat; an independent copy of the module's list.
COMPUTED_STATES = ["to review", "awaiting response", "my pull requests", "drafts", "missing", "current", "stale"]


def _generic_entry() -> dict[str, Any]:
    return {"reviewer": {"id": "generic", "protocol_version": 1, "scope": "generic"}}


def _config() -> dict[str, Any]:
    """The smallest valid configuration: one set of one repository with the generic reviewer, and the three required
    paths."""
    return {
        "schema_version": 1,
        "repository_sets": {"main": ["owner/repo"]},
        "default_repository_set": "main",
        "repositories": {"owner/repo": _generic_entry()},
        "archive_root": "C:\\reviews\\archive",
        "summary_root": "C:\\reviews\\summaries",
        "dashboard_file": "C:\\reviews\\dashboard.md",
    }


def _expected() -> dict[str, Any]:
    """What validate_config returns for _config(), in the order it returns the keys."""
    return {
        "schema_version": 1,
        "repository_sets": {"main": ["owner/repo"]},
        "default_repository_set": "main",
        "repositories": {
            "owner/repo": {
                "reviewer": {
                    "id": "generic",
                    "protocol_version": 1,
                    "trusted_ref": None,
                    "scope": "generic",
                    "manifest_path": None,
                    "skill": None,
                    "manifest": None,
                },
                "checkout_path": None,
            }
        },
        "archive_root": "C:\\reviews\\archive",
        "summary_root": "C:\\reviews\\summaries",
        "dashboard_file": "C:\\reviews\\dashboard.md",
        "operation_repository_sets": {},
        "runtime": "auto",
        "reviewer_effort": None,
        "re_review_scope": {"full_share": 0.5, "full_lines": 1000},
        "model_names": {},
        "local_mirror_root": None,
        "verdict_policy": {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
        "dashboard": {
            "start_marker": "<!-- code-review-pr-tracker:start -->",
            "end_marker": "<!-- code-review-pr-tracker:end -->",
            "status_overrides": {},
            "author_names": {},
        },
    }


def _full_config() -> dict[str, Any]:
    """Every section present: each reviewer source, an extra repository outside every set, and mixed-case identities."""
    return {
        "schema_version": 1,
        "default_repository_set": "work",
        "repository_sets": {"work": ["Owner/Repo", "owner/tools"], "solo": ["owner/generic"]},
        "repositories": {
            "OWNER/REPO": {
                "checkout_path": "C:/src/repo/",
                "reviewer": {
                    "manifest": True,
                    "skill": ".claude/skills/review",
                    "trusted_ref": " main ",
                    "protocol_version": 1,
                    "id": "repo-review",
                },
            },
            "owner/tools": {
                "reviewer": {
                    "id": "tools",
                    "protocol_version": 1,
                    "scope": "repository",
                    "skill": "skills/review",
                    "manifest": "C:/manifests/./tools.json",
                },
                "checkout_path": "C:\\src\\tools",
            },
            "owner/committed": {
                "reviewer": {
                    "id": "committed",
                    "protocol_version": 1,
                    "trusted_ref": "refs/heads/main",
                    "manifest_path": "review/manifest.json",
                },
                "checkout_path": "C:\\src\\committed",
            },
            "owner/generic": {
                "reviewer": {"id": "generic", "protocol_version": 1, "scope": "generic"},
                "checkout_path": "D:\\src\\generic",
            },
        },
        "archive_root": "C:/reviews/archive/",
        "local_mirror_root": "C:\\mirror",
        "summary_root": "C:\\reviews\\summaries",
        "dashboard_file": "C:\\notes\\..\\reviews\\dashboard.md",
        "github_login": "octo-cat",
        "runtime": "codex",
        "verdict_policy": {"should_fix_threshold": 2, "request_changes_for": ["MUST_FIX", "SHOULD_FIX"]},
        "dashboard": {
            "author_names": {"octo-cat": " Octo Cat "},
            "status_overrides": {"owner/repo#12": " Waiting on design "},
            "end_marker": "<!-- b -->",
            "start_marker": "<!-- a -->",
        },
        "operation_repository_sets": {"review-prs": "work", "update-pr-tracker": "solo"},
        "reviewer_effort": "high",
        "re_review_scope": {"full_lines": 200},
        "model_names": {"arn:aws:bedrock:model/x": " Model X "},
    }


def _full_expected() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "default_repository_set": "work",
        "repository_sets": {"work": ["owner/repo", "owner/tools"], "solo": ["owner/generic"]},
        "repositories": {
            "owner/repo": {
                "reviewer": {
                    "id": "repo-review",
                    "protocol_version": 1,
                    "trusted_ref": " main ",
                    "scope": "repository",
                    "manifest_path": None,
                    "skill": ".claude/skills/review",
                    "manifest": True,
                },
                "checkout_path": "C:\\src\\repo",
            },
            "owner/tools": {
                "reviewer": {
                    "id": "tools",
                    "protocol_version": 1,
                    "trusted_ref": None,
                    "scope": "repository",
                    "manifest_path": None,
                    "skill": "skills/review",
                    "manifest": "C:\\manifests\\tools.json",
                },
                "checkout_path": "C:\\src\\tools",
            },
            "owner/committed": {
                "reviewer": {
                    "id": "committed",
                    "protocol_version": 1,
                    "trusted_ref": "refs/heads/main",
                    "scope": "repository",
                    "manifest_path": "review/manifest.json",
                    "skill": None,
                    "manifest": None,
                },
                "checkout_path": "C:\\src\\committed",
            },
            "owner/generic": {
                "reviewer": {
                    "id": "generic",
                    "protocol_version": 1,
                    "trusted_ref": None,
                    "scope": "generic",
                    "manifest_path": None,
                    "skill": None,
                    "manifest": None,
                },
                "checkout_path": "D:\\src\\generic",
            },
        },
        "archive_root": "C:\\reviews\\archive",
        "local_mirror_root": "C:\\mirror",
        "summary_root": "C:\\reviews\\summaries",
        "dashboard_file": "C:\\reviews\\dashboard.md",
        "github_login": "octo-cat",
        "runtime": "codex",
        "verdict_policy": {"request_changes_for": ["MUST_FIX", "SHOULD_FIX"], "should_fix_threshold": 2},
        "dashboard": {
            "start_marker": "<!-- a -->",
            "end_marker": "<!-- b -->",
            "status_overrides": {"owner/repo#12": " Waiting on design "},
            "author_names": {"octo-cat": "Octo Cat"},
        },
        "operation_repository_sets": {"review-prs": "work", "update-pr-tracker": "solo"},
        "reviewer_effort": "high",
        "re_review_scope": {"full_share": 0.5, "full_lines": 200},
        "model_names": {"arn:aws:bedrock:model/x": "Model X"},
    }


def _rich_config() -> dict[str, Any]:
    """_config() with every optional section set to a valid non-default value and two repositories outside every
    set, leaving the parts the faults below change as they are in _config()."""
    config = _config()
    config["repositories"]["owner/tools"] = {
        "reviewer": {"id": "tools", "protocol_version": 1, "skill": "skills/review", "manifest": True},
        "checkout_path": "C:\\src\\tools",
    }
    config["repositories"]["owner/committed"] = {
        "reviewer": {"id": "committed", "protocol_version": 1, "manifest_path": "review/manifest.json"},
        "checkout_path": "C:\\src\\committed",
    }
    config["runtime"] = "codex"
    config["reviewer_effort"] = "high"
    config["re_review_scope"] = {"full_share": 0.25, "full_lines": 200}
    config["verdict_policy"] = {"request_changes_for": ["MUST_FIX", "SHOULD_FIX"], "should_fix_threshold": 2}
    config["operation_repository_sets"] = {"update-pr-tracker": "main"}
    config["model_names"] = {"model-x": "Model X"}
    config["local_mirror_root"] = "C:\\mirror"
    config["github_login"] = "octo-cat"
    # The markers stay at their defaults, which one fault below repeats; the accepted cases cover custom markers.
    config["dashboard"] = {
        "status_overrides": {"owner/repo#9": "Waiting"},
        "author_names": {"octo-cat": "Octo Cat"},
    }
    return config


def _target(config: Any, path: tuple[str, ...]) -> Any:
    target = config
    for key in path:
        target = target.setdefault(key, {})
    return target


def _set(path: tuple[str, ...], value: Any) -> Mutation:
    def apply(config: Any) -> Any:
        _target(config, path[:-1])[path[-1]] = copy.deepcopy(value)
        return config

    return apply


def _delete(path: tuple[str, ...]) -> Mutation:
    def apply(config: Any) -> Any:
        del _target(config, path[:-1])[path[-1]]
        return config

    return apply


def _prepend(path: tuple[str, ...], key: Any, value: Any) -> Mutation:
    """Put key first in the object at path, so a loop over it meets key before anything else."""

    def apply(config: Any) -> Any:
        target = _target(config, path)
        rest = list(target.items())
        target.clear()
        target[key] = copy.deepcopy(value)
        target.update(rest)
        return config

    return apply


def _replace(value: Any) -> Mutation:
    return lambda _ignored: copy.deepcopy(value)


def _chain(*mutations: Mutation) -> Mutation:
    def apply(config: Any) -> Any:
        for mutation in mutations:
            config = mutation(config)
        return config

    return apply


# The one repository's reviewer, switched from generic to its repository's own skill, with the checkout it needs.
REPOSITORY_REVIEWER = _chain(
    _set(REVIEWER, {"id": "repo-review", "protocol_version": 1, "skill": "skills/review"}),
    _set((*REPO, "checkout_path"), "C:\\src\\repo"),
)
# The same reviewer described by a manifest committed to the repository instead.
MANIFEST_PATH_REVIEWER = _chain(
    _set(REVIEWER, {"id": "repo-review", "protocol_version": 1, "manifest_path": "review/manifest.json"}),
    _set((*REPO, "checkout_path"), "C:\\src\\repo"),
)


def _repository_expected(**reviewer: Any) -> Mutation:
    fields = {
        "id": "repo-review",
        "protocol_version": 1,
        "trusted_ref": None,
        "scope": "repository",
        "manifest_path": None,
        "skill": "skills/review",
        "manifest": None,
    }
    fields.update(reviewer)
    return _set(REPO, {"reviewer": fields, "checkout_path": "C:\\src\\repo"})


# Configurations validate_config accepts: each mutates _config(), and the expected result is _expected() mutated alike.
ACCEPTED: list[tuple[str, Mutation, Mutation]] = [
    ("minimal configuration", _chain(), _chain()),
    (
        "every optional section at its default",
        _chain(
            _set(("runtime",), "auto"),
            _set(("reviewer_effort",), None),
            _set(("re_review_scope",), {}),
            _set(("verdict_policy",), {}),
            _set(("operation_repository_sets",), {}),
            _set(("model_names",), {}),
            _set(("local_mirror_root",), None),
            _set(("github_login",), None),
            _set(("dashboard",), {}),
            _set(("dashboard", "status_overrides"), {}),
            _set(("dashboard", "author_names"), {}),
        ),
        _set(("github_login",), None),
    ),
    *[
        (f"runtime {host}", _set(("runtime",), host), _set(("runtime",), host))
        for host in ["auto", "claude-code", "codex", "copilot-cli"]
    ],
    *[
        (f"reviewer_effort {effort}", _set(("reviewer_effort",), effort), _set(("reviewer_effort",), effort))
        for effort in ["low", "medium", "high", "xhigh", "max"]
    ],
    (
        "identities lowercased",
        _chain(
            _set(("repository_sets", "main"), ["Owner/Repo"]),
            _delete(REPO),
            _set(("repositories", "OWNER/repo"), _generic_entry()),
        ),
        _chain(),
    ),
    (
        "longest identity parts and punctuation",
        _chain(
            _set(("repository_sets", "main"), ["a" * 100 + "/" + "b" * 100, "my.org/my_repo-2"]),
            _set(("repositories", "a" * 100 + "/" + "b" * 100), _generic_entry()),
            _set(("repositories", "my.org/my_repo-2"), _generic_entry()),
        ),
        _chain(
            _set(("repository_sets", "main"), ["a" * 100 + "/" + "b" * 100, "my.org/my_repo-2"]),
            _set(("repositories", "a" * 100 + "/" + "b" * 100), _expected()["repositories"]["owner/repo"]),
            _set(("repositories", "my.org/my_repo-2"), _expected()["repositories"]["owner/repo"]),
        ),
    ),
    (
        "set names with punctuation, a repository in two sets, and one outside every set",
        _chain(
            _set(("repository_sets", "A.b_c-9"), ["owner/repo"]),
            _set(("repository_sets", "0"), ["owner/repo"]),
            _set(("repositories", "owner/extra"), _generic_entry()),
        ),
        _chain(
            _set(("repository_sets", "A.b_c-9"), ["owner/repo"]),
            _set(("repository_sets", "0"), ["owner/repo"]),
            _set(("repositories", "owner/extra"), _expected()["repositories"]["owner/repo"]),
        ),
    ),
    (
        "generic reviewer with a checkout",
        _set((*REPO, "checkout_path"), "C:/src/repo/"),
        _set((*REPO, "checkout_path"), "C:\\src\\repo"),
    ),
    (
        "generic reviewer with explicit nulls",
        _chain(
            _set((*REVIEWER, "trusted_ref"), None),
            _set((*REVIEWER, "manifest_path"), None),
            _set((*REVIEWER, "skill"), None),
            _set((*REVIEWER, "manifest"), None),
            _set((*REPO, "checkout_path"), None),
        ),
        _chain(),
    ),
    ("repository reviewer with its skill", REPOSITORY_REVIEWER, _repository_expected()),
    (
        "repository reviewer with an explicit scope",
        _chain(REPOSITORY_REVIEWER, _set((*REVIEWER, "scope"), "repository")),
        _repository_expected(),
    ),
    (
        "repository reviewer with the default manifest",
        _chain(REPOSITORY_REVIEWER, _set((*REVIEWER, "manifest"), True)),
        _repository_expected(manifest=True),
    ),
    (
        "repository reviewer with a manifest path",
        _chain(REPOSITORY_REVIEWER, _set((*REVIEWER, "manifest"), "c:/manifests/../kept/m.json")),
        _repository_expected(manifest="c:\\kept\\m.json"),
    ),
    (
        "repository reviewer with an untrimmed trusted ref",
        _chain(REPOSITORY_REVIEWER, _set((*REVIEWER, "trusted_ref"), " refs/heads/main ")),
        _repository_expected(trusted_ref=" refs/heads/main "),
    ),
    (
        "repository reviewer ids at the limits",
        _chain(REPOSITORY_REVIEWER, _set((*REVIEWER, "id"), "0" + "a-" * 31 + "b")),
        _repository_expected(id="0" + "a-" * 31 + "b"),
    ),
    (
        "repository reviewer with a one-character id",
        _chain(REPOSITORY_REVIEWER, _set((*REVIEWER, "id"), "z")),
        _repository_expected(id="z"),
    ),
    (
        "committed manifest with a trusted ref",
        _chain(MANIFEST_PATH_REVIEWER, _set((*REVIEWER, "trusted_ref"), "release")),
        _repository_expected(manifest_path="review/manifest.json", skill=None, trusted_ref="release"),
    ),
    (
        "committed manifest path with dot and empty segments",
        _chain(MANIFEST_PATH_REVIEWER, _set((*REVIEWER, "manifest_path"), "./review//manifest.json")),
        _repository_expected(manifest_path="./review//manifest.json", skill=None),
    ),
    (
        "skill path that leaves nothing out",
        _chain(REPOSITORY_REVIEWER, _set((*REVIEWER, "skill"), ".claude/skills/review/")),
        _repository_expected(skill=".claude/skills/review/"),
    ),
    (
        "re_review_scope at its bounds",
        _set(("re_review_scope",), {"full_share": 1, "full_lines": 1}),
        _set(("re_review_scope",), {"full_share": 1, "full_lines": 1}),
    ),
    (
        "re_review_scope with a small share",
        _set(("re_review_scope",), {"full_share": 1e-9}),
        _set(("re_review_scope",), {"full_share": 1e-9, "full_lines": 1000}),
    ),
    (
        "every severity, repeated, and the lowest threshold",
        _set(
            ("verdict_policy",),
            {"request_changes_for": ["SUGGESTION", "SHOULD_FIX", "MUST_FIX", "SUGGESTION"], "should_fix_threshold": 1},
        ),
        _set(
            ("verdict_policy",),
            {"request_changes_for": ["SUGGESTION", "SHOULD_FIX", "MUST_FIX", "SUGGESTION"], "should_fix_threshold": 1},
        ),
    ),
    (
        "verdict policy with only a threshold",
        _set(("verdict_policy",), {"should_fix_threshold": 10}),
        _set(("verdict_policy",), {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 10}),
    ),
    (
        "every operation with a set",
        _set(
            ("operation_repository_sets",),
            {"review-prs": "main", "update-pr-tracker": "main", "review-insights": "main"},
        ),
        _set(
            ("operation_repository_sets",),
            {"review-prs": "main", "update-pr-tracker": "main", "review-insights": "main"},
        ),
    ),
    (
        "paths normalized",
        _chain(
            _set(("archive_root",), "C:/a/../b"),
            _set(("summary_root",), "d:\\x\\.\\y\\"),
            _set(("dashboard_file",), "C:\\r\\\\d.md"),
            _set(("local_mirror_root",), "E:/m"),
        ),
        _chain(
            _set(("archive_root",), "C:\\b"),
            _set(("summary_root",), "d:\\x\\y"),
            _set(("dashboard_file",), "C:\\r\\d.md"),
            _set(("local_mirror_root",), "E:\\m"),
        ),
    ),
    ("github_login kept as written", _set(("github_login",), " x "), _set(("github_login",), " x ")),
    (
        "custom markers",
        _set(("dashboard",), {"start_marker": "<a>", "end_marker": "<b>"}),
        _chain(_set(("dashboard", "start_marker"), "<a>"), _set(("dashboard", "end_marker"), "<b>")),
    ),
    (
        "status overrides kept as written",
        _set(
            ("dashboard", "status_overrides"),
            {"a/b#1": " Waiting ", "my.org/my_repo-2#1234567890": "draft", "owner/repo#10": "Reviewing"},
        ),
        _set(
            ("dashboard", "status_overrides"),
            {"a/b#1": " Waiting ", "my.org/my_repo-2#1234567890": "draft", "owner/repo#10": "Reviewing"},
        ),
    ),
    (
        "model and author names trimmed",
        _chain(
            _set(("model_names",), {"m": " M ", "x" * 200: "y" * 100}),
            _set(("dashboard", "author_names"), {"a": " A ", "B-" + "c" * 37: "Bc"}),
        ),
        _chain(
            _set(("model_names",), {"m": "M", "x" * 200: "y" * 100}),
            _set(("dashboard", "author_names"), {"a": "A", "B-" + "c" * 37: "Bc"}),
        ),
    ),
    # Quirks pinned as they stand, not endorsed: the version fields compare with `!=`, so true and 1.0 equal 1; the
    # schema version passes through as given, and the reviewer protocol is stored as 1. A drive-letter skill or
    # manifest_path is not caught by the repository-relative check.
    ("schema_version true", _set(("schema_version",), True), _set(("schema_version",), True)),
    ("schema_version 1.0", _set(("schema_version",), 1.0), _set(("schema_version",), 1.0)),
    (
        "protocol_version true",
        _chain(REPOSITORY_REVIEWER, _set((*REVIEWER, "protocol_version"), True)),
        _repository_expected(),
    ),
    (
        "protocol_version 1.0",
        _chain(REPOSITORY_REVIEWER, _set((*REVIEWER, "protocol_version"), 1.0)),
        _repository_expected(),
    ),
    (
        "drive-letter skill",
        _chain(REPOSITORY_REVIEWER, _set((*REVIEWER, "skill"), "C:/skills/review")),
        _repository_expected(skill="C:/skills/review"),
    ),
    (
        "drive-letter manifest_path",
        _chain(MANIFEST_PATH_REVIEWER, _set((*REVIEWER, "manifest_path"), "C:/review/manifest.json")),
        _repository_expected(manifest_path="C:/review/manifest.json", skill=None),
    ),
]

C = ConfigurationError
# Null and an empty list, typed so the mixed value lists built from them below type-check.
NULL_OR_EMPTY_LIST: list[Any] = [None, []]


def _path_faults(field: str, path: tuple[str, ...], *, nullable: bool = False) -> list[Case]:
    """Every outcome of validate_windows_absolute_path for one field."""
    shape = f"{field} must be a non-empty absolute Windows path"
    drive = f"{field} must be an absolute drive-letter path"
    root = f"{field} must not be a filesystem root"
    faults: list[Case] = [
        (f"{field} empty", _set(path, ""), C, shape),
        (f"{field} not a string", _set(path, 1), C, shape),
        (f"{field} false", _set(path, False), C, shape),
        (f"{field} relative", _set(path, "reviews"), C, drive),
        (f"{field} rooted without a drive", _set(path, "\\reviews"), C, drive),
        (f"{field} drive-relative", _set(path, "C:reviews"), C, drive),
        (f"{field} UNC", _set(path, "\\\\server\\share\\reviews"), C, drive),
        (f"{field} extended-length", _set(path, "\\\\?\\C:\\reviews"), C, drive),
        (f"{field} root", _set(path, "C:\\"), C, root),
        (f"{field} root with a slash", _set(path, "C:/"), C, root),
        (f"{field} root after normalization", _set(path, "C:\\reviews\\.."), C, root),
    ]
    if not nullable:
        faults.append((f"{field} null", _set(path, None), C, shape))
    return faults


def _marker_faults(key: str) -> list[Case]:
    path = ("dashboard", key)
    return [
        (f"{key} not a string", _set(path, 1), C, MARKERS_MESSAGE),
        (f"{key} null", _set(path, None), C, MARKERS_MESSAGE),
        (f"{key} empty", _set(path, ""), C, MARKERS_MESSAGE),
        (f"{key} blank", _set(path, "  "), C, MARKERS_MESSAGE),
        (f"{key} untrimmed", _set(path, " <a>"), C, MARKERS_MESSAGE),
        (f"{key} untrimmed newline", _set(path, "<a>\n"), C, MARKERS_MESSAGE),
        (f"{key} inner newline", _set(path, "<a\nb>"), C, MARKERS_MESSAGE),
        (f"{key} inner carriage return", _set(path, "<a\rb>"), C, MARKERS_MESSAGE),
    ]


def _unsafe_paths(key: str, base: Mutation, message: str) -> list[Case]:
    return [
        (f"{key} {label}", _chain(base, _set((*REVIEWER, key), value)), C, message)
        for label, value in [
            ("empty", ""),
            ("not a string", 1),
            ("false", False),
            ("backslash", "skills\\review"),
            ("absolute", "/skills/review"),
            ("parent", "skills/../review"),
            ("only parent", ".."),
            ("trailing parent", "skills/.."),
        ]
    ]


REJECTED: list[Case] = [
    ("config a list", _replace([]), C, "config must be an object"),
    ("config null", _replace(None), C, "config must be an object"),
    ("unknown top-level field", _set(("extra",), 1), C, "config contains unknown field(s): extra"),
    (
        "unknown top-level fields sorted",
        _chain(_set(("zeta",), 1), _set(("alpha",), 1)),
        C,
        "config contains unknown field(s): alpha, zeta",
    ),
    ("schema_version missing", _delete(("schema_version",)), C, "Unsupported config schema version: None"),
    ("schema_version 0", _set(("schema_version",), 0), C, "Unsupported config schema version: 0"),
    ("schema_version string", _set(("schema_version",), "1"), C, "Unsupported config schema version: '1'"),
    ("schema_version false", _set(("schema_version",), False), C, "Unsupported config schema version: False"),
    ("schema_version 1.5", _set(("schema_version",), 1.5), C, "Unsupported config schema version: 1.5"),
    ("schema_version 2.0", _set(("schema_version",), 2.0), C, "Unsupported config schema version: 2.0"),
    ("schema_version 2", _set(("schema_version",), 2), C, "Unsupported future config schema version: 2"),
    # repository_sets
    ("repository_sets missing", _delete(("repository_sets",)), C, "repository_sets must be an object"),
    ("repository_sets a list", _set(("repository_sets",), ["owner/repo"]), C, "repository_sets must be an object"),
    ("repository_sets empty", _set(("repository_sets",), {}), C, "repository_sets must not be empty"),
    *[
        (
            f"set name {name!r}",
            _prepend(("repository_sets",), name, ["owner/repo"]),
            C,
            f"Invalid repository-set name: {name!r}",
        )
        for name in ["", "-main", "_main", ".main", "main set", "main\n", "mäin", "main/x"]
    ],
    ("set name not a string", _prepend(("repository_sets",), 1, ["owner/repo"]), C, "Invalid repository-set name: 1"),
    ("set members empty", _set(("repository_sets", "main"), []), C, "Repository set 'main' must not be empty"),
    ("set members null", _set(("repository_sets", "main"), None), C, "Repository set 'main' must not be empty"),
    (
        "set members a string",
        _set(("repository_sets", "main"), "owner/repo"),
        C,
        "Repository set 'main' must not be empty",
    ),
    (
        "set members an object",
        _set(("repository_sets", "main"), {"owner/repo": 1}),
        C,
        "Repository set 'main' must not be empty",
    ),
    *[
        (
            f"set member {member!r}",
            _set(("repository_sets", "main"), ["owner/repo", member]),
            C,
            f"Invalid repository identity: {member!r}",
        )
        for member in [
            "owner",
            "owner/repo/x",
            "-owner/repo",
            "owner/.repo",
            "owner/repo name",
            "owner/repo\n",
            "a" * 101 + "/repo",
            "owner/" + "b" * 101,
            "",
            1,
            None,
        ]
    ],
    (
        "set members duplicated ignoring case",
        _set(("repository_sets", "main"), ["owner/repo", "Owner/Repo"]),
        C,
        "Repository set 'main' contains duplicates",
    ),
    # default_repository_set
    *[
        (
            f"default set {value!r}",
            _set(("default_repository_set",), value),
            C,
            "default_repository_set must name an existing set",
        )
        for value in ["other", "Main", "", 1, ["main"], None]
    ],
    (
        "default set missing",
        _delete(("default_repository_set",)),
        C,
        "default_repository_set must name an existing set",
    ),
    # repositories
    ("repositories null", _set(("repositories",), None), C, "repositories must be an object"),
    ("repositories a list", _set(("repositories",), []), C, "repositories must be an object"),
    ("repositories missing", _delete(("repositories",)), C, "Missing per-repository configuration for: owner/repo"),
    ("repositories empty", _set(("repositories",), {}), C, "Missing per-repository configuration for: owner/repo"),
    (
        "repository key invalid",
        _prepend(("repositories",), "owner", _generic_entry()),
        C,
        "Invalid repository identity: 'owner'",
    ),
    (
        "repository keys duplicated after normalization",
        _set(("repositories", "Owner/Repo"), _generic_entry()),
        C,
        "Duplicate repository configuration after normalization: owner/repo",
    ),
    ("entry null", _set(REPO, None), C, f"{FIELD} must be an object"),
    ("entry a list", _set(REPO, []), C, f"{FIELD} must be an object"),
    (
        "entry named as written",
        _chain(_delete(REPO), _set(("repositories", "Owner/Repo"), None)),
        C,
        "repositories.Owner/Repo must be an object",
    ),
    ("entry unknown field", _set((*REPO, "extra"), 1), C, f"{FIELD} contains unknown field(s): extra"),
    ("reviewer missing", _delete(REVIEWER), C, f"{FIELD}.reviewer must be an object"),
    ("reviewer a string", _set(REVIEWER, "generic"), C, f"{FIELD}.reviewer must be an object"),
    (
        "reviewer unknown field",
        _set((*REVIEWER, "model"), "x"),
        C,
        f"{FIELD}.reviewer contains unknown field(s): model",
    ),
    ("reviewer id missing", _delete((*REVIEWER, "id")), C, f"{FIELD}.reviewer.id is invalid"),
    *[
        (f"reviewer id {value!r}", _set((*REVIEWER, "id"), value), C, f"{FIELD}.reviewer.id is invalid")
        for value in ["", "Generic", "-x", "a_b", "a.b", "a" * 65, "a\n", 1, None]
    ],
    ("protocol missing", _delete((*REVIEWER, "protocol_version")), C, f"{FIELD}.reviewer protocol is unsupported"),
    *[
        (
            f"protocol {value!r}",
            _set((*REVIEWER, "protocol_version"), value),
            C,
            f"{FIELD}.reviewer protocol is unsupported",
        )
        for value in [2, 0, "1", False, 1.5]
    ],
    *[
        (
            f"trusted_ref {value!r}",
            _set((*REVIEWER, "trusted_ref"), value),
            C,
            f"{FIELD}.reviewer.trusted_ref is invalid",
        )
        for value in ["", "  ", "\n", 1, False, ["main"]]
    ],
    (
        "trusted_ref a pull ref",
        _chain(REPOSITORY_REVIEWER, _set((*REVIEWER, "trusted_ref"), "refs/pull/1/head")),
        C,
        f"{FIELD}.reviewer.trusted_ref cannot be a pull ref",
    ),
    (
        "trusted_ref a pull ref on a generic reviewer",
        _set((*REVIEWER, "trusted_ref"), "refs/pull/"),
        C,
        f"{FIELD}.reviewer.trusted_ref cannot be a pull ref",
    ),
    *[
        (f"scope {value!r}", _set((*REVIEWER, "scope"), value), C, f"{FIELD}.reviewer.scope is invalid")
        for value in ["other", "Generic", "", None, 1]
    ],
    ("generic scope with another id", _set((*REVIEWER, "id"), "repo-review"), C, GENERIC_MESSAGE),
    *[
        (f"generic scope with {key} {value!r}", _set((*REVIEWER, key), value), C, GENERIC_MESSAGE)
        for key, value in [
            ("manifest_path", "review/manifest.json"),
            ("skill", "skills/review"),
            ("manifest", True),
            ("manifest", False),
            ("manifest", "C:\\manifest.json"),
            ("trusted_ref", "main"),
        ]
    ],
    (
        "manifest_path with a skill",
        _chain(MANIFEST_PATH_REVIEWER, _set((*REVIEWER, "skill"), "skills/review")),
        C,
        f"{FIELD}.reviewer sets manifest_path, so it cannot also set skill or manifest",
    ),
    *[
        (
            f"manifest_path with manifest {value!r}",
            _chain(MANIFEST_PATH_REVIEWER, _set((*REVIEWER, "manifest"), value)),
            C,
            f"{FIELD}.reviewer sets manifest_path, so it cannot also set skill or manifest",
        )
        for value in [True, False, "C:\\manifest.json"]
    ],
    *_unsafe_paths("manifest_path", MANIFEST_PATH_REVIEWER, f"{FIELD}.reviewer.manifest_path is unsafe"),
    (
        "neither skill nor manifest_path",
        _chain(REPOSITORY_REVIEWER, _delete((*REVIEWER, "skill"))),
        C,
        f"{FIELD}.reviewer needs skill (the repository's review skill) or manifest_path",
    ),
    (
        "manifest without a skill",
        _chain(REPOSITORY_REVIEWER, _delete((*REVIEWER, "skill")), _set((*REVIEWER, "manifest"), True)),
        C,
        f"{FIELD}.reviewer needs skill (the repository's review skill) or manifest_path",
    ),
    *_unsafe_paths("skill", REPOSITORY_REVIEWER, f"{FIELD}.reviewer.skill is unsafe"),
    *[
        (f"repository {name}", _chain(REPOSITORY_REVIEWER, mutation), error, message)
        for name, mutation, error, message in _path_faults(
            f"{FIELD}.reviewer.manifest", (*REVIEWER, "manifest"), nullable=True
        )
    ],
    (
        "reviewer.manifest the string true",
        _chain(REPOSITORY_REVIEWER, _set((*REVIEWER, "manifest"), "true")),
        C,
        f"{FIELD}.reviewer.manifest must be an absolute drive-letter path",
    ),
    (
        "checkout missing for a repository reviewer",
        _chain(REPOSITORY_REVIEWER, _delete((*REPO, "checkout_path"))),
        C,
        f"{FIELD}.checkout_path is required for a repository reviewer",
    ),
    (
        "checkout null for a repository reviewer",
        _chain(REPOSITORY_REVIEWER, _set((*REPO, "checkout_path"), None)),
        C,
        f"{FIELD}.checkout_path is required for a repository reviewer",
    ),
    (
        "checkout missing for a committed manifest",
        _chain(MANIFEST_PATH_REVIEWER, _delete((*REPO, "checkout_path"))),
        C,
        f"{FIELD}.checkout_path is required for a repository reviewer",
    ),
    *_path_faults(f"{FIELD}.checkout_path", (*REPO, "checkout_path"), nullable=True),
    *[
        (f"repository {name}", _chain(REPOSITORY_REVIEWER, mutation), error, message)
        for name, mutation, error, message in _path_faults(
            f"{FIELD}.checkout_path", (*REPO, "checkout_path"), nullable=True
        )
    ],
    (
        "a set member without configuration",
        _set(("repository_sets", "main"), ["owner/repo", "owner/other"]),
        C,
        "Missing per-repository configuration for: owner/other",
    ),
    (
        "set members without configuration sorted",
        _chain(
            _set(("repository_sets", "main"), ["owner/zed", "owner/repo"]),
            _set(("repository_sets", "other"), ["Owner/Alpha"]),
        ),
        C,
        "Missing per-repository configuration for: owner/alpha, owner/zed",
    ),
    # runtime and reviewer_effort
    *[
        (f"runtime {value!r}", _set(("runtime",), value), C, f"Unknown runtime host: {value!r}")
        for value in ["bash", "Auto", "", None, 1]
    ],
    *[
        (f"reviewer_effort {value!r}", _set(("reviewer_effort",), value), C, f"{EFFORT_MESSAGE}{value!r}")
        for value in ["extreme", "High", "", 1, False]
    ],
    # re_review_scope
    ("re_review_scope null", _set(("re_review_scope",), None), C, "re_review_scope must be an object"),
    ("re_review_scope a list", _set(("re_review_scope",), []), C, "re_review_scope must be an object"),
    (
        "re_review_scope unknown field",
        _set(("re_review_scope", "full_files"), 1),
        C,
        "re_review_scope contains unknown field(s): full_files",
    ),
    *[
        (f"full_share {value!r}", _set(("re_review_scope", "full_share"), value), C, SHARE_MESSAGE)
        for value in [0, 0.0, -0.5, 1.0000001, 2, True, False, "0.5", None, float("nan"), float("inf")]
    ],
    *[
        (f"full_lines {value!r}", _set(("re_review_scope", "full_lines"), value), C, LINES_MESSAGE)
        for value in [0, -1, True, 1.0, 10.5, "10", None]
    ],
    # verdict_policy
    ("verdict_policy null", _set(("verdict_policy",), None), C, "verdict_policy must be an object"),
    ("verdict_policy a list", _set(("verdict_policy",), []), C, "verdict_policy must be an object"),
    (
        "verdict_policy unknown field",
        _set(("verdict_policy", "block_on"), []),
        C,
        "verdict_policy contains unknown field(s): block_on",
    ),
    *[
        (
            f"request_changes_for {value!r}",
            _set(("verdict_policy", "request_changes_for"), value),
            C,
            "verdict_policy.request_changes_for must be non-empty",
        )
        for value in NULL_OR_EMPTY_LIST + ["MUST_FIX", {"MUST_FIX": 1}]
    ],
    *[
        (f"request_changes_for {value!r}", _set(("verdict_policy", "request_changes_for"), value), C, SEVERITY_MESSAGE)
        for value in [["BLOCKER"], ["MUST_FIX", "must_fix"], [1], [None], ["MUST_FIX "]]
    ],
    *[
        (
            f"should_fix_threshold {value!r}",
            _set(("verdict_policy", "should_fix_threshold"), value),
            C,
            THRESHOLD_MESSAGE,
        )
        for value in [0, -1, True, 1.0, "3", None]
    ],
    # operation_repository_sets
    *[
        (
            f"operation sets {value!r}",
            _set(("operation_repository_sets",), value),
            C,
            "operation_repository_sets must be an object",
        )
        for value in NULL_OR_EMPTY_LIST + ["main"]
    ],
    *[
        (
            f"operation {value!r}",
            _set(("operation_repository_sets", value), "main"),
            C,
            f"operation_repository_sets has an unknown operation: {value!r}",
        )
        for value in ["deploy", "Review-PRs", ""]
    ],
    (
        "operation not a string",
        _prepend(("operation_repository_sets",), 1, "main"),
        C,
        "operation_repository_sets has an unknown operation: 1",
    ),
    (
        "unknown operation with an unknown set",
        _set(("operation_repository_sets", "deploy"), "other"),
        C,
        "operation_repository_sets has an unknown operation: 'deploy'",
    ),
    *[
        (
            f"operation set {value!r}",
            _set(("operation_repository_sets", "review-insights"), value),
            C,
            "operation_repository_sets.review-insights must name an existing set",
        )
        for value in ["other", "Main", None, 1]
    ],
    # model_names, reached through normalize_model_names
    ("model_names null", _set(("model_names",), None), C, "model_names must be an object"),
    (
        "model_names key untrimmed",
        _set(("model_names",), {" m": "M"}),
        C,
        "Invalid model_names key ' m': it must be a trimmed single line of at most 200 characters",
    ),
    (
        "model_names key too long",
        _set(("model_names",), {"x" * 201: "M"}),
        C,
        f"Invalid model_names key {'x' * 201!r}: it must be a trimmed single line of at most 200 characters",
    ),
    (
        "model_names name blank",
        _set(("model_names",), {"m": " "}),
        C,
        "model_names.m must be a non-empty single-line name of at most 100 characters",
    ),
    (
        "model_names name too long",
        _set(("model_names",), {"m": "y" * 101}),
        C,
        "model_names.m must be a non-empty single-line name of at most 100 characters",
    ),
    # the four paths
    *_path_faults("archive_root", ("archive_root",)),
    ("archive_root missing", _delete(("archive_root",)), C, "archive_root must be a non-empty absolute Windows path"),
    *_path_faults("local_mirror_root", ("local_mirror_root",), nullable=True),
    *_path_faults("summary_root", ("summary_root",)),
    ("summary_root missing", _delete(("summary_root",)), C, "summary_root must be a non-empty absolute Windows path"),
    *_path_faults("dashboard_file", ("dashboard_file",)),
    (
        "dashboard_file missing",
        _delete(("dashboard_file",)),
        C,
        "dashboard_file must be a non-empty absolute Windows path",
    ),
    # github_login
    *[
        (
            f"github_login {value!r}",
            _set(("github_login",), value),
            C,
            "github_login must be null or a non-empty string",
        )
        for value in ["", "  ", "\n", 1, False, ["octo"]]
    ],
    # dashboard
    ("dashboard null", _set(("dashboard",), None), C, "dashboard must be an object"),
    ("dashboard a list", _set(("dashboard",), []), C, "dashboard must be an object"),
    ("dashboard unknown field", _set(("dashboard", "title"), "x"), C, "dashboard contains unknown field(s): title"),
    *_marker_faults("start_marker"),
    *_marker_faults("end_marker"),
    (
        "markers equal",
        _chain(_set(("dashboard", "start_marker"), "<m>"), _set(("dashboard", "end_marker"), "<m>")),
        C,
        MARKERS_MESSAGE,
    ),
    (
        "end marker equal to the default start marker",
        _set(("dashboard", "end_marker"), "<!-- code-review-pr-tracker:start -->"),
        C,
        MARKERS_MESSAGE,
    ),
    *[
        (
            f"status_overrides {value!r}",
            _set(("dashboard", "status_overrides"), value),
            C,
            "dashboard.status_overrides must be an object",
        )
        for value in NULL_OR_EMPTY_LIST + ["x"]
    ],
    *[
        (
            f"status override key {key!r}",
            _set(("dashboard", "status_overrides", key), "Waiting"),
            C,
            f"Invalid dashboard status override key: {key!r}",
        )
        for key in [
            "repo#1",
            "owner/repo",
            "owner/repo#0",
            "owner/repo#01",
            "owner/repo#1 ",
            "owner/re po#1",
            "o/r/x#1",
            "owner/repo#-1",
        ]
    ],
    (
        "status override key not a string",
        _prepend(("dashboard", "status_overrides"), 1, "Waiting"),
        C,
        "Invalid dashboard status override key: 1",
    ),
    *[
        (
            f"status override value {value!r}",
            _set(("dashboard", "status_overrides", "owner/repo#1"), value),
            C,
            "Dashboard status override owner/repo#1 must be non-empty",
        )
        for value in ["", "  ", 1, None]
    ],
    *[
        (
            f"status override {value!r}",
            _set(("dashboard", "status_overrides", "owner/repo#1"), value),
            C,
            "Dashboard status override owner/repo#1 duplicates a computed tracker state",
        )
        for value in [*COMPUTED_STATES, " To Review ", "DRAFTS"]
    ],
    ("author_names null", _set(("dashboard", "author_names"), None), C, "dashboard.author_names must be an object"),
    (
        "author_names login invalid",
        _set(("dashboard", "author_names"), {"-octo": "Octo"}),
        C,
        "Invalid dashboard author name login: '-octo'",
    ),
    (
        "author_names logins equal ignoring case",
        _set(("dashboard", "author_names"), {"Octo": "A", "octo": "B"}),
        C,
        "Duplicate dashboard author name login ignoring case: octo",
    ),
    (
        "author_names name blank",
        _set(("dashboard", "author_names"), {"octo": " "}),
        C,
        "Dashboard author name for octo must be a non-empty single-line string",
    ),
]

# Quirk pinned as it stands, not endorsed: a list where a membership test expects a hashable value escapes as
# TypeError instead of ConfigurationError. Only the type and its cause are pinned; the full text varies by Python.
UNHASHABLE: list[tuple[str, Mutation]] = [
    ("reviewer scope a list", _set((*REVIEWER, "scope"), ["generic"])),
    ("runtime a list", _set(("runtime",), ["auto"])),
    ("reviewer_effort a list", _set(("reviewer_effort",), ["high"])),
    ("request_changes_for holding a list", _set(("verdict_policy", "request_changes_for"), [["MUST_FIX"]])),
    ("operation set a list", _set(("operation_repository_sets", "review-prs"), ["main"])),
]

# One fault per check, in the order validate_config detects them, on the generic reviewer. For each k, every fault from
# k on is applied at once, and check k's error must be the one raised.
STAGES: list[Case] = [
    ("config not an object", _replace([]), C, "config must be an object"),
    ("unknown top-level field", _set(("zzz",), 1), C, "config contains unknown field(s): zzz"),
    ("future schema_version", _set(("schema_version",), 2), C, "Unsupported future config schema version: 2"),
    ("schema_version", _set(("schema_version",), 0), C, "Unsupported config schema version: 0"),
    ("repository_sets not an object", _set(("repository_sets",), []), C, "repository_sets must be an object"),
    ("repository_sets empty", _set(("repository_sets",), {}), C, "repository_sets must not be empty"),
    ("set name", _prepend(("repository_sets",), "-bad", []), C, "Invalid repository-set name: '-bad'"),
    ("set members empty", _set(("repository_sets", "main"), []), C, "Repository set 'main' must not be empty"),
    ("set member", _set(("repository_sets", "main"), ["bad", "bad"]), C, "Invalid repository identity: 'bad'"),
    (
        "set duplicates",
        _set(("repository_sets", "main"), ["owner/repo", "Owner/Repo"]),
        C,
        "Repository set 'main' contains duplicates",
    ),
    ("default set", _set(("default_repository_set",), "other"), C, "default_repository_set must name an existing set"),
    ("repositories not an object", _set(("repositories",), []), C, "repositories must be an object"),
    ("repository key", _prepend(("repositories",), "nope", None), C, "Invalid repository identity: 'nope'"),
    (
        "repository key duplicated",
        _prepend(("repositories",), "Owner/Repo", _generic_entry()),
        C,
        "Duplicate repository configuration after normalization: owner/repo",
    ),
    ("entry not an object", _set(REPO, []), C, f"{FIELD} must be an object"),
    ("entry unknown field", _set((*REPO, "zzz"), 1), C, f"{FIELD} contains unknown field(s): zzz"),
    ("reviewer not an object", _set(REVIEWER, []), C, f"{FIELD}.reviewer must be an object"),
    ("reviewer unknown field", _set((*REVIEWER, "zzz"), 1), C, f"{FIELD}.reviewer contains unknown field(s): zzz"),
    ("reviewer id", _set((*REVIEWER, "id"), "Bad"), C, f"{FIELD}.reviewer.id is invalid"),
    ("protocol", _set((*REVIEWER, "protocol_version"), 2), C, f"{FIELD}.reviewer protocol is unsupported"),
    ("trusted_ref", _set((*REVIEWER, "trusted_ref"), " "), C, f"{FIELD}.reviewer.trusted_ref is invalid"),
    (
        "pull ref",
        _set((*REVIEWER, "trusted_ref"), "refs/pull/1/head"),
        C,
        f"{FIELD}.reviewer.trusted_ref cannot be a pull ref",
    ),
    ("scope", _set((*REVIEWER, "scope"), "other"), C, f"{FIELD}.reviewer.scope is invalid"),
    ("generic scope", _set((*REVIEWER, "skill"), "skills/review"), C, GENERIC_MESSAGE),
    (
        "checkout",
        _set((*REPO, "checkout_path"), "relative"),
        C,
        f"{FIELD}.checkout_path must be an absolute drive-letter path",
    ),
    (
        "second entry",
        _set(("repositories", "owner/later"), {"reviewer": None}),
        C,
        "repositories.owner/later.reviewer must be an object",
    ),
    (
        "missing configuration",
        _set(("repository_sets", "zz"), ["owner/other"]),
        C,
        "Missing per-repository configuration for: owner/other",
    ),
    ("runtime", _set(("runtime",), "bash"), C, "Unknown runtime host: 'bash'"),
    ("reviewer_effort", _set(("reviewer_effort",), "extreme"), C, f"{EFFORT_MESSAGE}'extreme'"),
    ("re_review_scope not an object", _set(("re_review_scope",), []), C, "re_review_scope must be an object"),
    (
        "re_review_scope unknown field",
        _set(("re_review_scope", "zzz"), 1),
        C,
        "re_review_scope contains unknown field(s): zzz",
    ),
    ("full_share", _set(("re_review_scope", "full_share"), 0), C, SHARE_MESSAGE),
    ("full_lines", _set(("re_review_scope", "full_lines"), 0), C, LINES_MESSAGE),
    ("verdict_policy not an object", _set(("verdict_policy",), []), C, "verdict_policy must be an object"),
    (
        "verdict_policy unknown field",
        _set(("verdict_policy", "zzz"), 1),
        C,
        "verdict_policy contains unknown field(s): zzz",
    ),
    (
        "request_changes_for empty",
        _set(("verdict_policy", "request_changes_for"), []),
        C,
        "verdict_policy.request_changes_for must be non-empty",
    ),
    ("severity", _set(("verdict_policy", "request_changes_for"), ["BLOCKER"]), C, SEVERITY_MESSAGE),
    ("threshold", _set(("verdict_policy", "should_fix_threshold"), 0), C, THRESHOLD_MESSAGE),
    (
        "operation sets not an object",
        _set(("operation_repository_sets",), []),
        C,
        "operation_repository_sets must be an object",
    ),
    (
        "operation",
        _prepend(("operation_repository_sets",), "deploy", "nope"),
        C,
        "operation_repository_sets has an unknown operation: 'deploy'",
    ),
    (
        "operation set",
        _set(("operation_repository_sets", "review-prs"), "other"),
        C,
        "operation_repository_sets.review-prs must name an existing set",
    ),
    ("model_names", _set(("model_names",), None), C, "model_names must be an object"),
    ("archive_root", _set(("archive_root",), "relative"), C, "archive_root must be an absolute drive-letter path"),
    (
        "local_mirror_root",
        _set(("local_mirror_root",), "relative"),
        C,
        "local_mirror_root must be an absolute drive-letter path",
    ),
    ("summary_root", _set(("summary_root",), "relative"), C, "summary_root must be an absolute drive-letter path"),
    (
        "dashboard_file",
        _set(("dashboard_file",), "relative"),
        C,
        "dashboard_file must be an absolute drive-letter path",
    ),
    ("github_login", _set(("github_login",), ""), C, "github_login must be null or a non-empty string"),
    ("dashboard not an object", _set(("dashboard",), []), C, "dashboard must be an object"),
    ("dashboard unknown field", _set(("dashboard", "zzz"), 1), C, "dashboard contains unknown field(s): zzz"),
    ("markers", _set(("dashboard", "start_marker"), ""), C, MARKERS_MESSAGE),
    (
        "status_overrides not an object",
        _set(("dashboard", "status_overrides"), []),
        C,
        "dashboard.status_overrides must be an object",
    ),
    (
        "status override key",
        _prepend(("dashboard", "status_overrides"), "bad", ""),
        C,
        "Invalid dashboard status override key: 'bad'",
    ),
    (
        "status override value",
        _set(("dashboard", "status_overrides", "owner/repo#1"), ""),
        C,
        "Dashboard status override owner/repo#1 must be non-empty",
    ),
    (
        "status override computed",
        _set(("dashboard", "status_overrides", "owner/repo#1"), "drafts"),
        C,
        "Dashboard status override owner/repo#1 duplicates a computed tracker state",
    ),
    ("author_names", _set(("dashboard", "author_names"), None), C, "dashboard.author_names must be an object"),
]

# The same, inside one repository reviewer's entry: each branch's checks, then its checkout.
SKILL_STAGES: list[Case] = [
    (
        "needs skill",
        _delete((*REVIEWER, "skill")),
        C,
        f"{FIELD}.reviewer needs skill (the repository's review skill) or manifest_path",
    ),
    ("skill unsafe", _set((*REVIEWER, "skill"), "../x"), C, f"{FIELD}.reviewer.skill is unsafe"),
    (
        "manifest",
        _set((*REVIEWER, "manifest"), "relative"),
        C,
        f"{FIELD}.reviewer.manifest must be an absolute drive-letter path",
    ),
    (
        "checkout",
        _set((*REPO, "checkout_path"), "relative"),
        C,
        f"{FIELD}.checkout_path must be an absolute drive-letter path",
    ),
    (
        "checkout required",
        _delete((*REPO, "checkout_path")),
        C,
        f"{FIELD}.checkout_path is required for a repository reviewer",
    ),
]
MANIFEST_PATH_STAGES: list[Case] = [
    (
        "manifest_path with a skill",
        _set((*REVIEWER, "skill"), "skills/review"),
        C,
        f"{FIELD}.reviewer sets manifest_path, so it cannot also set skill or manifest",
    ),
    ("manifest_path unsafe", _set((*REVIEWER, "manifest_path"), "/x"), C, f"{FIELD}.reviewer.manifest_path is unsafe"),
    (
        "checkout",
        _set((*REPO, "checkout_path"), "relative"),
        C,
        f"{FIELD}.checkout_path must be an absolute drive-letter path",
    ),
    (
        "checkout required",
        _delete((*REPO, "checkout_path")),
        C,
        f"{FIELD}.checkout_path is required for a repository reviewer",
    ),
]
ORDERED: list[tuple[str, Callable[[], dict[str, Any]], list[Case]]] = [
    ("generic", _config, STAGES),
    ("skill", lambda: REPOSITORY_REVIEWER(_config()), SKILL_STAGES),
    ("manifest_path", lambda: MANIFEST_PATH_REVIEWER(_config()), MANIFEST_PATH_STAGES),
]

# Refusals whose check a stage already orders, with another value or field name in the message.
VALUE_VARIANTS = {
    "Invalid repository-set name: ",
    "Invalid repository identity: ",
    "Unsupported config schema version: ",
    "Missing per-repository configuration for: ",
    "repositories.Owner/Repo ",
    "Unknown runtime host: ",
    EFFORT_MESSAGE,
    "operation_repository_sets has an unknown operation: ",
    "operation_repository_sets.review-insights ",
    "Invalid model_names key ",
    "model_names.m ",
    "Invalid dashboard status override key: ",
    "Invalid dashboard author name login: ",
    "Duplicate dashboard author name login ignoring case: ",
    "Dashboard author name for ",
    "contains unknown field(s): ",
    "must be a non-empty absolute Windows path",
    "must not be a filesystem root",
}


def _canonical(value: Any) -> str:
    """JSON with sorted keys, so true, 1, and 1.0 stay distinct where plain equality would merge them."""
    return json.dumps(value, sort_keys=True)


class ConfigValidationTests(unittest.TestCase):
    def assert_refused(self, config: Any, error: type[Exception], message: str) -> None:
        with self.assertRaises(Exception) as caught:
            validate_config(config)
        self.assertIs(error, type(caught.exception))
        self.assertEqual(message, str(caught.exception))

    def test_accepted_configurations_return_their_exact_result(self) -> None:
        for name, mutation, expected_mutation in ACCEPTED:
            with self.subTest(name):
                config = mutation(_config())
                before = copy.deepcopy(config)
                result = validate_config(config)
                expected = expected_mutation(_expected())
                self.assertEqual(expected, result)
                self.assertEqual(_canonical(expected), _canonical(result))
                self.assertEqual(_canonical(before), _canonical(config))

    def test_full_configuration_result_in_order(self) -> None:
        config = _full_config()
        before = copy.deepcopy(config)
        result = validate_config(config)
        self.assertEqual(json.dumps(_full_expected()), json.dumps(result))
        self.assertEqual(_canonical(before), _canonical(config))

    def test_minimal_configuration_result_in_order(self) -> None:
        self.assertEqual(json.dumps(_expected()), json.dumps(validate_config(_config())))

    def test_given_keys_keep_their_order_and_defaults_follow(self) -> None:
        config = dict(reversed(list(_config().items())))
        self.assertEqual(
            [
                "dashboard_file",
                "summary_root",
                "archive_root",
                "repositories",
                "default_repository_set",
                "repository_sets",
                "schema_version",
                "operation_repository_sets",
                "runtime",
                "reviewer_effort",
                "re_review_scope",
                "model_names",
                "local_mirror_root",
                "verdict_policy",
                "dashboard",
            ],
            list(validate_config(config)),
        )

    def test_which_values_are_the_given_objects(self) -> None:
        config = _full_config()
        result = validate_config(config)
        self.assertIsNot(config, result)
        self.assertIs(config["verdict_policy"]["request_changes_for"], result["verdict_policy"]["request_changes_for"])
        self.assertIs(config["dashboard"]["status_overrides"], result["dashboard"]["status_overrides"])
        self.assertIsNot(config["operation_repository_sets"], result["operation_repository_sets"])
        self.assertIsNot(config["repository_sets"], result["repository_sets"])
        self.assertIsNot(config["repository_sets"]["solo"], result["repository_sets"]["solo"])
        self.assertIsNot(config["re_review_scope"], result["re_review_scope"])
        self.assertIsNot(config["dashboard"], result["dashboard"])
        self.assertIsNot(config["verdict_policy"], result["verdict_policy"])

    def test_each_fault_is_refused_with_its_error(self) -> None:
        for name, mutation, error, message in REJECTED:
            with self.subTest(name):
                self.assert_refused(mutation(_config()), error, message)

    def test_each_fault_is_refused_with_every_section_present(self) -> None:
        # The same faults with every optional section set, so no check depends on the sections being absent.
        for name, mutation, error, message in REJECTED:
            with self.subTest(name):
                self.assert_refused(mutation(_rich_config()), error, message)

    def test_unhashable_values_escape_as_type_errors(self) -> None:
        for name, mutation in UNHASHABLE:
            with self.subTest(name):
                with self.assertRaises(Exception) as caught:
                    validate_config(mutation(_config()))
                self.assertIs(TypeError, type(caught.exception))
                self.assertIn("unhashable type: 'list'", str(caught.exception))

    def test_each_stage_is_refused_alone(self) -> None:
        for label, build, stages in ORDERED:
            for name, mutation, error, message in stages:
                with self.subTest(f"{label}: {name}"):
                    self.assert_refused(mutation(build()), error, message)

    def test_faults_are_detected_in_order(self) -> None:
        # With the fault of every check from k on present at once, check k's fault is the one reported.
        for label, build, stages in ORDERED:
            for index, (name, _mutation, error, message) in enumerate(stages):
                with self.subTest(f"{label}: {name}"):
                    config: Any = build()
                    for _later, mutation, _error, _message in reversed(stages[index:]):
                        config = mutation(config)
                    self.assert_refused(config, error, message)

    def test_an_entry_is_checked_before_the_next(self) -> None:
        config = _config()
        config["repositories"]["owner/repo"]["checkout_path"] = "relative"
        config["repositories"]["owner/later"] = {"reviewer": None}
        self.assert_refused(config, C, f"{FIELD}.checkout_path must be an absolute drive-letter path")
        config = _config()
        config["repositories"] = {"owner/later": {"reviewer": None}, **config["repositories"]}
        config["repositories"]["owner/repo"]["checkout_path"] = "relative"
        self.assert_refused(config, C, "repositories.owner/later.reviewer must be an object")

    def test_a_set_is_checked_before_the_next(self) -> None:
        config = _config()
        config["repository_sets"]["main"] = ["owner/repo", "owner/repo"]
        config["repository_sets"]["-bad"] = ["owner/repo"]
        self.assert_refused(config, C, "Repository set 'main' contains duplicates")

    def test_stages_cover_every_distinct_error(self) -> None:
        staged = {message for _label, _build, stages in ORDERED for _name, _mutation, _error, message in stages}
        refused = {message for _name, _mutation, _error, message in REJECTED}
        unstaged = [
            message for message in sorted(refused - staged) if not any(variant in message for variant in VALUE_VARIANTS)
        ]
        self.assertEqual([], unstaged)


if __name__ == "__main__":
    unittest.main()

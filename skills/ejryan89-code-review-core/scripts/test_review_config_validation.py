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

from review_config import (
    COMPUTED_DASHBOARD_STATES,
    DASHBOARD_AI_REVIEW_STATES,
    ConfigurationError,
    ConfigurationFieldError,
    validate_config,
)

Mutation = Callable[[Any], Any]
Case = tuple[str, Mutation, type[Exception], str]

WINDOWS = sys.platform == "win32"
# How a configured absolute path must be written on this platform, as validate_absolute_path names it.
ABSOLUTE_PATH = "an absolute drive-letter path" if WINDOWS else "an absolute path"


def native(path: str) -> str:
    """A drive-letter fixture path as this platform writes an absolute one: itself on Windows, and elsewhere the same
    path under a folder named for the drive, with forward slashes, so `C:\\reviews` is `/C/reviews`."""
    if WINDOWS:
        return path
    return "/" + path[0] + path[2:].replace("\\", "/")


REPO = ("repositories", "owner/repo")
REVIEWER = (*REPO, "reviewer")
FIELD = "repositories.owner/repo"
GENERIC_MESSAGE = (
    f"{FIELD}.reviewer generic scope must use id 'generic' without manifest_path, skill, manifest, or trusted_ref"
)
MARKERS_MESSAGE = "dashboard markers must be distinct"
SHARE_MESSAGE = "re_review_scope.full_share must be a number above 0 and at most 1"
# The kinds a field of the wrong kind is refused as expecting, written out independently of the reader.
RUNTIME_KIND = 'one of "auto", "claude-code", "codex", "copilot-cli"'
EFFORT_KIND = 'one of "high", "low", "max", "medium", "xhigh" or null'
SEVERITY_KIND = 'one of "MUST_FIX", "SHOULD_FIX", "SUGGESTION"'
POSITIVE = "an integer of at least 1"
LINES_FIELD = "re_review_scope.full_lines"
THRESHOLD_FIELD = "verdict_policy.should_fix_threshold"
MODEL_KEY = "a key of config.model_names"
MODEL_KEY_KIND = "a non-empty trimmed single-line string of at most 200 characters"
# The tracker's own states, which a status override may not repeat; an independent copy of the module's list.
COMPUTED_STATES = [
    "to review",
    "awaiting response",
    "my prs",
    "my pull requests",
    "drafts",
    "missing",
    "current",
    "stale",
    "incomplete",
]


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
        "archive_root": native("C:\\reviews\\archive"),
        "summary_root": native("C:\\reviews\\summaries"),
        "dashboard_file": native("C:\\reviews\\dashboard.md"),
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
                "snapshot_exclude": [],
                "unreviewed_suffixes": [],
                "batch_size": None,
            }
        },
        "archive_root": native("C:\\reviews\\archive"),
        "summary_root": native("C:\\reviews\\summaries"),
        "dashboard_file": native("C:\\reviews\\dashboard.md"),
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
                "checkout_path": native("C:/src/repo/"),
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
                    "manifest": native("C:/manifests/./tools.json"),
                },
                "checkout_path": native("C:\\src\\tools"),
            },
            "owner/committed": {
                "reviewer": {
                    "id": "committed",
                    "protocol_version": 1,
                    "trusted_ref": "refs/heads/main",
                    "manifest_path": "review/manifest.json",
                },
                "checkout_path": native("C:\\src\\committed"),
            },
            "owner/generic": {
                "reviewer": {"id": "generic", "protocol_version": 1, "scope": "generic"},
                "checkout_path": native("D:\\src\\generic"),
            },
        },
        "archive_root": native("C:/reviews/archive/"),
        "local_mirror_root": native("C:\\mirror"),
        "summary_root": native("C:\\reviews\\summaries"),
        "dashboard_file": native("C:\\notes\\..\\reviews\\dashboard.md"),
        "github_login": "octo-cat",
        "runtime": "codex",
        "verdict_policy": {"should_fix_threshold": 2, "request_changes_for": ["MUST_FIX", "SHOULD_FIX"]},
        "dashboard": {
            "author_names": {"octo-cat": " Octo Cat "},
            "status_overrides": {"owner/repo#12": " Waiting on design "},
            "end_marker": "<!-- b -->",
            "start_marker": "<!-- a -->",
        },
        "operation_repository_sets": {"review-prs": "work", "update-pr-dashboard": "solo"},
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
                "checkout_path": native("C:\\src\\repo"),
                "snapshot_exclude": [],
                "unreviewed_suffixes": [],
                "batch_size": None,
            },
            "owner/tools": {
                "reviewer": {
                    "id": "tools",
                    "protocol_version": 1,
                    "trusted_ref": None,
                    "scope": "repository",
                    "manifest_path": None,
                    "skill": "skills/review",
                    "manifest": native("C:\\manifests\\tools.json"),
                },
                "checkout_path": native("C:\\src\\tools"),
                "snapshot_exclude": [],
                "unreviewed_suffixes": [],
                "batch_size": None,
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
                "checkout_path": native("C:\\src\\committed"),
                "snapshot_exclude": [],
                "unreviewed_suffixes": [],
                "batch_size": None,
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
                "checkout_path": native("D:\\src\\generic"),
                "snapshot_exclude": [],
                "unreviewed_suffixes": [],
                "batch_size": None,
            },
        },
        "archive_root": native("C:\\reviews\\archive"),
        "local_mirror_root": native("C:\\mirror"),
        "summary_root": native("C:\\reviews\\summaries"),
        "dashboard_file": native("C:\\reviews\\dashboard.md"),
        "github_login": "octo-cat",
        "runtime": "codex",
        "verdict_policy": {"request_changes_for": ["MUST_FIX", "SHOULD_FIX"], "should_fix_threshold": 2},
        "dashboard": {
            "start_marker": "<!-- a -->",
            "end_marker": "<!-- b -->",
            "status_overrides": {"owner/repo#12": " Waiting on design "},
            "author_names": {"octo-cat": "Octo Cat"},
        },
        "operation_repository_sets": {"review-prs": "work", "update-pr-dashboard": "solo"},
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
        "checkout_path": native("C:\\src\\tools"),
    }
    config["repositories"]["owner/committed"] = {
        "reviewer": {"id": "committed", "protocol_version": 1, "manifest_path": "review/manifest.json"},
        "checkout_path": native("C:\\src\\committed"),
    }
    config["runtime"] = "codex"
    config["reviewer_effort"] = "high"
    config["re_review_scope"] = {"full_share": 0.25, "full_lines": 200}
    config["verdict_policy"] = {"request_changes_for": ["MUST_FIX", "SHOULD_FIX"], "should_fix_threshold": 2}
    config["operation_repository_sets"] = {"update-pr-dashboard": "main"}
    config["model_names"] = {"model-x": "Model X"}
    config["local_mirror_root"] = native("C:\\mirror")
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
    _set((*REPO, "checkout_path"), native("C:\\src\\repo")),
)
# The same reviewer described by a manifest committed to the repository instead.
MANIFEST_PATH_REVIEWER = _chain(
    _set(REVIEWER, {"id": "repo-review", "protocol_version": 1, "manifest_path": "review/manifest.json"}),
    _set((*REPO, "checkout_path"), native("C:\\src\\repo")),
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
    return _set(
        REPO,
        {
            "reviewer": fields,
            "checkout_path": native("C:\\src\\repo"),
            "snapshot_exclude": [],
            "unreviewed_suffixes": [],
            "batch_size": None,
        },
    )


# Snapshot exclusion patterns validate_config accepts as given: a root file, any depth, a whole folder, character
# classes, a pattern as long as allowed, and as many as allowed.
ACCEPTED_PATTERNS: list[tuple[str, list[str]]] = [
    ("a root file and a nested glob", ["*.resx", "**/*.Designer.cs"]),
    ("a whole folder and a class", ["Reports/**", "src/[Gg]enerated/*.xml", "data/?.sql"]),
    ("a pattern of 200 characters", ["a" * 200]),
    ("100 patterns", [f"generated/{index}/**" for index in range(100)]),
    ("dots inside a segment", ["**/.vs/**", "..x/*..cs"]),
]
# Values that are not an array of patterns, and array items that are not a pattern string.
NOT_ARRAYS: list[tuple[str, Any]] = [("null", None), ("a string", "*.resx"), ("an object", {})]
NOT_PATTERNS: list[tuple[str, Any]] = [("null", None), ("a number", 1), ("a list", ["*.resx"])]
TOO_MANY_PATTERNS = [f"generated/{index}/**" for index in range(101)]
# Suffixes of unreviewed file types validate_config accepts as given: one, several parts, the punctuation allowed, one
# the suite already ships, one as long as allowed, and as many as allowed.
ACCEPTED_SUFFIXES: list[tuple[str, list[str]]] = [
    ("one", [".nupkg"]),
    ("of several parts", [".tar.lz", ".bin"]),
    ("with the punctuation allowed", [".c++", ".my_pkg", ".x-y", ".7z"]),
    ("already shipped", [".zip"]),
    ("of 32 characters", ["." + "a" * 31]),
    ("100 suffixes", [f".x{index}" for index in range(100)]),
]
TOO_MANY_SUFFIXES = [f".x{index}" for index in range(101)]
SUFFIX_MESSAGE = (
    f"config.{FIELD}.unreviewed_suffixes[0] must be a lowercase file-name suffix: a dot, then letters, digits, '_', "
    "'+', or '-', in parts each starting with a letter or digit and joined by dots, found "
)
PATTERN_MESSAGE = (
    f"config.{FIELD}.snapshot_exclude[0] must be a repository-relative glob pattern without a backslash, a control "
    "character, or an empty, '.', or '..' segment, found "
)


# Configurations validate_config accepts: each mutates _config(), and the expected result is _expected() mutated alike.
ACCEPTED: list[tuple[str, Mutation, Mutation]] = [
    ("minimal configuration", _chain(), _chain()),
    *[
        (
            f"snapshot_exclude {name}",
            _set((*REPO, "snapshot_exclude"), patterns),
            _set((*REPO, "snapshot_exclude"), patterns),
        )
        for name, patterns in ACCEPTED_PATTERNS
    ],
    *[
        (
            f"unreviewed_suffixes {name}",
            _set((*REPO, "unreviewed_suffixes"), suffixes),
            _set((*REPO, "unreviewed_suffixes"), suffixes),
        )
        for name, suffixes in ACCEPTED_SUFFIXES
    ],
    (
        "every optional section at its default",
        _chain(
            _set((*REPO, "snapshot_exclude"), []),
            _set((*REPO, "unreviewed_suffixes"), []),
            _set((*REPO, "batch_size"), None),
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
        (f"batch_size {size}", _set((*REPO, "batch_size"), size), _set((*REPO, "batch_size"), size))
        for size in [1, 25, 10_000]
    ],
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
        _set((*REPO, "checkout_path"), native("C:/src/repo/")),
        _set((*REPO, "checkout_path"), native("C:\\src\\repo")),
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
        _chain(REPOSITORY_REVIEWER, _set((*REVIEWER, "manifest"), native("c:/manifests/../kept/m.json"))),
        _repository_expected(manifest=native("c:\\kept\\m.json")),
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
            {"review-prs": "main", "update-pr-dashboard": "main", "review-insights": "main"},
        ),
        _set(
            ("operation_repository_sets",),
            {"review-prs": "main", "update-pr-dashboard": "main", "review-insights": "main"},
        ),
    ),
    (
        "paths normalized",
        _chain(
            _set(("archive_root",), native("C:/a/../b")),
            _set(("summary_root",), native("d:\\x\\.\\y\\")),
            _set(("dashboard_file",), native("C:\\r\\\\d.md")),
            _set(("local_mirror_root",), native("E:/m")),
        ),
        _chain(
            _set(("archive_root",), native("C:\\b")),
            _set(("summary_root",), native("d:\\x\\y")),
            _set(("dashboard_file",), native("C:\\r\\d.md")),
            _set(("local_mirror_root",), native("E:\\m")),
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
            _set(("dashboard", "author_names"), {"a": " A ", "B-" + "c" * 37: "Bc", "mona_acme": " Mona "}),
        ),
        _chain(
            _set(("model_names",), {"m": "M", "x" * 200: "y" * 100}),
            _set(("dashboard", "author_names"), {"a": "A", "B-" + "c" * 37: "Bc", "mona_acme": "Mona"}),
        ),
    ),
]

C = ConfigurationError
F = ConfigurationFieldError
# Severity lists whose last item is not a severity.
SEVERITY_LISTS: list[list[Any]] = [["BLOCKER"], ["MUST_FIX", "must_fix"], [1], [None], ["MUST_FIX "]]
# Null and an empty list, typed so the mixed value lists built from them below type-check.
NULL_OR_EMPTY_LIST: list[Any] = [None, []]
# What a field error names as found, for each wrong value the cases below use, written out independently of the reader.
FOUND: dict[str, str] = {
    "None": "null",
    "True": "boolean true",
    "False": "boolean false",
    "0": "integer 0",
    "1": "integer 1",
    "-1": "integer -1",
    "2": "integer 2",
    "1.0": "number 1.0",
    "1.5": "number 1.5",
    "2.0": "number 2.0",
    "10.5": "number 10.5",
    "nan": "number nan",
    "inf": "number inf",
    "[]": "an array",
    "{}": "an object",
}


def _found(value: Any, *, text: bool = False) -> str:
    """What a field error names as found: a listed literal, a quoted short string, or an array or object; where a
    string is expected (`text`), an empty or blank string is named as such."""
    if text and isinstance(value, str) and not value.strip():
        return "a blank string" if value else "an empty string"
    if repr(value) in FOUND:
        return FOUND[repr(value)]
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "an object"
    return f"string {json.dumps(value)}"


def _wrong(path: str, expected: str, value: Any) -> str:
    """The one shape a field of the wrong kind is refused with, written out independently of the reader."""
    return f"config.{path} must be {expected}, found {_found(value, text='string' in expected)}"


def _unknown(path: str, *fields: str) -> str:
    names = ", ".join(f'"{field}"' for field in fields)
    where = f"config.{path}" if path else "config"
    return f"{where} must be an object of known fields, found unknown field{'s' if len(fields) > 1 else ''} {names}"


def _path_faults(field: str, path: tuple[str, ...], *, nullable: bool = False) -> list[Case]:
    """Every outcome of validate_absolute_path for one field."""
    kind = "a non-empty string or null" if nullable else "a non-empty string"
    absolute = f"{field} must be {ABSOLUTE_PATH}"
    root = f"{field} must not be a filesystem root"
    faults: list[Case] = [
        (f"{field} empty", _set(path, ""), F, _wrong(field, kind, "")),
        (f"{field} not a string", _set(path, 1), F, _wrong(field, kind, 1)),
        (f"{field} false", _set(path, False), F, _wrong(field, kind, False)),
        (f"{field} relative", _set(path, "reviews"), C, absolute),
        # A drive-relative path is relative on every platform.
        (f"{field} drive-relative", _set(path, "C:reviews"), C, absolute),
    ]
    if WINDOWS:
        faults += [
            (f"{field} rooted without a drive", _set(path, "\\reviews"), C, absolute),
            (f"{field} UNC", _set(path, "\\\\server\\share\\reviews"), C, absolute),
            (f"{field} extended-length", _set(path, "\\\\?\\C:\\reviews"), C, absolute),
            (f"{field} root", _set(path, "C:\\"), C, root),
            (f"{field} root with a slash", _set(path, "C:/"), C, root),
            (f"{field} root after normalization", _set(path, "C:\\reviews\\.."), C, root),
        ]
    else:
        faults += [
            # Off Windows a backslash is a character of a name and a drive-letter path a relative one.
            (f"{field} with backslashes", _set(path, "\\reviews"), C, absolute),
            (f"{field} drive-letter", _set(path, "C:/reviews"), C, absolute),
            (f"{field} two leading slashes", _set(path, "//server/share/reviews"), C, absolute),
            (f"{field} root", _set(path, "/"), C, root),
            (f"{field} root after normalization", _set(path, "/reviews/.."), C, root),
        ]
    if not nullable:
        faults.append((f"{field} null", _set(path, None), F, _wrong(field, kind, None)))
    return faults


def _marker_faults(key: str) -> list[Case]:
    path = ("dashboard", key)
    field = f"dashboard.{key}"
    kind = "a non-empty trimmed single-line string"
    return [
        (f"{key} not a string", _set(path, 1), F, _wrong(field, kind, 1)),
        (f"{key} null", _set(path, None), F, _wrong(field, kind, None)),
        (f"{key} empty", _set(path, ""), F, _wrong(field, kind, "")),
        (f"{key} blank", _set(path, "  "), F, _wrong(field, kind, "  ")),
        (
            f"{key} untrimmed",
            _set(path, " <a>"),
            F,
            f"config.{field} must be {kind}, found a string with whitespace around it",
        ),
        (
            f"{key} untrimmed newline",
            _set(path, "<a>\n"),
            F,
            f"config.{field} must be {kind}, found a string of more than one line",
        ),
        (
            f"{key} inner newline",
            _set(path, "<a\nb>"),
            F,
            f"config.{field} must be {kind}, found a string of more than one line",
        ),
        (
            f"{key} inner carriage return",
            _set(path, "<a\rb>"),
            F,
            f"config.{field} must be {kind}, found a string of more than one line",
        ),
    ]


def _unsafe_paths(key: str, base: Mutation, message: str) -> list[Case]:
    field = f"{FIELD}.reviewer.{key}"
    return [
        *[
            (
                f"{key} {label}",
                _chain(base, _set((*REVIEWER, key), value)),
                F,
                _wrong(field, "a non-empty string or null", value),
            )
            for label, value in [("empty", ""), ("not a string", 1), ("false", False)]
        ],
        *[
            (f"{key} {label}", _chain(base, _set((*REVIEWER, key), value)), C, message)
            for label, value in [
                ("backslash", "skills\\review"),
                ("absolute", "/skills/review"),
                ("parent", "skills/../review"),
                ("only parent", ".."),
                ("trailing parent", "skills/.."),
            ]
        ],
    ]


REJECTED: list[Case] = [
    *(
        (
            f"schema_version {value!r}",
            _set(("schema_version",), value),
            F,
            _wrong("schema_version", "an integer", value),
        )
        for value in (True, 1.0)
    ),
    *(
        (
            f"protocol_version {value!r}",
            _chain(REPOSITORY_REVIEWER, _set((*REVIEWER, "protocol_version"), value)),
            F,
            _wrong(f"{FIELD}.reviewer.protocol_version", "an integer", value),
        )
        for value in (True, 1.0)
    ),
    *(
        (
            f"drive-letter {field} {path}",
            _chain(base, _set((*REVIEWER, field), path)),
            C,
            f"{FIELD}.reviewer.{field} is unsafe",
        )
        for field, base in (("skill", REPOSITORY_REVIEWER), ("manifest_path", MANIFEST_PATH_REVIEWER))
        for path in ("C:/review/manifest.json", "c:/review/manifest.json", "C:manifest.json", "C:\\manifest.json")
    ),
    ("config a list", _replace([]), F, "config must be an object, found an array"),
    ("config null", _replace(None), F, "config must be an object, found null"),
    ("unknown top-level field", _set(("extra",), 1), F, _unknown("", "extra")),
    (
        "unknown top-level fields sorted",
        _chain(_set(("zeta",), 1), _set(("alpha",), 1)),
        F,
        _unknown("", "alpha", "zeta"),
    ),
    ("schema_version missing", _delete(("schema_version",)), F, _wrong("schema_version", "an integer", None)),
    ("schema_version 0", _set(("schema_version",), 0), C, "Unsupported config schema version: 0"),
    *[
        (
            f"schema_version {value!r}",
            _set(("schema_version",), value),
            F,
            _wrong("schema_version", "an integer", value),
        )
        for value in ("1", False, 1.5, 2.0)
    ],
    ("schema_version 2", _set(("schema_version",), 2), C, "Unsupported future config schema version: 2"),
    # repository_sets
    (
        "repository_sets missing",
        _delete(("repository_sets",)),
        F,
        "config.repository_sets must be an object, found null",
    ),
    (
        "repository_sets a list",
        _set(("repository_sets",), ["owner/repo"]),
        F,
        "config.repository_sets must be an object, found an array",
    ),
    ("repository_sets empty", _set(("repository_sets",), {}), C, "repository_sets must not be empty"),
    *[
        (
            f"set name {name!r}",
            _prepend(("repository_sets",), name, ["owner/repo"]),
            C,
            f"Invalid repository-set name: {name!r}",
        )
        for name in ["-main", "_main", ".main", "main set", "main\n", "mäin", "main/x"]
    ],
    (
        "set name ''",
        _prepend(("repository_sets",), "", ["owner/repo"]),
        F,
        "a key of config.repository_sets must be a non-empty string, found an empty string",
    ),
    (
        "set name not a string",
        _prepend(("repository_sets",), 1, ["owner/repo"]),
        F,
        "a key of config.repository_sets must be a non-empty string, found integer 1",
    ),
    *[
        (
            f"set members {label}",
            _set(("repository_sets", "main"), value),
            F,
            _wrong("repository_sets.main", "a non-empty array", value),
        )
        for label, value in [("null", None), ("a string", "owner/repo"), ("an object", {"owner/repo": 1})]
    ],
    (
        "set members empty",
        _set(("repository_sets", "main"), []),
        F,
        "config.repository_sets.main must be a non-empty array, found an empty array",
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
        ]
    ],
    *[
        (
            f"set member {member!r}",
            _set(("repository_sets", "main"), ["owner/repo", member]),
            F,
            _wrong("repository_sets.main[1]", "a non-empty string", member),
        )
        for member in ["", 1, None]
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
        for value in ["other", "Main"]
    ],
    *[
        (
            f"default set {value!r}",
            _set(("default_repository_set",), value),
            F,
            _wrong("default_repository_set", "a non-empty string", value),
        )
        for value in ["", 1, ["main"], None]
    ],
    (
        "default set missing",
        _delete(("default_repository_set",)),
        F,
        "config.default_repository_set must be a non-empty string, found null",
    ),
    # repositories
    ("repositories null", _set(("repositories",), None), F, "config.repositories must be an object, found null"),
    ("repositories a list", _set(("repositories",), []), F, "config.repositories must be an object, found an array"),
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
    ("entry null", _set(REPO, None), F, f"config.{FIELD} must be an object, found null"),
    ("entry a list", _set(REPO, []), F, f"config.{FIELD} must be an object, found an array"),
    (
        "entry named as written",
        _chain(_delete(REPO), _set(("repositories", "Owner/Repo"), None)),
        F,
        "config.repositories.Owner/Repo must be an object, found null",
    ),
    ("entry unknown field", _set((*REPO, "extra"), 1), F, _unknown(FIELD, "extra")),
    ("reviewer missing", _delete(REVIEWER), F, f"config.{FIELD}.reviewer must be an object, found null"),
    (
        "reviewer a string",
        _set(REVIEWER, "generic"),
        F,
        f'config.{FIELD}.reviewer must be an object, found string "generic"',
    ),
    ("reviewer unknown field", _set((*REVIEWER, "model"), "x"), F, _unknown(f"{FIELD}.reviewer", "model")),
    (
        "reviewer id missing",
        _delete((*REVIEWER, "id")),
        F,
        f"config.{FIELD}.reviewer.id must be a non-empty string, found null",
    ),
    *[
        (f"reviewer id {value!r}", _set((*REVIEWER, "id"), value), C, f"{FIELD}.reviewer.id is invalid")
        for value in ["Generic", "-x", "a_b", "a.b", "a" * 65, "a\n"]
    ],
    *[
        (
            f"reviewer id {value!r}",
            _set((*REVIEWER, "id"), value),
            F,
            _wrong(f"{FIELD}.reviewer.id", "a non-empty string", value),
        )
        for value in ["", 1, None]
    ],
    (
        "protocol missing",
        _delete((*REVIEWER, "protocol_version")),
        F,
        f"config.{FIELD}.reviewer.protocol_version must be an integer, found null",
    ),
    *[
        (
            f"protocol {value!r}",
            _set((*REVIEWER, "protocol_version"), value),
            C,
            f"{FIELD}.reviewer protocol is unsupported",
        )
        for value in [2, 0]
    ],
    *[
        (
            f"protocol {value!r}",
            _set((*REVIEWER, "protocol_version"), value),
            F,
            _wrong(f"{FIELD}.reviewer.protocol_version", "an integer", value),
        )
        for value in ["1", False, 1.5]
    ],
    *[
        (
            f"trusted_ref {value!r}",
            _set((*REVIEWER, "trusted_ref"), value),
            F,
            _wrong(f"{FIELD}.reviewer.trusted_ref", "a non-empty string or null", value),
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
        (
            f"scope {value!r}",
            _set((*REVIEWER, "scope"), value),
            F,
            _wrong(f"{FIELD}.reviewer.scope", 'one of "generic", "repository"', value),
        )
        for value in ["other", "Generic", "", None, 1]
    ],
    ("generic scope with another id", _set((*REVIEWER, "id"), "repo-review"), C, GENERIC_MESSAGE),
    *[
        (f"generic scope with {key} {value!r}", _set((*REVIEWER, key), value), C, GENERIC_MESSAGE)
        for key, value in [
            ("manifest_path", "review/manifest.json"),
            ("skill", "skills/review"),
            ("manifest", True),
            ("manifest", "C:\\manifest.json"),
            ("trusted_ref", "main"),
        ]
    ],
    (
        "generic scope with manifest False",
        _set((*REVIEWER, "manifest"), False),
        F,
        f"config.{FIELD}.reviewer.manifest must be true, a non-empty string, or null, found boolean false",
    ),
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
        for value in [True, "C:\\manifest.json"]
    ],
    (
        "manifest_path with manifest False",
        _chain(MANIFEST_PATH_REVIEWER, _set((*REVIEWER, "manifest"), False)),
        F,
        f"config.{FIELD}.reviewer.manifest must be true, a non-empty string, or null, found boolean false",
    ),
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
        (
            f"repository {name}",
            _chain(REPOSITORY_REVIEWER, mutation),
            error,
            message.replace("a non-empty string or null", "true, a non-empty string, or null"),
        )
        for name, mutation, error, message in _path_faults(
            f"{FIELD}.reviewer.manifest", (*REVIEWER, "manifest"), nullable=True
        )
    ],
    (
        "reviewer.manifest the string true",
        _chain(REPOSITORY_REVIEWER, _set((*REVIEWER, "manifest"), "true")),
        C,
        f"{FIELD}.reviewer.manifest must be {ABSOLUTE_PATH}",
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
        (
            f"snapshot_exclude {name}",
            _set((*REPO, "snapshot_exclude"), value),
            F,
            _wrong(f"{FIELD}.snapshot_exclude", "an array of at most 100 items", value),
        )
        for name, value in NOT_ARRAYS
    ],
    (
        "snapshot_exclude of 101 patterns",
        _set((*REPO, "snapshot_exclude"), TOO_MANY_PATTERNS),
        F,
        f"config.{FIELD}.snapshot_exclude must be an array of at most 100 items, found an array of 101 items",
    ),
    *[
        (
            f"snapshot_exclude pattern {name}",
            _set((*REPO, "snapshot_exclude"), [pattern]),
            F,
            f"{PATTERN_MESSAGE}{found}",
        )
        for name, pattern, found in [
            ("with a backslash", "src\\*.resx", 'string "src\\\\*.resx"'),
            ("absolute", "/src/*.resx", 'string "/src/*.resx"'),
            ("drive absolute", "C:/src/*.resx", 'string "C:/src/*.resx"'),
            ("lowercase drive absolute", "c:/src/*.resx", 'string "c:/src/*.resx"'),
            ("drive relative", "C:*.resx", 'string "C:*.resx"'),
            ("ending in a slash", "Reports/", 'string "Reports/"'),
            ("with an empty segment", "src//*.resx", 'string "src//*.resx"'),
            ("with a parent segment", "../*.resx", 'string "../*.resx"'),
            ("with a parent segment inside", "src/../*.resx", 'string "src/../*.resx"'),
            ("with a current segment", "./*.resx", 'string "./*.resx"'),
            ("with a control character", "src/*.resx\n", 'string "src/*.resx\\n"'),
            ("with a delete character", "src/\x7f.resx", 'string "src/\\u007f.resx"'),
        ]
    ],
    *[
        (
            f"snapshot_exclude pattern {name}",
            _set((*REPO, "snapshot_exclude"), [value]),
            F,
            f"config.{FIELD}.snapshot_exclude[0] must be a non-empty string of at most 200 characters, found {found}",
        )
        for name, value, found in [
            *((name, value, _found(value)) for name, value in NOT_PATTERNS),
            ("empty", "", "an empty string"),
            ("of 201 characters", "a" * 201, "a string of 201 characters"),
        ]
    ],
    *[
        (
            f"unreviewed_suffixes {name}",
            _set((*REPO, "unreviewed_suffixes"), value),
            F,
            _wrong(f"{FIELD}.unreviewed_suffixes", "an array of at most 100 items", value),
        )
        for name, value in NOT_ARRAYS
    ],
    (
        "unreviewed_suffixes of 101 suffixes",
        _set((*REPO, "unreviewed_suffixes"), TOO_MANY_SUFFIXES),
        F,
        f"config.{FIELD}.unreviewed_suffixes must be an array of at most 100 items, found an array of 101 items",
    ),
    *[
        (
            f"unreviewed_suffixes suffix {name}",
            _set((*REPO, "unreviewed_suffixes"), [value]),
            F,
            f"config.{FIELD}.unreviewed_suffixes[0] must be a non-empty string of at most 32 characters, found {found}",
        )
        for name, value, found in [
            ("null", None, "null"),
            ("a number", 1, "integer 1"),
            ("a list", [".zip"], "an array"),
            ("empty", "", "an empty string"),
            ("of 33 characters", "." + "a" * 32, "a string of 33 characters"),
        ]
    ],
    *[
        (
            f"unreviewed_suffixes suffix {name}",
            _set((*REPO, "unreviewed_suffixes"), [suffix]),
            F,
            SUFFIX_MESSAGE + found,
        )
        for name, suffix, found in [
            ("without a dot", "zip", 'string "zip"'),
            ("in upper case", ".ZIP", 'string ".ZIP"'),
            ("in mixed case", ".Nupkg", 'string ".Nupkg"'),
            ("a dot alone", ".", 'string "."'),
            ("two dots", "..", 'string ".."'),
            ("with an empty part", ".tar..gz", 'string ".tar..gz"'),
            ("ending in a dot", ".zip.", 'string ".zip."'),
            ("a glob", "*.zip", 'string "*.zip"'),
            ("with a name before it", "setup.msi", 'string "setup.msi"'),
            ("with a slash", ".a/b", 'string ".a/b"'),
            ("with a space", ".a b", 'string ".a b"'),
            ("with a part starting in punctuation", ".-x", 'string ".-x"'),
            ("with a letter outside ASCII", ".zi\u00df", 'string ".zi\\u00df"'),
            ("with a control character", ".zip\n", 'string ".zip\\n"'),
        ]
    ],
    (
        "unreviewed_suffixes listing a suffix twice",
        _set((*REPO, "unreviewed_suffixes"), [".bin", ".dat", ".bin"]),
        F,
        f'config.{FIELD}.unreviewed_suffixes must be distinct file-name suffixes, found ".bin" twice',
    ),
    *[
        (
            f"batch_size {value!r}",
            _set((*REPO, "batch_size"), value),
            F,
            _wrong(f"{FIELD}.batch_size", "an integer of at least 1 or null", value),
        )
        for value in [0, -1, True, False, 1.0, 10.5, "25", [25], {}]
    ],
    (
        "snapshot_exclude listing a pattern twice ignoring case",
        _set((*REPO, "snapshot_exclude"), ["**/*.resx", "Reports/**", "**/*.RESX"]),
        F,
        f'config.{FIELD}.snapshot_exclude must be distinct glob patterns, ignoring case, found "**/*.RESX" twice',
    ),
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
        (f"runtime {value!r}", _set(("runtime",), value), F, _wrong("runtime", RUNTIME_KIND, value))
        for value in ["bash", "Auto", "", None, 1]
    ],
    *[
        (
            f"reviewer_effort {value!r}",
            _set(("reviewer_effort",), value),
            F,
            _wrong("reviewer_effort", EFFORT_KIND, value),
        )
        for value in ["extreme", "High", "", 1, False]
    ],
    # re_review_scope
    (
        "re_review_scope null",
        _set(("re_review_scope",), None),
        F,
        "config.re_review_scope must be an object, found null",
    ),
    (
        "re_review_scope a list",
        _set(("re_review_scope",), []),
        F,
        "config.re_review_scope must be an object, found an array",
    ),
    (
        "re_review_scope unknown field",
        _set(("re_review_scope", "full_files"), 1),
        F,
        _unknown("re_review_scope", "full_files"),
    ),
    *[
        (f"full_share {value!r}", _set(("re_review_scope", "full_share"), value), C, SHARE_MESSAGE)
        for value in [0, 0.0, -0.5, 1.0000001, 2]
    ],
    *[
        (
            f"full_share {value!r}",
            _set(("re_review_scope", "full_share"), value),
            F,
            _wrong("re_review_scope.full_share", "a finite number", value),
        )
        for value in [True, False, "0.5", None, float("nan"), float("inf")]
    ],
    *[
        (
            f"full_lines {value!r}",
            _set(("re_review_scope", "full_lines"), value),
            F,
            _wrong(LINES_FIELD, POSITIVE, value),
        )
        for value in [0, -1, True, 1.0, 10.5, "10", None]
    ],
    # verdict_policy
    ("verdict_policy null", _set(("verdict_policy",), None), F, "config.verdict_policy must be an object, found null"),
    (
        "verdict_policy a list",
        _set(("verdict_policy",), []),
        F,
        "config.verdict_policy must be an object, found an array",
    ),
    (
        "verdict_policy unknown field",
        _set(("verdict_policy", "block_on"), []),
        F,
        _unknown("verdict_policy", "block_on"),
    ),
    *[
        (
            f"request_changes_for {value!r}",
            _set(("verdict_policy", "request_changes_for"), value),
            F,
            _wrong("verdict_policy.request_changes_for", "a non-empty array", value),
        )
        for value in [None, "MUST_FIX", {"MUST_FIX": 1}]
    ],
    (
        "request_changes_for []",
        _set(("verdict_policy", "request_changes_for"), []),
        F,
        "config.verdict_policy.request_changes_for must be a non-empty array, found an empty array",
    ),
    *[
        (
            f"request_changes_for {value!r}",
            _set(("verdict_policy", "request_changes_for"), value),
            F,
            _wrong(f"verdict_policy.request_changes_for[{len(value) - 1}]", SEVERITY_KIND, value[-1]),
        )
        for value in SEVERITY_LISTS
    ],
    *[
        (
            f"should_fix_threshold {value!r}",
            _set(("verdict_policy", "should_fix_threshold"), value),
            F,
            _wrong(THRESHOLD_FIELD, POSITIVE, value),
        )
        for value in [0, -1, True, 1.0, "3", None]
    ],
    # operation_repository_sets
    *[
        (
            f"operation sets {value!r}",
            _set(("operation_repository_sets",), value),
            F,
            _wrong("operation_repository_sets", "an object", value),
        )
        for value in [*NULL_OR_EMPTY_LIST, "main"]
    ],
    *[
        (
            f"operation {value!r}",
            _set(("operation_repository_sets", value), "main"),
            F,
            _unknown("operation_repository_sets", value),
        )
        for value in ["deploy", "Review-PRs", ""]
    ],
    (
        "operation not a string",
        _prepend(("operation_repository_sets",), 1, "main"),
        F,
        _unknown("operation_repository_sets", "1"),
    ),
    (
        "operation a skill rename retired",
        _set(("operation_repository_sets", "update-pr-tracker"), "main"),
        C,
        "operation_repository_sets.update-pr-tracker names an operation renamed update-pr-dashboard; rename the key "
        "to update-pr-dashboard",
    ),
    (
        "unknown operation with an unknown set",
        _set(("operation_repository_sets", "deploy"), "other"),
        F,
        _unknown("operation_repository_sets", "deploy"),
    ),
    *[
        (
            f"operation set {value!r}",
            _set(("operation_repository_sets", "review-insights"), value),
            C,
            "operation_repository_sets.review-insights must name an existing set",
        )
        for value in ["other", "Main"]
    ],
    *[
        (
            f"operation set {value!r}",
            _set(("operation_repository_sets", "review-insights"), value),
            F,
            _wrong("operation_repository_sets.review-insights", "a non-empty string", value),
        )
        for value in [None, 1]
    ],
    # model_names, reached through normalize_model_names
    ("model_names null", _set(("model_names",), None), F, "config.model_names must be an object, found null"),
    (
        "model_names key untrimmed",
        _set(("model_names",), {" m": "M"}),
        F,
        f"{MODEL_KEY} must be {MODEL_KEY_KIND}, found a string with whitespace around it",
    ),
    (
        "model_names key too long",
        _set(("model_names",), {"x" * 201: "M"}),
        F,
        f"{MODEL_KEY} must be {MODEL_KEY_KIND}, found a string of 201 characters",
    ),
    (
        "model_names name blank",
        _set(("model_names",), {"m": " "}),
        F,
        "config.model_names.m must be a non-empty single-line string, found a blank string",
    ),
    (
        "model_names name too long",
        _set(("model_names",), {"m": "y" * 101}),
        C,
        "model_names.m must be a name of at most 100 characters",
    ),
    # the four paths
    *_path_faults("archive_root", ("archive_root",)),
    (
        "archive_root missing",
        _delete(("archive_root",)),
        F,
        "config.archive_root must be a non-empty string, found null",
    ),
    *_path_faults("local_mirror_root", ("local_mirror_root",), nullable=True),
    *_path_faults("summary_root", ("summary_root",)),
    (
        "summary_root missing",
        _delete(("summary_root",)),
        F,
        "config.summary_root must be a non-empty string, found null",
    ),
    *_path_faults("dashboard_file", ("dashboard_file",)),
    (
        "dashboard_file missing",
        _delete(("dashboard_file",)),
        F,
        "config.dashboard_file must be a non-empty string, found null",
    ),
    # github_login
    *[
        (
            f"github_login {value!r}",
            _set(("github_login",), value),
            F,
            _wrong("github_login", "a non-empty string or null", value),
        )
        for value in ["", "  ", "\n", 1, False, ["octo"]]
    ],
    # dashboard
    ("dashboard null", _set(("dashboard",), None), F, "config.dashboard must be an object, found null"),
    ("dashboard a list", _set(("dashboard",), []), F, "config.dashboard must be an object, found an array"),
    ("dashboard unknown field", _set(("dashboard", "title"), "x"), F, _unknown("dashboard", "title")),
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
            F,
            _wrong("dashboard.status_overrides", "an object", value),
        )
        for value in [*NULL_OR_EMPTY_LIST, "x"]
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
        F,
        "a key of config.dashboard.status_overrides must be a non-empty string, found integer 1",
    ),
    *[
        (
            f"status override value {value!r}",
            _set(("dashboard", "status_overrides", "owner/repo#1"), value),
            F,
            _wrong("dashboard.status_overrides.owner/repo#1", "a non-empty string", value),
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
        for value in [
            *COMPUTED_STATES,
            " To Review ",
            "DRAFTS",
            "My PRs",
            "Awaiting Response",
            " Incomplete ",
            "INCOMPLETE",
        ]
    ],
    (
        "author_names null",
        _set(("dashboard", "author_names"), None),
        F,
        "config.dashboard.author_names must be an object, found null",
    ),
    (
        "author_names login invalid",
        _set(("dashboard", "author_names"), {"-octo": "Octo"}),
        C,
        "Invalid dashboard author name login: '-octo'",
    ),
    (
        "author_names login with a doubled hyphen",
        _set(("dashboard", "author_names"), {"octo--cat": "Octo"}),
        C,
        "Invalid dashboard author name login: 'octo--cat'",
    ),
    (
        "author_names login longer than GitHub allows",
        _set(("dashboard", "author_names"), {"B-" + "c" * 38: "Octo"}),
        C,
        "Invalid dashboard author name login: 'B-" + "c" * 38 + "'",
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
        F,
        "config.dashboard.author_names.octo must be a non-empty single-line string, found a blank string",
    ),
]

# A list or an object where a scalar is expected is refused with the field's error, never a TypeError.
CONTAINER_CASES: list[Case] = [
    (
        f"{'.'.join(path)} {value!r}",
        _set(path, [value] if path[-1] == "request_changes_for" else value),
        F,
        _wrong(field, kind, value),
    )
    for path, member, field, kind in [
        (("schema_version",), 1, "schema_version", "an integer"),
        ((*REVIEWER, "protocol_version"), 1, f"{FIELD}.reviewer.protocol_version", "an integer"),
        ((*REVIEWER, "scope"), "generic", f"{FIELD}.reviewer.scope", 'one of "generic", "repository"'),
        (("runtime",), "auto", "runtime", RUNTIME_KIND),
        (("reviewer_effort",), "high", "reviewer_effort", EFFORT_KIND),
        (("verdict_policy", "request_changes_for"), "MUST_FIX", "verdict_policy.request_changes_for[0]", SEVERITY_KIND),
        (
            ("operation_repository_sets", "review-prs"),
            "main",
            "operation_repository_sets.review-prs",
            "a non-empty string",
        ),
    ]
    for value in ([], {}, [member], {"value": member})
]
REJECTED.extend(CONTAINER_CASES)

# One fault per check, in the order validate_config detects them, on the generic reviewer. For each k, every fault from
# k on is applied at once, and check k's error must be the one raised.
STAGES: list[Case] = [
    ("config not an object", _replace([]), F, "config must be an object, found an array"),
    ("unknown top-level field", _set(("zzz",), 1), F, _unknown("", "zzz")),
    (
        "schema_version a string",
        _set(("schema_version",), "1"),
        F,
        'config.schema_version must be an integer, found string "1"',
    ),
    ("future schema_version", _set(("schema_version",), 2), C, "Unsupported future config schema version: 2"),
    ("schema_version", _set(("schema_version",), 0), C, "Unsupported config schema version: 0"),
    (
        "repository_sets not an object",
        _set(("repository_sets",), []),
        F,
        "config.repository_sets must be an object, found an array",
    ),
    ("repository_sets empty", _set(("repository_sets",), {}), C, "repository_sets must not be empty"),
    (
        "set name not a string",
        _prepend(("repository_sets",), 1, []),
        F,
        "a key of config.repository_sets must be a non-empty string, found integer 1",
    ),
    ("set name", _prepend(("repository_sets",), "-bad", []), C, "Invalid repository-set name: '-bad'"),
    (
        "set members empty",
        _set(("repository_sets", "main"), []),
        F,
        "config.repository_sets.main must be a non-empty array, found an empty array",
    ),
    (
        "set member not a string",
        _set(("repository_sets", "main"), [1, "bad"]),
        F,
        "config.repository_sets.main[0] must be a non-empty string, found integer 1",
    ),
    ("set member", _set(("repository_sets", "main"), ["bad", "bad"]), C, "Invalid repository identity: 'bad'"),
    (
        "set duplicates",
        _set(("repository_sets", "main"), ["owner/repo", "Owner/Repo"]),
        C,
        "Repository set 'main' contains duplicates",
    ),
    (
        "default set",
        _set(("default_repository_set",), "other"),
        C,
        "default_repository_set must name an existing set",
    ),
    (
        "default set not a string",
        _set(("default_repository_set",), 1),
        F,
        "config.default_repository_set must be a non-empty string, found integer 1",
    ),
    (
        "repositories not an object",
        _set(("repositories",), []),
        F,
        "config.repositories must be an object, found an array",
    ),
    ("repository key", _prepend(("repositories",), "nope", None), C, "Invalid repository identity: 'nope'"),
    (
        "repository key duplicated",
        _prepend(("repositories",), "Owner/Repo", _generic_entry()),
        C,
        "Duplicate repository configuration after normalization: owner/repo",
    ),
    ("entry not an object", _set(REPO, []), F, f"config.{FIELD} must be an object, found an array"),
    ("entry unknown field", _set((*REPO, "zzz"), 1), F, _unknown(FIELD, "zzz")),
    ("reviewer not an object", _set(REVIEWER, []), F, f"config.{FIELD}.reviewer must be an object, found an array"),
    ("reviewer unknown field", _set((*REVIEWER, "zzz"), 1), F, _unknown(f"{FIELD}.reviewer", "zzz")),
    (
        "reviewer id not a string",
        _set((*REVIEWER, "id"), 1),
        F,
        f"config.{FIELD}.reviewer.id must be a non-empty string, found integer 1",
    ),
    ("reviewer id", _set((*REVIEWER, "id"), "Bad"), C, f"{FIELD}.reviewer.id is invalid"),
    (
        "protocol not an integer",
        _set((*REVIEWER, "protocol_version"), "1"),
        F,
        f'config.{FIELD}.reviewer.protocol_version must be an integer, found string "1"',
    ),
    ("protocol", _set((*REVIEWER, "protocol_version"), 2), C, f"{FIELD}.reviewer protocol is unsupported"),
    (
        "trusted_ref",
        _set((*REVIEWER, "trusted_ref"), " "),
        F,
        f"config.{FIELD}.reviewer.trusted_ref must be a non-empty string or null, found a blank string",
    ),
    (
        "pull ref",
        _set((*REVIEWER, "trusted_ref"), "refs/pull/1/head"),
        C,
        f"{FIELD}.reviewer.trusted_ref cannot be a pull ref",
    ),
    (
        "scope",
        _set((*REVIEWER, "scope"), "other"),
        F,
        f'config.{FIELD}.reviewer.scope must be one of "generic", "repository", found string "other"',
    ),
    (
        "manifest_path not a string",
        _set((*REVIEWER, "manifest_path"), 1),
        F,
        f"config.{FIELD}.reviewer.manifest_path must be a non-empty string or null, found integer 1",
    ),
    (
        "skill not a string",
        _set((*REVIEWER, "skill"), 1),
        F,
        f"config.{FIELD}.reviewer.skill must be a non-empty string or null, found integer 1",
    ),
    (
        "manifest not a string",
        _set((*REVIEWER, "manifest"), 1),
        F,
        f"config.{FIELD}.reviewer.manifest must be true, a non-empty string, or null, found integer 1",
    ),
    ("generic scope", _set((*REVIEWER, "skill"), "skills/review"), C, GENERIC_MESSAGE),
    (
        "checkout not a string",
        _set((*REPO, "checkout_path"), 1),
        F,
        f"config.{FIELD}.checkout_path must be a non-empty string or null, found integer 1",
    ),
    (
        "checkout",
        _set((*REPO, "checkout_path"), "relative"),
        C,
        f"{FIELD}.checkout_path must be {ABSOLUTE_PATH}",
    ),
    (
        "snapshot_exclude not an array",
        _set((*REPO, "snapshot_exclude"), "*.resx"),
        F,
        f'config.{FIELD}.snapshot_exclude must be an array of at most 100 items, found string "*.resx"',
    ),
    (
        "snapshot_exclude too long",
        _set((*REPO, "snapshot_exclude"), TOO_MANY_PATTERNS),
        F,
        f"config.{FIELD}.snapshot_exclude must be an array of at most 100 items, found an array of 101 items",
    ),
    (
        "snapshot_exclude pattern not a string",
        _set((*REPO, "snapshot_exclude"), ["*.resx", 1, "../*.resx"]),
        F,
        f"config.{FIELD}.snapshot_exclude[1] must be a non-empty string of at most 200 characters, found integer 1",
    ),
    (
        "snapshot_exclude pattern",
        _set((*REPO, "snapshot_exclude"), ["../*.resx", "*.resx", "*.resx"]),
        F,
        PATTERN_MESSAGE + 'string "../*.resx"',
    ),
    (
        "snapshot_exclude duplicate",
        _set((*REPO, "snapshot_exclude"), ["*.resx", "*.Resx"]),
        F,
        f'config.{FIELD}.snapshot_exclude must be distinct glob patterns, ignoring case, found "*.Resx" twice',
    ),
    (
        "unreviewed_suffixes not an array",
        _set((*REPO, "unreviewed_suffixes"), ".zip"),
        F,
        f'config.{FIELD}.unreviewed_suffixes must be an array of at most 100 items, found string ".zip"',
    ),
    (
        "unreviewed_suffixes too long",
        _set((*REPO, "unreviewed_suffixes"), TOO_MANY_SUFFIXES),
        F,
        f"config.{FIELD}.unreviewed_suffixes must be an array of at most 100 items, found an array of 101 items",
    ),
    (
        "unreviewed_suffixes suffix not a string",
        _set((*REPO, "unreviewed_suffixes"), [".bin", 1, "ZIP"]),
        F,
        f"config.{FIELD}.unreviewed_suffixes[1] must be a non-empty string of at most 32 characters, found integer 1",
    ),
    (
        "unreviewed_suffixes suffix",
        _set((*REPO, "unreviewed_suffixes"), ["ZIP", ".bin", ".bin"]),
        F,
        SUFFIX_MESSAGE + 'string "ZIP"',
    ),
    (
        "unreviewed_suffixes duplicate",
        _set((*REPO, "unreviewed_suffixes"), [".bin", ".bin"]),
        F,
        f'config.{FIELD}.unreviewed_suffixes must be distinct file-name suffixes, found ".bin" twice',
    ),
    (
        "batch_size",
        _set((*REPO, "batch_size"), 0),
        F,
        _wrong(f"{FIELD}.batch_size", "an integer of at least 1 or null", 0),
    ),
    (
        "second entry",
        _set(("repositories", "owner/later"), {"reviewer": None}),
        F,
        "config.repositories.owner/later.reviewer must be an object, found null",
    ),
    (
        "missing configuration",
        _set(("repository_sets", "zz"), ["owner/other"]),
        C,
        "Missing per-repository configuration for: owner/other",
    ),
    ("runtime", _set(("runtime",), "bash"), F, _wrong("runtime", RUNTIME_KIND, "bash")),
    ("reviewer_effort", _set(("reviewer_effort",), "extreme"), F, _wrong("reviewer_effort", EFFORT_KIND, "extreme")),
    (
        "re_review_scope not an object",
        _set(("re_review_scope",), []),
        F,
        "config.re_review_scope must be an object, found an array",
    ),
    (
        "re_review_scope unknown field",
        _set(("re_review_scope", "zzz"), 1),
        F,
        _unknown("re_review_scope", "zzz"),
    ),
    (
        "full_share not a number",
        _set(("re_review_scope", "full_share"), "0.5"),
        F,
        'config.re_review_scope.full_share must be a finite number, found string "0.5"',
    ),
    ("full_share", _set(("re_review_scope", "full_share"), 0), C, SHARE_MESSAGE),
    ("full_lines", _set(("re_review_scope", "full_lines"), 0), F, _wrong(LINES_FIELD, POSITIVE, 0)),
    (
        "verdict_policy not an object",
        _set(("verdict_policy",), []),
        F,
        "config.verdict_policy must be an object, found an array",
    ),
    (
        "verdict_policy unknown field",
        _set(("verdict_policy", "zzz"), 1),
        F,
        _unknown("verdict_policy", "zzz"),
    ),
    (
        "request_changes_for empty",
        _set(("verdict_policy", "request_changes_for"), []),
        F,
        "config.verdict_policy.request_changes_for must be a non-empty array, found an empty array",
    ),
    (
        "severity",
        _set(("verdict_policy", "request_changes_for"), ["BLOCKER"]),
        F,
        _wrong("verdict_policy.request_changes_for[0]", SEVERITY_KIND, "BLOCKER"),
    ),
    ("threshold", _set(("verdict_policy", "should_fix_threshold"), 0), F, _wrong(THRESHOLD_FIELD, POSITIVE, 0)),
    (
        "operation sets not an object",
        _set(("operation_repository_sets",), []),
        F,
        "config.operation_repository_sets must be an object, found an array",
    ),
    (
        "retired operation",
        _set(("operation_repository_sets", "update-pr-tracker"), "main"),
        C,
        "operation_repository_sets.update-pr-tracker names an operation renamed update-pr-dashboard; rename the key "
        "to update-pr-dashboard",
    ),
    (
        "operation",
        _prepend(("operation_repository_sets",), "deploy", "nope"),
        F,
        _unknown("operation_repository_sets", "deploy"),
    ),
    (
        "operation set not a string",
        _set(("operation_repository_sets", "review-prs"), 1),
        F,
        "config.operation_repository_sets.review-prs must be a non-empty string, found integer 1",
    ),
    (
        "operation set",
        _set(("operation_repository_sets", "review-prs"), "other"),
        C,
        "operation_repository_sets.review-prs must name an existing set",
    ),
    ("model_names", _set(("model_names",), None), F, "config.model_names must be an object, found null"),
    (
        "model_names key",
        _set(("model_names",), {" m": "M"}),
        F,
        f"{MODEL_KEY} must be {MODEL_KEY_KIND}, found a string with whitespace around it",
    ),
    (
        "model_names name",
        _set(("model_names",), {"m": 1}),
        F,
        "config.model_names.m must be a non-empty single-line string, found integer 1",
    ),
    (
        "model_names name too long",
        _set(("model_names",), {"m": "y" * 101}),
        C,
        "model_names.m must be a name of at most 100 characters",
    ),
    (
        "archive_root not a string",
        _set(("archive_root",), 1),
        F,
        "config.archive_root must be a non-empty string, found integer 1",
    ),
    ("archive_root", _set(("archive_root",), "relative"), C, f"archive_root must be {ABSOLUTE_PATH}"),
    (
        "local_mirror_root not a string",
        _set(("local_mirror_root",), 1),
        F,
        "config.local_mirror_root must be a non-empty string or null, found integer 1",
    ),
    (
        "local_mirror_root",
        _set(("local_mirror_root",), "relative"),
        C,
        f"local_mirror_root must be {ABSOLUTE_PATH}",
    ),
    (
        "summary_root not a string",
        _set(("summary_root",), 1),
        F,
        "config.summary_root must be a non-empty string, found integer 1",
    ),
    ("summary_root", _set(("summary_root",), "relative"), C, f"summary_root must be {ABSOLUTE_PATH}"),
    (
        "dashboard_file not a string",
        _set(("dashboard_file",), 1),
        F,
        "config.dashboard_file must be a non-empty string, found integer 1",
    ),
    (
        "dashboard_file",
        _set(("dashboard_file",), "relative"),
        C,
        f"dashboard_file must be {ABSOLUTE_PATH}",
    ),
    (
        "github_login",
        _set(("github_login",), ""),
        F,
        "config.github_login must be a non-empty string or null, found an empty string",
    ),
    ("dashboard not an object", _set(("dashboard",), []), F, "config.dashboard must be an object, found an array"),
    ("dashboard unknown field", _set(("dashboard", "zzz"), 1), F, _unknown("dashboard", "zzz")),
    (
        "start marker",
        _set(("dashboard", "start_marker"), ""),
        F,
        "config.dashboard.start_marker must be a non-empty trimmed single-line string, found an empty string",
    ),
    (
        "end marker",
        _set(("dashboard", "end_marker"), ""),
        F,
        "config.dashboard.end_marker must be a non-empty trimmed single-line string, found an empty string",
    ),
    ("markers", _set(("dashboard", "end_marker"), "<!-- code-review-pr-tracker:start -->"), C, MARKERS_MESSAGE),
    (
        "status_overrides not an object",
        _set(("dashboard", "status_overrides"), []),
        F,
        "config.dashboard.status_overrides must be an object, found an array",
    ),
    (
        "status override key not a string",
        _prepend(("dashboard", "status_overrides"), 1, ""),
        F,
        "a key of config.dashboard.status_overrides must be a non-empty string, found integer 1",
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
        F,
        "config.dashboard.status_overrides.owner/repo#1 must be a non-empty string, found an empty string",
    ),
    (
        "status override computed",
        _set(("dashboard", "status_overrides", "owner/repo#1"), "drafts"),
        C,
        "Dashboard status override owner/repo#1 duplicates a computed tracker state",
    ),
    (
        "author_names",
        _set(("dashboard", "author_names"), None),
        F,
        "config.dashboard.author_names must be an object, found null",
    ),
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
        f"{FIELD}.reviewer.manifest must be {ABSOLUTE_PATH}",
    ),
    (
        "checkout",
        _set((*REPO, "checkout_path"), "relative"),
        C,
        f"{FIELD}.checkout_path must be {ABSOLUTE_PATH}",
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
        f"{FIELD}.checkout_path must be {ABSOLUTE_PATH}",
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

# Refusals whose check a stage already orders, with another value or field name in the message. A field of the wrong
# kind is compared by its path and expected kind, without what was found.
VALUE_VARIANTS = {
    "Invalid repository-set name: ",
    "Invalid repository identity: ",
    "Unsupported config schema version: ",
    "Missing per-repository configuration for: ",
    "repositories.Owner/Repo ",
    "operation_repository_sets.review-insights ",
    "repository_sets.main[1] ",
    "Invalid dashboard status override key: ",
    "Invalid dashboard author name login: ",
    "Duplicate dashboard author name login ignoring case: ",
    "dashboard.author_names.octo ",
    "must be an object of known fields, found ",
    "must not be a filesystem root",
    PATTERN_MESSAGE,
    "snapshot_exclude[0] must be a non-empty string of at most 200 characters",
    SUFFIX_MESSAGE,
    "unreviewed_suffixes[0] must be a non-empty string of at most 32 characters",
    " twice",
    "verdict_policy.request_changes_for[1] ",
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

    def test_the_computed_states_are_the_sections_and_every_ai_review_state(self) -> None:
        # The tracker computes each AI review state from these constants, so pinning them here against literals
        # keeps a state added to the computation from being accepted as an override.
        self.assertEqual(("missing", "current", "stale", "incomplete"), DASHBOARD_AI_REVIEW_STATES)
        self.assertEqual(frozenset(COMPUTED_STATES), COMPUTED_DASHBOARD_STATES)

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
        self.assert_refused(config, C, f"{FIELD}.checkout_path must be {ABSOLUTE_PATH}")
        config = _config()
        config["repositories"] = {"owner/later": {"reviewer": None}, **config["repositories"]}
        config["repositories"]["owner/repo"]["checkout_path"] = "relative"
        self.assert_refused(config, F, "config.repositories.owner/later.reviewer must be an object, found null")

    def test_a_set_is_checked_before_the_next(self) -> None:
        config = _config()
        config["repository_sets"]["main"] = ["owner/repo", "owner/repo"]
        config["repository_sets"]["-bad"] = ["owner/repo"]
        self.assert_refused(config, C, "Repository set 'main' contains duplicates")

    def test_stages_cover_every_distinct_error(self) -> None:
        # A field of the wrong kind is one check whatever was found, so it is compared without what was found.
        def check(error: type[Exception], message: str) -> str:
            return message.split(", found ")[0] if error is F else message

        staged = {
            check(error, message) for _label, _build, stages in ORDERED for _name, _mutation, error, message in stages
        }
        refused = {check(error, message) for _name, _mutation, error, message in REJECTED}
        unstaged = [
            message for message in sorted(refused - staged) if not any(variant in message for variant in VALUE_VARIANTS)
        ]
        self.assertEqual([], unstaged)

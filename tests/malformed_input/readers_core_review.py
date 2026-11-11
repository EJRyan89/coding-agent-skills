"""The shared readers: ejryan89-skill-core's frontmatter, records, menus, and solutions, and
ejryan89-code-review-core's configuration, state, flags, runs, fixtures, manifests, records, and inventories.

Each refusal is the exception its module names; a reader that answers a file it cannot read with a value, an UNREAD
setting, a torn lock, or a printed denial or FAILED line is called through an adapter that raises that answer as
Refused.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, cast
from unittest import mock

import corpus
from reader_table import REPOSITORY, Reader, Refused, text_of

sys.path.insert(0, str(REPOSITORY / "skills" / "ejryan89-skill-core" / "scripts"))

import check_provenance
import frontmatter
import guidance_record
import skill_menu
import skill_roots
from dotnet_solution import FileRefused, read_listed_projects
from github_client import CommandResult, GitHubClient, GitHubError, body_reasons, json_body
from skill_roots import sibling_scripts
from xml_reader import XmlRefused

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))

import review_analyzers
import review_batches
import review_canary
import review_config
import review_dispatch
import review_flags
import review_guard
import review_operation
import review_records
import review_reviewers
import review_run
import review_runtime
import review_specialists
import review_state
from review_io import PersistenceError, ResourceLock, read_json


def _json(value: object) -> bytes:
    return json.dumps(value).encode()


def _frontmatter(path: Path) -> object:
    """The frontmatter as a consumer reads it: every key's value, each parsed only when it is read."""
    found = frontmatter.read(path)
    keys = found.keys()
    return {key: found.value(key) for key in keys}


def _guidance(path: Path) -> object:
    return guidance_record.read(path, (guidance_record.PYTHON_SUBJECT,))


def _provenance(path: Path) -> object:
    return check_provenance.load(path, ("alpha",))


def _menu(path: Path) -> object:
    return skill_menu.read_menu(path.parent)


def _plugin_name(path: Path) -> object:
    with mock.patch.object(skill_roots, "SKILLS_ROOT", path.parents[1] / "skills"):
        return skill_roots.plugin_name()


def _pull_selector(path: Path) -> object:
    return review_operation.parse_pull_selector(text_of(path))


def _gh_output(path: Path) -> object:
    return json_body(CommandResult(0, text_of(path), ""))


def _owner(path: Path) -> object:
    """The lock owner as a waiter inspects it: one it cannot tell, or a torn one, is refused."""
    found = ResourceLock._inspect(path.parent)
    if found is None or found[0].torn:
        raise Refused(f"the lock owner cannot be told or is torn: {found}")
    return found


def _fixture_pull(path: Path) -> object:
    return review_canary.read_fixture_pull(path.parent)


def _run(path: Path) -> object:
    return review_run.load_run(path.parent)


def _inventory(path: Path) -> object:
    found = review_analyzers.inventory(path.parent, [path.name])
    if any(setting["setting"] == review_analyzers.UNREAD for setting in found["settings"]):
        raise Refused(f"{path.name} is listed unread")
    return found


REVIEWS = Path(REPOSITORY.anchor) / "Reviews"
POLICY: dict[str, object] = {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3}
CONFIG = {
    "schema_version": 1,
    "default_repository_set": "primary",
    "repository_sets": {"primary": ["Example/One"]},
    "repositories": {
        "example/one": {
            "reviewer": {
                "id": "generic",
                "protocol_version": 1,
                "trusted_ref": None,
                "scope": "generic",
                "manifest_path": None,
            },
            "checkout_path": str(REVIEWS / "One"),
        }
    },
    "archive_root": str(REVIEWS / "Archive"),
    "local_mirror_root": str(REVIEWS / "Mirror"),
    "summary_root": str(REVIEWS / "Summaries"),
    "dashboard_file": str(REVIEWS / "Dashboard.md"),
    "github_login": "reviewer",
    "runtime": "auto",
    "verdict_policy": POLICY,
    "dashboard": {},
}
# A run.json as prepare writes it for a generic initial review, with only the fields every run has.
RUN = {
    "schema_version": review_run.RUN_SCHEMA_VERSION,
    "selector": "example/one#12",
    "mode": "initial",
    "canary": False,
    "fixture": None,
    "config_path": "/config/code-review.json",
    "host": None,
    "runtime": "claude-code",
    "kind": "generic",
    "request_path": "/run/request.json",
    "reviewer_root": None,
    "result_path": "/run/result.json",
    "adapter": {"name": "generic", "scope": "generic"},
    "roles": [{"id": "generic-review", "prompt_file": "/run/generic.md", "result_file": "/run/generic.json"}],
    "attempts": {"generic-review": 0},
    "notes": [],
    "patches": {},
    "scope": None,
    "uncovered_files": [],
}
RUN_LIST = {
    "schema_version": review_batches.RUN_LIST_SCHEMA_VERSION,
    "options": {
        "config": None,
        "host": None,
        "inline": False,
        "force": False,
        "canary": False,
        "scope": None,
        "prior": None,
    },
    "pending": [{"kind": "pull", "target": "example/one#13"}],
    "runs": [{"run": "/runs/one", "selector": "example/one#12", "status": "published"}],
}
BATCH_PULL = {
    "number": 12,
    "title": "Ship stock",
    "url": "https://github.com/example/one/pull/12",
    "state": "MERGED",
    "isDraft": False,
    "baseRefName": "main",
    "baseRefOid": "a" * 40,
    "headRefName": "ship",
    "headRefOid": "b" * 40,
    "mergedAt": "2026-03-05T10:00:00Z",
}
BATCH = {
    "schema_version": review_batches.BATCH_SCHEMA_VERSION,
    "today": "2026-03-10",
    "repositories": {
        "example/one": {
            "previous_watermark": "2026-03-01",
            "complete": True,
            "error": None,
            "eligible": [BATCH_PULL],
            "deferred": [],
            "listing": {"scan": "watermark", "pages": 2, "pulls": 1, "read": 1},
        },
        "example/two": {
            "previous_watermark": None,
            "complete": False,
            "error": "the listing failed",
            "eligible": [],
            "deferred": [],
            "listing": None,
        },
    },
}
PULL = {
    "schema_version": 1,
    "repository": "Example/Inventory",
    "number": 7,
    "title": "Ship stock",
    "base_ref": "main",
    "head_ref": "ship",
    "threads": [],
}
ENTRYPOINT_MANIFEST = {
    "schema_version": 1,
    "id": "team-review",
    "protocol_version": 1,
    "supports": ["initial"],
    "required_capabilities": [],
    "entrypoint": "SKILL.md",
    "resources": [],
    "agent_profiles": [],
}


def _record_pair() -> tuple[bytes, str]:
    """A review record and its Markdown report as write_record_pair writes them, for a review with no finding."""
    request = {
        "repository": "example/one",
        "pull_number": 12,
        "pull_url": "https://github.com/example/one/pull/12",
        "title": "Improve behavior",
        "base_ref": "main",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "mode": "initial",
        "adapter": {"name": "generic", "scope": "generic", "source_commit": None, "source_hashes": {}},
    }
    result = review_records.validate_adapter_result(
        {
            "protocol_version": 1,
            "repository": "example/one",
            "pull_number": 12,
            "head_sha": "b" * 40,
            "summary": "No issue was found.",
            "reviewer": "fixture-reviewer",
            "status": "complete",
            "findings": [],
            "prior_dispositions": [],
            "usage": None,
        },
        expected_repository="example/one",
        expected_number=12,
        expected_head_sha="b" * 40,
    )
    record = review_records.build_record(
        request,
        result,
        version=1,
        policy=POLICY,
        reviewed_at="2026-01-01T00:00:00+00:00",
    )
    with tempfile.TemporaryDirectory() as temporary:
        json_path, markdown_path = Path(temporary) / "review.json", Path(temporary) / "review.md"
        review_records.write_record_pair(json_path, markdown_path, record)
        return json_path.read_bytes(), markdown_path.read_text(encoding="utf-8")


RECORD, REPORT = _record_pair()


def _report(path: Path) -> None:
    path.with_name("review.md").write_bytes(REPORT.encode("utf-8"))


def _record(path: Path) -> object:
    return review_records.validate_record_pair(path, path.with_name("review.md"))


# A materialized specialist reviewer: its one declared file, the profile, written beside the metadata.
PROFILE = b"Review the Python files.\n"
MATERIALIZATION = {
    "schema_version": 1,
    "adapter_id": "team-specialists",
    "entrypoint": None,
    "source_commit": "c" * 40,
    "source_hashes": {"agents/python.md": hashlib.sha256(PROFILE).hexdigest()},
    "manifest": {
        "schema_version": 2,
        "id": "team-specialists",
        "protocol_version": 1,
        "kind": "specialists",
        "supports": ["initial"],
        "required_capabilities": [],
        "resources": [],
        "specialists": [
            {
                "id": "python-reviewer",
                "category": "Python",
                "profile": "agents/python.md",
                "include": [r"\.py$"],
                "exclude": [],
                "resources": [],
                "when": None,
            }
        ],
        "conditions": {},
    },
}


def _profile(path: Path) -> None:
    profile = path.parent / "agents" / "python.md"
    profile.parent.mkdir(exist_ok=True)
    profile.write_bytes(PROFILE)


def _materialized(path: Path) -> object:
    return review_specialists.load_materialized_manifest(path.parent)


UTF8_SIG = "the file is decoded as UTF-8 with an optional byte-order mark"
ANY_JSON = "it returns any JSON value, and each caller checks the shape it expects"
NO_ANALYZER = "a file that configures no analyzer names none, so the inventory lists nothing for it"
OWNER_FIELDS = (
    "it returns the owner record whole, and _stale reads its pid through read_pid as an integer from 1 to the "
    "platform's PID_MAXIMUM, holding a lock whose pid is not one, a pid past PID_MAXIMUM included, as still held, and "
    "its start time as unknown when it is not an integer"
)
MATERIALIZATION_UNREAD = "the loader reads the manifest, the adapter, and the hashes by kind, and never this field"


def _config_write(path: Path) -> object:
    """`review_config.py write` of the case into a configuration beside it, which a refusal leaves unwritten."""
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        code = review_config._main(["write", str(path), "--output", str(path.with_name("config.json"))])
    if code != 0:
        raise Refused(printed.getvalue())
    return code


READ_ROLE = "example"


def _read_log_source(path: Path) -> None:
    source = path.parents[1] / "source"
    source.mkdir()
    (source / "README.md").write_text("Example.\n", encoding="utf-8")


def _read_counts(path: Path) -> object:
    """The counts of the role's read log, which count_reads deletes once it has counted it, whatever it holds."""
    state: dict[str, Any] = {"reads": {}}
    review_dispatch.count_reads(path.parents[1], cast(Any, state), [READ_ROLE])
    return state["reads"]


READ_LOG = b'"README.md"\n{"search": "README"}\n'
# count_reads passes over a line that is not a path of the snapshot or a search, since the guard alone writes the log,
# and deletes the log once counted, so every case it can read is one it counts and accepts.
READ_LOG_COUNTED = {
    case.name: "a line that is not a path of the snapshot or a search the guard writes counts nothing"
    for case in corpus.cases("json-lines", READ_LOG)
    if case.content not in (corpus.DIRECTORY, corpus.LINK, corpus.UNREADABLE)
}


def _guard_event(path: Path) -> object:
    """The guard on the event as its hook gives it on stdin: an event it denies is Refused."""
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        review_guard.main(text_of(path))
    if printed.getvalue():
        raise Refused(printed.getvalue())
    return None


# A prepared run's roles, which _roles reads, and its restricted paths, which _restricted reads, each read apart.
GUARD_ROLES = {"roles": [{"id": "example", "prompt_file": "/run/prompt.md", "result_file": "/run/result.json"}]}
GUARD_RESTRICTED = {"restricted": {"work/issues.json": ["example"]}}
# What a reviewer's own frontmatter leaves unsaid, the inspection takes as the widest a runtime would give it.
NO_FRONTMATTER = "a document whose first line is not the opening rule has no frontmatter"
REVIEWER_FRONTMATTER = {
    **dict.fromkeys(("empty", "no_frontmatter", "bom"), NO_FRONTMATTER),
    "nul_bytes": "the body after the frontmatter's closing line is never read as frontmatter",
    **dict.fromkeys(("deep_nesting", "duplicate_keys"), "it reads one key, which the case does not hold"),
}


def _tool_grant(path: Path) -> object:
    return review_reviewers._tool_grant("example", text_of(path))


def _profile_model(path: Path) -> object:
    return review_reviewers.frontmatter_value("example.md", text_of(path), "model")


def _body_reasons(path: Path) -> object:
    reasons = body_reasons(text_of(path))
    if not reasons:
        raise Refused("the body gives no reason")
    return reasons


# A GraphQL answer GitHub gives with one node the viewer may not read, which gh exits 1 on.
PARTIAL_GRAPHQL = {"data": {"node": None}, "errors": [{"type": "NOT_FOUND", "message": "Could not resolve."}]}


def _partial_graphql(path: Path) -> object:
    output = text_of(path)
    client = GitHubClient(lambda arguments: CommandResult(1, output, "gh: Could not resolve."))
    return client.partial_graphql(
        ["api", "graphql"], lambda error: isinstance(error, dict) and error.get("type") == "NOT_FOUND", retry=False
    )


READERS = [
    Reader(
        "check_provenance.load",
        "json",
        _json({"checks": {"alpha": [{"decision": "This source grants each script by name."}]}}),
        _provenance,
        (check_provenance.ProvenanceError,),
    ),
    Reader(
        "dotnet_solution.read_listed_projects",
        "xml",
        b'<Solution><Project Path="src\\App\\App.csproj" /></Solution>',
        read_listed_projects,
        (XmlRefused, FileRefused),
        filename="Example.slnx",
        accepts={
            "bom": "a solution is read whatever its encoding, UTF-8 with or without a byte-order mark among them",
            "deep_nesting": "a well-formed .slnx with no Project element lists no project, which the reader tells "
            "apart from one it cannot read",
        },
    ),
    Reader(
        "frontmatter.read",
        "frontmatter",
        b"---\nname: example\ndescription: An example.\n---\n\nBody.\n",
        _frontmatter,
        (frontmatter.FrontmatterError,),
        accepts={
            "bom": "frontmatter.read decodes UTF-8 with an optional byte-order mark, as editors on Windows save it",
            "nul_bytes": "the body after the frontmatter's closing line is never read",
        },
    ),
    Reader(
        "github_client.json_body",
        "json",
        _json([{"number": 12, "title": "Improve behavior"}]),
        _gh_output,
        (GitHubError,),
        accepts={
            "top_level_list": f"json_body is the JSON gh printed, whatever its shape: {ANY_JSON}",
            "top_level_string": f"json_body is the JSON gh printed, whatever its shape: {ANY_JSON}",
            "top_level_null": f"json_body is the JSON gh printed, whatever its shape: {ANY_JSON}",
        },
        open_fields=ANY_JSON,
    ),
    Reader(
        "guidance_record.read",
        "json",
        _json(
            {
                "pages": [
                    {
                        "url": "https://docs.python.org/3/library/json.html",
                        "subject": guidance_record.PYTHON_SUBJECT,
                        "governs": "How JSON is read.",
                        "read": ["v1.0.0"],
                    }
                ]
            }
        ),
        _guidance,
        (guidance_record.GuidanceRecordError,),
    ),
    Reader(
        "review_analyzers.inventory:package.json",
        "json",
        _json({"devDependencies": {"eslint": "9.0.0"}}),
        _inventory,
        (Refused,),
        filename="package.json",
        accepts={
            "bom": UTF8_SIG,
            "top_level_list": NO_ANALYZER,
            "top_level_string": NO_ANALYZER,
            "top_level_null": NO_ANALYZER,
            "wrong_type:devDependencies": "npm installs nothing from devDependencies that is not an object, so it "
            "declares no analyzer",
            "wrong_type:devDependencies.eslint": "a dependency is declared whatever its version specifier holds",
        },
    ),
    Reader(
        "review_analyzers.inventory:pyproject.toml",
        "toml",
        b"[tool.ruff]\nline-length = 100\n",
        _inventory,
        (Refused,),
        filename="pyproject.toml",
        accepts={"bom": UTF8_SIG, "empty": NO_ANALYZER, "wrong_field_types": NO_ANALYZER},
    ),
    Reader(
        "review_canary.read_fixture_pull",
        "json",
        _json(PULL),
        _fixture_pull,
        (review_canary.FixtureError,),
        filename=review_canary.PULL_FILE,
        accepts={"bom": UTF8_SIG},
    ),
    Reader(
        "review_config.load_config",
        "json",
        _json(CONFIG),
        review_config.load_config,
        (review_config.ConfigurationError, PersistenceError),
        accepts={"bom": UTF8_SIG},
    ),
    Reader(
        "review_flags.load_store",
        "json",
        _json(review_flags.empty_store()),
        review_flags.load_store,
        (PersistenceError, review_flags.FlagError),
        accepts={"bom": UTF8_SIG},
    ),
    Reader(
        "review_io.ResourceLock._inspect",
        "json",
        _json({"pid": 4242, "token": "ab12", "created_unix": 1.0, "start_time": 1}),
        _owner,
        (Refused,),
        filename="lock/owner.json",
        accepts={
            "bom": UTF8_SIG,
            **{f"wrong_type:{field}": OWNER_FIELDS for field in ("pid", "created_unix", "start_time")},
        },
    ),
    Reader(
        "review_io.read_json",
        "json",
        _json({"value": 1}),
        read_json,
        (PersistenceError,),
        accepts={
            "bom": UTF8_SIG,
            "top_level_list": ANY_JSON,
            "top_level_string": ANY_JSON,
            "top_level_null": ANY_JSON,
        },
        open_fields=ANY_JSON,
    ),
    Reader(
        "review_operation.parse_pull_selector",
        "text",
        b"owner/repo#12",
        _pull_selector,
        # A repository part that is not one, such as one behind a byte-order mark, is refused as a configuration error.
        (review_operation.ReviewOperationError, review_config.ConfigurationError),
        number=lambda digits: b"owner/repo#" + digits,
    ),
    Reader(
        "review_records.validate_record_pair",
        "json",
        RECORD,
        _record,
        (PersistenceError, review_records.RecordError),
        filename="review.json",
        accepts={"bom": UTF8_SIG},
        prepare=_report,
    ),
    Reader(
        "review_batches.load_batch",
        "json",
        _json(BATCH),
        review_batches.load_batch,
        (PersistenceError, review_run.PipelineError, review_operation.ReviewOperationError),
        filename="batch.json",
        accepts={"bom": UTF8_SIG},
    ),
    Reader(
        "review_batches.load_run_list",
        "json",
        _json(RUN_LIST),
        review_batches.load_run_list,
        (PersistenceError, review_run.PipelineError),
        filename=review_batches.RUN_LIST_FILE,
        accepts={"bom": UTF8_SIG},
    ),
    Reader(
        "review_run.load_run",
        "json",
        _json(RUN),
        _run,
        (PersistenceError, review_run.PipelineError),
        filename=review_run.RUN_FILE,
        accepts={"bom": UTF8_SIG},
    ),
    Reader(
        "review_runtime.load_manifest_from_file",
        "json",
        _json(ENTRYPOINT_MANIFEST),
        review_runtime.load_manifest_from_file,
        (review_runtime.RuntimeContractError,),
        filename="manifest.json",
        accepts={"bom": UTF8_SIG},
        refuses_links=True,
    ),
    Reader(
        "review_specialists.load_materialized_manifest",
        "json",
        _json(MATERIALIZATION),
        _materialized,
        (review_specialists.SpecialistError,),
        filename="materialization.json",
        accepts={
            "bom": UTF8_SIG,
            "wrong_type:schema_version": MATERIALIZATION_UNREAD,
            "wrong_type:entrypoint": MATERIALIZATION_UNREAD,
        },
        prepare=_profile,
    ),
    Reader(
        "review_state.load_state",
        "json",
        _json(review_state.empty_state()),
        review_state.load_state,
        (PersistenceError, review_state.StateError),
        accepts={"bom": UTF8_SIG},
    ),
    Reader(
        "skill_menu.read_menu",
        "json",
        _json({"id": "example/source"}),
        _menu,
        (skill_menu.MenuError,),
        filename=skill_menu.SOURCE_FILE,
    ),
    Reader(
        "skill_roots.plugin_name",
        "json",
        _json({"name": "a-suite", "version": "1.0.0"}),
        _plugin_name,
        (ValueError,),
        filename=str(skill_roots.PLUGIN_MANIFEST.as_posix()),
        accepts={"wrong_type:version": "it reads the manifest's name alone"},
    ),
    Reader(
        "review_config._main",
        "json",
        _json(CONFIG),
        _config_write,
        (Refused,),
        accepts={"bom": UTF8_SIG},
    ),
    Reader(
        "review_dispatch.count_reads",
        "json-lines",
        READ_LOG,
        _read_counts,
        # A log it cannot stat or read raises its OSError, which check reports, never read as a role that read nothing.
        (OSError,),
        filename=f"run/work/reads-{READ_ROLE}.log",
        accepts=READ_LOG_COUNTED,
        prepare=_read_log_source,
    ),
    Reader(
        "review_guard.main",
        "json",
        _json({"tool_name": "TodoWrite", "tool_input": {"todos": []}}),
        _guard_event,
        (Refused,),
        accepts=dict.fromkeys(
            ("wrong_type:tool_input", "wrong_type:tool_input.todos"),
            "a tool the guard does not guard is let through before its input is read",
        ),
    ),
    Reader(
        "review_guard._roles",
        "json",
        _json(GUARD_ROLES),
        lambda path: review_guard._roles(path.parent),
        (review_guard.Denied,),
        filename=review_guard.RUN_FILE,
    ),
    Reader(
        "review_guard._restricted",
        "json",
        _json(GUARD_RESTRICTED),
        lambda path: review_guard._restricted(path.parent),
        (review_guard.Denied,),
        filename=review_guard.RUN_FILE,
    ),
    Reader(
        "review_reviewers._tool_grant",
        "frontmatter",
        b"---\nname: example\nallowed-tools: Read, Grep\n---\n\nBody.\n",
        _tool_grant,
        (review_runtime.RuntimeContractError,),
        # No tool list is every tool inherited, the widest grant, so the skill is inspected as one that can delegate.
        accepts=REVIEWER_FRONTMATTER,
    ),
    Reader(
        "review_reviewers.frontmatter_value",
        "frontmatter",
        b"---\nname: example\nmodel: opus\n---\n\nBody.\n",
        _profile_model,
        (review_runtime.RuntimeContractError,),
        # No model is the runtime's model inherited, which profile_model reports as the profile naming none.
        accepts=REVIEWER_FRONTMATTER,
    ),
    Reader(
        "github_client.body_reasons",
        "json",
        _json({"message": "Validation Failed", "errors": [{"message": "The listed users cannot be searched."}]}),
        _body_reasons,
        (Refused,),
        accepts={"wrong_type:message": "it reads each error's message alone, never the body's own"},
    ),
    Reader(
        "github_client.GitHubClient.partial_graphql",
        "json",
        _json(PARTIAL_GRAPHQL),
        _partial_graphql,
        (GitHubError,),
        open_fields="once every error is one its caller's accept takes, it returns the body whole, and the caller "
        "reads data and each error by kind",
    ),
]

"""The shared readers: ejryan89-skill-core's frontmatter, records, menus, and solutions, and
ejryan89-code-review-core's configuration, state, flags, runs, fixtures, manifests, records, and inventories.

Each refusal is the exception its module names; a reader that answers a file it cannot read with a value, an UNREAD
setting, or a torn lock is called through an adapter that raises that answer as Refused.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

from reader_table import REPOSITORY, Reader, Refused, text_of

sys.path.insert(0, str(REPOSITORY / "skills" / "ejryan89-skill-core" / "scripts"))

import check_provenance
import frontmatter
import guidance_record
import skill_menu
import skill_roots
from dotnet_solution import FileRefused, read_listed_projects
from github_client import CommandResult, GitHubError, json_body
from skill_roots import sibling_scripts
from xml_reader import XmlRefused

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))

import review_analyzers
import review_canary
import review_config
import review_flags
import review_records
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
        accepts={"bom": UTF8_SIG},
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
        accepts={"bom": UTF8_SIG},
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
    ),
]

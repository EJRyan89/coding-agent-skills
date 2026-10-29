"""The shipped skills' readers, other than the two cores': the files a skill reads from the repository it works on, and
the records an earlier run of it wrote.

A reader that reports a file it cannot read as a finding, an empty result, or None is called through an adapter that
raises Refused for that report; a sealed record is read with the seal its own save writes, so the case reaches the
parser rather than the seal check.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from reader_table import REPOSITORY, Reader, Refused, text_of

sys.path.insert(0, str(REPOSITORY / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))
sys.path.insert(0, str(sibling_scripts("audit-ai-config")))
sys.path.insert(0, str(sibling_scripts("audit-skill")))
sys.path.insert(0, str(sibling_scripts("curate-agent-memory")))
sys.path.insert(0, str(sibling_scripts("dotnet-dependencies")))
sys.path.insert(0, str(sibling_scripts("dotnet-format")))
sys.path.insert(0, str(sibling_scripts("flag-review-finding")))
sys.path.insert(0, str(sibling_scripts("repo-cleanup")))
sys.path.insert(0, str(sibling_scripts("review-document")))
sys.path.insert(0, str(sibling_scripts("review-insights")))
sys.path.insert(0, str(sibling_scripts("update-pr-dashboard")))
sys.path.insert(0, str(sibling_scripts("github-activity-report")))

import audit_ai_config
import cleanup_policy
import csharp_layout
import dashboard_pipeline
import dotnet_format_targets
import flag_review_finding
import github_activity_report
import memory_audit
import msbuild_conditions
import msbuild_reader
import repo_cleanup
import review_document
import review_insights
import review_synthesis
import skill_inventory
import update_pr_dashboard
from pr_change import CHANGED, UNCHANGED
from review_flags import FlagError
from review_io import PersistenceError

UTF8_SIG = "the file is decoded as UTF-8 with an optional byte-order mark"
TEXT = "the reader's contract is the file's text, and this is text"


def _errors(findings: Iterable[audit_ai_config.Finding]) -> None:
    errors = [finding.message for finding in findings if finding.severity == "ERROR"]
    if errors:
        raise Refused("; ".join(errors))


def _mcp_json(path: Path) -> object:
    servers, findings = audit_ai_config.read_mcp_json(path.parent, ".mcp.json")
    _errors(findings)
    return servers


def _claude_settings(path: Path) -> object:
    findings = audit_ai_config._claude_settings(path.parents[1])
    _errors(findings)
    return findings


def _codex_config(path: Path) -> object:
    config = audit_ai_config._codex_config(path.parents[1])
    if not config:
        raise Refused("the config read as nothing, which is what it returns for one it cannot read")
    return config


def _codex_mcp(path: Path) -> object:
    servers, findings = audit_ai_config._read_codex_mcp(path.parents[1])
    _errors(findings)
    return servers


def _metadata(path: Path) -> object:
    metadata = skill_inventory.read_metadata(path.parents[1], "example")
    if metadata is None:
        raise Refused("the metadata is missing or not a JSON object")
    return metadata


def _plugin_manifest(path: Path) -> None:
    manifest = path.parents[1] / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text('{"name": "example"}', encoding="utf-8")


# A plugin hooks file of one event with one matcher group and one handler, so each member below hooks has its cases.
PLUGIN_HOOKS = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "check"}]}]}}


def _plugin_hooks(path: Path) -> object:
    return skill_inventory.plugin_hooks(path.parents[1] / "skills" / "example")


def _memory_settings(path: Path) -> object:
    """The settings as the audit reads them, through blocking_scope, the use that reads their permissions by kind."""
    settings = memory_audit.read_settings(path)
    memory_audit.blocking_scope((("project", path),))
    return settings


def _policy(path: Path) -> object:
    cleanup_policy._read.cache_clear()
    return cleanup_policy._read(path)


@contextmanager
def _sealed(path: Path) -> Iterator[None]:
    """SEALS in a folder of its own, outside the case's folder the suite holds unwritten, holding the seal
    repo_cleanup.save_plan writes for the bytes at path, where they can be read."""
    with tempfile.TemporaryDirectory() as seals, mock.patch.object(repo_cleanup, "SEALS", Path(seals)):
        try:
            content = path.read_bytes()
        except OSError:
            content = None
        if content is not None:
            seal = repo_cleanup.seal_file(path)
            seal.parent.mkdir(parents=True, exist_ok=True)
            seal.write_text(hashlib.sha256(content).hexdigest() + "\n", encoding="utf-8")
        yield


def _record(path: Path) -> object:
    with _sealed(path):
        return repo_cleanup.read_record(path.parent)


def _plan(path: Path) -> object:
    with _sealed(path):
        return repo_cleanup.load_plan(path)


PLAN = {
    "schema_version": 1,
    "repo_root": "/repos/example",
    "repo_name": "example",
    "worktree_area": "/repos/Worktrees",
    "orphan_areas": ["/repos/Worktrees/example", "/repos/example/.claude/worktrees"],
    "default": {"name": "main", "status": "up to date"},
    "branches": [],
    "worktrees": [],
    "fastforward": [],
    "fastforward_skipped": [],
    "diverged": [],
    "policy": {"regenerable": "", "protected": ""},
    "applied": True,
    "events": [],
}


def _document_json(path: Path) -> object:
    value = review_document.json_object(path)
    if value is None:
        raise Refused("the file holds no JSON object")
    return value


SYNTHESIS_CONTEXT = "{}"
SYNTHESIS_SEAL = hashlib.sha256(f"\0{SYNTHESIS_CONTEXT}".encode()).hexdigest()


def _synthesis_companion(path: Path) -> None:
    (path.parent / review_synthesis.CONTEXT_NAME).write_text(SYNTHESIS_CONTEXT, encoding="utf-8")


def _synthesis_context(path: Path) -> object:
    return review_synthesis.load_context({"input": str(path), "input_sha256": SYNTHESIS_SEAL})


class _Detector:
    """A change detector that asks GitHub nothing: a head unchanged since the review is unchanged."""

    def detect(self, repository: str, number: int, base_ref: str, since_sha: str, head_sha: str) -> str:
        return UNCHANGED if since_sha == head_sha else CHANGED

    def prefetch(self, queries: Iterable[tuple[str, str, str, str]]) -> None:
        pass


def _dashboard(path: Path) -> None:
    dashboard = path.parent / "dashboard.md"
    dashboard.write_text(f"{update_pr_dashboard.START_MARKER}\n{update_pr_dashboard.END_MARKER}\n", encoding="utf-8")


def _dashboard_input(path: Path) -> object:
    return update_pr_dashboard.update_dashboard_rows(
        path, path.parent / "dashboard.md", "reviewer", detector=_Detector()
    )


DASHBOARD_ITEM = {
    "repository": "owner/repo",
    "number": 1,
    "url": "https://github.com/owner/repo/pull/1",
    "title": "Example",
    "author": "someone",
    "requested_reviewers": ["reviewer"],
    "participants": [],
    "draft": False,
    "base_ref": "main",
    "head_sha": "a" * 40,
    "updated_at": "2026-01-02T00:00:00Z",
    "reviewed_head_sha": "a" * 40,
    "user_review_state": None,
    "user_review_sha": None,
}

# A file system that answers every question, so a condition or a property function is settled by its text alone.
FILES = msbuild_conditions.FileSystem("/repo", lambda path: True, lambda name, start: "/repo")
GLOB = "every section name is a glob, matched as written"
NOT_A_LABEL = "text that is not a report label is a finding ID, which validate_target checks against the archive"
PYTHON_TEXT = "the case is still a valid Python module, whose facts are measured"
NO_STATED_CODE = "a line that states no exit code states none a script must exit with"


def _section_glob(path: Path) -> object:
    return csharp_layout.section_glob(text_of(path))


def _condition(path: Path) -> object:
    value = msbuild_conditions.condition_value(text_of(path), lambda name: ("Debug",), FILES)
    if value is None:
        raise Refused("the condition is malformed or cannot be settled, which the evaluation reports as UNREADABLE")
    return value


def _expanded(path: Path) -> object:
    value = msbuild_conditions.expanded(text_of(path), lambda name: ("Debug",), FILES)
    if value == msbuild_conditions.UNKNOWN:
        raise Refused("the text cannot be expanded, which the evaluation reports as UNREADABLE")
    return value


def _named_finding(path: Path) -> object:
    return flag_review_finding.named_finding(None, text_of(path))


def _stated_codes(path: Path) -> object:
    mismatches = skill_inventory.stated_codes(path, text_of(path).splitlines(), {1})
    if mismatches:
        raise Refused(" ".join(mismatches))
    return mismatches


def _script_facts(path: Path) -> object:
    return skill_inventory.script_facts(path)


HISTORY = (
    ",".join(github_activity_report.HISTORY_COLUMNS) + "\n2026-03,mona,0,0,0,0,0,false,2026-05-20T08:00:00Z,\n"
).encode()


READERS = [
    Reader(
        "audit_ai_config._claude_settings",
        "json",
        b'{"permissions": {}}',
        _claude_settings,
        (Refused,),
        filename=".claude/settings.json",
        accepts={"bom": UTF8_SIG},
    ),
    Reader(
        "audit_ai_config._codex_config",
        "toml",
        b"value = 1\n",
        _codex_config,
        (Refused,),
        filename=".codex/config.toml",
        accepts={"wrong_field_types": "it returns the table whatever keys it holds, and each check reads its own"},
    ),
    Reader(
        "audit_ai_config._read_codex_mcp",
        "toml",
        b"[mcp_servers.example]\ncommand = 'run'\n",
        _codex_mcp,
        (Refused,),
        filename=".codex/config.toml",
        accepts={
            "empty": "a configuration with no mcp_servers table declares no servers",
            "wrong_field_types": "it reads only the mcp_servers table, and one with no such table declares no servers",
        },
    ),
    Reader(
        "audit_ai_config.read_mcp_json",
        "json",
        b'{"mcpServers": {}}',
        _mcp_json,
        (Refused,),
        filename=".mcp.json",
        accepts={"bom": UTF8_SIG},
    ),
    Reader(
        "cleanup_policy._read",
        "json",
        cleanup_policy.POLICY_FILE.read_bytes(),
        _policy,
        (cleanup_policy.PolicyError,),
        filename="cleanup-policy.json",
    ),
    Reader(
        "csharp_layout.section_glob",
        "text",
        b"*.{cs,csx}",
        _section_glob,
        (csharp_layout.Failed,),
        accepts=dict.fromkeys(("empty", "bom", "nul_bytes", "truncated", "long_line"), GLOB),
        nesting=lambda levels: b"{a," * levels + b"b" + b"}" * levels,
        number=lambda digits: b"*.{1.." + digits + b"}",
    ),
    Reader(
        "flag_review_finding.named_finding",
        "text",
        b"v1 F001",
        _named_finding,
        (FlagError,),
        accepts=dict.fromkeys(("empty", "bom", "nul_bytes", "truncated", "long_line"), NOT_A_LABEL),
        number=lambda digits: b"v" + digits + b" F001",
    ),
    Reader(
        "msbuild_conditions.condition_value",
        "text",
        b"'$(Configuration)' == 'Debug'",
        _condition,
        (Refused,),
        nesting=lambda levels: b"!(" * levels + b"'a' == 'a'" + b")" * levels,
    ),
    Reader(
        "msbuild_conditions.expanded",
        "text",
        b"$([MSBuild]::GetPathOfFileAbove('Directory.Build.props', '/repo'))",
        _expanded,
        (Refused,),
        accepts=dict.fromkeys(
            ("empty", "bom", "nul_bytes", "truncated", "long_line"), "text that names no property expands to itself"
        ),
        nesting=lambda levels: b"$([MSBuild]::GetPathOfFileAbove('a', " * levels + b"'/repo'" + b"))" * levels,
    ),
    Reader(
        "skill_inventory.script_facts",
        "text",
        b"import sys\n\nsys.exit(1)\n",
        _script_facts,
        (skill_inventory.InputError,),
        filename="scripts/example.py",
        accepts={
            "empty": "an empty script prints nothing and exits 0",
            "bom": UTF8_SIG,
            "truncated": PYTHON_TEXT,
            "long_line": PYTHON_TEXT,
        },
        # A unary chain long enough to overflow the parser's own stack, which nests far deeper than a Python walk.
        nesting=lambda levels: b"x = " + b"-" * (levels * 50) + b"1\n",
    ),
    Reader(
        "skill_inventory.stated_codes",
        "text",
        b"It exits with code 1.\n",
        _stated_codes,
        (Refused,),
        filename="SKILL.md",
        accepts=dict.fromkeys(("empty", "bom", "nul_bytes", "truncated", "long_line"), NO_STATED_CODE),
        number=lambda digits: b"It exits with code " + digits + b".\n",
    ),
    Reader(
        "csharp_layout.read_source",
        "text",
        b"class Example\n{\n}\n",
        csharp_layout.read_source,
        (csharp_layout.Unreadable,),
        filename="Example.cs",
        accepts={
            "empty": "a file without a byte-order mark is decoded as UTF-8, else Latin-1, to be written back unchanged",
            "not_utf8": "a file without a byte-order mark that is not UTF-8 is decoded as Latin-1, to be written back "
            "unchanged",
            "bom": "a file with a byte-order mark is decoded in the encoding the mark names, and the mark is kept",
            "nul_bytes": "a file without a byte-order mark is decoded as UTF-8, else Latin-1, to be written back "
            "unchanged",
            "truncated": TEXT,
            "long_line": TEXT,
        },
    ),
    Reader(
        "dashboard_pipeline.read_tracker_record",
        "json",
        b'{"schema_version": 1, "pulls": []}',
        dashboard_pipeline.read_tracker_record,
        (PersistenceError, update_pr_dashboard.TrackerError),
        filename="tracker.json",
        accepts={"bom": UTF8_SIG},
    ),
    Reader(
        "dotnet_format_targets.read_file_list",
        "text",
        b"src/Example.cs\0src/Other.cs\0",
        dotnet_format_targets.read_file_list,
        (ValueError, OSError),
        filename="files.txt",
        accepts={"bom": "a byte-order mark begins the first name, as it may begin a name git lists"},
    ),
    Reader(
        "github_activity_report.read_history",
        "text",
        HISTORY,
        github_activity_report.read_history,
        (github_activity_report.ReportFailure,),
        filename="history.csv",
        accepts={"empty": "an empty history file holds no rows"},
    ),
    Reader(
        "memory_audit.read_settings",
        "json",
        b'{"permissions": {"blockReadsOutsideWorkingDirectories": false}}',
        _memory_settings,
        (memory_audit.UnreadableSettings,),
        filename="settings.json",
        accepts={
            "bom": UTF8_SIG,
            "top_level_list": "a settings file holding another value than an object reads as empty",
            "top_level_string": "a settings file holding another value than an object reads as empty",
            "top_level_null": "a settings file holding another value than an object reads as empty",
            "wrong_type:permissions.blockReadsOutsideWorkingDirectories": "only a literal true blocks reads, so a "
            "value of another kind blocks none",
        },
    ),
    Reader(
        "msbuild_reader.read_assets",
        "json",
        b'{"version": 3, "targets": {"net8.0": {}}, "project": {"frameworks": {"net8.0": {}}}}',
        msbuild_reader.read_assets,
        (msbuild_reader.Unreadable,),
        filename="project.assets.json",
        accepts={
            "bom": UTF8_SIG,
            "wrong_type:version": "the reader reads the targets and the project section, never the version",
        },
    ),
    Reader(
        "msbuild_reader.read_packages_config",
        "xml",
        b'<?xml version="1.0" encoding="utf-8"?><packages><package id="Example" version="1.0.0" /></packages>',
        msbuild_reader.read_packages_config,
        (msbuild_reader.Unreadable,),
        filename="packages.config",
        accepts={
            "bom": "an MSBuild file is decoded as UTF-16 by its byte-order mark, else as UTF-8 with an optional one",
        },
    ),
    Reader(
        "repo_cleanup.load_plan",
        "json",
        json.dumps(PLAN).encode(),
        _plan,
        (repo_cleanup.CleanupError,),
        filename="example.plan.json",
    ),
    Reader(
        "repo_cleanup.read_record",
        "json",
        b'{"schema_version": 1, "repositories": [], "policy": []}',
        _record,
        (repo_cleanup.CleanupError,),
        filename=repo_cleanup.SWEEP_RECORD,
    ),
    Reader(
        "review_document.json_object",
        "json",
        b'{"number": 1}',
        _document_json,
        (Refused,),
        filename="pull.json",
        open_fields="it returns the object whole, and its caller compares each field with the record's, so a field of "
        "another type fails as the review of another fixture",
    ),
    Reader(
        "review_insights.load_report",
        "json",
        json.dumps(
            {
                "schema_version": 6,
                "repository_set": "primary",
                "repositories": ["owner/repo"],
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
                "record_count": 0,
                "finding_count": 1,
                "severity_counts": {},
                "category_counts": {},
                # An analyzer recommendation, whose evidence entries the report renders field by field.
                "recommendations": [
                    {
                        "id": "REC-001",
                        "kind": "analyzer",
                        "coverage": "available",
                        "tool": "ruff",
                        "rule": "E501",
                        "repositories": ["owner/repo"],
                        "evidence": [
                            {
                                "repository": "owner/repo",
                                "pull_number": 1,
                                "review_version": 1,
                                "finding_id": "F001",
                                "path": "a.py",
                                "line": 1,
                                "title": "Long line",
                            }
                        ],
                        "finding_count": 1,
                        "recommendation": "Enable the rule.",
                        "decision": "deferred",
                        "decision_history": [],
                        "linked_flags": [],
                        "reviewers": [],
                    }
                ],
                "records": [],
            }
        ).encode(),
        review_insights.load_report,
        (review_insights.InsightError,),
        filename="insights.json",
        accepts={"bom": UTF8_SIG},
    ),
    Reader(
        "review_synthesis.load_context",
        "json-lines",
        json.dumps({"input_sha256": SYNTHESIS_SEAL}).encode() + b"\n",
        _synthesis_context,
        (review_synthesis.SynthesisError,),
        filename=review_synthesis.INPUT_NAME,
        prepare=_synthesis_companion,
    ),
    Reader(
        "skill_inventory.plugin_hooks",
        "json",
        json.dumps(PLUGIN_HOOKS).encode(),
        _plugin_hooks,
        (skill_inventory.InputError,),
        filename="plugin/hooks/hooks.json",
        accepts={"bom": UTF8_SIG},
        prepare=_plugin_manifest,
    ),
    Reader(
        "skill_inventory.read_metadata",
        "json",
        b'{"skill_deps": ["ejryan89-skill-core"]}',
        _metadata,
        (Refused,),
        filename="deploy-meta/example.json",
    ),
    Reader(
        "skill_inventory.require_text",
        "text",
        b"line one\nline two\n",
        skill_inventory.require_text,
        (skill_inventory.InputError,),
        filename="SKILL.md",
        accepts={"empty": TEXT, "bom": UTF8_SIG, "truncated": TEXT, "long_line": TEXT},
    ),
    Reader(
        "update_pr_dashboard.update_dashboard_rows",
        "json",
        json.dumps([DASHBOARD_ITEM]).encode(),
        _dashboard_input,
        (update_pr_dashboard.TrackerError,),
        filename="input.json",
        accepts={
            "bom": UTF8_SIG,
            "top_level_list": "the input is a list of pull requests, and an empty one holds none",
        },
        prepare=_dashboard,
    ),
]

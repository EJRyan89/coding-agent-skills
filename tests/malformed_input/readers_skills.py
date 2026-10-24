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
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from reader_table import REPOSITORY, Reader, Refused

sys.path.insert(0, str(REPOSITORY / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))
sys.path.insert(0, str(sibling_scripts("audit-ai-config")))
sys.path.insert(0, str(sibling_scripts("audit-skill")))
sys.path.insert(0, str(sibling_scripts("curate-agent-memory")))
sys.path.insert(0, str(sibling_scripts("dotnet-dependencies")))
sys.path.insert(0, str(sibling_scripts("dotnet-format")))
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
import github_activity_report
import memory_audit
import msbuild_reader
import repo_cleanup
import review_document
import review_insights
import review_synthesis
import skill_inventory
import update_pr_dashboard
from pr_change import CHANGED, UNCHANGED
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


def _plugin_hooks(path: Path) -> object:
    plugin = path.parents[1]
    manifest = plugin / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text('{"name": "example"}', encoding="utf-8")
    return skill_inventory.plugin_hooks(plugin / "skills" / "example")


def _policy(path: Path) -> object:
    cleanup_policy._read.cache_clear()
    return cleanup_policy._read(path)


@contextmanager
def _sealed(path: Path) -> Iterator[None]:
    """SEALS in the case's folder, holding the seal repo_cleanup.save_plan writes for the bytes at path."""
    with mock.patch.object(repo_cleanup, "SEALS", path.parent / "seals"):
        if path.is_file():
            seal = repo_cleanup.seal_file(path)
            seal.parent.mkdir(parents=True, exist_ok=True)
            seal.write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "\n", encoding="utf-8")
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


def _synthesis_context(path: Path) -> object:
    (path.parent / review_synthesis.CONTEXT_NAME).write_text(SYNTHESIS_CONTEXT, encoding="utf-8")
    return review_synthesis.load_context({"input": str(path), "input_sha256": SYNTHESIS_SEAL})


class _Detector:
    """A change detector that asks GitHub nothing: a head unchanged since the review is unchanged."""

    def detect(self, repository: str, number: int, base_ref: str, since_sha: str, head_sha: str) -> str:
        return UNCHANGED if since_sha == head_sha else CHANGED

    def prefetch(self, queries: Iterable[tuple[str, str, str, str]]) -> None:
        pass


def _dashboard_input(path: Path) -> object:
    dashboard = path.parent / "dashboard.md"
    dashboard.write_text(f"{update_pr_dashboard.START_MARKER}\n{update_pr_dashboard.END_MARKER}\n", encoding="utf-8")
    return update_pr_dashboard.update_dashboard_rows(path, dashboard, "reviewer", detector=_Detector())


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
        b"src/Example.cs\nsrc/Other.cs\n",
        dotnet_format_targets.read_file_list,
        (ValueError, OSError),
        filename="files.txt",
        accepts={
            "bom": "the list is read without a byte-order mark",
            "truncated": "each non-blank line is a path the list names",
            "long_line": "each non-blank line is a path the list names",
        },
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
        b'{"permissions": {}}',
        memory_audit.read_settings,
        (memory_audit.UnreadableSettings,),
        filename="settings.json",
        accepts={
            "bom": UTF8_SIG,
            "top_level_list": "a settings file holding another value than an object reads as empty",
            "top_level_string": "a settings file holding another value than an object reads as empty",
            "top_level_null": "a settings file holding another value than an object reads as empty",
        },
    ),
    Reader(
        "msbuild_reader.read_assets",
        "json",
        b'{"version": 3, "targets": {"net8.0": {}}, "project": {"frameworks": {"net8.0": {}}}}',
        msbuild_reader.read_assets,
        (msbuild_reader.Unreadable,),
        filename="project.assets.json",
        accepts={"bom": UTF8_SIG},
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
            "deep_nesting": "the case is well-formed XML with no package element, which lists no packages",
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
    ),
    Reader(
        "review_insights.load_report",
        "json",
        b'{"schema_version": 6, "recommendations": [], "records": []}',
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
    ),
    Reader(
        "skill_inventory.plugin_hooks",
        "json",
        b'{"hooks": {}}',
        _plugin_hooks,
        (skill_inventory.InputError,),
        filename="plugin/hooks/hooks.json",
        accepts={"bom": UTF8_SIG},
    ),
    Reader(
        "skill_inventory.read_metadata",
        "json",
        b'{"skill_deps": []}',
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
    ),
]

#!/usr/bin/env python3
"""Fixture tests for the AI agent configuration audit engine."""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import audit_ai_config as audit
import check_provenance
import markdown_literal
import pytest
from folder_walk import SKIPPED_FOLDERS

OWNERSHIP = audit.OWNERSHIP_MARKER


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _setup_conforming_repo(root: Path) -> None:
    """Create a minimal conforming repo with manifest."""
    (root / ".github").mkdir(parents=True)
    (root / ".claude/skills/demo").mkdir(parents=True)
    (root / ".agents/skills/demo").mkdir(parents=True)

    claude_content = (
        "# CLAUDE.md\n\n"
        "## Overview\n\nA test repo.\n\n"
        "## Build and Test Commands\n\n```bash\nmake test\n```\n\n"
        "## Formatting Rules\n\nUse tabs.\n\n"
        "## Maintaining AI Agent Config\n\n"
        "Run `python .github/scripts/ai_config.py --write`\n\n"
        "## CI / Quality Gates\n\n"
        "80% coverage required. Never disable linting rules.\n"
    )
    (root / "CLAUDE.md").write_text(claude_content, encoding="utf-8")

    repo_name = root.name

    copilot_content = (
        f"# {repo_name} — AI Coding Instructions\n\n"
        f"> {OWNERSHIP}. Do not edit directly"
        " — update CLAUDE.md instead.\n\n"
        "## Overview\n\nA test repo.\n\n"
        "## Build and Test Commands\n\n```bash\nmake test\n```\n\n"
        "## Formatting Rules\n\nUse tabs.\n\n"
        "## CI / Quality Gates\n\n"
        "80% coverage required. Never disable linting rules.\n"
    )
    (root / ".github/copilot-instructions.md").write_text(copilot_content, encoding="utf-8")
    agents_content = (
        f"<!-- {OWNERSHIP}. Do not edit directly"
        " — update CLAUDE.md instead. -->\n\n"
        f"# {repo_name}\n\n"
        "Read and follow `CLAUDE.md` as the authoritative source for all "
        "repository instructions, conventions, and workflows.\n\n"
        "All build commands, formatting rules, test conventions, architecture "
        "guidance, and behavioral constraints are maintained in `CLAUDE.md`. "
        "Do not duplicate or contradict its content here.\n"
    )
    (root / "AGENTS.md").write_text(agents_content, encoding="utf-8")

    shim_content = (
        "---\nname: demo\ndescription: Test skill.\n---\n\n"
        f"<!-- {OWNERSHIP}. Do not edit directly"
        " — update the canonical skill at .claude/skills/demo/SKILL.md"
        " and regenerate. -->\n\n"
        "Read and follow `../../../.claude/skills/demo/SKILL.md`"
        " as the authoritative workflow.\n"
        "Resolve all relative paths and supporting resources"
        " from `../../../.claude/skills/demo/`.\n"
    )
    (root / ".agents/skills/demo/SKILL.md").write_text(shim_content, encoding="utf-8")

    (root / ".claude/skills/demo/SKILL.md").write_text(
        "---\nname: demo\ndescription: Test skill.\n---\n\nCanonical workflow.\n",
        encoding="utf-8",
    )

    copilot_hash = _content_hash(copilot_content)
    manifest = {
        "generatedBy": "ai_config.py",
        "schemaVersion": 1,
        "canonicalSource": "CLAUDE.md",
        "runtimes": ["claude", "codex"],
        "surfaces": ["copilot_cli"],
        "features": [],
        "mcp_servers": [],
        "artifacts": [
            {"path": ".github/copilot-instructions.md", "hash": copilot_hash},
            {"path": "AGENTS.md"},
            {"path": ".agents/skills/demo/SKILL.md"},
        ],
    }
    (root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _assert_scope_status(test: unittest.TestCase, result: audit.AuditResult, expected: str) -> None:
    test.assertIn(result.scope_status, audit.SCOPE_STATUSES)
    test.assertEqual(expected, result.scope_status)
    test.assertEqual(expected, json.loads(audit.format_json(result))["scopeStatus"])
    test.assertIn(f"Scope: **{expected}**", audit.format_markdown(result).splitlines())


# ---------------------------------------------------------------------------
# Authority classification tests
# ---------------------------------------------------------------------------


class AuthorityClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_conforming_with_manifest(self) -> None:
        _setup_conforming_repo(self.root)
        cls, _, _ = audit.classify_authority(self.root)
        self.assertEqual("conforming", cls)

    def test_conforming_without_manifest_two_signals(self) -> None:
        (self.root / ".github/scripts").mkdir(parents=True)
        (self.root / "CLAUDE.md").write_text(
            "# CLAUDE.md\n\n## Maintaining AI Agent Config\n\nRun the generator.\n",
            encoding="utf-8",
        )
        (self.root / ".github/scripts/ai_config.py").write_text("# Reads CLAUDE.md\n", encoding="utf-8")
        cls, _, _ = audit.classify_authority(self.root)
        self.assertEqual("conforming", cls)

    def test_ambiguous_one_signal(self) -> None:
        (self.root / "CLAUDE.md").write_text("# CLAUDE.md\n", encoding="utf-8")
        cls, _, _ = audit.classify_authority(self.root)
        self.assertEqual("ambiguous", cls)

    def test_unconfigured_no_signals(self) -> None:
        cls, _, _ = audit.classify_authority(self.root)
        self.assertEqual("unconfigured", cls)

    def test_alternative_authority(self) -> None:
        (self.root / ".github").mkdir(parents=True)
        manifest = {
            "generatedBy": "custom_gen.py",
            "canonicalSource": "AGENTS.md",
        }
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        cls, _, _ = audit.classify_authority(self.root)
        self.assertEqual("alternative", cls)

    def test_ambiguous_no_false_drift(self) -> None:
        """Ambiguous repo should not produce drift findings."""
        (self.root / "CLAUDE.md").write_text("# CLAUDE.md\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("# Custom AGENTS.md\n", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertEqual("ambiguous", result.authority)
        self.assertFalse(
            any(f.check == "parity" for f in result.findings),
            "Ambiguous repo should not have parity findings",
        )

    def test_alternative_no_false_drift(self) -> None:
        """Alternative authority repo should not produce drift findings."""
        (self.root / ".github").mkdir(parents=True)
        manifest = {
            "generatedBy": "gen.py",
            "canonicalSource": "AGENTS.md",
        }
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertEqual("alternative", result.authority)
        self.assertFalse(
            any(f.check == "parity" for f in result.findings),
            "Alternative authority repo should not have parity findings",
        )


# ---------------------------------------------------------------------------
# Exit code tests
# ---------------------------------------------------------------------------


class ExitCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_conforming_passes_exit_0(self) -> None:
        _setup_conforming_repo(self.root)
        result = audit.audit(self.root)
        self.assertEqual(0, result.exit_code)
        self.assertEqual("COMPLIANT", result.result)

    def test_unconfigured_is_inconclusive_with_exit_1(self) -> None:
        result = audit.audit(self.root)
        _assert_scope_status(self, result, "not-applicable")
        self.assertEqual(1, result.exit_code)
        self.assertEqual("INCONCLUSIVE", result.result)

    def test_ambiguous_is_inconclusive_with_exit_1(self) -> None:
        (self.root / "CLAUDE.md").write_text("# CLAUDE.md\n", encoding="utf-8")
        result = audit.audit(self.root)
        _assert_scope_status(self, result, "not-applicable")
        self.assertEqual(1, result.exit_code)
        self.assertEqual("INCONCLUSIVE", result.result)

    def test_alternative_is_inconclusive_with_exit_1(self) -> None:
        (self.root / ".github").mkdir(parents=True)
        manifest = {
            "generatedBy": "custom_gen.py",
            "canonicalSource": "AGENTS.md",
        }
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        _assert_scope_status(self, result, "not-applicable")
        self.assertEqual(1, result.exit_code)
        self.assertEqual("INCONCLUSIVE", result.result)

    def test_error_finding_exit_1(self) -> None:
        _setup_conforming_repo(self.root)
        # Remove a generated artifact to trigger ERROR
        (self.root / ".github/copilot-instructions.md").unlink()
        result = audit.audit(self.root)
        self.assertEqual(1, result.exit_code)
        self.assertEqual("ERRORS", result.result)

    def test_inconclusive_wins_over_error_findings(self) -> None:
        result = audit.AuditResult(
            authority="ambiguous",
            findings=[audit.Finding(severity="ERROR", check="manifest")],
        )
        self.assertEqual(("INCONCLUSIVE", 1), (result.result, result.exit_code))


# ---------------------------------------------------------------------------
# Command-line tests
# ---------------------------------------------------------------------------


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo with spaces"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _main(self, *arguments: str) -> tuple[int, list[str], str]:
        output, errors = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
            mock.patch.object(sys, "argv", ["audit_ai_config.py", *arguments]),
        ):
            code = audit.main()
        return code, output.getvalue().splitlines(), errors.getvalue()

    def test_a_non_repository_fails_on_stdout_with_exit_1(self) -> None:
        for extra in ((), ("--json",)):
            with self.subTest(extra=extra):
                code, lines, errors = self._main("--root", str(self.root), *extra)
                self.assertEqual(1, code)
                self.assertEqual([f"FAILED {self.root} is not a Git repository (no .git found)"], lines)
                self.assertEqual("", errors)

    def test_an_inconclusive_audit_prints_its_result_line_and_exits_1(self) -> None:
        (self.root / ".git").mkdir()
        code, lines, errors = self._main("--root", str(self.root))
        self.assertEqual(1, code)
        self.assertEqual(1, lines.count("RESULT INCONCLUSIVE"), lines)
        self.assertEqual([], [line for line in lines if line.startswith("RESULT ") and line != "RESULT INCONCLUSIVE"])
        self.assertEqual("", errors)

    def test_an_inconclusive_json_audit_exits_1_and_keeps_its_fields(self) -> None:
        (self.root / ".git").mkdir()
        code, lines, _ = self._main("--root", str(self.root), "--json")
        data = json.loads("\n".join(lines))
        self.assertEqual(1, code)
        self.assertEqual(("unconfigured", 1), (data["authority"], data["exitCode"]))
        self.assertEqual({"repository", "authority", "scopeStatus", "exitCode", "findings"}, set(data))

    def test_a_compliant_audit_prints_its_result_line_and_exits_0(self) -> None:
        (self.root / ".git").mkdir()
        _setup_conforming_repo(self.root)
        code, lines, _ = self._main("--root", str(self.root))
        self.assertEqual(0, code, lines)
        self.assertIn("RESULT COMPLIANT", lines)

    def test_an_audit_with_errors_prints_its_result_line_and_exits_1(self) -> None:
        (self.root / ".git").mkdir()
        _setup_conforming_repo(self.root)
        (self.root / ".github/copilot-instructions.md").unlink()
        code, lines, _ = self._main("--root", str(self.root))
        self.assertEqual(1, code)
        self.assertIn("RESULT ERRORS", lines)

    def test_a_usage_error_exits_2(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self._main("--no-such-option")
        self.assertEqual(2, raised.exception.code)

    def test_output_survives_a_console_that_cannot_encode_it(self) -> None:
        # A Windows pipe defaults to a legacy code page, and the audited path is printed as it is named.
        root = Path(self.temp.name) / "repo → ✓"
        root.mkdir()
        result = subprocess.run(
            [sys.executable, "-B", str(Path(audit.__file__).resolve()), "--root", str(root)],
            capture_output=True,
            env={**os.environ, "PYTHONIOENCODING": "cp1252"},
            check=False,
        )
        self.assertEqual(1, result.returncode, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(
            [f"FAILED {root} is not a Git repository (no .git found)"], result.stdout.decode("utf-8").splitlines()
        )


# ---------------------------------------------------------------------------
# Parity validation tests
# ---------------------------------------------------------------------------


class ParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_missing_artifact(self) -> None:
        (self.root / ".github/copilot-instructions.md").unlink()
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" and "missing" in f.message.lower() for f in result.findings),
        )

    def test_a_hashed_artifact_that_changed_is_a_conflict_whatever_its_format(self) -> None:
        # The fixture's manifest hashes the Markdown copilot-instructions.md, not only its JSON artifacts.
        (self.root / ".github/copilot-instructions.md").write_text(
            f"> {OWNERSHIP}.\n\nModified content.\n", encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertIn(
            ("ERROR", "parity", "Artifact modified (hash mismatch with manifest)"),
            [(f.severity, f.check, f.message) for f in result.findings if f.path == ".github/copilot-instructions.md"],
        )

    def test_manifest_path_traversal_rejected(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["artifacts"].append({"path": "../outside/evil.md"})
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" and "path traversal" in f.message for f in result.findings),
        )

    def test_manifest_absolute_path_rejected(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["artifacts"].append({"path": "/etc/passwd"})
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" and "absolute path" in f.message for f in result.findings),
        )

    def test_manifest_backslash_traversal_rejected(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["artifacts"].append({"path": "..\\outside\\file.md"})
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" and "backslash" in f.message for f in result.findings),
        )

    def test_manifest_drive_qualified_rejected(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["artifacts"].append({"path": "C:/Windows/System32"})
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" and "drive-qualified" in f.message for f in result.findings),
        )

    def test_manifest_path_outside_allowlist_rejected(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["artifacts"].append({"path": "src/main.py"})
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" and "allowlist" in f.message for f in result.findings),
        )

    def test_ownership_marker_missing(self) -> None:
        (self.root / "AGENTS.md").write_text("# Plain AGENTS.md\n\nNo marker.\n", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check in ("ownership", "collision", "parity") and f.severity == "ERROR" for f in result.findings),
        )


# ---------------------------------------------------------------------------
# Orphan detection tests
# ---------------------------------------------------------------------------


class OrphanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_orphan_shim_detected(self) -> None:
        (self.root / ".agents/skills/orphan").mkdir(parents=True)
        (self.root / ".agents/skills/orphan/SKILL.md").write_text(
            "---\nname: orphan\ndescription: Stale.\n---\n\nOrphaned.\n",
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "orphan" and f.path is not None and "orphan" in f.path for f in result.findings),
        )


# ---------------------------------------------------------------------------
# MCP configuration tests
# ---------------------------------------------------------------------------


class McpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_malformed_mcp_json(self) -> None:
        (self.root / ".mcp.json").write_text("{bad json", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "mcp" and f.severity == "ERROR" and ".mcp.json" in (f.path or "") for f in result.findings),
        )

    def test_malformed_codex_toml(self) -> None:
        (self.root / ".codex").mkdir(exist_ok=True)
        (self.root / ".codex/config.toml").write_text("invalid [[ toml", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "mcp"
                and f.severity == "ERROR"
                and f.path == ".codex/config.toml"
                and f.message.startswith("Invalid TOML:")
                for f in result.findings
            ),
        )

    def test_unreadable_codex_toml_is_reported(self) -> None:
        (self.root / ".codex").mkdir(exist_ok=True)
        (self.root / ".codex/config.toml").write_text("", encoding="utf-8")

        with mock.patch.object(
            tomllib,
            "load",
            side_effect=OSError("access denied"),
        ):
            result = audit.audit(self.root)

        self.assertTrue(
            any(
                f.check == "mcp"
                and f.severity == "ERROR"
                and f.path == ".codex/config.toml"
                and f.message == "Error reading config: access denied"
                for f in result.findings
            ),
        )

    def test_codex_servers_that_are_not_tables_are_reported_as_the_json_readers_report_them(self) -> None:
        cases = {
            'mcp_servers = "docs"\n': [("ERROR", "'mcp_servers' must be a table")],
            "mcp_servers = [1]\n": [("ERROR", "'mcp_servers' must be a table")],
            "[mcp_servers]\ndocs = 1\n": [("WARNING", "Server 'docs': entry must be a table, got int")],
            '[mcp_servers.docs]\ncommand = "docs"\n': [],
            "model = 'x'\n": [],
        }
        (self.root / ".codex").mkdir(exist_ok=True)
        for text, expected in cases.items():
            with self.subTest(text=text):
                (self.root / ".codex/config.toml").write_text(text, encoding="utf-8")
                servers, findings = audit._read_codex_mcp(self.root)
                self.assertEqual(expected, [(f.severity, f.message) for f in findings])
                self.assertEqual({".codex/config.toml"}, {f.path for f in findings} or {".codex/config.toml"})
                self.assertEqual(["docs"] if "command" in text else [], list(servers))

    def test_vscode_mcp_wrong_schema(self) -> None:
        (self.root / ".vscode").mkdir(exist_ok=True)
        (self.root / ".vscode/mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "mcp" and "servers" in f.message.lower() for f in result.findings),
        )

    def test_duplicate_server_names(self) -> None:
        (self.root / ".mcp.json").write_text('{"mcpServers": {"srv": {"command": "a"}}}', encoding="utf-8")
        (self.root / ".github/mcp.json").write_text('{"mcpServers": {"srv": {"command": "b"}}}', encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "mcp" and "Duplicate" in f.message for f in result.findings),
        )

    def test_sse_codex_rejected(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["mcp_servers"] = [{"name": "bad", "transport": "sse", "targets": ["codex"]}]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "mcp" and "sse" in f.message and "codex" in f.message for f in result.findings),
        )

    def test_local_claude_rejected(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["mcp_servers"] = [{"name": "bad", "transport": "local", "targets": ["claude"]}]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "mcp" and "local" in f.message and "claude" in f.message for f in result.findings),
        )

    def test_codex_http_env_rejected(self) -> None:
        (self.root / ".codex").mkdir(exist_ok=True)
        (self.root / ".codex/config.toml").write_text(
            '[mcp_servers.srv]\nurl = "http://localhost:8080"\n[mcp_servers.srv.env]\nKEY = "value"\n',
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "mcp" and "STDIO-only" in f.message for f in result.findings),
        )

    def test_copilot_repository_emits_warning(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["copilot_cli"]
        manifest["mcp_servers"] = [
            {"name": "srv", "targets": ["copilot_repository"], "transport": "stdio"},
        ]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "mcp" and f.severity == "WARNING" and "repository" in f.message.lower()
                for f in result.findings
            ),
        )

    def test_code_review_repository_mcp_readonlyhint_warning(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["code_review"]
        manifest["mcp_servers"] = [
            {"name": "srv", "targets": ["copilot_repository"], "transport": "stdio"},
        ]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "mcp" and "readOnlyHint" in f.message for f in result.findings),
        )

    def test_copilot_local_tools_allowlist_reported(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["copilot_cli"]
        manifest["mcp_servers"] = [
            {"name": "srv", "targets": ["claude", "copilot_local"], "transport": "stdio"},
        ]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / ".mcp.json").write_text(
            '{"mcpServers": {"srv": {"command": "a", "tools": ["tool_a"]}}}',
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "mcp" and "allowlist" in f.message.lower() for f in result.findings),
        )

    def test_mcp_json_non_dict_toplevel(self) -> None:
        """Non-dict top-level JSON should produce an ERROR, not crash."""
        (self.root / ".mcp.json").write_text("[]", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "mcp" and f.severity == "ERROR" and "object" in f.message.lower() for f in result.findings),
        )

    def test_mcp_parity_includes_cwd_env(self) -> None:
        """Servers with same command but different cwd should warn."""
        (self.root / ".mcp.json").write_text(
            '{"mcpServers": {"srv": {"command": "python", "args": ["a.py"], "cwd": "/a"}}}',
            encoding="utf-8",
        )
        (self.root / ".vscode").mkdir(exist_ok=True)
        (self.root / ".vscode/mcp.json").write_text(
            '{"servers": {"srv": {"type": "stdio", "command": "python", "args": ["a.py"], "cwd": "/b"}}}',
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "mcp" and "differ" in f.message.lower() and "srv" in f.message for f in result.findings),
        )

    def test_codex_toml_included_in_parity(self) -> None:
        """A TOML server with different command than .mcp.json should warn."""
        (self.root / ".mcp.json").write_text(
            '{"mcpServers": {"srv": {"command": "python", "args": ["a.py"]}}}',
            encoding="utf-8",
        )
        (self.root / ".codex").mkdir(exist_ok=True)
        (self.root / ".codex/config.toml").write_text(
            '[mcp_servers.srv]\ncommand = "node"\nargs = ["a.js"]\n',
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "mcp" and "differ" in f.message.lower() and "srv" in f.message for f in result.findings),
        )

    def test_mcp_json_missing_mcpservers_key(self) -> None:
        """Empty object in .mcp.json should produce WARNING about missing key."""
        (self.root / ".mcp.json").write_text("{}", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "mcp" and f.severity == "WARNING" and "mcpServers" in f.message and "not found" in f.message
                for f in result.findings
            ),
        )

    def test_mcp_json_non_dict_server_entry(self) -> None:
        """Non-dict server entry should produce WARNING, not crash."""
        (self.root / ".mcp.json").write_text('{"mcpServers": {"srv": "not-a-dict"}}', encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "mcp" and f.severity == "WARNING" and "srv" in f.message and "object" in f.message.lower()
                for f in result.findings
            ),
        )

    def test_mcp_json_omitted_tools_reported_unrestricted(self) -> None:
        """Server with omitted tools field is unrestricted for copilot_local."""
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["copilot_cli"]
        manifest["mcp_servers"] = [
            {"name": "srv", "targets": ["claude", "copilot_local"], "transport": "stdio"},
        ]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / ".mcp.json").write_text('{"mcpServers": {"srv": {"command": "a"}}}', encoding="utf-8")
        result = audit.audit(self.root)
        # Omitted tools = unrestricted, which is fine (no allowlist to reject)
        mcp_errors = [
            f
            for f in result.findings
            if f.check == "mcp" and f.severity == "ERROR" and "allowlist" in f.message.lower()
        ]
        self.assertEqual(0, len(mcp_errors))

    def test_mcp_json_wildcard_tools_not_rejected(self) -> None:
        """tools: ["*"] is unrestricted — not a restricted allowlist."""
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["copilot_cli"]
        manifest["mcp_servers"] = [
            {"name": "srv", "targets": ["claude", "copilot_local"], "transport": "stdio"},
        ]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / ".mcp.json").write_text(
            '{"mcpServers": {"srv": {"command": "a", "tools": ["*"]}}}',
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        mcp_errors = [
            f
            for f in result.findings
            if f.check == "mcp" and f.severity == "ERROR" and "allowlist" in f.message.lower()
        ]
        self.assertEqual(0, len(mcp_errors))


# ---------------------------------------------------------------------------
# Instruction layering tests
# ---------------------------------------------------------------------------


class InstructionLayeringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_cloud_agent_adapter_redirects(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["cloud_agent"]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "layering" and "redirects" in f.message for f in result.findings),
        )

    def test_cloud_agent_no_agents_md(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["cloud_agent"]
        manifest["runtimes"] = ["claude"]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / "AGENTS.md").unlink()
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "layering" and "directly" in f.message for f in result.findings),
        )

    def test_cloud_agent_non_redirecting_agents_md(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["cloud_agent"]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / "AGENTS.md").write_text(
            f"<!-- {OWNERSHIP} -->\n\n# Custom instructions\n\nDo something else.\n",
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "layering" and "non-redirecting" in f.message for f in result.findings),
        )

    def test_nested_agents_md_warning(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["cloud_agent"]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / "src").mkdir(exist_ok=True)
        (self.root / "src/AGENTS.md").write_text("# Nested\n\nSubtree instructions.\n", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "layering" and "Nested" in f.message and "supersedes" in f.message for f in result.findings),
        )

    def test_code_review_emits_warning(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["code_review"]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "layering" and "code-review" in f.message.lower() for f in result.findings),
        )

    def test_vscode_emits_settings_warning(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["vscode"]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "layering" and "useClaudeMdFile" in f.message for f in result.findings),
        )

    def test_the_folder_trust_warning_follows_the_copilot_cli_and_app_surfaces(self) -> None:
        # references/report-schema.md ties this warning to the Copilot CLI/app surfaces, not to an MCP target.
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        for surfaces, servers, warned in (
            (["copilot_cli"], [], True),
            (["copilot_app"], [], True),
            (["vscode"], [{"name": "docs", "transport": "stdio", "targets": ["copilot_local"]}], False),
        ):
            with self.subTest(surfaces=surfaces):
                manifest["surfaces"], manifest["mcp_servers"] = surfaces, servers
                (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                result = audit.audit(self.root)
                self.assertEqual(
                    warned, any(f.check == "layering" and "folder trust" in f.message for f in result.findings)
                )

    def test_codex_override_masks_adapter(self) -> None:
        (self.root / "AGENTS.override.md").write_text("# Override\n\nCustom instructions.\n", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "layering" and f.severity == "ERROR" and "override" in f.message.lower()
                for f in result.findings
            ),
        )

    def test_codex_nested_override_warning(self) -> None:
        (self.root / "src").mkdir(exist_ok=True)
        (self.root / "src/AGENTS.override.md").write_text("# Subtree override\n\nCustom.\n", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "layering"
                and f.severity == "WARNING"
                and "conflicting" in f.message.lower()
                and f.path == "src/AGENTS.override.md"
                for f in result.findings
            ),
        )

    def test_codex_nested_agents_md_warning(self) -> None:
        (self.root / "src").mkdir(exist_ok=True)
        (self.root / "src/AGENTS.md").write_text("# Subtree instructions\n\nNon-redirecting.\n", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "layering"
                and f.severity == "WARNING"
                and "conflicting" in f.message.lower()
                and f.path == "src/AGENTS.md"
                for f in result.findings
            ),
        )

    def test_jetbrains_no_instructions_error(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["jetbrains"]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / ".github/copilot-instructions.md").unlink()
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "layering" and f.severity == "ERROR" and "jetbrains" in f.message.lower()
                for f in result.findings
            ),
        )

    def test_mcp_cross_runtime_parity_warning(self) -> None:
        """Server with different command in .mcp.json and .vscode/mcp.json."""
        (self.root / ".mcp.json").write_text(
            '{"mcpServers": {"srv": {"command": "python", "args": ["a.py"]}}}',
            encoding="utf-8",
        )
        (self.root / ".vscode").mkdir(exist_ok=True)
        (self.root / ".vscode/mcp.json").write_text(
            '{"servers": {"srv": {"type": "stdio", "command": "node", "args": ["a.js"]}}}',
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "mcp" and "differ" in f.message.lower() and "srv" in f.message for f in result.findings),
        )

    def test_parity_deterministic_content_mismatch(self) -> None:
        """AGENTS.md with correct marker but wrong content is flagged."""
        (self.root / "AGENTS.md").write_text(
            f"<!-- {audit.OWNERSHIP_MARKER}. Do not edit directly"
            " — update CLAUDE.md instead. -->\n\n"
            "# WRONG NAME\n\nDifferent content.\n",
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "parity" and f.severity == "ERROR" and "deterministic" in f.message.lower()
                for f in result.findings
            ),
        )

    def test_parity_copilot_instructions_drift(self) -> None:
        """copilot-instructions.md with marker but wrong sections is flagged."""
        # Remove the hash so the deterministic content comparison is reached
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        for artifact in manifest["artifacts"]:
            if artifact["path"] == ".github/copilot-instructions.md":
                artifact.pop("hash", None)
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / ".github/copilot-instructions.md").write_text(
            f"# Custom Title\n\n"
            f"> {audit.OWNERSHIP_MARKER}. Do not edit directly"
            " — update CLAUDE.md instead.\n\n"
            "## Overview\n\nCompletely different content.\n",
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "parity"
                and f.severity == "ERROR"
                and f.path == ".github/copilot-instructions.md"
                and "sections" in f.message.lower()
                for f in result.findings
            ),
        )

    def test_parity_copilot_instructions_custom_title_passes(self) -> None:
        """Custom title with correct sections should NOT trigger parity error."""
        # Remove the hash so the deterministic content comparison is reached
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        for artifact in manifest["artifacts"]:
            if artifact["path"] == ".github/copilot-instructions.md":
                artifact.pop("hash", None)
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        # Rewrite with a custom title but keep the same sections from CLAUDE.md
        claude_content = audit.read_text(self.root / "CLAUDE.md")
        sections = audit._extract_sections(claude_content, audit.DEFAULT_COPILOT_SECTIONS)
        (self.root / ".github/copilot-instructions.md").write_text(
            f"# Totally Custom Title\n\n{audit.COPILOT_BANNER}\n\n{sections}\n",
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        parity_errors = [
            f
            for f in result.findings
            if f.check == "parity" and f.severity == "ERROR" and f.path == ".github/copilot-instructions.md"
        ]
        self.assertEqual(0, len(parity_errors), parity_errors)

    def test_parity_uses_manifest_declared_copilot_sections(self) -> None:
        manifest_path = self.root / ".github/ai-config-manifest.json"
        manifest = json.loads(audit.read_text(manifest_path))
        manifest["copilot_sections"] = ["Overview", "Maintaining AI Agent Config"]
        for artifact in manifest["artifacts"]:
            if artifact["path"] == ".github/copilot-instructions.md":
                artifact.pop("hash", None)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        claude_content = audit.read_text(self.root / "CLAUDE.md")
        sections = audit._extract_sections(claude_content, manifest["copilot_sections"])
        (self.root / ".github/copilot-instructions.md").write_text(
            f"# Custom Title\n\n{audit.COPILOT_BANNER}\n\n{sections}\n",
            encoding="utf-8",
        )

        result = audit.audit(self.root)
        parity_errors = [
            finding
            for finding in result.findings
            if finding.check == "parity"
            and finding.severity == "ERROR"
            and finding.path == ".github/copilot-instructions.md"
        ]
        self.assertEqual([], parity_errors)


# ---------------------------------------------------------------------------
# Behavioral constraints tests
# ---------------------------------------------------------------------------


class BehavioralConstraintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_never_disable_present(self) -> None:
        result = audit.audit(self.root)
        behavioral_warnings = [f for f in result.findings if f.check == "behavioral" and "never" in f.message.lower()]
        self.assertEqual(0, len(behavioral_warnings))

    def test_never_disable_missing(self) -> None:
        (self.root / "CLAUDE.md").write_text(
            "# CLAUDE.md\n\n## Overview\n\nNo constraints here.\n\n"
            "## Maintaining AI Agent Config\n\nRun the generator.\n",
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "behavioral" and "never" in f.message.lower() for f in result.findings),
        )

    def test_workaround_rule_missing_and_present(self) -> None:
        body = (
            "# CLAUDE.md\n\n## Overview\n\nNever disable anything. Coverage 80%.\n\n"
            "## Maintaining AI Agent Config\n\nRun the generator.\n"
        )

        def workaround_warnings() -> list[audit.Finding]:
            return [
                f for f in audit.audit(self.root).findings if f.check == "behavioral" and "workarounds" in f.message
            ]

        (self.root / "CLAUDE.md").write_text(body, encoding="utf-8")
        self.assertEqual(1, len(workaround_warnings()))
        (self.root / "CLAUDE.md").write_text(
            body + "\nNever work around a failing check; finish the change instead.\n",
            encoding="utf-8",
        )
        self.assertEqual([], workaround_warnings())

    def test_quality_threshold_missing(self) -> None:
        (self.root / "CLAUDE.md").write_text(
            "# CLAUDE.md\n\n## Overview\n\nNo thresholds.\n\n"
            "## Maintaining AI Agent Config\n\nRun the generator.\n\n"
            "Never disable anything.\n",
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "behavioral" and "quality gate" in f.message.lower() for f in result.findings),
        )

    def test_quality_gate_requires_an_actual_gate(self) -> None:
        base = "# CLAUDE.md\n\n## Maintaining AI Agent Config\n\nRun the generator.\n\nNever disable anything.\n\n"

        def gate_warnings(text: str) -> list[audit.Finding]:
            (self.root / "CLAUDE.md").write_text(base + text + "\n", encoding="utf-8")
            return [
                f
                for f in audit.audit(self.root).findings
                if f.check == "behavioral" and "quality gate" in f.message.lower()
            ]

        for gate in (
            "Line coverage must stay at or above 85%.",
            "We require 90.5% branch coverage.",
            "Run `shellcheck --severity=warning` on every script.",
            "Treat warnings as errors.",
            "Build with TreatWarningsAsErrors enabled.",
            "Run eslint --max-warnings 0.",
            "Finish the change so every check passes.",
            "All required tests must pass before merging.",
        ):
            with self.subTest(gate=gate):
                self.assertEqual([], gate_warnings(gate))
        for not_a_gate in (
            "Answer 100% of questions politely.",
            "Coverage reports are published weekly.",
            "Checks run in CI.",
        ):
            with self.subTest(not_a_gate=not_a_gate):
                self.assertEqual(1, len(gate_warnings(not_a_gate)))


# ---------------------------------------------------------------------------
# Collision tests
# ---------------------------------------------------------------------------


class CollisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_user_authored_agents_md_collision(self) -> None:
        (self.root / "AGENTS.md").write_text("# My custom AGENTS.md\n\nUser content.\n", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "collision" and "AGENTS.md" in (f.path or "") for f in result.findings),
        )

    def test_user_authored_copilot_instructions_collision(self) -> None:
        (self.root / ".github/copilot-instructions.md").write_text(
            "# My custom instructions\n\nUser content.\n", encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check in ("collision", "parity") and f.severity == "ERROR" for f in result.findings),
        )


# ---------------------------------------------------------------------------
# Copilot configuration, provenance, and scope tests
# ---------------------------------------------------------------------------


class CopilotConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _manifest(self) -> dict[str, Any]:
        path = self.root / ".github/ai-config-manifest.json"
        manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return manifest

    def _write_manifest(self, manifest: dict[str, object]) -> None:
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

    def test_agent_frontmatter_target_and_tools_validated(self) -> None:
        path = self.root / ".github/agents/reviewer.agent.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\ndescription: Review changes\ntarget: invalid\ntools: [read, [edit]]\n---\n",
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(any(f.check == "copilot-agent" and "target must" in f.message for f in result.findings))
        self.assertTrue(any(f.check == "copilot-agent" and "tools must" in f.message for f in result.findings))

    def test_an_empty_tools_list_disables_every_tool_and_an_empty_value_is_an_error(self) -> None:
        # GitHub's custom-agents configuration reference: an empty list disables all tools, and an unset tools
        # property enables all of them. An empty value is neither: YAML reads it as unset, which enables every tool,
        # while its author may have meant none, so it stays an error that names both ways of saying it.
        empty = "tools must not be empty; write [] to disable every tool, or omit tools to enable every tool"
        cases = {
            "---\ndescription: d\ntools: []\n---\n": [],
            "---\ndescription: d\ntools: [] # none\n---\n": [],
            "---\ndescription: d\ntools:\n---\n": [(None, empty)],
            '---\ndescription: d\ntools: ""\n---\n': [(None, empty)],
            "---\ndescription: d\ntools: ''\n---\n": [(None, empty)],
            "---\ndescription: d\ntools: # none\n---\n": [(None, empty)],
        }
        agent = self.root / ".github/agents/demo.agent.md"
        agent.parent.mkdir(parents=True)
        for text, expected in cases.items():
            with self.subTest(text=text):
                agent.write_text(text, encoding="utf-8")
                found = audit._validate_agent(agent, self.root)
                self.assertEqual(expected, [(finding.line, finding.message) for finding in found])

    def test_an_empty_allowed_tools_list_is_an_error_like_an_empty_value(self) -> None:
        empty = [(None, "allowed-tools must not be empty")]
        cases = {
            "---\nname: demo\ndescription: d\nallowed-tools: []\n---\n": empty,
            "---\nname: demo\ndescription: d\nallowed-tools:\n---\n": empty,
            '---\nname: demo\ndescription: d\nallowed-tools: ""\n---\n': empty,
            "---\nname: demo\ndescription: d\nallowed-tools: # none\n---\n": empty,
            "---\nname: demo\ndescription: d\nallowed-tools: [Read]\n---\n": [],
            "---\nname: demo\ndescription: d\nallowed-tools: Read Grep\n---\n": [],
        }
        skill = self.root / ".github/skills/demo/SKILL.md"
        skill.parent.mkdir(parents=True)
        for text, expected in cases.items():
            with self.subTest(text=text):
                skill.write_text(text, encoding="utf-8")
                found = audit._validate_skill(skill, self.root)
                self.assertEqual(expected, [(finding.line, finding.message) for finding in found])

    def test_a_trailing_comment_is_not_part_of_a_plain_value(self) -> None:
        # YAML ends a plain scalar at a space followed by #, so the comment is never part of the value.
        cases = {
            "---\nname: demo # the directory's name\n---\n": "demo",
            "---\nname: see #42 # issue\n---\n": "see",
            "---\nname: a#b\n---\n": "a#b",
            "---\nname: # nothing\n---\n": "",
            "---\nname: *bold* text # kept as written\n---\n": "*bold* text",
            '---\nname: "a # b" # quoted\n---\n': "a # b",
            "---\nname: [a, b] # list\n---\n": ["a", "b"],
            "---\nname: > # folded\n  long\n  text\n---\n": "long text\n",
            "---\nname: # list follows\n  - a\n  - b\n---\n": ["a", "b"],
        }
        skill = self.root / ".github/skills/demo/SKILL.md"
        skill.parent.mkdir(parents=True)
        for text, expected in cases.items():
            with self.subTest(text=text):
                skill.write_text(text, encoding="utf-8")
                values, found = audit._frontmatter(skill)
                self.assertEqual(expected, values["name"])
                self.assertEqual([], found)
        agent = self.root / ".github/agents/demo.agent.md"
        agent.parent.mkdir(parents=True)
        agent.write_text(
            "---\ndescription: d # what it does\ntarget: vscode # editor only\ninfer: false # never\n"
            "modelPolicy: required # pinned\n---\n",
            encoding="utf-8",
        )
        self.assertEqual([], audit._validate_agent(agent, self.root))

    def test_frontmatter_the_shared_reader_would_accept_or_refuse_is_judged_line_by_line(self) -> None:
        # Pinned while _frontmatter keeps its own line walk beside ejryan89-skill-core's reader; see the comment above
        # it.
        unreadable = "Frontmatter must use key: value entries, block scalars, block lists, or nested mappings"
        mismatch = "Skill name must be lowercase hyphenated and match its directory"
        cases = {
            '---\nname: "demo"\ndescription: d\n---\n': [],
            "---\nname: 'demo' # quoted\ndescription: 'it''s'\n---\n": [],
            '---\nname: "dem\ndescription: d\n---\n': [
                (2, "Frontmatter entry cannot be read as YAML: name: a double-quoted value is not closed"),
                (None, mismatch),
            ],
            "---\nname: demo\ndescription: >-\n  long\n  text\n---\n": [],
            "---\nname: demo\ndescription: d\nmetadata:\n  author: x\n  tags:\n    - a\n---\n": [],
            "---\nname: demo\ndescription: d\nmetadata:\n    author: x\n  version: 1\n---\n": [(6, unreadable)],
            "---\nname: demo\ndescription: d\nlicense:\n  MIT\n---\n": [],
            "---\nname: demo\ndescription: d\n  more\n---\n": [],
            "---\nname: demo\ndescription: d\nlicense: MIT\n- x\n---\n": [(5, unreadable)],
            "---\nname: demo\ndescription: *bold* text\n---\n": [],
            "---\nname: demo\nname: demo\ndescription: d\n---\n": [(3, "Duplicate frontmatter key 'name'")],
            "---\nname: demo\ndescription: d\nallowed-tools: []\n---\n": [(None, "allowed-tools must not be empty")],
            "---\nname: demo\ndescription: d\nargument-hint: [pr] [flag]\n---\n": [],
        }
        skill = self.root / ".github/skills/demo/SKILL.md"
        skill.parent.mkdir(parents=True)
        for text, expected in cases.items():
            with self.subTest(text=text):
                skill.write_text(text, encoding="utf-8")
                found = audit._validate_skill(skill, self.root)
                self.assertEqual(expected, [(finding.line, finding.message) for finding in found])
        tools_shape = "tools must be a list of tool names or a comma-separated string"
        agents = {
            "---\ndescription: d\ntools: [read, edit]\n---\n": [],
            '---\ndescription: d\ntools: ["read", "edit"]\n---\n': [],
            "---\ndescription: d\ntools: ['read', 'custom-mcp/tool-1']\n---\n": [],
            "---\ndescription: d\ntools: [read, [edit]]\n---\n": [(None, tools_shape)],
            "---\ndescription: d\ntools:\n  - read\n  - 'edit'\n---\n": [],
            "---\ndescription: d\ntools:\n- read\n- edit\n---\n": [],
            "---\ndescription: d\ntools:\n  - read\n  edit\n---\n": [(5, unreadable), (None, tools_shape)],
            "---\ndescription: d\ntools:\n  - read: x\n---\n": [(None, tools_shape)],
            "---\ndescription: d\ntools:\n  - 'read\n---\n": [
                (3, "Frontmatter entry cannot be read as YAML: tools: a single-quoted value is not closed"),
                (None, tools_shape),
            ],
            "---\ndescription: d\ntools:\n  read: x\n---\n": [(None, tools_shape)],
            "---\ndescription: d\ntools:\n---\n": [
                (None, "tools must not be empty; write [] to disable every tool, or omit tools to enable every tool")
            ],
            "---\ndescription: d\ntools: []\n---\n": [],
            '---\ndescription: d\ntools: [read, ""]\n---\n': [(None, tools_shape)],
            "---\ndescription: d\ntarget: [vscode]\ninfer: [true]\n---\n": [
                (None, "target must be 'vscode' or 'github-copilot'"),
                (None, "infer must be a boolean"),
            ],
            "---\ndescription: d\nmcp-servers:\n  custom:\n    type: 'local'\n    tools: ['*']\n---\n": [],
        }
        agent = self.root / ".github/agents/demo.agent.md"
        agent.parent.mkdir(parents=True)
        for text, expected in agents.items():
            with self.subTest(text=text):
                agent.write_text(text, encoding="utf-8")
                found = audit._validate_agent(agent, self.root)
                self.assertEqual(expected, [(finding.line, finding.message) for finding in found])

    def test_quoted_scalars_lists_and_nested_mappings_are_read_as_yaml_reads_them(self) -> None:
        cases = {
            '---\nname: "de\\"mo"\n---\n': 'de"mo',
            "---\nname: 'it''s' # comment\n---\n": "it's",
            "---\nname: [a, 'b', \"c\"]\n---\n": ["a", "b", "c"],
            "---\nname: [a] [b]\n---\n": "[a] [b]",
            "---\nname:\n  - a\n\n  - 'b'\n---\n": ["a", "b"],
            "---\nname:\n- a\n# note\n- b\n---\n": ["a", "b"],
            "---\nname:\n  author: x\n  tags:\n    - a\n---\n": audit.NESTED,
            "---\nname:\n  - a: x\n    b: y\n---\n": audit.NESTED,
            "---\nname: d\n  more\n---\n": "d more",
            "---\nname:\n---\n": "",
        }
        skill = self.root / ".github/skills/demo/SKILL.md"
        skill.parent.mkdir(parents=True)
        for text, expected in cases.items():
            with self.subTest(text=text):
                skill.write_text(text, encoding="utf-8")
                values, found = audit._frontmatter(skill)
                self.assertEqual(expected, values["name"])
                self.assertEqual([], found)

    def test_block_scalars_are_read_as_yaml_reads_them(self) -> None:
        # Copilot's skill documentation and the Agent Skills specification define the frontmatter as YAML.
        cases = {
            "---\ndescription: >-\n  long\n  text\n---\n": ("long text", []),
            "---\ndescription: >\n  long\n\n  text\n---\n": ("long\ntext\n", []),
            "---\ndescription: >-\n  long\n    code\n  text\n---\n": ("long\n  code\ntext", []),
            "---\ndescription: |\n  long\n  text\n---\n": ("long\ntext\n", []),
            "---\ndescription: |+2\n    long\n\n---\n": ("  long\n\n", []),
            "---\ndescription: >2-\n   long\n---\n": (" long", []),
            "---\ndescription: >-\nname: demo\n---\n": ("", []),
            "---\ndescription: >-\n  long\nnot an entry\n---\n": (
                "long",
                [(4, "Frontmatter must use key: value entries, block scalars, block lists, or nested mappings")],
            ),
        }
        skill = self.root / ".github/skills/demo/SKILL.md"
        skill.parent.mkdir(parents=True)
        for text, (description, expected) in cases.items():
            with self.subTest(text=text):
                skill.write_text(text, encoding="utf-8")
                values, found = audit._frontmatter(skill)
                self.assertEqual(description, values["description"])
                self.assertEqual(expected, [(finding.line, finding.message) for finding in found])

    def test_an_empty_block_scalar_description_is_a_missing_description(self) -> None:
        skill = self.root / ".github/skills/demo/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: demo\ndescription: >-\n---\n", encoding="utf-8")
        found = audit._validate_skill(skill, self.root)
        self.assertEqual([(None, "Skill frontmatter requires description")], [(f.line, f.message) for f in found])

    def test_the_decision_keeping_the_bespoke_parser_is_stated_beside_it(self) -> None:
        source = Path(audit.__file__).read_text(encoding="utf-8")
        comment = source[: source.index("def _frontmatter(")].rsplit("\n\n", 1)[-1]
        self.assertIn("issues/27", comment)
        self.assertIn("line number", comment)

    def test_agent_filename_and_description_validated(self) -> None:
        path = self.root / ".claude/agents/bad name.txt"
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: bad\n---\n", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(any(f.check == "copilot-agent" and f.severity == "ERROR" for f in result.findings))

    def test_a_conforming_repository_with_yaml_frontmatter_forms_has_no_errors(self) -> None:
        skill = self.root / ".github/skills/demo/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            '---\nname: "demo"\ndescription: Demo.\nmetadata:\n  author: x\n  version: "1.0"\n---\n', encoding="utf-8"
        )
        agent = self.root / ".github/agents/demo.agent.md"
        agent.parent.mkdir(parents=True)
        agent.write_text("---\ndescription: Demo.\ntools:\n  - read\n  - edit\n---\n", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertEqual([], [f for f in result.findings if f.severity == "ERROR"])

    def test_code_review_collisions_are_errors(self) -> None:
        skill = self.root / ".github/skills/code-review/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: code-review\ndescription: Review.\n---\n", encoding="utf-8")
        agent = self.root / ".github/agents/code-review.agent.md"
        agent.parent.mkdir(parents=True)
        agent.write_text("---\ndescription: Review.\n---\n", encoding="utf-8")
        result = audit.audit(self.root)
        collisions = [f for f in result.findings if f.check == "collision" and "code-review" in f.message]
        self.assertEqual(2, len(collisions))

    def test_generated_copilot_projection_requires_manifest_ownership(self) -> None:
        projection = self.root / ".github/skills/project-skill/SKILL.md"
        projection.parent.mkdir(parents=True)
        projection.write_text(
            f"---\nname: project-skill\ndescription: Project skill.\n---\n\n<!-- {OWNERSHIP} -->\n",
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "provenance" and "not owned by the manifest" in f.message for f in result.findings)
        )

    def test_manifest_projection_requires_marker_and_hash(self) -> None:
        projection = self.root / ".github/agents/project-agent.agent.md"
        projection.parent.mkdir(parents=True)
        content = "---\ndescription: Project agent.\n---\n"
        projection.write_text(content, encoding="utf-8")
        manifest = self._manifest()
        manifest["artifacts"].append(
            {
                "path": ".github/agents/project-agent.agent.md",
                "hash": _content_hash(content),
            }
        )
        self._write_manifest(manifest)
        result = audit.audit(self.root)
        self.assertIn(
            ("ownership", "Generated file missing embedded ownership marker"),
            [(f.check, f.message) for f in result.findings if f.path == ".github/agents/project-agent.agent.md"],
        )

    def test_roles_distinguish_copilot_surfaces(self) -> None:
        manifest = self._manifest()
        manifest["surfaces"] = ["copilot_cli", "cloud_agent", "code_review"]
        manifest["runtimeRoles"] = {
            "copilot_cli": "full_local_host",
            "cloud_agent": "advisory_evidence_only",
            "code_review": "advisory_evidence_only",
        }
        self._write_manifest(manifest)
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "runtime-role" and "cloud_agent must declare" in f.message for f in result.findings)
        )

    def test_scope_is_derived_from_literal_generator_constants(self) -> None:
        script = self.root / ".github/scripts/ai_config.py"
        script.parent.mkdir(parents=True)
        script.write_text(
            "TARGET_RUNTIMES = ['claude', 'codex']\nTARGET_SURFACES = ['copilot_cli']\nTARGET_FEATURES = []\n",
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        _assert_scope_status(self, result, "independently-derived")
        manifest = self._manifest()
        manifest["surfaces"] = []
        self._write_manifest(manifest)
        result = audit.audit(self.root)
        _assert_scope_status(self, result, "independently-derived")
        self.assertTrue(any(f.check == "scope" and f.severity == "ERROR" for f in result.findings))

    def test_scope_without_manifest_or_generator_is_informational(self) -> None:
        (self.root / ".github/ai-config-manifest.json").unlink()
        result = audit.audit(self.root)
        self.assertEqual("conforming", result.authority)
        _assert_scope_status(self, result, "no-declared-scope")
        scope = [f for f in result.findings if f.check == "scope"]
        self.assertEqual(["INFO"], [f.severity for f in scope])
        self.assertIn("target-specific checks were not run", scope[0].message)

    def test_manifest_without_generator_still_warns_about_editable_scope(self) -> None:
        result = audit.audit(self.root)
        _assert_scope_status(self, result, "manifest-declared-only")
        self.assertEqual(
            [("WARNING", True)],
            [
                (f.severity, f.message.endswith("(manifest-declared-only)"))
                for f in result.findings
                if f.check == "scope"
            ],
        )

    def test_a_generator_scope_that_cannot_be_derived_reports_the_manifest_scope_and_why(self) -> None:
        generators = {
            "generator-scope-unreadable": "TARGET_RUNTIMES = [\n",
            "generator-scope-not-literal": "TARGET_RUNTIMES = list()\nTARGET_SURFACES = []\nTARGET_FEATURES = []\n",
            "generator-scope-invalid": "TARGET_RUNTIMES = 'claude'\nTARGET_SURFACES = []\nTARGET_FEATURES = []\n",
            "generator-scope-incomplete": "TARGET_RUNTIMES = ['claude']\n",
        }
        script = self.root / ".github/scripts/ai_config.py"
        script.parent.mkdir(parents=True)
        for reason, source in generators.items():
            with self.subTest(reason=reason):
                script.write_text(source, encoding="utf-8")
                result = audit.audit(self.root)
                _assert_scope_status(self, result, "manifest-declared-only")
                self.assertEqual(
                    [
                        (
                            "WARNING",
                            "Audit scope is manifest-declared only; editable manifest scope can suppress checks "
                            f"({reason})",
                        )
                    ],
                    [(f.severity, f.message) for f in result.findings if f.check == "scope"],
                )

    def test_scope_statuses_and_generator_failure_reasons_match_report_schema(self) -> None:
        documented = {"independently-derived", "manifest-declared-only", "no-declared-scope", "not-applicable"}
        text = (Path(audit.__file__).resolve().parent.parent / "references/report-schema.md").read_text(
            encoding="utf-8"
        )
        envelope = text[text.index("## Report Envelope") : text.index("## Finding Structure")]
        self.assertEqual(documented, set(re.findall(r"^- `([a-z-]+)`", envelope, re.MULTILINE)))
        self.assertEqual(documented, set(audit.SCOPE_STATUSES))
        bullet = next(line for line in envelope.split("\n- ") if line.startswith("`manifest-declared-only`"))
        reasons = {
            "manifest-declared-only",
            "generator-scope-unreadable",
            "generator-scope-not-literal",
            "generator-scope-invalid",
            "generator-scope-incomplete",
        }
        self.assertEqual(reasons, set(re.findall(r"`([a-z-]+)`", bullet.split(":", 1)[1])))

    def test_code_review_head_branch_trust_warning(self) -> None:
        manifest = self._manifest()
        manifest["surfaces"] = ["code_review"]
        self._write_manifest(manifest)
        result = audit.audit(self.root)
        self.assertTrue(any(f.check == "trust-boundary" and "PR head" in f.message for f in result.findings))


# ---------------------------------------------------------------------------
# Manifest safety tests
# ---------------------------------------------------------------------------


class ManifestSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".github").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_manifest_non_object_json_audit(self) -> None:
        """Non-object JSON manifest produces WARNING, no crash."""
        (self.root / ".github/ai-config-manifest.json").write_text("[]", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "authority" and "not a JSON object" in f.message for f in result.findings),
        )

    def test_manifest_wrong_generator_identity(self) -> None:
        """Manifest with wrong generatedBy does not count as conforming."""
        manifest = {
            "generatedBy": "other_gen.py",
            "canonicalSource": "CLAUDE.md",
            "schemaVersion": 1,
            "runtimes": ["claude"],
            "surfaces": [],
            "features": [],
            "mcp_servers": [],
            "artifacts": [],
        }
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        cls, _, _ = audit.classify_authority(self.root)
        self.assertNotEqual("conforming", cls)

    def test_manifest_invalid_schema_types(self) -> None:
        """Manifest with wrong field types produces schema WARNING."""
        manifest = {
            "generatedBy": "ai_config.py",
            "canonicalSource": "CLAUDE.md",
            "schemaVersion": "1",
            "runtimes": "claude",
            "surfaces": [],
            "features": [],
            "mcp_servers": [],
            "artifacts": [],
        }
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        cls, _, findings = audit.classify_authority(self.root)
        self.assertNotEqual("conforming", cls)
        self.assertTrue(
            any("schema" in f.message.lower() for f in findings),
        )

    def test_manifest_with_invalid_artifact_entries(self) -> None:
        """Non-dict artifact entries produce finding, no crash."""
        manifest = {
            "generatedBy": "ai_config.py",
            "canonicalSource": "CLAUDE.md",
            "schemaVersion": 1,
            "runtimes": ["claude"],
            "surfaces": [],
            "features": [],
            "mcp_servers": [],
            "artifacts": [42, "bad"],
        }
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        cls, _, findings = audit.classify_authority(self.root)
        self.assertNotEqual("conforming", cls)
        self.assertTrue(
            any("artifacts" in f.message for f in findings),
        )

    def test_manifest_missing_required_fields_audit(self) -> None:
        """Manifest without runtimes/surfaces/features is not conforming."""
        manifest = {
            "generatedBy": "ai_config.py",
            "canonicalSource": "CLAUDE.md",
            "schemaVersion": 1,
        }
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        cls, _, findings = audit.classify_authority(self.root)
        self.assertNotEqual("conforming", cls)
        schema_findings = [f for f in findings if "required" in f.message.lower()]
        self.assertTrue(len(schema_findings) > 0)


# ---------------------------------------------------------------------------
# Vocabulary validation tests
# ---------------------------------------------------------------------------


class VocabularyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_unknown_surface_in_manifest_is_error(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["copilot_local"]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "vocabulary" and f.severity == "ERROR" and "copilot_local" in f.message
                for f in result.findings
            ),
        )

    def test_unknown_runtime_in_manifest_is_error(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["runtimes"] = ["claude", "unknown_runtime"]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "vocabulary" and "unknown_runtime" in f.message for f in result.findings),
        )

    def test_unknown_mcp_target_in_manifest_is_error(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["mcp_servers"] = [
            {"name": "srv", "targets": ["bad_target"], "transport": "stdio"},
        ]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "vocabulary" and "bad_target" in f.message for f in result.findings),
        )

    def test_valid_vocabulary_passes(self) -> None:
        """Default conforming repo uses valid vocabulary — no vocabulary errors."""
        result = audit.audit(self.root)
        vocab_errors = [f for f in result.findings if f.check == "vocabulary"]
        self.assertEqual(0, len(vocab_errors))


# ---------------------------------------------------------------------------
# Output format tests
# ---------------------------------------------------------------------------


class OutputFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_markdown_output(self) -> None:
        result = audit.audit(self.root)
        md = audit.format_markdown(result)
        self.assertIn("## AI Config Audit", md)
        self.assertIn("conforming", md)

    def test_json_output(self) -> None:
        result = audit.audit(self.root)
        j = audit.format_json(result)
        data = json.loads(j)
        self.assertEqual("conforming", data["authority"])
        self.assertEqual(0, data["exitCode"])
        self.assertIsInstance(data["findings"], list)

    def test_reports_reject_an_undeclared_status_from_the_audit_engine(self) -> None:
        with mock.patch.object(audit, "check_scope_and_roles", return_value=("undeclared", [])):
            result = audit.audit(self.root)
        for formatter in (audit.format_markdown, audit.format_json):
            with (
                self.subTest(formatter=formatter.__name__),
                self.assertRaisesRegex(ValueError, "^Unknown scope status: 'undeclared'$"),
            ):
                formatter(result)

    def test_report_validation_reads_the_canonical_scope_statuses(self) -> None:
        result = audit.audit(self.root)
        for status in audit.SCOPE_STATUSES:
            remaining = tuple(value for value in audit.SCOPE_STATUSES if value != status)
            for formatter in (audit.format_markdown, audit.format_json):
                with (
                    self.subTest(status=status, formatter=formatter.__name__),
                    mock.patch.object(audit, "SCOPE_STATUSES", remaining),
                    self.assertRaisesRegex(ValueError, f"^Unknown scope status: {status!r}$"),
                ):
                    formatter(dataclasses.replace(result, scope_status=status))

    def test_markdown_summary_counts_each_severity_and_info_check(self) -> None:
        result = audit.AuditResult(
            repository="example",
            authority="conforming",
            scope_status="independently-derived",
            findings=[
                audit.Finding(severity="INFO", check="layering", path="AGENTS.md"),
                audit.Finding(severity="WARNING", check="mcp", path=".mcp.json"),
                audit.Finding(severity="INFO", check="inventory", path="CLAUDE.md"),
                audit.Finding(severity="ERROR", check="parity", path="AGENTS.md"),
                audit.Finding(severity="INFO", check="inventory", path="AGENTS.md"),
                audit.Finding(severity="INFO", check="limitation"),
            ],
        )
        summary = [line for line in audit.format_markdown(result).splitlines() if line.startswith("SUMMARY ")]
        self.assertEqual(
            [
                "SUMMARY ERROR 1",
                "SUMMARY WARNING 1",
                "SUMMARY INFO 4",
                "SUMMARY INFO inventory 2",
                "SUMMARY INFO layering 1",
                "SUMMARY INFO limitation 1",
            ],
            summary,
        )

    def test_markdown_result_line_precedes_the_summary(self) -> None:
        lines = audit.format_markdown(audit.audit(self.root)).splitlines()
        self.assertEqual(["RESULT COMPLIANT"], [line for line in lines if line.startswith("RESULT ")])
        self.assertEqual("SUMMARY ERROR 0", lines[lines.index("RESULT COMPLIANT") + 1])

    def test_markdown_summary_precedes_findings(self) -> None:
        md = audit.format_markdown(audit.audit(self.root))
        self.assertLess(md.index("SUMMARY ERROR"), md.index("### Findings"))
        self.assertRegex(md, r"(?m)^SUMMARY INFO inventory [1-9]\d*$")

    def test_markdown_summary_without_findings(self) -> None:
        result = audit.AuditResult(repository="example", authority="unconfigured")
        md = audit.format_markdown(result)
        self.assertEqual(
            ["SUMMARY ERROR 0", "SUMMARY WARNING 0", "SUMMARY INFO 0"],
            [line for line in md.splitlines() if line.startswith("SUMMARY ")],
        )
        self.assertIn("No findings.", md)

    def test_json_output_has_no_summary(self) -> None:
        data = json.loads(audit.format_json(audit.audit(self.root)))
        self.assertEqual(
            {"repository", "authority", "scopeStatus", "exitCode", "findings"},
            set(data),
        )
        self.assertNotIn("SUMMARY", audit.format_json(audit.audit(self.root)))

    def test_findings_sorted_deterministically(self) -> None:
        result = audit.audit(self.root)
        sorted_f = result.sorted_findings()
        for i in range(len(sorted_f) - 1):
            self.assertLessEqual(sorted_f[i].sort_key(), sorted_f[i + 1].sort_key())


# ---------------------------------------------------------------------------
# Read-only guarantee test
# ---------------------------------------------------------------------------


class ReadOnlyGuaranteeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_repo_unchanged_after_audit(self) -> None:
        """Capture file state before audit, verify unchanged after."""
        before: dict[str, str] = {}
        for f in self.root.rglob("*"):
            if f.is_file():
                rel = f.relative_to(self.root).as_posix()
                before[rel] = f.read_text(encoding="utf-8")

        audit.audit(self.root)

        after: dict[str, str] = {}
        for f in self.root.rglob("*"):
            if f.is_file():
                rel = f.relative_to(self.root).as_posix()
                after[rel] = f.read_text(encoding="utf-8")

        self.assertEqual(sorted(before.keys()), sorted(after.keys()), "Files created or deleted during audit")
        for rel in before:
            self.assertEqual(before[rel], after[rel], f"File modified during audit: {rel}")


class SchemaVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_schema_version_mismatch_not_conforming(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["schemaVersion"] = 999
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "authority" and "schema" in f.message.lower() for f in result.findings),
        )

    def test_artifact_non_string_path_caught(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["artifacts"].append({"path": 42})
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any("path must be a string" in f.message for f in result.findings),
        )

    def test_invalid_manifest_schema_blocks_conforming(self) -> None:
        """Manifest with bad schema + 2 other signals: manifest not counted."""
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        (root / ".github/scripts").mkdir(parents=True, exist_ok=True)
        claude_content = "# CLAUDE.md\n\n## Maintaining AI Agent Config\n\nRun the generator.\n"
        (root / "CLAUDE.md").write_text(claude_content, encoding="utf-8")
        (root / ".github/scripts/ai_config.py").write_text("# Reads CLAUDE.md\n", encoding="utf-8")
        manifest = {
            "generatedBy": "ai_config.py",
            "canonicalSource": "CLAUDE.md",
            "schemaVersion": 1,
            "runtimes": "not-a-list",
            "surfaces": [],
            "features": [],
            "mcp_servers": [],
            "artifacts": [],
        }
        (root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        cls, _, findings = audit.classify_authority(root)
        self.assertEqual("conforming", cls, "Should be conforming via 2 non-manifest signals")
        self.assertTrue(
            any("schema" in f.message.lower() for f in findings),
            "Should have schema warning",
        )
        temp.cleanup()

    def test_manifest_artifact_non_string_path_no_crash(self) -> None:
        """Artifact with non-string path produces finding, not crash."""
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["artifacts"].append({"path": 42})
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any("path must be a string" in f.message for f in result.findings),
        )


class EmptyManifestAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".github").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_empty_manifest_without_claude_md_not_conforming(self) -> None:
        manifest = {
            "generatedBy": "ai_config.py",
            "schemaVersion": 1,
            "canonicalSource": "CLAUDE.md",
            "runtimes": [],
            "surfaces": [],
            "features": [],
            "artifacts": [],
            "mcp_servers": [],
        }
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" for f in result.findings),
            [f.message for f in result.findings],
        )
        self.assertNotEqual(0, result.exit_code)

    def test_self_referencing_manifest_not_conforming(self) -> None:
        (self.root / "CLAUDE.md").write_text("# CLAUDE.md\n\n## Overview\n\nTest.\n", encoding="utf-8")
        manifest = {
            "generatedBy": "ai_config.py",
            "schemaVersion": 1,
            "canonicalSource": "CLAUDE.md",
            "runtimes": ["claude"],
            "surfaces": [],
            "features": [],
            "artifacts": [
                {"path": ".github/ai-config-manifest.json"},
            ],
            "mcp_servers": [],
        }
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" and "no derived" in f.message.lower() for f in result.findings),
            [f.message for f in result.findings],
        )

    def test_empty_artifacts_with_claude_md_not_conforming(self) -> None:
        (self.root / "CLAUDE.md").write_text("# CLAUDE.md\n\n## Overview\n\nTest.\n", encoding="utf-8")
        manifest = {
            "generatedBy": "ai_config.py",
            "schemaVersion": 1,
            "canonicalSource": "CLAUDE.md",
            "runtimes": ["claude"],
            "surfaces": [],
            "features": [],
            "artifacts": [],
            "mcp_servers": [],
        }
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" and "no derived" in f.message.lower() for f in result.findings),
            [f.message for f in result.findings],
        )


class JsonHashAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_hashless_json_artifact_blocks_authority(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["artifacts"].append({"path": ".mcp.json"})
        (self.root / ".mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        _, _, findings = audit.classify_authority(self.root)
        schema_errors = [f for f in findings if f.severity == "ERROR" and "hash" in f.message.lower()]
        self.assertTrue(schema_errors, [f.message for f in findings])


class BoolSchemaVersionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_bool_schema_version_not_conforming(self) -> None:
        manifest = json.loads(audit.read_text(self.root / ".github/ai-config-manifest.json"))
        manifest["schemaVersion"] = True
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" and "schema" in f.message.lower() for f in result.findings),
            [f.message for f in result.findings],
        )


class UnreadableArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_unreadable_artifact_produces_error_not_crash(self) -> None:
        (self.root / ".github/copilot-instructions.md").write_bytes(b"\x80\x81 invalid utf-8")
        result = audit.audit(self.root)
        error_findings = [
            f for f in result.findings if f.severity == "ERROR" and "could not be read" in f.message.lower()
        ]
        self.assertTrue(
            error_findings,
            [f.message for f in result.findings if f.severity == "ERROR"],
        )


class UnlistedGeneratedFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _orphans(self) -> list[str]:
        # An orphan finding always names its path; one that did not would be missing here and fail the comparison.
        return sorted(
            f.path
            for f in audit.audit(self.root).findings
            if f.check == "orphan" and "no longer listed" in f.message and f.path is not None
        )

    def test_listed_artifacts_are_not_orphans(self) -> None:
        self.assertEqual([], self._orphans())

    def test_marker_file_missing_from_manifest_is_orphan(self) -> None:
        workflow = self.root / ".github/workflows/ai-config-parity.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(f"# {OWNERSHIP}. Do not edit.\nname: AI Config Parity\n", encoding="utf-8")
        user_toml = self.root / ".codex/config.toml"
        user_toml.parent.mkdir(parents=True)
        user_toml.write_text('model = "x"\n', encoding="utf-8")
        self.assertEqual([".github/workflows/ai-config-parity.yml"], self._orphans())

    def test_unlisted_shim_with_canonical_skill_is_orphan_once(self) -> None:
        manifest_path = self.root / ".github/ai-config-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"] = [a for a in manifest["artifacts"] if a["path"] != ".agents/skills/demo/SKILL.md"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        stale = self.root / ".agents/skills/gone/SKILL.md"
        stale.parent.mkdir(parents=True)
        stale.write_text(f"---\nname: gone\ndescription: Gone.\n---\n\n<!-- {OWNERSHIP} -->\n", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertEqual([".agents/skills/demo/SKILL.md"], self._orphans())
        gone = [f for f in result.findings if f.path == ".agents/skills/gone/SKILL.md" and f.check == "orphan"]
        self.assertEqual(["Skill shim has no matching canonical skill"], [f.message for f in gone])

    def test_no_manifest_reports_no_unlisted_files(self) -> None:
        (self.root / ".github/ai-config-manifest.json").unlink()
        self.assertEqual([], self._orphans())


class CodeReviewSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)
        manifest_path = self.root / ".github/ai-config-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["surfaces"] = ["code_review"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _review_findings(self) -> list[tuple[str, str]]:
        return [
            (f.severity, f.message)
            for f in audit.audit(self.root).findings
            if f.check == "layering" and f.message.startswith("Code review:")
        ]

    def test_claude_md_and_copilot_instructions_are_effective_sources(self) -> None:
        self.assertEqual(
            [("INFO", "Code review: effective sources: CLAUDE.md, .github/copilot-instructions.md")],
            self._review_findings(),
        )

    def test_a_redirect_only_agents_md_leaves_code_review_the_claude_md_it_names(self) -> None:
        (self.root / ".github/copilot-instructions.md").unlink()
        self.assertEqual([("INFO", "Code review: effective sources: CLAUDE.md")], self._review_findings())

    def test_gemini_md_and_review_md_are_effective_sources(self) -> None:
        (self.root / ".github/copilot-instructions.md").unlink()
        (self.root / "GEMINI.md").write_text("# Rules\n", encoding="utf-8")
        (self.root / "REVIEW.md").write_text("# Review\n", encoding="utf-8")
        self.assertEqual(
            [("INFO", "Code review: effective sources: CLAUDE.md, GEMINI.md, REVIEW.md")], self._review_findings()
        )

    def test_a_redirect_only_agents_md_with_nothing_it_names_gives_code_review_nothing(self) -> None:
        (self.root / ".github/copilot-instructions.md").unlink()
        (self.root / "CLAUDE.md").unlink()
        layering = audit.check_instruction_layering(self.root, {"surfaces": ["code_review"]})
        self.assertEqual(
            [
                (
                    "ERROR",
                    "Code review: no project instructions it can read — no CLAUDE.md, GEMINI.md, REVIEW.md, or "
                    "Copilot instructions, and AGENTS.md only redirects to CLAUDE.md",
                )
            ],
            [(f.severity, f.message) for f in layering if f.message.startswith("Code review:")],
        )

    def test_path_specific_and_user_agents_md_count(self) -> None:
        (self.root / ".github/copilot-instructions.md").unlink()
        (self.root / "AGENTS.md").write_text("# Rules\n\nUse tabs.\n", encoding="utf-8")
        instructions = self.root / ".github/instructions/api.instructions.md"
        instructions.parent.mkdir(parents=True)
        instructions.write_text("Prefer async.\n", encoding="utf-8")
        self.assertEqual(
            [("INFO", "Code review: effective sources: AGENTS.md, CLAUDE.md, .github/instructions")],
            self._review_findings(),
        )


class LimitationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _limitations(self) -> list[tuple[str | None, str]]:
        return sorted((f.path, f.message) for f in audit.audit(self.root).findings if f.check == "limitation")

    def test_conforming_fixture_hits_only_the_old_manifest_limitation(self) -> None:
        self.assertEqual(
            [
                (
                    ".github/ai-config-manifest.json",
                    "Manifest predates copilot_sections; Copilot parity assumes the default sections",
                )
            ],
            self._limitations(),
        )

    def test_each_applicable_limitation_is_reported(self) -> None:
        manifest_path = self.root / ".github/ai-config-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["copilot_sections"] = list(audit.DEFAULT_COPILOT_SECTIONS)
        manifest["artifacts"].append({"path": ".codex/config.toml"})
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        codex = self.root / ".codex/config.toml"
        codex.parent.mkdir(parents=True)
        codex.write_text(f"# {OWNERSHIP}\n", encoding="utf-8")
        projection = self.root / ".github/skills/demo/SKILL.md"
        projection.parent.mkdir(parents=True)
        projection.write_text(f"---\nname: demo\ndescription: Demo.\n---\n\n<!-- {OWNERSHIP} -->\n", encoding="utf-8")
        instructions = self.root / ".github/instructions/api.instructions.md"
        instructions.parent.mkdir(parents=True)
        instructions.write_text("Prefer async.\n", encoding="utf-8")
        nested = self.root / "tools/.mcp.json"
        nested.parent.mkdir(parents=True)
        nested.write_text("{}\n", encoding="utf-8")
        self.assertEqual(
            [
                (
                    ".codex/config.toml",
                    "Only the ownership marker is checked; content drift inside this file is not detected",
                ),
                (".github/instructions", "Path-specific instructions are not checked for contradictions or overlap"),
                (
                    ".github/skills/demo/SKILL.md",
                    "Generated Copilot projection is provenance-checked only, not reconstructed",
                ),
                ("tools/.mcp.json", "Nested .mcp.json files are not validated"),
            ],
            self._limitations(),
        )

    def test_limitations_never_change_the_exit_code(self) -> None:
        self.assertEqual(0, audit.audit(self.root).exit_code)


# ---------------------------------------------------------------------------
# Finding sequences of the MCP and instruction-layering checks
# ---------------------------------------------------------------------------

REDIRECTING_AGENTS = "Read and follow `CLAUDE.md` as the authoritative source.\n"
PLAIN_AGENTS = "# Agents\n\nUse tabs.\n"
EVERY_SURFACE = ["copilot_cli", "jetbrains", "cloud_agent", "code_review", "vscode"]
# Exception text varies by Python version, so a parse failure is compared by its prefix.
PARSE_PREFIXES = ("Could not read or parse:", "Invalid TOML:")

MCP_SCENARIOS: dict[str, tuple[dict[str, str], dict[str, Any]]] = {
    "every_source": (
        {
            ".mcp.json": '{"mcpServers": {"alpha": {"command": "a", "tools": ["read"]}, "beta": "text", "gamma": {}}}',
            ".github/mcp.json": '{"mcpServers": {"alpha": {"command": "b"}}}',
            ".vscode/mcp.json": '{"servers": {"alpha": {"command": "a"}}}',
            ".codex/config.toml": (
                '[mcp_servers]\nflag = true\n\n[mcp_servers.alpha]\ncommand = "a"\n\n'
                '[mcp_servers.web]\nurl = "https://example.test/mcp"\nenv = { A = "1" }\nenv_vars = ["B"]\n'
            ),
        },
        {
            "surfaces": ["code_review"],
            "mcp_servers": [
                {"name": "odd", "transport": "carrier", "targets": ["claude"]},
                {"name": "local-only", "transport": "local", "targets": ["claude", "copilot_local"]},
                {"transport": "stdio", "targets": ["copilot_repository"]},
            ],
        },
    ),
    "top_level_lists": (
        {".mcp.json": "[]", ".github/mcp.json": '{"other": {}}', ".vscode/mcp.json": "[]"},
        {},
    ),
    "wrong_wrappers": (
        {".mcp.json": "{bad", ".github/mcp.json": '{"mcpServers": []}', ".vscode/mcp.json": '{"mcpServers": {}}'},
        {},
    ),
    "vscode_servers_not_an_object": (
        {".vscode/mcp.json": '{"servers": []}', ".codex/config.toml": "invalid [[ toml"},
        {},
    ),
    "vscode_unparseable": ({".vscode/mcp.json": "{bad"}, {}),
    "nothing": ({}, {}),
}

LAYERING_SCENARIOS: dict[str, tuple[dict[str, str], dict[str, Any]]] = {
    "everything": (
        {
            "CLAUDE.md": "# CLAUDE.md\n",
            "AGENTS.md": REDIRECTING_AGENTS,
            "AGENTS.override.md": "Override.\n",
            "sub/AGENTS.override.md": "Nested override.\n",
            "sub/AGENTS.md": PLAIN_AGENTS,
            ".github/copilot-instructions.md": "# Copilot\n",
            "GEMINI.md": "# Gemini\n",
            ".github/skills/demo/SKILL.md": "---\nname: demo\n---\n",
        },
        {"runtimes": ["codex"], "surfaces": EVERY_SURFACE},
    ),
    "redirect_with_nested_redirect": (
        {"CLAUDE.md": "# CLAUDE.md\n", "AGENTS.md": REDIRECTING_AGENTS, "sub/AGENTS.md": REDIRECTING_AGENTS},
        {"runtimes": ["codex"], "surfaces": EVERY_SURFACE},
    ),
    "plain_agents_and_path_instructions": (
        {"AGENTS.md": PLAIN_AGENTS, ".github/instructions/api.instructions.md": "Prefer async.\n"},
        {"runtimes": ["codex"], "surfaces": EVERY_SURFACE},
    ),
    "path_instructions_only": (
        {".github/instructions/api.instructions.md": "Prefer async.\n"},
        {"runtimes": [], "surfaces": ["copilot_cli", "copilot_app"]},
    ),
    "claude_only": (
        {"CLAUDE.md": "# CLAUDE.md\n"},
        {"runtimes": ["codex"], "surfaces": EVERY_SURFACE},
    ),
    "gemini_only": (
        {"GEMINI.md": "# Gemini\n"},
        {"runtimes": ["codex"], "surfaces": EVERY_SURFACE},
    ),
    "nothing": ({}, {"runtimes": ["codex"], "surfaces": EVERY_SURFACE}),
    "copilot_app_only": ({"CLAUDE.md": "# CLAUDE.md\n"}, {"runtimes": ["claude"], "surfaces": ["copilot_app"]}),
    "no_targets": ({"CLAUDE.md": "# CLAUDE.md\n"}, {"runtimes": [], "surfaces": []}),
}

MCP_EXPECTED: dict[str, list[tuple[str, str, str | None, str]]] = {
    "every_source": [
        ("WARNING", "mcp", ".mcp.json", "Server 'beta': entry must be an object, got str"),
        ("ERROR", "mcp", ".mcp.json", "Server 'gamma': must have 'command' (STDIO/local) or 'url' (HTTP/SSE)"),
        (
            "ERROR",
            "mcp",
            ".github/mcp.json",
            "Duplicate server name 'alpha' — .mcp.json takes precedence, making .github/mcp.json entry unreachable",
        ),
        ("WARNING", "mcp", ".codex/config.toml", "Server 'flag': entry must be a table, got bool"),
        (
            "ERROR",
            "mcp",
            ".codex/config.toml",
            "Server 'web': env is STDIO-only, must not be present on HTTP transport",
        ),
        (
            "ERROR",
            "mcp",
            ".codex/config.toml",
            "Server 'web': env_vars is STDIO-only, must not be present on HTTP transport",
        ),
        ("ERROR", "mcp", None, "Server 'odd': unknown transport 'carrier'"),
        ("ERROR", "mcp", None, "Server 'local-only': transport 'local' not supported for target 'claude'"),
        (
            "ERROR",
            "mcp",
            ".mcp.json",
            "Server 'alpha': copilot_local.tools allowlist cannot be enforced in shared .mcp.json",
        ),
        (
            "WARNING",
            "mcp",
            None,
            "Copilot repository MCP (cloud agent/code review) configured "
            "via repository settings — cannot validate statically",
        ),
        (
            "WARNING",
            "mcp",
            None,
            "Code-review tool set derived from repository allowlist intersected "
            "with readOnlyHint: true — cannot verify tool annotations statically",
        ),
        ("WARNING", "mcp", None, "Server 'alpha': connection fields differ between .mcp.json and .github/mcp.json"),
    ],
    "top_level_lists": [
        ("ERROR", "mcp", ".mcp.json", "Top-level value must be an object"),
        ("WARNING", "mcp", ".github/mcp.json", "'mcpServers' key not found"),
        ("ERROR", "mcp", ".vscode/mcp.json", "Top-level value must be an object"),
    ],
    "wrong_wrappers": [
        ("ERROR", "mcp", ".mcp.json", "Could not read or parse:"),
        ("ERROR", "mcp", ".github/mcp.json", "'mcpServers' must be an object"),
        ("ERROR", "mcp", ".vscode/mcp.json", "VS Code MCP must use 'servers' wrapper (not 'mcpServers')"),
    ],
    "vscode_servers_not_an_object": [
        ("ERROR", "mcp", ".vscode/mcp.json", "'servers' must be an object"),
        ("ERROR", "mcp", ".codex/config.toml", "Invalid TOML:"),
    ],
    "vscode_unparseable": [
        ("ERROR", "mcp", ".vscode/mcp.json", "Could not read or parse:"),
    ],
    "nothing": [],
}
LAYERING_EXPECTED: dict[str, list[tuple[str, str, str | None, str]]] = {
    "everything": [
        (
            "ERROR",
            "layering",
            "AGENTS.override.md",
            "AGENTS.override.md masks the generated AGENTS.md adapter "
            "— Codex will not load CLAUDE.md through the adapter",
        ),
        (
            "WARNING",
            "layering",
            "sub/AGENTS.override.md",
            "Nested AGENTS.override.md takes precedence over AGENTS.md in this "
            "subtree — review for conflicting guidance with the root adapter",
        ),
        (
            "INFO",
            "layering",
            None,
            "Copilot CLI/app: effective sources: CLAUDE.md, AGENTS.md, .github/copilot-instructions.md, GEMINI.md",
        ),
        (
            "WARNING",
            "layering",
            None,
            "Copilot CLI/app folder trust status cannot be determined "
            "statically — .mcp.json silently skipped in untrusted directories",
        ),
        ("INFO", "layering", None, "JetBrains: .github/copilot-instructions.md available"),
        ("INFO", "layering", None, "Cloud agent: AGENTS.md adapter redirects to CLAUDE.md"),
        (
            "WARNING",
            "layering",
            "sub/AGENTS.md",
            "Nested AGENTS.md supersedes root adapter for cloud agent sessions in this subtree",
        ),
        (
            "INFO",
            "layering",
            None,
            "Code review: effective sources: CLAUDE.md, GEMINI.md, .github/copilot-instructions.md",
        ),
        ("WARNING", "layering", None, "Code-review custom-instructions enablement cannot be verified statically"),
        (
            "WARNING",
            "trust-boundary",
            None,
            "Copilot code review loads instructions, agents, and skills from the PR head; "
            "this is advisory context, not a trusted-base or trusted-ref review contract",
        ),
        (
            "INFO",
            "layering",
            ".github/skills",
            "Code review can use relevant .github/skills entries; .claude/skills "
            "and .agents/skills are not its documented automatic skill location",
        ),
        (
            "WARNING",
            "runtime",
            None,
            "Copilot repository settings, organization policy, authentication, model availability, "
            "runtime enablement, and actual operational use cannot be verified statically",
        ),
        (
            "WARNING",
            "layering",
            None,
            "VS Code instruction settings (chat.useClaudeMdFile, chat.useAgentsMdFile, "
            "useInstructionFiles, includeApplyingInstructions) cannot be verified statically",
        ),
    ],
    "redirect_with_nested_redirect": [
        ("INFO", "layering", None, "Codex: AGENTS.md adapter redirects to CLAUDE.md"),
        ("INFO", "layering", None, "Copilot CLI/app: effective sources: CLAUDE.md, AGENTS.md"),
        (
            "WARNING",
            "layering",
            None,
            "Copilot CLI/app folder trust status cannot be determined "
            "statically — .mcp.json silently skipped in untrusted directories",
        ),
        (
            "ERROR",
            "layering",
            None,
            "JetBrains: no .github/copilot-instructions.md or path-specific "
            "instructions — JetBrains cannot load CLAUDE.md directly",
        ),
        ("INFO", "layering", None, "Cloud agent: AGENTS.md adapter redirects to CLAUDE.md"),
        (
            "WARNING",
            "layering",
            "sub/AGENTS.md",
            "Nested AGENTS.md supersedes root adapter for cloud agent sessions in this subtree",
        ),
        ("INFO", "layering", None, "Code review: effective sources: CLAUDE.md"),
        ("WARNING", "layering", None, "Code-review custom-instructions enablement cannot be verified statically"),
        (
            "WARNING",
            "trust-boundary",
            None,
            "Copilot code review loads instructions, agents, and skills from the PR head; "
            "this is advisory context, not a trusted-base or trusted-ref review contract",
        ),
        (
            "WARNING",
            "runtime",
            None,
            "Copilot repository settings, organization policy, authentication, model availability, "
            "runtime enablement, and actual operational use cannot be verified statically",
        ),
        (
            "WARNING",
            "layering",
            None,
            "VS Code instruction settings (chat.useClaudeMdFile, chat.useAgentsMdFile, "
            "useInstructionFiles, includeApplyingInstructions) cannot be verified statically",
        ),
    ],
    "plain_agents_and_path_instructions": [
        ("WARNING", "layering", None, "Codex: non-redirecting AGENTS.md — CLAUDE.md not loaded through adapter"),
        ("INFO", "layering", None, "Copilot CLI/app: effective sources: AGENTS.md, .github/instructions"),
        (
            "WARNING",
            "layering",
            None,
            "Copilot CLI/app folder trust status cannot be determined "
            "statically — .mcp.json silently skipped in untrusted directories",
        ),
        ("INFO", "layering", None, "JetBrains: path-specific instructions only (no copilot-instructions.md)"),
        ("INFO", "layering", None, "Cloud agent: non-redirecting AGENTS.md — CLAUDE.md not loaded directly"),
        ("INFO", "layering", None, "Code review: effective sources: AGENTS.md, .github/instructions"),
        ("WARNING", "layering", None, "Code-review custom-instructions enablement cannot be verified statically"),
        (
            "WARNING",
            "trust-boundary",
            None,
            "Copilot code review loads instructions, agents, and skills from the PR head; "
            "this is advisory context, not a trusted-base or trusted-ref review contract",
        ),
        (
            "WARNING",
            "runtime",
            None,
            "Copilot repository settings, organization policy, authentication, model availability, "
            "runtime enablement, and actual operational use cannot be verified statically",
        ),
        (
            "WARNING",
            "layering",
            None,
            "VS Code instruction settings (chat.useClaudeMdFile, chat.useAgentsMdFile, "
            "useInstructionFiles, includeApplyingInstructions) cannot be verified statically",
        ),
    ],
    "path_instructions_only": [
        ("INFO", "layering", None, "Copilot CLI/app: effective sources: .github/instructions"),
        (
            "WARNING",
            "layering",
            None,
            "Copilot CLI/app folder trust status cannot be determined "
            "statically — .mcp.json silently skipped in untrusted directories",
        ),
        (
            "WARNING",
            "runtime",
            None,
            "Copilot repository settings, organization policy, authentication, model availability, "
            "runtime enablement, and actual operational use cannot be verified statically",
        ),
    ],
    "claude_only": [
        (
            "WARNING",
            "layering",
            None,
            "Codex: no AGENTS.md, and .codex/config.toml does not name CLAUDE.md in project_doc_fallback_filenames "
            "— Codex loads CLAUDE.md only when a user's own configuration names it",
        ),
        ("INFO", "layering", None, "Copilot CLI/app: effective sources: CLAUDE.md"),
        (
            "WARNING",
            "layering",
            None,
            "Copilot CLI/app folder trust status cannot be determined "
            "statically — .mcp.json silently skipped in untrusted directories",
        ),
        (
            "ERROR",
            "layering",
            None,
            "JetBrains: no .github/copilot-instructions.md or path-specific "
            "instructions — JetBrains cannot load CLAUDE.md directly",
        ),
        ("INFO", "layering", None, "Cloud agent: no AGENTS.md, CLAUDE.md selected directly"),
        ("INFO", "layering", None, "Code review: effective sources: CLAUDE.md"),
        ("WARNING", "layering", None, "Code-review custom-instructions enablement cannot be verified statically"),
        (
            "WARNING",
            "trust-boundary",
            None,
            "Copilot code review loads instructions, agents, and skills from the PR head; "
            "this is advisory context, not a trusted-base or trusted-ref review contract",
        ),
        (
            "WARNING",
            "runtime",
            None,
            "Copilot repository settings, organization policy, authentication, model availability, "
            "runtime enablement, and actual operational use cannot be verified statically",
        ),
        (
            "WARNING",
            "layering",
            None,
            "VS Code instruction settings (chat.useClaudeMdFile, chat.useAgentsMdFile, "
            "useInstructionFiles, includeApplyingInstructions) cannot be verified statically",
        ),
    ],
    "gemini_only": [
        (
            "ERROR",
            "layering",
            None,
            "Codex: no AGENTS.override.md, AGENTS.md, or CLAUDE.md — no instructions available",
        ),
        ("INFO", "layering", None, "Copilot CLI/app: effective sources: GEMINI.md"),
        (
            "WARNING",
            "layering",
            None,
            "Copilot CLI/app folder trust status cannot be determined "
            "statically — .mcp.json silently skipped in untrusted directories",
        ),
        (
            "ERROR",
            "layering",
            None,
            "JetBrains: no .github/copilot-instructions.md or path-specific "
            "instructions — JetBrains cannot load CLAUDE.md directly",
        ),
        ("INFO", "layering", None, "Cloud agent: no AGENTS.md or CLAUDE.md, GEMINI.md selected as alternative"),
        ("INFO", "layering", None, "Code review: effective sources: GEMINI.md"),
        ("WARNING", "layering", None, "Code-review custom-instructions enablement cannot be verified statically"),
        (
            "WARNING",
            "trust-boundary",
            None,
            "Copilot code review loads instructions, agents, and skills from the PR head; "
            "this is advisory context, not a trusted-base or trusted-ref review contract",
        ),
        (
            "WARNING",
            "runtime",
            None,
            "Copilot repository settings, organization policy, authentication, model availability, "
            "runtime enablement, and actual operational use cannot be verified statically",
        ),
        (
            "WARNING",
            "layering",
            None,
            "VS Code instruction settings (chat.useClaudeMdFile, chat.useAgentsMdFile, "
            "useInstructionFiles, includeApplyingInstructions) cannot be verified statically",
        ),
    ],
    "nothing": [
        (
            "ERROR",
            "layering",
            None,
            "Codex: no AGENTS.override.md, AGENTS.md, or CLAUDE.md — no instructions available",
        ),
        ("ERROR", "layering", None, "Copilot CLI/app: no instruction sources available"),
        (
            "WARNING",
            "layering",
            None,
            "Copilot CLI/app folder trust status cannot be determined "
            "statically — .mcp.json silently skipped in untrusted directories",
        ),
        (
            "ERROR",
            "layering",
            None,
            "JetBrains: no .github/copilot-instructions.md or path-specific "
            "instructions — JetBrains cannot load CLAUDE.md directly",
        ),
        ("ERROR", "layering", None, "Cloud agent: no AGENTS.md, CLAUDE.md, or GEMINI.md — no instructions available"),
        (
            "ERROR",
            "layering",
            None,
            "Code review: no project instructions it can read — no CLAUDE.md, GEMINI.md, REVIEW.md, or Copilot "
            "instructions, and AGENTS.md is absent",
        ),
        ("WARNING", "layering", None, "Code-review custom-instructions enablement cannot be verified statically"),
        (
            "WARNING",
            "trust-boundary",
            None,
            "Copilot code review loads instructions, agents, and skills from the PR head; "
            "this is advisory context, not a trusted-base or trusted-ref review contract",
        ),
        (
            "WARNING",
            "runtime",
            None,
            "Copilot repository settings, organization policy, authentication, model availability, "
            "runtime enablement, and actual operational use cannot be verified statically",
        ),
        (
            "WARNING",
            "layering",
            None,
            "VS Code instruction settings (chat.useClaudeMdFile, chat.useAgentsMdFile, "
            "useInstructionFiles, includeApplyingInstructions) cannot be verified statically",
        ),
    ],
    "copilot_app_only": [
        ("INFO", "layering", None, "Copilot CLI/app: effective sources: CLAUDE.md"),
        (
            "WARNING",
            "layering",
            None,
            "Copilot CLI/app folder trust status cannot be determined "
            "statically — .mcp.json silently skipped in untrusted directories",
        ),
        (
            "WARNING",
            "runtime",
            None,
            "Copilot repository settings, organization policy, authentication, model availability, "
            "runtime enablement, and actual operational use cannot be verified statically",
        ),
    ],
    "no_targets": [],
}


# The exact findings, in order, of the MCP and instruction-layering checks for each branch they take.


def sequence_rows(
    check: Callable[[Path, dict[str, Any]], list[audit.Finding]], files: dict[str, str], manifest: dict[str, Any]
) -> list[tuple[str, str, str | None, str]]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for relative, text in files.items():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(text, encoding="utf-8")
        return [
            (f.severity, f.check, f.path, next((p for p in PARSE_PREFIXES if f.message.startswith(p)), f.message))
            for f in check(root, manifest)
        ]


def test_every_mcp_scenario_has_its_expected_findings() -> None:
    assert set(MCP_SCENARIOS) == set(MCP_EXPECTED)


@pytest.mark.parametrize("name", list(MCP_SCENARIOS))
def test_mcp_findings_and_their_order(name: str) -> None:
    files, manifest = MCP_SCENARIOS[name]
    assert MCP_EXPECTED[name] == sequence_rows(audit.check_mcp, files, manifest)


def test_every_layering_scenario_has_its_expected_findings() -> None:
    assert set(LAYERING_SCENARIOS) == set(LAYERING_EXPECTED)


@pytest.mark.parametrize("name", list(LAYERING_SCENARIOS))
def test_instruction_layering_findings_and_their_order(name: str) -> None:
    files, manifest = LAYERING_SCENARIOS[name]
    assert LAYERING_EXPECTED[name] == sequence_rows(audit.check_instruction_layering, files, manifest)


# ---------------------------------------------------------------------------
# Results of the authority classification and parity checks
# ---------------------------------------------------------------------------

MANIFEST_PATH = ".github/ai-config-manifest.json"
UNDECODABLE = b"\x80\x81 invalid utf-8"
PLAIN_CLAUDE = "# CLAUDE.md\n\nUse tabs.\n"
MAINTAINED_CLAUDE = "# CLAUDE.md\n\n## Maintaining AI Agent Config\n\nRegenerate.\n"
GENERATOR_NAMING_CLAUDE = "SOURCE = 'CLAUDE.md'\n"
PARITY_WORKFLOW = "steps:\n  - run: python .github/scripts/ai_config.py --check\n"


def _manifest(**overrides: Any) -> str:
    manifest = {
        "generatedBy": "ai_config.py",
        "schemaVersion": 1,
        "canonicalSource": "CLAUDE.md",
        "runtimes": [],
        "surfaces": [],
        "features": [],
        "mcp_servers": [],
        "artifacts": [{"path": "AGENTS.md"}],
    }
    manifest.update(overrides)
    return json.dumps({key: value for key, value in manifest.items() if value is not None})


AUTHORITY_SCENARIOS: dict[str, dict[str, str | bytes]] = {
    "nothing": {},
    "manifest_with_every_signal": {
        MANIFEST_PATH: _manifest(),
        "CLAUDE.md": MAINTAINED_CLAUDE,
        ".github/scripts/ai_config.py": GENERATOR_NAMING_CLAUDE,
        ".github/workflows/parity.yml": PARITY_WORKFLOW,
        ".github/workflows/second.yml": PARITY_WORKFLOW,
    },
    "manifest_and_plain_claude": {MANIFEST_PATH: _manifest(), "CLAUDE.md": PLAIN_CLAUDE},
    "manifest_not_an_object": {MANIFEST_PATH: "[]"},
    "manifest_malformed": {MANIFEST_PATH: "{bad", "CLAUDE.md": PLAIN_CLAUDE},
    "manifest_undecodable": {MANIFEST_PATH: UNDECODABLE},
    "manifest_schema_errors": {
        MANIFEST_PATH: _manifest(schemaVersion="1", runtimes=None, surfaces="x", features=[1], mcp_servers=None),
        "CLAUDE.md": MAINTAINED_CLAUDE,
    },
    "manifest_canonical_missing": {
        MANIFEST_PATH: _manifest(),
        "scripts/ai_config.py": GENERATOR_NAMING_CLAUDE,
        ".github/workflows/parity.yaml": PARITY_WORKFLOW,
    },
    "manifest_without_derived_artifacts": {
        MANIFEST_PATH: _manifest(artifacts=[{"path": MANIFEST_PATH}]),
        "CLAUDE.md": PLAIN_CLAUDE,
    },
    "generator_declares_alternative_source": {
        MANIFEST_PATH: _manifest(canonicalSource="AGENTS.md"),
        "CLAUDE.md": MAINTAINED_CLAUDE,
    },
    "other_generator_declares_alternative_source": {
        MANIFEST_PATH: _manifest(generatedBy="other.py", canonicalSource="AGENTS.md"),
        "CLAUDE.md": MAINTAINED_CLAUDE,
    },
    "other_generator_declares_claude": {
        MANIFEST_PATH: _manifest(generatedBy="other.py"),
        "CLAUDE.md": MAINTAINED_CLAUDE,
    },
    "generator_without_canonical_source": {MANIFEST_PATH: _manifest(canonicalSource=None)},
    "claude_undecodable": {"CLAUDE.md": UNDECODABLE},
    "signals_that_do_not_count": {
        ".github/scripts/ai_config.py": "SOURCE = 'AGENTS.md'\n",
        "scripts/ai_config.py": UNDECODABLE,
        ".github/workflows/parity.yml": "steps:\n  - run: python ai_config.py --write\n",
        ".github/workflows/other.yaml": UNDECODABLE,
    },
    "generator_and_ci_without_claude": {
        ".github/scripts/ai_config.py": UNDECODABLE,
        "scripts/ai_config.py": GENERATOR_NAMING_CLAUDE,
        ".github/workflows/build.yml": "steps:\n  - run: make\n",
        ".github/workflows/parity.yaml": PARITY_WORKFLOW,
    },
    "ci_only": {".github/workflows/parity.yml": PARITY_WORKFLOW},
}

PARITY_CLAUDE = (
    "# CLAUDE.md\n\n## Overview\n\nA test repo.\n\n## Formatting Rules\n\nUse tabs.\n\n"
    "## CI / Quality Gates\n\nNever disable linting rules.\n"
)
COPILOT_HEADER = f"# <repo>\n\n> {OWNERSHIP}. Do not edit directly — update CLAUDE.md instead.\n\n"
MATCHING_COPILOT = (
    COPILOT_HEADER + "## Overview\n\nA test repo.\n\n## Formatting Rules\n\nUse tabs.\n\n"
    "## CI / Quality Gates\n\nNever disable linting rules.\n"
)
MATCHING_AGENTS = (
    f"<!-- {OWNERSHIP}. Do not edit directly — update CLAUDE.md instead. -->\n\n# <repo>\n\n"
    "Read and follow `CLAUDE.md` as the authoritative source for all "
    "repository instructions, conventions, and workflows.\n\n"
    "All build commands, formatting rules, test conventions, architecture "
    "guidance, and behavioral constraints are maintained in `CLAUDE.md`. "
    "Do not duplicate or contradict its content here.\n"
)
DEMO_CANONICAL = "---\nname: demo\ndescription: Test skill.\n---\n\nCanonical workflow.\n"
MATCHING_SHIM = (
    "---\nname: demo\ndescription: Test skill.\n---\n\n"
    f"<!-- {OWNERSHIP}. Do not edit directly — update the canonical skill at .claude/skills/demo/SKILL.md"
    " and regenerate. -->\n\n"
    "Read and follow `../../../.claude/skills/demo/SKILL.md` as the authoritative workflow.\n"
    "Resolve all relative paths and supporting resources from `../../../.claude/skills/demo/`.\n"
)
MARKED = f"# {OWNERSHIP}\n"

PARITY_SCENARIOS: dict[str, tuple[dict[str, str | bytes], dict[str, Any]]] = {
    "every_artifact_matches": (
        {
            "CLAUDE.md": PARITY_CLAUDE,
            ".github/copilot-instructions.md": MATCHING_COPILOT,
            "AGENTS.md": MATCHING_AGENTS,
            ".claude/skills/demo/SKILL.md": DEMO_CANONICAL,
            ".agents/skills/demo/SKILL.md": MATCHING_SHIM,
            ".agents/skills/orphan/SKILL.md": MARKED,
            ".codex/config.toml": MARKED,
            ".mcp.json": "{}\n",
            ".github/mcp.json": "[]\n",
        },
        {
            "artifacts": [
                {"path": ".github/copilot-instructions.md"},
                {"path": "AGENTS.md"},
                {"path": ".agents/skills/demo/SKILL.md"},
                {"path": ".agents/skills/orphan/SKILL.md"},
                {"path": ".codex/config.toml"},
                {"path": ".mcp.json", "hash": _content_hash("{}\n")},
                {"path": ".github/mcp.json"},
            ]
        },
    ),
    "every_artifact_drifts": (
        {
            "CLAUDE.md": PARITY_CLAUDE,
            ".github/copilot-instructions.md": f"# {OWNERSHIP}\n\n## Overview\n\nA test repo.\n",
            "AGENTS.md": MATCHING_AGENTS + "Extra.\n",
            ".claude/skills/demo/SKILL.md": DEMO_CANONICAL,
            ".agents/skills/demo/SKILL.md": MATCHING_SHIM.replace("authoritative", "primary"),
            ".github/agents/reviewer.md": "# Reviewer\n",
            ".codex/config.toml": UNDECODABLE,
            ".mcp.json": "{}\n",
            ".vscode/mcp.json": "# no marker\n",
        },
        {
            "artifacts": [
                {"path": ""},
                {},
                {"path": "../outside.md"},
                {"path": "README.md"},
                {"path": ".github/skills/a/b/SKILL.md"},
                {"path": ".github/mcp.json"},
                {"path": ".codex/config.toml"},
                {"path": ".mcp.json", "hash": "0" * 64},
                {"path": ".github/agents/reviewer.md", "hash": _content_hash("# Reviewer\n")},
                {"path": ".github/copilot-instructions.md"},
                {"path": "AGENTS.md"},
                {"path": ".agents/skills/demo/SKILL.md"},
                {"path": ".vscode/mcp.json"},
            ]
        },
    ),
    "hash_mismatch_on_an_unmarked_file": (
        {".github/agents/reviewer.md": "# Reviewer\n"},
        {"artifacts": [{"path": ".github/agents/reviewer.md", "hash": "0" * 64}]},
    ),
    "copilot_sections_differ": (
        {
            "CLAUDE.md": PARITY_CLAUDE,
            ".github/copilot-instructions.md": COPILOT_HEADER + "## Overview\n\nA different repo.\n",
        },
        {"artifacts": [{"path": ".github/copilot-instructions.md"}]},
    ),
    "copilot_custom_sections_match": (
        {
            "CLAUDE.md": PARITY_CLAUDE,
            ".github/copilot-instructions.md": COPILOT_HEADER + "## Formatting Rules\n\nUse tabs.\n",
        },
        {"copilot_sections": ["Formatting Rules"], "artifacts": [{"path": ".github/copilot-instructions.md"}]},
    ),
    "copilot_without_claude": (
        {".github/copilot-instructions.md": COPILOT_HEADER + "Anything.\n"},
        {"artifacts": [{"path": ".github/copilot-instructions.md"}]},
    ),
    "copilot_with_undecodable_claude": (
        {"CLAUDE.md": UNDECODABLE, ".github/copilot-instructions.md": COPILOT_HEADER + "Anything.\n"},
        {"artifacts": [{"path": ".github/copilot-instructions.md"}]},
    ),
    "copilot_without_banner_beside_undecodable_claude": (
        {"CLAUDE.md": UNDECODABLE, ".github/copilot-instructions.md": MARKED},
        {"artifacts": [{"path": ".github/copilot-instructions.md"}]},
    ),
    "copilot_sections_absent_from_claude": (
        {
            "CLAUDE.md": "# CLAUDE.md\n\n## Other\n\nText.\n",
            ".github/copilot-instructions.md": COPILOT_HEADER + "Text.\n",
        },
        {"artifacts": [{"path": ".github/copilot-instructions.md"}]},
    ),
    "shims_without_a_reconstructable_canonical": (
        {
            ".claude/skills/nofront/SKILL.md": "No frontmatter.\n",
            ".agents/skills/nofront/SKILL.md": MARKED,
            ".claude/skills/open/SKILL.md": "---\nname: open\n",
            ".agents/skills/open/SKILL.md": MARKED,
            ".claude/skills/twice/SKILL.md": "---\nname: a\nname: b\ndescription: d\n---\n",
            ".agents/skills/twice/SKILL.md": MARKED,
            ".claude/skills/binary/SKILL.md": UNDECODABLE,
            ".agents/skills/binary/SKILL.md": MARKED,
        },
        {
            "artifacts": [
                {"path": ".agents/skills/nofront/SKILL.md"},
                {"path": ".agents/skills/open/SKILL.md"},
                {"path": ".agents/skills/twice/SKILL.md"},
                {"path": ".agents/skills/binary/SKILL.md"},
            ]
        },
    ),
    "no_artifacts": ({}, {}),
}

AUTHORITY_EXPECTED: dict[str, tuple[str, str, list[tuple[str, str, str | None, str]]]] = {
    "nothing": (
        "unconfigured",
        "empty",
        [
            ("INFO", "authority", None, "Authority classification: unconfigured (signals: none)"),
        ],
    ),
    "manifest_with_every_signal": (
        "conforming",
        "manifest",
        [
            (
                "INFO",
                "authority",
                None,
                "Authority classification: conforming (signals: manifest, "
                "claude_md_exists, maintaining_section, generator_script, ci_parity)",
            ),
        ],
    ),
    "manifest_and_plain_claude": (
        "conforming",
        "manifest",
        [
            ("INFO", "authority", None, "Authority classification: conforming (signals: manifest, claude_md_exists)"),
        ],
    ),
    "manifest_not_an_object": (
        "unconfigured",
        "empty",
        [
            ("WARNING", "authority", ".github/ai-config-manifest.json", "Manifest is not a JSON object"),
            ("INFO", "authority", None, "Authority classification: unconfigured (signals: none)"),
        ],
    ),
    "manifest_malformed": (
        "ambiguous",
        "empty",
        [
            ("WARNING", "authority", ".github/ai-config-manifest.json", "Manifest exists but is malformed"),
            ("INFO", "authority", None, "Authority classification: ambiguous (signals: claude_md_exists)"),
        ],
    ),
    "manifest_undecodable": (
        "unconfigured",
        "empty",
        [
            ("WARNING", "authority", ".github/ai-config-manifest.json", "Manifest exists but is malformed"),
            ("INFO", "authority", None, "Authority classification: unconfigured (signals: none)"),
        ],
    ),
    "manifest_schema_errors": (
        "conforming",
        "empty",
        [
            (
                "ERROR",
                "authority",
                ".github/ai-config-manifest.json",
                "Manifest schema: schemaVersion must be an integer",
            ),
            ("ERROR", "authority", ".github/ai-config-manifest.json", "Manifest schema: runtimes is required"),
            ("ERROR", "authority", ".github/ai-config-manifest.json", "Manifest schema: surfaces must be a list"),
            ("ERROR", "authority", ".github/ai-config-manifest.json", "Manifest schema: features[0] must be a string"),
            ("ERROR", "authority", ".github/ai-config-manifest.json", "Manifest schema: mcp_servers is required"),
            (
                "INFO",
                "authority",
                None,
                "Authority classification: conforming (signals: claude_md_exists, maintaining_section)",
            ),
        ],
    ),
    "manifest_canonical_missing": (
        "conforming",
        "empty",
        [
            (
                "ERROR",
                "authority",
                ".github/ai-config-manifest.json",
                "Manifest declares canonical source 'CLAUDE.md' but it does not exist",
            ),
            ("INFO", "authority", None, "Authority classification: conforming (signals: generator_script, ci_parity)"),
        ],
    ),
    "manifest_without_derived_artifacts": (
        "ambiguous",
        "empty",
        [
            ("ERROR", "authority", ".github/ai-config-manifest.json", "Manifest declares no derived artifacts"),
            ("INFO", "authority", None, "Authority classification: ambiguous (signals: claude_md_exists)"),
        ],
    ),
    "generator_declares_alternative_source": (
        "alternative",
        "manifest",
        [
            (
                "INFO",
                "authority",
                ".github/ai-config-manifest.json",
                "Manifest declares alternative canonical source: AGENTS.md",
            ),
        ],
    ),
    "other_generator_declares_alternative_source": (
        "alternative",
        "manifest",
        [
            (
                "INFO",
                "authority",
                ".github/ai-config-manifest.json",
                "Manifest declares alternative canonical source: AGENTS.md",
            ),
        ],
    ),
    "other_generator_declares_claude": (
        "conforming",
        "empty",
        [
            (
                "INFO",
                "authority",
                None,
                "Authority classification: conforming (signals: claude_md_exists, maintaining_section)",
            ),
        ],
    ),
    "generator_without_canonical_source": (
        "unconfigured",
        "empty",
        [
            ("INFO", "authority", None, "Authority classification: unconfigured (signals: none)"),
        ],
    ),
    "claude_undecodable": (
        "ambiguous",
        "empty",
        [
            ("INFO", "authority", None, "Authority classification: ambiguous (signals: claude_md_exists)"),
        ],
    ),
    "signals_that_do_not_count": (
        "unconfigured",
        "empty",
        [
            ("INFO", "authority", None, "Authority classification: unconfigured (signals: none)"),
        ],
    ),
    "generator_and_ci_without_claude": (
        "conforming",
        "empty",
        [
            ("INFO", "authority", None, "Authority classification: conforming (signals: generator_script, ci_parity)"),
        ],
    ),
    "ci_only": (
        "ambiguous",
        "empty",
        [
            ("INFO", "authority", None, "Authority classification: ambiguous (signals: ci_parity)"),
        ],
    ),
}
PARITY_EXPECTED: dict[str, list[tuple[str, str, str | None, str]]] = {
    "every_artifact_matches": [],
    "every_artifact_drifts": [
        ("ERROR", "parity", "", "Manifest path rejected: empty path"),
        ("ERROR", "parity", "", "Manifest path rejected: empty path"),
        ("ERROR", "parity", "../outside.md", "Manifest path rejected: path traversal not allowed: ../outside.md"),
        ("ERROR", "parity", "README.md", "Manifest path rejected: path not in manifest allowlist: README.md"),
        (
            "ERROR",
            "parity",
            ".github/skills/a/b/SKILL.md",
            "Manifest path rejected: skill projection path must have "
            "exactly one skill-name segment: .github/skills/a/b/SKILL.md",
        ),
        ("ERROR", "parity", ".github/mcp.json", "Generated artifact missing: .github/mcp.json"),
        ("ERROR", "parity", ".codex/config.toml", "Generated artifact exists but could not be read"),
        ("ERROR", "parity", ".mcp.json", "Artifact modified (hash mismatch with manifest)"),
        ("ERROR", "parity", ".github/copilot-instructions.md", "Copilot instructions missing banner"),
        ("ERROR", "parity", "AGENTS.md", "Content does not match deterministic template"),
        ("ERROR", "parity", ".agents/skills/demo/SKILL.md", "Content does not match deterministic template"),
    ],
    "hash_mismatch_on_an_unmarked_file": [
        ("ERROR", "parity", ".github/agents/reviewer.md", "Artifact modified (hash mismatch with manifest)"),
    ],
    "copilot_sections_differ": [
        (
            "ERROR",
            "parity",
            ".github/copilot-instructions.md",
            "Copilot instructions sections do not match CLAUDE.md content",
        ),
    ],
    "copilot_custom_sections_match": [],
    # The manifest is not accepted without CLAUDE.md, and the inventory reports one it cannot read.
    "copilot_without_claude": [],
    "copilot_with_undecodable_claude": [],
    "copilot_without_banner_beside_undecodable_claude": [
        ("ERROR", "parity", ".github/copilot-instructions.md", "Copilot instructions missing banner"),
    ],
    "copilot_sections_absent_from_claude": [
        (
            "ERROR",
            "parity",
            ".github/copilot-instructions.md",
            "CLAUDE.md holds none of the sections the manifest projects, so the Copilot instructions were not compared",
        ),
    ],
    "shims_without_a_reconstructable_canonical": [],
    "no_artifacts": [],
}


def _write_fixture(root: Path, files: dict[str, str | bytes]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content.replace("<repo>", root.name), encoding="utf-8")


def _finding_rows(findings: list[audit.Finding]) -> list[tuple[str, str, str | None, str]]:
    return [(f.severity, f.check, f.path, f.message) for f in findings]


def authority_result(files: dict[str, str | bytes]) -> tuple[str, str, list[tuple[str, str, str | None, str]]]:
    """Classify a fixture; the manifest data is 'manifest' when it is the file's object, else 'empty'."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write_fixture(root, files)
        classification, data, findings = audit.classify_authority(root)
        written = files.get(MANIFEST_PATH)
        if data == {}:
            returned = "empty"
        elif isinstance(written, str) and data == json.loads(written):
            returned = "manifest"
        else:
            returned = repr(data)
        return classification, returned, _finding_rows(findings)


def parity_rows(files: dict[str, str | bytes], manifest: dict[str, Any]) -> list[tuple[str, str, str | None, str]]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write_fixture(root, files)
        return _finding_rows(audit.check_parity(root, manifest))


# The exact result of the authority classification and the parity check for each branch they take.


def test_every_authority_scenario_has_its_expected_result() -> None:
    assert set(AUTHORITY_SCENARIOS) == set(AUTHORITY_EXPECTED)


@pytest.mark.parametrize("name", list(AUTHORITY_SCENARIOS))
def test_authority_classification_and_findings(name: str) -> None:
    assert AUTHORITY_EXPECTED[name] == authority_result(AUTHORITY_SCENARIOS[name])


def test_every_parity_scenario_has_its_expected_findings() -> None:
    assert set(PARITY_SCENARIOS) == set(PARITY_EXPECTED)


@pytest.mark.parametrize("name", list(PARITY_SCENARIOS))
def test_parity_findings_and_their_order(name: str) -> None:
    files, manifest = PARITY_SCENARIOS[name]
    assert PARITY_EXPECTED[name] == parity_rows(files, manifest)


# ---------------------------------------------------------------------------
# Results of the manifest schema validation and the inventory check
# ---------------------------------------------------------------------------

ABSENT = object()


def _schema(**overrides: Any) -> dict[str, Any]:
    """A manifest that passes the schema, with fields replaced, or removed when given ABSENT."""
    data: dict[str, Any] = {
        "schemaVersion": 1,
        "runtimes": [],
        "surfaces": [],
        "features": [],
        "artifacts": [],
        "mcp_servers": [],
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not ABSENT}


SCHEMA_SCENARIOS: dict[str, dict[str, Any]] = {
    "minimal": _schema(),
    "every_field_valid": _schema(
        runtimes=["claude", "codex"],
        surfaces=["copilot_cli"],
        features=["mcp"],
        artifacts=[
            {"path": "AGENTS.md"},
            {"path": "AGENTS.md", "hash": "abc"},
            {"path": ".mcp.json", "hash": "h"},
            {"path": MANIFEST_PATH},
        ],
        mcp_servers=[{"name": "docs", "transport": "stdio", "targets": ["claude"]}],
        runtimeRoles={"copilot_cli": "full_local_host"},
        copilot_sections=["Build"],
    ),
    "schema_version_absent": _schema(schemaVersion=ABSENT),
    "schema_version_null": _schema(schemaVersion=None),
    "schema_version_string": _schema(schemaVersion="1"),
    "schema_version_bool": _schema(schemaVersion=True),
    "schema_version_float": _schema(schemaVersion=1.0),
    "schema_version_unsupported": _schema(schemaVersion=2),
    "schema_version_zero": _schema(schemaVersion=0),
    "string_lists_absent": _schema(runtimes=ABSENT, surfaces=None, features=ABSENT),
    "string_lists_not_lists": _schema(runtimes="claude", surfaces={}, features=3),
    "string_lists_with_non_strings": _schema(runtimes=["claude", 1, None], surfaces=[True], features=["a", ["b"]]),
    "artifacts_absent": _schema(artifacts=ABSENT),
    "artifacts_null": _schema(artifacts=None),
    "artifacts_not_a_list": _schema(artifacts="AGENTS.md"),
    "artifact_entries_malformed": _schema(artifacts=["AGENTS.md", {}, {"path": 3}, {"path": None, "hash": 5}]),
    "json_artifact_hashes": _schema(
        artifacts=[
            {"path": ".mcp.json"},
            {"path": ".vscode/mcp.json", "hash": ""},
            {"path": ".github/mcp.json", "hash": 7},
            {"path": ".mcp.json", "hash": "h"},
        ]
    ),
    "manifest_artifact_is_not_json": _schema(artifacts=[{"path": MANIFEST_PATH}, {"path": MANIFEST_PATH, "hash": 3}]),
    "other_artifact_hashes": _schema(
        artifacts=[
            {"path": "AGENTS.md", "hash": 3},
            {"path": "AGENTS.md", "hash": ""},
            {"path": "AGENTS.md", "hash": None},
        ]
    ),
    "mcp_servers_absent": _schema(mcp_servers=ABSENT),
    "mcp_servers_not_a_list": _schema(mcp_servers={}),
    "mcp_server_entries_malformed": _schema(
        mcp_servers=[
            "docs",
            {},
            {"name": "   ", "transport": 1, "targets": "claude"},
            {"name": 5, "transport": "stdio", "targets": ["claude", 2, None]},
        ]
    ),
    "runtime_roles_null": _schema(runtimeRoles=None),
    "runtime_roles_not_an_object": _schema(runtimeRoles=[]),
    "runtime_roles_entries": _schema(
        runtimeRoles={"copilot_cli": "full_local_host", "vscode": "x", "code_review": 3, "jetbrains": None}
    ),
    "copilot_sections_null": _schema(copilot_sections=None),
    "copilot_sections_empty": _schema(copilot_sections=[]),
    "copilot_sections_not_a_list": _schema(copilot_sections="Build"),
    "copilot_sections_with_a_non_string": _schema(copilot_sections=["Build", 1]),
    # Keys in reverse order, so the result's order is the validator's, not the manifest's.
    "every_field_wrong": {
        "copilot_sections": 1,
        "runtimeRoles": {"bad": 1},
        "mcp_servers": [1],
        "artifacts": [1],
        "features": [1],
        "surfaces": "s",
        "schemaVersion": "x",
    },
}

SCHEMA_EXPECTED: dict[str, list[str]] = {
    "minimal": [],
    "every_field_valid": [],
    "schema_version_absent": ["schemaVersion must be an integer"],
    "schema_version_null": ["schemaVersion must be an integer"],
    "schema_version_string": ["schemaVersion must be an integer"],
    "schema_version_bool": ["schemaVersion must be an integer"],
    "schema_version_float": ["schemaVersion must be an integer"],
    "schema_version_unsupported": ["schemaVersion 2 is not supported (expected 1)"],
    "schema_version_zero": ["schemaVersion 0 is not supported (expected 1)"],
    "string_lists_absent": ["runtimes is required", "surfaces is required", "features is required"],
    "string_lists_not_lists": ["runtimes must be a list", "surfaces must be a list", "features must be a list"],
    "string_lists_with_non_strings": [
        "runtimes[1] must be a string",
        "runtimes[2] must be a string",
        "surfaces[0] must be a string",
        "features[1] must be a string",
    ],
    "artifacts_absent": ["artifacts is required"],
    "artifacts_null": ["artifacts is required"],
    "artifacts_not_a_list": ["artifacts must be a list"],
    "artifact_entries_malformed": [
        "artifacts[0] must be an object",
        "artifacts[1].path must be a string",
        "artifacts[2].path must be a string",
        "artifacts[3].path must be a string",
    ],
    "json_artifact_hashes": [
        "artifacts[0].hash is required for JSON artifact '.mcp.json'",
        "artifacts[1].hash is required for JSON artifact '.vscode/mcp.json'",
        "artifacts[2].hash is required for JSON artifact '.github/mcp.json'",
    ],
    "manifest_artifact_is_not_json": ["artifacts[1].hash must be a string or absent"],
    "other_artifact_hashes": ["artifacts[0].hash must be a string or absent"],
    "mcp_servers_absent": ["mcp_servers is required"],
    "mcp_servers_not_a_list": ["mcp_servers must be a list"],
    "mcp_server_entries_malformed": [
        "mcp_servers[0] must be an object",
        "mcp_servers[1].name must be a non-empty string",
        "mcp_servers[1].transport must be a string",
        "mcp_servers[1].targets must be a list",
        "mcp_servers[2].name must be a non-empty string",
        "mcp_servers[2].transport must be a string",
        "mcp_servers[2].targets must be a list",
        "mcp_servers[3].name must be a non-empty string",
        "mcp_servers[3].targets[1] must be a string",
        "mcp_servers[3].targets[2] must be a string",
    ],
    "runtime_roles_null": [],
    "runtime_roles_not_an_object": ["runtimeRoles must be an object when present"],
    "runtime_roles_entries": [
        "runtimeRoles has unknown Copilot surface 'vscode'",
        "runtimeRoles.code_review must be a string",
        "runtimeRoles has unknown Copilot surface 'jetbrains'",
        "runtimeRoles.jetbrains must be a string",
    ],
    "copilot_sections_null": [],
    "copilot_sections_empty": [],
    "copilot_sections_not_a_list": ["copilot_sections must be a list of strings when present"],
    "copilot_sections_with_a_non_string": ["copilot_sections must be a list of strings when present"],
    "every_field_wrong": [
        "schemaVersion must be an integer",
        "runtimes is required",
        "surfaces must be a list",
        "features[0] must be a string",
        "artifacts[0] must be an object",
        "mcp_servers[0] must be an object",
        "runtimeRoles has unknown Copilot surface 'bad'",
        "runtimeRoles.bad must be a string",
        "copilot_sections must be a list of strings when present",
    ],
}

SCHEMA_AUTHORITY_ROWS: list[tuple[str, str, str | None, str]] = [
    ("ERROR", "authority", ".github/ai-config-manifest.json", f"Manifest schema: {message}")
    for message in (
        "schemaVersion must be an integer",
        "runtimes is required",
        "surfaces must be a list",
        "features[0] must be a string",
        "artifacts[0] must be an object",
        "mcp_servers[0] must be an object",
        "runtimeRoles has unknown Copilot surface 'bad'",
        "runtimeRoles.bad must be a string",
        "copilot_sections must be a list of strings when present",
    )
]

DIRECTORY = "<directory>"

INVENTORY_SCENARIOS: dict[str, dict[str, str | bytes]] = {
    "nothing": {},
    "every_fixed_file": {
        "CLAUDE.md": "x",
        "GEMINI.md": "x",
        "REVIEW.md": "x",
        ".codex/config.toml": "x",
        ".mcp.json": "x",
        ".github/mcp.json": "x",
        ".vscode/mcp.json": "x",
        ".github/copilot-instructions.md": "x",
        ".github/workflows/copilot-setup-steps.yml": "x",
        MANIFEST_PATH: "x",
    },
    "fixed_file_is_a_directory": {"CLAUDE.md": DIRECTORY},
    "recursive_files": {
        "AGENTS.md": "x",
        "sub/AGENTS.md": "x",
        "sub/deeper/AGENTS.md": "x",
        "AGENTS.override.md": "x",
        ".git/AGENTS.md": "x",
        "vendor/.git/hooks/AGENTS.override.md": "x",
        "vendor/AGENTS.md": "x",
    },
    "skill_directories": {
        ".claude/skills/Beta/SKILL.md": "x",
        ".claude/skills/alpha/SKILL.md": "x",
        ".claude/skills/notes/README.md": "x",
        ".agents/skills": DIRECTORY,
        ".github/skills/demo/SKILL.md": "x",
    },
    "skill_directory_is_a_file": {".agents/skills": "x"},
    "agent_directories": {
        ".github/agents/Reviewer.md": "x",
        ".github/agents/author.agent.md": "x",
        ".github/agents/nested/inner.md": "x",
        ".claude/agents/only-a-folder/inner.md": "x",
    },
    "agent_directory_is_empty": {".claude/agents": DIRECTORY},
    "agent_directory_is_a_file": {".github/agents": "x"},
    "path_specific_instructions": {
        ".github/instructions/a.instructions.md": "x",
        ".github/instructions/deep/b.instructions.md": "x",
        ".github/instructions/c.md": "x",
    },
    "instructions_without_matches": {".github/instructions/notes.md": "x"},
    "generator_scripts": {"scripts/ai_config.py": "x", ".github/scripts/ai_config.py": "x"},
    "parity_workflows": {
        ".github/workflows/check.yaml": "run: python scripts/AI_CONFIG.py",
        ".github/workflows/parity.yml": "run: ai-config --check",
    },
    "workflow_not_naming_ai_config": {".github/workflows/build.yml": "run: make"},
    "unreadable_workflows": {
        ".github/workflows/broken.yml": UNDECODABLE,
        ".github/workflows/folder.yaml": DIRECTORY,
        ".github/workflows/parity.yaml": "ai_config",
    },
    "workflows_is_a_file": {".github/workflows": "ai-config"},
    "one_of_each_group": {
        ".github/workflows/copilot-setup-steps.yml": "uses ai_config",
        "scripts/ai_config.py": "x",
        ".github/instructions/a.instructions.md": "x",
        ".claude/agents/reviewer.md": "x",
        ".agents/skills/demo/SKILL.md": "x",
        "docs/AGENTS.md": "x",
        "CLAUDE.md": "x",
    },
}

INVENTORY_EXPECTED: dict[str, list[tuple[str, str, str | None, str]]] = {
    "nothing": [],
    "every_fixed_file": [
        ("INFO", "inventory", "CLAUDE.md", "Found CLAUDE.md"),
        ("INFO", "inventory", "GEMINI.md", "Found GEMINI.md"),
        ("INFO", "inventory", "REVIEW.md", "Found REVIEW.md"),
        ("INFO", "inventory", ".codex/config.toml", "Found .codex/config.toml"),
        ("INFO", "inventory", ".mcp.json", "Found .mcp.json"),
        ("INFO", "inventory", ".github/mcp.json", "Found .github/mcp.json"),
        ("INFO", "inventory", ".vscode/mcp.json", "Found .vscode/mcp.json"),
        ("INFO", "inventory", ".github/copilot-instructions.md", "Found .github/copilot-instructions.md"),
        (
            "INFO",
            "inventory",
            ".github/workflows/copilot-setup-steps.yml",
            "Found .github/workflows/copilot-setup-steps.yml",
        ),
        ("INFO", "inventory", ".github/ai-config-manifest.json", "Found .github/ai-config-manifest.json"),
    ],
    "fixed_file_is_a_directory": [],
    "recursive_files": [
        ("INFO", "inventory", "AGENTS.md", "Found AGENTS.md"),
        ("INFO", "inventory", "sub/AGENTS.md", "Found sub/AGENTS.md"),
        ("INFO", "inventory", "sub/deeper/AGENTS.md", "Found sub/deeper/AGENTS.md"),
        ("INFO", "inventory", "AGENTS.override.md", "Found AGENTS.override.md"),
    ],
    "skill_directories": [
        ("INFO", "inventory", ".claude/skills", "Skills found: Beta, alpha"),
        ("INFO", "inventory", ".github/skills", "Skills found: demo"),
    ],
    "skill_directory_is_a_file": [],
    "agent_directories": [
        ("INFO", "inventory", ".github/agents", "Custom agent files found: Reviewer.md, author.agent.md"),
    ],
    "agent_directory_is_empty": [],
    "agent_directory_is_a_file": [],
    "path_specific_instructions": [
        ("INFO", "inventory", ".github/instructions", "2 path-specific instruction file(s)"),
    ],
    "instructions_without_matches": [],
    "generator_scripts": [
        ("INFO", "inventory", ".github/scripts/ai_config.py", "Generator script found: .github/scripts/ai_config.py"),
        ("INFO", "inventory", "scripts/ai_config.py", "Generator script found: scripts/ai_config.py"),
    ],
    "parity_workflows": [
        ("INFO", "inventory", ".github/workflows/parity.yml", "AI config parity workflow found"),
        ("INFO", "inventory", ".github/workflows/check.yaml", "AI config parity workflow found"),
    ],
    "workflow_not_naming_ai_config": [],
    "unreadable_workflows": [
        (
            "ERROR",
            "inventory",
            ".github/workflows/broken.yml",
            "Could not read, so the checks that read it did not run: 'utf-8' codec can't decode byte 0x80 in "
            "position 0: invalid start byte",
        ),
        ("INFO", "inventory", ".github/workflows/parity.yaml", "AI config parity workflow found"),
    ],
    "workflows_is_a_file": [],
    "one_of_each_group": [
        ("INFO", "inventory", "CLAUDE.md", "Found CLAUDE.md"),
        (
            "INFO",
            "inventory",
            ".github/workflows/copilot-setup-steps.yml",
            "Found .github/workflows/copilot-setup-steps.yml",
        ),
        ("INFO", "inventory", "docs/AGENTS.md", "Found docs/AGENTS.md"),
        ("INFO", "inventory", ".agents/skills", "Skills found: demo"),
        ("INFO", "inventory", ".claude/agents", "Custom agent files found: reviewer.md"),
        ("INFO", "inventory", ".github/instructions", "1 path-specific instruction file(s)"),
        ("INFO", "inventory", "scripts/ai_config.py", "Generator script found: scripts/ai_config.py"),
        ("INFO", "inventory", ".github/workflows/copilot-setup-steps.yml", "AI config parity workflow found"),
    ],
}


def inventory_rows(files: dict[str, str | bytes]) -> list[tuple[str, str, str | None, str]]:
    """Inventory a fixture whose DIRECTORY entries are empty folders."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for relative in (path for path, content in files.items() if content == DIRECTORY):
            (root / relative).mkdir(parents=True)
        _write_fixture(root, {path: content for path, content in files.items() if content != DIRECTORY})
        return _finding_rows(audit.check_inventory(root))


class ManifestSchemaAndInventorySequenceTests(unittest.TestCase):
    """The exact result of the manifest schema validation and the inventory check for each branch they take."""

    def test_schema_errors_become_authority_findings_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = SCHEMA_SCENARIOS["every_field_wrong"]
            classification, returned, findings = audit._validate_claude_manifest(Path(temporary), data)
        self.assertEqual((None, {}), (classification, returned))
        self.assertEqual(SCHEMA_AUTHORITY_ROWS, _finding_rows(findings))


def test_every_schema_scenario_has_its_expected_errors() -> None:
    assert set(SCHEMA_SCENARIOS) == set(SCHEMA_EXPECTED)


@pytest.mark.parametrize("name", list(SCHEMA_SCENARIOS))
def test_manifest_schema_errors_and_their_order(name: str) -> None:
    assert SCHEMA_EXPECTED[name] == audit._validate_manifest_schema(SCHEMA_SCENARIOS[name])


def test_every_inventory_scenario_has_its_expected_findings() -> None:
    assert set(INVENTORY_SCENARIOS) == set(INVENTORY_EXPECTED)


@pytest.mark.parametrize("name", list(INVENTORY_SCENARIOS))
def test_inventory_findings_and_their_order(name: str) -> None:
    assert INVENTORY_EXPECTED[name] == inventory_rows(INVENTORY_SCENARIOS[name])


class GeneratedLayoutReferenceTests(unittest.TestCase):
    """references/generated-layout.md documents the layout the audit checks; it must agree with the engine."""

    REFERENCE = Path(__file__).resolve().parent.parent / "references/generated-layout.md"

    def _table(self, heading: str) -> list[list[str]]:
        lines = self.REFERENCE.read_text(encoding="utf-8").splitlines()
        start = lines.index(heading) + 1
        rows = []
        for line in lines[start:]:
            if line.startswith("#"):
                break
            if line.startswith("|") and not line.startswith("|---"):
                rows.append([cell.strip() for cell in line.strip("|").split("|")])
        return rows[1:]

    def test_transport_table_matches_the_engine(self) -> None:
        targets = ["claude", "codex", "copilot_local", "vscode", "copilot_repository"]
        documented = {
            row[0].strip("`"): {target for target, cell in zip(targets, row[1:], strict=True) if cell == "Yes"}
            for row in self._table("### Transport compatibility")
        }
        expected = {
            "stdio": {"claude", "codex", "copilot_local", "vscode", "copilot_repository"},
            "local": {"copilot_local", "copilot_repository"},
            "http": {"claude", "codex", "copilot_local", "vscode", "copilot_repository"},
            "sse": {"claude", "copilot_local", "vscode", "copilot_repository"},
        }
        self.assertEqual(expected, documented)
        self.assertEqual(expected, audit.TRANSPORT_COMPATIBILITY)

    def test_every_generated_file_is_a_path_the_manifest_may_own(self) -> None:
        documented = {
            path.replace("<name>", "*")
            for row in self._table("## What each choice generates")
            for path in re.findall(r"`([^`]+\.(?:md|json|toml|yml))`", row[1])
        }
        self.assertIn(".github/workflows/ai-config-parity-pr.yml", documented)
        self.assertEqual(set(), documented - set(audit.MANIFEST_ALLOWED_PATHS))

    def test_the_surface_tables_instruction_columns_agree_with_the_layering_resolvers(self) -> None:
        # The cloud agent's resolver selects its agent instruction file only, so its other columns are not compared.
        resolvers: dict[str, Callable[[Path, audit.InstructionSources], audit.Finding]] = {
            "JetBrains": lambda _, sources: audit._layering_jetbrains(sources)[0],
            "Copilot app": lambda _, sources: audit._layering_copilot_cli(sources)[0],
            "CLI": lambda _, sources: audit._layering_copilot_cli(sources)[0],
            "Cloud agent": lambda _, sources: audit._cloud_agent_selection(sources),
            "Code review": lambda root, sources: audit._layering_code_review(root, sources)[0],
        }
        columns: dict[str, dict[str, str | bytes]] = {
            "CLAUDE.md": {"CLAUDE.md": "# Rules\n"},
            "AGENTS.md": {"AGENTS.md": PLAIN_AGENTS},
            ".github/copilot-instructions.md": {".github/copilot-instructions.md": "# Copilot\n"},
            "Path-specific": {".github/instructions/a.instructions.md": "---\napplyTo: '**'\n---\nRules\n"},
        }
        compared = {"Cloud agent": 2}
        for row in self._table("### Copilot surface capabilities"):
            if row[0] not in resolvers:
                continue
            for column, cell in list(zip(columns, row[1:], strict=False))[: compared.get(row[0], len(columns))]:
                with self.subTest(surface=row[0], column=column), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    _write_fixture(root, columns[column])
                    finding = resolvers[row[0]](root, audit._instruction_sources(root))
                    self.assertEqual(cell != "No", finding.severity == "INFO", (cell, finding.message))

    def test_the_copilot_local_tools_row_names_what_the_mcp_check_accepts(self) -> None:
        row = next(row for row in self._table("## MCP server objects") if row[0] == "`copilot_local.tools`")
        self.assertEqual(["null", '["*"]'], re.findall(r"`([^`]+)`", row[1])[:2])
        manifest = {"mcp_servers": [{"name": "docs", "targets": ["claude", "copilot_local"]}]}
        accepted = {
            json.dumps(tools): not audit._check_copilot_local_tools(
                manifest, {"docs": {"command": "d", "tools": tools}}
            )
            for tools in (None, ["*"], ["search"])
        }
        self.assertEqual({"null": True, '["*"]': True, '["search"]': False}, accepted)


class ReportSchemaReferenceTests(unittest.TestCase):
    """references/report-schema.md shows the report the engine prints; its examples must be that output."""

    REFERENCE = Path(__file__).resolve().parent.parent / "references/report-schema.md"

    def _fence(self, heading: str, language: str) -> str:
        text = self.REFERENCE.read_text(encoding="utf-8")
        section = text[text.index(heading) :]
        start = section.index(f"```{language}\n") + len(f"```{language}\n")
        return section[start : section.index("\n```\n", start) + 1]

    def _example(self) -> audit.AuditResult:
        return audit.AuditResult(
            repository="{repo_name}",
            authority="conforming",
            scope_status="independently-derived",
            findings=[
                audit.Finding("INFO", "inventory", "CLAUDE.md", message="Found CLAUDE.md"),
                audit.Finding(
                    "WARNING",
                    "mcp",
                    message="Copilot repository MCP (cloud agent/code review) configured via repository settings "
                    "— cannot validate statically",
                ),
                audit.Finding(
                    "ERROR",
                    "copilot-config",
                    ".github/skills/demo/SKILL.md",
                    4,
                    "Frontmatter must use key: value entries, block scalars, block lists, or nested mappings",
                ),
            ],
        )

    def test_the_markdown_example_is_the_report_the_engine_prints(self) -> None:
        self.assertEqual(self._fence("### Markdown (default)", "markdown"), audit.format_markdown(self._example()))

    def test_the_json_example_is_the_report_the_engine_prints(self) -> None:
        self.assertEqual(
            json.loads(self._fence("### JSON (for CI integration)", "json")),
            json.loads(audit.format_json(self._example())),
        )

    def test_the_finding_table_names_exactly_the_fields_a_finding_has(self) -> None:
        text = self.REFERENCE.read_text(encoding="utf-8")
        section = text[text.index("## Finding Structure") : text.index("## Severities")]
        documented = [line.split("|")[1].strip().strip("`") for line in section.splitlines() if line.startswith("| `")]
        self.assertEqual(["severity", "check", "path", "line", "message"], documented)
        self.assertEqual(documented, [field.name for field in dataclasses.fields(audit.Finding)])


class AuditPolicyReferenceTests(unittest.TestCase):
    """references/audit-policy.md names, in its check headings, every check a finding can carry."""

    CHECKS = frozenset(
        {
            "inventory",
            "authority",
            "vocabulary",
            "scope",
            "parity",
            "orphan",
            "mcp",
            "layering",
            "trust-boundary",
            "runtime",
            "copilot-config",
            "copilot-skill",
            "copilot-agent",
            "collision",
            "provenance",
            "runtime-role",
            "behavioral",
            "ownership",
            "limitation",
        }
    )

    def test_the_check_headings_name_every_check_the_engine_reports(self) -> None:
        reference = Path(audit.__file__).resolve().parent.parent / "references/audit-policy.md"
        headings = re.findall(r"^### \d+\. .*\((.*)\)$", reference.read_text(encoding="utf-8"), re.MULTILINE)
        documented = {name for heading in headings for name in re.findall(r"`([a-z-]+)`", heading)}
        self.assertEqual(self.CHECKS, documented)
        tree = ast.parse(Path(audit.__file__).read_text(encoding="utf-8"))
        reported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Finding":
                check = node.args[1] if len(node.args) > 1 else next(k.value for k in node.keywords if k.arg == "check")
                if not isinstance(check, ast.Constant):
                    self.fail(f"a check named by an expression the reference cannot list: {ast.unparse(node)}")
                reported.add(check.value)
        self.assertEqual(self.CHECKS, reported)


class ReportFormatTests(unittest.TestCase):
    def test_the_markdown_table_carries_each_findings_line(self) -> None:
        result = audit.AuditResult(
            repository="example",
            authority="conforming",
            findings=[
                audit.Finding("ERROR", "copilot-config", "a/SKILL.md", 4, "Bad entry"),
                audit.Finding("ERROR", "copilot-config", "a/SKILL.md", 5, "Bad entry"),
                audit.Finding("INFO", "scope", message="No scope"),
            ],
        )
        rows = [line for line in audit.format_markdown(result).splitlines() if line.startswith("| ")]
        self.assertEqual(
            [
                "| Severity | Check | Path | Line | Message |",
                "| ERROR | copilot-config | a&#47;SKILL&#46;md | 4 | Bad entry |",
                "| ERROR | copilot-config | a&#47;SKILL&#46;md | 5 | Bad entry |",
                "| INFO | scope |  |  | No scope |",
            ],
            rows,
        )

    def test_json_findings_carry_no_detail(self) -> None:
        data = json.loads(audit.format_json(audit.AuditResult(findings=[audit.Finding("INFO", "scope")])))
        self.assertEqual({"severity", "check", "path", "line", "message"}, set(data["findings"][0]))


class RootNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "named repo"
        (self.root / ".git").mkdir(parents=True)
        _setup_conforming_repo(self.root)
        self.cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self) -> None:
        os.chdir(self.cwd)
        self.temp.cleanup()

    def test_a_relative_root_reports_and_checks_the_directorys_own_name(self) -> None:
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            mock.patch.object(sys, "argv", ["audit_ai_config.py", "--root", ".", "--json"]),
        ):
            code = audit.main()
        data = json.loads(output.getvalue())
        self.assertEqual("named repo", data["repository"])
        # The generated AGENTS.md names the repository, so an empty name would be a parity ERROR.
        self.assertEqual((0, []), (code, [f for f in data["findings"] if f["severity"] == "ERROR"]))


class UnreadableConfigurationTests(unittest.TestCase):
    """An undecodable or invalid file is a finding, never a traceback."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _errors(self, rel: str) -> list[tuple[str, str]]:
        return [
            (f.check, f.message) for f in audit.audit(self.root).findings if f.path == rel and f.severity == "ERROR"
        ]

    def test_an_undecodable_copilot_skill_or_agent_is_an_error(self) -> None:
        for rel in (".github/skills/demo/SKILL.md", ".github/agents/reviewer.md", ".claude/agents/reviewer.md"):
            with self.subTest(rel=rel):
                path = self.root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(UNDECODABLE)
                errors = self._errors(rel)
                self.assertIn("copilot-config", [check for check, _ in errors])
                self.assertTrue(any(message.startswith("Could not read:") for _, message in errors), errors)
                path.unlink()

    def test_an_undecodable_manifest_owned_projection_is_an_error(self) -> None:
        manifest = json.loads((self.root / ".github/ai-config-manifest.json").read_text(encoding="utf-8"))
        manifest["artifacts"].append({"path": ".github/skills/demo/SKILL.md"})
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / ".github/skills/demo").mkdir(parents=True)
        (self.root / ".github/skills/demo/SKILL.md").write_bytes(UNDECODABLE)
        errors = self._errors(".github/skills/demo/SKILL.md")
        self.assertIn(("parity", "Generated artifact exists but could not be read"), errors)

    def test_an_undecodable_codex_config_is_invalid_toml(self) -> None:
        (self.root / ".codex").mkdir()
        (self.root / ".codex/config.toml").write_bytes(UNDECODABLE)
        errors = self._errors(".codex/config.toml")
        self.assertEqual(["mcp"], [check for check, _ in errors])
        self.assertTrue(errors[0][1].startswith("Invalid TOML:"), errors)

    def test_a_generator_with_a_null_byte_is_unreadable_scope(self) -> None:
        (self.root / ".github/scripts").mkdir(parents=True)
        (self.root / ".github/scripts/ai_config.py").write_text("CLAUDE.md\0\n", encoding="utf-8")
        self.assertEqual((None, "generator-scope-unreadable"), audit.derive_generator_scope(self.root))


class RepositoryWalkTests(unittest.TestCase):
    """Walks for nested files skip version-control and dependency directories."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)
        manifest = json.loads((self.root / ".github/ai-config-manifest.json").read_text(encoding="utf-8"))
        manifest["surfaces"] = ["copilot_cli", "cloud_agent"]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_nested_files_below_skipped_directories_are_not_reported(self) -> None:
        for directory in ("src", *sorted(SKIPPED_FOLDERS), "src/node_modules/pkg", "lib/.venv", "vendor/clone"):
            for name in ("AGENTS.md", "AGENTS.override.md", ".mcp.json"):
                path = self.root / directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n" if name == ".mcp.json" else "# Nested\n", encoding="utf-8")
        # A clone, submodule, or worktree inside the repository is another repository's, marked by its .git.
        (self.root / "vendor/clone/.git").write_text("gitdir: ../../.git/modules/clone\n", encoding="utf-8")
        reported = {f.path for f in audit.audit(self.root).findings if f.path and "/" in f.path}
        self.assertEqual(
            {"src/AGENTS.md", "src/AGENTS.override.md", "src/.mcp.json"},
            {path for path in reported if path.rsplit("/", 1)[-1] in ("AGENTS.md", "AGENTS.override.md", ".mcp.json")},
        )

    def test_the_skipped_directories_are_version_control_dependency_and_build_directories(self) -> None:
        self.assertEqual(
            {
                ".git",
                ".hg",
                ".svn",
                "node_modules",
                ".venv",
                "venv",
                "__pycache__",
                ".tox",
                ".nox",
                "bin",
                "obj",
                ".vs",
            },
            SKIPPED_FOLDERS,
        )


class ScopeWithoutAValidManifestTests(unittest.TestCase):
    """A derived generator scope is compared with a valid manifest only; a missing or invalid one is one finding."""

    GENERATOR = "SOURCE = 'CLAUDE.md'\nTARGET_RUNTIMES = ['claude']\nTARGET_SURFACES = []\nTARGET_FEATURES = []\n"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _write_fixture(self.root, {"CLAUDE.md": MAINTAINED_CLAUDE, ".github/scripts/ai_config.py": self.GENERATOR})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _scope(self) -> tuple[str, list[tuple[str, str | None, str]]]:
        result = audit.audit(self.root)
        self.assertEqual("conforming", result.authority)
        _assert_scope_status(self, result, "independently-derived")
        return result.scope_status, [(f.severity, f.path, f.message) for f in result.findings if f.check == "scope"]

    def test_a_missing_manifest_is_one_scope_error(self) -> None:
        self.assertEqual(
            (
                "independently-derived",
                [("ERROR", MANIFEST_PATH, "Manifest is missing, so the derived generator scope was not compared")],
            ),
            self._scope(),
        )

    def test_a_malformed_ignored_or_schema_invalid_manifest_is_one_scope_error(self) -> None:
        for name, content in (
            ("malformed", "{bad"),
            ("not generated", json.dumps({"runtimes": ["claude"]})),
            ("schema invalid", _manifest(runtimes=None)),
        ):
            with self.subTest(name=name):
                _write_fixture(self.root, {MANIFEST_PATH: content})
                self.assertEqual(
                    (
                        "independently-derived",
                        [
                            (
                                "ERROR",
                                MANIFEST_PATH,
                                "Manifest is not a valid ai_config.py manifest, so the derived generator scope "
                                "was not compared",
                            )
                        ],
                    ),
                    self._scope(),
                )

    def test_a_valid_manifest_is_still_compared_field_by_field(self) -> None:
        _write_fixture(self.root, {MANIFEST_PATH: _manifest(runtimes=["claude"], surfaces=["copilot_cli"])})
        self.assertEqual(
            (
                "independently-derived",
                [("ERROR", MANIFEST_PATH, "Manifest surfaces differs from independently derived generator scope")],
            ),
            self._scope(),
        )


def markdown_row(path: str, message: str) -> str:
    result = audit.AuditResult(
        repository="example", authority="conforming", findings=[audit.Finding("ERROR", "mcp", path, 3, message)]
    )
    rows = [line for line in audit.format_markdown(result).splitlines() if line.startswith("| ERROR")]
    assert len(rows) == 1, rows
    return rows[0]


class MarkdownCellTests(unittest.TestCase):
    """A value the repository controls cannot split a findings row or add a column."""

    def test_pipes_are_encoded_and_line_breaks_flattened_in_a_cell(self) -> None:
        self.assertEqual(
            "| ERROR | mcp | a&#124;b c&#46;json | 3 | Server &#39;a&#124;b c&#39;&#58; bad |",
            markdown_row("a|b\r\nc.json", "Server 'a|b\nc': bad"),
        )

    def test_every_repository_controlled_cell_is_skill_cores_markdown_line(self) -> None:
        self.assertFalse(hasattr(audit, "_cell"))
        # The module imports markdown_line and exports none, so its own binding is read from its namespace.
        self.assertIs(markdown_literal.markdown_line, vars(audit)["markdown_line"])
        text = "x`*_[a](b)<i>&|\\"
        line = markdown_literal.markdown_line(text)
        self.assertEqual(f"| ERROR | mcp | {line} | 3 | {line} |", markdown_row(text, text))

    def test_server_names_that_open_markup_or_end_a_line_stay_literal_in_their_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _setup_conforming_repo(root)
            names = ["a<!-- hidden", "b\u2028x", "c\u0085x"]
            (root / ".mcp.json").write_text(json.dumps({"mcpServers": {name: {} for name in names}}), encoding="utf-8")
            result = audit.audit(root)
            report = audit.format_markdown(result)
        self.assertNotIn("<!--", report)
        rows = report[report.index("| Severity") :].splitlines()[2:]
        self.assertEqual(len(result.findings), len(rows))
        for name in ("a&lt;&#33;&#45;&#45; hidden", "b x", "c x"):
            self.assertIn(
                f"Server &#39;{name}&#39;&#58; must have &#39;command&#39; &#40;STDIO&#47;local&#41; or &#39;url&#39; "
                "&#40;HTTP&#47;SSE&#41;",
                report,
            )

    def test_a_server_name_from_the_repository_stays_in_its_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _setup_conforming_repo(root)
            (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"a|b\nc": {}}}), encoding="utf-8")
            report = audit.format_markdown(audit.audit(root))
        table = report[report.index("| Severity") :].splitlines()
        self.assertTrue(table)
        for line in table:
            self.assertEqual(6, len(re.findall(r"(?<!\\)\|", line)), line)


@pytest.mark.parametrize(
    ("text", "cell"),
    [
        pytest.param("a<!-- hidden", "a&lt;&#33;&#45;&#45; hidden", id="a-comment-opening"),
        pytest.param("a\\|b", "a&#92;&#124;b", id="an-escaped-pipe"),
        pytest.param("b\u2028x", "b x", id="a-line-separator"),
        pytest.param("c\u0085x", "c x", id="a-next-line"),
    ],
)
def test_a_cell_opens_no_comment_and_an_escaped_pipe_adds_no_cell(text: str, cell: str) -> None:
    assert f"| ERROR | mcp | {cell} | 3 | {cell} |" == markdown_row(text, text)


@pytest.mark.parametrize(
    "separator", ["\n", "\r", "\u0085", "\u2028", "\u2029"], ids=lambda separator: hex(ord(separator))
)
def test_no_line_separator_in_a_value_starts_a_report_line(separator: str) -> None:
    forged = f"x{separator}RESULT COMPLIANT{separator}SUMMARY ERROR 0"
    result = audit.AuditResult(
        repository="example",
        authority="conforming",
        findings=[audit.Finding("ERROR", "mcp", forged, None, forged)],
    )
    lines = audit.format_markdown(result).splitlines()
    assert [line for line in lines if line.startswith("RESULT")] == ["RESULT ERRORS"]
    assert [line for line in lines if line.startswith("| ERROR")] == [
        "| ERROR | mcp | x RESULT COMPLIANT SUMMARY ERROR 0 |  | x RESULT COMPLIANT SUMMARY ERROR 0 |"
    ]


class OwnershipFindingTests(unittest.TestCase):
    """The ownership marker and an unreadable artifact are each reported once, by one check."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _own(self, rel: str, content: str | bytes) -> list[tuple[str, str]]:
        manifest = json.loads((self.root / MANIFEST_PATH).read_text(encoding="utf-8"))
        manifest["artifacts"].append({"path": rel})
        _write_fixture(self.root, {MANIFEST_PATH: json.dumps(manifest), rel: content})
        return [
            (f.check, f.message)
            for f in audit.audit(self.root).findings
            if f.path == rel and f.check in ("parity", "provenance", "ownership")
        ]

    def test_a_manifest_owned_projection_without_its_marker_is_one_ownership_error(self) -> None:
        self.assertEqual(
            [("ownership", "Generated file missing embedded ownership marker")],
            self._own(".github/skills/demo/SKILL.md", "---\nname: demo\ndescription: Demo skill.\n---\n"),
        )

    def test_an_unreadable_artifact_is_one_parity_error(self) -> None:
        self.assertEqual(
            [("parity", "Generated artifact exists but could not be read")],
            self._own(".codex/config.toml", UNDECODABLE),
        )

    def test_a_json_artifacts_hash_is_left_to_the_manifest_schema(self) -> None:
        _write_fixture(self.root, {".mcp.json": "{}"})
        self.assertEqual([], audit.check_ownership(self.root, {"artifacts": [{"path": ".mcp.json"}]}))


class UnlistableDirectoryTests(unittest.TestCase):
    """A directory the audit cannot list is a finding at that directory, never a traceback."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)
        _write_fixture(self.root, {".github/agents/reviewer.md": "---\ndescription: Reviews.\n---\n"})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_a_permission_error_listing_agents_is_an_error_at_the_directory(self) -> None:
        listed = Path.iterdir

        def iterdir(path: Path) -> Iterator[Path]:
            # The audit resolves its root, which can spell the temporary directory differently from self.root.
            if path.parts[-2:] == (".github", "agents"):
                raise PermissionError(13, "Access is denied", str(path))
            return listed(path)

        with mock.patch.object(Path, "iterdir", iterdir):
            result = audit.audit(self.root)
        self.assertEqual(
            [("ERROR", "copilot-config", "Could not list the directory: Access is denied")],
            [(f.severity, f.check, f.message) for f in result.findings if f.path == ".github/agents"],
        )


def _link_directory(target: Path, link: Path) -> None:
    """A link to a directory: a junction on Windows, which needs no privilege, else a symbolic link."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


class OutsideLinkTests(unittest.TestCase):
    """No walk or listing follows a link out of the repository; each such link is one inventory WARNING."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        _setup_conforming_repo(self.root)
        self.outside = Path(self.temp.name) / "outside"
        _write_fixture(
            self.outside,
            {
                "elsewhere-agent.md": "---\ndescription: Not the repository's.\n---\n",
                "AGENTS.md": "# Not the repository's\n",
                ".mcp.json": "{}\n",
                "SKILL.md": "---\nname: elsewhere\ndescription: Not the repository's.\n---\n",
                "nested/SKILL.md": "---\nname: elsewhere\ndescription: Not the repository's.\n---\n",
                "nested/elsewhere.instructions.md": "Not the repository's.\n",
                "ai-config.yml": "steps:\n  - run: python .github/scripts/ai_config.py --check\n",
            },
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _outside_links(self, result: audit.AuditResult) -> list[tuple[str, str, str | None]]:
        return [(f.severity, f.check, f.path) for f in result.findings if f.message == audit.OUTSIDE_LINK]

    def test_a_link_out_of_the_repository_is_reported_once_and_nothing_behind_it_is_read(self) -> None:
        links = (
            ".github/agents",
            ".claude/agents",
            ".github/skills/elsewhere",
            ".agents/skills/elsewhere",
            ".github/instructions",
            ".github/workflows",
            "vendor",
            "src/lib",
        )
        for link in links:
            _link_directory(self.outside, self.root / link)
        result = audit.audit(self.root)
        self.assertEqual("conforming", result.authority)
        self.assertEqual(sorted(("WARNING", "inventory", link) for link in links), sorted(self._outside_links(result)))
        for finding in result.findings:
            self.assertNotIn("elsewhere", finding.message, finding)
            self.assertFalse((finding.path or "").startswith(tuple(f"{link}/" for link in links)), finding)
        self.assertFalse(any(f.path == ".github/instructions" and f.severity == "INFO" for f in result.findings))

    def test_a_folder_whose_parent_links_out_is_neither_listed_nor_walked(self) -> None:
        _link_directory(self.outside, self.root / "linked")
        nested = self.root / "linked/nested"
        listing = audit.list_directory(self.root, nested)
        self.assertEqual(([], [nested]), (listing.paths, listing.outside))
        walk = audit.walk_files(self.root, nested, lambda name: True)
        self.assertEqual(([], [nested]), (walk.paths, walk.outside))

    def test_a_link_that_stays_inside_the_repository_is_followed(self) -> None:
        _write_fixture(self.root, {"shared/agents/reviewer.md": "---\ndescription: Reviews.\n---\n"})
        _link_directory(self.root / "shared/agents", self.root / ".github/agents")
        result = audit.audit(self.root)
        self.assertEqual([], self._outside_links(result))
        self.assertIn(
            ("INFO", "Custom agent files found: reviewer.md"),
            [(f.severity, f.message) for f in result.findings if f.path == ".github/agents"],
        )

    def test_a_link_back_into_the_repository_is_not_walked_again(self) -> None:
        # A link inside the root is followed, so one back to the root, or to a folder holding it, would walk it again
        # without end; each nested file is found once through its own folder, and once through the link to it.
        _write_fixture(self.root, {"target/AGENTS.md": "# Nested\n"})
        _link_directory(self.root / "target", self.root / "linked")
        _link_directory(self.root, self.root / "loop")
        _link_directory(self.root / "target", self.root / "target/self")
        self.assertEqual(
            [self.root / "linked/AGENTS.md", self.root / "target/AGENTS.md"],
            [path for path in audit.named_files(self.root, "AGENTS.md") if path != self.root / "AGENTS.md"],
        )
        self.assertEqual([], self._outside_links(audit.audit(self.root)))

    def test_the_link_finding_is_the_one_the_audit_policy_names(self) -> None:
        message = "Link leads out of the repository; the audit does not follow it"
        self.assertEqual(message, audit.OUTSIDE_LINK)
        policy = Path(audit.__file__).resolve().parent.parent / "references/audit-policy.md"
        self.assertIn(f"inventory `WARNING` at its path, `{message}`", policy.read_text(encoding="utf-8"))

    def test_a_path_leaves_the_root_through_dot_dot_and_stays_through_a_folder_that_returns(self) -> None:
        self.assertTrue(audit.leaves_root(self.root, self.root / ".." / "outside"))
        self.assertFalse(audit.leaves_root(self.root, self.root / ".github" / ".." / "CLAUDE.md"))
        self.assertFalse(audit.leaves_root(self.root, self.root))


class UnreadInputTests(unittest.TestCase):
    """A file a check reads but cannot read or decode is an ERROR, so the audit is never COMPLIANT having skipped it."""

    UNREAD = "Could not read, so the checks that read it did not run: "

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _manifest(self) -> dict[str, Any]:
        manifest: dict[str, Any] = json.loads((self.root / MANIFEST_PATH).read_text(encoding="utf-8"))
        return manifest

    def _unread(self, result: audit.AuditResult) -> list[str | None]:
        return [f.path for f in result.findings if f.severity == "ERROR" and f.message.startswith(self.UNREAD)]

    def test_an_undecodable_canonical_source_is_one_error_and_the_audit_is_not_compliant(self) -> None:
        self.assertEqual("COMPLIANT", audit.audit(self.root).result)
        (self.root / "CLAUDE.md").write_bytes(b"\xff\xfe\x00bad \x80\x81")
        result = audit.audit(self.root)
        self.assertEqual(("conforming", "ERRORS"), (result.authority, result.result))
        self.assertEqual(["CLAUDE.md"], [f.path for f in result.findings if f.severity == "ERROR"])
        self.assertEqual(["CLAUDE.md"], self._unread(result))

    def test_every_file_a_check_reads_silently_is_an_error_when_it_cannot_be_decoded(self) -> None:
        for rel in (
            "CLAUDE.md",
            MANIFEST_PATH,
            ".github/copilot-instructions.md",
            ".github/scripts/ai_config.py",
            "scripts/ai_config.py",
            "AGENTS.md",
            "src/AGENTS.md",
            ".github/workflows/parity.yml",
            ".github/workflows/parity.yaml",
        ):
            with self.subTest(rel=rel):
                path = self.root / rel
                original = path.read_bytes() if path.is_file() else None
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(UNDECODABLE)
                result = audit.audit(self.root)
                self.assertIn(rel, self._unread(result))
                self.assertNotEqual("COMPLIANT", result.result)
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)

    def test_an_unreadable_instruction_file_is_not_called_user_authored(self) -> None:
        for rel in ("AGENTS.md", ".github/copilot-instructions.md"):
            with self.subTest(rel=rel):
                original = (self.root / rel).read_bytes()
                (self.root / rel).write_bytes(UNDECODABLE)
                result = audit.audit(self.root)
                self.assertEqual([], [f for f in result.findings if f.check == "collision"])
                self.assertIn(rel, self._unread(result))
                (self.root / rel).write_bytes(original)

    def test_a_copilot_projection_compared_with_no_projected_section_is_a_parity_error(self) -> None:
        manifest = self._manifest()
        manifest["artifacts"][0].pop("hash")
        (self.root / MANIFEST_PATH).write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / "CLAUDE.md").write_text(
            "# CLAUDE.md\n\n## Maintaining AI Agent Config\n\nRegenerate. Never disable checks.\n", encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertIn(
            (
                ".github/copilot-instructions.md",
                "CLAUDE.md holds none of the sections the manifest projects, so the Copilot instructions were not "
                "compared",
            ),
            [(f.path, f.message) for f in result.findings if f.check == "parity" and f.severity == "ERROR"],
        )

    def test_the_banner_is_checked_beside_an_unreadable_canonical_source(self) -> None:
        manifest = self._manifest()
        manifest["artifacts"][0].pop("hash")
        (self.root / MANIFEST_PATH).write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / ".github/copilot-instructions.md").write_text(f"# {OWNERSHIP}\n\nAnything.\n", encoding="utf-8")
        (self.root / "CLAUDE.md").write_bytes(UNDECODABLE)
        result = audit.audit(self.root)
        self.assertIn(
            (".github/copilot-instructions.md", "Copilot instructions missing banner"),
            [(f.path, f.message) for f in result.findings if f.check == "parity"],
        )


class CodexLayeringTests(unittest.TestCase):
    """Codex takes, in each directory, the first of AGENTS.override.md, AGENTS.md, and its configured fallbacks that
    exists, skips it when it holds only whitespace, and reads CLAUDE.md only as a fallback a configuration names."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _codex(self, files: dict[str, str | bytes]) -> list[tuple[str, str | None, str]]:
        _write_fixture(self.root, files)
        findings = [
            *audit._layering_codex(self.root, audit._instruction_sources(self.root)),
            *audit._nested_codex_instructions(self.root, audit._instruction_sources(self.root)),
        ]
        return [(f.severity, f.path, f.message) for f in findings]

    def test_a_root_override_without_agents_md_masks_claude_md(self) -> None:
        self.assertEqual(
            [
                (
                    "ERROR",
                    "AGENTS.override.md",
                    "AGENTS.override.md masks CLAUDE.md — Codex loads the root override instead of AGENTS.md or a "
                    "fallback",
                )
            ],
            self._codex({"CLAUDE.md": "# Rules\n", "AGENTS.override.md": "Use spaces, never tabs.\n"}),
        )

    def test_a_root_override_beside_a_non_redirecting_agents_md_is_the_file_codex_loads(self) -> None:
        self.assertEqual(
            [
                (
                    "ERROR",
                    "AGENTS.override.md",
                    "AGENTS.override.md masks CLAUDE.md — Codex loads the root override instead of AGENTS.md or a "
                    "fallback",
                )
            ],
            self._codex({"CLAUDE.md": "# Rules\n", "AGENTS.md": PLAIN_AGENTS, "AGENTS.override.md": "Override.\n"}),
        )

    def test_a_root_override_beside_the_adapter_masks_the_adapter(self) -> None:
        self.assertEqual(
            [
                (
                    "ERROR",
                    "AGENTS.override.md",
                    "AGENTS.override.md masks the generated AGENTS.md adapter — Codex will not load CLAUDE.md "
                    "through the adapter",
                )
            ],
            self._codex({"AGENTS.md": REDIRECTING_AGENTS, "AGENTS.override.md": "Override.\n"}),
        )

    def test_an_empty_file_codex_chooses_is_skipped_without_trying_the_next(self) -> None:
        for name in ("AGENTS.override.md", "AGENTS.md"):
            with self.subTest(name=name):
                self.assertEqual(
                    [
                        (
                            "ERROR",
                            name,
                            f"Codex: the root {name} is empty — Codex chooses it, skips it, and loads no root "
                            "instructions",
                        )
                    ],
                    self._codex({"CLAUDE.md": "# Rules\n", "AGENTS.md": " \n\t\n", name: " \n\t\n"}),
                )
                (self.root / name).unlink()

    def test_the_adapter_and_a_plain_agents_md(self) -> None:
        self.assertEqual(
            [("INFO", None, "Codex: AGENTS.md adapter redirects to CLAUDE.md")],
            self._codex({"AGENTS.md": REDIRECTING_AGENTS}),
        )
        self.assertEqual(
            [("WARNING", None, "Codex: non-redirecting AGENTS.md — CLAUDE.md not loaded through adapter")],
            self._codex({"AGENTS.md": PLAIN_AGENTS}),
        )

    def test_claude_md_is_loaded_only_as_a_configured_fallback(self) -> None:
        self.assertEqual(
            [
                (
                    "WARNING",
                    None,
                    "Codex: no AGENTS.md, and .codex/config.toml does not name CLAUDE.md in "
                    "project_doc_fallback_filenames — Codex loads CLAUDE.md only when a user's own configuration "
                    "names it",
                )
            ],
            self._codex({"CLAUDE.md": "# Rules\n"}),
        )
        self.assertEqual(
            [
                (
                    "INFO",
                    None,
                    "Codex: no AGENTS.md, CLAUDE.md loaded as a project_doc_fallback_filenames entry in "
                    ".codex/config.toml",
                )
            ],
            self._codex({".codex/config.toml": 'project_doc_fallback_filenames = ["TEAM.md", "CLAUDE.md"]\n'}),
        )

    def test_the_first_configured_fallback_that_exists_is_the_one_codex_loads(self) -> None:
        self.assertEqual(
            [
                (
                    "WARNING",
                    "TEAM.md",
                    "Codex: no AGENTS.md, TEAM.md loaded as a project_doc_fallback_filenames entry in "
                    ".codex/config.toml — CLAUDE.md not loaded",
                )
            ],
            self._codex(
                {
                    "CLAUDE.md": "# Rules\n",
                    "TEAM.md": "# Team\n",
                    ".codex/config.toml": 'project_doc_fallback_filenames = ["TEAM.md", "CLAUDE.md"]\n',
                }
            ),
        )

    def test_the_size_cap_is_codexs_documented_default(self) -> None:
        self.assertEqual(32768, audit.CODEX_DEFAULT_MAX_BYTES)
        self.assertEqual("project_doc_max_bytes", audit.CODEX_SIZE_SETTING)

    def test_a_root_file_over_the_cap_is_read_in_part_and_one_under_or_at_it_whole(self) -> None:
        redirect = REDIRECTING_AGENTS.encode()
        adapter = ("INFO", None, "Codex: AGENTS.md adapter redirects to CLAUDE.md")
        for size, expected in (
            (32767, [adapter]),
            (32768, [adapter]),
            (
                32769,
                [
                    adapter,
                    (
                        "WARNING",
                        "AGENTS.md",
                        "Codex: the root AGENTS.md is 32769 bytes, over the 32768-byte project_doc_max_bytes cap — "
                        "Codex reads only its first 32768 bytes",
                    ),
                ],
            ),
        ):
            with self.subTest(size=size):
                self.assertEqual(expected, self._codex({"AGENTS.md": redirect + b" " * (size - len(redirect))}))

    def test_a_fallback_claude_md_over_a_configured_cap_is_read_in_part(self) -> None:
        self.assertEqual(
            [
                (
                    "INFO",
                    None,
                    "Codex: no AGENTS.md, CLAUDE.md loaded as a project_doc_fallback_filenames entry in "
                    ".codex/config.toml",
                ),
                (
                    "WARNING",
                    "CLAUDE.md",
                    "Codex: the root CLAUDE.md is 101 bytes, over the 100-byte project_doc_max_bytes cap — Codex "
                    "reads only its first 100 bytes",
                ),
            ],
            self._codex(
                {
                    "CLAUDE.md": "#" * 101,
                    ".codex/config.toml": 'project_doc_fallback_filenames = ["CLAUDE.md"]\n'
                    "project_doc_max_bytes = 100\n",
                }
            ),
        )

    def test_a_cap_that_is_not_a_positive_integer_leaves_codexs_default(self) -> None:
        for value in ("0", "-1", "true", '"100"', "1.5"):
            with self.subTest(value=value):
                _write_fixture(self.root, {".codex/config.toml": f"project_doc_max_bytes = {value}\n"})
                self.assertEqual(32768, audit._codex_max_bytes(self.root))
        _write_fixture(self.root, {".codex/config.toml": "project_doc_max_bytes = [\n"})
        self.assertEqual(32768, audit._codex_max_bytes(self.root))

    def test_an_override_over_the_cap_is_reported_beside_the_mask(self) -> None:
        self.assertEqual(
            ["ERROR", "WARNING"],
            [severity for severity, _, _ in self._codex({"AGENTS.override.md": "x" * 40000})],
        )

    def test_a_fallback_that_is_not_a_plain_file_name_is_ignored_as_codex_ignores_it(self) -> None:
        names = '"", ".", "..", "docs/CLAUDE.md", "a\\\\b", "c:d", "AGENTS.md", 3, "TEAM.md"'
        _write_fixture(self.root, {".codex/config.toml": f"project_doc_fallback_filenames = [{names}]\n"})
        self.assertEqual(["TEAM.md"], audit._codex_fallback_filenames(self.root))
        _write_fixture(self.root, {".codex/config.toml": "project_doc_fallback_filenames = [\n"})
        self.assertEqual([], audit._codex_fallback_filenames(self.root))

    def test_no_root_instruction_file_is_an_error(self) -> None:
        self.assertEqual(
            [("ERROR", None, "Codex: no AGENTS.override.md, AGENTS.md, or CLAUDE.md — no instructions available")],
            self._codex({"GEMINI.md": "# Gemini\n"}),
        )

    def test_a_nested_agents_md_beside_a_nested_override_is_never_loaded(self) -> None:
        rows = self._codex(
            {"AGENTS.md": REDIRECTING_AGENTS, "sub/AGENTS.override.md": "Override.\n", "sub/AGENTS.md": PLAIN_AGENTS}
        )
        self.assertEqual(["sub/AGENTS.override.md"], [path for _, path, _ in rows if path])

    def test_the_reported_repository_with_only_a_root_override_is_not_compliant(self) -> None:
        _setup_conforming_repo(self.root)
        (self.root / ".git").mkdir()
        (self.root / "AGENTS.md").unlink()
        manifest = json.loads((self.root / MANIFEST_PATH).read_text(encoding="utf-8"))
        manifest["artifacts"] = [a for a in manifest["artifacts"] if a["path"] != "AGENTS.md"]
        (self.root / MANIFEST_PATH).write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / "AGENTS.override.md").write_text("Use spaces, never tabs.\n", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertEqual("ERRORS", result.result)
        self.assertNotIn("Codex: no AGENTS.md, CLAUDE.md used via fallback", [f.message for f in result.findings])


class GeneratorPathTests(unittest.TestCase):
    """The skill runs the generator the engine found, which is one of the paths the engine recognizes."""

    SKILL_DIRECTORY = Path(audit.__file__).resolve().parent.parent

    def test_the_engine_recognizes_both_generator_paths(self) -> None:
        self.assertEqual([".github/scripts/ai_config.py", "scripts/ai_config.py"], audit.GENERATOR_CANDIDATES)

    def test_the_skill_takes_the_generator_from_the_engines_inventory_finding(self) -> None:
        skill = (self.SKILL_DIRECTORY / "SKILL.md").read_text(encoding="utf-8")
        step = skill[skill.index("## 3. Optional operational validation") :]
        self.assertIn('`{{PYTHON}} -B "<path>" --check`', step)
        self.assertIn("message begins `Generator script found:` in the engine's `--json` report", step)
        for candidate in audit.GENERATOR_CANDIDATES:
            self.assertNotIn(candidate, skill)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for candidate in reversed(audit.GENERATOR_CANDIDATES):
                _write_fixture(root, {candidate: "SOURCE = 'CLAUDE.md'\n"})
            found = [
                (f.path, f.message)
                for f in audit.AuditResult(findings=audit.check_inventory(root)).sorted_findings()
                if f.message.startswith("Generator script found: ")
            ]
        # The first finding is the generator the scope check reads, the first candidate that exists.
        self.assertEqual([(path, f"Generator script found: {path}") for path in audit.GENERATOR_CANDIDATES], found)

    def test_every_reference_that_names_a_generator_path_names_each_one(self) -> None:
        for reference in sorted((self.SKILL_DIRECTORY / "references").glob("*.md")):
            text = reference.read_text(encoding="utf-8")
            named = [candidate for candidate in audit.GENERATOR_CANDIDATES if f"`{candidate}`" in text]
            with self.subTest(reference=reference.name):
                self.assertIn(named, ([], audit.GENERATOR_CANDIDATES))


class ProvenanceTests(unittest.TestCase):
    """Every check names the guidance it derives from, and the report names it when asked."""

    SCRIPT = Path(audit.__file__).resolve()
    PROVENANCE = SCRIPT.parents[1] / "references" / "provenance.json"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo with spaces"
        (self.root / ".git").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _main(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            mock.patch.object(sys, "argv", ["audit_ai_config.py", "--root", str(self.root), *arguments]),
        ):
            code = audit.main()
        return code, output.getvalue()

    def named_checks(self) -> list[object]:
        """The check each Finding the engine builds names: its second argument, or its check keyword."""
        checks: list[object] = []
        for node in ast.walk(ast.parse(self.SCRIPT.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Finding":
                named = [keyword.value for keyword in node.keywords if keyword.arg == "check"]
                argument = named[0] if named else node.args[1]
                checks.append(argument.value if isinstance(argument, ast.Constant) else ast.unparse(argument))
        return checks

    def test_every_check_a_finding_can_name_is_a_literal_check(self) -> None:
        checks = self.named_checks()
        self.assertEqual([], [check for check in checks if check not in audit.CHECKS], "named, not in CHECKS")
        self.assertEqual([], sorted(audit.CHECKS - set(checks)), "in CHECKS, named by no finding")

    def test_every_check_has_a_provenance_entry_and_every_entry_names_a_check(self) -> None:
        entries = json.loads(self.PROVENANCE.read_text(encoding="utf-8"))["checks"]
        self.assertEqual([], sorted(audit.CHECKS - set(entries)), "checks without an entry")
        self.assertEqual([], sorted(set(entries) - audit.CHECKS), "entries without a check")
        self.assertEqual(sorted(audit.CHECKS), sorted(check_provenance.load(self.PROVENANCE, audit.CHECKS)))

    def test_every_check_the_policy_describes_is_a_check(self) -> None:
        policy = (self.PROVENANCE.parent / "audit-policy.md").read_text(encoding="utf-8")
        headings = re.findall(r"^### \d+\. .*\((.*)\)$", policy, re.MULTILINE)
        described = {name for heading in headings for name in re.findall(r"`([a-z-]+)`", heading)}
        self.assertEqual(sorted(audit.CHECKS), sorted(described))

    def test_the_markdown_report_names_the_sources_of_the_checks_it_reports_only_when_asked(self) -> None:
        code, plain = self._main()
        self.assertEqual(1, code)
        self.assertNotIn("### Sources", plain)
        code, report = self._main("--provenance")
        self.assertEqual(1, code)
        self.assertEqual(plain, report[: len(plain)])
        self.assertEqual(
            "\n### Sources\n\n- `authority`: decision — A repository counts as generated from CLAUDE.md only on the "
            "signals check 2 of references/audit-policy.md names, the layout of this source's retired init-ai-config "
            "generator, so a repository that keeps its instructions another way is never reported as drifted.\n",
            report[len(plain) :],
        )

    def test_a_page_source_names_its_url_and_passage(self) -> None:
        _setup_conforming_repo(self.root)
        _, report = self._main("--provenance")
        self.assertIn(
            "- `inventory`: https://code.claude.com/docs/en/memory — Claude Code reads CLAUDE.md from the project "
            "root, or from .claude/CLAUDE.md, at the start of every session.\n",
            report,
        )
        reported = {line.split("`")[1] for line in report.splitlines() if line.startswith("- `")}
        findings = {line.split(" | ")[1] for line in report.splitlines() if line.startswith("| ") and " | " in line}
        self.assertEqual(findings - {"Check", "---"}, reported)

    def test_the_json_report_carries_the_sources_by_check_only_when_asked(self) -> None:
        _, plain = self._main("--json")
        self.assertNotIn("provenance", json.loads(plain))
        _, report = self._main("--json", "--provenance")
        data = json.loads(report)
        self.assertEqual({check["check"] for check in data["findings"]}, set(data["provenance"]))
        self.assertEqual(
            [
                {
                    "decision": "A repository counts as generated from CLAUDE.md only on the signals check 2 of "
                    "references/audit-policy.md names, the layout of this source's retired init-ai-config generator, "
                    "so a repository that keeps its instructions another way is never reported as drifted."
                }
            ],
            data["provenance"]["authority"],
        )

    def test_a_report_without_findings_says_no_check_has_a_source(self) -> None:
        self.assertEqual(
            ["", "### Sources", "", "No findings, so no check to name a source for."], audit.source_markdown({})
        )

    def test_a_provenance_file_that_leaves_a_check_out_fails_the_run(self) -> None:
        broken = Path(self.temp.name) / "provenance.json"
        broken.write_text(json.dumps({"checks": {"mcp": [{"decision": "A."}]}}), encoding="utf-8")
        with mock.patch.object(audit, "PROVENANCE", broken):
            code, output = self._main("--provenance")
        self.assertEqual(1, code)
        self.assertEqual([f"FAILED {broken.as_posix()}: no entry for authority, "], [output.split("behavioral")[0]])

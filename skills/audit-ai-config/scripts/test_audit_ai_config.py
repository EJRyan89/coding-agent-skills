#!/usr/bin/env python3
"""Fixture tests for the AI agent configuration audit engine."""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import errno
import hashlib
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

import audit_ai_config as audit
import check_provenance
import folder_walk
import link_probe
import markdown_literal
import path_keys
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

    def test_a_manifest_naming_its_generator_or_source_in_no_text_is_a_warning_never_an_alternative(self) -> None:
        manifest = self.root / ".github/ai-config-manifest.json"
        manifest.parent.mkdir(parents=True)
        for document in (
            {"generatedBy": "x", "canonicalSource": {"path": "AGENTS.md"}},
            {"generatedBy": ["x"], "canonicalSource": "AGENTS.md"},
        ):
            with self.subTest(document=document):
                manifest.write_text(json.dumps(document), encoding="utf-8")
                cls, data, findings = audit.classify_authority(self.root)
                self.assertEqual("unconfigured", cls)
                self.assertEqual({}, data)
                self.assertEqual(
                    [("WARNING", "Manifest names its generator or source in no text")],
                    [(finding.severity, finding.message) for finding in findings if finding.severity != "INFO"],
                )

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

    def test_a_json_report_that_cannot_hold_a_value_fails_by_name_with_exit_1(self) -> None:
        # A root whose name is not UTF-8 on Linux reads as a lone surrogate, which no JSON text holds.
        (self.root / ".git").mkdir()
        found = audit.audit(self.root)
        found.repository = "repo\udcff"
        with mock.patch.object(audit, "audit", return_value=found):
            code, lines, errors = self._main("--root", str(self.root), "--json")
        self.assertEqual(1, code)
        self.assertEqual(1, len(lines), lines)
        self.assertTrue(lines[0].startswith("FAILED the JSON report cannot hold a value the audit found: "), lines)
        self.assertEqual("", errors)

    def test_the_relay_of_a_conforming_repository_names_each_surfaces_effective_sources(self) -> None:
        # The skill's step 2 reads the files the layering findings list as effective sources, from the relay alone.
        _setup_conforming_repo(self.root)
        (self.root / ".git").mkdir()
        manifest_path = self.root / ".github/ai-config-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["surfaces"] = ["copilot_cli", "code_review"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        layering = [
            f"FINDING\t{f.severity}\tlayering\t{f.path or ''}\t{'' if f.line is None else f.line}\t{f.message}"
            for f in audit.audit(self.root).sorted_findings()
            if f.check == "layering"
        ]
        _, lines, errors = self._main("--relay", "--root", str(self.root))
        self.assertEqual("", errors)
        self.assertIn(
            "FINDING\tINFO\tlayering\t\t\t"
            "Copilot CLI/app: effective sources: CLAUDE.md, AGENTS.md, .github/copilot-instructions.md",
            lines,
        )
        self.assertEqual(
            1, sum(line.startswith("FINDING\tINFO\tlayering\t\t\tCode review: effective sources: ") for line in lines)
        )
        self.assertEqual(layering, [line for line in lines if "\tlayering\t" in line])

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

    def test_a_json_report_is_printed_as_format_json_writes_it_with_nothing_after(self) -> None:
        (self.root / ".git").mkdir()
        found = audit.audit(self.root)
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            mock.patch.object(audit, "audit", return_value=found),
            mock.patch.object(sys, "argv", ["audit_ai_config.py", "--root", str(self.root), "--json"]),
        ):
            audit.main()
        self.assertEqual(audit.format_json(found), output.getvalue())

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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
        manifest["artifacts"].append({"path": "../outside/evil.md"})
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" and "path traversal" in f.message for f in result.findings),
        )

    def test_manifest_absolute_path_rejected(self) -> None:
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
        manifest["artifacts"].append({"path": "/etc/passwd"})
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" and "absolute path" in f.message for f in result.findings),
        )

    def test_manifest_backslash_traversal_rejected(self) -> None:
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
        manifest["artifacts"].append({"path": "..\\outside\\file.md"})
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" and "backslash" in f.message for f in result.findings),
        )

    def test_manifest_drive_qualified_rejected(self) -> None:
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
        manifest["artifacts"].append({"path": "C:/Windows/System32"})
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" and "drive-qualified" in f.message for f in result.findings),
        )

    def test_manifest_path_outside_allowlist_rejected(self) -> None:
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
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

        with mock.patch.object(Path, "read_bytes", side_effect=OSError("access denied")):
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

    def test_mcp_json_servers_claude_code_skips_are_errors_and_loaded_ones_pass(self) -> None:
        url = "https://mcp.example.com/mcp"
        skipped = {
            "remote": {"url": url},
            "blank-type": {"url": url, "type": " "},
            "in-process": {"type": "sdk"},
            "workspace": {"type": "http", "url": url},
            "Claude Browser": {"command": "browser"},
        }
        loaded = {
            "http": {"type": "http", "url": url},
            "alias": {"type": "streamable-http", "url": url},
            "events": {"type": "SSE", "url": url},
            "socket": {"type": "ws", "url": url},
            "local": {"command": "server", "url": url},
            "Workspace": {"command": "server"},
        }
        (self.root / ".mcp.json").write_text(json.dumps({"mcpServers": {**skipped, **loaded}}), encoding="utf-8")
        fix = 'add "type": "http" (or "sse" / "ws") to this entry'
        self.assertEqual(
            [
                f"Server 'remote': has a 'url' but no 'type', which Claude Code reads as stdio and skips; {fix}",
                f"Server 'blank-type': has a 'url' but no 'type', which Claude Code reads as stdio and skips; {fix}",
                "Server 'in-process': declares type 'sdk', which only an SDK host application can register, "
                "so Claude Code skips it",
                "Server 'workspace': Claude Code reserves this name for a built-in server and skips it; rename it",
                "Server 'Claude Browser': Claude Code reserves this name for a built-in server and skips it; rename it",
            ],
            [f.message for f in audit.check_mcp(self.root, {}) if f.severity == "ERROR"],
        )

    def test_only_mcp_json_is_held_to_what_claude_code_skips(self) -> None:
        servers = json.dumps({"remote": {"url": "https://mcp.example.com/mcp"}, "workspace": {"type": "sdk"}})
        for rel, wrapper in ((".github/mcp.json", "mcpServers"), (".vscode/mcp.json", "servers")):
            with self.subTest(rel=rel):
                path = self.root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'{{"{wrapper}": {servers}}}', encoding="utf-8")
                self.assertEqual(
                    ["Server 'workspace': must have 'command' (STDIO/local) or 'url' (HTTP/SSE)"],
                    [f.message for f in audit.read_mcp_json(self.root, rel)[1]],
                )

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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
        manifest["mcp_servers"] = [{"name": "bad", "transport": "sse", "targets": ["codex"]}]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "mcp" and "sse" in f.message and "codex" in f.message for f in result.findings),
        )

    def test_local_claude_rejected(self) -> None:
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["cloud_agent"]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "layering" and "redirects" in f.message for f in result.findings),
        )

    def test_cloud_agent_no_agents_md(self) -> None:
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["cloud_agent"]
        manifest["runtimes"] = ["claude"]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / "AGENTS.md").unlink()
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "layering" and "directly" in f.message for f in result.findings),
        )

    def test_cloud_agent_non_redirecting_agents_md(self) -> None:
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["cloud_agent"]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / "src").mkdir(exist_ok=True)
        (self.root / "src/AGENTS.md").write_text("# Nested\n\nSubtree instructions.\n", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "layering" and "Nested" in f.message and "supersedes" in f.message for f in result.findings),
        )

    def test_code_review_emits_warning(self) -> None:
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["code_review"]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "layering" and "code-review" in f.message.lower() for f in result.findings),
        )

    def test_vscode_emits_settings_warning(self) -> None:
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
        manifest["surfaces"] = ["vscode"]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "layering" and "useClaudeMdFile" in f.message for f in result.findings),
        )

    def test_the_folder_trust_warning_follows_the_copilot_cli_and_app_surfaces(self) -> None:
        # references/report-schema.md ties this warning to the Copilot CLI/app surfaces, not to an MCP target.
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
        for artifact in manifest["artifacts"]:
            if artifact["path"] == ".github/copilot-instructions.md":
                artifact.pop("hash", None)
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        # Rewrite with a custom title but keep the same sections from CLAUDE.md
        claude_content = audit.read_text(self.root, self.root / "CLAUDE.md")
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

    def test_a_heading_inside_a_code_fence_does_not_split_a_section(self) -> None:
        claude_content = (
            "# CLAUDE.md\n\n"
            "## Overview\n\nAn example:\n\n```markdown\n## Not a heading\n\nStill the overview.\n```\n\n"
            "~~~\n## Nor this\n~~~\n\nEnd of the overview.\n\n"
            "## Formatting Rules\n\nUse tabs.\n"
        )
        self.assertEqual(
            "## Overview\n\nAn example:\n\n```markdown\n## Not a heading\n\nStill the overview.\n```\n\n"
            "~~~\n## Nor this\n~~~\n\nEnd of the overview.",
            audit._extract_sections(claude_content, ["Overview"]),
        )
        self.assertEqual("", audit._extract_sections(claude_content, ["Not a heading", "Nor this"]))

    def test_parity_uses_manifest_declared_copilot_sections(self) -> None:
        manifest_path = self.root / ".github/ai-config-manifest.json"
        manifest = json.loads(audit.read_text(self.root, manifest_path))
        manifest["copilot_sections"] = ["Overview", "Maintaining AI Agent Config"]
        for artifact in manifest["artifacts"]:
            if artifact["path"] == ".github/copilot-instructions.md":
                artifact.pop("hash", None)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        claude_content = audit.read_text(self.root, self.root / "CLAUDE.md")
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
        self.assertIn(
            (
                "ERROR",
                "collision",
                ".github/copilot-instructions.md",
                "User-authored copilot-instructions.md would conflict with generated projection",
            ),
            [(f.severity, f.check, f.path, f.message) for f in result.findings],
        )
        marked = f"# Instructions\n\n> {OWNERSHIP}.\n"
        (self.root / ".github/copilot-instructions.md").write_text(marked, encoding="utf-8")
        self.assertEqual([], [f for f in audit.audit(self.root).findings if f.check == "collision"])


# The "Built-in agents" table of Copilot CLI's command reference, typed here rather than read from the constant under
# test: https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference#built-in-agents
BUILT_IN_AGENTS = ("code-review", "explore", "general-purpose", "research", "rubber-duck", "security-review", "task")


def builtin_collision(name: str) -> str:
    return (
        f"Custom agent '{name}' takes the name of a Copilot CLI built-in agent, which delegating to '{name}' always "
        "runs, so Copilot CLI never offers this agent to the model; rename it"
    )


class BuiltInAgentCollisionTests(unittest.TestCase):
    """Copilot CLI's changelog: a custom agent that shares a built-in agent's name is not offered to the model, since
    delegating to that name always runs the built-in agent. Its name is its name field, else its ID, in its own case."""

    def test_the_catalogue_is_the_command_references_built_in_agents(self) -> None:
        self.assertEqual(frozenset(BUILT_IN_AGENTS), audit.COPILOT_BUILTIN_AGENTS)

    def test_a_custom_agent_whose_id_is_any_built_in_agents_collides_in_both_folders(self) -> None:
        for name in BUILT_IN_AGENTS:
            with self.subTest(name=name):
                self.assertEqual(
                    [
                        ("ERROR", "collision", f".github/agents/{name}.agent.md", builtin_collision(name)),
                        ("ERROR", "collision", f".claude/agents/{name}.md", builtin_collision(name)),
                    ],
                    runtime_rows(
                        {
                            f".github/agents/{name}.agent.md": "---\ndescription: Mine.\n---\n",
                            f".claude/agents/{name}.md": f"---\nname: {name}\ndescription: Mine.\n---\n",
                        },
                        ("collision",),
                    ),
                )

    def test_the_name_field_collides_and_the_id_it_replaces_does_not(self) -> None:
        self.assertEqual(
            [
                ("ERROR", "collision", ".github/agents/helper.agent.md", builtin_collision("explore")),
                ("ERROR", "collision", ".claude/agents/reviewer.md", builtin_collision("task")),
            ],
            runtime_rows(
                {
                    # Named explore and task, whatever their files are called.
                    ".github/agents/helper.agent.md": "---\nname: explore\ndescription: Mine.\n---\n",
                    ".claude/agents/reviewer.md": "---\nname: task\ndescription: Mine.\n---\n",
                    # Named reviewer and helper: the name field, not the file, is what Copilot CLI offers.
                    ".github/agents/research.agent.md": "---\nname: reviewer\ndescription: Mine.\n---\n",
                    ".claude/agents/code-review.md": "---\nname: helper\ndescription: Mine.\n---\n",
                },
                ("collision",),
            ),
        )

    def test_a_name_in_another_case_does_not_collide(self) -> None:
        self.assertEqual(
            [],
            runtime_rows(
                {
                    ".github/agents/Explore.agent.md": "---\ndescription: Mine.\n---\n",
                    ".github/agents/other.agent.md": "---\nname: Code-Review\ndescription: Mine.\n---\n",
                    ".claude/agents/task.md": "---\nname: TASK\ndescription: Mine.\n---\n",
                },
                ("collision",),
            ),
        )

    def test_an_agent_in_a_subfolder_takes_the_folder_into_its_id(self) -> None:
        # Copilot's custom-agents page: an agent in a subdirectory of agents includes the subdirectory name in its ID,
        # the separator replaced with --, so team/explore.agent.md is team--explore, which no built-in agent is.
        self.assertEqual(
            [
                ("ERROR", "collision", ".github/agents/team/named.agent.md", builtin_collision("task")),
                ("ERROR", "collision", ".claude/agents/deep/er/reviewer.md", builtin_collision("research")),
            ],
            runtime_rows(
                {
                    ".github/agents/team/explore.agent.md": "---\ndescription: Mine.\n---\n",
                    ".github/agents/team/named.agent.md": "---\nname: task\ndescription: Mine.\n---\n",
                    ".claude/agents/deep/er/reviewer.md": "---\nname: research\ndescription: Mine.\n---\n",
                },
                ("collision",),
            ),
        )

    def test_the_id_of_an_agent_in_a_subfolder_joins_its_folders_with_two_hyphens(self) -> None:
        agents = Path("repo/.github/agents")
        self.assertEqual("team--reviewer", audit._agent_id(agents, agents / "team/reviewer.agent.md"))
        self.assertEqual("a--b--c", audit._agent_id(agents, agents / "a/b/c.md"))
        self.assertEqual("reviewer", audit._agent_id(agents, agents / "reviewer.md"))

    def test_an_id_a_subfolder_gives_characters_copilot_does_not_take_is_an_error(self) -> None:
        self.assertEqual(
            [
                (
                    "ERROR",
                    "copilot-agent",
                    ".github/agents/my team/reviewer.agent.md",
                    "Custom agent ID 'my team--reviewer', which Copilot takes from the file's path, holds characters "
                    "other than letters, digits, '.', '_', and '-'",
                )
            ],
            runtime_rows(
                {
                    ".github/agents/my team/reviewer.agent.md": "---\ndescription: Mine.\n---\n",
                    ".github/agents/team/reviewer.agent.md": "---\ndescription: Mine.\n---\n",
                },
                ("copilot-agent",),
            ),
        )

    def test_an_agent_in_a_subfolder_is_checked_like_one_at_the_top(self) -> None:
        self.assertEqual(
            [
                (
                    "ERROR",
                    "copilot-agent",
                    ".github/agents/team/reviewer.agent.md",
                    "Agent frontmatter requires description",
                ),
                ("ERROR", "copilot-agent", ".github/agents/team/reviewer.agent.md", "infer must be a boolean"),
                ("ERROR", "claude-agent", ".claude/agents/team/unnamed.md", "Agent frontmatter requires name"),
            ],
            runtime_rows(
                {
                    ".github/agents/team/reviewer.agent.md": "---\ninfer: sometimes\n---\n",
                    ".claude/agents/team/unnamed.md": "---\ndescription: Mine.\n---\n",
                    ".claude/agents/team/notes.txt": "not an agent",
                },
                ("copilot-agent", "claude-agent"),
            ),
        )

    def test_an_empty_name_falls_back_to_the_id(self) -> None:
        self.assertEqual(
            [("ERROR", "collision", ".github/agents/rubber-duck.agent.md", builtin_collision("rubber-duck"))],
            runtime_rows(
                {".github/agents/rubber-duck.agent.md": '---\nname: ""\ndescription: Mine.\n---\n'}, ("collision",)
            ),
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

    def _name_findings(self, name: str) -> dict[str, list[tuple[str, str, str]]]:
        """The findings for a skill named `name` in a folder named review-helper, in each folder a runtime reads."""
        folders = (".github/skills", ".claude/skills", ".agents/skills")
        for folder in folders:
            _write_fixture(self.root, {f"{folder}/review-helper/SKILL.md": f"---\nname: {name}\ndescription: d\n---\n"})
        found = audit.check_copilot_configuration(self.root, {})
        return {
            folder: [(f.severity, f.check, f.message) for f in found if f.path == f"{folder}/review-helper/SKILL.md"]
            for folder in folders
        }

    def test_a_name_that_differs_from_its_folder_is_an_error_only_where_codex_reads_it(self) -> None:
        # Copilot's add-skills page: the name "typically" matches its directory; Claude Code's skills page defaults it
        # to the directory's name; the Agent Skills specification, which Codex's skills page builds on, requires it.
        codex = "Skill name must match its directory, as the Agent Skills specification Codex builds on requires"
        self.assertEqual(
            {".github/skills": [], ".claude/skills": [], ".agents/skills": [("ERROR", "codex-skill", codex)]},
            self._name_findings("reviewer"),
        )
        self.assertEqual(
            {".github/skills": [], ".claude/skills": [], ".agents/skills": []}, self._name_findings("review-helper")
        )

    def test_a_name_copilot_cannot_read_is_an_error_in_every_folder_it_reads(self) -> None:
        # Copilot's add-skills page: the name "must be lowercase, using hyphens for spaces", whatever its folder.
        lowercase = ("ERROR", "copilot-skill", "Skill name must be lowercase hyphenated")
        found = self._name_findings("Review_Helper")
        self.assertEqual([lowercase], found[".github/skills"])
        self.assertEqual([lowercase], found[".claude/skills"])
        self.assertIn(lowercase, found[".agents/skills"])

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
                found = audit._validate_agent(agent, self.root, agent.parent)
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
                values, found = audit._frontmatter(self.root, skill)
                self.assertEqual(expected, values["name"])
                self.assertEqual([], found)
        agent = self.root / ".github/agents/demo.agent.md"
        agent.parent.mkdir(parents=True)
        agent.write_text(
            "---\ndescription: d # what it does\ntarget: vscode # editor only\ninfer: false # never\n"
            "modelPolicy: required # pinned\n---\n",
            encoding="utf-8",
        )
        self.assertEqual([], audit._validate_agent(agent, self.root, agent.parent))

    def test_frontmatter_the_shared_reader_would_accept_or_refuse_is_judged_line_by_line(self) -> None:
        # Pinned while _frontmatter keeps its own line walk beside ejryan89-skill-core's reader; see the comment above
        # it.
        unreadable = "Frontmatter must use key: value entries, block scalars, block lists, or nested mappings"
        mismatch = "Skill name must be lowercase hyphenated"
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
                found = audit._validate_agent(agent, self.root, agent.parent)
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
                values, found = audit._frontmatter(self.root, skill)
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
                values, found = audit._frontmatter(self.root, skill)
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
        path = self.root / ".github/agents/bad name.txt"
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

    def test_a_custom_agent_named_code_review_collides_and_a_skill_so_named_does_not(self) -> None:
        # GitHub's code review page recommends a review-focused skill directory name such as code-review; only a custom
        # agent takes the ID of Copilot CLI's built-in code-review agent.
        skill = self.root / ".github/skills/code-review/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: code-review\ndescription: Review.\n---\n", encoding="utf-8")
        agent = self.root / ".github/agents/code-review.agent.md"
        agent.parent.mkdir(parents=True)
        agent.write_text("---\ndescription: Review.\n---\n", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertEqual(
            [(".github/agents/code-review.agent.md", builtin_collision("code-review"))],
            [(f.path, f.message) for f in result.findings if f.check == "collision"],
        )
        self.assertEqual(
            [], [f for f in result.findings if f.severity == "ERROR" and (f.path or "").startswith(".github/skills/")]
        )

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

    def test_a_skill_that_cannot_be_read_is_one_error_and_no_rule_judges_its_body(self) -> None:
        _write_fixture(self.root, {".claude/skills/broken/SKILL.md": UNDECODABLE})
        found = [
            (f.severity, f.check)
            for f in audit.check_copilot_configuration(self.root, {})
            if f.path == ".claude/skills/broken/SKILL.md"
        ]
        self.assertEqual([("ERROR", "copilot-config"), ("ERROR", "copilot-skill"), ("ERROR", "copilot-skill")], found)

    def test_only_the_body_after_the_frontmatter_is_searched_for_arguments(self) -> None:
        # A rule (---) in the body does not end the frontmatter again, and $ARGUMENTS in the frontmatter is not read.
        hint = (
            "Skill reads $ARGUMENTS but gives no argument-hint, so Claude Code's autocomplete shows no "
            "expected arguments"
        )
        cases = {
            "---\nname: demo\ndescription: d\n---\nUse $ARGUMENTS here.\n\n---\n\nNothing more.\n": [hint],
            "---\nname: demo\ndescription: d\n---\nFirst part.\n\n---\n\nThen $ARGUMENTS.\n\n---\n\nEnd.\n": [hint],
            "---\nname: demo\ndescription: Takes $ARGUMENTS.\n---\nIt reads none.\n": [],
            "---\nname: demo\ndescription: Takes $ARGUMENTS.\n---\n": [],
            "---\nname: demo\ndescription: Takes $ARGUMENTS.\n": [],
            "Use $ARGUMENTS without frontmatter.\n": [hint],
        }
        skill = self.root / ".claude/skills/demo/SKILL.md"
        for text, expected in cases.items():
            with self.subTest(text=text):
                skill.write_text(text, encoding="utf-8")
                found = audit.check_copilot_configuration(self.root, {})
                self.assertEqual(expected, [f.message for f in found if f.check == "claude-skill"])

    def test_a_skill_md_in_another_case_is_audited_only_where_its_folder_folds_case(self) -> None:
        # A runtime opens SKILL.md, which is this file where the folder folds case: its broken frontmatter was never
        # reported, since the walk matched SKILL.md by its exact spelling.
        for skill_dir in (".github/skills", ".claude/skills", ".agents/skills"):
            folder = self.root / skill_dir / "alpha"
            _write_fixture(self.root, {f"{skill_dir}/alpha/skill.md": "---\nname: alpha\ndescription: 'open\n---\n"})
            for folds in (True, False):
                with self.subTest(skill_dir=skill_dir, folds=folds):
                    with mock.patch.dict(path_keys._FOLDS_CASE, {str(folder): folds}):
                        found = audit.check_copilot_configuration(self.root, {})
                    paths = {f.path for f in found if f.severity == "ERROR"}
                    self.assertEqual(folds, f"{skill_dir}/alpha/skill.md" in paths, paths)
            (folder / "skill.md").unlink()

    def test_a_skill_md_below_a_skill_s_folder_in_another_case_is_never_audited(self) -> None:
        # A runtime opens a skill's SKILL.md in the skill's own folder; a lowercase skill.md deeper, such as a
        # template, is no skill's main file in any folder, so it draws no finding where its folder folds case either.
        alpha = self.root / ".claude/skills/alpha"
        _write_fixture(
            self.root,
            {
                ".claude/skills/alpha/SKILL.md": "---\nname: alpha\ndescription: Does a thing.\n---\n",
                ".claude/skills/alpha/templates/skill.md": "# A template\n",
            },
        )
        folds = {str(alpha): True, str(alpha / "templates"): True}
        with mock.patch.dict(path_keys._FOLDS_CASE, folds):
            found = audit.check_copilot_configuration(self.root, {})
        self.assertEqual([], [(f.path, f.message) for f in found if "alpha" in (f.path or "")])

    @unittest.skipIf(sys.platform == "win32", "Windows makes a file symbolic link only with a privilege tests lack")
    def test_a_link_out_spelled_as_no_runtime_opens_it_draws_no_finding(self) -> None:
        # Where the folder keeps case, Skill.md is a file no runtime opens as the skill's, so its link out of the
        # repository is not the audit's to report; where the folder folds case it is the skill's file.
        alpha = self.root / ".claude/skills/alpha"
        alpha.mkdir(parents=True)
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "Skill.md"
            target.write_text("---\nname: alpha\ndescription: Elsewhere.\n---\n", encoding="utf-8")
            (alpha / "Skill.md").symlink_to(target)
            for folds in (False, True):
                with self.subTest(folds=folds), mock.patch.dict(path_keys._FOLDS_CASE, {str(alpha): folds}):
                    found = audit.check_copilot_configuration(self.root, {})
                    linked = [f.path for f in found if f.path == ".claude/skills/alpha/Skill.md"]
                    self.assertEqual([".claude/skills/alpha/Skill.md"] if folds else [], linked)

    def test_a_projection_in_another_case_is_owned_by_the_skill_md_the_manifest_names(self) -> None:
        # The manifest names a projection as the runtime opens it, SKILL.md, which is this file where the folder folds
        # case.
        marked = f"---\nname: project-skill\ndescription: Projected.\n---\n\n<!-- {OWNERSHIP} -->\n"
        _write_fixture(self.root, {".github/skills/project-skill/skill.md": marked})
        folder = self.root / ".github/skills/project-skill"
        manifest = self._manifest()
        manifest["artifacts"].append({"path": ".github/skills/project-skill/SKILL.md"})
        with mock.patch.dict(path_keys._FOLDS_CASE, {str(folder): True}):
            owned = audit.check_copilot_configuration(self.root, manifest)
            unowned = audit.check_copilot_configuration(self.root, self._manifest())
        self.assertEqual([], [f for f in owned if f.check == "provenance"])
        self.assertEqual(
            [
                (
                    ".github/skills/project-skill/skill.md",
                    "Generated Copilot skill projection is not owned by the manifest",
                )
            ],
            [(f.path, f.message) for f in unowned if f.check == "provenance"],
        )

    def test_each_unowned_projection_is_named_by_its_path_and_kind_and_an_owned_one_is_not(self) -> None:
        marked = f"---\nname: project-skill\ndescription: Projected.\n---\n\n<!-- {OWNERSHIP} -->\n"
        _write_fixture(
            self.root,
            {".github/skills/project-skill/SKILL.md": marked, ".github/agents/projected.agent.md": marked},
        )
        found = [
            (f.severity, f.path, f.message)
            for f in audit.check_copilot_configuration(self.root, self._manifest())
            if f.check == "provenance"
        ]
        self.assertEqual(
            [
                (
                    "ERROR",
                    ".github/skills/project-skill/SKILL.md",
                    "Generated Copilot skill projection is not owned by the manifest",
                ),
                (
                    "ERROR",
                    ".github/agents/projected.agent.md",
                    "Generated Copilot agent projection is not owned by the manifest",
                ),
            ],
            found,
        )
        manifest = self._manifest()
        manifest["artifacts"].extend(
            {"path": rel} for rel in (".github/skills/project-skill/SKILL.md", ".github/agents/projected.agent.md")
        )
        owned = audit.check_copilot_configuration(self.root, manifest)
        self.assertEqual([], [f for f in owned if f.check == "provenance"])

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

    def test_scope_without_manifest_or_generator_is_the_one_its_files_show(self) -> None:
        (self.root / ".github/ai-config-manifest.json").unlink()
        result = audit.audit(self.root)
        self.assertEqual("conforming", result.authority)
        _assert_scope_status(self, result, "file-derived")
        scope = [f for f in result.findings if f.check == "scope"]
        self.assertEqual(["INFO"], [f.severity for f in scope])
        self.assertIn("files show it serves", scope[0].message)

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

    def test_a_generator_scope_assigned_more_than_once_or_with_others_is_not_read(self) -> None:
        rest = "TARGET_SURFACES = []\nTARGET_FEATURES = []\n"
        script = self.root / ".github/scripts/ai_config.py"
        script.parent.mkdir(parents=True)
        for source in (
            f"TARGET_RUNTIMES = ['claude']\nTARGET_RUNTIMES = ['codex']\n{rest}",
            f"TARGET_RUNTIMES = OTHER = ['claude']\n{rest}",
            f"TARGET_RUNTIMES, OTHER = ['claude'], []\n{rest}",
            f"TARGET_RUNTIMES = ['claude']\nTARGET_RUNTIMES += ['codex']\n{rest}",
        ):
            with self.subTest(source=source):
                script.write_text(source, encoding="utf-8")
                self.assertEqual((None, "generator-scope-not-literal"), audit.derive_generator_scope(self.root))
        script.write_text(f"TARGET_RUNTIMES: list[str] = ['claude']\n{rest}", encoding="utf-8")
        self.assertEqual(
            ({"runtimes": ["claude"], "surfaces": [], "features": []}, "independently-derived"),
            audit.derive_generator_scope(self.root),
        )

    def test_the_maintaining_section_is_a_level_two_heading_as_markdown_reads_one(self) -> None:
        claude_md = self.root / "CLAUDE.md"
        for text, found in (
            ("# CLAUDE.md\n\n## Maintaining AI Agent Config\n", True),
            ("# CLAUDE.md\r\n\r\n   ##\tmaintaining ai agent config ##\r\n", True),
            ("## Maintaining\u00a0AI Agent Config\n", False),
            ("```\n## Maintaining AI Agent Config\n```\n", False),
            ("### Maintaining AI Agent Config\n", False),
            ("    ## Maintaining AI Agent Config\n", False),
        ):
            with self.subTest(text=text):
                claude_md.write_bytes(text.encode("utf-8"))
                self.assertEqual(found, audit._has_maintaining_section(self.root))

    def test_scope_statuses_and_generator_failure_reasons_match_report_schema(self) -> None:
        documented = {
            "independently-derived",
            "manifest-declared-only",
            "file-derived",
            "no-declared-scope",
            "not-applicable",
        }
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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
        manifest["runtimes"] = ["claude", "unknown_runtime"]
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "vocabulary" and "unknown_runtime" in f.message for f in result.findings),
        )

    def test_unknown_mcp_target_in_manifest_is_error(self) -> None:
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
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

    def test_json_output_is_two_space_indented_in_its_fields_order_with_text_past_ascii_kept(self) -> None:
        # references/report-schema.md documents the report, which a person reads, so its bytes are fixed.
        result = dataclasses.replace(audit.audit(self.root), repository="C:/caf\u00e9", findings=[])
        self.assertEqual(
            '{\n  "repository": "C:/caf\u00e9",\n  "authority": "conforming",\n  "scopeStatus": '
            + json.dumps(result.scope_status)
            + ',\n  "exitCode": 0,\n  "findings": []\n}\n',
            audit.format_json(result),
        )

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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
        manifest["schemaVersion"] = 999
        (self.root / ".github/ai-config-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.check == "authority" and "schema" in f.message.lower() for f in result.findings),
        )

    def test_artifact_non_string_path_caught(self) -> None:
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
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
        manifest = json.loads(audit.read_text(self.root, self.root / ".github/ai-config-manifest.json"))
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

    def test_a_manifest_owned_projection_in_another_case_is_noted_only_where_its_folder_folds_case(self) -> None:
        # The manifest records a skill projection as the runtime opens it, SKILL.md, which is this unmarked skill.md
        # where the folder folds case: it was looked up by its spelling on disk, so its limitation went unreported.
        folder = self.root / ".github/skills/demo"
        folder.mkdir(parents=True)
        (folder / "skill.md").write_text("---\nname: demo\ndescription: Demo.\n---\n", encoding="utf-8")
        manifest = {"artifacts": [{"path": ".github/skills/demo/SKILL.md"}]}
        note = (
            ".github/skills/demo/skill.md",
            "Generated Copilot projection is provenance-checked only, not reconstructed",
        )
        for folds in (True, False):
            with self.subTest(folds=folds), mock.patch.dict(path_keys._FOLDS_CASE, {str(folder): folds}):
                rows = [(f.path, f.message) for f in audit.check_limitations(self.root, manifest)]
                self.assertEqual(folds, note in rows, rows)

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
    # Copilot CLI finds modular instructions at the repository root and the working directory but not in the
    # directories between them, so a nested .github/instructions folder is no source of the repository's own.
    "nested_path_instructions": (
        {"CLAUDE.md": "# CLAUDE.md\n", "sub/.github/instructions/api.instructions.md": "Prefer async.\n"},
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
    # Copilot CLI's command reference reads .claude/rules/**/*.md the way it reads .github/instructions.
    "claude_rules_only": (
        {".claude/rules/api.md": "Prefer async.\n", ".claude/rules/frontend/ui.md": "Use tabs.\n"},
        {"runtimes": [], "surfaces": ["copilot_cli"]},
    ),
    "claude_rules_without_markdown": (
        {".claude/rules/notes.txt": "Not a rule.\n"},
        {"runtimes": [], "surfaces": ["copilot_cli"]},
    ),
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
        ("INFO", "layering", None, "JetBrains: path-specific instructions without .github/copilot-instructions.md"),
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
    "claude_rules_only": [
        ("INFO", "layering", None, "Copilot CLI/app: effective sources: .claude/rules"),
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
    "claude_rules_without_markdown": [
        ("ERROR", "layering", None, "Copilot CLI/app: no instruction sources available"),
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
    "nested_path_instructions": [
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


def test_an_instruction_file_code_review_reads_over_its_length_is_a_warning() -> None:
    # GitHub's customize-code-review page: limit any single instruction file to a maximum of about 1,000 lines.
    long, limit = "line\n" * 1001, "line\n" * 1000

    def over(rel: str) -> tuple[str, str, str | None, str]:
        return (
            "WARNING",
            "layering",
            rel,
            "1001 lines, over the about 1,000 lines GitHub advises for one instruction file Copilot code review reads; "
            "beyond it the quality of its reviews may deteriorate",
        )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write_fixture(
            root,
            {
                "AGENTS.md": long,
                "CLAUDE.md": long,
                # The inventory reports an unreadable fixed file; the files after it are still measured.
                "GEMINI.md": UNDECODABLE,
                "REVIEW.md": long,
                ".github/copilot-instructions.md": long,
                ".github/instructions/api.instructions.md": long,
                ".github/instructions/ui.instructions.md": limit,
                ".github/instructions/aa.instructions.md": UNDECODABLE,
            },
        )
        rows = [
            row
            for row in _finding_rows(audit.check_instruction_layering(root, {"surfaces": ["code_review"]}))
            if row[2]
        ]
    assert rows == [
        over("AGENTS.md"),
        over("CLAUDE.md"),
        over("REVIEW.md"),
        over(".github/copilot-instructions.md"),
        (
            "ERROR",
            "layering",
            ".github/instructions/aa.instructions.md",
            "Could not read, so its length was not checked",
        ),
        over(".github/instructions/api.instructions.md"),
    ]


def test_a_redirecting_agents_md_is_no_length_of_code_reviews() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write_fixture(root, {"AGENTS.md": REDIRECTING_AGENTS + "line\n" * 1001, "CLAUDE.md": "# CLAUDE.md\n"})
        assert audit._instruction_sources(root).agents_redirects
        findings = audit.check_instruction_layering(root, {"surfaces": ["code_review"]})
    assert [f for f in findings if f.path == "AGENTS.md"] == []


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
    "dot_claude_md_alone": {".claude/CLAUDE.md": PLAIN_CLAUDE},
    "maintained_dot_claude_md": {".claude/CLAUDE.md": MAINTAINED_CLAUDE},
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
    # Claude Code's memory page reads .claude/CLAUDE.md as the project's CLAUDE.md, so it is the same signal.
    "dot_claude_md_alone": (
        "ambiguous",
        "empty",
        [("INFO", "authority", None, "Authority classification: ambiguous (signals: claude_md_exists)")],
    ),
    "maintained_dot_claude_md": (
        "conforming",
        "empty",
        [
            (
                "INFO",
                "authority",
                None,
                "Authority classification: conforming (signals: claude_md_exists, maintaining_section)",
            )
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
UNREAD = "Could not read, so the checks that read it did not run:"

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
    "root_instruction_files_are_directories": {"AGENTS.md": DIRECTORY, "AGENTS.override.md": DIRECTORY},
    "claude_code_files": {
        ".claude/CLAUDE.md": "x",
        "CLAUDE.local.md": "x",
        ".claude/settings.json": "{}",
        ".claude/settings.local.json": "{}",
        ".claude/rules/testing.md": "x",
        ".claude/rules/frontend/ui.md": "x",
        ".claude/rules/notes.txt": "x",
    },
    "claude_rules_without_markdown": {".claude/rules/notes.txt": "x"},
    "claude_rules_is_a_file": {".claude/rules": "x"},
    "codex_agents_is_a_file": {".codex/agents": "x", ".codex/rules/default.rules": "x"},
    "codex_files": {
        ".codex/hooks.json": "{}",
        ".codex/agents/reviewer.toml": "x",
        ".codex/agents/explorer.toml": "x",
        ".codex/agents/notes.md": "x",
        ".codex/rules/default.rules": "x",
        ".codex/rules/nested/inner.rules": "x",
    },
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
    "skill_file_is_a_directory": {".claude/skills/demo/SKILL.md": DIRECTORY, ".claude/skills/alpha/SKILL.md": "x"},
    "runtime_folders_are_files": {".codex": "x", ".github": "x"},
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
    "instructions_is_a_file": {".github/instructions": "x"},
    "generator_scripts": {"scripts/ai_config.py": "x", ".github/scripts/ai_config.py": "x"},
    "generator_is_a_directory": {"scripts/ai_config.py": DIRECTORY},
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
    "fixed_file_is_a_directory": [
        ("ERROR", "inventory", "CLAUDE.md", f"{UNREAD} [Errno {errno.EISDIR}] not a file: <path>"),
    ],
    "root_instruction_files_are_directories": [
        ("ERROR", "inventory", "AGENTS.md", f"{UNREAD} [Errno {errno.EISDIR}] not a file: <path>"),
        ("ERROR", "inventory", "AGENTS.override.md", f"{UNREAD} [Errno {errno.EISDIR}] not a file: <path>"),
    ],
    "claude_code_files": [
        ("INFO", "inventory", ".claude/CLAUDE.md", "Found .claude/CLAUDE.md"),
        ("INFO", "inventory", "CLAUDE.local.md", "Found CLAUDE.local.md"),
        ("INFO", "inventory", ".claude/settings.json", "Found .claude/settings.json"),
        ("INFO", "inventory", ".claude/rules", "Rules found: frontend/ui.md, testing.md"),
    ],
    "claude_rules_without_markdown": [],
    "claude_rules_is_a_file": [
        ("ERROR", "inventory", ".claude/rules", f"{UNREAD} [Errno {errno.ENOTDIR}] not a directory: <path>"),
    ],
    "codex_agents_is_a_file": [
        ("ERROR", "inventory", ".codex/agents", f"{UNREAD} [Errno {errno.ENOTDIR}] not a directory: <path>"),
        ("INFO", "inventory", ".codex/rules", "Rules found: default.rules"),
    ],
    "codex_files": [
        ("INFO", "inventory", ".codex/hooks.json", "Found .codex/hooks.json"),
        ("INFO", "inventory", ".codex/agents", "Custom agents found: explorer.toml, reviewer.toml"),
        ("INFO", "inventory", ".codex/rules", "Rules found: default.rules"),
    ],
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
    "skill_directory_is_a_file": [
        ("ERROR", "inventory", ".agents/skills", f"{UNREAD} [Errno {errno.ENOTDIR}] not a directory: <path>"),
    ],
    "skill_file_is_a_directory": [
        ("INFO", "inventory", ".claude/skills", "Skills found: alpha"),
        ("ERROR", "inventory", ".claude/skills/demo/SKILL.md", f"{UNREAD} [Errno {errno.EISDIR}] not a file: <path>"),
    ],
    "runtime_folders_are_files": [
        ("ERROR", "inventory", ".codex", f"{UNREAD} [Errno {errno.ENOTDIR}] not a directory: <path>"),
        ("ERROR", "inventory", ".github", f"{UNREAD} [Errno {errno.ENOTDIR}] not a directory: <path>"),
    ],
    "agent_directories": [
        # Copilot CLI and Claude Code read the agents in a subfolder too.
        (
            "INFO",
            "inventory",
            ".github/agents",
            "Custom agent files found: Reviewer.md, author.agent.md, nested/inner.md",
        ),
        ("INFO", "inventory", ".claude/agents", "Custom agent files found: only-a-folder/inner.md"),
    ],
    "agent_directory_is_empty": [],
    "agent_directory_is_a_file": [
        ("ERROR", "inventory", ".github/agents", f"{UNREAD} [Errno {errno.ENOTDIR}] not a directory: <path>"),
    ],
    "path_specific_instructions": [
        ("INFO", "inventory", ".github/instructions", "2 path-specific instruction file(s)"),
    ],
    "instructions_without_matches": [],
    "instructions_is_a_file": [
        ("ERROR", "inventory", ".github/instructions", f"{UNREAD} [Errno {errno.ENOTDIR}] not a directory: <path>"),
    ],
    "generator_scripts": [
        ("INFO", "inventory", ".github/scripts/ai_config.py", "Generator script found: .github/scripts/ai_config.py"),
        ("INFO", "inventory", "scripts/ai_config.py", "Generator script found: scripts/ai_config.py"),
    ],
    "generator_is_a_directory": [
        ("ERROR", "inventory", "scripts/ai_config.py", f"{UNREAD} [Errno {errno.EISDIR}] not a file: <path>"),
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
    "workflows_is_a_file": [
        ("ERROR", "inventory", ".github/workflows", f"{UNREAD} [Errno {errno.ENOTDIR}] not a directory: <path>"),
    ],
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
        # An error names its path as the temporary folder spells it; a row names it <path>.
        return [
            (severity, check, path, message.replace(repr(str(root / path)), "<path>") if path else message)
            for severity, check, path, message in _finding_rows(audit.check_inventory(root))
        ]


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

    def test_the_relay_table_names_each_key_the_relay_prints_in_its_order(self) -> None:
        text = self.REFERENCE.read_text(encoding="utf-8")
        section = text[text.index("### Relay (for the skill)") : text.index("### JSON (for CI integration)")]
        documented = [line.split("|")[1].strip().strip("`") for line in section.splitlines() if line.startswith("| `")]
        sources: dict[str, tuple[check_provenance.Source, ...]] = {
            "mcp": (check_provenance.Source(page="https://example.test", text="A passage."),)
        }
        printed: list[str] = []
        for line in audit.format_relay(self._example(), sources).splitlines():
            key = re.split(r"[\t ]", line)[0]
            if key not in printed:
                printed.append(key)
        self.assertEqual(["REPOSITORY", "AUTHORITY", "SCOPE", "RESULT", "SUMMARY", "FINDING", "SOURCE"], documented)
        self.assertEqual(documented, printed)

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
            "claude-settings",
            "claude-hook",
            "claude-memory",
            "agent-skill",
            "claude-skill",
            "claude-agent",
            "codex-skill",
            "codex-config",
            "codex-hook",
            "codex-agent",
            "codex-rule",
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

    def test_the_relay_prints_the_relayed_findings_as_plain_lines(self) -> None:
        result = audit.AuditResult(
            repository="my\nrepo",
            authority="conforming",
            scope_status="file-derived",
            findings=[
                audit.Finding("INFO", "inventory", "CLAUDE.md", message="Found CLAUDE.md"),
                audit.Finding("INFO", "limitation", ".github/instructions", message="Not checked"),
                audit.Finding("WARNING", "mcp", message="Cannot validate statically"),
                audit.Finding("ERROR", "mcp", "a|b c.json", 3, "Server 'a|b\nc': bad\u2028more"),
            ],
        )
        self.assertEqual(
            "REPOSITORY\tmy repo\n"
            "AUTHORITY\tconforming\n"
            "SCOPE\tfile-derived\n"
            "RESULT ERRORS\n"
            "SUMMARY ERROR 1\n"
            "SUMMARY WARNING 1\n"
            "SUMMARY INFO 2\n"
            "SUMMARY INFO inventory 1\n"
            "SUMMARY INFO limitation 1\n"
            # A path holding punctuation and a message holding line breaks, each as plain text on its one line.
            "FINDING\tERROR\tmcp\ta|b c.json\t3\tServer 'a|b c': bad more\n"
            "FINDING\tWARNING\tmcp\t\t\tCannot validate statically\n"
            "FINDING\tINFO\tlimitation\t.github/instructions\t\tNot checked\n",
            audit.format_relay(result),
        )

    def test_the_relay_names_the_layering_findings_and_counts_the_other_info_ones(self) -> None:
        result = audit.AuditResult(
            repository="r",
            authority="conforming",
            scope_status="no-declared-scope",
            findings=[
                audit.Finding("INFO", "inventory", "CLAUDE.md", message="Found CLAUDE.md"),
                audit.Finding("INFO", "layering", message="Copilot CLI/app: effective sources: CLAUDE.md"),
            ],
        )
        self.assertEqual(
            ["FINDING\tINFO\tlayering\t\t\tCopilot CLI/app: effective sources: CLAUDE.md"],
            [line for line in audit.format_relay(result).splitlines() if line.startswith("FINDING")],
        )

    def test_the_relay_of_a_clean_audit_has_no_finding_lines(self) -> None:
        result = audit.AuditResult(repository="r", authority="conforming", scope_status="no-declared-scope")
        self.assertEqual(
            [
                "REPOSITORY\tr",
                "AUTHORITY\tconforming",
                "SCOPE\tno-declared-scope",
                "RESULT COMPLIANT",
                "SUMMARY ERROR 0",
                "SUMMARY WARNING 0",
                "SUMMARY INFO 0",
            ],
            audit.format_relay(result).splitlines(),
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

    def test_an_undecodable_skill_or_agent_is_an_error(self) -> None:
        for rel, check in (
            (".github/skills/demo/SKILL.md", "copilot-config"),
            (".github/agents/reviewer.md", "copilot-config"),
            (".claude/agents/reviewer.md", "claude-agent"),
        ):
            with self.subTest(rel=rel):
                path = self.root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(UNDECODABLE)
                errors = self._errors(rel)
                self.assertIn(check, [check for check, _ in errors])
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

    def test_a_directory_in_a_configuration_file_place_is_an_error_never_no_file(self) -> None:
        for rel, check, message in (
            (".mcp.json", "mcp", "Could not read or parse:"),
            (".claude/settings.json", "claude-settings", "Could not read or parse:"),
            (".codex/config.toml", "mcp", "Error reading config:"),
            (".codex/hooks.json", "codex-hook", "Could not read or parse:"),
        ):
            with self.subTest(rel=rel):
                (self.root / rel).mkdir(parents=True)
                errors = self._errors(rel)
                self.assertEqual([check], [check for check, _ in errors])
                self.assertTrue(errors[0][1].startswith(message), errors)
                self.assertIn("not a file", errors[0][1])
                (self.root / rel).rmdir()

    def test_a_directory_in_a_configuration_file_place_is_the_inventorys_error_where_its_check_does_not_run(
        self,
    ) -> None:
        # A manifest naming AGENTS.md as the canonical source is an alternative authority, so no check after the
        # classification runs, and the inventory's ERROR is the one report of the file.
        manifest = self.root / ".github/ai-config-manifest.json"
        manifest.write_text(json.dumps({"generatedBy": "gen.py", "canonicalSource": "AGENTS.md"}), encoding="utf-8")
        for rel in (".mcp.json", ".claude/settings.json", ".codex/config.toml", ".codex/hooks.json"):
            with self.subTest(rel=rel):
                (self.root / rel).mkdir(parents=True)
                result = audit.audit(self.root)
                errors = [(f.check, f.message) for f in result.findings if f.path == rel and f.severity == "ERROR"]
                self.assertEqual("alternative", result.authority)
                self.assertEqual(["inventory"], [check for check, _ in errors])
                self.assertTrue(errors[0][1].startswith(UNREAD), errors)
                (self.root / rel).rmdir()

    def test_only_the_reading_checks_error_takes_the_place_of_the_inventorys(self) -> None:
        unread = audit.Finding("ERROR", "inventory", ".mcp.json", message=f"{UNREAD} not a file")
        warning = audit.Finding("WARNING", "mcp", ".mcp.json", message="Server 'x': no command")
        self.assertEqual([unread, warning], audit._left_to_reading_check([unread, warning]))

    def test_a_configuration_file_its_check_cannot_parse_is_still_found_by_the_inventory(self) -> None:
        (self.root / ".mcp.json").write_text("{", encoding="utf-8")
        rows = [(f.severity, f.check) for f in audit.audit(self.root).findings if f.path == ".mcp.json"]
        self.assertEqual([("INFO", "inventory"), ("ERROR", "mcp")], rows)

    def test_an_undecodable_file_the_length_checks_read_is_an_inventory_error(self) -> None:
        # CLAUDE.local.md is the memory check's, GEMINI.md and REVIEW.md the code-review length check's: one they
        # could not read would otherwise pass as a file within its length.
        for rel in ("CLAUDE.local.md", "GEMINI.md", "REVIEW.md"):
            with self.subTest(rel=rel):
                (self.root / rel).write_bytes(UNDECODABLE + b"\nline" * 1001)
                errors = self._errors(rel)
                self.assertEqual(["inventory"], [check for check, _ in errors])
                self.assertTrue(
                    errors[0][1].startswith("Could not read, so the checks that read it did not run:"), errors
                )
                (self.root / rel).unlink()

    def test_a_directory_in_the_manifest_place_is_a_malformed_manifest_never_none(self) -> None:
        manifest = self.root / ".github/ai-config-manifest.json"
        manifest.unlink()
        manifest.mkdir()
        self.assertEqual(
            [("WARNING", "Manifest exists but is malformed")],
            [(finding.severity, finding.message) for finding in audit._manifest_signal(self.root)[2]],
        )

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

    def _not_audited(self, result: audit.AuditResult) -> list[tuple[str, str, str | None]]:
        return sorted(
            (f.severity, f.check, f.path) for f in result.findings if f.message.startswith(audit.SKIPPED_FOLDER)
        )

    def test_a_nested_checkout_in_a_configuration_folder_is_named_as_not_audited(self) -> None:
        checkouts = (
            ".github/agents/shared",
            ".claude/agents/team/shared",
            ".claude/rules/shared",
            ".github/instructions/shared",
            ".claude/skills/shared",
        )
        _write_fixture(
            self.root,
            {
                ".github/agents/shared/x.agent.md": "---\nname: x\n---\n",
                ".claude/agents/team/shared/y.md": "---\nname: y\n---\n",
                ".claude/rules/shared/rule.md": "A rule.\n",
                ".github/instructions/shared/a.instructions.md": "---\napplyTo: '**'\n---\nA.\n",
                ".claude/skills/shared/SKILL.md": "---\nname: shared\ndescription: Shared.\n---\n",
                # A nested checkout below the root is another repository's, which the repository walk skips silently.
                "vendor/clone/AGENTS.md": "# Nested\n",
            },
        )
        for checkout in (*checkouts, "vendor/clone"):
            (self.root / checkout / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertEqual(sorted(("WARNING", "inventory", path) for path in checkouts), self._not_audited(result))
        message = next(f.message for f in result.findings if f.path == ".github/agents/shared")
        self.assertEqual(f"{audit.SKIPPED_FOLDER}{folder_walk.NESTED_CHECKOUT_REASON}", message)
        # Nothing behind a checkout is listed or validated as the repository's own.
        for finding in result.findings:
            self.assertNotIn("x.agent.md", finding.message, finding)
            self.assertFalse((finding.path or "").startswith(tuple(f"{path}/" for path in checkouts)), finding)

    def test_a_link_back_into_an_agents_folder_is_named_as_not_audited(self) -> None:
        _write_fixture(self.root, {".claude/agents/team/reviewer.md": "---\nname: r\ndescription: R.\n---\n"})
        _link_directory(self.root / ".claude/agents", self.root / ".claude/agents/team/back")
        result = audit.audit(self.root)
        self.assertEqual([("WARNING", "inventory", ".claude/agents/team/back")], self._not_audited(result))
        message = next(f.message for f in result.findings if f.path == ".claude/agents/team/back")
        self.assertEqual(f"{audit.SKIPPED_FOLDER}{folder_walk.LOOP_REASON}", message)

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


# A repository in this source's own layout: a CLAUDE.md, an AGENTS.md that redirects to it, and no manifest.
FILE_SCOPE_REPOSITORY: dict[str, str | bytes] = {
    "CLAUDE.md": MAINTAINED_CLAUDE,
    "AGENTS.md": "Before working here, read `CLAUDE.md` and apply it as the authoritative instructions.\n",
    "deployer/AGENTS.md": PLAIN_AGENTS,
    ".github/pull_request_template.md": "## Summary\n",
}


class FileDerivedScopeTests(unittest.TestCase):
    """Without a manifest or a generator, the layering checks cover what the repository's files show it serves."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _audit(self, files: dict[str, str | bytes]) -> audit.AuditResult:
        _write_fixture(self.root, files)
        result = audit.audit(self.root)
        self.assertEqual("conforming", result.authority)
        return result

    def test_a_repository_without_a_manifest_gets_the_layering_its_files_show(self) -> None:
        result = self._audit(FILE_SCOPE_REPOSITORY)
        _assert_scope_status(self, result, "file-derived")
        self.assertEqual(
            [
                (
                    "INFO",
                    None,
                    "No AI-config manifest or recognized generator declares target runtimes or surfaces; the layering "
                    "checks cover what the repository's files show it serves: runtimes claude, codex; surfaces "
                    "copilot_cli, code_review",
                )
            ],
            [(f.severity, f.path, f.message) for f in result.findings if f.check == "scope"],
        )
        self.assertEqual(
            [
                ("INFO", None, "Codex: AGENTS.md adapter redirects to CLAUDE.md"),
                (
                    "WARNING",
                    "deployer/AGENTS.md",
                    "Nested AGENTS.md adds instructions alongside the root adapter in this subtree — review for "
                    "conflicting or redundant guidance",
                ),
                ("INFO", None, "Copilot CLI/app: effective sources: CLAUDE.md, AGENTS.md"),
                ("INFO", None, "Code review: effective sources: CLAUDE.md"),
            ],
            [
                (f.severity, f.path, f.message)
                for f in result.findings
                if f.check == "layering"
                and f.message.startswith(("Codex", "Nested", "Copilot CLI/app: eff", "Code review"))
            ],
        )

    def test_a_hand_written_agents_md_is_no_collision_without_a_generator(self) -> None:
        result = self._audit({**FILE_SCOPE_REPOSITORY, "AGENTS.md": PLAIN_AGENTS})
        self.assertEqual([], [f for f in result.findings if f.check == "collision"])
        self.assertIn(
            ("WARNING", "Codex: non-redirecting AGENTS.md — CLAUDE.md not loaded through adapter"),
            [(f.severity, f.message) for f in result.findings if f.check == "layering"],
        )

    def test_a_manifest_keeps_the_scope_it_declares(self) -> None:
        _setup_conforming_repo(self.root)
        result = audit.audit(self.root)
        _assert_scope_status(self, result, "manifest-declared-only")
        layering = [f.message for f in result.findings if f.check == "layering"]
        self.assertIn("Codex: AGENTS.md adapter redirects to CLAUDE.md", layering)
        self.assertIn(
            "Copilot CLI/app: effective sources: CLAUDE.md, AGENTS.md, .github/copilot-instructions.md", layering
        )
        self.assertEqual([], [message for message in layering if message.startswith("Code review")])

    def test_each_file_shows_its_runtime_or_surface(self) -> None:
        cases: dict[str, tuple[dict[str, str | bytes], dict[str, list[str]]]] = {
            "nothing": ({}, {}),
            "root CLAUDE.md": ({"CLAUDE.md": "x"}, {"runtimes": ["claude"], "surfaces": []}),
            ".claude/CLAUDE.md": ({".claude/CLAUDE.md": "x"}, {"runtimes": ["claude"], "surfaces": []}),
            "root AGENTS.md": ({"AGENTS.md": "x"}, {"runtimes": ["claude", "codex"], "surfaces": []}),
            "AGENTS.override.md": ({"AGENTS.override.md": "x"}, {"runtimes": ["codex"], "surfaces": []}),
            ".codex folder": ({".codex/hooks.json": "{}"}, {"runtimes": ["codex"], "surfaces": []}),
            ".agents/skills": ({".agents/skills/demo/SKILL.md": "x"}, {"runtimes": ["codex"], "surfaces": []}),
            ".github folder": (
                {".github/CODEOWNERS": "x"},
                {"runtimes": [], "surfaces": ["copilot_cli", "code_review"]},
            ),
            "cloud agent setup": (
                {".github/workflows/copilot-setup-steps.yml": "x"},
                {"runtimes": [], "surfaces": ["copilot_cli", "code_review", "cloud_agent"]},
            ),
        }
        for name, (files, expected) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                _write_fixture(Path(temporary), files)
                self.assertEqual(expected, audit.derive_file_scope(Path(temporary)))

    def test_a_repository_whose_only_instructions_are_agents_md_serves_claude_code_and_codex(self) -> None:
        # Claude Code's memory page: Claude Code reads AGENTS.md as the project's instructions when there is no
        # CLAUDE.md, from v2.1.277 on, and not while a CLAUDE.local.md is present.
        result = self._audit(
            {
                "AGENTS.md": PLAIN_AGENTS,
                "scripts/ai_config.py": GENERATOR_NAMING_CLAUDE,
                ".github/workflows/parity.yml": PARITY_WORKFLOW,
            }
        )
        _assert_scope_status(self, result, "file-derived")
        self.assertEqual(
            [
                (
                    "INFO",
                    None,
                    "No AI-config manifest or recognized generator declares target runtimes or surfaces; the layering "
                    "checks cover what the repository's files show it serves: runtimes claude, codex; surfaces "
                    "copilot_cli, code_review",
                ),
                (
                    "INFO",
                    "AGENTS.md",
                    "Claude Code reads AGENTS.md as the project's instructions, since no CLAUDE.md is present, from "
                    "v2.1.277 on and while no CLAUDE.local.md is; an earlier Claude Code reads no project instructions "
                    "here",
                ),
            ],
            [(f.severity, f.path, f.message) for f in result.findings if f.check == "scope"],
        )

    def test_a_claude_local_md_keeps_claude_code_from_reading_agents_md(self) -> None:
        # Claude Code reads AGENTS.md in place of a CLAUDE.md only while no CLAUDE.local.md is present, so beside one
        # AGENTS.md brings in Codex alone, and no scope INFO says Claude Code reads it; a CLAUDE.md still brings it in.
        cases: dict[str, tuple[dict[str, str | bytes], dict[str, list[str]]]] = {
            "AGENTS.md beside CLAUDE.local.md": (
                {"AGENTS.md": "x", "CLAUDE.local.md": "x"},
                {"runtimes": ["codex"], "surfaces": []},
            ),
            "AGENTS.md alone": ({"AGENTS.md": "x"}, {"runtimes": ["claude", "codex"], "surfaces": []}),
            "CLAUDE.local.md alone": ({"CLAUDE.local.md": "x"}, {}),
            "CLAUDE.md beside CLAUDE.local.md and AGENTS.md": (
                {"CLAUDE.md": "x", "CLAUDE.local.md": "x", "AGENTS.md": "x"},
                {"runtimes": ["claude", "codex"], "surfaces": []},
            ),
        }
        for name, (files, expected) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                _write_fixture(Path(temporary), files)
                self.assertEqual(expected, audit.derive_file_scope(Path(temporary)))
        result = self._audit(
            {
                "AGENTS.md": PLAIN_AGENTS,
                "CLAUDE.local.md": "# Personal\n",
                "scripts/ai_config.py": GENERATOR_NAMING_CLAUDE,
                ".github/workflows/parity.yml": PARITY_WORKFLOW,
            }
        )
        self.assertEqual(
            [
                (
                    "INFO",
                    None,
                    "No AI-config manifest or recognized generator declares target runtimes or surfaces; the layering "
                    "checks cover what the repository's files show it serves: runtimes codex; surfaces copilot_cli, "
                    "code_review",
                )
            ],
            [(f.severity, f.path, f.message) for f in result.findings if f.check == "scope"],
        )

    def test_a_scope_no_file_shows_names_no_runtime(self) -> None:
        status, findings = audit.check_scope_and_roles(self.root, {})
        self.assertEqual(("no-declared-scope", ["scope"]), (status, [f.check for f in findings]))

    def test_a_repository_whose_files_show_nothing_says_the_checks_did_not_run(self) -> None:
        self.assertEqual(
            (
                "no-declared-scope",
                audit.Finding(
                    "INFO",
                    "scope",
                    message="No AI-config manifest or recognized generator declares target runtimes or surfaces, and "
                    "no file shows one; target-specific checks were not run",
                ),
            ),
            audit._file_scope_finding({}),
        )


SETTINGS = ".claude/settings.json"


def claude_code_rows(files: dict[str, str | bytes]) -> list[tuple[str, str, str | None, str]]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write_fixture(root, files)
        return [
            (
                f.severity,
                f.check,
                f.path,
                f.message.split(":")[0] if "Could not read or parse" in f.message else f.message,
            )
            for f in audit.check_claude_code(root)
        ]


def settings_rows(settings: Any) -> list[tuple[str, str, str | None, str]]:
    return claude_code_rows({SETTINGS: json.dumps(settings)})


def command_hook(**fields: Any) -> dict[str, Any]:
    return {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", **fields}]}]}}


class ClaudeCodeConfigurationTests(unittest.TestCase):
    """Claude Code's shared settings, hooks, CLAUDE.md files, imports, and rules, read as its own pages describe."""

    def test_settings_that_do_not_parse_are_a_read_failure(self) -> None:
        self.assertEqual(
            [("ERROR", "claude-settings", SETTINGS, "Could not read or parse")], claude_code_rows({SETTINGS: "{bad"})
        )
        self.assertEqual(
            [("ERROR", "claude-settings", SETTINGS, "Top-level value must be an object")], settings_rows([])
        )

    def test_this_repositorys_settings_shape_is_clean(self) -> None:
        settings = {
            "$schema": "https://json.schemastore.org/claude-code-settings.json",
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [{"type": "command", "command": "python guard.py", "timeout": 30}],
                    }
                ]
            },
            "attribution": {"commit": "", "pr": ""},
        }
        self.assertEqual([], settings_rows(settings))

    def test_shared_permissions_are_named_and_a_grant_is_a_warning(self) -> None:
        grant = (
            "WARNING",
            "claude-settings",
            SETTINGS,
            "The shared settings grant permissions.allow and permissions.additionalDirectories to everyone who trusts "
            "this folder; personal approvals belong in .claude/settings.local.json or ~/.claude/settings.json",
        )
        self.assertEqual(
            [grant], settings_rows({"permissions": {"allow": ["Bash(git *)"], "additionalDirectories": ["../x"]}})
        )
        self.assertEqual(
            [
                (
                    "INFO",
                    "claude-settings",
                    SETTINGS,
                    "The shared settings set permissions.deny, permissions.ask for everyone who opens the repository",
                )
            ],
            settings_rows({"permissions": {"deny": ["Read(.env)"], "ask": ["Bash(git push *)"], "allow": []}}),
        )
        self.assertEqual([], settings_rows({"permissions": {}}))

    def test_a_default_mode_is_checked_against_the_settings_reference(self) -> None:
        self.assertEqual(
            [
                (
                    "WARNING",
                    "claude-settings",
                    SETTINGS,
                    "permissions.defaultMode 'bypassPermissions' does not take effect from project settings; set it "
                    "in ~/.claude/settings.json",
                ),
                (
                    "INFO",
                    "claude-settings",
                    SETTINGS,
                    "The shared settings set permissions.defaultMode for everyone who opens the repository",
                ),
            ],
            settings_rows({"permissions": {"defaultMode": "bypassPermissions"}}),
        )
        self.assertEqual(
            [
                (
                    "ERROR",
                    "claude-settings",
                    SETTINGS,
                    "permissions.defaultMode 'yolo' is not a mode Claude Code knows",
                ),
                (
                    "INFO",
                    "claude-settings",
                    SETTINGS,
                    "The shared settings set permissions.defaultMode for everyone who opens the repository",
                ),
            ],
            settings_rows({"permissions": {"defaultMode": "yolo"}}),
        )
        self.assertEqual(
            [("ERROR", "claude-settings", SETTINGS, "'permissions' must be an object")],
            settings_rows({"permissions": ["allow"]}),
        )

    def test_a_value_that_is_not_text_is_an_error_never_a_crash(self) -> None:
        set_mode = (
            "INFO",
            "claude-settings",
            SETTINGS,
            "The shared settings set permissions.defaultMode for everyone who opens the repository",
        )
        for mode in (["plan"], {"mode": "plan"}):
            with self.subTest(mode=mode):
                self.assertEqual(
                    [
                        (
                            "ERROR",
                            "claude-settings",
                            SETTINGS,
                            f"permissions.defaultMode {mode!r} is not a mode Claude Code knows",
                        ),
                        set_mode,
                    ],
                    settings_rows({"permissions": {"defaultMode": mode}}),
                )
        for kind in (["command"], {"type": "command"}):
            with self.subTest(kind=kind):
                self.assertEqual(
                    [
                        (
                            "ERROR",
                            "claude-hook",
                            SETTINGS,
                            f"Hook on 'PreToolUse': type {kind!r} is not one Claude Code runs",
                        )
                    ],
                    settings_rows(
                        {"hooks": {"PreToolUse": [{"hooks": [{"type": kind, "command": "x", "timeout": 5}]}]}}
                    ),
                )

    def test_a_hook_is_read_as_the_hooks_page_gives_it(self) -> None:
        cases: dict[str, tuple[dict[str, Any], list[tuple[str, str]]]] = {
            "conforming": (command_hook(command="x", timeout=30, onFailure="block"), []),
            "no timeout": (
                command_hook(command="x"),
                [("WARNING", "Hook on 'PreToolUse' sets no timeout, so Claude Code waits up to 600 seconds for it")],
            ),
            "timeout not a number": (
                command_hook(command="x", timeout="30"),
                [("ERROR", "Hook on 'PreToolUse': timeout must be a positive number of seconds")],
            ),
            "no command": (
                command_hook(command=" ", timeout=5),
                [("ERROR", "Hook on 'PreToolUse': a command hook needs 'command'")],
            ),
            "unknown onFailure": (
                command_hook(command="x", timeout=5, onFailure="ignore"),
                [("ERROR", "Hook on 'PreToolUse': onFailure must be 'continue' or 'block'")],
            ),
            "unknown event": (
                {"hooks": {"BeforeTool": [], "PreToolUse": []}},
                [("ERROR", "Hook event 'BeforeTool' is not one Claude Code knows, so its hooks never run")],
            ),
            "unknown type": (
                {"hooks": {"Stop": [{"hooks": [{"type": "shell", "command": "x"}]}]}},
                [("ERROR", "Hook on 'Stop': type 'shell' is not one Claude Code runs")],
            ),
            "prompt hook with onFailure and its short default": (
                {"hooks": {"Stop": [{"hooks": [{"type": "prompt", "prompt": "Check.", "onFailure": "block"}]}]}},
                [("ERROR", "Hook on 'Stop': onFailure applies to command and http hooks only")],
            ),
            "an event with a short default": (
                {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "x"}]}]}},
                [],
            ),
            "http and mcp_tool fields": (
                {
                    "hooks": {
                        "PostToolUse": [
                            {
                                "hooks": [
                                    {"type": "http", "timeout": 5},
                                    {"type": "mcp_tool", "server": "s", "timeout": 5},
                                ]
                            }
                        ]
                    }
                },
                [
                    ("ERROR", "Hook on 'PostToolUse': a http hook needs 'url'"),
                    ("ERROR", "Hook on 'PostToolUse': a mcp_tool hook needs 'tool'"),
                ],
            ),
            "malformed levels": (
                {
                    "hooks": {
                        "Stop": {"hooks": []},
                        "PreToolUse": [{"matcher": "Bash"}],
                        "PostToolUse": [{"hooks": ["x"]}],
                    }
                },
                [
                    ("ERROR", "Hook event 'Stop': must be a list of matcher groups"),
                    ("ERROR", "Hook event 'PreToolUse': each matcher group needs a 'hooks' list"),
                    ("ERROR", "Hook on 'PostToolUse': each handler must be an object"),
                ],
            ),
            "hooks not an object": (
                {"hooks": ["PreToolUse"]},
                [("ERROR", "'hooks' must be an object mapping event names to matcher groups")],
            ),
            "a matcher that is not text": (
                {"hooks": {"PreToolUse": [{"matcher": {"tool": "Bash"}, "hooks": []}, {"matcher": "", "hooks": []}]}},
                [("ERROR", "Hook event 'PreToolUse': a matcher must be text naming the tools it matches")],
            ),
        }
        for name, (settings, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(
                    [(severity, "claude-hook", SETTINGS, message) for severity, message in expected],
                    settings_rows(settings),
                )

    def test_a_claude_md_over_the_target_length_is_a_warning(self) -> None:
        for name in ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md"):
            with self.subTest(name=name):
                self.assertEqual([], claude_code_rows({name: "line\n" * 200}))
                self.assertEqual(
                    [
                        (
                            "WARNING",
                            "claude-memory",
                            name,
                            "201 lines, over the 200 lines Claude Code's memory page targets; longer files consume "
                            "more context and reduce adherence",
                        )
                    ],
                    claude_code_rows({name: "line\n" * 201}),
                )

    def test_imports_outside_code_are_named_and_a_missing_one_is_a_warning(self) -> None:
        text = (
            "See @docs/guide.md and @docs/present.md, mail a@example.com, ask @maintainer,\n"
            "and keep `@literal.md` literal.\n"
            "```text\n@fenced.md\n```\n"
            "- @~/.claude/personal.md\n"
        )
        self.assertEqual(
            ["docs/guide.md", "docs/present.md", "maintainer", "~/.claude/personal.md"], audit.claude_imports(text)
        )
        self.assertEqual(
            [
                (
                    "INFO",
                    "claude-memory",
                    "CLAUDE.md",
                    "Imports @docs/guide.md, @docs/present.md, @maintainer, @~/.claude/personal.md; imported files "
                    "load together with this file, so they organize it without reducing its context cost",
                ),
                (
                    "WARNING",
                    "claude-memory",
                    "CLAUDE.md",
                    "Import @docs/guide.md names no file, so nothing loads for it",
                ),
            ],
            claude_code_rows({"CLAUDE.md": text, "docs/present.md": "x"}),
        )

    def test_an_import_inside_a_fence_is_none_as_markdown_reads_the_fence(self) -> None:
        # A closing fence takes no info string, so a ``` text line is body; a fence a blockquote or list item holds is
        # still a fence; and a shorter run of the opener's character, or the other character, closes nothing.
        cases = {
            "```\n@inside.md\n``` text\n@still_inside.md\n```\n@outside.md\n": ["outside.md"],
            "> ```\n> @quoted.md\n> ```\n@after.md\n": ["after.md"],
            "- ```\n  @listed.md\n  ```\n@after.md\n": ["after.md"],
            "````\n```\n@inner.md\n````\n@after.md\n": ["after.md"],
            "~~~\n```\n@tilde.md\n~~~\n@after.md\n": ["after.md"],
            "> ```\n> @quoted.md\n@after.md\n": ["after.md"],
            "```\n@unclosed.md\n": [],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expected, audit.claude_imports(text))

    def test_an_import_resolves_from_the_file_that_holds_it(self) -> None:
        self.assertEqual(
            [
                "Imports @rules.md; imported files load together with this file, so they organize it without "
                "reducing its context cost"
            ],
            [row[3] for row in claude_code_rows({".claude/CLAUDE.md": "@rules.md\n", ".claude/rules.md": "x"})],
        )

    def test_each_rule_is_unconditional_or_scoped_by_its_paths(self) -> None:
        self.assertEqual(
            [
                ("INFO", "claude-memory", ".claude/rules/api.md", "Rule scoped to src/api/**/*.ts, lib/**"),
                (
                    "WARNING",
                    "claude-memory",
                    ".claude/rules/frontend/ui.md",
                    "paths should be a YAML list of glob patterns, as the memory page gives it",
                ),
                ("INFO", "claude-memory", ".claude/rules/testing.md", "Rule without paths: loads at launch"),
                ("INFO", "claude-memory", ".claude/rules/titled.md", "Rule without paths: loads at launch"),
                ("ERROR", "claude-memory", ".claude/rules/zz.md", "Could not read, so the rule was not checked"),
            ],
            claude_code_rows(
                {
                    ".claude/rules/api.md": '---\npaths:\n  - "src/api/**/*.ts"\n  - lib/**\n---\n\nUse REST.\n',
                    ".claude/rules/frontend/ui.md": "---\npaths: src/ui/**\n---\n\nUse tabs.\n",
                    ".claude/rules/testing.md": "# Testing\n\nRun pytest.\n",
                    ".claude/rules/titled.md": "---\ndescription: Titled.\n---\n",
                    ".claude/rules/zz.md": UNDECODABLE,
                }
            ),
        )

    def test_a_dot_claude_md_is_the_one_the_behavioral_checks_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_fixture(root, {".claude/CLAUDE.md": MAINTAINED_CLAUDE})
            result = audit.audit(root)
        self.assertEqual("conforming", result.authority)
        self.assertEqual({".claude/CLAUDE.md"}, {f.path for f in result.findings if f.check == "behavioral"})


def skill_md(name: str, description: str = "Does a thing. Use it when asked.", extra: str = "", body: str = "") -> str:
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n{body}\n"


def runtime_rows(files: dict[str, str | bytes], checks: tuple[str, ...]) -> list[tuple[str, str, str | None, str]]:
    """The findings of the named checks in the skill and agent check of a fixture."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write_fixture(root, files)
        return [row for row in _finding_rows(audit.check_copilot_configuration(root, {})) if row[1] in checks]


SKILL_CHECKS = ("agent-skill", "claude-skill", "codex-skill")
USER_ONLY = "disable-model-invocation: true\n"


class RuntimeSkillAndAgentTests(unittest.TestCase):
    """Skills under .claude/skills and .agents/skills, and agents under .claude/agents, are judged by the rules of the
    runtimes that read their folders."""

    def test_the_agent_skills_field_requirements_hold_in_both_folders_but_not_in_github_skills(self) -> None:
        long = "x" * 1025
        files: dict[str, str | bytes] = {}
        for folder in (".claude/skills", ".agents/skills", ".github/skills"):
            files[f"{folder}/claude-helper/SKILL.md"] = skill_md("claude-helper", "Formats <b>bold</b> text.")
            files[f"{folder}/long/SKILL.md"] = skill_md("long", long)
            files[f"{folder}/fine/SKILL.md"] = skill_md("fine", "Mentions claude and a < b comparison.")
        expected = []
        for folder in (".claude/skills", ".agents/skills"):
            expected += [
                (
                    "ERROR",
                    "agent-skill",
                    f"{folder}/claude-helper/SKILL.md",
                    "Skill name contains the reserved word 'claude'",
                ),
                ("ERROR", "agent-skill", f"{folder}/claude-helper/SKILL.md", "Skill description contains an XML tag"),
                ("ERROR", "agent-skill", f"{folder}/long/SKILL.md", "Skill description is 1025 characters, over 1024"),
            ]
        self.assertEqual(expected, runtime_rows(files, SKILL_CHECKS))

    def test_a_name_over_64_characters_is_an_error(self) -> None:
        name = "a" * 65
        self.assertEqual(
            [("ERROR", "agent-skill", f".claude/skills/{name}/SKILL.md", "Skill name is over 64 characters")],
            runtime_rows({f".claude/skills/{name}/SKILL.md": skill_md(name)}, SKILL_CHECKS),
        )

    def test_a_claude_skill_reading_arguments_needs_an_argument_hint(self) -> None:
        files: dict[str, str | bytes] = {
            ".claude/skills/bare/SKILL.md": skill_md("bare", body="Review $ARGUMENTS."),
            ".claude/skills/hinted/SKILL.md": skill_md("hinted", extra='argument-hint: "<pr>"\n', body="$ARGUMENTS"),
            ".claude/skills/none/SKILL.md": skill_md("none", body="No arguments."),
            ".agents/skills/codex/SKILL.md": skill_md("codex", body="Review $ARGUMENTS."),
        }
        self.assertEqual(
            [
                (
                    "WARNING",
                    "claude-skill",
                    ".claude/skills/bare/SKILL.md",
                    "Skill reads $ARGUMENTS but gives no argument-hint, so Claude Code's autocomplete shows no "
                    "expected arguments",
                )
            ],
            [row for row in runtime_rows(files, SKILL_CHECKS) if row[1] == "claude-skill"],
        )

    def test_a_user_only_codex_skill_needs_the_policy_file_that_stops_implicit_invocation(self) -> None:
        warning = (
            "Skill is user-only (disable-model-invocation: true), which Codex does not read; without "
            "agents/openai.yaml setting policy.allow_implicit_invocation: false, Codex may invoke it on its own"
        )
        cases: dict[str, tuple[dict[str, str | bytes], list[tuple[str, str, str]]]] = {
            "no policy file": ({}, [("WARNING", ".agents/skills/demo/SKILL.md", warning)]),
            "block policy": ({"agents/openai.yaml": "policy:\n  allow_implicit_invocation: false\n"}, []),
            "flow policy": ({"agents/openai.yaml": "interface: {}\npolicy: {allow_implicit_invocation: false}\n"}, []),
            "policy left on": (
                {"agents/openai.yaml": "policy:\n  allow_implicit_invocation: true\n"},
                [("WARNING", ".agents/skills/demo/SKILL.md", warning)],
            ),
            "unreadable policy": (
                {"agents/openai.yaml": UNDECODABLE},
                [
                    (
                        "ERROR",
                        ".agents/skills/demo/agents/openai.yaml",
                        "Could not read agents/openai.yaml, so the skill's invocation policy was not checked",
                    )
                ],
            ),
        }
        for name, (extra, expected) in cases.items():
            with self.subTest(name=name):
                files: dict[str, str | bytes] = {".agents/skills/demo/SKILL.md": skill_md("demo", extra=USER_ONLY)}
                files.update({f".agents/skills/demo/{rel}": content for rel, content in extra.items()})
                self.assertEqual(
                    [(severity, "codex-skill", path, message) for severity, path, message in expected],
                    runtime_rows(files, SKILL_CHECKS),
                )

    def test_a_shim_of_a_user_only_skill_is_user_only_too(self) -> None:
        rows = runtime_rows(
            {
                ".claude/skills/demo/SKILL.md": skill_md("demo", extra=USER_ONLY),
                ".agents/skills/demo/SKILL.md": skill_md("demo"),
                ".agents/skills/model/SKILL.md": skill_md("model"),
            },
            ("codex-skill",),
        )
        self.assertEqual([".agents/skills/demo/SKILL.md"], [row[2] for row in rows])

    def test_a_shim_whose_description_differs_from_its_skill_is_a_warning(self) -> None:
        self.assertEqual(
            [
                (
                    "WARNING",
                    "codex-skill",
                    ".agents/skills/demo/SKILL.md",
                    "Skill description differs from .claude/skills/demo/SKILL.md, the skill it stands for, so Codex "
                    "and Copilot choose it on other words than Claude Code",
                )
            ],
            runtime_rows(
                {
                    ".claude/skills/demo/SKILL.md": skill_md("demo", "Reviews a pull request."),
                    ".agents/skills/demo/SKILL.md": skill_md("demo", "Reviews a document."),
                    ".claude/skills/same/SKILL.md": skill_md("same", "Same words."),
                    ".agents/skills/same/SKILL.md": skill_md("same", '"Same words."'),
                },
                SKILL_CHECKS,
            ),
        )

    def test_a_claude_code_agent_is_read_with_its_own_fields_and_copilots(self) -> None:
        files: dict[str, str | bytes] = {
            ".claude/agents/reviewer.md": (
                "---\nname: code-review\ndescription: Reviews.\ntarget: invalid\ntools: Read, Grep\nmodel: inherit\n"
                "hooks:\n  PreToolUse:\n    - matcher: Bash\n---\n"
            ),
            ".claude/agents/open.md": "---\nname: open\ndescription: Does anything.\n---\n",
            ".claude/agents/listed.md": "---\nname: listed\ndescription: Lists.\ntools:\n  - Read\n  - Edit\n---\n",
            ".claude/agents/unnamed.md": "---\ndescription: No name.\n---\n",
            ".claude/agents/colon.md": "---\nname: a:b\n---\n",
            ".claude/agents/notes.txt": "not an agent",
        }
        rows = runtime_rows(files, ("claude-agent", "copilot-agent", "copilot-config", "collision"))
        self.assertEqual(
            [
                (
                    "ERROR",
                    "claude-agent",
                    ".claude/agents/colon.md",
                    "Agent name must be at most 256 characters, hold no ':', and not start with '-', or Claude Code "
                    "skips the file",
                ),
                ("ERROR", "claude-agent", ".claude/agents/colon.md", "Agent frontmatter requires description"),
                (
                    "INFO",
                    "claude-agent",
                    ".claude/agents/listed.md",
                    "Claude Code agent 'listed': tools Read, Edit; the default model; no hooks of its own",
                ),
                (
                    "INFO",
                    "claude-agent",
                    ".claude/agents/open.md",
                    "Claude Code agent 'open': tools every tool (inherited); the default model; no hooks of its own",
                ),
                (
                    "INFO",
                    "claude-agent",
                    ".claude/agents/reviewer.md",
                    "Claude Code agent 'code-review': tools Read, Grep; inherit; its own hooks",
                ),
                ("ERROR", "copilot-agent", ".claude/agents/reviewer.md", "target must be 'vscode' or 'github-copilot'"),
                # Copilot CLI names the agent by its name field, not its file.
                ("ERROR", "collision", ".claude/agents/reviewer.md", builtin_collision("code-review")),
                ("ERROR", "claude-agent", ".claude/agents/unnamed.md", "Agent frontmatter requires name"),
            ],
            rows,
        )

    def test_copilot_cli_judges_a_claude_code_agent_by_its_custom_agent_fields(self) -> None:
        # Copilot CLI's command reference reads custom agents from .claude/agents after .github/agents, each by the
        # ID its file name gives.
        self.assertEqual(
            [
                # .github/agents is read first, and its findings name its own file.
                (
                    "ERROR",
                    "copilot-agent",
                    ".github/agents/fields.agent.md",
                    "modelPolicy must be 'preferred' or 'required'",
                ),
                ("ERROR", "collision", ".claude/agents/code-review.md", builtin_collision("code-review")),
                ("ERROR", "copilot-agent", ".claude/agents/fields.md", "infer must be a boolean"),
                (
                    "ERROR",
                    "copilot-agent",
                    ".claude/agents/fields.md",
                    "tools must not be empty; write [] to disable every tool, or omit tools to enable every tool",
                ),
                ("ERROR", "copilot-agent", ".claude/agents/fields.md", "modelPolicy must be 'preferred' or 'required'"),
            ],
            runtime_rows(
                {
                    ".claude/agents/code-review.md": "---\nname: code-review\ndescription: Reviews.\n---\n",
                    ".claude/agents/fields.md": (
                        "---\nname: fields\ndescription: Fields.\ninfer: sometimes\ntools:\nmodelPolicy: strict\n---\n"
                    ),
                    ".github/agents/fields.agent.md": "---\ndescription: Fields.\nmodelPolicy: strict\n---\n",
                    ".claude/agents/clean.md": (
                        "---\nname: clean\ndescription: Clean.\ntools: Read, Grep\ninfer: false\n"
                        "modelPolicy: required\n---\n"
                    ),
                },
                ("copilot-agent", "collision"),
            ),
        )


def codex_rows(files: dict[str, str | bytes]) -> list[tuple[str, str, str | None, int | None, str]]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write_fixture(root, files)
        return [
            (
                f.severity,
                f.check,
                f.path,
                f.line,
                f.message.split(":")[0] if "Could not read or parse" in f.message else f.message,
            )
            for f in audit.check_codex(root)
        ]


CODEX_HOOK = (
    '{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x", "timeout": 5}]}]}}'
)
CODEX_AGENT = 'name = "explorer"\ndescription = "Explores."\ndeveloper_instructions = "Read only."\n'


class CodexConfigurationTests(unittest.TestCase):
    """Codex's hooks, custom agents, and rules under .codex, read as its hooks, subagents, and rules pages describe."""

    def test_hooks_are_read_from_hooks_json_and_config_toml(self) -> None:
        self.assertEqual([], codex_rows({".codex/hooks.json": CODEX_HOOK}))
        self.assertEqual(
            [("ERROR", "codex-hook", ".codex/hooks.json", None, "Could not read or parse")],
            codex_rows({".codex/hooks.json": "{bad"}),
        )
        self.assertEqual(
            [
                (
                    "ERROR",
                    "codex-hook",
                    ".codex/hooks.json",
                    None,
                    "Hook event 'PreToolCall' is not one Codex knows, so its hooks never run",
                ),
                (
                    "WARNING",
                    "codex-hook",
                    ".codex/hooks.json",
                    None,
                    "Hook on 'Stop' sets no timeout, so Codex waits up to 600 seconds for it",
                ),
            ],
            codex_rows(
                {
                    ".codex/hooks.json": json.dumps(
                        {
                            "hooks": {
                                "PreToolCall": [],
                                "Stop": [{"hooks": [{"type": "command", "command": "x"}]}],
                                "SessionEnd": [{"hooks": [{"type": "command", "command": "x"}]}],
                            }
                        }
                    )
                }
            ),
        )
        self.assertEqual(
            [
                ("ERROR", "codex-hook", ".codex/config.toml", None, "Hook on 'Stop': a command hook needs 'command'"),
                (
                    "WARNING",
                    "codex-hook",
                    ".codex/hooks.json",
                    None,
                    "Hooks are configured in both .codex/hooks.json and .codex/config.toml; Codex merges them and "
                    "warns at startup",
                ),
            ],
            codex_rows(
                {
                    ".codex/hooks.json": CODEX_HOOK,
                    ".codex/config.toml": '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ntimeout = 5\n',
                }
            ),
        )

    def test_the_hook_events_are_the_pages(self) -> None:
        # The hooks page's events, the model-request pair under "For each model request attempt" among them.
        self.assertEqual(
            {
                "PreModelRequest",
                "PostModelRequest",
                "PreToolUse",
                "PermissionRequest",
                "PostToolUse",
                "PreCompact",
                "PostCompact",
                "UserPromptSubmit",
                "SubagentStop",
                "Stop",
                "Interrupt",
                "SessionStart",
                "SubagentStart",
                "SessionEnd",
            },
            audit.CODEX_HOOKS.events,
        )
        handler = [{"hooks": [{"type": "command", "command": "x", "timeout": 5}]}]
        self.assertEqual(
            [],
            codex_rows(
                {".codex/hooks.json": json.dumps({"hooks": {"PreModelRequest": handler, "PostModelRequest": handler}})}
            ),
        )

    def test_a_hook_handler_is_held_to_the_types_the_hooks_page_lists(self) -> None:
        def hook(event: str = "PreToolUse", /, **fields: Any) -> str:
            return json.dumps({"hooks": {event: [{"matcher": "Bash", "hooks": [fields]}]}})

        cases: dict[str, tuple[str, list[tuple[str, str]]]] = {
            "command": (hook(type="command", command="x", timeout=5), []),
            "mcp_tool": (hook(type="mcp_tool", server="docs", tool="lookup", timeout=5), []),
            "mcp_tool without server or tool": (
                hook(type="mcp_tool", timeout=5),
                [
                    ("ERROR", "Hook on 'PreToolUse': a mcp_tool hook needs 'server'"),
                    ("ERROR", "Hook on 'PreToolUse': a mcp_tool hook needs 'tool'"),
                ],
            ),
            "mcp_tool without a timeout": (
                hook(type="mcp_tool", server="docs", tool="lookup"),
                [("WARNING", "Hook on 'PreToolUse' sets no timeout, so Codex waits up to 600 seconds for it")],
            ),
            "prompt": (
                hook(type="prompt", prompt="Check."),
                [("ERROR", "Hook on 'PreToolUse': Codex reads a prompt hook but skips it, so it never runs")],
            ),
            "agent": (
                hook(type="agent", prompt="Check."),
                [("ERROR", "Hook on 'PreToolUse': Codex reads a agent hook but skips it, so it never runs")],
            ),
            "http": (
                hook(type="http", url="https://example.test", timeout=5),
                [("ERROR", "Hook on 'PreToolUse': type 'http' is not one Codex runs")],
            ),
            "a type that is a list": (
                hook(type=["command"], command="x", timeout=5),
                [("ERROR", "Hook on 'PreToolUse': type ['command'] is not one Codex runs")],
            ),
            "an async guard": (
                hook(type="command", command="x", timeout=5, **{"async": True}),
                [
                    (
                        "WARNING",
                        "Hook on 'PreToolUse' runs in the background (async), so it cannot block or control the "
                        "operation; a guard must run synchronously",
                    )
                ],
            ),
            "an async observer": (hook("PostToolUse", type="command", command="x", timeout=5, **{"async": True}), []),
            "a synchronous guard": (hook(type="command", command="x", timeout=5, **{"async": False}), []),
        }
        for name, (hooks, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(
                    [(severity, "codex-hook", ".codex/hooks.json", None, message) for severity, message in expected],
                    codex_rows({".codex/hooks.json": hooks}),
                )
        for event in ("PermissionRequest", "UserPromptSubmit", "Stop"):
            with self.subTest(event=event):
                self.assertEqual(
                    ["WARNING"],
                    [
                        row[0]
                        for row in codex_rows(
                            {
                                ".codex/hooks.json": hook(
                                    event, type="command", command="x", timeout=5, **{"async": True}
                                )
                            }
                        )
                    ],
                )

    def test_a_config_toml_hook_timeout_is_a_positive_finite_number_of_seconds(self) -> None:
        # TOML writes an infinity and a not-a-number as bare words, which tomllib reads as floats, and an integer of any
        # length; a timeout is a positive finite number, as the JSON readers hold it.
        refused = [
            (
                "ERROR",
                "codex-hook",
                ".codex/config.toml",
                None,
                "Hook on 'Stop': timeout must be a positive number of seconds",
            )
        ]
        cases = {
            "5": [],
            "0.5": [],
            "inf": refused,
            "+inf": refused,
            "-inf": refused,
            "nan": refused,
            "-nan": refused,
            "0": refused,
            "-1": refused,
            "true": refused,
            "1" + "0" * 400: refused,
        }
        for timeout, expected in cases.items():
            with self.subTest(timeout=timeout[:8]):
                self.assertEqual(
                    expected,
                    codex_rows(
                        {
                            ".codex/config.toml": "[[hooks.Stop]]\n[[hooks.Stop.hooks]]\n"
                            f'type = "command"\ncommand = "x"\ntimeout = {timeout}\n'
                        }
                    ),
                )

    def test_a_config_toml_hook_whose_type_is_a_list_is_an_error_never_a_crash(self) -> None:
        self.assertEqual(
            [
                (
                    "ERROR",
                    "codex-hook",
                    ".codex/config.toml",
                    None,
                    "Hook on 'Stop': type ['command'] is not one Codex runs",
                )
            ],
            codex_rows(
                {".codex/config.toml": '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = ["command"]\ncommand = "x"\n'}
            ),
        )

    def test_a_session_end_or_interrupt_hook_timeout_over_three_seconds_is_a_warning(self) -> None:
        # The hooks page: "SessionEnd and Interrupt use 1 second by default and support up to 3 seconds."
        def hook(event: str, timeout: str) -> str:
            return f'[[hooks.{event}]]\n[[hooks.{event}.hooks]]\ntype = "command"\ncommand = "x"\ntimeout = {timeout}\n'

        def over(event: str, timeout: str) -> list[tuple[str, str, str | None, int | None, str]]:
            return [
                (
                    "WARNING",
                    "codex-hook",
                    ".codex/config.toml",
                    None,
                    f"Hook on '{event}' sets a timeout of {timeout} seconds, over the 3 seconds Codex supports for "
                    f"{event} hooks",
                )
            ]

        cases = {
            ("SessionEnd", "30"): over("SessionEnd", "30"),
            ("SessionEnd", "3"): [],
            ("SessionEnd", "1"): [],
            ("Interrupt", "3.5"): over("Interrupt", "3.5"),
            ("Interrupt", "3.0"): [],
            # Other events run up to the long default, so a long timeout there is theirs to set.
            ("Stop", "30"): [],
        }
        for (event, timeout), expected in cases.items():
            with self.subTest(event=event, timeout=timeout):
                self.assertEqual(expected, codex_rows({".codex/config.toml": hook(event, timeout)}))
        self.assertEqual(
            [
                (
                    "WARNING",
                    "codex-hook",
                    ".codex/hooks.json",
                    None,
                    "Hook on 'SessionEnd' sets a timeout of 30 seconds, over the 3 seconds Codex supports for "
                    "SessionEnd hooks",
                )
            ],
            codex_rows(
                {
                    ".codex/hooks.json": json.dumps(
                        {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "x", "timeout": 30}]}]}}
                    )
                }
            ),
        )

    def test_claude_code_sets_no_such_limit_on_its_session_end_hooks(self) -> None:
        hooks = {"SessionEnd": [{"hooks": [{"type": "command", "command": "x", "timeout": 30}]}]}
        self.assertEqual([], audit.hook_problems(audit.CLAUDE_HOOKS, hooks))

    def test_a_retired_approval_policy_is_reported_by_how_far_codex_has_retired_it(self) -> None:
        # The configuration reference: "untrusted is unsupported, and on-failure is deprecated; use on-request for
        # interactive runs or never for non-interactive runs." The approvals page: the retired untrusted "can prevent
        # either client from starting".
        cases: dict[str, list[tuple[str, str, str | None, int | None, str]]] = {
            '"untrusted"': [
                (
                    "ERROR",
                    "codex-config",
                    ".codex/config.toml",
                    None,
                    "approval_policy = 'untrusted': Codex no longer supports it, and the retired setting can prevent "
                    "Codex from starting; use 'on-request' for interactive runs or 'never' for non-interactive runs",
                )
            ],
            '"on-failure"': [
                (
                    "WARNING",
                    "codex-config",
                    ".codex/config.toml",
                    None,
                    "approval_policy = 'on-failure': Codex has deprecated it; use 'on-request' for interactive runs "
                    "or 'never' for non-interactive runs",
                )
            ],
            '"on-request"': [],
            '"never"': [],
            "{ granular = { sandbox_approval = true } }": [],
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(expected, codex_rows({".codex/config.toml": f"approval_policy = {value}\n"}))

    def test_a_profile_in_a_projects_config_is_not_read_since_codex_ignores_it(self) -> None:
        # The configuration reference: Codex ignores profile and profiles in a project's config.
        self.assertEqual(
            [],
            codex_rows({".codex/config.toml": '[profiles.strict]\napproval_policy = "untrusted"\n'}),
        )

    def test_default_permissions_with_the_legacy_sandbox_settings_is_a_warning(self) -> None:
        # The configuration reference, at default_permissions: "Don't combine with sandbox_mode or
        # [sandbox_workspace_write]."
        def combined(label: str) -> tuple[str, str, str | None, int | None, str]:
            return (
                "WARNING",
                "codex-config",
                ".codex/config.toml",
                None,
                f"default_permissions is set together with {label}, which Codex's configuration reference says not to "
                "combine; set either default_permissions and its [permissions] tables, or sandbox_mode and "
                "[sandbox_workspace_write]",
            )

        permissions = 'default_permissions = ":workspace"\n'
        mode = 'sandbox_mode = "workspace-write"\n'
        table = "[sandbox_workspace_write]\nnetwork_access = true\n"
        cases = {
            "with sandbox_mode": (permissions + mode, [combined("sandbox_mode")]),
            "with the table": (permissions + table, [combined("a [sandbox_workspace_write] table")]),
            "with both": (
                permissions + mode + table,
                [combined("sandbox_mode"), combined("a [sandbox_workspace_write] table")],
            ),
            "default_permissions alone": (permissions, []),
            "sandbox_mode and its table alone": (mode + table, []),
        }
        for name, (config, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(expected, codex_rows({".codex/config.toml": config}))

    def test_a_custom_agent_needs_its_fields_and_sets_no_session_setting(self) -> None:
        self.assertEqual([], codex_rows({".codex/agents/explorer.toml": CODEX_AGENT}))
        self.assertEqual(
            [
                ("ERROR", "codex-agent", ".codex/agents/broken.toml", None, "Could not read or parse"),
                ("ERROR", "codex-agent", ".codex/agents/thin.toml", None, "Custom agent requires description"),
                (
                    "ERROR",
                    "codex-agent",
                    ".codex/agents/thin.toml",
                    None,
                    "Custom agent requires developer_instructions",
                ),
                (
                    "WARNING",
                    "codex-agent",
                    ".codex/agents/thin.toml",
                    None,
                    "sandbox_mode has no effect in a custom agent file; the parent session's setting applies",
                ),
            ],
            codex_rows(
                {
                    ".codex/agents/broken.toml": "name = ",
                    ".codex/agents/thin.toml": 'name = "thin"\nsandbox_mode = "danger-full-access"\n',
                    ".codex/agents/notes.md": "not an agent",
                }
            ),
        )

    def test_each_allowing_rule_is_a_warning_at_its_line(self) -> None:
        rules = (
            "# Project rules\n"
            'prefix_rule(pattern = ["git", "push"], decision = "prompt")\n'
            'prefix_rule(pattern = ["npm", ["test", "run"]])\n'
            'prefix_rule(pattern = ["rm"], decision = "forbidden")\n'
            'prefix_rule(pattern = ["make"], decision = "allow")\n'
            'prefix_rule(pattern = [], decision = "allow")\n'
            'prefix_rule(pattern = ["curl"], decision = "maybe")\n'
        )
        self.assertEqual(
            [
                (
                    "WARNING",
                    "codex-rule",
                    ".codex/rules/default.rules",
                    3,
                    "prefix_rule allows 'npm ['test', 'run']' to run outside the sandbox without prompting, for "
                    "everyone who trusts this project",
                ),
                (
                    "WARNING",
                    "codex-rule",
                    ".codex/rules/default.rules",
                    5,
                    "prefix_rule allows 'make' to run outside the sandbox without prompting, for everyone who trusts "
                    "this project",
                ),
                (
                    "ERROR",
                    "codex-rule",
                    ".codex/rules/default.rules",
                    6,
                    "prefix_rule needs a non-empty literal pattern list",
                ),
                (
                    "ERROR",
                    "codex-rule",
                    ".codex/rules/default.rules",
                    7,
                    "prefix_rule decision must be 'allow', 'prompt', or 'forbidden'",
                ),
            ],
            sorted(codex_rows({".codex/rules/default.rules": rules}), key=lambda row: row[3] or 0),
        )

    def test_a_rules_file_that_does_not_parse_is_a_read_failure(self) -> None:
        self.assertEqual(
            [
                ("ERROR", "codex-rule", ".codex/rules/broken.rules", None, "Could not read or parse"),
                ("ERROR", "codex-rule", ".codex/rules/bytes.rules", None, "Could not read or parse"),
            ],
            codex_rows({".codex/rules/broken.rules": "prefix_rule(\n", ".codex/rules/bytes.rules": UNDECODABLE}),
        )

    def test_a_parse_failure_names_the_rules_file_and_line_python_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_fixture(root, {".codex/rules/broken.rules": "prefix_rule(\n"})
            (finding,) = audit.check_codex(root)
        # The finding names the file; python_reader's refusal names the parser's reason and its line.
        self.assertEqual(".codex/rules/broken.rules", finding.path)
        self.assertRegex(finding.message, r"^Could not read or parse: .+ at line 1$")


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


def _denied_error(target: Path) -> PermissionError:
    return PermissionError(errno.EACCES, "Access is denied", str(target))


def _names(target: Path, argument: object) -> bool:
    """Whether a path argument a stat or a listing is given names `target`; a descriptor never does."""
    return isinstance(argument, (str, os.PathLike)) and Path(argument) == target


@contextlib.contextmanager
def listing_denied(target: Path) -> Iterator[None]:
    """`target`, a folder, cannot be listed: os.scandir and os.listdir, and so Path.iterdir and every walk, raise the
    error a file system gives, and Path.glob and Path.rglob match nothing in it, as Python 3.15's do where iterdir
    raises."""
    listers = {name: getattr(os, name) for name in ("scandir", "listdir")}
    globs = {name: getattr(Path, name) for name in ("glob", "rglob")}

    def lister(name: str) -> Callable[..., Any]:
        def listed(*args: Any, **kwargs: Any) -> Any:
            if args and _names(target, args[0]):
                raise _denied_error(target)
            return listers[name](*args, **kwargs)

        return listed

    def glob(name: str) -> Callable[..., Any]:
        def matched(path: Path, *args: Any, **kwargs: Any) -> Any:
            return iter(()) if path == target else globs[name](path, *args, **kwargs)

        return matched

    with contextlib.ExitStack() as stack:
        for name in listers:
            stack.enter_context(mock.patch.object(os, name, lister(name)))
        for name in globs:
            stack.enter_context(mock.patch.object(Path, name, glob(name)))
        yield


@contextlib.contextmanager
def os_stat_denied(target: Path) -> Iterator[None]:
    """os.stat and os.lstat refuse `target` alone, and the os.path predicates answer False for it, as they always
    have for a stat that fails."""
    stats = {"stat": os.stat, "lstat": os.lstat}

    def os_stat(name: str) -> Callable[..., os.stat_result]:
        def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            if _names(target, path):
                raise _denied_error(target)
            return stats[name](path, *args, **kwargs)

        return stat

    os_predicates = {name: getattr(os.path, name) for name in ("exists", "lexists", "isfile", "isdir")}

    def os_predicate(name: str) -> Callable[..., bool]:
        def answer(path: Any) -> bool:
            return False if _names(target, path) else bool(os_predicates[name](path))

        return answer

    with contextlib.ExitStack() as stack:
        for name in stats:
            stack.enter_context(mock.patch.object(os, name, os_stat(name)))
        for name in os_predicates:
            stack.enter_context(mock.patch.object(os.path, name, os_predicate(name)))
        yield


@contextlib.contextmanager
def stat_denied(target: Path, *, older: bool, walked: bool = True) -> Iterator[None]:
    """Path.stat, os.stat, and os.lstat refuse `target` alone, as a file system does a path the audit may not examine,
    and it cannot be listed either (listing_denied). Path.exists, is_file, and is_dir raise that error for it, as
    before Python 3.14, when `older`, and answer False, as from 3.14 on, when not, and the os.path predicates answer
    False, as they always have, so every Python the suite runs on meets a predicate left on the path as either does.
    The folder walk's own examination of the entry is refused too, unless not `walked`, as when a listing found the
    entry by its name alone."""
    real = Path.stat

    def refuse(path: object) -> None:
        if _names(target, path):
            raise _denied_error(target)

    def stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        refuse(path)
        return real(path, *args, **kwargs)

    predicates = {name: getattr(Path, name) for name in ("exists", "is_file", "is_dir")}

    def predicate(name: str) -> Callable[..., bool]:
        def answer(path: Path, *args: Any, **kwargs: Any) -> bool:
            if path == target and not older:
                return False
            refuse(path)
            return bool(predicates[name](path, *args, **kwargs))

        return answer

    probe = link_probe.is_link_or_junction

    def entry_probe(path: Any, *args: Any) -> bool:
        # The folder walk examines a listed entry by its DirEntry's own stat, which no patch reaches.
        refuse(path)
        return probe(path, *args)

    with contextlib.ExitStack() as stack:
        if walked:
            stack.enter_context(mock.patch.object(folder_walk, "is_link_or_junction", entry_probe))
        stack.enter_context(mock.patch.object(Path, "stat", stat))
        for name in predicates:
            stack.enter_context(mock.patch.object(Path, name, predicate(name)))
        stack.enter_context(os_stat_denied(target))
        stack.enter_context(listing_denied(target))
        yield


class UnexaminedPathTests(unittest.TestCase):
    """A configuration file or folder whose stat fails, or with something else in its place, is a finding at its
    path, never read as no such file or folder."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        # The audit resolves its root, so a path the stat refuses is spelled as the resolved root spells it.
        self.root = Path(self.temp.name).resolve()
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    # Whether a predicate left on a refused path raises, as before Python 3.14, or answers False, as from 3.14 on.
    OLDER = True

    def denied(self, target: Path) -> contextlib.AbstractContextManager[None]:
        return stat_denied(target, older=self.OLDER)

    # Each path the inventory reports when its stat fails, with what makes it there, which the stat then hides.
    INVENTORY_DENIED: ClassVar[dict[str, dict[str, str | bytes]]] = {
        "CLAUDE.md": {},
        ".github/ai-config-manifest.json": {},
        "scripts/ai_config.py": {"scripts/ai_config.py": "x"},
        ".claude/rules": {".claude/rules/testing.md": "x"},
        ".github/instructions": {".github/instructions/a.instructions.md": "x"},
        ".github/workflows": {".github/workflows/parity.yml": "run: ai_config --check"},
        ".codex/rules": {".codex/rules/default.rules": "x"},
        # A fixed file no check reads in the inventory, so only the fixed files' own examination reports it.
        ".vscode/mcp.json": {".vscode/mcp.json": "{}"},
    }

    def test_a_path_the_inventory_reads_that_cannot_be_examined_is_an_inventory_error(self) -> None:
        for rel, files in self.INVENTORY_DENIED.items():
            with self.subTest(rel=rel):
                _write_fixture(self.root, files)
                with self.denied(self.root / rel):
                    errors = [row for row in _finding_rows(audit.check_inventory(self.root)) if row[0] == "ERROR"]
                self.assertEqual(
                    [("ERROR", "inventory", rel, f"{UNREAD} [Errno {errno.EACCES}] Access is denied: ")],
                    [(*row[:3], row[3].replace(repr(str(self.root / rel)), "")) for row in errors],
                )

    def test_each_folder_a_check_reads_that_cannot_be_examined_is_one_error_and_the_audit_ends(self) -> None:
        rows: list[tuple[str, dict[str, str | bytes]]] = [
            (".claude/rules", {".claude/rules/testing.md": "x"}),
            (".github/workflows", {".github/workflows/parity.yml": "run: ai_config --check"}),
            (".github/skills", {".github/skills/demo/SKILL.md": "x"}),
            (".github/agents", {".github/agents/reviewer.md": "x"}),
            (".claude/skills", {}),
            (".codex/agents", {".codex/agents/reviewer.toml": "x"}),
            (".codex/rules", {".codex/rules/default.rules": "x"}),
        ]
        for rel, files in rows:
            with self.subTest(rel=rel):
                _write_fixture(self.root, files)
                with self.denied(self.root / rel):
                    findings = audit.audit(self.root).findings
                self.assertEqual(
                    ["inventory"], [f.check for f in findings if f.path == rel and f.severity == "ERROR"], findings
                )

    # Each file or folder a check reads, whether a listing or walk finds it or a check names it, with what makes it
    # there: its stat refused, it is the one inventory ERROR at its path, never dropped as no such file or folder.
    EXAMINED: ClassVar[dict[str, dict[str, str | bytes]]] = {
        ".claude/agents/reviewer.md": {".claude/agents/reviewer.md": "x"},
        ".github/agents/reviewer.agent.md": {".github/agents/reviewer.agent.md": "x"},
        ".claude/skills/demo/SKILL.md": {".claude/skills/demo/SKILL.md": "x"},
        ".agents/skills/demo/SKILL.md": {".agents/skills/demo/SKILL.md": "x"},
        ".github/skills/demo/SKILL.md": {".github/skills/demo/SKILL.md": "x"},
        ".codex/agents/reviewer.toml": {".codex/agents/reviewer.toml": "x"},
        ".codex/rules/default.rules": {".codex/rules/default.rules": "x"},
        ".github/instructions/a.instructions.md": {".github/instructions/a.instructions.md": "x"},
        ".claude/rules/testing.md": {".claude/rules/testing.md": "x"},
        ".claude/rules/frontend": {".claude/rules/frontend/ui.md": "x"},
        ".github/workflows/parity.yml": {".github/workflows/parity.yml": "run: ai_config --check"},
        "docs/AGENTS.override.md": {"docs/AGENTS.override.md": "x", "docs/AGENTS.md": "x"},
        "docs": {"docs/AGENTS.md": "x"},
        "CLAUDE.md": {},
        ".github/copilot-instructions.md": {},
        "REVIEW.md": {"REVIEW.md": "x"},
        ".github/instructions": {".github/instructions/a.instructions.md": "x"},
        ".claude/settings.json": {".claude/settings.json": "{}"},
        ".codex/config.toml": {".codex/config.toml": ""},
        ".mcp.json": {".mcp.json": "{}"},
        ".codex": {".codex/config.toml": ""},
        ".github": {},
        ".agents/skills": {},
        ".claude/agents": {".claude/agents/reviewer.md": "x"},
    }

    # Files only a walk finds, which the checks then read with no stat of their own, so only the walk's examination
    # of the entry can fail; a read that fails reports itself.
    WALKED_ONLY: ClassVar[frozenset[str]] = frozenset(
        {".github/instructions/a.instructions.md", ".claude/rules/testing.md"}
    )

    # The files among EXAMINED whose own check reports a failed examination as its read failure, with that check: the
    # audit reports each once, by that check, though the inventory alone reports it as its own.
    READ_BY_OWN_CHECK: ClassVar[dict[str, str]] = {
        ".claude/settings.json": "claude-settings",
        ".codex/config.toml": "mcp",
        ".mcp.json": "mcp",
    }

    def test_each_path_a_check_reads_that_cannot_be_examined_is_one_inventory_error(self) -> None:
        # Once as the repository walk meets the path, which it reports as an entry it cannot examine, and once as a
        # listing that found it by its name alone, which leaves it to the reporter of the files or folders it is one of.
        for (rel, files), walked in itertools.product(self.EXAMINED.items(), (True, False)):
            if not walked and rel in self.WALKED_ONLY:
                continue
            with self.subTest(rel=rel, walked=walked):
                _write_fixture(self.root, files)
                with stat_denied(self.root / rel, older=self.OLDER, walked=walked):
                    rows = _finding_rows(audit.check_inventory(self.root))
                    # The positive checks, such as those of the scope a repository's files show, take such a path for
                    # no find, leaving it to the ERROR, never raising its stat's error and ending the audit.
                    audit.derive_file_scope(self.root)
                    findings = audit.audit(self.root).findings
                self.assertEqual([("ERROR", "inventory", rel)], [row[:3] for row in rows if row[0] == "ERROR"])
                self.assertTrue(rows[[row[2] for row in rows].index(rel)][3].startswith(UNREAD), rows)
                if rel in self.READ_BY_OWN_CHECK:
                    # The check that reads the file reports its failure itself, so the audit reports it once, there.
                    self.assertEqual(
                        [self.READ_BY_OWN_CHECK[rel]],
                        [f.check for f in findings if f.path == rel and f.severity == "ERROR"],
                        findings,
                    )
                else:
                    self.assertIn(("ERROR", "inventory", rel), [(f.severity, f.check, f.path) for f in findings])

    # Each folder there whose listing is refused, with a file in it: the inventory's one ERROR at the folder, though
    # a glob matches nothing in it.
    UNLISTED: ClassVar[dict[str, dict[str, str | bytes]]] = {
        ".claude/rules": {".claude/rules/testing.md": "x"},
        ".claude/rules/frontend": {".claude/rules/frontend/ui.md": "x"},
        ".github/instructions": {".github/instructions/a.instructions.md": "x"},
        ".github/instructions/deep": {".github/instructions/deep/b.instructions.md": "x"},
        # The repository walk skips a folder named bin, which these walks enter.
        ".claude/rules/bin": {".claude/rules/bin/ui.md": "x"},
        ".github/instructions/bin": {".github/instructions/bin/b.instructions.md": "x"},
        ".github/workflows": {".github/workflows/parity.yml": "run: ai_config --check"},
        ".codex/agents": {".codex/agents/reviewer.toml": "x"},
        ".codex/rules": {".codex/rules/default.rules": "x"},
        ".claude/agents": {".claude/agents/reviewer.md": "x"},
        ".github/agents": {".github/agents/reviewer.md": "x"},
        ".claude/skills": {".claude/skills/demo/SKILL.md": "x"},
        ".github/skills": {".github/skills/demo/SKILL.md": "x"},
        ".github/skills/demo": {".github/skills/demo/SKILL.md": "x"},
        "docs": {"docs/AGENTS.md": "x"},
    }

    def test_each_folder_a_check_lists_that_cannot_be_listed_is_one_error_never_an_empty_folder(self) -> None:
        for rel, files in self.UNLISTED.items():
            with self.subTest(rel=rel):
                _write_fixture(self.root, files)
                with listing_denied(self.root / rel):
                    inventory = [
                        row[:3] for row in _finding_rows(audit.check_inventory(self.root)) if row[0] == "ERROR"
                    ]
                    findings = audit.audit(self.root).findings
                self.assertEqual([("ERROR", "inventory", rel)], inventory)
                self.assertEqual(
                    [("inventory", rel)],
                    [(f.check, f.path) for f in findings if f.severity == "ERROR" and f.path == rel],
                )

    def test_an_agent_whose_stat_fails_is_still_checked_so_its_own_errors_are_reported(self) -> None:
        rows: list[tuple[str, str, Callable[[Path], list[audit.Finding]]]] = [
            (".codex/agents/reviewer.toml", "codex-agent", audit._codex_agents),
            (".github/agents/reviewer.md", "copilot-agent", lambda root: audit.check_copilot_configuration(root, {})),
            (".claude/agents/reviewer.md", "claude-agent", lambda root: audit.check_copilot_configuration(root, {})),
        ]
        for rel, check, run in rows:
            with self.subTest(rel=rel):
                # No name and no description: each check's own ERROR at the file.
                _write_fixture(self.root, {rel: 'model = "x"\n' if rel.endswith(".toml") else "---\nx: y\n---\n"})
                with self.denied(self.root / rel):
                    errors = {row[1] for row in _finding_rows(run(self.root)) if row[0] == "ERROR" and row[2] == rel}
                self.assertIn(check, errors)

    def test_a_skill_listing_keeps_what_it_cannot_list_or_examine_for_the_inventory(self) -> None:
        skills = self.root / ".claude/skills"
        skill = skills / "demo/SKILL.md"
        _write_fixture(self.root, {".claude/skills/demo/SKILL.md": "x"})
        with listing_denied(skills):
            self.assertEqual(([], [skills]), self._paths_and_unread(audit.skill_files(self.root, skills)))
        with stat_denied(skill, older=self.OLDER, walked=False):
            self.assertEqual(([skill], [skill]), self._paths_and_unread(audit.skill_files(self.root, skills)))

    @staticmethod
    def _paths_and_unread(listing: audit.Listing) -> tuple[list[Path], list[Path]]:
        return listing.paths, [path for path, _ in listing.unread]

    def test_a_skill_listing_keeps_the_reason_it_could_not_list_or_examine(self) -> None:
        skills = self.root / ".claude/skills"
        skill = skills / "demo/SKILL.md"
        _write_fixture(self.root, {".claude/skills/demo/SKILL.md": "x"})
        with listing_denied(skills):
            self.assertEqual([(skills, str(_denied_error(skills)))], audit.skill_files(self.root, skills).unread)
        with stat_denied(skill, older=self.OLDER, walked=False):
            self.assertEqual([(skill, str(_denied_error(skill)))], audit.skill_files(self.root, skills).unread)

    def test_the_files_of_a_listing_keep_its_links_out_and_what_it_could_not_list_or_examine(self) -> None:
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        agents = self.root / ".claude/agents"
        reviewer, nested = agents / "reviewer.md", agents / "nested"
        _write_fixture(self.root, {".claude/agents/reviewer.md": "x", ".claude/agents/nested/helper.md": "x"})
        _link_directory(Path(elsewhere.name).resolve(), agents / "linked")
        # A folder is no file, a link out stays one, and an entry whose stat fails is kept with its reason.
        with stat_denied(reviewer, older=self.OLDER, walked=False):
            files = audit.regular_files(self.root, audit.list_directory(self.root, agents))
        self.assertEqual(
            ([reviewer], [agents / "linked"], [(reviewer, str(_denied_error(reviewer)))]),
            (files.paths, files.outside, files.unread),
        )
        # A folder the walk it is given could not list stays one the inventory reports.
        with listing_denied(nested):
            walk = audit.walk_files(self.root, agents, lambda name: True)
        self.assertIn(nested, [path for path, _ in walk.unread])
        self.assertEqual(walk.unread, audit.regular_files(self.root, walk).unread)

    def test_the_inventory_reads_a_nested_agents_md_and_only_examines_a_nested_override(self) -> None:
        # No check reads a nested AGENTS.override.md, so one that cannot be decoded is no inventory ERROR.
        _write_fixture(self.root, {"docs/AGENTS.md": b"\xff\xfe\x00bad", "docs/AGENTS.override.md": b"\xff\xfe\x00bad"})
        rows = _finding_rows(audit.check_inventory(self.root))
        self.assertEqual(["docs/AGENTS.md"], [row[2] for row in rows if row[0] == "ERROR"])
        self.assertIn(("INFO", "inventory", "docs/AGENTS.override.md", "Found docs/AGENTS.override.md"), rows)

    def test_the_scope_of_a_repositorys_files_takes_a_path_it_cannot_examine_for_no_find(self) -> None:
        # Each path alone shows its runtime or surface, so an earlier one never answers for it.
        rows: list[tuple[str, dict[str, str | bytes], dict[str, list[str]]]] = [
            ("AGENTS.override.md", {"AGENTS.override.md": "x"}, {}),
            (".codex", {".codex/config.toml": ""}, {}),
            (".agents/skills", {".agents/skills/demo/SKILL.md": "x"}, {}),
            (
                ".github/workflows/copilot-setup-steps.yml",
                {".github/workflows/copilot-setup-steps.yml": "x"},
                {"runtimes": [], "surfaces": ["copilot_cli", "code_review"]},
            ),
        ]
        for rel, files, scope in rows:
            with self.subTest(rel=rel), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                _write_fixture(root, files)
                with self.denied(root / rel):
                    self.assertEqual(scope, audit.derive_file_scope(root))

    def test_code_review_takes_a_review_file_it_cannot_examine_for_no_source(self) -> None:
        _write_fixture(self.root, {"REVIEW.md": "x"})
        sources = audit._instruction_sources(self.root)
        with self.denied(self.root / "REVIEW.md"):
            first = audit._layering_code_review(self.root, sources)[0]
        self.assertNotIn("REVIEW.md", first.message)

    def test_a_skill_folder_the_skill_check_cannot_list_is_its_error_where_the_repository_walk_skips_it(self) -> None:
        # The repository walk skips a folder named bin, which the skill check's own walk enters.
        _write_fixture(self.root, {".github/skills/bin/SKILL.md": "x"})
        with listing_denied(self.root / ".github/skills/bin"):
            rows = _finding_rows(audit.check_copilot_configuration(self.root, {}))
        self.assertEqual(
            [("ERROR", "copilot-config")], [row[:2] for row in rows if row[2] == ".github/skills/bin"], rows
        )

    def test_a_projection_it_cannot_examine_keeps_its_limitation_note(self) -> None:
        _write_fixture(self.root, {".github/agents/reviewer.md": f"<!-- {OWNERSHIP} -->\n"})
        with stat_denied(self.root / ".github/agents/reviewer.md", older=self.OLDER, walked=False):
            rows = _finding_rows(audit.check_limitations(self.root, {}))
        self.assertIn(("INFO", "limitation", ".github/agents/reviewer.md"), [row[:3] for row in rows])

    def test_a_listing_raises_for_a_folder_it_cannot_list_where_a_glob_matches_nothing(self) -> None:
        folder = self.root / ".github/workflows"
        _write_fixture(self.root, {".github/workflows/parity.yml": "x", ".github/workflows/notes.txt": "x"})
        self.assertEqual([folder / "parity.yml"], audit.list_directory(self.root, folder, "*.yml").paths)
        with listing_denied(folder), self.assertRaises(PermissionError):
            audit.list_directory(self.root, folder, "*.yml")

    def test_an_import_it_cannot_examine_is_a_warning_saying_so_never_a_missing_file(self) -> None:
        _write_fixture(self.root, {"docs/guide.md": "x"})
        self.assertEqual([], audit._missing_imports(self.root, "CLAUDE.md", ["docs/guide.md"]))
        with self.denied(self.root / "docs/guide.md"):
            rows = _finding_rows(audit._missing_imports(self.root, "CLAUDE.md", ["docs/guide.md"]))
        self.assertEqual([("WARNING", "claude-memory", "CLAUDE.md")], [row[:3] for row in rows])
        self.assertTrue(rows[0][3].startswith("Import @docs/guide.md names a path that could not be examined"), rows)

    def test_a_canonical_source_it_cannot_examine_is_there_to_the_manifest_and_a_folder_is_not(self) -> None:
        manifest = json.loads((self.root / MANIFEST_PATH).read_text(encoding="utf-8"))
        with self.denied(self.root / "CLAUDE.md"):
            self.assertEqual("manifest", audit._validate_claude_manifest(self.root, manifest)[0])
        (self.root / "CLAUDE.md").unlink()
        (self.root / "CLAUDE.md").mkdir()
        self.assertIsNone(audit._validate_claude_manifest(self.root, manifest)[0])

    def test_a_git_marker_it_cannot_examine_fails_saying_so_never_no_repository(self) -> None:
        (self.root / ".git").mkdir()
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            mock.patch.object(sys, "argv", ["audit_ai_config.py", "--root", str(self.root)]),
            self.denied(self.root / ".git"),
        ):
            code = audit.main()
        self.assertEqual(1, code)
        self.assertTrue(
            output.getvalue().startswith(f"FAILED {self.root} could not be examined for a .git: [Errno 13]"),
            output.getvalue(),
        )

    def test_a_file_in_a_skill_or_agent_folders_place_is_an_error_never_no_folder(self) -> None:
        for rel in (".github/skills", ".github/agents", ".claude/agents"):
            with self.subTest(rel=rel):
                _write_fixture(self.root, {rel: "../.claude/skills\n"})
                self.assertEqual(
                    [("ERROR", "copilot-config", rel, "Could not list the directory: not a directory")],
                    [row for row in _finding_rows(audit.check_copilot_configuration(self.root, {})) if row[2] == rel],
                )
                (self.root / rel).unlink()

    def test_a_skill_folder_that_cannot_be_examined_is_an_error_never_no_folder(self) -> None:
        with self.denied(self.root / ".agents/skills"):
            rows = _finding_rows(audit.check_copilot_configuration(self.root, {}))
        self.assertIn(
            ("ERROR", "copilot-config", ".agents/skills", "Could not list the directory: Access is denied"), rows
        )

    def test_a_generator_that_is_a_folder_or_cannot_be_examined_is_unreadable_scope(self) -> None:
        generator = self.root / ".github/scripts/ai_config.py"
        generator.mkdir(parents=True)
        self.assertEqual((None, "generator-scope-unreadable"), audit.derive_generator_scope(self.root))
        generator.rmdir()
        _write_fixture(self.root, {".github/scripts/ai_config.py": "TARGET_RUNTIMES = []\n"})
        with self.denied(generator):
            self.assertEqual((None, "generator-scope-unreadable"), audit.derive_generator_scope(self.root))

    def test_an_invocation_policy_that_is_a_folder_or_cannot_be_examined_is_a_problem_never_no_policy(self) -> None:
        policy = self.root / ".agents/skills/demo/agents/openai.yaml"
        problem = "Could not read agents/openai.yaml, so the skill's invocation policy was not checked"
        policy.mkdir(parents=True)
        self.assertEqual((True, problem), audit._codex_implicit_invocation(self.root, policy))
        policy.rmdir()
        _write_fixture(
            self.root, {".agents/skills/demo/agents/openai.yaml": "policy:\n  allow_implicit_invocation: false\n"}
        )
        with self.denied(policy):
            self.assertEqual((True, problem), audit._codex_implicit_invocation(self.root, policy))

    def test_a_manifest_that_is_a_folder_or_cannot_be_examined_is_there_to_the_scope_check(self) -> None:
        manifest = self.root / MANIFEST_PATH
        with self.denied(manifest):
            self.assertEqual("manifest-declared-only", audit.check_scope_and_roles(self.root, {})[0])
        manifest.unlink()
        manifest.mkdir()
        self.assertEqual("manifest-declared-only", audit.check_scope_and_roles(self.root, {})[0])

    def test_an_artifact_that_is_a_folder_or_cannot_be_examined_could_not_be_read_never_is_missing(self) -> None:
        unread = (None, "Generated artifact exists but could not be read")
        with self.denied(self.root / "AGENTS.md"):
            self.assertEqual(unread, audit._read_artifact(self.root, "AGENTS.md"))
        (self.root / "AGENTS.md").unlink()
        (self.root / "AGENTS.md").mkdir()
        self.assertEqual(unread, audit._read_artifact(self.root, "AGENTS.md"))

    def test_codex_chooses_an_instruction_file_it_cannot_examine_as_holding_text_and_passes_a_folder_by(self) -> None:
        (self.root / "AGENTS.md").write_text("  \n", encoding="utf-8")
        self.assertEqual(("AGENTS.md", False), audit._codex_choice(self.root, self.root, []))
        with self.denied(self.root / "AGENTS.md"):
            self.assertEqual(("AGENTS.md", True), audit._codex_choice(self.root, self.root, []))
        (self.root / "AGENTS.md").unlink()
        (self.root / "AGENTS.md").mkdir()
        self.assertEqual(("CLAUDE.md", True), audit._codex_choice(self.root, self.root, ["CLAUDE.md"]))


class PresenceAnswerTests(unittest.TestCase):
    """What each check that asks whether a configuration file or folder is there answers, found or not, and that one
    path it cannot examine leaves the rest of its loop to run."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_found_file_and_found_folder_answer_false_for_a_path_they_cannot_examine(self) -> None:
        _write_fixture(self.root, {"CLAUDE.md": "x", ".claude/rules/a.md": "x"})
        file, folder = self.root / "CLAUDE.md", self.root / ".claude/rules"
        self.assertEqual(
            (True, True, False, False),
            (audit.found_file(file), audit.found_folder(folder), audit.found_file(folder), audit.found_folder(file)),
        )
        for older in (True, False):
            with self.subTest(older=older):
                with stat_denied(file, older=older):
                    self.assertFalse(audit.found_file(file))
                with stat_denied(folder, older=older):
                    self.assertFalse(audit.found_folder(folder))

    def test_the_ci_parity_signal_reads_workflow_files_alone_and_none_in_a_folder_it_cannot_examine(self) -> None:
        _write_fixture(self.root, {".github/workflows/notes.txt": "run: ai_config --check"})
        self.assertFalse(audit._has_ci_parity_workflow(self.root))
        _write_fixture(self.root, {".github/workflows/parity.yaml": "run: ai_config --check"})
        self.assertTrue(audit._has_ci_parity_workflow(self.root))
        with stat_denied(self.root / ".github/workflows", older=False):
            self.assertFalse(audit._has_ci_parity_workflow(self.root))

    def test_an_orphan_candidate_it_cannot_examine_leaves_the_next_one_checked(self) -> None:
        _setup_conforming_repo(self.root)
        with stat_denied(self.root / ".github/copilot-instructions.md", older=False):
            rows = _finding_rows(audit.check_orphans(self.root, {"artifacts": []}))
        self.assertIn(("WARNING", "orphan", "AGENTS.md", "Generated file is no longer listed in the manifest"), rows)
        self.assertNotIn(".github/copilot-instructions.md", [row[2] for row in rows])

    def test_the_instruction_sources_name_each_file_and_folder_where_it_is(self) -> None:
        _write_fixture(self.root, {".claude/CLAUDE.md": "x", ".github/instructions/notes.md": "x"})
        sources = audit._instruction_sources(self.root)
        self.assertEqual((False, True, False), (sources.claude_md, sources.dot_claude_md, sources.path_instructions))
        _write_fixture(self.root, {".github/instructions/a.instructions.md": "x"})
        self.assertTrue(audit._instruction_sources(self.root).path_instructions)

    def test_the_codex_rules_check_reads_rules_files_alone(self) -> None:
        _write_fixture(self.root, {".codex/rules/README.md": "Not Starlark (\n"})
        self.assertEqual([], audit._codex_rules(self.root))

    def test_a_projection_folder_absent_or_unexamined_leaves_the_other_folders_notes(self) -> None:
        _write_fixture(self.root, {".github/agents/reviewer.md": f"<!-- {OWNERSHIP} -->\n"})
        note = ("INFO", "limitation", ".github/agents/reviewer.md")
        self.assertIn(note, [row[:3] for row in _finding_rows(audit.check_limitations(self.root, {}))])
        (self.root / ".github/skills").mkdir()
        with stat_denied(self.root / ".github/skills", older=False):
            rows = _finding_rows(audit.check_limitations(self.root, {}))
        self.assertIn(note, [row[:3] for row in rows])

    def test_a_path_reported_once_leaves_every_finding_after_its_repeat_kept(self) -> None:
        unread = audit._unreadable_finding(self.root, self.root / "CLAUDE.md", "denied")
        found = audit._inventory_finding("REVIEW.md", "Found REVIEW.md")
        self.assertEqual([unread, found], audit._one_unread_error_per_path([unread, unread, found]))

    def test_a_fixed_file_behind_a_link_out_leaves_the_fixed_files_after_it_found(self) -> None:
        root, outside = self.root / "repo", self.root / "outside"
        outside.mkdir()
        _write_fixture(root, {"REVIEW.md": "x", ".mcp.json": "{}"})
        _link_directory(outside, root / ".claude")
        # .claude/CLAUDE.md and .claude/settings.json lie behind the link, and the files after them are found.
        self.assertEqual(
            [
                ("WARNING", "inventory", ".claude", audit.OUTSIDE_LINK),
                ("WARNING", "inventory", ".claude", audit.OUTSIDE_LINK),
                ("INFO", "inventory", "REVIEW.md", "Found REVIEW.md"),
                ("INFO", "inventory", ".mcp.json", "Found .mcp.json"),
            ],
            _finding_rows(audit._inventory_fixed_files(root)),
        )

    def test_a_skill_or_agent_folder_it_cannot_examine_leaves_the_next_folder_listed(self) -> None:
        _write_fixture(
            self.root,
            {
                ".claude/skills/first/SKILL.md": "x",
                ".agents/skills/second/SKILL.md": "x",
                ".github/agents/first.md": "x",
                ".claude/agents/second.md": "x",
            },
        )
        with stat_denied(self.root / ".claude/skills", older=False):
            rows = _finding_rows(audit._inventory_skill_directories(self.root))
        self.assertIn(("INFO", "inventory", ".agents/skills", "Skills found: second"), rows)
        with stat_denied(self.root / ".github/agents", older=False):
            rows = _finding_rows(audit._inventory_agent_directories(self.root))
        self.assertIn(("INFO", "inventory", ".claude/agents", "Custom agent files found: second.md"), rows)

    def test_an_import_that_names_a_file_leaves_the_next_import_checked(self) -> None:
        _write_fixture(self.root, {"docs/guide.md": "x"})
        self.assertEqual(
            [
                (
                    "WARNING",
                    "claude-memory",
                    "CLAUDE.md",
                    "Import @docs/missing.md names no file, so nothing loads for it",
                )
            ],
            _finding_rows(audit._missing_imports(self.root, "CLAUDE.md", ["docs/guide.md", "docs/missing.md"])),
        )

    @unittest.skipIf(sys.platform == "win32", "a symbolic link to a file needs a privilege on Windows")
    def test_an_import_or_a_git_that_is_a_link_to_nothing_is_no_file(self) -> None:
        # Nothing can be read through a link to nothing, so the import loads nothing, and no checkout is there.
        (self.root / "docs").mkdir(parents=True, exist_ok=True)
        (self.root / "docs" / "rules.md").symlink_to(self.root / "nowhere.md")
        self.assertEqual(
            [("WARNING", "claude-memory", "CLAUDE.md", "Import @docs/rules.md names no file, so nothing loads for it")],
            _finding_rows(audit._missing_imports(self.root, "CLAUDE.md", ["docs/rules.md"])),
        )
        checkout = self.root / "checkout"
        checkout.mkdir()
        (checkout / ".git").symlink_to(self.root / "no-git")
        self.assertEqual(f"{checkout} is not a Git repository (no .git found)", audit.not_a_checkout(checkout))


class UnexaminedPathOnNewerPythonTests(UnexaminedPathTests):
    """The same rows with each predicate left on a refused path answering False, as from Python 3.14 on."""

    OLDER = False


class UnlistableDirectoryTests(unittest.TestCase):
    """A directory the audit cannot list is a finding at that directory, never a traceback."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        # The audit resolves its root, so a path the listing or stat refuses is spelled as the resolved root spells
        # it, which differs where the temporary directory is spelled with a short 8.3 name, as on a CI runner.
        self.root = Path(self.temp.name).resolve()
        _setup_conforming_repo(self.root)
        _write_fixture(self.root, {".github/agents/reviewer.md": "---\ndescription: Reviews.\n---\n"})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_a_permission_error_listing_agents_is_an_error_at_the_directory(self) -> None:
        # The agents folder is walked, its subfolders too, so it is refused as every walk and listing meets it.
        with listing_denied(self.root / ".github/agents"):
            result = audit.audit(self.root)
        # The inventory reports it first, and audit() keeps one such ERROR per path.
        self.assertEqual(
            [("ERROR", "inventory")],
            [(f.severity, f.check) for f in result.findings if f.path == ".github/agents"],
        )
        (message,) = [f.message for f in result.findings if f.path == ".github/agents"]
        self.assertEqual(f"{UNREAD} the folder cannot be listed: Access is denied", message)

    def test_an_agent_the_walk_cannot_examine_is_still_listed_and_checked(self) -> None:
        # The folder walk leaves an entry whose stat fails out of its files; the agents walk keeps it, since it may be
        # an agent, so the inventory names it, with the ERROR for the stat it could not make, and the Copilot check
        # still reads it: a stat that fails need not stop a read, so the agent's own ERROR, its missing description,
        # shows it was checked.
        agent = self.root / ".github/agents/reviewer.md"
        agent.write_text("---\nname: reviewer\n---\n", encoding="utf-8")
        with stat_denied(agent, older=True):
            inventory = audit.check_inventory(self.root)
            copilot = audit.check_copilot_configuration(self.root, {})
        self.assertIn(
            ("INFO", "inventory", ".github/agents", "Custom agent files found: reviewer.md"),
            _finding_rows(inventory),
        )
        self.assertIn(
            ("ERROR", "inventory", ".github/agents/reviewer.md"),
            [row[:3] for row in _finding_rows(inventory)],
        )
        self.assertEqual(
            [("ERROR", "copilot-agent", ".github/agents/reviewer.md", "Agent frontmatter requires description")],
            [row for row in _finding_rows(copilot) if row[0] == "ERROR"],
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
        # Each names the link the folder lies behind, so the inventory reports that link once, at its own path.
        nested = self.root / "linked/nested"
        listing = audit.list_directory(self.root, nested)
        self.assertEqual(([], [self.root / "linked"]), (listing.paths, listing.outside))
        walk = audit.walk_files(self.root, nested, lambda name: True)
        self.assertEqual(([], [self.root / "linked"]), (walk.paths, walk.outside))

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

    def _audit_reading(self) -> tuple[audit.AuditResult, list[Path]]:
        """The audit's result, and every file it read, resolved."""
        read: list[Path] = []
        read_text, read_bytes = Path.read_text, Path.read_bytes

        def text(path: Path, *args: Any, **kwargs: Any) -> str:
            read.append(path.resolve())
            return read_text(path, *args, **kwargs)

        def data(path: Path) -> bytes:
            read.append(path.resolve())
            return read_bytes(path)

        with mock.patch.object(Path, "read_text", text), mock.patch.object(Path, "read_bytes", data):
            result = audit.audit(self.root)
        return result, read

    # Each folder a fixed path passes through, and the files a check reads there by name, each holding text a finding
    # would show if a check read it.
    FIXED_PATH_FOLDERS: ClassVar[dict[str, dict[str, str | bytes]]] = {
        ".claude": {
            "settings.json": '{"permissions": {"allow": ["Bash(elsewhere)"]}, "hooks": {"Elsewhere": []}}\n',
            "CLAUDE.md": "# Elsewhere\n\n@elsewhere/missing.md\n",
        },
        ".codex": {
            "config.toml": '[mcp_servers.elsewhere]\nurl = "https://example.invalid"\nenv = {}\n',
            "hooks.json": '{"hooks": {"Elsewhere": []}}\n',
        },
        ".vscode": {"mcp.json": '{"mcpServers": {"elsewhere": {}}}\n'},
        ".github/scripts": {"ai_config.py": "TARGET_RUNTIMES = ['elsewhere']  # CLAUDE.md\n"},
        "scripts": {"ai_config.py": "TARGET_RUNTIMES = ['elsewhere']  # CLAUDE.md\n"},
        ".agents/skills/demo/agents": {"openai.yaml": "policy:\n  allow_implicit_invocation: elsewhere\n"},
    }

    def _fresh_repository(self, name: str) -> None:
        """A conforming repository of its own as self.root, whose manifest lists the Codex and VS Code configuration
        and whose skill is user-only, so the parity and Codex checks read those files too."""
        self.root = Path(self.temp.name) / name / "repo"
        _setup_conforming_repo(self.root)
        manifest = json.loads((self.root / MANIFEST_PATH).read_text(encoding="utf-8"))
        manifest["artifacts"].extend({"path": rel} for rel in (".codex/config.toml", ".vscode/mcp.json"))
        (self.root / MANIFEST_PATH).write_text(json.dumps(manifest), encoding="utf-8")
        canonical = self.root / ".claude/skills/demo/SKILL.md"
        canonical.write_text(
            canonical.read_text(encoding="utf-8").replace("---\n\n", "disable-model-invocation: true\n---\n\n", 1),
            encoding="utf-8",
        )

    def test_no_check_reads_a_fixed_path_through_a_link_out_of_the_repository(self) -> None:
        for folder, files in self.FIXED_PATH_FOLDERS.items():
            with self.subTest(folder=folder):
                self._fresh_repository(folder.replace("/", "-"))
                target = self.outside / folder.replace("/", "-")
                if (self.root / folder).is_dir():
                    shutil.move(self.root / folder, target)  # what the folder held now lies behind the link
                _write_fixture(target, files)
                _link_directory(target, self.root / folder)
                result, read = self._audit_reading()
                self.assertEqual([], [path for path in read if path.is_relative_to(target.resolve())])
                self.assertEqual([("WARNING", "inventory", folder)], self._outside_links(result))
                for finding in result.findings:
                    self.assertNotIn("elsewhere", finding.message.lower(), finding)

    def test_the_codex_check_names_the_link_its_invocation_policy_lies_behind(self) -> None:
        # The walks name this link too; the Codex rule names it itself, so its ERROR never stands alone wherever it
        # is called from.
        self._fresh_repository("policy")
        _write_fixture(self.outside / "agents", {"openai.yaml": "policy:\n  allow_implicit_invocation: false\n"})
        _link_directory(self.outside / "agents", self.root / ".agents/skills/demo/agents")
        skill = self.root / ".agents/skills/demo/SKILL.md"
        values: dict[str, audit.FrontmatterValue] = {"name": "demo", "disable-model-invocation": "true"}
        found = [
            (f.severity, f.check, f.path)
            for f in audit._codex_skill_rules(skill, self.root, ".agents/skills/demo/SKILL.md", values)
        ]
        self.assertEqual(
            [
                ("ERROR", "codex-skill", ".agents/skills/demo/agents/openai.yaml"),
                ("WARNING", "inventory", ".agents/skills/demo/agents"),
            ],
            found,
        )

    def test_a_canonical_source_behind_a_link_out_is_an_error_so_the_audit_is_not_compliant(self) -> None:
        shutil.move(self.root / ".claude", self.outside / "claude")
        _write_fixture(self.outside / "claude", {"CLAUDE.md": (self.root / "CLAUDE.md").read_text(encoding="utf-8")})
        _link_directory(self.outside / "claude", self.root / ".claude")
        result = audit.audit(self.root)
        self.assertEqual("ERRORS", result.result)
        not_followed = (
            "it lies behind .claude, a link that leads out of the repository, which the audit does not follow"
        )
        self.assertIn(
            ("ERROR", "inventory", ".claude/CLAUDE.md", UnreadInputTests.UNREAD + not_followed),
            [(f.severity, f.check, f.path, f.message) for f in result.findings],
        )
        self.assertNotIn(".claude/CLAUDE.md", [f.path for f in result.findings if f.severity == "INFO"])

    @unittest.skipIf(sys.platform == "win32", "Windows makes a file symbolic link only with a privilege tests lack")
    def test_no_check_reads_a_root_file_that_is_a_link_out_of_the_repository(self) -> None:
        for rel, text in {
            "CLAUDE.md": "# Elsewhere\n",
            "CLAUDE.local.md": "# Elsewhere\n",
            "AGENTS.md": "# Elsewhere\n",
            "GEMINI.md": "# Elsewhere\n",
            ".mcp.json": '{"mcpServers": {"elsewhere": {}}}\n',
        }.items():
            with self.subTest(rel=rel):
                target = self.outside / f"root-{rel}"
                target.write_text(text, encoding="utf-8")
                original = self.root / rel
                kept = original.read_bytes() if original.is_file() else None
                original.unlink(missing_ok=True)
                original.symlink_to(target)
                result, read = self._audit_reading()
                self.assertNotIn(target.resolve(), read)
                self.assertEqual([("WARNING", "inventory", rel)], self._outside_links(result))
                self.assertNotIn(f"Found {rel}", [f.message for f in result.findings])
                for finding in result.findings:
                    self.assertNotIn("elsewhere", finding.message.lower(), finding)
                original.unlink()
                if kept is not None:
                    original.write_bytes(kept)

    def test_the_link_on_the_way_to_a_path_is_the_first_that_leads_out(self) -> None:
        _write_fixture(self.root, {"inside/file.md": "x"})
        _link_directory(self.root / "inside", self.root / "stays")
        _link_directory(self.outside, self.root / "leaves")
        self.assertIsNone(audit.outside_link(self.root, self.root / "inside/file.md"))
        self.assertIsNone(audit.outside_link(self.root, self.root / "stays/file.md"))
        self.assertIsNone(audit.outside_link(self.root, self.root / "absent/file.md"))
        self.assertEqual(self.root / "leaves", audit.outside_link(self.root, self.root / "leaves"))
        self.assertEqual(self.root / "leaves", audit.outside_link(self.root, self.root / "leaves/nested/SKILL.md"))
        with self.assertRaisesRegex(OSError, "it lies behind leaves, a link that leads out of the repository"):
            audit.read_text(self.root, self.root / "leaves/AGENTS.md")
        self.assertEqual("x", audit.read_text(self.root, self.root / "stays/file.md"))

    def test_every_file_read_goes_through_the_guarded_readers(self) -> None:
        # read_text and read_bytes refuse a path behind a link out of the root, which no other read would.
        for script in ("audit_ai_config.py", "mcp_handshake.py"):
            tree = ast.parse((Path(audit.__file__).parent / script).read_text(encoding="utf-8"))
            readers = {"read_bytes", "read_text"} if script == "audit_ai_config.py" else set()
            allowed = {
                id(node)
                for function in ast.walk(tree)
                if isinstance(function, ast.FunctionDef) and function.name in readers
                for node in ast.walk(function)
            }
            reads = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and id(node) not in allowed
                and (
                    (isinstance(node.func, ast.Attribute) and node.func.attr in {"read_text", "read_bytes", "open"})
                    or (isinstance(node.func, ast.Name) and node.func.id == "open")
                    # A parser that reads a file handle; check_provenance.load reads the skill's own file.
                    or (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "load"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in {"json", "tomllib"}
                    )
                )
            ]
            self.assertEqual([], reads, script)

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

    def test_a_root_file_whose_size_cannot_be_read_is_an_error_never_measured_as_within_the_cap(self) -> None:
        _write_fixture(self.root, {"AGENTS.md": "# Agents\n"})
        agents = self.root / "AGENTS.md"
        real = Path.stat

        def stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            if path == agents and follow_symlinks:
                raise PermissionError(13, "Access is denied", str(path))
            return real(path, follow_symlinks=follow_symlinks)

        with mock.patch.object(Path, "stat", stat):
            findings = audit._codex_size(self.root, "AGENTS.md")
        self.assertEqual(
            [
                (
                    "ERROR",
                    "layering",
                    "AGENTS.md",
                    "Codex: the size of the root AGENTS.md cannot be read (Access is denied), so whether Codex reads "
                    "it whole is not known",
                )
            ],
            [(f.severity, f.check, f.path, f.message) for f in findings],
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

    def test_a_nested_agents_md_beside_an_override_leaves_the_next_nested_agents_md_checked(self) -> None:
        rows = self._codex(
            {
                "AGENTS.md": REDIRECTING_AGENTS,
                "a/AGENTS.override.md": "Override.\n",
                "a/AGENTS.md": PLAIN_AGENTS,
                "b/AGENTS.md": PLAIN_AGENTS,
            }
        )
        self.assertEqual(["a/AGENTS.override.md", "b/AGENTS.md"], [path for _, path, _ in rows if path])

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

    def test_a_provenance_failure_holding_a_line_break_is_one_failed_line(self) -> None:
        # The provenance error names a source the reference file holds, whose text may span lines.
        refusal = check_provenance.ProvenanceError("no source for\nSKIPPED x\u2028y")
        with mock.patch.object(audit, "reported_sources", side_effect=refusal):
            self.assertEqual((1, "FAILED no source for SKIPPED x y\n"), self._main("--provenance"))

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

    def test_the_relay_names_the_sources_of_the_checks_it_reports_only_when_asked(self) -> None:
        code, plain = self._main("--relay")
        self.assertEqual(1, code)
        self.assertNotIn("SOURCE\t", plain)
        code, report = self._main("--relay", "--provenance")
        self.assertEqual(1, code)
        self.assertEqual(plain, report[: len(plain)])
        self.assertEqual(
            "SOURCE\tauthority\tdecision\tA repository counts as generated from CLAUDE.md only on the signals check 2 "
            "of references/audit-policy.md names, the layout of this source's retired init-ai-config generator, so a "
            "repository that keeps its instructions another way is never reported as drifted.\n",
            report[len(plain) :],
        )

    def test_the_relay_and_json_reports_are_one_or_the_other(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self._main("--relay", "--json")
        self.assertEqual(2, raised.exception.code)

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
        self.assertEqual(
            [f"FAILED {broken.as_posix()}: no entry for agent-skill, authority, "], [output.split("behavioral")[0]]
        )


# ---------------------------------------------------------------------------
# Unparseable configuration a repository supplies
# ---------------------------------------------------------------------------

# Past the parsers' nesting limit, and past Python's limit on the digits of an integer it converts from text.
TOO_DEEP = "[" * 100_000 + "]" * 100_000
OVER_LONG_INTEGER = "1" * 5_000
UNPARSEABLE_JSON = {"too_deep": TOO_DEEP, "over_long_integer": '{"value": ' + OVER_LONG_INTEGER + "}"}
UNPARSEABLE_TOML = {"too_deep": "value = " + TOO_DEEP, "over_long_integer": "value = " + OVER_LONG_INTEGER}
# Python source Python's parser refuses, each in its own way: too deep for its stack, too many nested parentheses,
# and a null byte.
PAST_THE_PARSER = {
    "too_deep": "-" * 100_000 + "1",
    "too_many_parentheses": "x = " + "(" * 100_000 + ")" * 100_000,
    "null_byte": "x = 1\0",
}
# Each reader of a JSON or TOML file a repository supplies, with the finding that names a file it cannot parse.
UNPARSEABLE_READERS = {
    ".mcp.json": (UNPARSEABLE_JSON, "ERROR", "mcp", "Could not read or parse:"),
    ".github/mcp.json": (UNPARSEABLE_JSON, "ERROR", "mcp", "Could not read or parse:"),
    ".vscode/mcp.json": (UNPARSEABLE_JSON, "ERROR", "mcp", "Could not read or parse:"),
    ".claude/settings.json": (UNPARSEABLE_JSON, "ERROR", "claude-settings", "Could not read or parse:"),
    ".codex/hooks.json": (UNPARSEABLE_JSON, "ERROR", "codex-hook", "Could not read or parse:"),
    ".codex/config.toml": (UNPARSEABLE_TOML, "ERROR", "mcp", "Invalid TOML:"),
    ".codex/agents/reviewer.toml": (UNPARSEABLE_TOML, "ERROR", "codex-agent", "Could not read or parse:"),
    MANIFEST_PATH: (UNPARSEABLE_JSON, "WARNING", "authority", "Manifest exists but is malformed"),
}


@pytest.mark.parametrize("shape", sorted(UNPARSEABLE_JSON))
@pytest.mark.parametrize("rel", sorted(UNPARSEABLE_READERS))
def test_an_unparseable_file_is_its_reader_s_finding_and_the_audit_carries_on(rel: str, shape: str) -> None:
    contents, severity, check, prefix = UNPARSEABLE_READERS[rel]
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _setup_conforming_repo(root)
        _write_fixture(root, {rel: contents[shape]})
        result = audit.audit(root)
        report = audit.format_markdown(result, None)
    assert [(severity, check, rel)] == [
        (f.severity, f.check, f.path) for f in result.findings if f.path == rel and f.message.startswith(prefix)
    ]
    assert any(f.check == "inventory" and f.path == "CLAUDE.md" for f in result.findings), "the audit carried on"
    assert markdown_literal.markdown_line(rel) in report


@pytest.mark.parametrize("shape", sorted(PAST_THE_PARSER))
def test_a_rules_file_past_the_parser_s_limits_is_an_error_and_the_audit_carries_on(shape: str) -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _setup_conforming_repo(root)
        _write_fixture(root, {".codex/rules/deep.rules": PAST_THE_PARSER[shape]})
        result = audit.audit(root)
    assert [
        (f.severity, f.check)
        for f in result.findings
        if f.path == ".codex/rules/deep.rules" and f.message.startswith("Could not read or parse:")
    ] == [("ERROR", "codex-rule")]


@pytest.mark.parametrize("shape", sorted(PAST_THE_PARSER))
def test_a_generator_past_the_parser_s_limits_has_an_unreadable_scope(shape: str) -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _write_fixture(root, {"scripts/ai_config.py": PAST_THE_PARSER[shape]})
        assert audit.derive_generator_scope(root) == (None, "generator-scope-unreadable")


@pytest.mark.parametrize("error", [RecursionError, MemoryError])
def test_a_scope_literal_too_deep_to_evaluate_is_not_a_literal(error: type[Exception]) -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _write_fixture(root, {"scripts/ai_config.py": 'TARGET_RUNTIMES = ["claude"]\n'})
        with mock.patch.object(ast, "literal_eval", side_effect=error):
            assert audit.derive_generator_scope(root) == (None, "generator-scope-not-literal")

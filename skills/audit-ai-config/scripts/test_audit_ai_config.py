#!/usr/bin/env python3
"""Fixture tests for the AI agent configuration audit engine."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

import audit_ai_config as audit


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
    (root / ".github/copilot-instructions.md").write_text(
        copilot_content, encoding="utf-8"
    )
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
    (root / ".agents/skills/demo/SKILL.md").write_text(
        shim_content, encoding="utf-8"
    )

    (root / ".claude/skills/demo/SKILL.md").write_text(
        "---\nname: demo\ndescription: Test skill.\n---\n\n"
        "Canonical workflow.\n",
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
    (root / ".github/ai-config-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


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
        (self.root / ".github/scripts/ai_config.py").write_text(
            "# Reads CLAUDE.md\n", encoding="utf-8"
        )
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
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        cls, _, _ = audit.classify_authority(self.root)
        self.assertEqual("alternative", cls)

    def test_ambiguous_no_false_drift(self) -> None:
        """Ambiguous repo should not produce drift findings."""
        (self.root / "CLAUDE.md").write_text("# CLAUDE.md\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text(
            "# Custom AGENTS.md\n", encoding="utf-8"
        )
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
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
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
        self.assertEqual(1, result.exit_code)
        self.assertEqual("INCONCLUSIVE", result.result)

    def test_ambiguous_is_inconclusive_with_exit_1(self) -> None:
        (self.root / "CLAUDE.md").write_text("# CLAUDE.md\n", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertEqual(1, result.exit_code)
        self.assertEqual("INCONCLUSIVE", result.result)

    def test_alternative_is_inconclusive_with_exit_1(self) -> None:
        (self.root / ".github").mkdir(parents=True)
        manifest = {
            "generatedBy": "custom_gen.py",
            "canonicalSource": "AGENTS.md",
        }
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
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
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                mock.patch.object(sys, "argv", ["audit_ai_config.py", *arguments]):
            code = audit.main()
        return code, output.getvalue().splitlines(), errors.getvalue()

    def test_a_non_repository_fails_on_stdout_with_exit_1(self) -> None:
        for extra in ((), ("--json",)):
            with self.subTest(extra=extra):
                code, lines, errors = self._main("--root", str(self.root), *extra)
                self.assertEqual(1, code)
                self.assertEqual(
                    [f"FAILED {self.root} is not a Git repository (no .git found)"], lines
                )
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
            any(
                f.severity == "ERROR" and "missing" in f.message.lower()
                for f in result.findings
            ),
        )

    def test_json_hash_mismatch_is_conflict(self) -> None:
        """Modified JSON artifact flagged as hash mismatch."""
        (self.root / ".github/copilot-instructions.md").write_text(
            f"> {OWNERSHIP}.\n\nModified content.\n", encoding="utf-8"
        )
        # Update manifest to point to copilot-instructions as JSON for this test
        # Actually, copilot-instructions has a hash in our fixture
        result = audit.audit(self.root)
        errors = [f for f in result.findings if f.severity == "ERROR"]
        self.assertTrue(len(errors) > 0)

    def test_manifest_path_traversal_rejected(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["artifacts"].append({"path": "../outside/evil.md"})
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.severity == "ERROR" and "path traversal" in f.message
                for f in result.findings
            ),
        )

    def test_manifest_absolute_path_rejected(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["artifacts"].append({"path": "/etc/passwd"})
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.severity == "ERROR" and "absolute path" in f.message
                for f in result.findings
            ),
        )

    def test_manifest_backslash_traversal_rejected(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["artifacts"].append({"path": "..\\outside\\file.md"})
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.severity == "ERROR" and "backslash" in f.message
                for f in result.findings
            ),
        )

    def test_manifest_drive_qualified_rejected(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["artifacts"].append({"path": "C:/Windows/System32"})
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.severity == "ERROR" and "drive-qualified" in f.message
                for f in result.findings
            ),
        )

    def test_manifest_path_outside_allowlist_rejected(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["artifacts"].append({"path": "src/main.py"})
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.severity == "ERROR" and "allowlist" in f.message
                for f in result.findings
            ),
        )

    def test_ownership_marker_missing(self) -> None:
        (self.root / "AGENTS.md").write_text(
            "# Plain AGENTS.md\n\nNo marker.\n", encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check in ("ownership", "collision", "parity")
                and f.severity == "ERROR"
                for f in result.findings
            ),
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
            any(
                f.check == "orphan"
                and "orphan" in f.path
                for f in result.findings
            ),
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
            any(
                f.check == "mcp" and f.severity == "ERROR" and ".mcp.json" in (f.path or "")
                for f in result.findings
            ),
        )

    def test_malformed_codex_toml(self) -> None:
        (self.root / ".codex").mkdir(exist_ok=True)
        (self.root / ".codex/config.toml").write_text(
            "invalid [[ toml", encoding="utf-8"
        )
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
            audit.tomllib,
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

    def test_vscode_mcp_wrong_schema(self) -> None:
        (self.root / ".vscode").mkdir(exist_ok=True)
        (self.root / ".vscode/mcp.json").write_text(
            '{"mcpServers": {}}', encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "mcp" and "servers" in f.message.lower()
                for f in result.findings
            ),
        )

    def test_duplicate_server_names(self) -> None:
        (self.root / ".mcp.json").write_text(
            '{"mcpServers": {"srv": {"command": "a"}}}', encoding="utf-8"
        )
        (self.root / ".github/mcp.json").write_text(
            '{"mcpServers": {"srv": {"command": "b"}}}', encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "mcp" and "Duplicate" in f.message
                for f in result.findings
            ),
        )

    def test_sse_codex_rejected(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["mcp_servers"] = [
            {"name": "bad", "transport": "sse", "targets": ["codex"]}
        ]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "mcp"
                and "sse" in f.message
                and "codex" in f.message
                for f in result.findings
            ),
        )

    def test_local_claude_rejected(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["mcp_servers"] = [
            {"name": "bad", "transport": "local", "targets": ["claude"]}
        ]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "mcp"
                and "local" in f.message
                and "claude" in f.message
                for f in result.findings
            ),
        )

    def test_codex_http_env_rejected(self) -> None:
        (self.root / ".codex").mkdir(exist_ok=True)
        (self.root / ".codex/config.toml").write_text(
            '[mcp_servers.srv]\n'
            'url = "http://localhost:8080"\n'
            '[mcp_servers.srv.env]\n'
            'KEY = "value"\n',
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "mcp" and "STDIO-only" in f.message
                for f in result.findings
            ),
        )

    def test_copilot_repository_emits_warning(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["surfaces"] = ["copilot_cli"]
        manifest["mcp_servers"] = [
            {"name": "srv", "targets": ["copilot_repository"], "transport": "stdio"},
        ]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "mcp"
                and f.severity == "WARNING"
                and "repository" in f.message.lower()
                for f in result.findings
            ),
        )

    def test_code_review_repository_mcp_readonlyhint_warning(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["surfaces"] = ["code_review"]
        manifest["mcp_servers"] = [
            {"name": "srv", "targets": ["copilot_repository"], "transport": "stdio"},
        ]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "mcp"
                and "readOnlyHint" in f.message
                for f in result.findings
            ),
        )

    def test_copilot_local_tools_allowlist_reported(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["surfaces"] = ["copilot_cli"]
        manifest["mcp_servers"] = [
            {"name": "srv", "targets": ["claude", "copilot_local"], "transport": "stdio"},
        ]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (self.root / ".mcp.json").write_text(
            '{"mcpServers": {"srv": {"command": "a", "tools": ["tool_a"]}}}',
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "mcp"
                and "allowlist" in f.message.lower()
                for f in result.findings
            ),
        )

    def test_mcp_json_non_dict_toplevel(self) -> None:
        """Non-dict top-level JSON should produce an ERROR, not crash."""
        (self.root / ".mcp.json").write_text("[]", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "mcp"
                and f.severity == "ERROR"
                and "object" in f.message.lower()
                for f in result.findings
            ),
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
            any(
                f.check == "mcp"
                and "differ" in f.message.lower()
                and "srv" in f.message
                for f in result.findings
            ),
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
            any(
                f.check == "mcp"
                and "differ" in f.message.lower()
                and "srv" in f.message
                for f in result.findings
            ),
        )

    def test_mcp_json_missing_mcpservers_key(self) -> None:
        """Empty object in .mcp.json should produce WARNING about missing key."""
        (self.root / ".mcp.json").write_text("{}", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "mcp"
                and f.severity == "WARNING"
                and "mcpServers" in f.message
                and "not found" in f.message
                for f in result.findings
            ),
        )

    def test_mcp_json_non_dict_server_entry(self) -> None:
        """Non-dict server entry should produce WARNING, not crash."""
        (self.root / ".mcp.json").write_text(
            '{"mcpServers": {"srv": "not-a-dict"}}', encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "mcp"
                and f.severity == "WARNING"
                and "srv" in f.message
                and "object" in f.message.lower()
                for f in result.findings
            ),
        )

    def test_mcp_json_omitted_tools_reported_unrestricted(self) -> None:
        """Server with omitted tools field is unrestricted for copilot_local."""
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["surfaces"] = ["copilot_cli"]
        manifest["mcp_servers"] = [
            {"name": "srv", "targets": ["claude", "copilot_local"], "transport": "stdio"},
        ]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (self.root / ".mcp.json").write_text(
            '{"mcpServers": {"srv": {"command": "a"}}}', encoding="utf-8"
        )
        result = audit.audit(self.root)
        # Omitted tools = unrestricted, which is fine (no allowlist to reject)
        mcp_errors = [
            f for f in result.findings
            if f.check == "mcp" and f.severity == "ERROR" and "allowlist" in f.message.lower()
        ]
        self.assertEqual(0, len(mcp_errors))

    def test_mcp_json_wildcard_tools_not_rejected(self) -> None:
        """tools: ["*"] is unrestricted — not a restricted allowlist."""
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["surfaces"] = ["copilot_cli"]
        manifest["mcp_servers"] = [
            {"name": "srv", "targets": ["claude", "copilot_local"], "transport": "stdio"},
        ]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (self.root / ".mcp.json").write_text(
            '{"mcpServers": {"srv": {"command": "a", "tools": ["*"]}}}',
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        mcp_errors = [
            f for f in result.findings
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
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["surfaces"] = ["cloud_agent"]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "layering" and "redirects" in f.message
                for f in result.findings
            ),
        )

    def test_cloud_agent_no_agents_md(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["surfaces"] = ["cloud_agent"]
        manifest["runtimes"] = ["claude"]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (self.root / "AGENTS.md").unlink()
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "layering"
                and "directly" in f.message
                for f in result.findings
            ),
        )

    def test_cloud_agent_non_redirecting_agents_md(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["surfaces"] = ["cloud_agent"]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (self.root / "AGENTS.md").write_text(
            f"<!-- {OWNERSHIP} -->\n\n"
            "# Custom instructions\n\nDo something else.\n",
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "layering"
                and "non-redirecting" in f.message
                for f in result.findings
            ),
        )

    def test_nested_agents_md_warning(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["surfaces"] = ["cloud_agent"]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (self.root / "src").mkdir(exist_ok=True)
        (self.root / "src/AGENTS.md").write_text(
            "# Nested\n\nSubtree instructions.\n", encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "layering"
                and "Nested" in f.message
                and "supersedes" in f.message
                for f in result.findings
            ),
        )

    def test_code_review_emits_warning(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["surfaces"] = ["code_review"]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "layering"
                and "code-review" in f.message.lower()
                for f in result.findings
            ),
        )

    def test_vscode_emits_settings_warning(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["surfaces"] = ["vscode"]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "layering"
                and "useClaudeMdFile" in f.message
                for f in result.findings
            ),
        )

    def test_copilot_local_folder_trust_warning(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["surfaces"] = ["copilot_cli"]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "layering"
                and "trust" in f.message.lower()
                for f in result.findings
            ),
        )


    def test_codex_override_masks_adapter(self) -> None:
        (self.root / "AGENTS.override.md").write_text(
            "# Override\n\nCustom instructions.\n", encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "layering"
                and f.severity == "ERROR"
                and "override" in f.message.lower()
                for f in result.findings
            ),
        )

    def test_codex_nested_override_warning(self) -> None:
        (self.root / "src").mkdir(exist_ok=True)
        (self.root / "src/AGENTS.override.md").write_text(
            "# Subtree override\n\nCustom.\n", encoding="utf-8"
        )
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
        (self.root / "src/AGENTS.md").write_text(
            "# Subtree instructions\n\nNon-redirecting.\n", encoding="utf-8"
        )
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
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["surfaces"] = ["jetbrains"]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (self.root / ".github/copilot-instructions.md").unlink()
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "layering"
                and f.severity == "ERROR"
                and "jetbrains" in f.message.lower()
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
            any(
                f.check == "mcp"
                and "differ" in f.message.lower()
                and "srv" in f.message
                for f in result.findings
            ),
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
                f.check == "parity"
                and f.severity == "ERROR"
                and "deterministic" in f.message.lower()
                for f in result.findings
            ),
        )

    def test_parity_copilot_instructions_drift(self) -> None:
        """copilot-instructions.md with marker but wrong sections is flagged."""
        # Remove the hash so the deterministic content comparison is reached
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        for artifact in manifest["artifacts"]:
            if artifact["path"] == ".github/copilot-instructions.md":
                artifact.pop("hash", None)
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
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
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        for artifact in manifest["artifacts"]:
            if artifact["path"] == ".github/copilot-instructions.md":
                artifact.pop("hash", None)
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        # Rewrite with a custom title but keep the same sections from CLAUDE.md
        claude_content = audit.read_text(self.root / "CLAUDE.md")
        sections = audit._extract_sections(
            claude_content, audit.DEFAULT_COPILOT_SECTIONS
        )
        (self.root / ".github/copilot-instructions.md").write_text(
            f"# Totally Custom Title\n\n"
            f"{audit.COPILOT_BANNER}\n\n"
            f"{sections}\n",
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        parity_errors = [
            f for f in result.findings
            if f.check == "parity"
            and f.severity == "ERROR"
            and f.path == ".github/copilot-instructions.md"
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
        sections = audit._extract_sections(
            claude_content, manifest["copilot_sections"]
        )
        (self.root / ".github/copilot-instructions.md").write_text(
            f"# Custom Title\n\n{audit.COPILOT_BANNER}\n\n{sections}\n",
            encoding="utf-8",
        )

        result = audit.audit(self.root)
        parity_errors = [
            finding for finding in result.findings
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
        behavioral_warnings = [
            f for f in result.findings
            if f.check == "behavioral" and "never" in f.message.lower()
        ]
        self.assertEqual(0, len(behavioral_warnings))

    def test_never_disable_missing(self) -> None:
        (self.root / "CLAUDE.md").write_text(
            "# CLAUDE.md\n\n## Overview\n\nNo constraints here.\n\n"
            "## Maintaining AI Agent Config\n\nRun the generator.\n",
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "behavioral" and "never" in f.message.lower()
                for f in result.findings
            ),
        )

    def test_workaround_rule_missing_and_present(self) -> None:
        body = (
            "# CLAUDE.md\n\n## Overview\n\nNever disable anything. Coverage 80%.\n\n"
            "## Maintaining AI Agent Config\n\nRun the generator.\n"
        )

        def workaround_warnings() -> list:
            return [
                f for f in audit.audit(self.root).findings
                if f.check == "behavioral" and "workarounds" in f.message
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
            any(
                f.check == "behavioral" and "quality gate" in f.message.lower()
                for f in result.findings
            ),
        )

    def test_quality_gate_requires_an_actual_gate(self) -> None:
        base = (
            "# CLAUDE.md\n\n## Maintaining AI Agent Config\n\nRun the generator.\n\n"
            "Never disable anything.\n\n"
        )

        def gate_warnings(text: str) -> list:
            (self.root / "CLAUDE.md").write_text(base + text + "\n", encoding="utf-8")
            return [
                f for f in audit.audit(self.root).findings
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
        (self.root / "AGENTS.md").write_text(
            "# My custom AGENTS.md\n\nUser content.\n", encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "collision" and "AGENTS.md" in (f.path or "")
                for f in result.findings
            ),
        )

    def test_user_authored_copilot_instructions_collision(self) -> None:
        (self.root / ".github/copilot-instructions.md").write_text(
            "# My custom instructions\n\nUser content.\n", encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check in ("collision", "parity")
                and f.severity == "ERROR"
                for f in result.findings
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

    def _manifest(self) -> dict[str, object]:
        path = self.root / ".github/ai-config-manifest.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_manifest(self, manifest: dict[str, object]) -> None:
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

    def test_agent_frontmatter_target_and_tools_validated(self) -> None:
        path = self.root / ".github/agents/reviewer.agent.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\ndescription: Review changes\ntarget: invalid\ntools: []\n---\n",
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertTrue(any(
            f.check == "copilot-agent" and "target must" in f.message
            for f in result.findings
        ))
        self.assertTrue(any(
            f.check == "copilot-agent" and "tools must" in f.message
            for f in result.findings
        ))

    def test_agent_filename_and_description_validated(self) -> None:
        path = self.root / ".claude/agents/bad name.txt"
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: bad\n---\n", encoding="utf-8")
        result = audit.audit(self.root)
        self.assertTrue(any(f.check == "copilot-agent" and f.severity == "ERROR" for f in result.findings))

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
        self.assertTrue(any(
            f.check == "provenance" and "not owned by the manifest" in f.message
            for f in result.findings
        ))

    def test_manifest_projection_requires_marker_and_hash(self) -> None:
        projection = self.root / ".github/agents/project-agent.agent.md"
        projection.parent.mkdir(parents=True)
        content = "---\ndescription: Project agent.\n---\n"
        projection.write_text(content, encoding="utf-8")
        manifest = self._manifest()
        manifest["artifacts"].append({
            "path": ".github/agents/project-agent.agent.md",
            "hash": _content_hash(content),
        })
        self._write_manifest(manifest)
        result = audit.audit(self.root)
        self.assertTrue(any(
            f.check == "provenance" and "lacks an ownership marker" in f.message
            for f in result.findings
        ))

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
        self.assertTrue(any(
            f.check == "runtime-role" and "cloud_agent must declare" in f.message
            for f in result.findings
        ))

    def test_scope_is_derived_from_literal_generator_constants(self) -> None:
        script = self.root / ".github/scripts/ai_config.py"
        script.parent.mkdir(parents=True)
        script.write_text(
            "TARGET_RUNTIMES = ['claude', 'codex']\n"
            "TARGET_SURFACES = ['copilot_cli']\n"
            "TARGET_FEATURES = []\n",
            encoding="utf-8",
        )
        result = audit.audit(self.root)
        self.assertEqual("independently-derived", result.scope_status)
        manifest = self._manifest()
        manifest["surfaces"] = []
        self._write_manifest(manifest)
        result = audit.audit(self.root)
        self.assertTrue(any(f.check == "scope" and f.severity == "ERROR" for f in result.findings))

    def test_scope_without_manifest_or_generator_is_informational(self) -> None:
        (self.root / ".github/ai-config-manifest.json").unlink()
        result = audit.audit(self.root)
        self.assertEqual("conforming", result.authority)
        self.assertEqual("no-declared-scope", result.scope_status)
        scope = [f for f in result.findings if f.check == "scope"]
        self.assertEqual(["INFO"], [f.severity for f in scope])
        self.assertIn("target-specific checks were not run", scope[0].message)

    def test_manifest_without_generator_still_warns_about_editable_scope(self) -> None:
        result = audit.audit(self.root)
        self.assertEqual("manifest-declared-only", result.scope_status)
        self.assertTrue(any(f.check == "scope" and f.severity == "WARNING" for f in result.findings))

    def test_code_review_head_branch_trust_warning(self) -> None:
        manifest = self._manifest()
        manifest["surfaces"] = ["code_review"]
        self._write_manifest(manifest)
        result = audit.audit(self.root)
        self.assertTrue(any(
            f.check == "trust-boundary" and "PR head" in f.message
            for f in result.findings
        ))


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
        """#23: Non-object JSON manifest produces WARNING, no crash."""
        (self.root / ".github/ai-config-manifest.json").write_text(
            "[]", encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "authority"
                and "not a JSON object" in f.message
                for f in result.findings
            ),
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
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
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
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
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
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
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
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
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
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["surfaces"] = ["copilot_local"]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "vocabulary"
                and f.severity == "ERROR"
                and "copilot_local" in f.message
                for f in result.findings
            ),
        )

    def test_unknown_runtime_in_manifest_is_error(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["runtimes"] = ["claude", "unknown_runtime"]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "vocabulary"
                and "unknown_runtime" in f.message
                for f in result.findings
            ),
        )

    def test_unknown_mcp_target_in_manifest_is_error(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["mcp_servers"] = [
            {"name": "srv", "targets": ["bad_target"], "transport": "stdio"},
        ]
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "vocabulary"
                and "bad_target" in f.message
                for f in result.findings
            ),
        )

    def test_valid_vocabulary_passes(self) -> None:
        """Default conforming repo uses valid vocabulary — no vocabulary errors."""
        result = audit.audit(self.root)
        vocab_errors = [
            f for f in result.findings if f.check == "vocabulary"
        ]
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
        summary = [
            line for line in audit.format_markdown(result).splitlines()
            if line.startswith("SUMMARY ")
        ]
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
            self.assertLessEqual(
                sorted_f[i].sort_key(), sorted_f[i + 1].sort_key()
            )


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

        self.assertEqual(sorted(before.keys()), sorted(after.keys()),
                         "Files created or deleted during audit")
        for rel in before:
            self.assertEqual(before[rel], after[rel],
                             f"File modified during audit: {rel}")


class SchemaVersionTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_schema_version_mismatch_not_conforming(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["schemaVersion"] = 999
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                f.check == "authority" and "schema" in f.message.lower()
                for f in result.findings
            ),
        )

    def test_artifact_non_string_path_caught(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["artifacts"].append({"path": 42})
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                "path must be a string" in f.message
                for f in result.findings
            ),
        )


    def test_invalid_manifest_schema_blocks_conforming(self) -> None:
        """Manifest with bad schema + 2 other signals: manifest not counted."""
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        (root / ".github/scripts").mkdir(parents=True, exist_ok=True)
        claude_content = (
            "# CLAUDE.md\n\n"
            "## Maintaining AI Agent Config\n\nRun the generator.\n"
        )
        (root / "CLAUDE.md").write_text(claude_content, encoding="utf-8")
        (root / ".github/scripts/ai_config.py").write_text(
            "# Reads CLAUDE.md\n", encoding="utf-8"
        )
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
        (root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        cls, _, findings = audit.classify_authority(root)
        self.assertEqual("conforming", cls,
            "Should be conforming via 2 non-manifest signals")
        self.assertTrue(
            any("schema" in f.message.lower() for f in findings),
            "Should have schema warning",
        )
        temp.cleanup()

    def test_manifest_artifact_non_string_path_no_crash(self) -> None:
        """Artifact with non-string path produces finding, not crash."""
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["artifacts"].append({"path": 42})
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(
                "path must be a string" in f.message
                for f in result.findings
            ),
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
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" for f in result.findings),
            [f.message for f in result.findings],
        )
        self.assertNotEqual(0, result.exit_code)

    def test_self_referencing_manifest_not_conforming(self) -> None:
        (self.root / "CLAUDE.md").write_text(
            "# CLAUDE.md\n\n## Overview\n\nTest.\n", encoding="utf-8"
        )
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
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" and "no derived" in f.message.lower()
                for f in result.findings),
            [f.message for f in result.findings],
        )

    def test_empty_artifacts_with_claude_md_not_conforming(self) -> None:
        (self.root / "CLAUDE.md").write_text(
            "# CLAUDE.md\n\n## Overview\n\nTest.\n", encoding="utf-8"
        )
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
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" and "no derived" in f.message.lower()
                for f in result.findings),
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
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["artifacts"].append({"path": ".mcp.json"})
        (self.root / ".mcp.json").write_text(
            '{"mcpServers": {}}\n', encoding="utf-8"
        )
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        cls, _, findings = audit.classify_authority(self.root)
        schema_errors = [
            f for f in findings
            if f.severity == "ERROR" and "hash" in f.message.lower()
        ]
        self.assertTrue(schema_errors, [f.message for f in findings])


class BoolSchemaVersionTest(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _setup_conforming_repo(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_bool_schema_version_not_conforming(self) -> None:
        manifest = json.loads(
            audit.read_text(self.root / ".github/ai-config-manifest.json")
        )
        manifest["schemaVersion"] = True
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = audit.audit(self.root)
        self.assertTrue(
            any(f.severity == "ERROR" and "schema" in f.message.lower()
                for f in result.findings),
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
        (self.root / ".github/copilot-instructions.md").write_bytes(
            b"\x80\x81 invalid utf-8"
        )
        result = audit.audit(self.root)
        error_findings = [
            f for f in result.findings
            if f.severity == "ERROR" and "could not be read" in f.message.lower()
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
        return sorted(
            f.path for f in audit.audit(self.root).findings
            if f.check == "orphan" and "no longer listed" in f.message
        )

    def test_listed_artifacts_are_not_orphans(self) -> None:
        self.assertEqual([], self._orphans())

    def test_marker_file_missing_from_manifest_is_orphan(self) -> None:
        workflow = self.root / ".github/workflows/ai-config-parity.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(f"# {OWNERSHIP}. Do not edit.\nname: AI Config Parity\n", encoding="utf-8")
        user_toml = self.root / ".codex/config.toml"
        user_toml.parent.mkdir(parents=True)
        user_toml.write_text("model = \"x\"\n", encoding="utf-8")
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
            (f.severity, f.message) for f in audit.audit(self.root).findings
            if f.check == "layering" and f.message.startswith("Code review:")
        ]

    def test_copilot_instructions_are_an_effective_source(self) -> None:
        self.assertEqual(
            [("INFO", "Code review: effective sources: .github/copilot-instructions.md")],
            self._review_findings(),
        )

    def test_redirect_only_agents_md_gives_code_review_nothing(self) -> None:
        (self.root / ".github/copilot-instructions.md").unlink()
        findings = self._review_findings()
        self.assertEqual(["ERROR"], [severity for severity, _ in findings])
        self.assertIn("only redirects to CLAUDE.md", findings[0][1])

    def test_path_specific_and_user_agents_md_count(self) -> None:
        (self.root / ".github/copilot-instructions.md").unlink()
        (self.root / "AGENTS.md").write_text("# Rules\n\nUse tabs.\n", encoding="utf-8")
        instructions = self.root / ".github/instructions/api.instructions.md"
        instructions.parent.mkdir(parents=True)
        instructions.write_text("Prefer async.\n", encoding="utf-8")
        self.assertEqual(
            [("INFO", "Code review: effective sources: AGENTS.md, .github/instructions")],
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
        return sorted(
            (f.path, f.message) for f in audit.audit(self.root).findings
            if f.check == "limitation"
        )

    def test_conforming_fixture_hits_only_the_old_manifest_limitation(self) -> None:
        self.assertEqual(
            [(".github/ai-config-manifest.json",
              "Manifest predates copilot_sections; Copilot parity assumes the default sections")],
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
                (".codex/config.toml",
                 "Only the ownership marker is checked; content drift inside this file is not detected"),
                (".github/instructions", "Path-specific instructions are not checked for contradictions or overlap"),
                (".github/skills/demo/SKILL.md",
                 "Generated Copilot projection is provenance-checked only, not reconstructed"),
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

MCP_SCENARIOS: dict[str, tuple[dict[str, str], dict]] = {
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

LAYERING_SCENARIOS: dict[str, tuple[dict[str, str], dict]] = {
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
    'every_source': [
        ('WARNING', 'mcp', '.mcp.json', "Server 'beta': entry must be an object, got str"),
        ('ERROR', 'mcp', '.mcp.json', "Server 'gamma': must have 'command' (STDIO/local) or 'url' (HTTP/SSE)"),
        ('ERROR', 'mcp', '.github/mcp.json', "Duplicate server name 'alpha' — .mcp.json takes precedence, making .github/mcp.json entry unreachable"),
        ('ERROR', 'mcp', '.codex/config.toml', "Server 'web': env is STDIO-only, must not be present on HTTP transport"),
        ('ERROR', 'mcp', '.codex/config.toml', "Server 'web': env_vars is STDIO-only, must not be present on HTTP transport"),
        ('ERROR', 'mcp', None, "Server 'odd': unknown transport 'carrier'"),
        ('ERROR', 'mcp', None, "Server 'local-only': transport 'local' not supported for target 'claude'"),
        ('ERROR', 'mcp', '.mcp.json', "Server 'alpha': copilot_local.tools allowlist cannot be enforced in shared .mcp.json"),
        ('WARNING', 'mcp', None, 'Copilot repository MCP (cloud agent/code review) configured via repository settings — cannot validate statically'),
        ('WARNING', 'mcp', None, 'Code-review tool set derived from repository allowlist intersected with readOnlyHint: true — cannot verify tool annotations statically'),
        ('WARNING', 'mcp', None, "Server 'alpha': connection fields differ between .mcp.json and .github/mcp.json"),
    ],
    'top_level_lists': [
        ('ERROR', 'mcp', '.mcp.json', 'Top-level value must be an object'),
        ('WARNING', 'mcp', '.github/mcp.json', "'mcpServers' key not found"),
        ('ERROR', 'mcp', '.vscode/mcp.json', 'Top-level value must be an object'),
    ],
    'wrong_wrappers': [
        ('ERROR', 'mcp', '.mcp.json', 'Could not read or parse:'),
        ('ERROR', 'mcp', '.github/mcp.json', "'mcpServers' must be an object"),
        ('ERROR', 'mcp', '.vscode/mcp.json', "VS Code MCP must use 'servers' wrapper (not 'mcpServers')"),
    ],
    'vscode_servers_not_an_object': [
        ('ERROR', 'mcp', '.vscode/mcp.json', "'servers' must be an object"),
        ('ERROR', 'mcp', '.codex/config.toml', 'Invalid TOML:'),
    ],
    'vscode_unparseable': [
        ('ERROR', 'mcp', '.vscode/mcp.json', 'Could not read or parse:'),
    ],
    'nothing': [],
}
LAYERING_EXPECTED: dict[str, list[tuple[str, str, str | None, str]]] = {
    'everything': [
        ('ERROR', 'layering', 'AGENTS.override.md', 'AGENTS.override.md masks the generated AGENTS.md adapter — Codex will not load CLAUDE.md through the adapter'),
        ('WARNING', 'layering', 'sub/AGENTS.override.md', 'Nested AGENTS.override.md takes precedence over AGENTS.md in this subtree — review for conflicting guidance with the root adapter'),
        ('WARNING', 'layering', 'sub/AGENTS.md', 'Nested AGENTS.md adds instructions alongside the root adapter in this subtree — review for conflicting or redundant guidance'),
        ('INFO', 'layering', None, 'Copilot CLI/app: effective sources: CLAUDE.md, AGENTS.md, .github/copilot-instructions.md, GEMINI.md'),
        ('WARNING', 'layering', None, 'Copilot CLI/app folder trust status cannot be determined statically — .mcp.json silently skipped in untrusted directories'),
        ('INFO', 'layering', None, 'JetBrains: .github/copilot-instructions.md available'),
        ('INFO', 'layering', None, 'Cloud agent: AGENTS.md adapter redirects to CLAUDE.md'),
        ('WARNING', 'layering', 'sub/AGENTS.md', 'Nested AGENTS.md supersedes root adapter for cloud agent sessions in this subtree'),
        ('INFO', 'layering', None, 'Code review: effective sources: .github/copilot-instructions.md'),
        ('WARNING', 'layering', None, 'Code-review custom-instructions enablement cannot be verified statically'),
        ('WARNING', 'trust-boundary', None, 'Copilot code review loads instructions, agents, and skills from the PR head; this is advisory context, not a trusted-base or trusted-ref review contract'),
        ('INFO', 'layering', '.github/skills', 'Code review can use relevant .github/skills entries; .claude/skills and .agents/skills are not its documented automatic skill location'),
        ('WARNING', 'runtime', None, 'Copilot repository settings, organization policy, authentication, model availability, runtime enablement, and actual operational use cannot be verified statically'),
        ('WARNING', 'layering', None, 'VS Code instruction settings (chat.useClaudeMdFile, chat.useAgentsMdFile, useInstructionFiles, includeApplyingInstructions) cannot be verified statically'),
    ],
    'redirect_with_nested_redirect': [
        ('INFO', 'layering', None, 'Codex: AGENTS.md adapter redirects to CLAUDE.md'),
        ('INFO', 'layering', None, 'Copilot CLI/app: effective sources: CLAUDE.md, AGENTS.md'),
        ('WARNING', 'layering', None, 'Copilot CLI/app folder trust status cannot be determined statically — .mcp.json silently skipped in untrusted directories'),
        ('ERROR', 'layering', None, 'JetBrains: no .github/copilot-instructions.md or path-specific instructions — JetBrains cannot load CLAUDE.md directly'),
        ('INFO', 'layering', None, 'Cloud agent: AGENTS.md adapter redirects to CLAUDE.md'),
        ('WARNING', 'layering', 'sub/AGENTS.md', 'Nested AGENTS.md supersedes root adapter for cloud agent sessions in this subtree'),
        ('ERROR', 'layering', None, 'Code review: no project instructions it can read — it ignores CLAUDE.md, and AGENTS.md only redirects to CLAUDE.md'),
        ('WARNING', 'layering', None, 'Code-review custom-instructions enablement cannot be verified statically'),
        ('WARNING', 'trust-boundary', None, 'Copilot code review loads instructions, agents, and skills from the PR head; this is advisory context, not a trusted-base or trusted-ref review contract'),
        ('WARNING', 'runtime', None, 'Copilot repository settings, organization policy, authentication, model availability, runtime enablement, and actual operational use cannot be verified statically'),
        ('WARNING', 'layering', None, 'VS Code instruction settings (chat.useClaudeMdFile, chat.useAgentsMdFile, useInstructionFiles, includeApplyingInstructions) cannot be verified statically'),
    ],
    'plain_agents_and_path_instructions': [
        ('WARNING', 'layering', None, 'Codex: non-redirecting AGENTS.md — CLAUDE.md not loaded through adapter'),
        ('INFO', 'layering', None, 'Copilot CLI/app: effective sources: AGENTS.md'),
        ('WARNING', 'layering', None, 'Copilot CLI/app folder trust status cannot be determined statically — .mcp.json silently skipped in untrusted directories'),
        ('INFO', 'layering', None, 'JetBrains: path-specific instructions only (no copilot-instructions.md)'),
        ('INFO', 'layering', None, 'Cloud agent: non-redirecting AGENTS.md — CLAUDE.md not loaded directly'),
        ('INFO', 'layering', None, 'Code review: effective sources: AGENTS.md, .github/instructions'),
        ('WARNING', 'layering', None, 'Code-review custom-instructions enablement cannot be verified statically'),
        ('WARNING', 'trust-boundary', None, 'Copilot code review loads instructions, agents, and skills from the PR head; this is advisory context, not a trusted-base or trusted-ref review contract'),
        ('WARNING', 'runtime', None, 'Copilot repository settings, organization policy, authentication, model availability, runtime enablement, and actual operational use cannot be verified statically'),
        ('WARNING', 'layering', None, 'VS Code instruction settings (chat.useClaudeMdFile, chat.useAgentsMdFile, useInstructionFiles, includeApplyingInstructions) cannot be verified statically'),
    ],
    'claude_only': [
        ('INFO', 'layering', None, 'Codex: no AGENTS.md, CLAUDE.md used via fallback'),
        ('INFO', 'layering', None, 'Copilot CLI/app: effective sources: CLAUDE.md'),
        ('WARNING', 'layering', None, 'Copilot CLI/app folder trust status cannot be determined statically — .mcp.json silently skipped in untrusted directories'),
        ('ERROR', 'layering', None, 'JetBrains: no .github/copilot-instructions.md or path-specific instructions — JetBrains cannot load CLAUDE.md directly'),
        ('INFO', 'layering', None, 'Cloud agent: no AGENTS.md, CLAUDE.md selected directly'),
        ('ERROR', 'layering', None, 'Code review: no project instructions it can read — it ignores CLAUDE.md, and AGENTS.md is absent'),
        ('WARNING', 'layering', None, 'Code-review custom-instructions enablement cannot be verified statically'),
        ('WARNING', 'trust-boundary', None, 'Copilot code review loads instructions, agents, and skills from the PR head; this is advisory context, not a trusted-base or trusted-ref review contract'),
        ('WARNING', 'runtime', None, 'Copilot repository settings, organization policy, authentication, model availability, runtime enablement, and actual operational use cannot be verified statically'),
        ('WARNING', 'layering', None, 'VS Code instruction settings (chat.useClaudeMdFile, chat.useAgentsMdFile, useInstructionFiles, includeApplyingInstructions) cannot be verified statically'),
    ],
    'gemini_only': [
        ('ERROR', 'layering', None, 'Codex: no AGENTS.md or CLAUDE.md — no instructions available'),
        ('INFO', 'layering', None, 'Copilot CLI/app: effective sources: GEMINI.md'),
        ('WARNING', 'layering', None, 'Copilot CLI/app folder trust status cannot be determined statically — .mcp.json silently skipped in untrusted directories'),
        ('ERROR', 'layering', None, 'JetBrains: no .github/copilot-instructions.md or path-specific instructions — JetBrains cannot load CLAUDE.md directly'),
        ('INFO', 'layering', None, 'Cloud agent: no AGENTS.md or CLAUDE.md, GEMINI.md selected as alternative'),
        ('ERROR', 'layering', None, 'Code review: no project instructions it can read — it ignores CLAUDE.md, and AGENTS.md is absent'),
        ('WARNING', 'layering', None, 'Code-review custom-instructions enablement cannot be verified statically'),
        ('WARNING', 'trust-boundary', None, 'Copilot code review loads instructions, agents, and skills from the PR head; this is advisory context, not a trusted-base or trusted-ref review contract'),
        ('WARNING', 'runtime', None, 'Copilot repository settings, organization policy, authentication, model availability, runtime enablement, and actual operational use cannot be verified statically'),
        ('WARNING', 'layering', None, 'VS Code instruction settings (chat.useClaudeMdFile, chat.useAgentsMdFile, useInstructionFiles, includeApplyingInstructions) cannot be verified statically'),
    ],
    'nothing': [
        ('ERROR', 'layering', None, 'Codex: no AGENTS.md or CLAUDE.md — no instructions available'),
        ('ERROR', 'layering', None, 'Copilot CLI/app: no instruction sources available'),
        ('WARNING', 'layering', None, 'Copilot CLI/app folder trust status cannot be determined statically — .mcp.json silently skipped in untrusted directories'),
        ('ERROR', 'layering', None, 'JetBrains: no .github/copilot-instructions.md or path-specific instructions — JetBrains cannot load CLAUDE.md directly'),
        ('ERROR', 'layering', None, 'Cloud agent: no AGENTS.md, CLAUDE.md, or GEMINI.md — no instructions available'),
        ('ERROR', 'layering', None, 'Code review: no project instructions it can read — it ignores CLAUDE.md, and AGENTS.md is absent'),
        ('WARNING', 'layering', None, 'Code-review custom-instructions enablement cannot be verified statically'),
        ('WARNING', 'trust-boundary', None, 'Copilot code review loads instructions, agents, and skills from the PR head; this is advisory context, not a trusted-base or trusted-ref review contract'),
        ('WARNING', 'runtime', None, 'Copilot repository settings, organization policy, authentication, model availability, runtime enablement, and actual operational use cannot be verified statically'),
        ('WARNING', 'layering', None, 'VS Code instruction settings (chat.useClaudeMdFile, chat.useAgentsMdFile, useInstructionFiles, includeApplyingInstructions) cannot be verified statically'),
    ],
    'copilot_app_only': [
        ('INFO', 'layering', None, 'Copilot CLI/app: effective sources: CLAUDE.md'),
        ('WARNING', 'layering', None, 'Copilot CLI/app folder trust status cannot be determined statically — .mcp.json silently skipped in untrusted directories'),
        ('WARNING', 'runtime', None, 'Copilot repository settings, organization policy, authentication, model availability, runtime enablement, and actual operational use cannot be verified statically'),
    ],
    'no_targets': [],
}


class FindingSequenceTests(unittest.TestCase):
    """The exact findings, in order, of the MCP and instruction-layering checks for each branch they take."""

    def _rows(self, check, files: dict[str, str], manifest: dict) -> list[tuple[str, str, str | None, str]]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative, text in files.items():
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_text(text, encoding="utf-8")
            return [
                (f.severity, f.check, f.path, next((p for p in PARSE_PREFIXES if f.message.startswith(p)), f.message))
                for f in check(root, manifest)
            ]

    def test_mcp_findings_and_their_order(self) -> None:
        self.assertEqual(set(MCP_SCENARIOS), set(MCP_EXPECTED))
        for name, (files, manifest) in MCP_SCENARIOS.items():
            with self.subTest(scenario=name):
                self.assertEqual(MCP_EXPECTED[name], self._rows(audit.check_mcp, files, manifest))

    def test_instruction_layering_findings_and_their_order(self) -> None:
        self.assertEqual(set(LAYERING_SCENARIOS), set(LAYERING_EXPECTED))
        for name, (files, manifest) in LAYERING_SCENARIOS.items():
            with self.subTest(scenario=name):
                self.assertEqual(
                    LAYERING_EXPECTED[name], self._rows(audit.check_instruction_layering, files, manifest)
                )


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
            row[0].strip("`"): {target for target, cell in zip(targets, row[1:]) if cell == "Yes"}
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


if __name__ == "__main__":
    unittest.main()

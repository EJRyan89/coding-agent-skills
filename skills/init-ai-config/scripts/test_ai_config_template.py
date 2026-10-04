#!/usr/bin/env python3
"""Fixture tests for the AI-agent configuration generator template.

init-ai-config installs this file as .github/scripts/test_ai_config.py next to the
generator. Run with: python test_ai_config.py
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

# When copied to .github/scripts/test_ai_config.py, change this import to:
#   import ai_config
try:
    import ai_config_template as ai_config
except ModuleNotFoundError:
    import ai_config  # type: ignore[no-redef]

# These tests exercise the generator's logic, so they run against the template's default
# configuration rather than the repository-specific constants an installed copy carries.
TEMPLATE_DEFAULTS: dict[str, object] = {
    "COPILOT_TITLE": "# {repo_name} — AI Coding Instructions",
    "COPILOT_SECTIONS": ["Overview", "Build and Test Commands", "Formatting Rules", "CI / Quality Gates"],
    "COPILOT_REQUIRED_SECTIONS": [
        "Overview", "Build and Test Commands", "Formatting Rules", "CI / Quality Gates",
    ],
    "TARGET_RUNTIMES": ["claude"],
    "TARGET_SURFACES": [],
    "TARGET_FEATURES": [],
    "COPILOT_SETUP_COMMANDS": [],
    "MCP_SERVERS": [],
}
for _name, _value in TEMPLATE_DEFAULTS.items():
    setattr(ai_config, _name, copy.deepcopy(_value))


def _setup_minimal_repo(root: Path) -> None:
    """Create a minimal valid repo structure for testing."""
    (root / ".claude/skills/demo").mkdir(parents=True)
    (root / ".github").mkdir(parents=True)
    (root / "CLAUDE.md").write_text(
        "# CLAUDE.md\n\n"
        "## Overview\n\nA test repository.\n\n"
        "## Build and Test Commands\n\n```bash\nmake test\n```\n\n"
        "## Formatting Rules\n\nUse tabs.\n\n"
        "## CI / Quality Gates\n\n80% coverage required.\n",
        encoding="utf-8",
    )
    (root / ".claude/skills/demo/SKILL.md").write_text(
        "---\nname: demo\n"
        "description: Demonstrate parity checks.\n---\n\n"
        "Canonical workflow.\n",
        encoding="utf-8",
    )


def _commit_generated_manifest(root: Path) -> None:
    """Commit the generated manifest as the fixture's ownership trust anchor."""
    commands = [
        ["git", "init", "--quiet"],
        ["git", "add", ".github/ai-config-manifest.json"],
        [
            "git",
            "-c",
            "user.name=Fixture User",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "--no-gpg-sign",
            "-m",
            "Commit generated manifest",
        ],
    ]
    for command in commands:
        subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            check=True,
            encoding="utf-8",
            errors="strict",
        )
class TemplateParsingTests(unittest.TestCase):
    """Verify templates can be loaded and parsed correctly."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_copilot_instructions_generated(self) -> None:
        content = ai_config.read_text(self.root / "CLAUDE.md")
        result = ai_config.generate_copilot_instructions(content)
        self.assertIn("AI Coding Instructions", result)
        self.assertIn(ai_config.COPILOT_BANNER, result)
        self.assertIn("## Overview", result)

    def test_copilot_instructions_empty_claude_md(self) -> None:
        with self.assertRaises(ValueError):
            ai_config.generate_copilot_instructions("")

    def test_copilot_instructions_no_matching_sections(self) -> None:
        with self.assertRaises(ValueError):
            ai_config.generate_copilot_instructions(
                "# CLAUDE.md\n\n## Unrelated Section\n\nContent.\n"
            )

    def test_copilot_required_sections_enforced_for_jetbrains(self) -> None:
        """#2: Missing required section fails when JetBrains is targeted."""
        content = (
            "# CLAUDE.md\n\n"
            "## Overview\n\nA repo.\n\n"
            "## Build and Test Commands\n\n```bash\nmake test\n```\n"
        )
        original = ai_config.COPILOT_SECTIONS[:]
        ai_config.COPILOT_SECTIONS.clear()
        ai_config.COPILOT_SECTIONS.extend(["Overview", "Build and Test Commands"])
        try:
            with self.assertRaises(ValueError) as ctx:
                ai_config.generate_copilot_instructions(
                    content, surfaces=["jetbrains"]
                )
            self.assertIn("Required sections missing", str(ctx.exception))
        finally:
            ai_config.COPILOT_SECTIONS.clear()
            ai_config.COPILOT_SECTIONS.extend(original)

    def test_copilot_required_sections_not_enforced_for_cli_only(self) -> None:
        """Required sections not enforced when only copilot_cli is targeted."""
        content = (
            "# CLAUDE.md\n\n"
            "## Overview\n\nA repo.\n\n"
            "## Build and Test Commands\n\n```bash\nmake test\n```\n"
        )
        original = ai_config.COPILOT_SECTIONS[:]
        ai_config.COPILOT_SECTIONS.clear()
        ai_config.COPILOT_SECTIONS.extend(["Overview", "Build and Test Commands"])
        try:
            result = ai_config.generate_copilot_instructions(
                content, surfaces=["copilot_cli"]
            )
            self.assertIn("Overview", result)
        finally:
            ai_config.COPILOT_SECTIONS.clear()
            ai_config.COPILOT_SECTIONS.extend(original)

    def test_agents_adapter_generated(self) -> None:
        result = ai_config.generate_agents_adapter("test-repo")
        self.assertIn(ai_config.OWNERSHIP_MARKER, result)
        self.assertIn("CLAUDE.md", result)
        self.assertIn("test-repo", result)

    def test_section_extraction(self) -> None:
        content = ai_config.read_text(self.root / "CLAUDE.md")
        result = ai_config.extract_sections(content, ["Overview"])
        self.assertIn("A test repository", result)
        self.assertNotIn("Build and Test", result)

    def test_section_extraction_multiple(self) -> None:
        content = ai_config.read_text(self.root / "CLAUDE.md")
        result = ai_config.extract_sections(
            content, ["Overview", "Formatting Rules"]
        )
        self.assertIn("A test repository", result)
        self.assertIn("Use tabs", result)

    def test_section_extraction_missing(self) -> None:
        content = ai_config.read_text(self.root / "CLAUDE.md")
        result = ai_config.extract_sections(content, ["Nonexistent"])
        self.assertEqual("", result)


class SkillShimTests(unittest.TestCase):
    """Verify skill shim generation and validation."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_skill_shim_generated(self) -> None:
        skill = self.root / ".claude/skills/demo/SKILL.md"
        name, content = ai_config.expected_skill_shim(skill)
        self.assertEqual("demo", name)
        self.assertIn(ai_config.OWNERSHIP_MARKER, content)
        self.assertIn(".claude/skills/demo/SKILL.md", content)

    def test_frontmatter_missing_start(self) -> None:
        skill = self.root / ".claude/skills/demo/SKILL.md"
        skill.write_text("no frontmatter\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            ai_config.canonical_frontmatter(skill)

    def test_frontmatter_missing_end(self) -> None:
        skill = self.root / ".claude/skills/demo/SKILL.md"
        skill.write_text("---\nname: demo\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            ai_config.canonical_frontmatter(skill)

    def test_block_scalar_description_rejected(self) -> None:
        skill = self.root / ".claude/skills/demo/SKILL.md"
        for indicator in (">-", ">+", "|-", "|+", ">", "|"):
            with self.subTest(indicator=indicator):
                skill.write_text(
                    f"---\nname: demo\ndescription: {indicator}\n"
                    "  Multiline.\n---\n",
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    ai_config.canonical_frontmatter(skill)

    def test_name_directory_mismatch_rejected(self) -> None:
        skill = self.root / ".claude/skills/demo/SKILL.md"
        skill.write_text(
            "---\nname: wrong\ndescription: Test.\n---\n",
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            ai_config.canonical_frontmatter(skill)

    def test_orphan_detection(self) -> None:
        (self.root / ".agents/skills/orphan").mkdir(parents=True)
        (self.root / ".agents/skills/orphan/SKILL.md").write_text(
            "---\nname: orphan\ndescription: Stale.\n---\n\nOrphaned.\n",
            encoding="utf-8",
        )
        orphans = ai_config.orphaned_skill_shims(self.root)
        self.assertEqual(1, len(orphans))
        self.assertEqual("orphan", orphans[0].parent.name)


class DriftDetectionTests(unittest.TestCase):
    """Verify drift and conflict detection."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)
        self._saved_runtimes = ai_config.TARGET_RUNTIMES[:]
        self._saved_surfaces = ai_config.TARGET_SURFACES[:]
        ai_config.TARGET_RUNTIMES[:] = ["claude", "codex"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        self.assertEqual(
            [],
            ai_config.regenerate(self.root),
        )

    def tearDown(self) -> None:
        ai_config.TARGET_RUNTIMES[:] = self._saved_runtimes
        ai_config.TARGET_SURFACES[:] = self._saved_surfaces
        self.temp_directory.cleanup()

    def test_valid_configuration_passes(self) -> None:
        self.assertEqual([], ai_config.validate(self.root))

    def test_copilot_drift_detected(self) -> None:
        (self.root / ".github/copilot-instructions.md").write_text(
            "stale content\n", encoding="utf-8"
        )
        errors = ai_config.validate(self.root)
        self.assertTrue(
            any("DRIFT: .github/copilot-instructions.md" in e for e in errors),
            errors,
        )

    def test_shim_drift_detected(self) -> None:
        shim = self.root / ".agents/skills/demo/SKILL.md"
        shim.write_text(
            shim.read_text(encoding="utf-8").replace(
                "description: Demonstrate parity checks.",
                "description: Changed.",
            ),
            encoding="utf-8",
        )
        errors = ai_config.validate(self.root)
        self.assertTrue(
            any("DRIFT: .agents/skills/demo/SKILL.md" in e for e in errors),
            errors,
        )

    def test_user_authored_agents_md_conflict(self) -> None:
        (self.root / "AGENTS.md").write_text(
            "# My custom AGENTS.md\n\nUser-authored content.\n",
            encoding="utf-8",
        )
        _, errors = ai_config.expected_generated_files(
            self.root, runtimes=["claude", "codex"]
        )
        self.assertTrue(
            any("CONFLICT: AGENTS.md" in e for e in errors), errors
        )

    def test_orphan_shim_detected_in_validation(self) -> None:
        (self.root / ".agents/skills/orphan").mkdir(parents=True)
        (self.root / ".agents/skills/orphan/SKILL.md").write_text(
            f"---\nname: orphan\ndescription: Stale.\n---\n\n"
            f"<!-- {ai_config.OWNERSHIP_MARKER}. -->\n\nOrphaned.\n",
            encoding="utf-8",
        )
        errors = ai_config.validate(self.root)
        self.assertTrue(
            any("ORPHAN: .agents/skills/orphan/SKILL.md" in e for e in errors),
            errors,
        )

    def test_regenerate_reports_orphan_shim_conflict(self) -> None:
        orphan_dir = self.root / ".agents/skills/orphan"
        orphan_dir.mkdir(parents=True)
        orphan = orphan_dir / "SKILL.md"
        orphan.write_text(
            f"---\nname: orphan\ndescription: Stale.\n---\n\n"
            f"<!-- {ai_config.OWNERSHIP_MARKER}. -->\n\nOrphaned.\n",
            encoding="utf-8",
        )
        errors = ai_config.regenerate(
            self.root,
            runtimes=["claude", "codex"],
            surfaces=["copilot_cli"],
        )
        self.assertTrue(orphan.exists())
        self.assertTrue(
            any("CONFLICT" in e and "orphan" in e.lower() for e in errors),
            errors,
        )

    def test_regenerate_preserves_non_generated_orphan(self) -> None:
        orphan_dir = self.root / ".agents/skills/orphan"
        orphan_dir.mkdir(parents=True)
        orphan = orphan_dir / "SKILL.md"
        orphan.write_text(
            "---\nname: orphan\ndescription: User skill.\n---\n\n"
            "User-authored content.\n",
            encoding="utf-8",
        )
        ai_config.regenerate(
            self.root,
            runtimes=["claude", "codex"],
            surfaces=["copilot_cli"],
        )
        self.assertTrue(orphan.exists())


class ManifestTests(unittest.TestCase):
    """Verify manifest generation, round-trips, and safety."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_manifest_generated_on_write(self) -> None:
        ai_config.regenerate(
            self.root,
            runtimes=["claude", "codex"],
            surfaces=["copilot_cli"],
        )
        manifest_path = self.root / ".github/ai-config-manifest.json"
        self.assertTrue(manifest_path.is_file())
        data = json.loads(ai_config.read_text(manifest_path))
        self.assertEqual("ai_config.py", data["generatedBy"])
        self.assertEqual(ai_config.MANIFEST_SCHEMA_VERSION, data["schemaVersion"])
        self.assertEqual("CLAUDE.md", data["canonicalSource"])

    def test_manifest_round_trip(self) -> None:
        ai_config.regenerate(
            self.root,
            runtimes=["claude", "codex"],
            surfaces=["copilot_cli"],
        )
        manifest = ai_config.load_manifest(self.root)
        self.assertIsNotNone(manifest)
        self.assertIn("runtimes", manifest)
        self.assertIn("surfaces", manifest)
        self.assertIn("artifacts", manifest)

    def test_manifest_records_runtimes_and_surfaces(self) -> None:
        ai_config.regenerate(
            self.root,
            runtimes=["claude", "codex"],
            surfaces=["copilot_cli", "vscode"],
            features=["ci_parity"],
        )
        manifest = ai_config.load_manifest(self.root)
        self.assertIn("codex", manifest["runtimes"])
        self.assertIn("copilot_cli", manifest["surfaces"])
        self.assertIn("ci_parity", manifest["features"])

    def test_manifest_path_traversal_rejected(self) -> None:
        error = ai_config.validate_manifest_path("../outside/file.md")
        self.assertIsNotNone(error)
        self.assertIn("path traversal", error)

    def test_manifest_absolute_path_rejected(self) -> None:
        error = ai_config.validate_manifest_path("/etc/passwd")
        self.assertIsNotNone(error)
        self.assertIn("absolute path", error)

    def test_manifest_empty_path_rejected(self) -> None:
        error = ai_config.validate_manifest_path("")
        self.assertIsNotNone(error)

    def test_manifest_valid_path_accepted(self) -> None:
        error = ai_config.validate_manifest_path(
            ".github/copilot-instructions.md"
        )
        self.assertIsNone(error)

    def test_manifest_backslash_traversal_rejected(self) -> None:
        error = ai_config.validate_manifest_path("..\\outside\\file.md")
        self.assertIsNotNone(error)
        self.assertIn("backslash", error)

    def test_manifest_drive_qualified_path_rejected(self) -> None:
        error = ai_config.validate_manifest_path("C:/Windows/System32")
        self.assertIsNotNone(error)
        self.assertIn("drive-qualified", error)

    def test_manifest_unc_path_rejected(self) -> None:
        error = ai_config.validate_manifest_path("//server/share/file")
        self.assertIsNotNone(error)
        self.assertIn("UNC", error)

    def test_manifest_path_outside_allowlist_rejected(self) -> None:
        error = ai_config.validate_manifest_path("src/main.py")
        self.assertIsNotNone(error)
        self.assertIn("allowlist", error)

    def test_manifest_non_object_json(self) -> None:
        """#23: Non-object JSON manifest doesn't crash load_manifest."""
        (self.root / ".github/ai-config-manifest.json").write_text(
            "[]", encoding="utf-8"
        )
        result = ai_config.load_manifest(self.root)
        self.assertIsNone(result)

    def test_json_artifact_hash_mismatch_is_conflict(self) -> None:
        """A JSON artifact whose hash doesn't match manifest is user-modified."""
        # Set up MCP server for JSON generation
        original_servers = ai_config.MCP_SERVERS[:]
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "test-server",
            "targets": ["claude"],
            "transport": "stdio",
            "command": "python",
            "args": ["server.py"],
        })
        try:
            ai_config.regenerate(
                self.root,
                runtimes=["claude"],
                surfaces=[],
            )
            # Modify the .mcp.json after generation
            mcp_path = self.root / ".mcp.json"
            mcp_path.write_text(
                '{"mcpServers": {"test-server": {"command": "modified"}}}\n',
                encoding="utf-8",
            )
            errors = ai_config.validate(self.root)
            self.assertTrue(
                any("CONFLICT" in e or "DRIFT" in e for e in errors), errors
            )
        finally:
            ai_config.MCP_SERVERS.clear()
            ai_config.MCP_SERVERS.extend(original_servers)


class AllowlistAndTitleTests(unittest.TestCase):
    """Verify allowlist fixes and title formatting."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)
        self._orig_servers = ai_config.MCP_SERVERS[:]
        self._orig_runtimes = ai_config.TARGET_RUNTIMES[:]
        self._orig_surfaces = ai_config.TARGET_SURFACES[:]
        self._orig_features = ai_config.TARGET_FEATURES[:]

    def tearDown(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.extend(self._orig_servers)
        ai_config.TARGET_RUNTIMES.clear()
        ai_config.TARGET_RUNTIMES.extend(self._orig_runtimes)
        ai_config.TARGET_SURFACES.clear()
        ai_config.TARGET_SURFACES.extend(self._orig_surfaces)
        ai_config.TARGET_FEATURES.clear()
        ai_config.TARGET_FEATURES.extend(self._orig_features)
        self.temp_directory.cleanup()

    def test_copilot_title_formatted(self) -> None:
        """Title should contain actual repo name, not {repo_name}."""
        ai_config.TARGET_RUNTIMES[:] = ["claude"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        errors = ai_config.regenerate(self.root)
        self.assertEqual([], errors)
        content = ai_config.read_text(
            self.root / ".github/copilot-instructions.md"
        )
        self.assertNotIn("{repo_name}", content)
        self.assertIn(self.root.name, content)

    def test_manifest_with_string_artifact_rejected(self) -> None:
        """Manifest with non-dict artifact entries should be rejected."""
        (self.root / ".github").mkdir(exist_ok=True)
        manifest = {
            "generatedBy": "ai_config.py",
            "schemaVersion": 1,
            "canonicalSource": "CLAUDE.md",
            "runtimes": ["claude"],
            "surfaces": [],
            "features": [],
            "mcp_servers": [],
            "artifacts": ["bad-entry"],
        }
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        loaded = ai_config.load_manifest(self.root)
        self.assertIsNone(loaded)

    def test_github_mcp_json_integrated_round_trip(self) -> None:
        """Copilot-only server generates .github/mcp.json without ValueError."""
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "copilot-only",
            "targets": ["copilot_local"],
            "transport": "stdio",
            "command": "test-cmd",
            "args": [],
        })
        ai_config.TARGET_RUNTIMES[:] = ["claude"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        errors = ai_config.regenerate(self.root)
        self.assertEqual([], errors)
        self.assertTrue(
            (self.root / ".github/mcp.json").is_file()
        )
        errors = ai_config.validate(self.root)
        self.assertEqual([], errors)


class McpValidationTests(unittest.TestCase):
    """Verify MCP transport/target compatibility validation."""

    def setUp(self) -> None:
        self.original_servers = ai_config.MCP_SERVERS[:]

    def tearDown(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.extend(self.original_servers)

    def test_sse_codex_rejected(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "bad-server",
            "targets": ["codex"],
            "transport": "sse",
            "url": "http://localhost:8080/sse",
        })
        errors = ai_config.validate_mcp_servers()
        self.assertTrue(
            any("sse" in e and "codex" in e for e in errors), errors
        )

    def test_local_claude_rejected(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "bad-server",
            "targets": ["claude"],
            "transport": "local",
            "command": "test",
            "args": [],
        })
        errors = ai_config.validate_mcp_servers()
        self.assertTrue(
            any("local" in e and "claude" in e for e in errors), errors
        )

    def test_stdio_universal_accepted(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "good-server",
            "targets": ["claude", "codex", "copilot_local", "vscode"],
            "transport": "stdio",
            "command": "test",
            "args": [],
        })
        errors = ai_config.validate_mcp_servers()
        self.assertEqual([], errors)

    def test_copilot_local_tools_allowlist_rejected(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "restricted-server",
            "targets": ["claude", "copilot_local"],
            "transport": "stdio",
            "command": "test",
            "args": [],
            "copilot_local": {"tools": ["tool_a"]},
        })
        errors = ai_config.validate_mcp_servers()
        self.assertTrue(
            any("copilot_local.tools must be null" in e for e in errors),
            errors,
        )

    def test_oauth_copilot_repository_rejected(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "oauth-server",
            "targets": ["copilot_repository"],
            "transport": "http",
            "url": "http://localhost:8080",
            "oauth": {"client_id": "abc"},
        })
        errors = ai_config.validate_mcp_servers()
        self.assertTrue(
            any("OAuth" in e and "copilot_repository" in e for e in errors),
            errors,
        )

    def test_unknown_transport_rejected(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "bad-server",
            "targets": ["claude"],
            "transport": "grpc",
            "url": "http://localhost:8080",
        })
        errors = ai_config.validate_mcp_servers()
        self.assertTrue(
            any("unknown transport" in e for e in errors), errors
        )


class McpSeparationTests(unittest.TestCase):
    """Verify Claude-only and Copilot-only servers go to separate files."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)
        self.original_servers = ai_config.MCP_SERVERS[:]

    def tearDown(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.extend(self.original_servers)
        self.temp_directory.cleanup()

    def test_claude_only_server_not_in_github_mcp(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "claude-only",
            "targets": ["claude"],
            "transport": "stdio",
            "command": "python",
            "args": ["server.py"],
        })
        mcp = ai_config.generate_mcp_json(self.root)
        github_mcp = ai_config.generate_github_mcp_json(self.root)
        self.assertIn(self.root / ".mcp.json", mcp)
        self.assertEqual({}, github_mcp)

    def test_copilot_only_server_in_github_mcp(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "copilot-only",
            "targets": ["copilot_local"],
            "transport": "stdio",
            "command": "node",
            "args": ["server.js"],
        })
        mcp = ai_config.generate_mcp_json(self.root)
        github_mcp = ai_config.generate_github_mcp_json(self.root)
        self.assertEqual({}, mcp)
        self.assertIn(self.root / ".github/mcp.json", github_mcp)

    def test_shared_server_in_mcp_json(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "shared-server",
            "targets": ["claude", "copilot_local"],
            "transport": "stdio",
            "command": "python",
            "args": ["server.py"],
        })
        mcp = ai_config.generate_mcp_json(self.root)
        github_mcp = ai_config.generate_github_mcp_json(self.root)
        self.assertIn(self.root / ".mcp.json", mcp)
        self.assertEqual({}, github_mcp)


class CodexConfigTomlTests(unittest.TestCase):
    """Verify .codex/config.toml generation."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)
        self.original_servers = ai_config.MCP_SERVERS[:]
        self.original_runtimes = ai_config.TARGET_RUNTIMES[:]
        self.original_surfaces = ai_config.TARGET_SURFACES[:]

    def tearDown(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.extend(self.original_servers)
        ai_config.TARGET_RUNTIMES.clear()
        ai_config.TARGET_RUNTIMES.extend(self.original_runtimes)
        ai_config.TARGET_SURFACES.clear()
        ai_config.TARGET_SURFACES.extend(self.original_surfaces)
        self.temp_directory.cleanup()

    def test_codex_config_toml_stdio_generated(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "my-server",
            "targets": ["codex"],
            "transport": "stdio",
            "command": "python",
            "args": ["server.py"],
        })
        ai_config.TARGET_RUNTIMES.clear()
        ai_config.TARGET_RUNTIMES.extend(["claude", "codex"])
        ai_config.TARGET_SURFACES.clear()
        ai_config.TARGET_SURFACES.extend(["copilot_cli"])
        errors = ai_config.regenerate(self.root)
        self.assertEqual([], errors)
        toml_path = self.root / ".codex/config.toml"
        self.assertTrue(toml_path.is_file())
        content = ai_config.read_text(toml_path)
        self.assertIn("[mcp_servers.my-server]", content)
        self.assertIn('command = "python"', content)
        self.assertIn('args = ["server.py"]', content)

    def test_codex_config_toml_http_no_env(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "http-server",
            "targets": ["codex"],
            "transport": "http",
            "url": "https://example.com/mcp",
            "env": {"KEY": "val"},
        })
        ai_config.TARGET_RUNTIMES.clear()
        ai_config.TARGET_RUNTIMES.extend(["claude", "codex"])
        ai_config.TARGET_SURFACES.clear()
        ai_config.TARGET_SURFACES.extend(["copilot_cli"])
        errors = ai_config.regenerate(self.root)
        self.assertEqual([], errors)
        toml_path = self.root / ".codex/config.toml"
        content = ai_config.read_text(toml_path)
        self.assertIn('url = "https://example.com/mcp"', content)
        self.assertNotIn("env", content.split("[mcp_servers.http-server]")[1])

    def test_codex_config_toml_ownership_marker(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "srv",
            "targets": ["codex"],
            "transport": "stdio",
            "command": "test",
            "args": [],
        })
        ai_config.TARGET_RUNTIMES.clear()
        ai_config.TARGET_RUNTIMES.extend(["claude", "codex"])
        ai_config.TARGET_SURFACES.clear()
        ai_config.TARGET_SURFACES.extend(["copilot_cli"])
        errors = ai_config.regenerate(self.root)
        self.assertEqual([], errors)
        content = ai_config.read_text(self.root / ".codex/config.toml")
        self.assertIn(ai_config.OWNERSHIP_MARKER, content)

    def test_codex_config_toml_round_trip(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "srv",
            "targets": ["claude", "codex", "copilot_local"],
            "transport": "stdio",
            "command": "python",
            "args": ["server.py"],
        })
        ai_config.TARGET_RUNTIMES.clear()
        ai_config.TARGET_RUNTIMES.extend(["claude", "codex"])
        ai_config.TARGET_SURFACES.clear()
        ai_config.TARGET_SURFACES.extend(["copilot_cli"])
        errors = ai_config.regenerate(self.root)
        self.assertEqual([], errors)
        errors = ai_config.validate(self.root)
        self.assertEqual([], errors)


class EndToEndTests(unittest.TestCase):
    """Full regenerate → validate round-trips."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_claude_only_no_surfaces_rejected(self) -> None:
        """Claude-only with no surfaces produces no derived files."""
        errors = ai_config.regenerate(self.root, runtimes=["claude"])
        self.assertTrue(
            any("NO ARTIFACTS" in e for e in errors), errors
        )

    def test_claude_only_with_surface_round_trip(self) -> None:
        errors = ai_config.regenerate(
            self.root, runtimes=["claude"], surfaces=["copilot_cli"]
        )
        self.assertEqual([], errors)
        self.assertEqual(
            [],
            ai_config.validate(
                self.root, runtimes=["claude"], surfaces=["copilot_cli"]
            ),
        )

    def test_claude_codex_copilot_round_trip(self) -> None:
        errors = ai_config.regenerate(
            self.root,
            runtimes=["claude", "codex"],
            surfaces=["copilot_cli"],
        )
        self.assertEqual([], errors)
        self.assertEqual(
            [],
            ai_config.validate(
                self.root,
                runtimes=["claude", "codex"],
                surfaces=["copilot_cli"],
            ),
        )

        # Verify expected files exist
        self.assertTrue(
            (self.root / ".github/copilot-instructions.md").is_file()
        )
        self.assertTrue((self.root / "AGENTS.md").is_file())
        self.assertTrue(
            (self.root / ".agents/skills/demo/SKILL.md").is_file()
        )
        self.assertTrue(
            (self.root / ".github/ai-config-manifest.json").is_file()
        )

    def test_missing_claude_md_errors(self) -> None:
        (self.root / "CLAUDE.md").unlink()
        errors = ai_config.validate(self.root)
        self.assertTrue(
            any("MISSING: CLAUDE.md" in e for e in errors), errors
        )

    def test_idempotent_write(self) -> None:
        ai_config.regenerate(
            self.root,
            runtimes=["claude", "codex"],
            surfaces=["copilot_cli"],
        )
        copilot_before = ai_config.read_text(
            self.root / ".github/copilot-instructions.md"
        )
        ai_config.regenerate(
            self.root,
            runtimes=["claude", "codex"],
            surfaces=["copilot_cli"],
        )
        copilot_after = ai_config.read_text(
            self.root / ".github/copilot-instructions.md"
        )
        self.assertEqual(copilot_before, copilot_after)


class WriteSafetyTests(unittest.TestCase):
    """Verify preflight collision checks and no-artifact rejection."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_user_authored_copilot_instructions_blocked(self) -> None:
        """#15: user-authored file at a generated path blocks regeneration."""
        (self.root / ".github/copilot-instructions.md").write_text(
            "# My custom instructions\n\nUser content.\n", encoding="utf-8"
        )
        errors = ai_config.regenerate(
            self.root,
            runtimes=["claude"],
            surfaces=["copilot_cli"],
        )
        self.assertTrue(
            any("COLLISION" in e for e in errors), errors
        )
        # Verify the file was NOT overwritten
        content = ai_config.read_text(
            self.root / ".github/copilot-instructions.md"
        )
        self.assertIn("User content", content)

    def test_generator_owned_file_overwritten(self) -> None:
        """Generator-owned file (has marker) is safely overwritten."""
        ai_config.regenerate(
            self.root,
            runtimes=["claude"],
            surfaces=["copilot_cli"],
        )
        # Modify the generated file (keeping the marker)
        path = self.root / ".github/copilot-instructions.md"
        original = ai_config.read_text(path)
        self.assertIn(ai_config.OWNERSHIP_MARKER, original)
        path.write_text(original + "\n# Extra line\n", encoding="utf-8")
        # Regenerate should succeed (no collision)
        errors = ai_config.regenerate(
            self.root,
            runtimes=["claude"],
            surfaces=["copilot_cli"],
        )
        self.assertEqual([], errors)

    def test_no_artifacts_rejected(self) -> None:
        """#17: first-run --write with no derived artifacts is rejected."""
        errors = ai_config.regenerate(
            self.root,
            runtimes=["claude"],
            surfaces=[],
        )
        self.assertTrue(
            any("NO ARTIFACTS" in e for e in errors), errors
        )

    def test_manifest_missing_required_fields_rejected(self) -> None:
        """#18: manifest with missing schema fields is treated as absent."""
        ai_config.regenerate(
            self.root,
            runtimes=["claude", "codex"],
            surfaces=["copilot_cli"],
        )
        manifest_path = self.root / ".github/ai-config-manifest.json"
        manifest = json.loads(ai_config.read_text(manifest_path))
        del manifest["runtimes"]
        manifest_path.write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        loaded = ai_config.load_manifest(self.root)
        self.assertIsNone(loaded)

    def test_unowned_manifest_blocked(self) -> None:
        """#15: unowned manifest at target path blocks regeneration."""
        (self.root / ".github").mkdir(parents=True, exist_ok=True)
        (self.root / ".github/ai-config-manifest.json").write_text(
            '{"custom": true}', encoding="utf-8"
        )
        errors = ai_config.regenerate(
            self.root,
            runtimes=["claude", "codex"],
            surfaces=["copilot_cli"],
        )
        self.assertTrue(
            any("COLLISION" in e and "manifest" in e.lower() for e in errors),
            errors,
        )

    def test_forged_manifest_hash_blocked(self) -> None:
        """Forging manifest hash does not authorize a JSON overwrite."""
        original_servers = ai_config.MCP_SERVERS[:]
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "copilot-only",
            "targets": ["copilot_local"],
            "transport": "stdio",
            "command": "test-cmd",
            "args": [],
        })
        try:
            errors = ai_config.regenerate(
                self.root,
                runtimes=["claude"],
                surfaces=["copilot_cli"],
            )
            self.assertEqual([], errors)
            _commit_generated_manifest(self.root)
            mcp_path = self.root / ".github/mcp.json"
            self.assertTrue(mcp_path.is_file())
            # Replace the file with content that lacks the ownership marker
            forged_content = '{"mcpServers": {"injected": {"command": "evil"}}}\n'
            mcp_path.write_text(forged_content, encoding="utf-8")
            # Update the manifest hash to match the forged content
            manifest_path = self.root / ".github/ai-config-manifest.json"
            manifest = json.loads(ai_config.read_text(manifest_path))
            for artifact in manifest["artifacts"]:
                if artifact.get("path") == ".github/mcp.json":
                    artifact["hash"] = ai_config.content_hash(forged_content)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            # Regenerate must not silently overwrite the forged file
            errors = ai_config.regenerate(
                self.root,
                runtimes=["claude"],
                surfaces=["copilot_cli"],
            )
            self.assertTrue(
                any("COLLISION" in e for e in errors),
                f"Forged manifest hash should not bypass collision check: {errors}",
            )
            # Verify the forged content was NOT overwritten
            self.assertEqual(forged_content, ai_config.read_text(mcp_path))
        finally:
            ai_config.MCP_SERVERS.clear()
            ai_config.MCP_SERVERS.extend(original_servers)

    def test_generated_json_owned_by_content_match(self) -> None:
        """JSON files are owned by matching expected content, not text markers."""
        original_servers = ai_config.MCP_SERVERS[:]
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "claude-srv",
            "targets": ["claude"],
            "transport": "stdio",
            "command": "python",
            "args": ["srv.py"],
        })
        try:
            errors = ai_config.regenerate(
                self.root,
                runtimes=["claude"],
                surfaces=["copilot_cli"],
            )
            blocking = [e for e in errors if not e.startswith("WARNING:")]
            self.assertEqual([], blocking)
            mcp = self.root / ".mcp.json"
            self.assertTrue(mcp.is_file())
            content = json.loads(ai_config.read_text(mcp))
            self.assertNotIn("_generator", content)
            self.assertIn("mcpServers", content)
        finally:
            ai_config.MCP_SERVERS.clear()
            ai_config.MCP_SERVERS.extend(original_servers)

    def test_root_not_git_repo_rejected(self) -> None:
        """#9: CLI rejects a root that has no .git marker."""
        original_argv = sys.argv
        try:
            sys.argv = [
                "ai_config.py", "--check",
                "--root", str(self.root),
            ]
            exit_code = ai_config.main()
            self.assertEqual(1, exit_code)
        finally:
            sys.argv = original_argv


class VocabularyTests(unittest.TestCase):
    """Verify vocabulary validation catches invalid identifiers."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_unknown_runtime_rejected(self) -> None:
        _, errors = ai_config.expected_generated_files(
            self.root, runtimes=["claude", "bad_runtime"], surfaces=[]
        )
        self.assertTrue(any("unknown runtime" in e for e in errors), errors)

    def test_unknown_surface_rejected(self) -> None:
        _, errors = ai_config.expected_generated_files(
            self.root, runtimes=["claude"], surfaces=["copilot_local"]
        )
        self.assertTrue(any("unknown surface" in e for e in errors), errors)

    def test_valid_vocabulary_accepted(self) -> None:
        errors = ai_config.regenerate(
            self.root,
            runtimes=["claude", "codex"],
            surfaces=["copilot_cli", "vscode"],
        )
        vocab_errors = [e for e in errors if "unknown" in e.lower()]
        self.assertEqual([], vocab_errors)

    def test_manifest_tampering_does_not_suppress_validation(self) -> None:
        """#18: Emptying manifest surfaces does not suppress drift detection."""
        saved_runtimes = ai_config.TARGET_RUNTIMES[:]
        saved_surfaces = ai_config.TARGET_SURFACES[:]
        ai_config.TARGET_RUNTIMES[:] = ["claude", "codex"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        try:
            ai_config.regenerate(self.root)
            # Tamper: empty the manifest surfaces
            manifest_path = self.root / ".github/ai-config-manifest.json"
            manifest = json.loads(ai_config.read_text(manifest_path))
            manifest["surfaces"] = []
            manifest["runtimes"] = ["claude"]
            manifest["artifacts"] = []
            manifest_path.write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            # Modify a generated file
            (self.root / ".github/copilot-instructions.md").write_text(
                "stale\n", encoding="utf-8"
            )
            # Validate should still detect drift because TARGET constants
            # are the source of truth, not the manifest
            errors = ai_config.validate(self.root)
            self.assertTrue(
                any("DRIFT" in e or "MISSING" in e for e in errors), errors
            )
        finally:
            ai_config.TARGET_RUNTIMES[:] = saved_runtimes
            ai_config.TARGET_SURFACES[:] = saved_surfaces


class TargetConstantsTests(unittest.TestCase):
    """Verify TARGET_* constants as the source of truth for scope."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)
        self._saved_runtimes = ai_config.TARGET_RUNTIMES[:]
        self._saved_surfaces = ai_config.TARGET_SURFACES[:]
        self._saved_features = ai_config.TARGET_FEATURES[:]

    def tearDown(self) -> None:
        ai_config.TARGET_RUNTIMES[:] = self._saved_runtimes
        ai_config.TARGET_SURFACES[:] = self._saved_surfaces
        ai_config.TARGET_FEATURES[:] = self._saved_features
        self.temp_directory.cleanup()

    def test_target_constants_drive_generation(self) -> None:
        """Setting TARGET_RUNTIMES/SURFACES produces artifacts without explicit args."""
        ai_config.TARGET_RUNTIMES[:] = ["claude", "codex"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        errors = ai_config.regenerate(self.root)
        self.assertEqual([], errors)
        self.assertTrue(
            (self.root / ".github/copilot-instructions.md").is_file()
        )
        self.assertTrue((self.root / "AGENTS.md").is_file())

    def test_cli_args_override_target_constants(self) -> None:
        """Explicit runtimes/surfaces passed to regenerate override TARGET_*."""
        ai_config.TARGET_RUNTIMES[:] = ["claude"]
        ai_config.TARGET_SURFACES[:] = []
        errors = ai_config.regenerate(
            self.root,
            runtimes=["claude", "codex"],
            surfaces=["copilot_cli"],
        )
        self.assertEqual([], errors)
        self.assertTrue((self.root / "AGENTS.md").is_file())

    def test_validate_uses_target_constants(self) -> None:
        """Validate uses TARGET_* for scope, not manifest values."""
        ai_config.TARGET_RUNTIMES[:] = ["claude", "codex"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        ai_config.regenerate(self.root)
        # Introduce drift in a copilot artifact
        (self.root / ".github/copilot-instructions.md").write_text(
            "stale content\n", encoding="utf-8"
        )
        # Validate with TARGET_* still set should detect the drift
        errors = ai_config.validate(self.root)
        self.assertTrue(
            any("DRIFT" in e for e in errors),
            "TARGET_* scope should detect copilot drift"
        )
        # Narrowing TARGET_* while manifest retains wider scope triggers
        # STALE CONFIG — this is the correct behavior from #17 fix
        ai_config.TARGET_RUNTIMES[:] = ["claude"]
        ai_config.TARGET_SURFACES[:] = []
        errors_narrow = ai_config.validate(self.root)
        self.assertTrue(
            any("STALE CONFIG" in e for e in errors_narrow),
            "Narrow TARGET_* with wider manifest should detect stale config"
        )

    def test_stale_config_detected(self) -> None:
        """Regenerate with wide scope, then validate with narrow TARGET_*."""
        errors = ai_config.regenerate(
            self.root,
            runtimes=["claude", "codex"],
            surfaces=["copilot_cli"],
        )
        self.assertEqual([], errors)
        # Default TARGET_* is claude-only, no surfaces — manifest is wider
        errors = ai_config.validate(self.root)
        self.assertTrue(
            any("STALE CONFIG" in e for e in errors), errors
        )


class CiParityTests(unittest.TestCase):
    """Verify CI parity workflow generation and caller verification."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)
        self._orig_runtimes = ai_config.TARGET_RUNTIMES[:]
        self._orig_surfaces = ai_config.TARGET_SURFACES[:]
        self._orig_features = ai_config.TARGET_FEATURES[:]
        ai_config.TARGET_RUNTIMES[:] = ["claude", "codex"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        ai_config.TARGET_FEATURES[:] = ["ci_parity"]

    def tearDown(self) -> None:
        ai_config.TARGET_RUNTIMES[:] = self._orig_runtimes
        ai_config.TARGET_SURFACES[:] = self._orig_surfaces
        ai_config.TARGET_FEATURES[:] = self._orig_features
        self.temp_directory.cleanup()

    def test_ci_parity_workflow_generated(self) -> None:
        """Workflow file is written even when no caller exists yet."""
        errors = ai_config.regenerate(self.root)
        # regenerate calls validate, which reports NO CALLER — that's expected
        no_caller = [e for e in errors if "NO CALLER" in e]
        other = [e for e in errors if "NO CALLER" not in e]
        self.assertTrue(len(no_caller) > 0)
        self.assertEqual([], other)
        wf_path = self.root / ai_config.CI_PARITY_WORKFLOW_PATH
        self.assertTrue(wf_path.is_file())
        content = ai_config.read_text(wf_path)
        self.assertIn(ai_config.OWNERSHIP_MARKER, content)
        self.assertIn("workflow_call", content)
        self.assertIn("ai_config.py --check", content)

    def test_ci_parity_no_caller_error(self) -> None:
        """Validation without a caller workflow reports NO CALLER."""
        # Create a caller first so regenerate succeeds cleanly, then remove it
        caller_dir = self.root / ".github/workflows"
        caller_dir.mkdir(parents=True, exist_ok=True)
        caller = caller_dir / "ci.yml"
        caller.write_text(
            "name: CI\non: [push]\njobs:\n"
            "  parity:\n"
            "    uses: ./.github/workflows/ai-config-parity.yml\n",
            encoding="utf-8",
        )
        errors = ai_config.regenerate(self.root)
        self.assertEqual([], errors)
        caller.unlink()
        val_errors = ai_config.validate(self.root)
        self.assertTrue(
            any("NO CALLER" in e for e in val_errors), val_errors
        )

    def test_ci_parity_caller_feature_generates_a_reachable_caller(self) -> None:
        ai_config.TARGET_FEATURES[:] = ["ci_parity", "ci_parity_caller"]
        self.assertEqual([], ai_config.regenerate(self.root))
        caller = self.root / ai_config.CI_PARITY_CALLER_PATH
        content = ai_config.read_text(caller)
        self.assertIn(ai_config.OWNERSHIP_MARKER, content)
        self.assertIn("  pull_request:\n", content)
        self.assertIn("    uses: ./.github/workflows/ai-config-parity.yml\n", content)
        self.assertIn("  contents: read\n", content)
        self.assertEqual(
            [ai_config.CI_PARITY_CALLER_PATH],
            ai_config.find_ci_parity_callers(self.root, ai_config.CI_PARITY_WORKFLOW_PATH),
        )
        manifest = json.loads(ai_config.read_text(self.root / ".github/ai-config-manifest.json"))
        self.assertIn(
            {"path": ai_config.CI_PARITY_CALLER_PATH}, manifest["artifacts"]
        )
        self.assertEqual([], ai_config.validate(self.root))

    def test_ci_parity_caller_requires_ci_parity(self) -> None:
        ai_config.TARGET_FEATURES[:] = ["ci_parity_caller"]
        self.assertIn(
            "TARGET_FEATURES: ci_parity_caller requires ci_parity",
            ai_config.validate_config(self.root),
        )
        self.assertIn(
            "INVALID: ci_parity_caller requires the ci_parity feature",
            ai_config.regenerate(self.root),
        )
        self.assertFalse((self.root / ai_config.CI_PARITY_CALLER_PATH).exists())

    def test_dropping_ci_features_removes_unmodified_workflows(self) -> None:
        (self.root / ".git").mkdir(exist_ok=True)
        ai_config.TARGET_FEATURES[:] = ["ci_parity", "ci_parity_caller"]
        self.assertEqual([], ai_config.regenerate(self.root))
        _commit_generated_manifest(self.root)
        ai_config.TARGET_FEATURES[:] = []
        self.assertEqual([], ai_config.regenerate(self.root))
        self.assertFalse((self.root / ai_config.CI_PARITY_WORKFLOW_PATH).exists())
        self.assertFalse((self.root / ai_config.CI_PARITY_CALLER_PATH).exists())

    def test_orphaned_caller_reported_when_feature_dropped(self) -> None:
        caller = self.root / ai_config.CI_PARITY_CALLER_PATH
        caller.parent.mkdir(parents=True, exist_ok=True)
        caller.write_text(ai_config.generate_ci_parity_caller_workflow(), encoding="utf-8")
        errors = ai_config.regenerate(self.root)
        self.assertEqual(
            [f"ORPHAN: {ai_config.CI_PARITY_CALLER_PATH} — ci_parity_caller not in TARGET_FEATURES"],
            errors,
        )
        self.assertTrue(caller.is_file())

    def test_ci_parity_with_caller_passes(self) -> None:
        caller_dir = self.root / ".github/workflows"
        caller_dir.mkdir(parents=True, exist_ok=True)
        (caller_dir / "ci.yml").write_text(
            "name: CI\non: [push]\njobs:\n"
            "  parity:\n"
            "    uses: ./.github/workflows/ai-config-parity.yml\n",
            encoding="utf-8",
        )
        errors = ai_config.regenerate(self.root)
        self.assertEqual([], errors)
        val_errors = ai_config.validate(self.root)
        self.assertEqual([], val_errors)


class ValidateConfigTests(unittest.TestCase):
    """Verify --validate-config mode."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)
        self._saved_runtimes = ai_config.TARGET_RUNTIMES[:]
        self._saved_surfaces = ai_config.TARGET_SURFACES[:]
        self._saved_sections = ai_config.COPILOT_SECTIONS[:]
        self._saved_req_sections = ai_config.COPILOT_REQUIRED_SECTIONS[:]
        self._saved_title = ai_config.COPILOT_TITLE

    def tearDown(self) -> None:
        ai_config.TARGET_RUNTIMES[:] = self._saved_runtimes
        ai_config.TARGET_SURFACES[:] = self._saved_surfaces
        ai_config.COPILOT_SECTIONS[:] = self._saved_sections
        ai_config.COPILOT_REQUIRED_SECTIONS[:] = self._saved_req_sections
        ai_config.COPILOT_TITLE = self._saved_title
        self.temp_directory.cleanup()

    def test_validate_config_passes(self) -> None:
        ai_config.TARGET_RUNTIMES[:] = ["claude", "codex"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        errors = ai_config.validate_config(self.root)
        self.assertEqual([], errors)

    def test_validate_config_bad_runtime(self) -> None:
        ai_config.TARGET_RUNTIMES[:] = ["claude", "bad_runtime"]
        errors = ai_config.validate_config(self.root)
        self.assertTrue(
            any("TARGET_RUNTIMES" in e and "bad_runtime" in e for e in errors),
            errors,
        )

    def test_validate_config_missing_section(self) -> None:
        ai_config.COPILOT_SECTIONS[:] = [
            "Overview",
            "Build and Test Commands",
            "Nonexistent Section",
        ]
        errors = ai_config.validate_config(self.root)
        self.assertTrue(
            any("COPILOT_SECTIONS" in e and "Nonexistent" in e for e in errors),
            errors,
        )

    def test_validate_config_static_title_accepted(self) -> None:
        """A static title (no placeholder) is valid — it's a customized title."""
        ai_config.COPILOT_TITLE = "# My Repo — Instructions"
        errors = ai_config.validate_config(self.root)
        self.assertFalse(any("COPILOT_TITLE" in e for e in errors), errors)

    def test_validate_config_malformed_title_rejected(self) -> None:
        """A title with invalid format placeholders is rejected."""
        ai_config.COPILOT_TITLE = "# {unknown_key} — Instructions"
        errors = ai_config.validate_config(self.root)
        self.assertTrue(
            any("COPILOT_TITLE" in e for e in errors), errors
        )

    def test_validate_config_required_not_in_configured(self) -> None:
        ai_config.COPILOT_REQUIRED_SECTIONS[:] = ["Missing From Config"]
        errors = ai_config.validate_config(self.root)
        self.assertTrue(
            any("COPILOT_REQUIRED_SECTIONS" in e for e in errors), errors
        )


class McpIsolationAndTomlSafetyTests(unittest.TestCase):
    """Verify MCP isolation policy and TOML key safety."""

    def setUp(self) -> None:
        self.original_servers = ai_config.MCP_SERVERS[:]
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)

    def tearDown(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.extend(self.original_servers)
        self.temp_directory.cleanup()

    def test_toml_dotted_server_name_quoted(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "vendor.server",
            "targets": ["codex"],
            "transport": "stdio",
            "command": "python",
            "args": ["s.py"],
        })
        outputs = ai_config.generate_codex_config_toml(self.root)
        self.assertEqual(1, len(outputs))
        content = list(outputs.values())[0]
        self.assertIn('[mcp_servers."vendor.server"]', content)
        self.assertNotIn("[mcp_servers.vendor.server]", content)

    def test_toml_quote_escapes_control_chars(self) -> None:
        self.assertIn("\\n", ai_config._toml_quote("line\nbreak"))
        self.assertIn("\\t", ai_config._toml_quote("has\ttab"))

    def test_copilot_local_tools_allowed_in_github_mcp(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "cop-only",
            "targets": ["copilot_local"],
            "transport": "stdio",
            "command": "python",
            "args": ["s.py"],
            "copilot_local": {"tools": ["tool_a"]},
        })
        errors = ai_config.validate_mcp_servers()
        self.assertFalse(
            any("copilot_local.tools" in e for e in errors), errors
        )

    def test_claude_only_server_copilot_warning(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "claude-srv",
            "targets": ["claude"],
            "transport": "stdio",
            "command": "python",
            "args": ["s.py"],
        })
        errors = ai_config.validate_mcp_servers(surfaces=["copilot_cli"])
        self.assertTrue(
            any("WARNING" in e and "claude-srv" in e for e in errors), errors
        )

    def test_stdio_server_requires_command(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "no-cmd",
            "targets": ["claude"],
            "transport": "stdio",
        })
        errors = ai_config.validate_mcp_servers()
        self.assertTrue(
            any("requires 'command'" in e for e in errors), errors
        )


class CiWorkflowHardeningTests(unittest.TestCase):
    """Verify CI workflow SHA pins and caller detection accuracy."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)
        self._orig_runtimes = ai_config.TARGET_RUNTIMES[:]
        self._orig_surfaces = ai_config.TARGET_SURFACES[:]
        self._orig_features = ai_config.TARGET_FEATURES[:]

    def tearDown(self) -> None:
        ai_config.TARGET_RUNTIMES[:] = self._orig_runtimes
        ai_config.TARGET_SURFACES[:] = self._orig_surfaces
        ai_config.TARGET_FEATURES[:] = self._orig_features
        self.temp_directory.cleanup()

    def test_ci_workflow_sha_pinned(self) -> None:
        content = ai_config.generate_ci_parity_workflow()
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", content)
        self.assertIn("actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97", content)
        self.assertNotIn("actions/checkout@v4\n", content)
        self.assertNotIn("actions/setup-python@v5\n", content)

    def test_ci_caller_comment_not_counted(self) -> None:
        wf_dir = self.root / ".github/workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "ci.yml").write_text(
            "name: CI\n"
            "# This references ai-config-parity.yml for context\n"
            "on: push\n"
            "jobs:\n"
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: echo test\n",
            encoding="utf-8",
        )
        callers = ai_config.find_ci_parity_callers(
            self.root, ai_config.CI_PARITY_WORKFLOW_PATH
        )
        self.assertEqual([], callers)

    def test_ci_caller_yaml_extension(self) -> None:
        wf_dir = self.root / ".github/workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "ci.yaml").write_text(
            "name: CI\n"
            "on: push\n"
            "jobs:\n"
            "  parity:\n"
            "    uses: ./.github/workflows/ai-config-parity.yml\n",
            encoding="utf-8",
        )
        callers = ai_config.find_ci_parity_callers(
            self.root, ai_config.CI_PARITY_WORKFLOW_PATH
        )
        self.assertEqual(1, len(callers))


class CopilotSetupWorkflowTests(unittest.TestCase):
    """Verify deterministic cloud-agent setup generation."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)
        self._orig_commands = ai_config.COPILOT_SETUP_COMMANDS[:]
        self._orig_sections = ai_config.COPILOT_SECTIONS[:]

    def tearDown(self) -> None:
        ai_config.COPILOT_SETUP_COMMANDS[:] = self._orig_commands
        ai_config.COPILOT_SECTIONS[:] = self._orig_sections
        self.temp_directory.cleanup()

    def test_cloud_agent_workflow_is_generated_and_sha_pinned(self) -> None:
        ai_config.COPILOT_SETUP_COMMANDS[:] = [
            {"name": "Restore dependencies", "run": "dotnet restore\nnpm ci"}
        ]
        outputs, errors = ai_config.expected_generated_files(
            self.root,
            runtimes=["claude"],
            surfaces=["cloud_agent"],
            features=[],
        )
        self.assertEqual([], errors)
        workflow = outputs[self.root / ai_config.COPILOT_SETUP_WORKFLOW_PATH]
        self.assertIn(
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            workflow,
        )
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn('name: "Restore dependencies"', workflow)
        self.assertIn("          dotnet restore\n          npm ci", workflow)
        self.assertNotIn("# Customize:", workflow)

    def test_manifest_records_custom_copilot_sections(self) -> None:
        ai_config.COPILOT_SECTIONS[:] = ["Overview"]
        outputs, errors = ai_config.expected_generated_files(
            self.root,
            runtimes=["claude"],
            surfaces=["copilot_cli"],
            features=[],
        )
        self.assertEqual([], errors)
        manifest = json.loads(
            outputs[self.root / ".github/ai-config-manifest.json"]
        )
        self.assertEqual(["Overview"], manifest["copilot_sections"])

    def test_invalid_cloud_agent_commands_fail_closed(self) -> None:
        ai_config.COPILOT_SETUP_COMMANDS[:] = [
            {"name": "Missing command"}  # type: ignore[list-item]
        ]
        errors = ai_config.validate_config(self.root)
        self.assertTrue(
            any("must contain exactly name and run" in error for error in errors),
            errors,
        )


class FeatureValidationTests(unittest.TestCase):
    """Verify feature vocabulary validation and stale/orphan detection."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)
        self._orig_runtimes = ai_config.TARGET_RUNTIMES[:]
        self._orig_surfaces = ai_config.TARGET_SURFACES[:]
        self._orig_features = ai_config.TARGET_FEATURES[:]
        self._orig_servers = ai_config.MCP_SERVERS[:]

    def tearDown(self) -> None:
        ai_config.TARGET_RUNTIMES[:] = self._orig_runtimes
        ai_config.TARGET_SURFACES[:] = self._orig_surfaces
        ai_config.TARGET_FEATURES[:] = self._orig_features
        ai_config.MCP_SERVERS[:] = self._orig_servers
        self.temp_directory.cleanup()

    def test_unknown_feature_rejected(self) -> None:
        ai_config.TARGET_FEATURES[:] = ["bad_feature"]
        errors = ai_config.validate_config(self.root)
        self.assertTrue(
            any("TARGET_FEATURES" in e and "bad_feature" in e for e in errors),
            errors,
        )

    def test_stale_feature_detected(self) -> None:
        """Regenerate with ci_parity, then validate without — expect stale."""
        ai_config.TARGET_RUNTIMES[:] = ["claude", "codex"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        ai_config.TARGET_FEATURES[:] = ["ci_parity"]
        # Create a caller so regenerate fully succeeds
        wf_dir = self.root / ".github/workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "ci.yml").write_text(
            "name: CI\non: push\njobs:\n  p:\n"
            "    uses: ./.github/workflows/ai-config-parity.yml\n",
            encoding="utf-8",
        )
        errors = ai_config.regenerate(self.root)
        self.assertEqual([], errors)
        # Now validate with features=[] (no ci_parity)
        ai_config.TARGET_FEATURES[:] = []
        errors = ai_config.validate(self.root)
        self.assertTrue(
            any("STALE CONFIG" in e or "ORPHAN" in e for e in errors), errors
        )


class WarningPipelineTests(unittest.TestCase):
    """Verify MCP warnings don't block generation or validation."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)
        self._orig_servers = ai_config.MCP_SERVERS[:]
        self._orig_runtimes = ai_config.TARGET_RUNTIMES[:]
        self._orig_surfaces = ai_config.TARGET_SURFACES[:]
        self._orig_features = ai_config.TARGET_FEATURES[:]

    def tearDown(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.extend(self._orig_servers)
        ai_config.TARGET_RUNTIMES[:] = self._orig_runtimes
        ai_config.TARGET_SURFACES[:] = self._orig_surfaces
        ai_config.TARGET_FEATURES[:] = self._orig_features
        self.temp_directory.cleanup()

    def test_warning_does_not_block_check(self) -> None:
        """Claude-only MCP server with copilot_cli surface produces warning, not error."""
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "claude-only",
            "targets": ["claude"],
            "transport": "stdio",
            "command": "python",
            "args": ["server.py"],
        })
        ai_config.TARGET_RUNTIMES[:] = ["claude"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        errors = ai_config.regenerate(self.root)
        blocking = [e for e in errors if not e.startswith("WARNING:")]
        self.assertEqual([], blocking)

    def test_copilot_tools_policy_emitted(self) -> None:
        """Copilot-only server with tools policy has it in generated JSON."""
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "copilot-srv",
            "targets": ["copilot_local"],
            "transport": "stdio",
            "command": "python",
            "args": ["server.py"],
            "copilot_local": {"tools": ["read_file", "search"]},
        })
        ai_config.TARGET_RUNTIMES[:] = ["claude"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        errors = ai_config.regenerate(self.root)
        blocking = [e for e in errors if not e.startswith("WARNING:")]
        self.assertEqual([], blocking)
        github_mcp = self.root / ".github/mcp.json"
        self.assertTrue(github_mcp.is_file())
        data = json.loads(ai_config.read_text(github_mcp))
        srv = data["mcpServers"]["copilot-srv"]
        self.assertEqual(["read_file", "search"], srv["tools"])

    def test_malformed_mcp_server_entry(self) -> None:
        """Non-dict MCP_SERVERS entry produces error, not crash."""
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append("not-a-dict")
        errors = ai_config.validate_mcp_servers()
        self.assertTrue(
            any("not a dictionary" in e for e in errors), errors
        )

    def test_duplicate_server_name_rejected(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.extend([
            {"name": "srv", "targets": ["claude"], "transport": "stdio",
             "command": "a"},
            {"name": "srv", "targets": ["claude"], "transport": "stdio",
             "command": "b"},
        ])
        errors = ai_config.validate_mcp_servers()
        self.assertTrue(
            any("duplicate" in e.lower() for e in errors), errors
        )

    def test_schema_version_mismatch_rejected(self) -> None:
        """Manifest with wrong schemaVersion is treated as absent."""
        ai_config.TARGET_RUNTIMES[:] = ["claude", "codex"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        ai_config.regenerate(self.root)
        manifest_path = self.root / ".github/ai-config-manifest.json"
        manifest = json.loads(ai_config.read_text(manifest_path))
        manifest["schemaVersion"] = 999
        manifest_path.write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        loaded = ai_config.load_manifest(self.root)
        self.assertIsNone(loaded)


class ScopeAndCallerTests(unittest.TestCase):
    """Verify scope narrowing cleanup, CI caller detection, and MCP target validation."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)
        (self.root / ".git").mkdir(exist_ok=True)
        self._orig_servers = ai_config.MCP_SERVERS[:]
        self._orig_runtimes = ai_config.TARGET_RUNTIMES[:]
        self._orig_surfaces = ai_config.TARGET_SURFACES[:]
        self._orig_features = ai_config.TARGET_FEATURES[:]

    def tearDown(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.extend(self._orig_servers)
        ai_config.TARGET_RUNTIMES[:] = self._orig_runtimes
        ai_config.TARGET_SURFACES[:] = self._orig_surfaces
        ai_config.TARGET_FEATURES[:] = self._orig_features
        self.temp_directory.cleanup()

    def test_scope_narrowing_cleans_artifacts(self) -> None:
        """Narrowing from codex to claude-only removes AGENTS.md and shims."""
        ai_config.TARGET_RUNTIMES[:] = ["claude", "codex"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        errors = ai_config.regenerate(self.root)
        blocking = [e for e in errors if not e.startswith("WARNING:")]
        self.assertEqual([], blocking)
        self.assertTrue((self.root / "AGENTS.md").is_file())
        self.assertTrue(
            (self.root / ".agents/skills/demo/SKILL.md").is_file()
        )
        _commit_generated_manifest(self.root)

        ai_config.TARGET_RUNTIMES[:] = ["claude"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        errors = ai_config.regenerate(self.root)
        blocking = [e for e in errors if not e.startswith("WARNING:")]
        self.assertEqual([], blocking)
        self.assertFalse(
            (self.root / "AGENTS.md").is_file(),
            "AGENTS.md should be removed when codex is dropped",
        )
        self.assertFalse(
            (self.root / ".agents/skills/demo/SKILL.md").is_file(),
            "Skill shim should be removed when codex is dropped",
        )

    def test_ci_step_level_uses_not_counted(self) -> None:
        """Step-level '- uses:' should not count as a caller."""
        wf_dir = self.root / ".github/workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "ci.yml").write_text(
            "name: CI\non:\n  push:\njobs:\n  build:\n"
            "    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: ./.github/workflows/ai-config-parity.yml\n",
            encoding="utf-8",
        )
        callers = ai_config.find_ci_parity_callers(
            self.root, ai_config.CI_PARITY_WORKFLOW_PATH
        )
        self.assertEqual([], callers)

    def test_ci_job_level_uses_counted(self) -> None:
        """Job-level 'uses:' should count as a caller."""
        wf_dir = self.root / ".github/workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "ci.yml").write_text(
            "name: CI\non:\n  pull_request:\njobs:\n  parity:\n"
            "    uses: ./.github/workflows/ai-config-parity.yml\n",
            encoding="utf-8",
        )
        callers = ai_config.find_ci_parity_callers(
            self.root, ai_config.CI_PARITY_WORKFLOW_PATH
        )
        self.assertEqual(1, len(callers))

    def test_mcp_target_scope_mismatch_warning(self) -> None:
        """Server targeting codex without codex in runtimes produces warning."""
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "srv",
            "targets": ["codex"],
            "transport": "stdio",
            "command": "python",
        })
        errors = ai_config.validate_mcp_servers(
            runtimes=["claude"], surfaces=[]
        )
        self.assertTrue(
            any("codex" in e and "TARGET_RUNTIMES" in e for e in errors),
            errors,
        )


class DefensiveValidationTests(unittest.TestCase):
    """Regression tests for validation hardening and cleanup safety."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)
        (self.root / ".git").mkdir(exist_ok=True)
        self._orig_servers = ai_config.MCP_SERVERS[:]
        self._orig_runtimes = ai_config.TARGET_RUNTIMES[:]
        self._orig_surfaces = ai_config.TARGET_SURFACES[:]
        self._orig_features = ai_config.TARGET_FEATURES[:]

    def tearDown(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.extend(self._orig_servers)
        ai_config.TARGET_RUNTIMES[:] = self._orig_runtimes
        ai_config.TARGET_SURFACES[:] = self._orig_surfaces
        ai_config.TARGET_FEATURES[:] = self._orig_features
        self.temp_directory.cleanup()

    def test_manifest_tampering_detected_by_check(self) -> None:
        """Narrowing manifest scope is detected by --check."""
        ai_config.TARGET_RUNTIMES[:] = ["claude", "codex"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        errors = ai_config.regenerate(self.root)
        blocking = [e for e in errors if not e.startswith("WARNING:")]
        self.assertEqual([], blocking)

        manifest_path = self.root / ".github/ai-config-manifest.json"
        manifest = json.loads(ai_config.read_text(manifest_path))
        manifest["runtimes"] = ["claude"]
        manifest["surfaces"] = []
        manifest["artifacts"] = []
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        errors = ai_config.validate(self.root)
        self.assertTrue(
            any("DRIFT" in e and "manifest" in e.lower() for e in errors),
            f"Manifest tampering should be detected: {errors}",
        )

    def test_unsafe_manifest_path_rejects_load(self) -> None:
        """Manifest with traversal path in artifacts rejects load."""
        ai_config.TARGET_RUNTIMES[:] = ["claude", "codex"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        ai_config.regenerate(self.root)

        manifest_path = self.root / ".github/ai-config-manifest.json"
        manifest = json.loads(ai_config.read_text(manifest_path))
        manifest["artifacts"].append({"path": "../outside.json", "hash": "abc"})
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        loaded = ai_config.load_manifest(self.root)
        self.assertIsNone(loaded)

    def test_cleanup_modified_non_json_reports_conflict(self) -> None:
        """Modified AGENTS.md with marker retained causes CONFLICT on narrowing."""
        ai_config.TARGET_RUNTIMES[:] = ["claude", "codex"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        errors = ai_config.regenerate(self.root)
        blocking = [e for e in errors if not e.startswith("WARNING:")]
        self.assertEqual([], blocking)
        _commit_generated_manifest(self.root)

        agents = self.root / "AGENTS.md"
        original = ai_config.read_text(agents)
        agents.write_text(original + "\n# User addition\n", encoding="utf-8")

        ai_config.TARGET_RUNTIMES[:] = ["claude"]
        errors = ai_config.regenerate(self.root)
        self.assertTrue(
            any("CONFLICT" in e and "AGENTS.md" in e for e in errors),
            f"Modified AGENTS.md should produce CONFLICT: {errors}",
        )
        self.assertTrue(agents.is_file(), "Modified file should NOT be deleted")

    def test_cleanup_modified_json_reports_conflict(self) -> None:
        """Modified .mcp.json causes CONFLICT on scope narrowing."""
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "srv",
            "targets": ["claude"],
            "transport": "stdio",
            "command": "python",
            "args": ["server.py"],
        })
        ai_config.TARGET_RUNTIMES[:] = ["claude"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        errors = ai_config.regenerate(self.root)
        blocking = [e for e in errors if not e.startswith("WARNING:")]
        self.assertEqual([], blocking)
        _commit_generated_manifest(self.root)

        mcp = self.root / ".mcp.json"
        mcp.write_text('{"mcpServers": {"srv": {"command": "modified"}}}\n',
                        encoding="utf-8")

        ai_config.MCP_SERVERS.clear()
        errors = ai_config.regenerate(self.root)
        self.assertTrue(
            any("CONFLICT" in e and ".mcp.json" in e for e in errors),
            f"Modified .mcp.json should produce CONFLICT: {errors}",
        )
        self.assertTrue(mcp.is_file(), "Modified file should NOT be deleted")

    def test_malformed_mcp_through_regenerate(self) -> None:
        """Malformed MCP entries produce errors through regenerate, not crashes."""
        ai_config.TARGET_RUNTIMES[:] = ["claude", "codex"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]

        test_cases = [
            ("missing name", {"targets": ["claude"], "transport": "stdio",
                              "command": "x"}),
            ("non-string transport", {"name": "srv", "targets": ["claude"],
                                      "transport": 123, "command": "x"}),
            ("non-list targets", {"name": "srv", "targets": "claude",
                                  "transport": "stdio", "command": "x"}),
            ("non-string target element", {"name": "srv", "targets": [{}],
                                           "transport": "stdio", "command": "x"}),
        ]
        for desc, server_def in test_cases:
            with self.subTest(case=desc):
                ai_config.MCP_SERVERS.clear()
                ai_config.MCP_SERVERS.append(server_def)
                try:
                    errors = ai_config.regenerate(self.root)
                    self.assertTrue(
                        any("MCP" in e for e in errors),
                        f"{desc}: expected MCP error, got {errors}",
                    )
                except Exception as exc:
                    self.fail(f"{desc}: crashed with {type(exc).__name__}: {exc}")

    def test_nested_shim_path_rejected(self) -> None:
        """.agents/skills/a/b/SKILL.md is rejected by path validation."""
        error = ai_config.validate_manifest_path(
            ".agents/skills/a/b/SKILL.md"
        )
        self.assertIsNotNone(error)
        self.assertIn("one skill-name segment", error)

    def test_zero_artifact_via_validate(self) -> None:
        """Claude-only with no surfaces fails --check with NO ARTIFACTS."""
        ai_config.TARGET_RUNTIMES[:] = ["claude"]
        ai_config.TARGET_SURFACES[:] = []
        ai_config.TARGET_FEATURES[:] = []
        ai_config.MCP_SERVERS.clear()
        errors = ai_config.validate(self.root)
        self.assertTrue(
            any("NO ARTIFACTS" in e for e in errors),
            f"Zero-artifact config should fail validate: {errors}",
        )

    def test_zero_artifact_via_validate_config(self) -> None:
        """Claude-only with no surfaces fails --validate-config."""
        ai_config.TARGET_RUNTIMES[:] = ["claude"]
        ai_config.TARGET_SURFACES[:] = []
        ai_config.TARGET_FEATURES[:] = []
        ai_config.MCP_SERVERS.clear()
        errors = ai_config.validate_config(self.root)
        self.assertTrue(
            any("NO ARTIFACTS" in e for e in errors),
            f"Zero-artifact config should fail validate_config: {errors}",
        )


class NoneConstantTests(unittest.TestCase):
    """Verify None and malformed constants produce errors, not crashes."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)
        (self.root / ".git").mkdir(exist_ok=True)
        self._orig_runtimes = ai_config.TARGET_RUNTIMES
        self._orig_surfaces = ai_config.TARGET_SURFACES
        self._orig_features = ai_config.TARGET_FEATURES
        self._orig_required = ai_config.COPILOT_REQUIRED_SECTIONS[:]

    def tearDown(self) -> None:
        ai_config.TARGET_RUNTIMES = self._orig_runtimes
        ai_config.TARGET_SURFACES = self._orig_surfaces
        ai_config.TARGET_FEATURES = self._orig_features
        ai_config.COPILOT_REQUIRED_SECTIONS = list(self._orig_required)
        self.temp_directory.cleanup()

    def test_none_runtimes_validate(self) -> None:
        ai_config.TARGET_RUNTIMES = None
        errors = ai_config.validate(self.root)
        self.assertTrue(
            any("CONFIG TYPE" in e and "TARGET_RUNTIMES" in e for e in errors),
            errors,
        )

    def test_none_runtimes_regenerate(self) -> None:
        ai_config.TARGET_RUNTIMES = None
        errors = ai_config.regenerate(self.root)
        self.assertTrue(
            any("CONFIG TYPE" in e and "TARGET_RUNTIMES" in e for e in errors),
            errors,
        )

    def test_none_surfaces_validate(self) -> None:
        ai_config.TARGET_SURFACES = None
        errors = ai_config.validate(self.root)
        self.assertTrue(
            any("CONFIG TYPE" in e and "TARGET_SURFACES" in e for e in errors),
            errors,
        )

    def test_none_features_regenerate(self) -> None:
        ai_config.TARGET_FEATURES = None
        errors = ai_config.regenerate(self.root)
        self.assertTrue(
            any("CONFIG TYPE" in e and "TARGET_FEATURES" in e for e in errors),
            errors,
        )

    def test_bad_required_sections_validate(self) -> None:
        ai_config.TARGET_RUNTIMES = ["claude", "codex"]
        ai_config.TARGET_SURFACES = ["copilot_cli", "jetbrains"]
        ai_config.COPILOT_REQUIRED_SECTIONS[:] = [42]
        errors = ai_config.validate(self.root)
        self.assertTrue(
            any("CONFIG TYPE" in e and "COPILOT_REQUIRED_SECTIONS" in e
                for e in errors),
            errors,
        )

    def test_none_required_sections_validate(self) -> None:
        ai_config.TARGET_RUNTIMES = ["claude", "codex"]
        ai_config.TARGET_SURFACES = ["copilot_cli", "jetbrains"]
        ai_config.COPILOT_REQUIRED_SECTIONS = None
        errors = ai_config.validate(self.root)
        self.assertTrue(
            any("CONFIG TYPE" in e and "COPILOT_REQUIRED_SECTIONS" in e
                for e in errors),
            errors,
        )


class MainCrashGuardTests(unittest.TestCase):
    """Verify main() doesn't crash on malformed constants."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)
        (self.root / ".git").mkdir(exist_ok=True)
        self._orig_runtimes = ai_config.TARGET_RUNTIMES
        self._orig_surfaces = ai_config.TARGET_SURFACES
        self._orig_features = ai_config.TARGET_FEATURES
        self._orig_servers = ai_config.MCP_SERVERS

    def tearDown(self) -> None:
        ai_config.TARGET_RUNTIMES = self._orig_runtimes
        ai_config.TARGET_SURFACES = self._orig_surfaces
        ai_config.TARGET_FEATURES = self._orig_features
        ai_config.MCP_SERVERS = self._orig_servers
        self.temp_directory.cleanup()

    def test_main_none_runtimes(self) -> None:
        ai_config.TARGET_RUNTIMES = None
        sys.argv = ["ai_config.py", "--check", "--root", str(self.root)]
        try:
            code = ai_config.main()
            self.assertEqual(1, code)
        except SystemExit:
            pass
        except TypeError:
            self.fail("main() crashed with TypeError on None runtimes")

    def test_main_unhashable_runtimes(self) -> None:
        ai_config.TARGET_RUNTIMES = [{}]
        sys.argv = ["ai_config.py", "--check", "--root", str(self.root)]
        try:
            code = ai_config.main()
            self.assertEqual(1, code)
        except SystemExit:
            pass
        except TypeError:
            self.fail("main() crashed with TypeError on unhashable runtimes")

    def test_main_unhashable_surfaces(self) -> None:
        ai_config.TARGET_SURFACES = [{}]
        sys.argv = ["ai_config.py", "--validate-config", "--root", str(self.root)]
        try:
            code = ai_config.main()
            self.assertEqual(1, code)
        except SystemExit:
            pass
        except TypeError:
            self.fail("main() crashed with TypeError on unhashable surfaces")

    def test_main_none_mcp_servers(self) -> None:
        ai_config.MCP_SERVERS = None
        sys.argv = ["ai_config.py", "--check", "--root", str(self.root)]
        try:
            code = ai_config.main()
            self.assertEqual(1, code)
        except SystemExit:
            pass
        except TypeError:
            self.fail("main() crashed with TypeError on None MCP_SERVERS")


class SchemaVersionBoolTests(unittest.TestCase):
    """Verify boolean schemaVersion is rejected."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_bool_schema_version_rejected_by_generator(self) -> None:
        (self.root / ".github").mkdir(parents=True, exist_ok=True)
        manifest = {
            "generatedBy": "ai_config.py",
            "canonicalSource": "CLAUDE.md",
            "schemaVersion": True,
            "runtimes": ["claude"],
            "surfaces": [],
            "features": [],
            "artifacts": [],
            "mcp_servers": [],
        }
        (self.root / ".github/ai-config-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        loaded = ai_config.load_manifest(self.root)
        self.assertIsNone(loaded)


class EmptyScopeWarningTests(unittest.TestCase):
    """Verify target/scope warnings fire even with empty scope lists."""

    def setUp(self) -> None:
        self._orig_servers = ai_config.MCP_SERVERS[:]

    def tearDown(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.extend(self._orig_servers)

    def test_codex_target_warns_with_empty_runtimes(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "srv",
            "targets": ["codex"],
            "transport": "stdio",
            "command": "x",
        })
        errors = ai_config.validate_mcp_servers(runtimes=[], surfaces=[])
        self.assertTrue(
            any("codex" in e and "TARGET_RUNTIMES" in e for e in errors),
            f"Empty runtimes should still warn about codex target: {errors}",
        )

    def test_vscode_target_warns_with_empty_surfaces(self) -> None:
        ai_config.MCP_SERVERS.clear()
        ai_config.MCP_SERVERS.append({
            "name": "srv",
            "targets": ["vscode"],
            "transport": "stdio",
            "command": "x",
        })
        errors = ai_config.validate_mcp_servers(runtimes=["claude"], surfaces=[])
        self.assertTrue(
            any("vscode" in e and "TARGET_SURFACES" in e for e in errors),
            f"Empty surfaces should still warn about vscode target: {errors}",
        )


class UnreadableFileTests(unittest.TestCase):
    """Verify invalid-encoding files produce errors, not crashes."""

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        _setup_minimal_repo(self.root)
        (self.root / ".git").mkdir(exist_ok=True)
        self._orig_runtimes = ai_config.TARGET_RUNTIMES[:]
        self._orig_surfaces = ai_config.TARGET_SURFACES[:]

    def tearDown(self) -> None:
        ai_config.TARGET_RUNTIMES[:] = self._orig_runtimes
        ai_config.TARGET_SURFACES[:] = self._orig_surfaces
        self.temp_directory.cleanup()

    def _write_bad_utf8(self, rel: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x80\x81\x82 invalid utf-8")

    def test_unreadable_claude_md(self) -> None:
        self._write_bad_utf8("CLAUDE.md")
        ai_config.TARGET_RUNTIMES[:] = ["claude", "codex"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        _, errors = ai_config.expected_generated_files(
            self.root, ["claude", "codex"], ["copilot_cli"]
        )
        self.assertTrue(
            any("UNREADABLE" in e for e in errors), errors
        )

    def test_unreadable_agents_md_in_collision(self) -> None:
        ai_config.TARGET_RUNTIMES[:] = ["claude", "codex"]
        ai_config.TARGET_SURFACES[:] = ["copilot_cli"]
        self._write_bad_utf8("AGENTS.md")
        _, errors = ai_config.expected_generated_files(
            self.root, ["claude", "codex"], ["copilot_cli"]
        )
        self.assertTrue(
            any("UNREADABLE" in e or "CONFLICT" in e for e in errors), errors
        )


if __name__ == "__main__":
    unittest.main()

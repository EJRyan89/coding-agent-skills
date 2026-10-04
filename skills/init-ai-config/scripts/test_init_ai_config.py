#!/usr/bin/env python3
"""Regression tests for the init-ai-config setup commands."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import ai_config_template
import init_ai_config as setup
import test_ai_config_template

OWNERSHIP = ai_config_template.OWNERSHIP_MARKER
CLAUDE_MD = (
    "# CLAUDE.md\n\n"
    "## Overview\n\nA fixture.\n\n"
    "## Build and Test Commands\n\n```bash\nmake test\n```\n\n"
    "## Formatting Rules\n\nUse tabs.\n\n"
    "## Commands\n\nRun make.\n\n"
    "## CI / Quality Gates\n\nEvery check must pass.\n"
)


def literal(source: str, name: str) -> object:
    for node in ast.parse(source).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not a literal assignment")


class Fixture(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo with spaces"
        self.root.mkdir()
        (self.root / ".git").mkdir()
        (self.root / "CLAUDE.md").write_text(CLAUDE_MD, encoding="utf-8")
        self.spec_path = Path(self.temp.name) / "spec.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, rel: str, content: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def run_command(self, *arguments: str) -> tuple[int, list[str], str]:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = setup.main(["--root", str(self.root), *arguments])
        return code, output.getvalue().splitlines(), errors.getvalue()

    def install(self, spec: object, *extra: str) -> tuple[int, list[str], str]:
        self.spec_path.write_text(json.dumps(spec), encoding="utf-8")
        return self.run_command("install", "--spec", str(self.spec_path), *extra)


SPEC = {
    "runtimes": ["claude", "codex"],
    "surfaces": ["copilot_cli", "cloud_agent"],
    "features": ["ci_parity", "ci_parity_caller"],
    "copilot_title": "# Fixture \"quoted\" — instructions",
    "copilot_sections": ["Overview", "Build and Test Commands", "Formatting Rules", "CI / Quality Gates", "Commands"],
    "copilot_setup_commands": [{"name": "Restore", "run": "dotnet restore\ndotnet tool restore"}],
    "mcp_servers": [{
        "name": "docs",
        "targets": ["claude", "codex", "copilot_local"],
        "transport": "stdio",
        "command": "C:\\Tools\\docs server.exe",
        "args": ["--root", "a b"],
        "cwd": None,
        "env": {"MODE": "read only"},
        "copilot_local": {"tools": None},
        "codex": {"approval_mode": "writes", "enabled_tools": None},
        "required": True,
    }],
}


class InstallTests(Fixture):

    def test_install_writes_literal_constants_and_tests(self) -> None:
        code, lines, errors = self.install(SPEC)
        self.assertEqual((0, ""), (code, errors))
        self.assertEqual(
            ["INSTALLED .github/scripts/ai_config.py", "INSTALLED .github/scripts/test_ai_config.py", "CONFIG_VALID"],
            lines,
        )
        generator = (self.root / ".github/scripts/ai_config.py").read_text(encoding="utf-8")
        self.assertIn(setup.INSTALL_MARKER, generator)
        expected = {**setup.template_defaults(), **SPEC}
        for key, name in setup.SPEC_FIELDS.items():
            with self.subTest(constant=name):
                self.assertEqual(expected[key], literal(generator, name))
        self.assertIn("\nMCP_SERVERS: list[dict[str, Any]] = [\n", generator)
        self.assertIn('\nCOPILOT_TITLE = "# Fixture \\"quoted\\" — instructions"\n', generator)
        tests = (self.root / ".github/scripts/test_ai_config.py").read_text(encoding="utf-8")
        self.assertIn("\nimport ai_config\n", tests)
        self.assertNotIn("ai_config_template", tests)
        self.assertIn(setup.INSTALL_MARKER, tests)

    def test_reference_example_spec_installs(self) -> None:
        example = setup.SCRIPTS.parent / "references" / "example-spec.json"
        code, lines, errors = self.run_command("install", "--spec", str(example))
        self.assertEqual((0, ""), (code, errors), lines)
        self.assertEqual("CONFIG_VALID", lines[-1])
        self.assertEqual(set(setup.SPEC_FIELDS), set(json.loads(example.read_text(encoding="utf-8"))))

    def test_reinstall_over_own_installation_and_conflict_with_foreign_generator(self) -> None:
        self.assertEqual(0, self.install(SPEC)[0])
        self.assertEqual(0, self.install({"runtimes": ["claude"], "surfaces": ["jetbrains"]})[0])
        generator = (self.root / ".github/scripts/ai_config.py").read_text(encoding="utf-8")
        self.assertEqual(["jetbrains"], literal(generator, "TARGET_SURFACES"))
        self.assertEqual([], literal(generator, "MCP_SERVERS"))

        foreign = self.write(".github/scripts/ai_config.py", "TARGET_RUNTIMES = ['claude']\n")
        code, lines, _ = self.install(SPEC)
        self.assertEqual(1, code)
        self.assertEqual(
            ["CONFLICT .github/scripts/ai_config.py exists and was not installed by init-ai-config;"
             " export its spec, then install with --replace"],
            lines,
        )
        self.assertEqual("TARGET_RUNTIMES = ['claude']\n", foreign.read_text(encoding="utf-8"))
        self.assertEqual(0, self.install(SPEC, "--replace")[0])
        self.assertIn(setup.INSTALL_MARKER, foreign.read_text(encoding="utf-8"))

    def test_spec_errors_write_nothing(self) -> None:
        cases = {
            "[]": ["SPEC_ERROR the spec must be a JSON object"],
            '{"runtimes": ["claude"], "surface": []}': [
                "SPEC_ERROR unknown key 'surface'", "SPEC_ERROR missing required key 'surfaces'",
            ],
            '{"runtimes": "claude", "surfaces": [], "mcp_servers": ["docs"], "copilot_title": 1}': [
                "SPEC_ERROR runtimes must be a list of strings",
                "SPEC_ERROR mcp_servers must be a list of objects",
                "SPEC_ERROR copilot_title must be a string",
            ],
        }
        for text, expected in cases.items():
            with self.subTest(spec=text):
                self.spec_path.write_text(text, encoding="utf-8")
                self.assertEqual((1, expected, ""), self.run_command("install", "--spec", str(self.spec_path)))
        self.spec_path.write_text("{not json", encoding="utf-8")
        code, lines, _ = self.run_command("install", "--spec", str(self.spec_path))
        self.assertEqual(1, code)
        self.assertTrue(lines[0].startswith("SPEC_ERROR the spec is not valid JSON"))
        self.assertFalse((self.root / ".github").exists())

    def test_configuration_errors_write_nothing(self) -> None:
        cases = [
            ({"runtimes": ["claude"], "surfaces": ["jetbrain"]}, "CONFIG_ERROR TARGET_SURFACES: unknown surface 'jetbrain'"),
            ({"runtimes": ["claude"], "surfaces": ["vscode"], "copilot_sections": ["Overview", "Missing"]},
             "CONFIG_ERROR COPILOT_SECTIONS: 'Missing' not found in CLAUDE.md"),
            ({"runtimes": ["claude"], "surfaces": [], "features": ["ci_parity_caller"]},
             "CONFIG_ERROR TARGET_FEATURES: ci_parity_caller requires ci_parity"),
            ({"runtimes": ["claude", "codex"], "surfaces": [],
              "mcp_servers": [{"name": "events", "targets": ["codex"], "transport": "sse", "url": "https://x.invalid"}]},
             "CONFIG_ERROR MCP server 'events': transport 'sse' is not supported for target 'codex'"),
        ]
        for spec, expected in cases:
            with self.subTest(expected=expected):
                code, lines, _ = self.install(spec)
                self.assertEqual(1, code)
                self.assertIn(expected, lines)
                self.assertFalse((self.root / ".github").exists())
        (self.root / "CLAUDE.md").unlink()
        self.assertEqual(
            (1, ["CONFIG_ERROR CLAUDE.md is missing; create it before installing the generator"], ""),
            self.install({"runtimes": ["claude", "codex"], "surfaces": []}),
        )

    def test_configuration_warnings_do_not_block(self) -> None:
        code, lines, _ = self.install({
            "runtimes": ["claude"], "surfaces": ["copilot_cli"],
            "mcp_servers": [{"name": "docs", "targets": ["claude", "codex"], "transport": "stdio", "command": "docs"}],
        })
        self.assertEqual(0, code)
        self.assertIn(
            "CONFIG_WARNING MCP server 'docs' targets codex but codex is not in TARGET_RUNTIMES", lines
        )
        self.assertEqual("CONFIG_VALID", lines[-1])

    def test_not_a_repository_and_missing_spec_fail(self) -> None:
        code, lines, errors = self.run_command("install", "--spec", str(self.spec_path))
        self.assertEqual((2, []), (code, lines))
        self.assertIn("FAILED cannot read spec", errors)
        (self.root / ".git").rmdir()
        code, _, errors = self.run_command("inventory")
        self.assertEqual(2, code)
        self.assertIn("is not a Git repository root", errors)


class InstalledGeneratorTests(Fixture):
    """The installed generator and its tests must work in a real repository."""

    def test_installed_generator_writes_checks_and_its_tests_pass(self) -> None:
        (self.root / ".git").rmdir()
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True, capture_output=True)
        self.write(".claude/skills/demo/SKILL.md", "---\nname: demo\ndescription: Demo skill.\n---\n\nWork.\n")
        self.assertEqual(0, self.install(SPEC)[0])
        generator = self.root / ".github/scripts/ai_config.py"
        for arguments in (["--validate-config"], ["--write"], ["--check"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, "-B", str(generator), *arguments],
                    cwd=self.root, capture_output=True, encoding="utf-8", errors="replace",
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue((self.root / ".github/workflows/ai-config-parity-pr.yml").is_file())
        self.assertTrue((self.root / ".github/workflows/copilot-setup-steps.yml").is_file())
        manifest = json.loads((self.root / ".github/ai-config-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(SPEC["features"], manifest["features"])
        tests = subprocess.run(
            [sys.executable, "-B", str(self.root / ".github/scripts/test_ai_config.py")],
            cwd=self.root, capture_output=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(0, tests.returncode, tests.stderr[-3000:])

    def test_template_test_defaults_match_the_template(self) -> None:
        source = setup.TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertEqual(
            {name: literal(source, name) for name in setup.SPEC_FIELDS.values()},
            test_ai_config_template.TEMPLATE_DEFAULTS,
        )


class ExportSpecTests(Fixture):

    def test_export_round_trips_an_installed_spec(self) -> None:
        self.assertEqual(0, self.install(SPEC)[0])
        output = Path(self.temp.name) / "exported.json"
        self.assertEqual((0, [f"SPEC {output}"], ""), self.run_command("export-spec", "--output", str(output)))
        exported = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual({**setup.template_defaults(), **SPEC}, exported)
        self.spec_path.write_text(json.dumps(exported), encoding="utf-8")
        before = (self.root / ".github/scripts/ai_config.py").read_text(encoding="utf-8")
        self.assertEqual(0, self.run_command("install", "--spec", str(self.spec_path))[0])
        self.assertEqual(before, (self.root / ".github/scripts/ai_config.py").read_text(encoding="utf-8"))

    def test_export_from_a_hand_customized_generator_fills_defaults(self) -> None:
        self.write("scripts/ai_config.py", (
            'COPILOT_TITLE = "# Old"\nCOPILOT_SECTIONS = ["Overview"]\nCOPILOT_REQUIRED_SECTIONS = []\n'
            'TARGET_RUNTIMES = ["claude"]\nTARGET_SURFACES: list[str] = ["vscode"]\nTARGET_FEATURES = []\n'
            "MCP_SERVERS = []\n"
        ))
        output = Path(self.temp.name) / "exported.json"
        self.assertEqual(
            (0, ["DEFAULTED copilot_setup_commands", f"SPEC {output}"], ""),
            self.run_command("export-spec", "--output", str(output)),
        )
        exported = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(["vscode"], exported["surfaces"])
        self.assertEqual([], exported["copilot_setup_commands"])

    def test_export_failures(self) -> None:
        output = Path(self.temp.name) / "exported.json"
        code, _, errors = self.run_command("export-spec", "--output", str(output))
        self.assertEqual(2, code)
        self.assertIn("FAILED no repository generator found", errors)
        self.write(".github/scripts/ai_config.py", "TARGET_RUNTIMES = list(BASE)\n")
        code, _, errors = self.run_command("export-spec", "--output", str(output))
        self.assertEqual(2, code)
        self.assertIn("TARGET_RUNTIMES in .github/scripts/ai_config.py is not a literal value", errors)
        self.assertFalse(output.exists())


class InventoryTests(Fixture):

    def test_inventory_reports_ownership_and_conflicts(self) -> None:
        mcp = '{"mcpServers": {}}\n'
        self.write(".mcp.json", mcp)
        modified = self.write(".vscode/mcp.json", '{"servers": {}}\n')
        manifest = {
            "generatedBy": "ai_config.py", "schemaVersion": 1, "canonicalSource": "CLAUDE.md",
            "runtimes": ["claude"], "surfaces": ["vscode"], "features": [], "mcp_servers": [],
            "artifacts": [
                {"path": ".mcp.json", "hash": hashlib.sha256(mcp.encode()).hexdigest()},
                {"path": ".vscode/mcp.json", "hash": "0" * 64},
            ],
        }
        self.write(".github/ai-config-manifest.json", json.dumps(manifest))
        self.write("AGENTS.md", "# Hand-written rules\n")
        self.write("docs/AGENTS.md", f"<!-- {OWNERSHIP} -->\n")
        self.write("tools/.mcp.json", "{}\n")
        self.write(".github/copilot-instructions.md", f"> {OWNERSHIP}\n")
        self.write(".claude/skills/demo/SKILL.md", "---\nname: demo\ndescription: Demo.\n---\n")
        self.write(".agents/skills/gone/SKILL.md", f"<!-- {OWNERSHIP} -->\n")
        self.write(".github/instructions/api.instructions.md", "Prefer async.\n")
        self.write(".github/agents/reviewer.agent.md", "---\ndescription: Review.\n---\n")
        self.write(".github/scripts/ai_config.py", f'OWNERSHIP_MARKER = "{OWNERSHIP}"\n')
        self.write(".github/workflows/ci.yml", "jobs:\n  parity:\n    uses: ./.github/workflows/ai-config-parity.yml\n")
        self.write(".github/workflows/build.yml", "jobs: {}\n")
        self.write(".git/AGENTS.md", "ignored\n")
        self.assertIsNotNone(modified)
        code, lines, errors = self.run_command("inventory")
        self.assertEqual((0, ""), (code, errors))
        self.assertEqual(
            [
                "FILE .agents/skills/gone/SKILL.md owner=generated",
                "FILE .claude/skills/demo/SKILL.md owner=user",
                "FILE .github/agents/reviewer.agent.md owner=user",
                "FILE .github/ai-config-manifest.json owner=generated",
                "FILE .github/copilot-instructions.md owner=generated",
                "FILE .github/instructions/api.instructions.md owner=user",
                "FILE .github/scripts/ai_config.py owner=user",
                "FILE .github/workflows/ci.yml owner=user",
                "FILE .mcp.json owner=generated",
                "FILE .vscode/mcp.json owner=hash-mismatch",
                "FILE AGENTS.md owner=user",
                "FILE CLAUDE.md owner=user",
                "FILE docs/AGENTS.md owner=generated",
                "FILE tools/.mcp.json owner=user",
                "CONFLICT .github/scripts/ai_config.py existing generator file was not installed by init-ai-config;"
                " export its spec, then install with --replace",
                "CONFLICT .vscode/mcp.json modified after generation (hash mismatch with manifest)",
                "CONFLICT AGENTS.md user-authored file at a generated path;"
                " --write refuses to replace it if the selected scope generates it",
                "CONFLICT .agents/skills/gone/SKILL.md generated shim whose canonical skill is gone;"
                " --write refuses until it is removed",
            ],
            lines,
        )

    def test_malformed_manifest_unowned_json_and_legacy_generator_are_conflicts(self) -> None:
        self.write(".github/ai-config-manifest.json", '{"generatedBy": "ai_config.py"}')
        self.write(".github/mcp.json", '{"mcpServers": {}}')
        self.write("scripts/ai_config.py", "TARGET_RUNTIMES = []\n")
        code, lines, _ = self.run_command("inventory")
        self.assertEqual(0, code)
        self.assertEqual(
            [
                "FILE .github/ai-config-manifest.json owner=user",
                "FILE .github/mcp.json owner=user",
                "FILE CLAUDE.md owner=user",
                "FILE scripts/ai_config.py owner=user",
                "CONFLICT .github/ai-config-manifest.json manifest is malformed or unsafe; --write will not trust it",
                "CONFLICT .github/mcp.json user-authored file at a generated path;"
                " --write refuses to replace it if the selected scope generates it",
                "CONFLICT scripts/ai_config.py legacy generator location; export its spec, install, then remove it"
                " and update workflows that run it",
            ],
            lines,
        )

    def test_installed_generator_is_generated_and_unreadable_files_conflict(self) -> None:
        self.assertEqual(0, self.install(SPEC)[0])
        (self.root / "GEMINI.md").write_bytes(b"\x80\x81 not utf-8")
        code, lines, _ = self.run_command("inventory")
        self.assertEqual(0, code)
        self.assertIn("FILE .github/scripts/ai_config.py owner=generated", lines)
        self.assertIn("FILE .github/scripts/test_ai_config.py owner=generated", lines)
        self.assertIn("FILE GEMINI.md owner=user", lines)
        self.assertIn("CONFLICT GEMINI.md file is unreadable; ownership cannot be determined", lines)


class DetectTests(Fixture):

    def test_detect_reports_build_format_workflow_and_mcp_facts(self) -> None:
        self.write("package.json", "{}")
        self.write("src/App/App.csproj", "<Project />")
        self.write("src/App/deep/inner/pyproject.toml", "")
        self.write("node_modules/pkg/package.json", "{}")
        self.write(".hidden/Makefile", "")
        self.write(".editorconfig", "root = true\n")
        self.write("web/.prettierrc", "{}")
        self.write(".github/workflows/ci.yml", "jobs:\n  parity:\n    uses: ./.github/workflows/ai-config-parity.yml\n")
        self.write(".mcp.json", json.dumps({"mcpServers": {
            "local": {"command": "docs"}, "remote": {"url": "https://x.invalid"}, "typed": {"type": "sse", "url": "u"},
        }}))
        self.write(".vscode/mcp.json", json.dumps({"mcpServers": {}}))
        self.write(".github/mcp.json", "{broken")
        self.write(".codex/config.toml", '[mcp_servers.codex-docs]\ncommand = "docs"\n[mcp_servers.web]\nurl = "u"\n')
        code, lines, errors = self.run_command("detect")
        self.assertEqual((0, ""), (code, errors))
        self.assertEqual(
            [
                "FORMAT_CONFIG .editorconfig",
                "BUILD_FILE package.json",
                "BUILD_FILE src/App/App.csproj",
                "FORMAT_CONFIG web/.prettierrc",
                "WORKFLOW .github/workflows/ci.yml",
                "PARITY_CALLER .github/workflows/ci.yml",
                "MCP_SERVER local transport=stdio source=.mcp.json",
                "MCP_SERVER remote transport=http source=.mcp.json",
                "MCP_SERVER typed transport=sse source=.mcp.json",
            ],
            lines[:9],
        )
        self.assertTrue(lines[9].startswith("UNREADABLE .github/mcp.json "), lines[9])
        self.assertEqual(
            [
                "UNREADABLE .vscode/mcp.json no 'servers' object",
                "MCP_SERVER codex-docs transport=stdio source=.codex/config.toml",
                "MCP_SERVER web transport=http source=.codex/config.toml",
            ],
            lines[10:],
        )


class RenderLiteralTests(unittest.TestCase):

    def test_values_round_trip_through_python_literals(self) -> None:
        value = {
            "quote": 'say "hi"', "single": "it's", "backslash": "C:\\path\\x", "newline": "a\nb\tc",
            "unicode": "café — ☃", "nested": [[], {}, [1, 2.5, True, None]], "empty": "",
            "long": ["x" * 40, "y" * 40, "z" * 40],
        }
        rendered = setup.render_literal(value)
        self.assertEqual(value, ast.literal_eval(rendered))
        self.assertIn('"quote": "say \\"hi\\""', rendered)

    def test_lone_surrogates_are_rejected(self) -> None:
        with self.assertRaises(setup.SetupError):
            setup.render_literal("\ud800")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Deterministic init-ai-config steps, so the agent keeps only judgment and user questions.

    inventory    list AI configuration files with their ownership, and the conflicts to resolve
    detect       list build files, formatter configs, workflows, parity callers, and MCP servers
    export-spec  write the JSON spec of an existing repository generator, to edit and reinstall
    install      validate a JSON spec, then install the generator and its tests into a repository

Every command takes --root (default: the current directory), which must be a Git repository.
Commands print one fact per line and exit 0; install exits 1 when it prints SPEC_ERROR,
CONFIG_ERROR, or CONFLICT lines, and writes nothing in that case. Expected failures print
`FAILED <reason>` on stderr and exit 2.
"""

from __future__ import annotations

import argparse
import ast
import copy
import fnmatch
import json
import os
from pathlib import Path
import sys
import tomllib
import types
from typing import Any

import ai_config_template as template

SCRIPTS = Path(__file__).resolve().parent
TEMPLATE_PATH = SCRIPTS / "ai_config_template.py"
TEST_TEMPLATE_PATH = SCRIPTS / "test_ai_config_template.py"
GENERATOR_PATH = ".github/scripts/ai_config.py"
GENERATOR_TEST_PATH = ".github/scripts/test_ai_config.py"
MANIFEST_PATH = ".github/ai-config-manifest.json"
INSTALL_MARKER = "Installed by init-ai-config"

# Identical to audit-ai-config's inventory; tests/ai-config/test_cross_skill_contracts.py enforces parity.
INVENTORY_FILES: list[str] = [
    "CLAUDE.md",
    "GEMINI.md",
    "REVIEW.md",
    ".codex/config.toml",
    ".mcp.json",
    ".github/mcp.json",
    ".vscode/mcp.json",
    ".github/copilot-instructions.md",
    ".github/workflows/copilot-setup-steps.yml",
    ".github/ai-config-manifest.json",
]
RECURSIVE_INVENTORY_NAMES: list[str] = ["AGENTS.md", "AGENTS.override.md"]
GENERATOR_CANDIDATES: list[str] = [".github/scripts/ai_config.py", "scripts/ai_config.py"]

# Spec key -> generator constant. Omitted optional keys keep the template default.
SPEC_FIELDS: dict[str, str] = {
    "runtimes": "TARGET_RUNTIMES",
    "surfaces": "TARGET_SURFACES",
    "features": "TARGET_FEATURES",
    "copilot_title": "COPILOT_TITLE",
    "copilot_sections": "COPILOT_SECTIONS",
    "copilot_required_sections": "COPILOT_REQUIRED_SECTIONS",
    "copilot_setup_commands": "COPILOT_SETUP_COMMANDS",
    "mcp_servers": "MCP_SERVERS",
}
REQUIRED_SPEC_FIELDS = ("runtimes", "surfaces")
STRING_LIST_FIELDS = ("runtimes", "surfaces", "features", "copilot_sections", "copilot_required_sections")
OBJECT_LIST_FIELDS = ("copilot_setup_commands", "mcp_servers")

INSTALLED_GENERATOR_DOCSTRING = '''"""Generate and validate AI-agent configuration derived from CLAUDE.md.

Installed by init-ai-config from a JSON spec. To change the repository-specific
constants below, export the spec with init-ai-config, edit it, and reinstall it rather
than editing them here. Use --write to regenerate derived files and --check to validate
parity without writing.
"""'''
TEMPLATE_TEST_IMPORT = """# When copied to .github/scripts/test_ai_config.py, change this import to:
#   import ai_config
try:
    import ai_config_template as ai_config
except ModuleNotFoundError:
    import ai_config  # type: ignore[no-redef]
"""
TEMPLATE_TEST_INTRO = """init-ai-config installs this file as .github/scripts/test_ai_config.py next to the
generator. Run with: python test_ai_config.py
"""
INSTALLED_TEST_INTRO = """Installed by init-ai-config next to ai_config.py; reinstall it rather than editing it.
Run with: python .github/scripts/test_ai_config.py
"""

BUILD_FILE_NAMES = {
    "package.json", "pnpm-workspace.yaml", "deno.json", "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt", "Pipfile", "tox.ini", "noxfile.py", "go.mod", "Cargo.toml", "pom.xml",
    "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "build.sbt",
    "Makefile", "CMakeLists.txt", "meson.build", "justfile", "Taskfile.yml", "BUILD.bazel",
    "WORKSPACE", "MODULE.bazel", "Gemfile", "composer.json", "mix.exs", "Package.swift",
    "global.json", "Directory.Build.props", "Directory.Packages.props", "nuget.config",
}
BUILD_FILE_SUFFIXES = (".sln", ".slnx", ".csproj", ".fsproj", ".vbproj", ".vcxproj")
FORMAT_CONFIG_NAMES = {
    ".editorconfig", ".prettierrc", ".prettierrc.json", ".prettierrc.yml", ".prettierrc.yaml",
    ".prettierrc.js", ".prettierrc.cjs", "prettier.config.js", ".eslintrc", ".eslintrc.json",
    ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.yml", "eslint.config.js", "eslint.config.mjs",
    "biome.json", ".rubocop.yml", ".clang-format", "rustfmt.toml", ".rustfmt.toml", "ruff.toml",
    ".ruff.toml", ".flake8", ".pylintrc", ".golangci.yml", ".golangci.yaml", ".stylelintrc",
    ".markdownlint.json", ".shellcheckrc", "PSScriptAnalyzerSettings.psd1", "stylecop.json",
}
SKIPPED_DIRECTORIES = {"node_modules", "bin", "obj", "dist", "build", "target", "out", "vendor", "venv"}
MCP_JSON_SOURCES = ((".mcp.json", "mcpServers"), (".github/mcp.json", "mcpServers"), (".vscode/mcp.json", "servers"))


class SetupError(Exception):
    """An expected failure reported as FAILED."""


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _in_git_directory(root: Path, path: Path) -> bool:
    return ".git" in path.relative_to(root).parts


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------

def inventory_paths(root: Path) -> list[str]:
    """Every AI-configuration file the skill must know about before generating."""
    found: set[str] = {rel for rel in INVENTORY_FILES if (root / rel).is_file()}
    for name in [*RECURSIVE_INVENTORY_NAMES, ".mcp.json"]:
        found.update(_relative(root, path) for path in root.rglob(name) if path.is_file() and not _in_git_directory(root, path))
    for directory in (".claude/skills", ".agents/skills", ".github/skills"):
        found.update(_relative(root, path) for path in (root / directory).glob("*/SKILL.md") if path.is_file())
    for directory in (".github/agents", ".claude/agents"):
        if (root / directory).is_dir():
            found.update(_relative(root, path) for path in (root / directory).iterdir() if path.is_file())
    instructions = root / ".github/instructions"
    if instructions.is_dir():
        found.update(_relative(root, path) for path in instructions.rglob("*.instructions.md") if path.is_file())
    found.update(rel for rel in [*GENERATOR_CANDIDATES, GENERATOR_TEST_PATH] if (root / rel).is_file())
    workflows = root / ".github/workflows"
    if workflows.is_dir():
        for path in [*workflows.glob("*.yml"), *workflows.glob("*.yaml")]:
            try:
                text = template.read_text(path).lower()
            except (OSError, UnicodeError):
                text = "ai-config"
            if "ai-config" in text or "ai_config" in text:
                found.add(_relative(root, path))
    return sorted(found)


def _generator_owned(rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, pattern) for pattern in template.MANIFEST_ALLOWED_PATHS)


def inventory(root: Path) -> list[str]:
    lines: list[str] = []
    conflicts: list[str] = []
    generated: set[str] = set()
    manifest = template.load_manifest(root)
    if (root / MANIFEST_PATH).is_file() and manifest is None:
        conflicts.append(f"CONFLICT {MANIFEST_PATH} manifest is malformed or unsafe; --write will not trust it")
    hashes = {
        artifact["path"]: artifact.get("hash")
        for artifact in (manifest or {}).get("artifacts", [])
    }
    for rel in inventory_paths(root):
        path = root / rel
        try:
            content = template.read_text(path)
        except (OSError, UnicodeError):
            lines.append(f"FILE {rel} owner=user")
            conflicts.append(f"CONFLICT {rel} file is unreadable; ownership cannot be determined")
            continue
        if rel in (GENERATOR_PATH, GENERATOR_TEST_PATH):
            owner = "generated" if INSTALL_MARKER in content else "user"
            if owner == "user":
                conflicts.append(
                    f"CONFLICT {rel} existing generator file was not installed by init-ai-config;"
                    " export its spec, then install with --replace"
                )
        elif rel in GENERATOR_CANDIDATES:
            owner = "user"
            conflicts.append(
                f"CONFLICT {rel} legacy generator location; export its spec, install, then remove it"
                " and update workflows that run it"
            )
        elif rel == MANIFEST_PATH:
            owner = "generated" if manifest is not None else "user"
        elif rel.endswith(".json"):
            stored = hashes.get(rel)
            if stored is None:
                owner = "user"
            elif template.content_hash(content) == stored:
                owner = "generated"
            else:
                owner = "hash-mismatch"
                conflicts.append(f"CONFLICT {rel} modified after generation (hash mismatch with manifest)")
        else:
            owner = "generated" if template.OWNERSHIP_MARKER in content else "user"
        if owner == "user" and rel != MANIFEST_PATH and _generator_owned(rel):
            conflicts.append(
                f"CONFLICT {rel} user-authored file at a generated path;"
                " --write refuses to replace it if the selected scope generates it"
            )
        lines.append(f"FILE {rel} owner={owner}")
        if owner == "generated":
            generated.add(rel)
    for orphan in template.orphaned_skill_shims(root):
        rel = _relative(root, orphan)
        if rel in generated:
            conflicts.append(
                f"CONFLICT {rel} generated shim whose canonical skill is gone; --write refuses until it is removed"
            )
    return lines + conflicts


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------

def _walk(root: Path, depth: int) -> list[Path]:
    """Files at most `depth` directories below the root, skipping hidden and generated trees."""
    files: list[Path] = []
    for directory, subdirectories, names in os.walk(root):
        current = Path(directory)
        level = len(current.relative_to(root).parts)
        subdirectories[:] = sorted(
            name for name in subdirectories
            if level < depth and not name.startswith(".") and name not in SKIPPED_DIRECTORIES
        )
        files.extend(current / name for name in names)
    return files


def _transport(entry: dict[str, Any]) -> str:
    declared = entry.get("type")
    if isinstance(declared, str) and declared.strip():
        return declared.strip().lower()
    if isinstance(entry.get("command"), str) and entry["command"].strip():
        return "stdio"
    return "http" if isinstance(entry.get("url"), str) and entry["url"].strip() else "unknown"


def mcp_servers(root: Path) -> list[str]:
    lines: list[str] = []
    for rel, wrapper in MCP_JSON_SOURCES:
        path = root / rel
        if not path.is_file():
            continue
        try:
            data = json.loads(template.read_text(path))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            lines.append(f"UNREADABLE {rel} {error}")
            continue
        servers = data.get(wrapper) if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            lines.append(f"UNREADABLE {rel} no '{wrapper}' object")
            continue
        for name, entry in servers.items():
            kind = _transport(entry) if isinstance(entry, dict) else "unknown"
            lines.append(f"MCP_SERVER {name} transport={kind} source={rel}")
    codex = root / ".codex/config.toml"
    if codex.is_file():
        try:
            with codex.open("rb") as handle:
                table = tomllib.load(handle).get("mcp_servers", {})
        except (OSError, tomllib.TOMLDecodeError) as error:
            lines.append(f"UNREADABLE .codex/config.toml {error}")
            table = {}
        for name, entry in (table.items() if isinstance(table, dict) else []):
            kind = _transport(entry) if isinstance(entry, dict) else "unknown"
            lines.append(f"MCP_SERVER {name} transport={kind} source=.codex/config.toml")
    return lines


def detect(root: Path) -> list[str]:
    lines: list[str] = []
    for path in sorted(_walk(root, 2), key=lambda p: _relative(root, p)):
        rel = _relative(root, path)
        if path.name in BUILD_FILE_NAMES or path.name.endswith(BUILD_FILE_SUFFIXES):
            lines.append(f"BUILD_FILE {rel}")
        elif path.name in FORMAT_CONFIG_NAMES:
            lines.append(f"FORMAT_CONFIG {rel}")
    workflows = root / ".github/workflows"
    if workflows.is_dir():
        for path in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
            lines.append(f"WORKFLOW {_relative(root, path)}")
    for caller in template.find_ci_parity_callers(root, template.CI_PARITY_WORKFLOW_PATH):
        lines.append(f"PARITY_CALLER {caller}")
    return lines + mcp_servers(root)


# ---------------------------------------------------------------------------
# Spec handling
# ---------------------------------------------------------------------------

def _constant_nodes(tree: ast.Module) -> dict[str, ast.Assign | ast.AnnAssign]:
    nodes: dict[str, ast.Assign | ast.AnnAssign] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            nodes[node.target.id] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    nodes[target.id] = node
    return nodes


def literal_spec(source: str, filename: str) -> tuple[dict[str, Any], list[str]]:
    """Read the spec constants from generator source with AST only; return (spec, defaulted keys)."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as error:
        raise SetupError(f"{filename} is not valid Python: {error}") from error
    nodes = _constant_nodes(tree)
    spec: dict[str, Any] = {}
    missing: list[str] = []
    for key, name in SPEC_FIELDS.items():
        node = nodes.get(name)
        if node is None:
            missing.append(key)
            continue
        try:
            spec[key] = ast.literal_eval(node.value)
        except ValueError as error:
            raise SetupError(f"{name} in {filename} is not a literal value") from error
    return spec, missing


def template_defaults() -> dict[str, Any]:
    spec, missing = literal_spec(TEMPLATE_PATH.read_text(encoding="utf-8"), TEMPLATE_PATH.name)
    if missing:
        raise SetupError(f"the template lacks {', '.join(missing)}")
    return spec


def check_spec(spec: Any) -> list[str]:
    """Structural problems with an authored spec, before the generator's own validation."""
    if not isinstance(spec, dict):
        return ["the spec must be a JSON object"]
    errors = [f"unknown key '{key}'" for key in spec if key not in SPEC_FIELDS]
    errors.extend(f"missing required key '{key}'" for key in REQUIRED_SPEC_FIELDS if key not in spec)
    for key in STRING_LIST_FIELDS:
        value = spec.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            errors.append(f"{key} must be a list of strings")
    for key in OBJECT_LIST_FIELDS:
        value = spec.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            errors.append(f"{key} must be a list of objects")
    if "copilot_title" in spec and not isinstance(spec["copilot_title"], str):
        errors.append("copilot_title must be a string")
    return errors


def render_literal(value: Any, indent: int = 0) -> str:
    """Render JSON data as a readable Python literal that ast.literal_eval reads back."""
    pad = " " * indent
    inner = " " * (indent + 4)
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise SetupError("the spec contains a string that is not valid Unicode") from error
        return json.dumps(value, ensure_ascii=False)
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        flat = "[" + ", ".join(render_literal(item) for item in value) + "]"
        if all(not isinstance(item, (list, dict)) for item in value) and len(pad) + len(flat) <= 88:
            return flat
        return "[\n" + "".join(f"{inner}{render_literal(item, indent + 4)},\n" for item in value) + f"{pad}]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "{\n" + "".join(
            f"{inner}{json.dumps(key, ensure_ascii=False)}: {render_literal(item, indent + 4)},\n"
            for key, item in value.items()
        ) + f"{pad}}}"
    raise SetupError(f"unsupported value in spec: {type(value).__name__}")


def render_generator(values: dict[str, Any]) -> str:
    """The template with its docstring marked as installed and each spec constant written literally."""
    source = TEMPLATE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=TEMPLATE_PATH.name)
    lines = source.split("\n")
    nodes = _constant_nodes(tree)
    docstring = tree.body[0]
    if not (isinstance(docstring, ast.Expr) and isinstance(docstring.value, ast.Constant)):
        raise SetupError(f"{TEMPLATE_PATH.name} no longer starts with a docstring")
    replacements: list[tuple[int, int, str]] = [
        (docstring.lineno, docstring.end_lineno or docstring.lineno, INSTALLED_GENERATOR_DOCSTRING)
    ]
    for key, name in SPEC_FIELDS.items():
        node = nodes[name]
        literal = render_literal(values[key])
        if isinstance(node, ast.AnnAssign):
            annotation = ast.get_source_segment(source, node.annotation)
            text = f"{name}: {annotation} = {literal}"
        else:
            text = f"{name} = {literal}"
        replacements.append((node.lineno, node.end_lineno or node.lineno, text))
    for start, end, text in sorted(replacements, reverse=True):
        lines[start - 1:end] = text.split("\n")
    rendered = "\n".join(lines)
    written, _ = literal_spec(rendered, GENERATOR_PATH)
    if written != values:
        raise SetupError("internal error: rendered constants do not round-trip")
    return rendered


def render_tests() -> str:
    source = TEST_TEMPLATE_PATH.read_text(encoding="utf-8")
    for old, new in ((TEMPLATE_TEST_IMPORT, "import ai_config\n"), (TEMPLATE_TEST_INTRO, INSTALLED_TEST_INTRO)):
        if source.count(old) != 1:
            raise SetupError(f"{TEST_TEMPLATE_PATH.name} no longer contains the expected block to rewrite")
        source = source.replace(old, new)
    return source


def load_module(source: str, path: Path) -> types.ModuleType:
    module = types.ModuleType("ai_config_installed")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def configuration_messages(module: types.ModuleType, root: Path) -> list[str]:
    if not (root / "CLAUDE.md").is_file():
        return ["CONFIG_ERROR CLAUDE.md is missing; create it before installing the generator"]
    errors = module.validate_config(root)
    messages = [f"CONFIG_ERROR {error}" for error in errors if not error.startswith("WARNING:")]
    if messages:
        return messages
    warnings = module.validate_mcp_servers(
        surfaces=list(module.TARGET_SURFACES), runtimes=list(module.TARGET_RUNTIMES)
    )
    return [f"CONFIG_WARNING {warning.removeprefix('WARNING: ')}" for warning in warnings if warning.startswith("WARNING:")]


def install(root: Path, spec_path: Path, replace: bool) -> tuple[int, list[str]]:
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    except OSError as error:
        raise SetupError(f"cannot read spec {spec_path}: {error}") from error
    except json.JSONDecodeError as error:
        return 1, [f"SPEC_ERROR the spec is not valid JSON: {error}"]
    problems = check_spec(spec)
    if problems:
        return 1, [f"SPEC_ERROR {problem}" for problem in problems]
    values = {**template_defaults(), **copy.deepcopy(spec)}
    generator = render_generator(values)
    messages = configuration_messages(load_module(generator, root / GENERATOR_PATH), root)
    if any(message.startswith("CONFIG_ERROR") for message in messages):
        return 1, messages
    outputs = {GENERATOR_PATH: generator, GENERATOR_TEST_PATH: render_tests()}
    conflicts: list[str] = []
    for rel in outputs:
        path = root / rel
        if path.is_file() and not replace:
            try:
                owned = INSTALL_MARKER in template.read_text(path)
            except (OSError, UnicodeError):
                owned = False
            if not owned:
                conflicts.append(
                    f"CONFLICT {rel} exists and was not installed by init-ai-config;"
                    " export its spec, then install with --replace"
                )
    if conflicts:
        return 1, conflicts
    lines: list[str] = []
    for rel, content in outputs.items():
        try:
            template.write_text(root / rel, content)
        except OSError as error:
            raise SetupError(f"could not write {rel}: {error}") from error
        lines.append(f"INSTALLED {rel}")
    return 0, lines + messages + ["CONFIG_VALID"]


def export_spec(root: Path, output: Path) -> list[str]:
    for rel in GENERATOR_CANDIDATES:
        path = root / rel
        if path.is_file():
            try:
                source = template.read_text(path)
            except (OSError, UnicodeError) as error:
                raise SetupError(f"cannot read {rel}: {error}") from error
            spec, missing = literal_spec(source, rel)
            defaults = template_defaults()
            spec.update({key: defaults[key] for key in missing})
            template.write_text(output, json.dumps(spec, indent=2, ensure_ascii=False) + "\n")
            return [f"DEFAULTED {key}" for key in missing] + [f"SPEC {output}"]
    raise SetupError("no repository generator found at " + " or ".join(GENERATOR_CANDIDATES))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root (default: current directory)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inventory")
    commands.add_parser("detect")
    export_parser = commands.add_parser("export-spec")
    export_parser.add_argument("--output", type=Path, required=True)
    install_parser = commands.add_parser("install")
    install_parser.add_argument("--spec", type=Path, required=True)
    install_parser.add_argument("--replace", action="store_true",
                                help="replace generator files that init-ai-config did not install")
    args = parser.parse_args(arguments)
    root = args.root.resolve()
    code = 0
    try:
        if not (root / ".git").exists():
            raise SetupError(f"{root} is not a Git repository root")
        if args.command == "inventory":
            lines = inventory(root)
        elif args.command == "detect":
            lines = detect(root)
        elif args.command == "export-spec":
            lines = export_spec(root, args.output)
        else:
            code, lines = install(root, args.spec, args.replace)
    except SetupError as error:
        print(f"FAILED {error}", file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Generate and validate AI-agent configuration derived from CLAUDE.md.

This is the init-ai-config template. Its install command writes it to a repository's
.github/scripts/ai_config.py with the repository-specific constants below replaced by
literal values from a JSON spec. Use --write to regenerate derived files and --check to
validate parity without writing.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from typing import Any

EXIT_CONTRACT_EXEMPT = "Installed as .github/scripts/ai_config.py and run by other repositories' CI, which reads its exit status and stderr"


# ---------------------------------------------------------------------------
# Repository-specific constants — customize these for each repo
# ---------------------------------------------------------------------------

COPILOT_TITLE = "# {repo_name} — AI Coding Instructions"

COPILOT_BANNER = (
    "> AUTO-GENERATED from CLAUDE.md. Do not edit directly"
    " — update CLAUDE.md instead."
)

AGENTS_BANNER = (
    "<!-- AUTO-GENERATED from CLAUDE.md."
    " Do not edit directly — update CLAUDE.md instead. -->"
)

OWNERSHIP_MARKER = "AUTO-GENERATED from CLAUDE.md"

SKILL_GLOB = "*/SKILL.md"

# Sections from CLAUDE.md to include in Copilot projection (concise, not full mirror).
# GitHub recommends concise repository-wide instructions; detailed procedures belong in skills.
COPILOT_SECTIONS: list[str] = [
    "Overview",
    "Build and Test Commands",
    "Formatting Rules",
    "CI / Quality Gates",
    # Add/remove sections per repo — avoid excessive context on every Copilot request
]

# Required minimum sections — generation fails if configuration removes these when
# the least-capable targeted surface needs them.
COPILOT_REQUIRED_SECTIONS: list[str] = [
    "Overview",
    "Build and Test Commands",
    "Formatting Rules",
    "CI / Quality Gates",
]

# Repository-specific target configuration — the source of truth for what
# this repository generates. Set during initial configuration; these override
# manifest values during both --write and --check.
TARGET_RUNTIMES: list[str] = ["claude"]
TARGET_SURFACES: list[str] = []
TARGET_FEATURES: list[str] = []

# Commands needed only by the Copilot cloud agent. Each entry becomes a named
# run step in copilot-setup-steps.yml when cloud_agent is targeted.
COPILOT_SETUP_COMMANDS: list[dict[str, str]] = [
    # {"name": "Restore dependencies", "run": "dotnet restore"},
]

# Transport-discriminated server definitions — supports multiple servers.
# See the plan for the full field reference.
MCP_SERVERS: list[dict[str, Any]] = [
    # {
    #     "name": "{server_name}",
    #     "targets": ["claude", "codex", "copilot_local", "vscode"],
    #     "transport": "stdio",
    #     "command": "{command}",
    #     "args": ["{arg1}"],
    #     "cwd": None,
    #     "url": None,
    #     "env": {},
    #     "env_vars": [],
    #     "copilot_local": {"tools": None},
    #     "codex": {
    #         "approval_mode": "writes",
    #         "enabled_tools": None,
    #         "disabled_tools": None,
    #     },
    #     "copilot_repository": {
    #         "tools": [],
    #         "secrets": {},
    #     },
    # },
]

CI_PARITY_WORKFLOW_PATH = ".github/workflows/ai-config-parity.yml"
CI_PARITY_CALLER_PATH = ".github/workflows/ai-config-parity-pr.yml"
COPILOT_SETUP_WORKFLOW_PATH = ".github/workflows/copilot-setup-steps.yml"

# Manifest schema version
MANIFEST_SCHEMA_VERSION = 1

# Fixed allowlist of paths the manifest may own (relative to repo root).
# Patterns use fnmatch-style globs.
MANIFEST_ALLOWED_PATHS: list[str] = [
    ".github/copilot-instructions.md",
    ".github/ai-config-manifest.json",
    ".github/mcp.json",
    ".github/workflows/copilot-setup-steps.yml",
    ".github/workflows/ai-config-parity.yml",
    ".github/workflows/ai-config-parity-pr.yml",
    "AGENTS.md",
    ".mcp.json",
    ".vscode/mcp.json",
    ".codex/config.toml",
    ".agents/skills/*/SKILL.md",
]

# Canonical vocabulary — these are the only valid values for each dimension.
# Runtimes: who runs the agent session.
VALID_RUNTIMES: set[str] = {"claude", "codex"}
# Surfaces: where instructions are consumed (determines which artifacts to generate).
VALID_SURFACES: set[str] = {
    "copilot_cli", "copilot_app", "vscode", "jetbrains",
    "cloud_agent", "code_review",
}
# MCP targets: where server configs are deployed (used in MCP_SERVERS[].targets).
VALID_MCP_TARGETS: set[str] = {
    "claude", "codex", "copilot_local", "vscode", "copilot_repository",
}
# Features: optional generator capabilities. ci_parity_caller adds a pull-request
# workflow that calls the reusable ci_parity workflow, for repositories whose own CI
# does not call it.
VALID_FEATURES: set[str] = {"ci_parity", "ci_parity_caller"}

# Supported transports per MCP target. The audit engine is intentionally
# standalone; tests/ai-config/test_cross_skill_contracts.py enforces parity.
TRANSPORT_COMPATIBILITY: dict[str, set[str]] = {
    "stdio": {"claude", "codex", "copilot_local", "vscode", "copilot_repository"},
    "local": {"copilot_local", "copilot_repository"},
    "http": {"claude", "codex", "copilot_local", "vscode", "copilot_repository"},
    "sse": {"claude", "copilot_local", "vscode", "copilot_repository"},
}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def write_text(path: Path, content: str) -> None:
    """Atomic write: temp file + rename. Idempotent — skips if content matches."""
    try:
        if path.is_file() and read_text(path) == content:
            return
    except (OSError, UnicodeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            output.write(content)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def validate_vocabulary(
    runtimes: list[str],
    surfaces: list[str],
    mcp_targets: list[str] | None = None,
) -> list[str]:
    """Validate that runtimes, surfaces, and MCP targets use canonical names."""
    errors: list[str] = []
    for r in runtimes:
        if r not in VALID_RUNTIMES:
            errors.append(f"unknown runtime: '{r}'")
    for s in surfaces:
        if s not in VALID_SURFACES:
            errors.append(f"unknown surface: '{s}'")
    if mcp_targets is not None:
        for t in mcp_targets:
            if t not in VALID_MCP_TARGETS:
                errors.append(f"unknown MCP target: '{t}'")
    return errors


def validate_manifest_path(path_str: str) -> str | None:
    """Return an error message if path is unsafe for manifest ownership."""
    if not path_str:
        return "empty path"
    if "\\" in path_str:
        return f"backslash not allowed in manifest path: {path_str}"
    if re.match(r"^[A-Za-z]:", path_str):
        return f"drive-qualified path not allowed: {path_str}"
    if path_str.startswith("//"):
        return f"UNC path not allowed: {path_str}"
    posix = PurePosixPath(path_str)
    if posix.is_absolute():
        return f"absolute path not allowed: {path_str}"
    if ".." in posix.parts:
        return f"path traversal not allowed: {path_str}"
    import fnmatch
    if not any(fnmatch.fnmatch(path_str, pat) for pat in MANIFEST_ALLOWED_PATHS):
        return f"path not in manifest allowlist: {path_str}"
    if path_str.startswith(".agents/skills/"):
        segments = path_str.split("/")
        if len(segments) != 4:
            return f"skill shim path must have exactly one skill-name segment: {path_str}"
    return None


# ---------------------------------------------------------------------------
# Section extraction from CLAUDE.md
# ---------------------------------------------------------------------------

def extract_sections(claude_content: str, section_names: list[str]) -> str:
    """Extract named H2 sections from CLAUDE.md content."""
    lines = claude_content.splitlines()
    sections: dict[str, list[str]] = {}
    current_section: str | None = None

    for line in lines:
        heading_match = re.match(r"^## (.+)$", line)
        if heading_match:
            current_section = heading_match.group(1).strip()
            if current_section not in sections:
                sections[current_section] = []
            sections[current_section].append(line)
        elif current_section and current_section in sections:
            sections[current_section].append(line)

    result_parts: list[str] = []
    for name in section_names:
        if name in sections:
            section_text = "\n".join(sections[name]).strip()
            if section_text:
                result_parts.append(section_text)

    return "\n\n".join(result_parts)


# ---------------------------------------------------------------------------
# Generation: Copilot instructions
# ---------------------------------------------------------------------------

def generate_copilot_instructions(
    claude_content: str,
    surfaces: list[str] | None = None,
    repo_name: str = "",
) -> str:
    """Generate .github/copilot-instructions.md from CLAUDE.md sections."""
    if not claude_content.strip():
        raise ValueError("CLAUDE.md is empty")

    projected = extract_sections(claude_content, COPILOT_SECTIONS)
    if not projected:
        raise ValueError(
            f"No matching sections found in CLAUDE.md for: {COPILOT_SECTIONS}"
        )

    # Enforce required sections when least-capable surfaces are targeted
    needs_required = False
    if surfaces:
        least_capable = {"jetbrains", "vscode", "cloud_agent", "code_review"}
        if least_capable & set(surfaces):
            needs_required = True
    if needs_required and COPILOT_REQUIRED_SECTIONS:
        available = set(
            re.match(r"^## (.+)$", line).group(1).strip()
            for line in claude_content.splitlines()
            if re.match(r"^## (.+)$", line)
        )
        configured = set(COPILOT_SECTIONS)
        missing = []
        for req in COPILOT_REQUIRED_SECTIONS:
            if req not in configured:
                missing.append(f"{req} (not in COPILOT_SECTIONS)")
            elif req not in available:
                missing.append(f"{req} (not in CLAUDE.md)")
        if missing:
            raise ValueError(
                f"Required sections missing for targeted surfaces: "
                f"{', '.join(missing)}"
            )

    try:
        title = COPILOT_TITLE.format(repo_name=repo_name) if repo_name else COPILOT_TITLE
    except (KeyError, IndexError, ValueError):
        title = COPILOT_TITLE
    return f"{title}\n\n{COPILOT_BANNER}\n\n{projected}\n"


# ---------------------------------------------------------------------------
# Generation: AGENTS.md adapter
# ---------------------------------------------------------------------------

def generate_agents_adapter(repo_name: str) -> str:
    """Generate a minimal AGENTS.md that redirects to CLAUDE.md."""
    return (
        f"{AGENTS_BANNER}\n\n"
        f"# {repo_name}\n\n"
        "Read and follow `CLAUDE.md` as the authoritative source for all "
        "repository instructions, conventions, and workflows.\n\n"
        "All build commands, formatting rules, test conventions, architecture "
        "guidance, and behavioral constraints are maintained in `CLAUDE.md`. "
        "Do not duplicate or contradict its content here.\n"
    )


# ---------------------------------------------------------------------------
# Generation: Skill shims
# ---------------------------------------------------------------------------

def canonical_frontmatter(skill_path: Path) -> tuple[str, str, str]:
    """Extract name and description from skill frontmatter."""
    lines = read_text(skill_path).splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("frontmatter must start with '---'")

    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise ValueError("frontmatter is missing its closing '---'") from error

    frontmatter = lines[1:end]
    name_lines = [line for line in frontmatter if line.startswith("name:")]
    description_lines = [
        line for line in frontmatter if line.startswith("description:")
    ]
    if len(name_lines) != 1 or len(description_lines) != 1:
        raise ValueError(
            "frontmatter must contain one single-line name and description"
        )

    name_line = name_lines[0]
    description_line = description_lines[0]
    name = name_line.split(":", 1)[1].strip().strip("\"'")
    description = description_line.split(":", 1)[1].strip()
    if not name or not description or description.startswith((">", "|")):
        raise ValueError(
            "frontmatter name and description must be single-line values"
        )
    if name != skill_path.parent.name:
        raise ValueError(
            f"frontmatter name '{name}' does not match "
            f"directory '{skill_path.parent.name}'"
        )

    return name, name_line, description_line


def expected_skill_shim(skill_path: Path) -> tuple[str, str]:
    """Return (name, expected_content) for a Codex skill shim."""
    name, name_line, description_line = canonical_frontmatter(skill_path)
    content = (
        "---\n"
        f"{name_line}\n"
        f"{description_line}\n"
        "---\n\n"
        f"<!-- {OWNERSHIP_MARKER}. Do not edit directly"
        f" — update the canonical skill at .claude/skills/{name}/SKILL.md"
        " and regenerate. -->\n\n"
        f"Read and follow `../../../.claude/skills/{name}/SKILL.md`"
        " as the authoritative workflow.\n"
        f"Resolve all relative paths and supporting resources"
        f" from `../../../.claude/skills/{name}/`.\n"
    )
    return name, content


def orphaned_skill_shims(root: Path) -> list[Path]:
    """Find shims in .agents/skills/ with no matching canonical skill."""
    claude_skills = root / ".claude/skills"
    canonical_names = {
        path.parent.name
        for path in claude_skills.glob(SKILL_GLOB)
    } if claude_skills.is_dir() else set()
    agents_dir = root / ".agents/skills"
    if not agents_dir.is_dir():
        return []
    return sorted(
        path
        for path in agents_dir.glob(SKILL_GLOB)
        if path.parent.name not in canonical_names
    )


# ---------------------------------------------------------------------------
# Generation: MCP configs
# ---------------------------------------------------------------------------

def validate_mcp_servers(
    surfaces: list[str] | None = None,
    runtimes: list[str] | None = None,
) -> list[str]:
    """Validate MCP_SERVERS transport/target compatibility."""
    errors: list[str] = []
    seen_names: set[str] = set()
    for server in MCP_SERVERS:
        if not isinstance(server, dict):
            errors.append("MCP_SERVERS: entry is not a dictionary")
            continue
        name = server.get("name")
        if name is None:
            errors.append("MCP_SERVERS: server entry missing required 'name'")
            continue
        if not isinstance(name, str):
            errors.append("MCP_SERVERS: server name must be a string")
            continue
        if not name.strip():
            errors.append("MCP_SERVERS: server name must not be empty or whitespace")
            continue
        if name in seen_names:
            errors.append(f"MCP_SERVERS: duplicate server name '{name}'")
        seen_names.add(name)
        transport = server.get("transport", "")
        if not isinstance(transport, str):
            errors.append(f"MCP server '{name}': transport must be a string")
            continue
        raw_targets = server.get("targets", [])
        if not isinstance(raw_targets, list):
            errors.append(f"MCP server '{name}': targets must be a list")
            continue
        if not all(isinstance(t, str) for t in raw_targets):
            errors.append(f"MCP server '{name}': all targets must be strings")
            continue
        targets = set(raw_targets)

        for target in targets:
            if target not in VALID_MCP_TARGETS:
                errors.append(
                    f"MCP server '{name}': unknown MCP target '{target}'"
                )

        if transport not in TRANSPORT_COMPATIBILITY:
            errors.append(
                f"MCP server '{name}': unknown transport '{transport}'"
            )
            continue

        supported = TRANSPORT_COMPATIBILITY[transport]
        for target in targets:
            if target in VALID_MCP_TARGETS and target not in supported:
                errors.append(
                    f"MCP server '{name}': transport '{transport}' "
                    f"is not supported for target '{target}'"
                )

        cmd = server.get("command")
        url = server.get("url")
        if cmd is not None and not isinstance(cmd, str):
            errors.append(f"MCP server '{name}': command must be a string")
        if url is not None and not isinstance(url, str):
            errors.append(f"MCP server '{name}': url must be a string")
        if transport in ("stdio", "local") and not cmd:
            errors.append(
                f"MCP server '{name}': STDIO/local transport requires 'command'"
            )
        if transport in ("http", "sse") and not url:
            errors.append(
                f"MCP server '{name}': HTTP/SSE transport requires 'url'"
            )
        args_val = server.get("args")
        if args_val is not None:
            if not isinstance(args_val, list):
                errors.append(f"MCP server '{name}': args must be a list")
            elif not all(isinstance(a, str) for a in args_val):
                errors.append(f"MCP server '{name}': all args must be strings")
        cwd_val = server.get("cwd")
        if cwd_val is not None and not isinstance(cwd_val, str):
            errors.append(f"MCP server '{name}': cwd must be a string")
        env = server.get("env")
        if env is not None:
            if not isinstance(env, dict):
                errors.append(f"MCP server '{name}': env must be an object")
            elif not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in env.items()
            ):
                errors.append(
                    f"MCP server '{name}': env keys and values must be strings"
                )

        local_policy = server.get("copilot_local")
        if local_policy is not None and not isinstance(local_policy, dict):
            errors.append(
                f"MCP server '{name}': copilot_local must be an object"
            )
            local_policy = None
        if isinstance(local_policy, dict):
            tools = local_policy.get("tools")
            if tools is not None:
                if not isinstance(tools, list) or not all(
                    isinstance(t, str) for t in tools
                ):
                    errors.append(
                        f"MCP server '{name}': copilot_local.tools must be"
                        " null or a list of strings"
                    )
        if "copilot_local" in targets and "claude" in targets:
            if isinstance(local_policy, dict) and local_policy.get("tools") is not None:
                errors.append(
                    f"MCP server '{name}': copilot_local.tools must be null "
                    "in a shared .mcp.json — a restricted allowlist cannot "
                    "be enforced in the shared file"
                )

        if "copilot_repository" in targets and server.get("oauth"):
            errors.append(
                f"MCP server '{name}': OAuth is not supported for "
                "copilot_repository target"
            )

    copilot_cli_surfaces = {"copilot_cli", "copilot_app"}
    if surfaces is not None and copilot_cli_surfaces & set(surfaces):
        for server in MCP_SERVERS:
            if not isinstance(server, dict):
                continue
            name = server.get("name", "<unnamed>")
            raw = server.get("targets", [])
            if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
                continue
            targets = set(raw)
            if "claude" in targets and "copilot_local" not in targets:
                errors.append(
                    f"WARNING: MCP server '{name}' targets claude only "
                    "but will be visible to Copilot CLI/app via shared "
                    ".mcp.json"
                )

    # Target/scope consistency warnings
    for server in MCP_SERVERS:
        if not isinstance(server, dict):
            continue
        name = server.get("name", "<unnamed>")
        raw_targets = server.get("targets", [])
        if not isinstance(raw_targets, list):
            continue
        for target in raw_targets:
            if target == "codex" and runtimes is not None and "codex" not in runtimes:
                errors.append(
                    f"WARNING: MCP server '{name}' targets codex but "
                    "codex is not in TARGET_RUNTIMES"
                )
            if target == "vscode" and surfaces is not None and "vscode" not in surfaces:
                errors.append(
                    f"WARNING: MCP server '{name}' targets vscode but "
                    "vscode is not in TARGET_SURFACES"
                )

    return errors


def _build_mcp_entry(server: dict[str, Any]) -> dict[str, Any]:
    """Build a server entry dict from an MCP_SERVERS definition."""
    entry: dict[str, Any] = {}
    transport = server.get("transport", "stdio")
    if transport in ("stdio", "local"):
        if server.get("command"):
            entry["command"] = server["command"]
        if server.get("args"):
            entry["args"] = server["args"]
    elif transport in ("http", "sse"):
        if server.get("url"):
            entry["url"] = server["url"]
    if server.get("env"):
        entry["env"] = server["env"]
    return entry


def generate_mcp_json(root: Path) -> dict[Path, str]:
    """Generate .mcp.json for servers targeting claude (including shared)."""
    outputs: dict[Path, str] = {}
    servers: dict[str, Any] = {}

    for server in MCP_SERVERS:
        name = server["name"]
        targets = set(server.get("targets", []))
        if "claude" in targets:
            servers[name] = _build_mcp_entry(server)

    if servers:
        content = json.dumps(
            {"mcpServers": servers},
            indent=2,
            ensure_ascii=False,
        ) + "\n"
        outputs[root / ".mcp.json"] = content

    return outputs


def generate_github_mcp_json(root: Path) -> dict[Path, str]:
    """Generate .github/mcp.json for servers targeting only copilot_local."""
    outputs: dict[Path, str] = {}
    servers: dict[str, Any] = {}

    for server in MCP_SERVERS:
        name = server["name"]
        targets = set(server.get("targets", []))
        if "copilot_local" in targets and "claude" not in targets:
            entry = _build_mcp_entry(server)
            local_policy = server.get("copilot_local", {})
            if isinstance(local_policy, dict) and local_policy.get("tools") is not None:
                entry["tools"] = local_policy["tools"]
            servers[name] = entry

    if servers:
        content = json.dumps(
            {"mcpServers": servers},
            indent=2,
            ensure_ascii=False,
        ) + "\n"
        outputs[root / ".github/mcp.json"] = content

    return outputs


def generate_vscode_mcp_json(root: Path) -> dict[Path, str]:
    """Generate .vscode/mcp.json content."""
    outputs: dict[Path, str] = {}
    vscode_servers: dict[str, Any] = {}

    for server in MCP_SERVERS:
        name = server["name"]
        targets = server.get("targets", [])

        if "vscode" in targets:
            transport = server.get("transport", "stdio")
            entry: dict[str, Any] = {"type": transport}

            if transport in ("stdio", "local"):
                if server.get("command"):
                    entry["command"] = server["command"]
                if server.get("args"):
                    entry["args"] = server["args"]
            elif transport in ("http", "sse"):
                if server.get("url"):
                    entry["url"] = server["url"]

            if server.get("env"):
                entry["env"] = server["env"]

            vscode_servers[name] = entry

    if vscode_servers:
        content = json.dumps(
            {"servers": vscode_servers},
            indent=2,
            ensure_ascii=False,
        ) + "\n"
        outputs[root / ".vscode/mcp.json"] = content

    return outputs


# ---------------------------------------------------------------------------
# Generation: CI parity workflow
# ---------------------------------------------------------------------------

def generate_ci_parity_workflow() -> str:
    """Generate deterministic CI parity workflow YAML."""
    return (
        "# AUTO-GENERATED from CLAUDE.md."
        " Do not edit directly — update CLAUDE.md instead.\n"
        "\n"
        "name: AI Config Parity\n"
        "\n"
        "on:\n"
        "  workflow_call:\n"
        "\n"
        "permissions:\n"
        "  contents: read\n"
        "\n"
        "jobs:\n"
        "  ai-config-parity:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1\n"
        "        with:\n"
        "          persist-credentials: false\n"
        "\n"
        "      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97  # v7.0.0\n"
        "        with:\n"
        '          python-version: "3.12"\n'
        "\n"
        "      - name: Run generator tests\n"
        "        run: python .github/scripts/test_ai_config.py\n"
        "\n"
        "      - name: Check AI config parity\n"
        "        run: python .github/scripts/ai_config.py --check\n"
    )


def generate_ci_parity_caller_workflow() -> str:
    """Generate the pull-request workflow that calls the reusable parity workflow."""
    lines = [
        f"# {OWNERSHIP_MARKER}. Do not edit directly — update CLAUDE.md instead.",
        "",
        "name: AI Config Parity (pull requests)",
        "",
        "on:",
        "  pull_request:",
        "",
        "permissions:",
        "  contents: read",
        "",
        "jobs:",
        "  ai-config-parity:",
        f"    uses: ./{CI_PARITY_WORKFLOW_PATH}",
    ]
    return "\n".join(lines) + "\n"


def generate_copilot_setup_workflow() -> str:
    """Generate the deterministic Copilot cloud-agent setup workflow."""
    lines = [
        f"# {OWNERSHIP_MARKER}. Do not edit directly — update CLAUDE.md instead.",
        "",
        "name: Copilot Setup Steps",
        "",
        "on:",
        "  workflow_dispatch:",
        "",
        "permissions:",
        "  contents: read",
        "",
        "jobs:",
        "  copilot-setup-steps:",
        "    runs-on: ubuntu-latest",
        "    steps:",
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1",
        "        with:",
        "          persist-credentials: false",
    ]
    for step in COPILOT_SETUP_COMMANDS:
        lines.extend([
            "",
            f"      - name: {json.dumps(step['name'], ensure_ascii=False)}",
            "        run: |",
        ])
        lines.extend(f"          {line}" for line in step["run"].split("\n"))
    return "\n".join(lines) + "\n"


def validate_copilot_setup_commands() -> list[str]:
    """Validate cloud-agent command steps before rendering YAML."""
    if not isinstance(COPILOT_SETUP_COMMANDS, list):
        return ["CONFIG TYPE: COPILOT_SETUP_COMMANDS must be a list"]
    errors: list[str] = []
    for index, step in enumerate(COPILOT_SETUP_COMMANDS):
        prefix = f"COPILOT_SETUP_COMMANDS[{index}]"
        if not isinstance(step, dict):
            errors.append(f"{prefix}: must be an object")
            continue
        if set(step) != {"name", "run"}:
            errors.append(f"{prefix}: must contain exactly name and run")
            continue
        for field in ("name", "run"):
            value = step[field]
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{prefix}.{field}: must be a non-empty string")
            elif "\x00" in value or "\r" in value:
                errors.append(f"{prefix}.{field}: contains unsupported control characters")
    return errors


def find_ci_parity_callers(root: Path, workflow_path: str) -> list[str]:
    """Find workflows that call the CI parity workflow via job-level uses."""
    callers: list[str] = []
    workflows_dir = root / ".github/workflows"
    if not workflows_dir.is_dir():
        return callers
    workflow_rel = workflow_path.replace(".github/workflows/", "")
    for pattern in ("*.yml", "*.yaml"):
        for wf in sorted(workflows_dir.glob(pattern)):
            if wf.name == workflow_rel:
                continue
            try:
                content = read_text(wf)
                for line in content.splitlines():
                    # Job-level uses: is indented at exactly 4 spaces
                    # (under jobs.<id>:). Step-level uses: is at 8+ spaces
                    # or prefixed with "- ". Only match job-level.
                    if not line.strip().startswith("uses:"):
                        continue
                    indent = len(line) - len(line.lstrip())
                    if indent > 6:
                        continue
                    stripped = line.strip()
                    if workflow_rel in stripped or workflow_path in stripped:
                        callers.append(wf.relative_to(root).as_posix())
                        break
            except OSError:
                pass
    return callers


def _toml_quote(value: str) -> str:
    """Escape and quote a string for TOML basic string."""
    escaped = (
        value
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("\b", "\\b")
        .replace("\f", "\\f")
    )
    return f'"{escaped}"'


def _toml_key(name: str) -> str:
    """Return a bare TOML key if safe, otherwise a quoted key."""
    if re.match(r"^[A-Za-z0-9_-]+$", name):
        return name
    return _toml_quote(name)


def _toml_array(items: list[str]) -> str:
    """Format a list of strings as a TOML array."""
    return "[" + ", ".join(_toml_quote(v) for v in items) + "]"


def generate_codex_config_toml(root: Path) -> dict[Path, str]:
    """Generate .codex/config.toml content for Codex MCP servers."""
    outputs: dict[Path, str] = {}
    sections: list[str] = []

    for server in MCP_SERVERS:
        name = server["name"]
        targets = server.get("targets", [])

        if "codex" not in targets:
            continue

        transport = server.get("transport", "stdio")
        quoted_name = _toml_key(name)
        lines: list[str] = [f"[mcp_servers.{quoted_name}]"]

        if transport in ("stdio", "local"):
            if server.get("command"):
                lines.append(f"command = {_toml_quote(server['command'])}")
            if server.get("args"):
                lines.append(f"args = {_toml_array(server['args'])}")
            if server.get("cwd"):
                lines.append(f"cwd = {_toml_quote(server['cwd'])}")
        elif transport in ("http", "sse"):
            if server.get("url"):
                lines.append(f"url = {_toml_quote(server['url'])}")

        sections.append("\n".join(lines))

        # Env table (STDIO only)
        if transport in ("stdio", "local") and server.get("env"):
            env_lines = [f"[mcp_servers.{quoted_name}.env]"]
            for key, val in sorted(server["env"].items()):
                env_lines.append(f"{_toml_key(key)} = {_toml_quote(val)}")
            sections.append("\n".join(env_lines))

    if not sections:
        return outputs

    header = (
        f"# {OWNERSHIP_MARKER}. Do not edit directly"
        " — update CLAUDE.md instead.\n"
        "\n"
        'project_doc_fallback_filenames = ["CLAUDE.md"]'
    )
    content = header + "\n\n" + "\n\n".join(sections) + "\n"
    outputs[root / ".codex/config.toml"] = content

    return outputs


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def generate_manifest(
    root: Path,
    artifacts: dict[Path, str],
    runtimes: list[str],
    surfaces: list[str],
    features: list[str],
) -> str:
    """Generate .github/ai-config-manifest.json."""
    entries: list[dict[str, str]] = []
    for path, file_content in sorted(artifacts.items()):
        rel = path.relative_to(root).as_posix()
        path_error = validate_manifest_path(rel)
        if path_error:
            raise ValueError(f"Manifest path rejected: {path_error}")
        entry: dict[str, str] = {"path": rel}
        if rel.endswith(".json") and rel != ".github/ai-config-manifest.json":
            entry["hash"] = content_hash(file_content)
        entries.append(entry)

    manifest: dict[str, Any] = {
        "generatedBy": "ai_config.py",
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "canonicalSource": "CLAUDE.md",
        "runtimes": runtimes,
        "surfaces": surfaces,
        "features": features,
        "copilot_sections": list(COPILOT_SECTIONS),
        "mcp_servers": [
            {
                "name": s["name"],
                "targets": s.get("targets", []),
                "transport": s.get("transport", "stdio"),
            }
            for s in MCP_SERVERS
        ],
        "artifacts": entries,
    }

    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def parse_manifest(content: str) -> dict[str, Any] | None:
    """Parse and validate manifest content, or return None."""
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            return None
        if data.get("generatedBy") != "ai_config.py":
            return None
        if data.get("canonicalSource") != "CLAUDE.md":
            return None
        if type(data.get("schemaVersion")) is not int:
            return None
        if data["schemaVersion"] != MANIFEST_SCHEMA_VERSION:
            return None
        for field in ("runtimes", "surfaces", "features"):
            val = data.get(field)
            if not isinstance(val, list):
                return None
            if not all(isinstance(v, str) for v in val):
                return None
        copilot_sections = data.get("copilot_sections")
        if copilot_sections is not None and (
            not isinstance(copilot_sections, list)
            or not all(isinstance(section, str) for section in copilot_sections)
        ):
            return None
        if not isinstance(data.get("artifacts"), list):
            return None
        for artifact in data["artifacts"]:
            if not isinstance(artifact, dict):
                return None
            if not isinstance(artifact.get("path"), str):
                return None
            path_val = artifact["path"]
            if validate_manifest_path(path_val) is not None:
                return None
            hash_val = artifact.get("hash")
            if path_val.endswith(".json") and path_val != ".github/ai-config-manifest.json":
                if not isinstance(hash_val, str) or not hash_val:
                    return None
            elif hash_val is not None and not isinstance(hash_val, str):
                return None
        mcp_servers = data.get("mcp_servers")
        if not isinstance(mcp_servers, list):
            return None
        for srv in mcp_servers:
            if not isinstance(srv, dict):
                return None
            if not isinstance(srv.get("name"), str):
                return None
            if not isinstance(srv.get("transport"), str):
                return None
            srv_targets = srv.get("targets")
            if not isinstance(srv_targets, list):
                return None
            if not all(isinstance(t, str) for t in srv_targets):
                return None
        return data
    except (json.JSONDecodeError, KeyError, OSError, UnicodeError):
        pass
    return None


def load_manifest(root: Path) -> dict[str, Any] | None:
    """Load and parse the working-tree manifest, or return None."""
    manifest_path = root / ".github/ai-config-manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        return parse_manifest(read_text(manifest_path))
    except (OSError, UnicodeError):
        return None


def load_trusted_manifest(root: Path) -> dict[str, Any] | None:
    """Load the manifest from Git HEAD as the ownership trust anchor.

    An editable working-tree manifest cannot authenticate its own hashes. Only a
    valid manifest committed at HEAD may authorize replacing or deleting JSON
    artifacts whose deterministic content has changed.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "show",
                "HEAD:.github/ai-config-manifest.json",
            ],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="strict",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    if result.returncode != 0:
        return None
    return parse_manifest(result.stdout)


# ---------------------------------------------------------------------------
# Expected generated files
# ---------------------------------------------------------------------------

def expected_generated_files(
    root: Path,
    runtimes: list[str] | None = None,
    surfaces: list[str] | None = None,
    features: list[str] | None = None,
) -> tuple[dict[Path, str], list[str]]:
    """Compute all expected generated artifacts and any errors."""
    if runtimes is None:
        runtimes = ["claude"]
    if surfaces is None:
        surfaces = []
    if features is None:
        features = []

    errors: list[str] = []
    outputs: dict[Path, str] = {}

    # Type-guard active constants before using them
    if not isinstance(runtimes, list) or not all(isinstance(r, str) for r in runtimes):
        return outputs, ["CONFIG TYPE: TARGET_RUNTIMES must be a list of strings"]
    if not isinstance(surfaces, list) or not all(isinstance(s, str) for s in surfaces):
        return outputs, ["CONFIG TYPE: TARGET_SURFACES must be a list of strings"]
    if not isinstance(features, list) or not all(isinstance(f, str) for f in features):
        return outputs, ["CONFIG TYPE: TARGET_FEATURES must be a list of strings"]
    if not isinstance(COPILOT_TITLE, str):
        return outputs, ["CONFIG TYPE: COPILOT_TITLE must be a string"]
    if not isinstance(COPILOT_SECTIONS, list) or not all(isinstance(s, str) for s in COPILOT_SECTIONS):
        return outputs, ["CONFIG TYPE: COPILOT_SECTIONS must be a list of strings"]
    if not isinstance(COPILOT_REQUIRED_SECTIONS, list) or not all(
        isinstance(s, str) for s in COPILOT_REQUIRED_SECTIONS
    ):
        return outputs, ["CONFIG TYPE: COPILOT_REQUIRED_SECTIONS must be a list of strings"]
    if not isinstance(MCP_SERVERS, list):
        return outputs, ["CONFIG TYPE: MCP_SERVERS must be a list"]
    setup_errors = validate_copilot_setup_commands()
    if setup_errors:
        return outputs, setup_errors

    # Validate vocabulary
    all_mcp_targets = [
        t for s in MCP_SERVERS
        if isinstance(s, dict) and isinstance(s.get("targets"), list)
        for t in s["targets"]
        if isinstance(t, str)
    ]
    vocab_errors = validate_vocabulary(runtimes, surfaces, all_mcp_targets)
    if vocab_errors:
        errors.extend(vocab_errors)
        return outputs, errors

    claude_path = root / "CLAUDE.md"
    if not claude_path.is_file():
        errors.append("MISSING: CLAUDE.md")
        return outputs, errors

    try:
        claude_content = read_text(claude_path)
    except (OSError, UnicodeError):
        errors.append("UNREADABLE: CLAUDE.md")
        return outputs, errors

    # Copilot instructions
    copilot_surfaces = {
        "vscode", "jetbrains", "copilot_app", "copilot_cli",
        "cloud_agent", "code_review",
    }
    if copilot_surfaces & set(surfaces):
        try:
            outputs[root / ".github/copilot-instructions.md"] = (
                generate_copilot_instructions(
                    claude_content, surfaces, repo_name=root.name
                )
            )
        except ValueError as error:
            errors.append(f"INVALID: CLAUDE.md: {error}")

    # AGENTS.md adapter
    if "codex" in runtimes:
        agents_path = root / "AGENTS.md"
        if agents_path.is_file():
            try:
                existing = read_text(agents_path)
            except (OSError, UnicodeError):
                errors.append(
                    "UNREADABLE: AGENTS.md — cannot determine ownership"
                )
                existing = ""
            if existing and OWNERSHIP_MARKER not in existing:
                errors.append(
                    "CONFLICT: AGENTS.md exists and is user-authored"
                    " — resolve before regenerating"
                )
            else:
                repo_name = root.name
                outputs[agents_path] = generate_agents_adapter(repo_name)
        else:
            repo_name = root.name
            outputs[agents_path] = generate_agents_adapter(repo_name)

    # Skill shims
    if "codex" in runtimes:
        canonical_skills = sorted(
            (root / ".claude/skills").glob(SKILL_GLOB)
        ) if (root / ".claude/skills").is_dir() else []
        for skill_path in canonical_skills:
            try:
                name, skill_content = expected_skill_shim(skill_path)
            except ValueError as error:
                errors.append(
                    f"INVALID: "
                    f"{skill_path.relative_to(root).as_posix()}: {error}"
                )
                continue
            outputs[root / f".agents/skills/{name}/SKILL.md"] = skill_content

    # MCP configs
    mcp_errors = validate_mcp_servers(surfaces=surfaces, runtimes=runtimes)
    mcp_warnings = [e for e in mcp_errors if e.startswith("WARNING:")]
    mcp_blocking = [e for e in mcp_errors if not e.startswith("WARNING:")]
    errors.extend(mcp_blocking)
    if not mcp_blocking:
        outputs.update(generate_mcp_json(root))
        outputs.update(generate_github_mcp_json(root))
        if "vscode" in surfaces:
            outputs.update(generate_vscode_mcp_json(root))
        if "codex" in runtimes:
            outputs.update(generate_codex_config_toml(root))

    # CI parity workflow
    if "ci_parity" in features:
        outputs[root / CI_PARITY_WORKFLOW_PATH] = generate_ci_parity_workflow()
    if "ci_parity_caller" in features:
        if "ci_parity" not in features:
            errors.append("INVALID: ci_parity_caller requires the ci_parity feature")
        else:
            outputs[root / CI_PARITY_CALLER_PATH] = generate_ci_parity_caller_workflow()

    if "cloud_agent" in surfaces:
        outputs[root / COPILOT_SETUP_WORKFLOW_PATH] = (
            generate_copilot_setup_workflow()
        )

    # Manifest (always last — includes hashes of other artifacts)
    if outputs and not errors:
        manifest_content = generate_manifest(
            root, outputs, runtimes, surfaces, features
        )
        outputs[root / ".github/ai-config-manifest.json"] = manifest_content

    errors.extend(mcp_warnings)
    return outputs, errors


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_generated_files(
    root: Path,
    runtimes: list[str] | None = None,
    surfaces: list[str] | None = None,
    features: list[str] | None = None,
) -> list[str]:
    """Validate generated files against expected content."""
    if runtimes is None:
        if not isinstance(TARGET_RUNTIMES, list):
            return ["CONFIG TYPE: TARGET_RUNTIMES must be a list"]
        runtimes = list(TARGET_RUNTIMES)
    if surfaces is None:
        if not isinstance(TARGET_SURFACES, list):
            return ["CONFIG TYPE: TARGET_SURFACES must be a list"]
        surfaces = list(TARGET_SURFACES)
    if features is None:
        if not isinstance(TARGET_FEATURES, list):
            return ["CONFIG TYPE: TARGET_FEATURES must be a list"]
        features = list(TARGET_FEATURES)

    manifest = load_manifest(root)

    # Detect stale config: manifest declares scope beyond TARGET_* constants
    if manifest and all(isinstance(v, str) for v in runtimes) and all(isinstance(v, str) for v in surfaces) and all(isinstance(v, str) for v in features):
        manifest_runtimes = set(manifest.get("runtimes", []))
        manifest_surfaces = set(manifest.get("surfaces", []))
        manifest_features = set(manifest.get("features", []))
        config_runtimes = set(runtimes)
        config_surfaces = set(surfaces)
        config_features = set(features)
        extra_runtimes = manifest_runtimes - config_runtimes
        extra_surfaces = manifest_surfaces - config_surfaces
        extra_features = manifest_features - config_features
        if extra_runtimes or extra_surfaces or extra_features:
            extras = []
            if extra_runtimes:
                extras.append(
                    f"runtimes: {', '.join(sorted(extra_runtimes))}"
                )
            if extra_surfaces:
                extras.append(
                    f"surfaces: {', '.join(sorted(extra_surfaces))}"
                )
            if extra_features:
                extras.append(
                    f"features: {', '.join(sorted(extra_features))}"
                )
            return [
                "STALE CONFIG: manifest declares scope beyond TARGET_* "
                f"constants ({'; '.join(extras)}) — update "
                "TARGET_RUNTIMES/TARGET_SURFACES/TARGET_FEATURES to match, "
                "or regenerate to narrow scope"
            ]

    expected_files, all_messages = expected_generated_files(
        root, runtimes, surfaces, features
    )
    errors = [m for m in all_messages if not m.startswith("WARNING:")]

    # Reject zero-artifact configurations (same rule as --write)
    non_manifest = {
        p for p in expected_files
        if p.relative_to(root).as_posix() != ".github/ai-config-manifest.json"
    }
    if not non_manifest and not errors:
        return [
            "NO ARTIFACTS: configuration produces no derived files"
            " — specify runtimes and/or surfaces"
        ]

    # Verify manifest presence and validity when artifacts are expected
    manifest_path = root / ".github/ai-config-manifest.json"
    if manifest_path in expected_files:
        if not manifest_path.is_file():
            errors.append("MISSING: .github/ai-config-manifest.json")
        elif manifest is None:
            errors.append(
                "INVALID: .github/ai-config-manifest.json exists but failed"
                " schema validation — regenerate to fix"
            )

    # Orphan detection
    if (root / ".agents/skills").is_dir():
        for orphan in orphaned_skill_shims(root):
            errors.append(
                f"ORPHAN: {orphan.relative_to(root).as_posix()}"
                " has no matching canonical skill"
            )

    # Content drift detection
    for path, expected in expected_files.items():
        relative_path = path.relative_to(root).as_posix()

        if not path.is_file():
            errors.append(f"MISSING: {relative_path}")
            continue

        try:
            actual = read_text(path)
        except (OSError, UnicodeError):
            errors.append(f"UNREADABLE: {relative_path}")
            continue

        # Manifest: deterministic, no timestamps — compare fully
        if relative_path == ".github/ai-config-manifest.json":
            if actual != expected:
                try:
                    actual_manifest = json.loads(actual)
                    if not isinstance(actual_manifest, dict):
                        raise TypeError("manifest is not an object")
                    expected_manifest = json.loads(expected)
                    drifted: list[str] = []
                    for key in (
                        "runtimes", "surfaces", "features",
                        "copilot_sections", "mcp_servers", "artifacts",
                    ):
                        if actual_manifest.get(key) != expected_manifest.get(key):
                            drifted.append(key)
                    if drifted:
                        errors.append(
                            "DRIFT: .github/ai-config-manifest.json"
                            f" — {', '.join(drifted)} does not match"
                            " expected configuration"
                        )
                    else:
                        errors.append(
                            "DRIFT: .github/ai-config-manifest.json"
                            " — content does not match expected output"
                        )
                except (json.JSONDecodeError, KeyError, TypeError,
                        AttributeError):
                    errors.append(
                        "INVALID: .github/ai-config-manifest.json"
                        " — could not parse for comparison"
                    )
            continue
        if actual != expected:
            # For JSON artifacts, check manifest hash for user-modification
            if relative_path.endswith(".json") and manifest:
                for artifact in manifest.get("artifacts", []):
                    if artifact.get("path") == relative_path:
                        stored_hash = artifact.get("hash")
                        if stored_hash and content_hash(actual) != stored_hash:
                            errors.append(
                                f"CONFLICT: {relative_path} has been modified"
                                " (hash mismatch with manifest)"
                                " — will not overwrite"
                            )
                            break
                else:
                    _append_drift(errors, relative_path, actual, expected)
            else:
                _append_drift(errors, relative_path, actual, expected)

    # CI parity caller verification
    if "ci_parity" in features:
        callers = find_ci_parity_callers(root, CI_PARITY_WORKFLOW_PATH)
        if not callers:
            errors.append(
                f"NO CALLER: {CI_PARITY_WORKFLOW_PATH} has no reachable"
                " caller workflow"
            )

    # Feature-artifact orphan detection
    for feature, workflow_path in (
        ("ci_parity", CI_PARITY_WORKFLOW_PATH),
        ("ci_parity_caller", CI_PARITY_CALLER_PATH),
    ):
        if feature in features:
            continue
        ci_workflow = root / workflow_path
        if ci_workflow.is_file():
            try:
                if OWNERSHIP_MARKER in read_text(ci_workflow):
                    errors.append(
                        f"ORPHAN: {workflow_path}"
                        f" — {feature} not in TARGET_FEATURES"
                    )
            except OSError:
                pass

    return errors


def _append_drift(
    errors: list[str],
    relative_path: str,
    actual: str,
    expected: str,
) -> None:
    diff = "".join(
        difflib.unified_diff(
            actual.splitlines(keepends=True),
            expected.splitlines(keepends=True),
            fromfile=relative_path,
            tofile=f"expected/{relative_path}",
        )
    )
    errors.append(
        f"DRIFT: {relative_path} does not match its authoritative"
        f" Claude source\n{diff}"
    )


def validate(
    root: Path,
    runtimes: list[str] | None = None,
    surfaces: list[str] | None = None,
    features: list[str] | None = None,
) -> list[str]:
    """Full validation: generated files."""
    return validate_generated_files(root, runtimes, surfaces, features)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def validate_config(root: Path) -> list[str]:
    """Validate repository-specific constants for correctness."""
    errors: list[str] = []

    if not isinstance(TARGET_RUNTIMES, list):
        errors.append("TARGET_RUNTIMES: must be a list")
        return errors
    if not isinstance(TARGET_SURFACES, list):
        errors.append("TARGET_SURFACES: must be a list")
        return errors
    if not isinstance(TARGET_FEATURES, list):
        errors.append("TARGET_FEATURES: must be a list")
        return errors
    if not isinstance(COPILOT_TITLE, str):
        errors.append("COPILOT_TITLE: must be a string")
        return errors
    if not isinstance(COPILOT_SECTIONS, list):
        errors.append("COPILOT_SECTIONS: must be a list")
        return errors
    if not isinstance(COPILOT_REQUIRED_SECTIONS, list):
        errors.append("COPILOT_REQUIRED_SECTIONS: must be a list")
        return errors
    if not isinstance(MCP_SERVERS, list):
        errors.append("MCP_SERVERS: must be a list")
        return errors
    errors.extend(validate_copilot_setup_commands())

    for r in TARGET_RUNTIMES:
        if not isinstance(r, str):
            errors.append(f"TARGET_RUNTIMES: entries must be strings, got {type(r).__name__}")
            continue
        if r not in VALID_RUNTIMES:
            errors.append(f"TARGET_RUNTIMES: unknown runtime '{r}'")
    for s in TARGET_SURFACES:
        if not isinstance(s, str):
            errors.append(f"TARGET_SURFACES: entries must be strings, got {type(s).__name__}")
            continue
        if s not in VALID_SURFACES:
            errors.append(f"TARGET_SURFACES: unknown surface '{s}'")
    for f in TARGET_FEATURES:
        if not isinstance(f, str):
            errors.append(f"TARGET_FEATURES: entries must be strings, got {type(f).__name__}")
            continue
        if f not in VALID_FEATURES:
            errors.append(f"TARGET_FEATURES: unknown feature '{f}'")
    if "ci_parity_caller" in TARGET_FEATURES and "ci_parity" not in TARGET_FEATURES:
        errors.append("TARGET_FEATURES: ci_parity_caller requires ci_parity")

    try:
        COPILOT_TITLE.format(repo_name="test")
    except (KeyError, IndexError, ValueError) as e:
        errors.append(f"COPILOT_TITLE: invalid format string: {e}")

    for section in COPILOT_SECTIONS:
        if not isinstance(section, str):
            errors.append(f"COPILOT_SECTIONS: entries must be strings, got {type(section).__name__}")
            return errors
    for section in COPILOT_REQUIRED_SECTIONS:
        if not isinstance(section, str):
            errors.append(f"COPILOT_REQUIRED_SECTIONS: entries must be strings, got {type(section).__name__}")
            return errors
    configured = set(COPILOT_SECTIONS)
    for req in COPILOT_REQUIRED_SECTIONS:
        if req not in configured:
            errors.append(
                f"COPILOT_REQUIRED_SECTIONS: '{req}' is not in COPILOT_SECTIONS"
            )

    claude_path = root / "CLAUDE.md"
    if claude_path.is_file():
        content = read_text(claude_path)
        available = set(
            m.group(1).strip()
            for line in content.splitlines()
            if (m := re.match(r"^## (.+)$", line))
        )
        for section in COPILOT_SECTIONS:
            if section not in available:
                errors.append(
                    f"COPILOT_SECTIONS: '{section}' not found in CLAUDE.md"
                )

    errors.extend(validate_mcp_servers())

    # Check that the configuration would produce at least one derived artifact
    str_surfaces = {s for s in TARGET_SURFACES if isinstance(s, str)}
    str_runtimes = {r for r in TARGET_RUNTIMES if isinstance(r, str)}
    has_copilot_surface = bool(VALID_SURFACES & str_surfaces)
    has_codex = "codex" in str_runtimes
    active_mcp_targets = {"claude", "copilot_local"}
    if "codex" in str_runtimes:
        active_mcp_targets.add("codex")
    if "vscode" in str_surfaces:
        active_mcp_targets.add("vscode")
    has_local_mcp = any(
        active_mcp_targets & set(t for t in s["targets"] if isinstance(t, str))
        for s in MCP_SERVERS
        if isinstance(s, dict) and isinstance(s.get("targets"), list)
    )
    has_features = bool(TARGET_FEATURES)
    if not has_copilot_surface and not has_codex and not has_local_mcp and not has_features:
        errors.append(
            "NO ARTIFACTS: current TARGET_* constants produce no derived"
            " files — add runtimes, surfaces, MCP servers, or features"
        )

    return errors


# ---------------------------------------------------------------------------
# Regeneration
# ---------------------------------------------------------------------------

def _check_write_collisions(
    root: Path,
    expected_files: dict[Path, str],
    working_manifest: dict[str, Any] | None,
    trusted_manifest: dict[str, Any] | None,
) -> list[str]:
    """Preflight: verify every existing target is generator-owned.

    JSON ownership: matches new expected content OR a Git-trusted manifest hash.
    Comment-supporting ownership: embedded OWNERSHIP_MARKER.
    """
    errors: list[str] = []
    manifest_hashes: dict[str, str] = {}
    if trusted_manifest:
        for a in trusted_manifest.get("artifacts", []):
            if (isinstance(a, dict) and isinstance(a.get("hash"), str)
                    and isinstance(a.get("path"), str)):
                manifest_hashes[a["path"]] = a["hash"]

    for path in sorted(expected_files):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == ".github/ai-config-manifest.json":
            try:
                existing_manifest = read_text(path)
            except (OSError, UnicodeError):
                existing_manifest = ""
            if existing_manifest == expected_files[path]:
                continue
            if (working_manifest is not None and trusted_manifest is not None
                    and working_manifest == trusted_manifest):
                continue
            errors.append(
                f"COLLISION: {rel} differs from expected output and does not"
                " match the manifest committed at HEAD — commit reviewed"
                " generated state or resolve manually before regenerating"
            )
            continue
        try:
            existing = read_text(path)
        except (OSError, UnicodeError):
            errors.append(
                f"COLLISION: {rel} exists but could not be read"
                " — resolve before regenerating"
            )
            continue
        if rel.endswith(".json"):
            if existing == expected_files[path]:
                continue
            stored = manifest_hashes.get(rel)
            if stored and content_hash(existing) == stored:
                continue
        elif OWNERSHIP_MARKER in existing:
            continue
        errors.append(
            f"COLLISION: {rel} exists and is not generator-owned"
            " — resolve before regenerating"
        )
    return errors


def regenerate(
    root: Path,
    runtimes: list[str] | None = None,
    surfaces: list[str] | None = None,
    features: list[str] | None = None,
) -> list[str]:
    """Regenerate all derived files. Returns errors if any."""
    if runtimes is None:
        if not isinstance(TARGET_RUNTIMES, list):
            return ["CONFIG TYPE: TARGET_RUNTIMES must be a list"]
        runtimes = list(TARGET_RUNTIMES)
    if surfaces is None:
        if not isinstance(TARGET_SURFACES, list):
            return ["CONFIG TYPE: TARGET_SURFACES must be a list"]
        surfaces = list(TARGET_SURFACES)
    if features is None:
        if not isinstance(TARGET_FEATURES, list):
            return ["CONFIG TYPE: TARGET_FEATURES must be a list"]
        features = list(TARGET_FEATURES)

    manifest = load_manifest(root)
    trusted_manifest = load_trusted_manifest(root)

    expected_files, all_messages = expected_generated_files(
        root, runtimes, surfaces, features
    )
    warnings = [m for m in all_messages if m.startswith("WARNING:")]
    errors = [m for m in all_messages if not m.startswith("WARNING:")]
    if errors:
        return errors

    # #17: Reject if no derived artifacts beyond the manifest
    non_manifest = {
        p for p in expected_files
        if p.relative_to(root).as_posix() != ".github/ai-config-manifest.json"
    }
    if not non_manifest:
        return [
            "NO ARTIFACTS: configuration produces no derived files"
            " — specify runtimes and/or surfaces"
        ]

    # #15: Preflight ownership check — abort on user-authored collisions
    collision_errors = _check_write_collisions(
        root, expected_files, manifest, trusted_manifest,
    )
    if collision_errors:
        return collision_errors

    # --- Cleanup plan: compute ALL deletions and conflicts BEFORE any writes ---
    cleanup_plan: list[tuple[Path, str]] = []  # (path, action: "delete"|"conflict")
    cleanup_conflicts: list[str] = []

    # Orphaned skill shims (canonical skill deleted)
    orphan_reported: set[str] = set()
    for orphan in orphaned_skill_shims(root):
        if not orphan.is_file():
            continue
        try:
            orphan_content = read_text(orphan)
        except (OSError, UnicodeError):
            orphan_rel = orphan.relative_to(root).as_posix()
            orphan_reported.add(orphan_rel)
            cleanup_conflicts.append(
                f"CONFLICT: {orphan_rel} is orphaned and could not be read"
                " — verify and remove manually"
            )
            continue
        if OWNERSHIP_MARKER not in orphan_content:
            continue
        orphan_rel = orphan.relative_to(root).as_posix()
        orphan_reported.add(orphan_rel)
        cleanup_conflicts.append(
            f"CONFLICT: {orphan_rel} is orphaned (canonical skill removed)"
            " — verify and remove manually"
        )

    # Artifacts from previous broader scope
    if trusted_manifest:
        manifest_hashes: dict[str, str] = {}
        for a in trusted_manifest.get("artifacts", []):
            if (isinstance(a, dict) and isinstance(a.get("hash"), str)
                    and isinstance(a.get("path"), str)
                    and validate_manifest_path(a["path"]) is None):
                manifest_hashes[a["path"]] = a["hash"]

        previous_artifacts = {
            a["path"]
            for a in trusted_manifest.get("artifacts", [])
            if isinstance(a, dict) and isinstance(a.get("path"), str)
            and validate_manifest_path(a["path"]) is None
        }
        current_artifacts = {
            p.relative_to(root).as_posix() for p in expected_files
        }

        # Reconstruct expected content for known artifact types
        repo_name = root.name
        expected_old: dict[str, str] = {}
        claude_path = root / "CLAUDE.md"
        claude_content = ""
        if claude_path.is_file():
            try:
                claude_content = read_text(claude_path)
            except (OSError, UnicodeError):
                pass
        removed = previous_artifacts - current_artifacts
        expected_old[CI_PARITY_WORKFLOW_PATH] = generate_ci_parity_workflow()
        expected_old[CI_PARITY_CALLER_PATH] = generate_ci_parity_caller_workflow()
        if "AGENTS.md" in removed:
            expected_old["AGENTS.md"] = generate_agents_adapter(repo_name)
        if ".github/copilot-instructions.md" in removed:
            if claude_content:
                try:
                    expected_old[".github/copilot-instructions.md"] = (
                        generate_copilot_instructions(
                            claude_content, surfaces, repo_name=repo_name
                        )
                    )
                except ValueError:
                    pass
        for old_rel in removed:
            shim_match = re.match(
                r"\.agents/skills/([^/]+)/SKILL\.md$", old_rel
            )
            if shim_match:
                skill_path = (
                    root / f".claude/skills/{shim_match.group(1)}/SKILL.md"
                )
                if skill_path.is_file():
                    try:
                        _, shim_content = expected_skill_shim(skill_path)
                        expected_old[old_rel] = shim_content
                    except ValueError:
                        pass

        for old_rel in sorted(removed):
            if old_rel == ".github/ai-config-manifest.json":
                continue
            if old_rel in orphan_reported:
                continue
            old_path = root / old_rel
            if not old_path.is_file():
                continue
            try:
                old_content = read_text(old_path)
            except (OSError, UnicodeError):
                cleanup_conflicts.append(
                    f"CONFLICT: {old_rel} is no longer in scope but could"
                    " not be read — verify and remove manually"
                )
                continue

            if old_rel.endswith(".json"):
                stored = manifest_hashes.get(old_rel)
                if stored and content_hash(old_content) == stored:
                    cleanup_plan.append((old_path, "delete"))
                elif stored:
                    cleanup_conflicts.append(
                        f"CONFLICT: {old_rel} is no longer in scope and has"
                        " been modified — remove manually or restore and"
                        " regenerate"
                    )
                else:
                    cleanup_conflicts.append(
                        f"CONFLICT: {old_rel} is no longer in scope and has"
                        " no manifest hash — verify and remove manually"
                    )
            else:
                if OWNERSHIP_MARKER not in old_content:
                    cleanup_conflicts.append(
                        f"CONFLICT: {old_rel} is no longer in scope and"
                        " ownership marker has been removed — verify and"
                        " remove manually"
                    )
                    continue
                deterministic = expected_old.get(old_rel)
                if deterministic is not None and old_content == deterministic:
                    cleanup_plan.append((old_path, "delete"))
                elif deterministic is not None:
                    cleanup_conflicts.append(
                        f"CONFLICT: {old_rel} is no longer in scope and has"
                        " been modified — remove manually or restore and"
                        " regenerate"
                    )
                else:
                    cleanup_conflicts.append(
                        f"CONFLICT: {old_rel} is no longer in scope"
                        " — verify and remove manually"
                    )

    # Abort BEFORE any writes if there are cleanup conflicts
    if cleanup_conflicts:
        return cleanup_conflicts

    # Execute cleanup (all verified safe) — abort on failure
    for path_to_delete, _ in cleanup_plan:
        try:
            path_to_delete.unlink()
            try:
                path_to_delete.parent.rmdir()
            except OSError:
                pass
        except OSError as error:
            return [
                f"ERROR: could not remove "
                f"{path_to_delete.relative_to(root).as_posix()}: {error}"
            ]

    # Write all generated files
    for path, file_content in sorted(expected_files.items()):
        try:
            write_text(path, file_content)
        except OSError as error:
            return [
                f"ERROR: could not write "
                f"{path.relative_to(root).as_posix()}: {error}"
            ]

    return validate(root, runtimes, surfaces, features)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate and validate AI-agent configuration"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="validate generated files and adapters (read-only)",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="regenerate derived files from CLAUDE.md",
    )
    mode.add_argument(
        "--validate-config",
        action="store_true",
        help="validate repository-specific constants (read-only)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="repository root (defaults to the root containing this script)",
    )
    parser.add_argument(
        "--runtimes",
        nargs="+",
        help="override TARGET_RUNTIMES (e.g. --runtimes claude codex)",
    )
    parser.add_argument(
        "--surfaces",
        nargs="+",
        help="override TARGET_SURFACES (e.g. --surfaces copilot_cli vscode)",
    )
    args = parser.parse_args()

    git_marker = args.root / ".git"
    if not git_marker.exists():
        print(
            f"ERROR: {args.root} does not appear to be a Git repository"
            " (no .git found)",
            file=sys.stderr,
        )
        return 1

    cli_runtimes = args.runtimes or None
    cli_surfaces = args.surfaces or None

    if args.validate_config:
        errors = validate_config(args.root)
    elif args.write:
        errors = regenerate(
            args.root, runtimes=cli_runtimes, surfaces=cli_surfaces
        )
    else:
        errors = validate(
            args.root, runtimes=cli_runtimes, surfaces=cli_surfaces
        )
    blocking = [e for e in errors if not e.startswith("WARNING:")]
    mcp_info: list[str] = []
    if (isinstance(MCP_SERVERS, list) and isinstance(TARGET_SURFACES, list)
            and isinstance(TARGET_RUNTIMES, list)
            and all(isinstance(s, str) for s in TARGET_SURFACES)
            and all(isinstance(r, str) for r in TARGET_RUNTIMES)):
        mcp_info = [
            m for m in validate_mcp_servers(
                surfaces=cli_surfaces or list(TARGET_SURFACES),
                runtimes=cli_runtimes or list(TARGET_RUNTIMES),
            ) if m.startswith("WARNING:")
        ]
    for msg in mcp_info:
        print(msg, file=sys.stderr)
    if blocking:
        for error in blocking:
            print(error, file=sys.stderr)
        if args.check:
            print(
                "Regenerate derived files with:"
                " python .github/scripts/ai_config.py --write",
                file=sys.stderr,
            )
        return 1

    if args.validate_config:
        print("Configuration constants are valid.")
    elif args.write:
        print("AI config regenerated and validated.")
    else:
        print("AI config parity check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

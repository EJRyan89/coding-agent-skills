#!/usr/bin/env python3
"""Deterministic read-only audit engine for AI agent configuration.

Owned by the audit-ai-config skill. The init-ai-config skill may invoke this
for post-generation verification but does not contain a copy of this logic.
This engine works independently and does not depend on init-ai-config files.

Usage:
    python audit_ai_config.py [--json] [--root <path>]

Exit codes:
    0  Compliant within statically verifiable scope
    1  One or more ERROR-level findings
    2  INCONCLUSIVE — ambiguous authority model
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from collections import Counter
from pathlib import Path, PurePosixPath
import re
import sys
import tomllib
from dataclasses import dataclass, field, asdict
from typing import Any


# ---------------------------------------------------------------------------
# Finding model
# ---------------------------------------------------------------------------

SEVERITY_ORDER = {"ERROR": 0, "WARNING": 1, "INFO": 2}

OWNERSHIP_MARKER = "AUTO-GENERATED from CLAUDE.md"

# Canonical vocabulary — must match the generator's definitions.
VALID_RUNTIMES: set[str] = {"claude", "codex"}
VALID_SURFACES: set[str] = {
    "copilot_cli", "copilot_app", "vscode", "jetbrains",
    "cloud_agent", "code_review",
}
VALID_MCP_TARGETS: set[str] = {
    "claude", "codex", "copilot_local", "vscode", "copilot_repository",
}

# This engine must remain standalone after deployment. The repository-level
# cross-skill contract test enforces parity with init-ai-config's generator.
TRANSPORT_COMPATIBILITY: dict[str, set[str]] = {
    "stdio": {"claude", "codex", "copilot_local", "vscode", "copilot_repository"},
    "local": {"copilot_local", "copilot_repository"},
    "http": {"claude", "codex", "copilot_local", "vscode", "copilot_repository"},
    "sse": {"claude", "copilot_local", "vscode", "copilot_repository"},
}


@dataclass
class Finding:
    severity: str  # ERROR, WARNING, INFO
    check: str
    path: str | None = None
    line: int | None = None
    message: str = ""
    detail: str = ""

    def sort_key(self) -> tuple[int, str, int]:
        return (
            SEVERITY_ORDER.get(self.severity, 99),
            self.path or "",
            self.line or 0,
        )


@dataclass
class AuditResult:
    repository: str = ""
    authority: str = "unknown"
    scope_status: str = "not-applicable"
    findings: list[Finding] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.authority in ("ambiguous", "unconfigured", "alternative"):
            return 2
        if any(f.severity == "ERROR" for f in self.findings):
            return 1
        return 0

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=Finding.sort_key)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


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
    ".github/skills/*/SKILL.md",
    ".github/agents/*.md",
    ".claude/agents/*.md",
]

COPILOT_BUILTIN_NAMES: set[str] = {"code-review"}
COPILOT_ROLE_BY_SURFACE: dict[str, str] = {
    "copilot_cli": "full_local_host",
    "copilot_app": "full_local_host",
    "cloud_agent": "deferred_remote_worker",
    "code_review": "advisory_evidence_only",
}
VALID_COPILOT_ROLES = set(COPILOT_ROLE_BY_SURFACE.values())


def validate_manifest_path(path_str: str) -> str | None:
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
    if path_str.startswith(".github/skills/"):
        if len(path_str.split("/")) != 4:
            return f"skill projection path must have exactly one skill-name segment: {path_str}"
    if path_str.startswith((".github/agents/", ".claude/agents/")):
        if len(path_str.split("/")) != 3 or not path_str.endswith(".md"):
            return f"agent projection must be a direct Markdown file: {path_str}"
    return None


def has_ownership_marker(content: str) -> bool:
    return OWNERSHIP_MARKER in content


def manifest_mcp_targets(manifest: dict[str, Any]) -> set[str]:
    """Extract the set of MCP targets declared in the manifest."""
    targets: set[str] = set()
    servers = manifest.get("mcp_servers", [])
    if not isinstance(servers, list):
        return targets
    for server in servers:
        if not isinstance(server, dict):
            continue
        server_targets = server.get("targets", [])
        if isinstance(server_targets, list):
            targets.update(t for t in server_targets if isinstance(t, str))
    return targets


def validate_manifest_vocabulary(manifest: dict[str, Any]) -> list[Finding]:
    """Check that manifest runtimes, surfaces, and MCP targets use canonical names."""
    findings: list[Finding] = []
    runtimes = manifest.get("runtimes", [])
    if isinstance(runtimes, list):
        for r in runtimes:
            if isinstance(r, str) and r not in VALID_RUNTIMES:
                findings.append(Finding(
                    severity="ERROR",
                    check="vocabulary",
                    path=".github/ai-config-manifest.json",
                    message=f"Unknown runtime in manifest: '{r}'",
                ))
    surfaces = manifest.get("surfaces", [])
    if isinstance(surfaces, list):
        for s in surfaces:
            if isinstance(s, str) and s not in VALID_SURFACES:
                findings.append(Finding(
                    severity="ERROR",
                    check="vocabulary",
                    path=".github/ai-config-manifest.json",
                    message=f"Unknown surface in manifest: '{s}'",
                ))
    for t in manifest_mcp_targets(manifest):
        if isinstance(t, str) and t not in VALID_MCP_TARGETS:
            findings.append(Finding(
                severity="ERROR",
                check="vocabulary",
                path=".github/ai-config-manifest.json",
                message=f"Unknown MCP target in manifest: '{t}'",
            ))
    return findings


def is_redirecting_agents_md(content: str) -> bool:
    """Check if AGENTS.md is an adapter that redirects to CLAUDE.md."""
    lower = content.lower()
    return "claude.md" in lower and (
        "read and follow" in lower
        or "authoritative source" in lower
    )


# ---------------------------------------------------------------------------
# Check 1: Inventory
# ---------------------------------------------------------------------------

# Fixed and recursively discovered configuration files. init-ai-config's inventory
# keeps an identical copy; tests/ai-config/test_cross_skill_contracts.py enforces parity.
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


def check_inventory(root: Path) -> list[Finding]:
    findings: list[Finding] = []

    for rel_path in INVENTORY_FILES:
        full = root / rel_path
        if full.is_file():
            findings.append(Finding(
                severity="INFO",
                check="inventory",
                path=rel_path,
                message=f"Found {rel_path}",
            ))

    # Recursive scans
    for name in RECURSIVE_INVENTORY_NAMES:
        for found in root.rglob(name):
            if ".git" in found.parts:
                continue
            rel = found.relative_to(root).as_posix()
            findings.append(Finding(
                severity="INFO",
                check="inventory",
                path=rel,
                message=f"Found {rel}",
            ))

    # Skills directories
    for skill_dir in (".claude/skills", ".agents/skills", ".github/skills"):
        d = root / skill_dir
        if d.is_dir():
            skills = [p.parent.name for p in d.glob("*/SKILL.md")]
            if skills:
                findings.append(Finding(
                    severity="INFO",
                    check="inventory",
                    path=skill_dir,
                    message=f"Skills found: {', '.join(sorted(skills))}",
                ))

    for agent_dir in (".github/agents", ".claude/agents"):
        d = root / agent_dir
        if d.is_dir():
            agents = [p.name for p in d.iterdir() if p.is_file()]
            if agents:
                findings.append(Finding(
                    severity="INFO", check="inventory", path=agent_dir,
                    message=f"Custom agent files found: {', '.join(sorted(agents))}",
                ))

    # Path-specific Copilot instructions
    instructions_dir = root / ".github/instructions"
    if instructions_dir.is_dir():
        instr_files = list(instructions_dir.rglob("*.instructions.md"))
        if instr_files:
            findings.append(Finding(
                severity="INFO",
                check="inventory",
                path=".github/instructions",
                message=f"{len(instr_files)} path-specific instruction file(s)",
            ))

    # Generator scripts
    for candidate in GENERATOR_CANDIDATES:
        if (root / candidate).is_file():
            findings.append(Finding(
                severity="INFO",
                check="inventory",
                path=candidate,
                message=f"Generator script found: {candidate}",
            ))

    # AI parity workflows
    workflows_dir = root / ".github/workflows"
    if workflows_dir.is_dir():
        for pattern in ("*.yml", "*.yaml"):
            for wf in workflows_dir.glob(pattern):
                try:
                    wf_content = read_text(wf)
                    if "ai-config" in wf_content.lower() or "ai_config" in wf_content.lower():
                        rel = wf.relative_to(root).as_posix()
                        findings.append(Finding(
                            severity="INFO",
                            check="inventory",
                            path=rel,
                            message="AI config parity workflow found",
                        ))
                except (OSError, UnicodeError):
                    pass

    return findings


# ---------------------------------------------------------------------------
# Copilot skills, agents, projections, and roles
# ---------------------------------------------------------------------------

def _frontmatter(path: Path) -> tuple[dict[str, str], list[Finding]]:
    """Read simple YAML frontmatter without executing or loading YAML tags."""
    rel = path.as_posix()
    try:
        lines = read_text(path).splitlines()
    except (OSError, UnicodeError) as error:
        return {}, [Finding("ERROR", "copilot-config", rel, message=f"Could not read: {error}")]
    if not lines or lines[0] != "---":
        return {}, [Finding("ERROR", "copilot-config", rel, message="Missing YAML frontmatter")]
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}, [Finding("ERROR", "copilot-config", rel, message="Unterminated YAML frontmatter")]
    values: dict[str, str] = {}
    findings: list[Finding] = []
    for offset, line in enumerate(lines[1:end], 2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*?)\s*$", line)
        if not match:
            findings.append(Finding("ERROR", "copilot-config", rel, offset,
                                    "Frontmatter must use single-line key: value entries"))
            continue
        key, value = match.groups()
        if key in values:
            findings.append(Finding("ERROR", "copilot-config", rel, offset,
                                    f"Duplicate frontmatter key '{key}'"))
        values[key] = value
    return values, findings


def _identifier_from_agent_filename(path: Path) -> str:
    name = path.name
    return name[:-9] if name.endswith(".agent.md") else name[:-3]


def _validate_skill(path: Path, root: Path) -> list[Finding]:
    rel = path.relative_to(root).as_posix()
    values, findings = _frontmatter(path)
    for finding in findings:
        finding.path = rel
    name = values.get("name", "")
    directory_name = path.parent.name
    if not name:
        findings.append(Finding("ERROR", "copilot-skill", rel, message="Skill frontmatter requires name"))
    elif name != directory_name or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        findings.append(Finding("ERROR", "copilot-skill", rel,
                                message="Skill name must be lowercase hyphenated and match its directory"))
    if not values.get("description", "").strip():
        findings.append(Finding("ERROR", "copilot-skill", rel, message="Skill frontmatter requires description"))
    if "allowed-tools" in values and not values["allowed-tools"].strip():
        findings.append(Finding("ERROR", "copilot-skill", rel, message="allowed-tools must not be empty"))
    if directory_name in COPILOT_BUILTIN_NAMES:
        findings.append(Finding("ERROR", "collision", rel,
                                message=f"Repository skill '{directory_name}' collides with a Copilot built-in agent"))
    return findings


def _validate_agent(path: Path, root: Path) -> list[Finding]:
    rel = path.relative_to(root).as_posix()
    findings: list[Finding] = []
    if not (path.name.endswith(".agent.md") or path.suffix == ".md"):
        return [Finding("ERROR", "copilot-agent", rel,
                        message="Custom agent filenames must end in .md or .agent.md")]
    agent_id = _identifier_from_agent_filename(path)
    if not agent_id or not re.fullmatch(r"[A-Za-z0-9._-]+", agent_id):
        findings.append(Finding("ERROR", "copilot-agent", rel,
                                message="Custom agent filename contains unsupported characters"))
    values, frontmatter_findings = _frontmatter(path)
    for finding in frontmatter_findings:
        finding.path = rel
    findings.extend(frontmatter_findings)
    if not values.get("description", "").strip():
        findings.append(Finding("ERROR", "copilot-agent", rel, message="Agent frontmatter requires description"))
    target = values.get("target")
    if target is not None and target not in {"vscode", "github-copilot"}:
        findings.append(Finding("ERROR", "copilot-agent", rel,
                                message="target must be 'vscode' or 'github-copilot'"))
    for key in ("include-custom-instructions", "infer", "disable-model-invocation", "user-invocable"):
        if key in values and values[key] not in {"true", "false"}:
            findings.append(Finding("ERROR", "copilot-agent", rel,
                                    message=f"{key} must be a boolean"))
    if "tools" in values:
        raw_tools = values["tools"]
        if raw_tools.startswith("["):
            try:
                tools = json.loads(raw_tools)
            except json.JSONDecodeError:
                tools = None
            if not isinstance(tools, list) or not tools or not all(isinstance(tool, str) and tool for tool in tools):
                findings.append(Finding("ERROR", "copilot-agent", rel,
                                        message="tools must be a non-empty string list or comma-separated string"))
        elif not raw_tools.strip():
            findings.append(Finding("ERROR", "copilot-agent", rel, message="tools must not be empty"))
    if "modelPolicy" in values and values["modelPolicy"] not in {"preferred", "required"}:
        findings.append(Finding("ERROR", "copilot-agent", rel,
                                message="modelPolicy must be 'preferred' or 'required'"))
    if agent_id.lower() in COPILOT_BUILTIN_NAMES:
        findings.append(Finding("ERROR", "collision", rel,
                                message=f"Custom agent '{agent_id}' collides with a Copilot built-in agent"))
    return findings


def check_copilot_configuration(root: Path, manifest: dict[str, Any]) -> list[Finding]:
    """Statically validate Copilot-discovered repository skills and agents."""
    findings: list[Finding] = []
    manifest_paths = {
        artifact.get("path") for artifact in manifest.get("artifacts", [])
        if isinstance(artifact, dict) and isinstance(artifact.get("path"), str)
    }
    for skill_dir in (".github/skills", ".claude/skills", ".agents/skills"):
        directory = root / skill_dir
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("SKILL.md")):
            findings.extend(_validate_skill(path, root))
            rel = path.relative_to(root).as_posix()
            if skill_dir == ".github/skills" and (has_ownership_marker(read_text(path)) or rel in manifest_paths):
                if rel not in manifest_paths:
                    findings.append(Finding("ERROR", "provenance", rel,
                                            message="Generated Copilot skill projection is not owned by the manifest"))
                elif not has_ownership_marker(read_text(path)):
                    findings.append(Finding("ERROR", "provenance", rel,
                                            message="Manifest-owned Copilot skill projection lacks an ownership marker"))
    for agent_dir in (".github/agents", ".claude/agents"):
        directory = root / agent_dir
        if not directory.is_dir():
            continue
        for path in sorted(p for p in directory.iterdir() if p.is_file()):
            findings.extend(_validate_agent(path, root))
            rel = path.relative_to(root).as_posix()
            content = read_text(path)
            if has_ownership_marker(content) or rel in manifest_paths:
                if rel not in manifest_paths:
                    findings.append(Finding("ERROR", "provenance", rel,
                                            message="Generated Copilot agent projection is not owned by the manifest"))
                elif not has_ownership_marker(content):
                    findings.append(Finding("ERROR", "provenance", rel,
                                            message="Manifest-owned Copilot agent projection lacks an ownership marker"))
    return findings


def derive_generator_scope(root: Path) -> tuple[dict[str, list[str]] | None, str]:
    """Safely extract literal TARGET_* declarations from the local generator."""
    for candidate in GENERATOR_CANDIDATES:
        path = root / candidate
        if not path.is_file():
            continue
        try:
            tree = ast.parse(read_text(path), filename=candidate)
        except (SyntaxError, OSError, UnicodeError):
            return None, "generator-scope-unreadable"
        values: dict[str, list[str]] = {}
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {"TARGET_RUNTIMES", "TARGET_SURFACES", "TARGET_FEATURES"}:
                    value = node.value
                    try:
                        literal = ast.literal_eval(value)
                    except (ValueError, TypeError):
                        return None, "generator-scope-not-literal"
                    if not isinstance(literal, list) or not all(isinstance(item, str) for item in literal):
                        return None, "generator-scope-invalid"
                    values[target.id] = literal
        if len(values) != 3:
            return None, "generator-scope-incomplete"
        return {"runtimes": values["TARGET_RUNTIMES"], "surfaces": values["TARGET_SURFACES"], "features": values["TARGET_FEATURES"]}, "independently-derived"
    return None, "manifest-declared-only"


def check_scope_and_roles(root: Path, manifest: dict[str, Any]) -> tuple[str, list[Finding]]:
    findings: list[Finding] = []
    derived, status = derive_generator_scope(root)
    if derived is None and not (root / ".github/ai-config-manifest.json").is_file():
        # With no manifest there is no editable scope that could narrow checks; say what was skipped.
        status = "no-declared-scope"
        findings.append(Finding("INFO", "scope", message=(
            "No AI-config manifest or recognized generator declares target runtimes or surfaces; "
            "target-specific checks were not run")))
    elif derived is None:
        findings.append(Finding("WARNING", "scope", message=(
            "Audit scope is manifest-declared only; editable manifest scope can suppress checks "
            f"({status})")))
    else:
        for field, expected in derived.items():
            actual = manifest.get(field)
            if actual != expected:
                findings.append(Finding("ERROR", "scope", ".github/ai-config-manifest.json",
                                        message=f"Manifest {field} differs from independently derived generator scope"))
        status = "independently-derived"
    roles = manifest.get("runtimeRoles")
    copilot_surfaces = set(manifest.get("surfaces", [])) & set(COPILOT_ROLE_BY_SURFACE)
    if copilot_surfaces and not isinstance(roles, dict):
        findings.append(Finding("WARNING", "runtime-role", ".github/ai-config-manifest.json",
                                message="Copilot surfaces lack explicit runtimeRoles declarations"))
    elif isinstance(roles, dict):
        for surface in sorted(copilot_surfaces):
            role = roles.get(surface)
            expected_role = COPILOT_ROLE_BY_SURFACE[surface]
            if role not in VALID_COPILOT_ROLES:
                findings.append(Finding("ERROR", "runtime-role", ".github/ai-config-manifest.json",
                                        message=f"{surface} has an unknown runtime role"))
            elif role != expected_role:
                findings.append(Finding("ERROR", "runtime-role", ".github/ai-config-manifest.json",
                                        message=f"{surface} must declare role '{expected_role}', not '{role}'"))
    return status, findings


# ---------------------------------------------------------------------------
# Check 2: Authority classification
# ---------------------------------------------------------------------------

def _validate_manifest_schema(data: dict[str, Any]) -> list[str]:
    """Validate manifest structure recursively. Returns error messages."""
    errors: list[str] = []
    if type(data.get("schemaVersion")) is not int:
        errors.append("schemaVersion must be an integer")
    elif data["schemaVersion"] != 1:
        errors.append(f"schemaVersion {data['schemaVersion']} is not supported (expected 1)")
    for field in ("runtimes", "surfaces", "features"):
        val = data.get(field)
        if val is None:
            errors.append(f"{field} is required")
            continue
        if not isinstance(val, list):
            errors.append(f"{field} must be a list")
            continue
        for i, item in enumerate(val):
            if not isinstance(item, str):
                errors.append(f"{field}[{i}] must be a string")
    artifacts = data.get("artifacts")
    if artifacts is None:
        errors.append("artifacts is required")
    elif not isinstance(artifacts, list):
        errors.append("artifacts must be a list")
    else:
        for i, art in enumerate(artifacts):
            if not isinstance(art, dict):
                errors.append(f"artifacts[{i}] must be an object")
                continue
            if "path" not in art or not isinstance(art.get("path"), str):
                errors.append(f"artifacts[{i}].path must be a string")
            path_val = art.get("path")
            if not isinstance(path_val, str):
                continue
            hash_val = art.get("hash")
            is_json = path_val.endswith(".json") and path_val != ".github/ai-config-manifest.json"
            if is_json:
                if not isinstance(hash_val, str) or not hash_val:
                    errors.append(f"artifacts[{i}].hash is required for JSON artifact '{path_val}'")
            elif hash_val is not None and not isinstance(hash_val, str):
                errors.append(f"artifacts[{i}].hash must be a string or absent")
    mcp_servers = data.get("mcp_servers")
    if mcp_servers is None:
        errors.append("mcp_servers is required")
    elif not isinstance(mcp_servers, list):
        errors.append("mcp_servers must be a list")
    else:
        for i, srv in enumerate(mcp_servers):
            if not isinstance(srv, dict):
                errors.append(f"mcp_servers[{i}] must be an object")
                continue
            srv_name = srv.get("name")
            if not isinstance(srv_name, str) or not srv_name.strip():
                errors.append(f"mcp_servers[{i}].name must be a non-empty string")
            srv_transport = srv.get("transport")
            if not isinstance(srv_transport, str):
                errors.append(f"mcp_servers[{i}].transport must be a string")
            targets = srv.get("targets")
            if not isinstance(targets, list):
                errors.append(f"mcp_servers[{i}].targets must be a list")
            else:
                for j, t in enumerate(targets):
                    if not isinstance(t, str):
                        errors.append(
                            f"mcp_servers[{i}].targets[{j}] must be a string"
                        )
    roles = data.get("runtimeRoles")
    if roles is not None:
        if not isinstance(roles, dict):
            errors.append("runtimeRoles must be an object when present")
        else:
            for surface, role in roles.items():
                if surface not in COPILOT_ROLE_BY_SURFACE:
                    errors.append(f"runtimeRoles has unknown Copilot surface '{surface}'")
                if not isinstance(role, str):
                    errors.append(f"runtimeRoles.{surface} must be a string")
    copilot_sections = data.get("copilot_sections")
    if copilot_sections is not None and (
        not isinstance(copilot_sections, list)
        or not all(isinstance(section, str) for section in copilot_sections)
    ):
        errors.append("copilot_sections must be a list of strings when present")
    return errors


def classify_authority(root: Path) -> tuple[str, dict[str, Any], list[Finding]]:
    """Return (classification, manifest_data_or_empty, findings)."""
    findings: list[Finding] = []
    signals: list[str] = []
    manifest_data: dict[str, Any] = {}

    # Signal 1: Valid manifest
    manifest_path = root / ".github/ai-config-manifest.json"
    if manifest_path.is_file():
        try:
            data = json.loads(read_text(manifest_path))
            if not isinstance(data, dict):
                findings.append(Finding(
                    severity="WARNING",
                    check="authority",
                    path=".github/ai-config-manifest.json",
                    message="Manifest is not a JSON object",
                ))
            elif data.get("generatedBy") == "ai_config.py" and data.get("canonicalSource"):
                canonical = data["canonicalSource"]
                if canonical == "CLAUDE.md":
                    schema_errors = _validate_manifest_schema(data)
                    if schema_errors:
                        for msg in schema_errors:
                            findings.append(Finding(
                                severity="ERROR",
                                check="authority",
                                path=".github/ai-config-manifest.json",
                                message=f"Manifest schema: {msg}",
                            ))
                    elif not (root / canonical).is_file():
                        findings.append(Finding(
                            severity="ERROR",
                            check="authority",
                            path=".github/ai-config-manifest.json",
                            message=f"Manifest declares canonical source"
                                    f" '{canonical}' but it does not exist",
                        ))
                    elif not any(
                        isinstance(a, dict)
                        and isinstance(a.get("path"), str)
                        and a["path"] != ".github/ai-config-manifest.json"
                        for a in data.get("artifacts", [])
                    ):
                        findings.append(Finding(
                            severity="ERROR",
                            check="authority",
                            path=".github/ai-config-manifest.json",
                            message="Manifest declares no derived artifacts",
                        ))
                    else:
                        signals.append("manifest")
                        manifest_data = data
                else:
                    findings.append(Finding(
                        severity="INFO",
                        check="authority",
                        path=".github/ai-config-manifest.json",
                        message=f"Manifest declares alternative canonical source: {canonical}",
                    ))
                    return "alternative", data if isinstance(data, dict) else {}, findings
            elif data.get("generatedBy") and data.get("canonicalSource"):
                canonical = data["canonicalSource"]
                if canonical != "CLAUDE.md":
                    findings.append(Finding(
                        severity="INFO",
                        check="authority",
                        path=".github/ai-config-manifest.json",
                        message=f"Manifest declares alternative canonical source: {canonical}",
                    ))
                    return "alternative", data, findings
        except (json.JSONDecodeError, KeyError, OSError, UnicodeError):
            findings.append(Finding(
                severity="WARNING",
                check="authority",
                path=".github/ai-config-manifest.json",
                message="Manifest exists but is malformed",
            ))

    # Signal 2: CLAUDE.md exists
    claude_path = root / "CLAUDE.md"
    if claude_path.is_file():
        signals.append("claude_md_exists")

        # Signal 3: "Maintaining AI Agent Config" section
        try:
            content = read_text(claude_path)
            if re.search(
                r"^##\s+Maintaining\s+AI\s+Agent\s+Config",
                content,
                re.MULTILINE | re.IGNORECASE,
            ):
                signals.append("maintaining_section")
        except (OSError, UnicodeError):
            pass

    # Signal 4: Generator script referencing CLAUDE.md
    for candidate in GENERATOR_CANDIDATES:
        gen_path = root / candidate
        if gen_path.is_file():
            try:
                gen_content = read_text(gen_path)
                if "CLAUDE.md" in gen_content:
                    signals.append("generator_script")
                    break
            except (OSError, UnicodeError):
                pass

    # Signal 5: CI parity workflow referencing generator
    workflows_dir = root / ".github/workflows"
    ci_found = False
    if workflows_dir.is_dir():
        for pattern in ("*.yml", "*.yaml"):
            if ci_found:
                break
            for wf in workflows_dir.glob(pattern):
                try:
                    wf_content = read_text(wf)
                    if "ai_config" in wf_content and "--check" in wf_content:
                        signals.append("ci_parity")
                        ci_found = True
                        break
                except (OSError, UnicodeError):
                    pass

    # Classify
    if "manifest" in signals:
        classification = "conforming"
    elif len([s for s in signals if s != "manifest"]) >= 2:
        classification = "conforming"
    elif len(signals) == 0:
        classification = "unconfigured"
    else:
        classification = "ambiguous"

    findings.append(Finding(
        severity="INFO",
        check="authority",
        message=f"Authority classification: {classification} "
                f"(signals: {', '.join(signals) or 'none'})",
    ))

    return classification, manifest_data, findings


# ---------------------------------------------------------------------------
# Check 3: Parity validation
# ---------------------------------------------------------------------------

DEFAULT_COPILOT_SECTIONS: list[str] = [
    "Overview",
    "Build and Test Commands",
    "Formatting Rules",
    "CI / Quality Gates",
]

COPILOT_BANNER = (
    "> AUTO-GENERATED from CLAUDE.md. Do not edit directly"
    " — update CLAUDE.md instead."
)


def _extract_sections(content: str, section_names: list[str]) -> str:
    """Extract named H2 sections from Markdown content."""
    lines = content.splitlines()
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        m = re.match(r"^## (.+)$", line)
        if m:
            current = m.group(1).strip()
            if current not in sections:
                sections[current] = []
            sections[current].append(line)
        elif current and current in sections:
            sections[current].append(line)
    parts: list[str] = []
    for name in section_names:
        if name in sections:
            text = "\n".join(sections[name]).strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def _expected_copilot_sections_body(
    root: Path, manifest: dict[str, Any]
) -> str | None:
    """Return the expected sections body for copilot-instructions parity.

    Only the projected sections are compared — the title line is
    repository-specific and allowed to vary.
    """
    claude_path = root / "CLAUDE.md"
    if not claude_path.is_file():
        return None
    try:
        claude_content = read_text(claude_path)
    except (OSError, UnicodeError):
        return None
    section_names = manifest.get("copilot_sections", DEFAULT_COPILOT_SECTIONS)
    if not isinstance(section_names, list) or not all(
        isinstance(section, str) for section in section_names
    ):
        return None
    projected = _extract_sections(claude_content, section_names)
    if not projected:
        return None
    return projected


def _expected_agents_adapter(repo_name: str) -> str:
    """Reconstruct the deterministic AGENTS.md adapter for parity comparison."""
    return (
        f"<!-- {OWNERSHIP_MARKER}. Do not edit directly"
        " — update CLAUDE.md instead. -->\n\n"
        f"# {repo_name}\n\n"
        "Read and follow `CLAUDE.md` as the authoritative source for all "
        "repository instructions, conventions, and workflows.\n\n"
        "All build commands, formatting rules, test conventions, architecture "
        "guidance, and behavioral constraints are maintained in `CLAUDE.md`. "
        "Do not duplicate or contradict its content here.\n"
    )


def _expected_skill_shim(
    root: Path, skill_name: str,
) -> str | None:
    """Reconstruct the deterministic shim content for a skill, or None."""
    canonical = root / f".claude/skills/{skill_name}/SKILL.md"
    if not canonical.is_file():
        return None
    try:
        lines = read_text(canonical).splitlines()
    except (OSError, UnicodeError):
        return None
    if not lines or lines[0] != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    fm = lines[1:end]
    name_lines = [l for l in fm if l.startswith("name:")]
    desc_lines = [l for l in fm if l.startswith("description:")]
    if len(name_lines) != 1 or len(desc_lines) != 1:
        return None
    return (
        "---\n"
        f"{name_lines[0]}\n"
        f"{desc_lines[0]}\n"
        "---\n\n"
        f"<!-- {OWNERSHIP_MARKER}. Do not edit directly"
        f" — update the canonical skill at .claude/skills/{skill_name}/SKILL.md"
        " and regenerate. -->\n\n"
        f"Read and follow `../../../.claude/skills/{skill_name}/SKILL.md`"
        " as the authoritative workflow.\n"
        f"Resolve all relative paths and supporting resources"
        f" from `../../../.claude/skills/{skill_name}/`.\n"
    )


def check_parity(
    root: Path,
    manifest: dict[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []

    artifacts = manifest.get("artifacts", [])
    for artifact in artifacts:
        path_str = artifact.get("path", "")

        # Validate path safety
        path_error = validate_manifest_path(path_str)
        if path_error:
            findings.append(Finding(
                severity="ERROR",
                check="parity",
                path=path_str,
                message=f"Manifest path rejected: {path_error}",
            ))
            continue

        full_path = root / path_str
        if not full_path.is_file():
            findings.append(Finding(
                severity="ERROR",
                check="parity",
                path=path_str,
                message=f"Generated artifact missing: {path_str}",
            ))
            continue

        try:
            actual_content = read_text(full_path)
        except (OSError, UnicodeError):
            findings.append(Finding(
                severity="ERROR",
                check="parity",
                path=path_str,
                message="Generated artifact exists but could not be read",
            ))
            continue

        # JSON artifacts: check hash
        stored_hash = artifact.get("hash")
        if stored_hash:
            actual_hash = content_hash(actual_content)
            if actual_hash != stored_hash:
                findings.append(Finding(
                    severity="ERROR",
                    check="parity",
                    path=path_str,
                    message="JSON artifact modified (hash mismatch with manifest)",
                ))
                continue

        # Comment-supporting formats: ownership marker + deterministic content
        if not path_str.endswith(".json"):
            if not has_ownership_marker(actual_content):
                findings.append(Finding(
                    severity="ERROR",
                    check="parity",
                    path=path_str,
                    message="Generated file missing ownership marker",
                ))
                continue

            # Deterministic content comparison for known artifact types
            if path_str == ".github/copilot-instructions.md":
                expected_body = _expected_copilot_sections_body(root, manifest)
                if expected_body is not None:
                    if COPILOT_BANNER not in actual_content:
                        findings.append(Finding(
                            severity="ERROR",
                            check="parity",
                            path=path_str,
                            message="Copilot instructions missing banner",
                        ))
                    else:
                        banner_idx = actual_content.index(COPILOT_BANNER)
                        after_banner = actual_content[
                            banner_idx + len(COPILOT_BANNER):
                        ].strip()
                        if after_banner != expected_body.strip():
                            findings.append(Finding(
                                severity="ERROR",
                                check="parity",
                                path=path_str,
                                message="Copilot instructions sections "
                                        "do not match CLAUDE.md content",
                            ))
            else:
                expected: str | None = None
                if path_str == "AGENTS.md":
                    expected = _expected_agents_adapter(root.name)
                elif re.match(
                    r"\.agents/skills/([^/]+)/SKILL\.md$", path_str
                ):
                    match = re.match(
                        r"\.agents/skills/([^/]+)/SKILL\.md$", path_str
                    )
                    if match:
                        expected = _expected_skill_shim(
                            root, match.group(1)
                        )

                if expected is not None and actual_content != expected:
                    findings.append(Finding(
                        severity="ERROR",
                        check="parity",
                        path=path_str,
                        message="Content does not match deterministic "
                                "template",
                    ))

    return findings


# ---------------------------------------------------------------------------
# Check 4: Orphan detection
# ---------------------------------------------------------------------------

def check_orphans(root: Path, manifest: dict[str, Any] | None = None) -> list[Finding]:
    findings: list[Finding] = []

    # Skill shims without canonical sources
    agents_dir = root / ".agents/skills"
    claude_dir = root / ".claude/skills"
    orphan_shims: set[str] = set()
    if agents_dir.is_dir():
        canonical_names = set()
        if claude_dir.is_dir():
            canonical_names = {
                p.parent.name for p in claude_dir.glob("*/SKILL.md")
            }
        for shim in agents_dir.glob("*/SKILL.md"):
            if shim.parent.name not in canonical_names:
                rel = shim.relative_to(root).as_posix()
                orphan_shims.add(rel)
                findings.append(Finding(
                    severity="WARNING",
                    check="orphan",
                    path=rel,
                    message="Skill shim has no matching canonical skill",
                ))

    # Marker-bearing generated files the manifest no longer lists. Copilot skill and
    # agent projections are reported by the provenance check instead.
    if manifest:
        listed = {
            artifact.get("path") for artifact in manifest.get("artifacts", [])
            if isinstance(artifact, dict)
        }
        candidates = [
            root / pattern for pattern in MANIFEST_ALLOWED_PATHS
            if "*" not in pattern and not pattern.endswith(".json")
        ]
        if agents_dir.is_dir():
            candidates.extend(sorted(agents_dir.glob("*/SKILL.md")))
        for path in candidates:
            rel = path.relative_to(root).as_posix()
            if rel in listed or rel in orphan_shims or not path.is_file():
                continue
            try:
                generated = has_ownership_marker(read_text(path))
            except (OSError, UnicodeError):
                continue
            if generated:
                findings.append(Finding(
                    severity="WARNING",
                    check="orphan",
                    path=rel,
                    message="Generated file is no longer listed in the manifest",
                ))

    return findings


# ---------------------------------------------------------------------------
# Check 5: MCP configuration
# ---------------------------------------------------------------------------

def _parse_mcp_json(
    path: Path,
    rel_path: str,
    wrapper_key: str,
) -> tuple[dict[str, Any], list[Finding]]:
    """Parse a JSON MCP config file with full type guards. Returns (servers, findings)."""
    findings: list[Finding] = []
    if not path.is_file():
        return {}, findings
    try:
        data = json.loads(read_text(path))
    except (json.JSONDecodeError, OSError, UnicodeError) as e:
        findings.append(Finding(
            severity="ERROR", check="mcp", path=rel_path,
            message=f"Could not read or parse: {e}",
        ))
        return {}, findings
    if not isinstance(data, dict):
        findings.append(Finding(
            severity="ERROR", check="mcp", path=rel_path,
            message="Top-level value must be an object",
        ))
        return {}, findings
    if wrapper_key not in data:
        findings.append(Finding(
            severity="WARNING", check="mcp", path=rel_path,
            message=f"'{wrapper_key}' key not found",
        ))
        return {}, findings
    servers = data[wrapper_key]
    if not isinstance(servers, dict):
        findings.append(Finding(
            severity="ERROR", check="mcp", path=rel_path,
            message=f"'{wrapper_key}' must be an object",
        ))
        return {}, findings
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            findings.append(Finding(
                severity="WARNING", check="mcp", path=rel_path,
                message=f"Server '{name}': entry must be an object, got {type(entry).__name__}",
            ))
            continue
        has_command = isinstance(entry.get("command"), str) and entry["command"].strip()
        has_url = isinstance(entry.get("url"), str) and entry["url"].strip()
        if not has_command and not has_url:
            findings.append(Finding(
                severity="ERROR", check="mcp", path=rel_path,
                message=f"Server '{name}': must have 'command' (STDIO/local) or 'url' (HTTP/SSE)",
            ))
    return servers, findings


def check_mcp(root: Path, manifest: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []

    # .mcp.json
    mcp_servers, f = _parse_mcp_json(root / ".mcp.json", ".mcp.json", "mcpServers")
    findings.extend(f)

    # .github/mcp.json
    github_mcp_servers, f = _parse_mcp_json(
        root / ".github/mcp.json", ".github/mcp.json", "mcpServers"
    )
    findings.extend(f)

    # Duplicate server names between .mcp.json and .github/mcp.json
    if mcp_servers and github_mcp_servers:
        duplicates = set(mcp_servers) & set(github_mcp_servers)
        for dup in sorted(duplicates):
            findings.append(Finding(
                severity="ERROR",
                check="mcp",
                path=".github/mcp.json",
                message=f"Duplicate server name '{dup}' — .mcp.json takes "
                        "precedence, making .github/mcp.json entry unreachable",
            ))

    # .vscode/mcp.json — uses "servers" wrapper, not "mcpServers"
    vscode_mcp_path = root / ".vscode/mcp.json"
    vscode_servers: dict[str, Any] = {}
    if vscode_mcp_path.is_file():
        try:
            data = json.loads(read_text(vscode_mcp_path))
        except (json.JSONDecodeError, OSError, UnicodeError) as e:
            findings.append(Finding(
                severity="ERROR", check="mcp", path=".vscode/mcp.json",
                message=f"Could not read or parse: {e}",
            ))
            data = None
        if data is not None:
            if not isinstance(data, dict):
                findings.append(Finding(
                    severity="ERROR", check="mcp", path=".vscode/mcp.json",
                    message="Top-level value must be an object",
                ))
            elif "servers" not in data:
                findings.append(Finding(
                    severity="ERROR", check="mcp", path=".vscode/mcp.json",
                    message="VS Code MCP must use 'servers' wrapper (not 'mcpServers')",
                ))
            elif not isinstance(data["servers"], dict):
                findings.append(Finding(
                    severity="ERROR", check="mcp", path=".vscode/mcp.json",
                    message="'servers' must be an object",
                ))
            else:
                vscode_servers = data["servers"]

    # .codex/config.toml
    codex_servers: dict[str, dict[str, Any]] = {}
    codex_path = root / ".codex/config.toml"
    if codex_path.is_file():
        try:
            with codex_path.open("rb") as f:
                codex_config = tomllib.load(f)
            codex_mcp = codex_config.get("mcp_servers", {})
            if isinstance(codex_mcp, dict):
                for name, server in codex_mcp.items():
                    if not isinstance(server, dict):
                        continue
                    codex_servers[name] = server
                    has_url = "url" in server
                    if has_url:
                        if "env" in server:
                            findings.append(Finding(
                                severity="ERROR",
                                check="mcp",
                                path=".codex/config.toml",
                                message=f"Server '{name}': env is STDIO-only, "
                                        "must not be present on HTTP transport",
                            ))
                        if "env_vars" in server:
                            findings.append(Finding(
                                severity="ERROR",
                                check="mcp",
                                path=".codex/config.toml",
                                message=f"Server '{name}': env_vars is STDIO-only, "
                                        "must not be present on HTTP transport",
                            ))
        except tomllib.TOMLDecodeError as e:
            findings.append(Finding(
                severity="ERROR",
                check="mcp",
                path=".codex/config.toml",
                message=f"Invalid TOML: {e}",
            ))
        except OSError as e:
            findings.append(Finding(
                severity="ERROR",
                check="mcp",
                path=".codex/config.toml",
                message=f"Error reading config: {e}",
            ))

    # Transport/target validation from manifest
    manifest_servers = manifest.get("mcp_servers", [])
    for server in manifest_servers:
        name = server.get("name", "<unnamed>")
        transport = server.get("transport", "")
        targets = server.get("targets", [])

        if transport not in TRANSPORT_COMPATIBILITY:
            findings.append(Finding(
                severity="ERROR",
                check="mcp",
                message=f"Server '{name}': unknown transport '{transport}'",
            ))
            continue

        supported = TRANSPORT_COMPATIBILITY[transport]
        for target in targets:
            if target not in supported:
                findings.append(Finding(
                    severity="ERROR",
                    check="mcp",
                    message=f"Server '{name}': transport '{transport}' not "
                            f"supported for target '{target}'",
                ))

    # Copilot local tool policy in shared .mcp.json
    mcp_targets = manifest_mcp_targets(manifest)
    if "copilot_local" in mcp_targets and mcp_servers:
        for name, server in mcp_servers.items():
            if not isinstance(server, dict):
                continue
            tools = server.get("tools")
            if tools is not None and tools != ["*"]:
                findings.append(Finding(
                    severity="ERROR",
                    check="mcp",
                    path=".mcp.json",
                    message=f"Server '{name}': copilot_local.tools allowlist "
                            "cannot be enforced in shared .mcp.json",
                ))

    # Copilot repository MCP: mandatory manual-verification warning
    if "copilot_repository" in mcp_targets:
        findings.append(Finding(
            severity="WARNING",
            check="mcp",
            message="Copilot repository MCP (cloud agent/code review) "
                    "configured via repository settings — cannot validate statically",
        ))

    # Code review + repository MCP: readOnlyHint warning
    surfaces = manifest.get("surfaces", [])
    if "code_review" in surfaces and "copilot_repository" in mcp_targets:
        findings.append(Finding(
            severity="WARNING",
            check="mcp",
            message="Code-review tool set derived from repository allowlist "
                    "intersected with readOnlyHint: true — cannot verify "
                    "tool annotations statically",
        ))

    # Cross-runtime semantic parity
    def _normalize_server(server: dict[str, Any]) -> dict[str, Any]:
        """Extract connection-relevant fields for parity comparison."""
        normalized: dict[str, Any] = {}
        for key in ("command", "args", "url", "cwd", "env"):
            if key in server:
                normalized[key] = server[key]
        return normalized

    all_servers: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for name, server in mcp_servers.items():
        if isinstance(server, dict):
            all_servers.setdefault(name, []).append((".mcp.json", server))
    for name, server in github_mcp_servers.items():
        if isinstance(server, dict):
            all_servers.setdefault(name, []).append((".github/mcp.json", server))
    for name, server in vscode_servers.items():
        if isinstance(server, dict):
            all_servers.setdefault(name, []).append((".vscode/mcp.json", server))
    for name, server in codex_servers.items():
        all_servers.setdefault(name, []).append((".codex/config.toml", server))

    for name, entries in all_servers.items():
        if len(entries) < 2:
            continue
        normalized_entries = [
            (source, _normalize_server(server)) for source, server in entries
        ]
        base_source, base_norm = normalized_entries[0]
        for other_source, other_norm in normalized_entries[1:]:
            if base_norm != other_norm:
                findings.append(Finding(
                    severity="WARNING",
                    check="mcp",
                    message=f"Server '{name}': connection fields differ between "
                            f"{base_source} and {other_source}",
                ))

    return findings


# ---------------------------------------------------------------------------
# Check 6: Instruction layering
# ---------------------------------------------------------------------------

def check_instruction_layering(
    root: Path,
    manifest: dict[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    surfaces = set(manifest.get("surfaces", []))
    runtimes = set(manifest.get("runtimes", []))

    has_claude_md = (root / "CLAUDE.md").is_file()
    has_agents_md = (root / "AGENTS.md").is_file()
    agents_redirects = False
    if has_agents_md:
        try:
            agents_content = read_text(root / "AGENTS.md")
            agents_redirects = is_redirecting_agents_md(agents_content)
        except (OSError, UnicodeError):
            pass

    has_copilot_instructions = (root / ".github/copilot-instructions.md").is_file()
    has_gemini_md = (root / "GEMINI.md").is_file()

    # Check for AGENTS.override.md masking the adapter
    has_override = (root / "AGENTS.override.md").is_file()

    # Codex analysis
    if "codex" in runtimes:
        if has_override and has_agents_md and agents_redirects:
            findings.append(Finding(
                severity="ERROR",
                check="layering",
                path="AGENTS.override.md",
                message="AGENTS.override.md masks the generated AGENTS.md adapter"
                        " — Codex will not load CLAUDE.md through the adapter",
            ))
        elif has_agents_md:
            if agents_redirects:
                findings.append(Finding(
                    severity="INFO",
                    check="layering",
                    message="Codex: AGENTS.md adapter redirects to CLAUDE.md",
                ))
            else:
                findings.append(Finding(
                    severity="WARNING",
                    check="layering",
                    message="Codex: non-redirecting AGENTS.md — "
                            "CLAUDE.md not loaded through adapter",
                ))
        elif has_claude_md:
            findings.append(Finding(
                severity="INFO",
                check="layering",
                message="Codex: no AGENTS.md, CLAUDE.md used via fallback",
            ))
        else:
            findings.append(Finding(
                severity="ERROR",
                check="layering",
                message="Codex: no AGENTS.md or CLAUDE.md — "
                        "no instructions available",
            ))

        # Nested instruction files: Codex concatenates from root to cwd
        for nested_override in root.rglob("AGENTS.override.md"):
            if ".git" in nested_override.parts:
                continue
            if nested_override == root / "AGENTS.override.md":
                continue
            rel = nested_override.relative_to(root).as_posix()
            findings.append(Finding(
                severity="WARNING",
                check="layering",
                path=rel,
                message="Nested AGENTS.override.md takes precedence over "
                        "AGENTS.md in this subtree — review for conflicting "
                        "guidance with the root adapter",
            ))

        # Nested non-redirecting AGENTS.md in the chain
        if has_agents_md and agents_redirects:
            for nested_agents in root.rglob("AGENTS.md"):
                if ".git" in nested_agents.parts:
                    continue
                if nested_agents == root / "AGENTS.md":
                    continue
                rel = nested_agents.relative_to(root).as_posix()
                try:
                    nested_content = read_text(nested_agents)
                    if not is_redirecting_agents_md(nested_content):
                        findings.append(Finding(
                            severity="WARNING",
                            check="layering",
                            path=rel,
                            message="Nested AGENTS.md adds instructions alongside "
                                    "the root adapter in this subtree — review "
                                    "for conflicting or redundant guidance",
                        ))
                except (OSError, UnicodeError):
                    pass

    # Copilot CLI/app analysis
    if {"copilot_cli", "copilot_app"} & surfaces:
        effective_sources: list[str] = []
        if has_claude_md:
            effective_sources.append("CLAUDE.md")
        if has_agents_md:
            effective_sources.append("AGENTS.md")
        if has_copilot_instructions:
            effective_sources.append(".github/copilot-instructions.md")
        if has_gemini_md:
            effective_sources.append("GEMINI.md")

        if not effective_sources:
            findings.append(Finding(
                severity="ERROR",
                check="layering",
                message="Copilot CLI/app: no instruction sources available",
            ))
        else:
            findings.append(Finding(
                severity="INFO",
                check="layering",
                message=f"Copilot CLI/app: effective sources: "
                        f"{', '.join(effective_sources)}",
            ))

        findings.append(Finding(
            severity="WARNING",
            check="layering",
            message="Copilot CLI/app folder trust status cannot be determined "
                    "statically — .mcp.json silently skipped in untrusted directories",
        ))

    # JetBrains analysis
    if "jetbrains" in surfaces:
        if not has_copilot_instructions:
            has_path_instructions = (root / ".github/instructions").is_dir() and any(
                (root / ".github/instructions").rglob("*.instructions.md")
            )
            if not has_path_instructions:
                findings.append(Finding(
                    severity="ERROR",
                    check="layering",
                    message="JetBrains: no .github/copilot-instructions.md or "
                            "path-specific instructions — JetBrains cannot "
                            "load CLAUDE.md directly",
                ))
            else:
                findings.append(Finding(
                    severity="INFO",
                    check="layering",
                    message="JetBrains: path-specific instructions only "
                            "(no copilot-instructions.md)",
                ))
        else:
            findings.append(Finding(
                severity="INFO",
                check="layering",
                message="JetBrains: .github/copilot-instructions.md available",
            ))

    # Cloud agent analysis
    if "cloud_agent" in surfaces:
        if has_agents_md:
            if agents_redirects:
                findings.append(Finding(
                    severity="INFO",
                    check="layering",
                    message="Cloud agent: AGENTS.md adapter redirects to CLAUDE.md",
                ))
            else:
                findings.append(Finding(
                    severity="INFO",
                    check="layering",
                    message="Cloud agent: non-redirecting AGENTS.md — "
                            "CLAUDE.md not loaded directly",
                ))
        elif has_claude_md:
            findings.append(Finding(
                severity="INFO",
                check="layering",
                message="Cloud agent: no AGENTS.md, CLAUDE.md selected directly",
            ))
        elif has_gemini_md:
            findings.append(Finding(
                severity="INFO",
                check="layering",
                message="Cloud agent: no AGENTS.md or CLAUDE.md, "
                        "GEMINI.md selected as alternative",
            ))
        else:
            findings.append(Finding(
                severity="ERROR",
                check="layering",
                message="Cloud agent: no AGENTS.md, CLAUDE.md, or GEMINI.md "
                        "— no instructions available",
            ))

        # Check for nested AGENTS.md that supersedes root adapter
        nested_agents = [
            p for p in root.rglob("AGENTS.md")
            if ".git" not in p.parts and p != root / "AGENTS.md"
        ]
        if nested_agents and has_agents_md and agents_redirects:
            for nested in nested_agents:
                rel = nested.relative_to(root).as_posix()
                findings.append(Finding(
                    severity="WARNING",
                    check="layering",
                    path=rel,
                    message="Nested AGENTS.md supersedes root adapter for "
                            "cloud agent sessions in this subtree",
                ))

    # Code review analysis: it reads AGENTS.md, copilot-instructions, and path-specific
    # instructions, but never CLAUDE.md or GEMINI.md, so a redirect-only AGENTS.md adds nothing.
    if "code_review" in surfaces:
        review_sources: list[str] = []
        if has_agents_md and not agents_redirects:
            review_sources.append("AGENTS.md")
        if has_copilot_instructions:
            review_sources.append(".github/copilot-instructions.md")
        if (root / ".github/instructions").is_dir() and any(
            (root / ".github/instructions").rglob("*.instructions.md")
        ):
            review_sources.append(".github/instructions")
        if review_sources:
            findings.append(Finding(
                severity="INFO",
                check="layering",
                message=f"Code review: effective sources: {', '.join(review_sources)}",
            ))
        else:
            findings.append(Finding(
                severity="ERROR",
                check="layering",
                message="Code review: no project instructions it can read — it ignores "
                        "CLAUDE.md, and AGENTS.md "
                        + ("only redirects to CLAUDE.md" if has_agents_md else "is absent"),
            ))
        findings.append(Finding(
            severity="WARNING",
            check="layering",
            message="Code-review custom-instructions enablement cannot "
                    "be verified statically",
        ))
        findings.append(Finding(
            severity="WARNING",
            check="trust-boundary",
            message="Copilot code review loads instructions, agents, and skills from the PR head; "
                    "this is advisory context, not a trusted-base or trusted-ref review contract",
        ))
        if (root / ".github/skills").is_dir():
            findings.append(Finding(
                severity="INFO", check="layering", path=".github/skills",
                message="Code review can use relevant .github/skills entries; .claude/skills and .agents/skills are not its documented automatic skill location",
            ))

    if {"copilot_cli", "copilot_app", "cloud_agent", "code_review"} & surfaces:
        findings.append(Finding(
            severity="WARNING", check="runtime",
            message="Copilot repository settings, organization policy, authentication, model availability, runtime enablement, and actual operational use cannot be verified statically",
        ))

    # VS Code analysis
    if "vscode" in surfaces:
        findings.append(Finding(
            severity="WARNING",
            check="layering",
            message="VS Code instruction settings (chat.useClaudeMdFile, "
                    "chat.useAgentsMdFile, useInstructionFiles, "
                    "includeApplyingInstructions) cannot be verified statically",
        ))

    return findings


# ---------------------------------------------------------------------------
# Check 7: Behavioral constraints
# ---------------------------------------------------------------------------

QUALITY_GATE_PATTERNS = (
    # Coverage threshold: "coverage must stay at or above 80%", "90% branch coverage".
    re.compile(
        r"\bcoverage\b[^\n]{0,80}?\b\d{1,3}(?:\.\d+)?\s*%"
        r"|\b\d{1,3}(?:\.\d+)?\s*%\s+(?:line\s+|branch\s+|statement\s+|test\s+)?coverage\b",
        re.IGNORECASE,
    ),
    # Lint severity level: "--severity=warning", "treat warnings as errors", "--max-warnings 0".
    re.compile(
        r"--severity[= ](?:style|info|warning|error)\b|\bwarnings?\s+as\s+errors\b"
        r"|\bTreatWarningsAsErrors\b|-Werror\b|--max-warnings[= ]0\b",
        re.IGNORECASE,
    ),
    # Pass/fail gate: "every check must pass", "all tests pass".
    re.compile(
        r"\b(?:every|all)\s+(?:required\s+)?(?:checks?|tests?|gates?)\s+(?:must\s+|should\s+)?pass(?:es)?\b",
        re.IGNORECASE,
    ),
)


def check_behavioral_constraints(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    claude_path = root / "CLAUDE.md"
    if not claude_path.is_file():
        return findings

    try:
        content = read_text(claude_path)
    except (OSError, UnicodeError):
        return findings

    lower = content.lower()

    if "never disable" not in lower and "never suppress" not in lower:
        findings.append(Finding(
            severity="WARNING",
            check="behavioral",
            path="CLAUDE.md",
            message="No 'never disable/suppress' rule found",
        ))

    if not re.search(r"\b(?:never|do not|don't)\s+work\s*around\b", lower):
        findings.append(Finding(
            severity="WARNING",
            check="behavioral",
            path="CLAUDE.md",
            message=(
                "No rule forbidding workarounds for failing checks "
                "(skip or expected-failure markers, weakened gates, TODO comments)"
            ),
        ))

    if not any(pattern.search(content) for pattern in QUALITY_GATE_PATTERNS):
        findings.append(Finding(
            severity="WARNING",
            check="behavioral",
            path="CLAUDE.md",
            message=(
                "No quality gate found (a coverage threshold, a lint severity level, "
                "or a rule that every check must pass)"
            ),
        ))

    return findings


# ---------------------------------------------------------------------------
# Check 8: Ownership tracking
# ---------------------------------------------------------------------------

def check_ownership(
    root: Path,
    manifest: dict[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    artifacts = manifest.get("artifacts", [])

    for artifact in artifacts:
        path_str = artifact.get("path", "")
        if validate_manifest_path(path_str):
            continue  # Already reported in parity check

        full_path = root / path_str
        if not full_path.is_file():
            continue  # Already reported in parity check

        try:
            content = read_text(full_path)
        except (OSError, UnicodeError):
            findings.append(Finding(
                severity="ERROR",
                check="ownership",
                path=path_str,
                message="Generated artifact exists but could not be read",
            ))
            continue

        if path_str.endswith(".json"):
            # JSON: ownership tracked via manifest hash
            stored_hash = artifact.get("hash")
            if not stored_hash and path_str != ".github/ai-config-manifest.json":
                findings.append(Finding(
                    severity="ERROR",
                    check="ownership",
                    path=path_str,
                    message="JSON artifact has no hash in manifest",
                ))
        else:
            # Comment-supporting: ownership tracked via embedded marker
            if not has_ownership_marker(content):
                findings.append(Finding(
                    severity="ERROR",
                    check="ownership",
                    path=path_str,
                    message="Generated file missing embedded ownership marker",
                ))

    return findings


# ---------------------------------------------------------------------------
# Check 9: User-authored collisions
# ---------------------------------------------------------------------------

def check_collisions(
    root: Path,
    manifest: dict[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    runtimes = manifest.get("runtimes", [])

    if "codex" in runtimes:
        agents_path = root / "AGENTS.md"
        if agents_path.is_file():
            try:
                content = read_text(agents_path)
            except (OSError, UnicodeError):
                content = ""
            if not has_ownership_marker(content):
                findings.append(Finding(
                    severity="ERROR",
                    check="collision",
                    path="AGENTS.md",
                    message="User-authored AGENTS.md would conflict with "
                            "generated adapter",
                ))

    copilot_path = root / ".github/copilot-instructions.md"
    if copilot_path.is_file():
        try:
            content = read_text(copilot_path)
        except (OSError, UnicodeError):
            content = ""
        if not has_ownership_marker(content):
            surfaces = manifest.get("surfaces", [])
            copilot_surfaces = {
                "vscode", "jetbrains", "copilot_app", "copilot_cli",
                "cloud_agent", "code_review",
            }
            if copilot_surfaces & set(surfaces):
                findings.append(Finding(
                    severity="ERROR",
                    check="collision",
                    path=".github/copilot-instructions.md",
                    message="User-authored copilot-instructions.md would "
                            "conflict with generated projection",
                ))

    return findings


# ---------------------------------------------------------------------------
# Known limitations that apply to this repository
# ---------------------------------------------------------------------------

def check_limitations(root: Path, manifest: dict[str, Any]) -> list[Finding]:
    """Report each documented static-audit limitation this repository actually hits."""
    findings: list[Finding] = []

    def note(path: str | None, message: str) -> None:
        findings.append(Finding(severity="INFO", check="limitation", path=path, message=message))

    artifact_paths = [
        artifact["path"] for artifact in manifest.get("artifacts", [])
        if isinstance(artifact, dict) and isinstance(artifact.get("path"), str)
    ]
    for path in sorted(artifact_paths):
        if path.endswith((".toml", ".yml", ".yaml")):
            note(path, "Only the ownership marker is checked; content drift inside this file is not detected")
    if (manifest and "copilot_sections" not in manifest
            and ".github/copilot-instructions.md" in artifact_paths):
        note(".github/ai-config-manifest.json",
             "Manifest predates copilot_sections; Copilot parity assumes the default sections")
    for directory in (".github/skills", ".github/agents"):
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(p for p in base.rglob("*.md") if p.is_file()):
            rel = path.relative_to(root).as_posix()
            try:
                generated = has_ownership_marker(read_text(path))
            except (OSError, UnicodeError):
                generated = False
            if generated or rel in artifact_paths:
                note(rel, "Generated Copilot projection is provenance-checked only, not reconstructed")
    instructions = root / ".github/instructions"
    if instructions.is_dir() and any(instructions.rglob("*.instructions.md")):
        note(".github/instructions", "Path-specific instructions are not checked for contradictions or overlap")
    for nested in sorted(root.rglob(".mcp.json")):
        if ".git" in nested.parts or nested == root / ".mcp.json":
            continue
        note(nested.relative_to(root).as_posix(), "Nested .mcp.json files are not validated")
    return findings


# ---------------------------------------------------------------------------
# Main audit orchestrator
# ---------------------------------------------------------------------------

def audit(root: Path) -> AuditResult:
    """Run all audit checks and return the result."""
    result = AuditResult(repository=root.name)

    # Step 1: Inventory
    result.findings.extend(check_inventory(root))

    # Step 2: Authority classification
    classification, manifest, authority_findings = classify_authority(root)
    result.authority = classification
    result.findings.extend(authority_findings)

    # Steps 3-9 only apply to conforming repos
    if classification != "conforming":
        return result

    # Vocabulary validation (before any checks that depend on manifest values)
    if manifest:
        result.findings.extend(validate_manifest_vocabulary(manifest))

    result.scope_status, scope_findings = check_scope_and_roles(root, manifest or {})
    result.findings.extend(scope_findings)

    # Step 3: Parity validation
    if manifest:
        result.findings.extend(check_parity(root, manifest))

    # Step 4: Orphan detection
    result.findings.extend(check_orphans(root, manifest))

    # Step 5: MCP configuration
    result.findings.extend(check_mcp(root, manifest or {}))

    # Step 6: Instruction layering
    result.findings.extend(check_instruction_layering(root, manifest or {}))

    result.findings.extend(check_copilot_configuration(root, manifest or {}))

    # Step 7: Behavioral constraints
    result.findings.extend(check_behavioral_constraints(root))

    # Step 8: Ownership tracking
    if manifest:
        result.findings.extend(check_ownership(root, manifest))

    # Step 9: User-authored collisions
    result.findings.extend(check_collisions(root, manifest or {}))

    result.findings.extend(check_limitations(root, manifest or {}))

    return result


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def summary_lines(findings: list[Finding]) -> list[str]:
    """Count findings per severity, then INFO findings per check."""
    severities = Counter(f.severity for f in findings)
    info_checks = Counter(f.check for f in findings if f.severity == "INFO")
    lines = [
        f"SUMMARY {severity} {severities[severity]}"
        for severity in SEVERITY_ORDER
    ]
    lines.extend(
        f"SUMMARY INFO {check} {info_checks[check]}"
        for check in sorted(info_checks)
    )
    return lines


def format_markdown(result: AuditResult) -> str:
    sorted_findings = result.sorted_findings()
    lines = [
        f"## AI Config Audit — {result.repository}",
        "",
        f"Authority: **{result.authority}**",
        f"Scope: **{result.scope_status}**",
        "",
        *summary_lines(sorted_findings),
        "",
    ]

    if not sorted_findings:
        lines.append("No findings.")
        return "\n".join(lines) + "\n"

    lines.append("### Findings")
    lines.append("")
    lines.append("| Severity | Check | Path | Message |")
    lines.append("|---|---|---|---|")

    for f in sorted_findings:
        path = f.path or ""
        lines.append(f"| {f.severity} | {f.check} | {path} | {f.message} |")

    # Append details for findings that have them
    details = [f for f in sorted_findings if f.detail]
    if details:
        lines.append("")
        lines.append("### Details")
        lines.append("")
        for f in details:
            lines.append(f"**{f.path or f.check}**: {f.message}")
            lines.append("")
            lines.append(f"```\n{f.detail}\n```")
            lines.append("")

    return "\n".join(lines) + "\n"


def format_json(result: AuditResult) -> str:
    output = {
        "repository": result.repository,
        "authority": result.authority,
        "scopeStatus": result.scope_status,
        "exitCode": result.exit_code,
        "findings": [asdict(f) for f in result.sorted_findings()],
    }
    return json.dumps(output, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit of AI agent configuration"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="output JSON instead of Markdown",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root (defaults to current directory)",
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

    result = audit(args.root)

    if args.json:
        print(format_json(result), end="")
    else:
        print(format_markdown(result), end="")

    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

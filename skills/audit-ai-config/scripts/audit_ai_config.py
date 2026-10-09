#!/usr/bin/env python3
"""Deterministic read-only audit engine for AI agent configuration.

Owned by the audit-ai-config skill and standalone after deployment. It checks
any repository, and checks the generated layout described in
references/generated-layout.md where a repository's own ai_config.py generator
produced one.

Usage:
    python audit_ai_config.py [--json] [--root <path>]

The Markdown report states its result on one line, before the SUMMARY lines:

    RESULT COMPLIANT      compliant within statically verifiable scope
    RESULT ERRORS         one or more ERROR-level findings
    RESULT INCONCLUSIVE   ambiguous, unconfigured, or alternative authority

Exit codes:
    0  RESULT COMPLIANT
    1  RESULT ERRORS or RESULT INCONCLUSIVE, or a last line FAILED <reason>
       (the root is not a Git repository)
    2  usage error
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import os
import re
import sys
import tomllib
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from console import use_utf8_output
from frontmatter import NESTED_KEY, SEQUENCE_ITEM, Frontmatter, FrontmatterError

# ---------------------------------------------------------------------------
# Finding model
# ---------------------------------------------------------------------------

SEVERITY_ORDER = {"ERROR": 0, "WARNING": 1, "INFO": 2}

OWNERSHIP_MARKER = "AUTO-GENERATED from CLAUDE.md"

# Canonical vocabulary of the generated manifest (references/generated-layout.md).
VALID_RUNTIMES: set[str] = {"claude", "codex"}
VALID_SURFACES: set[str] = {
    "copilot_cli",
    "copilot_app",
    "vscode",
    "jetbrains",
    "cloud_agent",
    "code_review",
}
VALID_MCP_TARGETS: set[str] = {
    "claude",
    "codex",
    "copilot_local",
    "vscode",
    "copilot_repository",
}

# The transport table in references/generated-layout.md; test_audit_ai_config.py pins both.
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
    def result(self) -> str:
        """COMPLIANT, ERRORS, or INCONCLUSIVE; the report prints it as a RESULT line."""
        if self.authority in ("ambiguous", "unconfigured", "alternative"):
            return "INCONCLUSIVE"
        if any(f.severity == "ERROR" for f in self.findings):
            return "ERRORS"
        return "COMPLIANT"

    @property
    def exit_code(self) -> int:
        return 0 if self.result == "COMPLIANT" else 1

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=Finding.sort_key)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def read_text_or_none(path: Path) -> str | None:
    """The file's text, or None when it cannot be read or decoded; the caller has reported that, or ignores it."""
    try:
        return read_text(path)
    except (OSError, UnicodeError):
        return None


# Directories no repository author writes agent configuration into: version control and installed dependencies.
SKIPPED_DIRECTORIES: frozenset[str] = frozenset(
    {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".nox"}
)


def named_files(root: Path, *names: str) -> list[Path]:
    """Every file below the root with one of these names, outside SKIPPED_DIRECTORIES, in a stable order."""
    found: list[Path] = []
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = sorted(name for name in subdirectories if name not in SKIPPED_DIRECTORIES)
        found.extend(Path(directory, name) for name in sorted(files) if name in names)
    return found


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
    if path_str.startswith(".github/skills/") and len(path_str.split("/")) != 4:
        return f"skill projection path must have exactly one skill-name segment: {path_str}"
    if path_str.startswith((".github/agents/", ".claude/agents/")) and (
        len(path_str.split("/")) != 3 or not path_str.endswith(".md")
    ):
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
                findings.append(
                    Finding(
                        severity="ERROR",
                        check="vocabulary",
                        path=".github/ai-config-manifest.json",
                        message=f"Unknown runtime in manifest: '{r}'",
                    )
                )
    surfaces = manifest.get("surfaces", [])
    if isinstance(surfaces, list):
        for s in surfaces:
            if isinstance(s, str) and s not in VALID_SURFACES:
                findings.append(
                    Finding(
                        severity="ERROR",
                        check="vocabulary",
                        path=".github/ai-config-manifest.json",
                        message=f"Unknown surface in manifest: '{s}'",
                    )
                )
    for t in manifest_mcp_targets(manifest):
        if isinstance(t, str) and t not in VALID_MCP_TARGETS:
            findings.append(
                Finding(
                    severity="ERROR",
                    check="vocabulary",
                    path=".github/ai-config-manifest.json",
                    message=f"Unknown MCP target in manifest: '{t}'",
                )
            )
    return findings


def is_redirecting_agents_md(content: str) -> bool:
    """Check if AGENTS.md is an adapter that redirects to CLAUDE.md."""
    lower = content.lower()
    return "claude.md" in lower and ("read and follow" in lower or "authoritative source" in lower)


# ---------------------------------------------------------------------------
# Check 1: Inventory
# ---------------------------------------------------------------------------

# Fixed and recursively discovered configuration files.
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
    return [
        *_inventory_fixed_files(root),
        *_inventory_recursive_files(root),
        *_inventory_skill_directories(root),
        *_inventory_agent_directories(root),
        *_inventory_path_instructions(root),
        *_inventory_generator_scripts(root),
        *_inventory_parity_workflows(root),
    ]


def _inventory_finding(path: str, message: str) -> Finding:
    return Finding(severity="INFO", check="inventory", path=path, message=message)


def _inventory_fixed_files(root: Path) -> list[Finding]:
    return [
        _inventory_finding(rel_path, f"Found {rel_path}") for rel_path in INVENTORY_FILES if (root / rel_path).is_file()
    ]


def _inventory_recursive_files(root: Path) -> list[Finding]:
    return [
        _inventory_finding(rel, f"Found {rel}")
        for name in RECURSIVE_INVENTORY_NAMES
        for rel in (found.relative_to(root).as_posix() for found in named_files(root, name))
    ]


def _inventory_skill_directories(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for skill_dir in (".claude/skills", ".agents/skills", ".github/skills"):
        d = root / skill_dir
        if d.is_dir():
            skills = [p.parent.name for p in d.glob("*/SKILL.md")]
            if skills:
                findings.append(_inventory_finding(skill_dir, f"Skills found: {', '.join(sorted(skills))}"))
    return findings


def _inventory_agent_directories(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for agent_dir in (".github/agents", ".claude/agents"):
        d = root / agent_dir
        if d.is_dir():
            agents = [p.name for p in d.iterdir() if p.is_file()]
            if agents:
                findings.append(_inventory_finding(agent_dir, f"Custom agent files found: {', '.join(sorted(agents))}"))
    return findings


def _inventory_path_instructions(root: Path) -> list[Finding]:
    """Path-specific Copilot instructions."""
    instructions_dir = root / ".github/instructions"
    if not instructions_dir.is_dir():
        return []
    instr_files = list(instructions_dir.rglob("*.instructions.md"))
    if not instr_files:
        return []
    return [_inventory_finding(".github/instructions", f"{len(instr_files)} path-specific instruction file(s)")]


def _inventory_generator_scripts(root: Path) -> list[Finding]:
    return [
        _inventory_finding(candidate, f"Generator script found: {candidate}")
        for candidate in GENERATOR_CANDIDATES
        if (root / candidate).is_file()
    ]


def _inventory_parity_workflows(root: Path) -> list[Finding]:
    """Workflows under .github/workflows that name ai-config or ai_config; unreadable ones are skipped."""
    workflows_dir = root / ".github/workflows"
    if not workflows_dir.is_dir():
        return []
    findings: list[Finding] = []
    for pattern in ("*.yml", "*.yaml"):
        for wf in workflows_dir.glob(pattern):
            try:
                wf_content = read_text(wf)
                if "ai-config" in wf_content.lower() or "ai_config" in wf_content.lower():
                    rel = wf.relative_to(root).as_posix()
                    findings.append(_inventory_finding(rel, "AI config parity workflow found"))
            except (OSError, UnicodeError):
                pass
    return findings


# ---------------------------------------------------------------------------
# Copilot skills, agents, projections, and roles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Nested:
    """A value that is neither text nor a list of text: a nested mapping, such as the Agent Skills specification's
    metadata, or a list of mappings, both accepted unread, or a list reported as unreadable."""


NESTED = Nested()
FrontmatterValue = str | list[str] | Nested
UNREADABLE_ENTRY = "Frontmatter must use key: value entries, block scalars, block lists, or nested mappings"


# The line walk is kept beside skill-core's frontmatter.py by decision
# (https://github.com/EJRyan89/coding-agent-skills/issues/27#issuecomment-6022293710): this audit is a lint policy that
# reports each frontmatter problem at its line number, which the shared reader does not give. The walk finds each
# entry's lines and hands quoted values, lists, and continued scalars to the shared reader, so both read them alike.
def _frontmatter(path: Path) -> tuple[dict[str, FrontmatterValue], list[Finding]]:
    """Read YAML frontmatter line by line without executing or loading YAML tags.

    A one-line plain value is kept as written. A block scalar (| or >) is read as YAML reads it. A quoted value, a
    list, or a scalar continued on indented lines is read by skill-core's reader, and a nested mapping is accepted
    without reading its entries.
    """
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
    values: dict[str, FrontmatterValue] = {}
    findings: list[Finding] = []
    index = 1
    while index < end:
        line = lines[index]
        index += 1
        number = index
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*?)\s*$", line)
        if not match:
            findings.append(Finding("ERROR", "copilot-config", rel, number, UNREADABLE_ENTRY))
            continue
        key, inline = match.groups()
        value: FrontmatterValue
        header = BLOCK_SCALAR_HEADER.fullmatch(inline)
        if header:
            body = _block_scalar_lines(lines[index:end], header)
            value = _block_scalar_value(body, header)
        else:
            body = _entry_lines(lines[index:end], listed=not inline)
            value, problem = _entry_value(key, inline, body)
            if problem:
                offset, message = problem
                findings.append(Finding("ERROR", "copilot-config", rel, number + offset, message))
        index += len(body)
        if key in values:
            findings.append(Finding("ERROR", "copilot-config", rel, number, f"Duplicate frontmatter key '{key}'"))
        values[key] = value
    return values, findings


def _entry_lines(lines: list[str], listed: bool) -> list[str]:
    """The lines after a key that belong to its value: indented lines, and list items at the key's own indentation
    when the key has no inline value, with the blank and comment lines between them."""
    count = 0
    for position, line in enumerate(lines, start=1):
        if line.startswith((" ", "\t")) or (listed and line.startswith("-")):
            count = position
        elif line.strip() and not line.startswith("#"):
            break
    return lines[:count]


def _indentation(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _entry_value(key: str, inline: str, body: list[str]) -> tuple[FrontmatterValue, tuple[int, str] | None]:
    """An entry's value, and the offset from its key line and the message of the problem that makes it an ERROR."""
    content = [
        (offset, line)
        for offset, line in enumerate(body, start=1)
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not content and (not inline or inline[0] not in "\"'["):
        return inline, None
    if not inline:
        first = content[0][1]
        indent = _indentation(first)
        if SEQUENCE_ITEM.match(first.strip()):
            for offset, line in content:
                if _indentation(line) < indent or (
                    _indentation(line) == indent and not SEQUENCE_ITEM.match(line.strip())
                ):
                    return NESTED, (offset, UNREADABLE_ENTRY)
            if any(_indentation(line) > indent or NESTED_KEY.match(line.strip()[1:].strip()) for _, line in content):
                return NESTED, None
        elif NESTED_KEY.match(first.strip()):
            stray = next((offset for offset, line in content if _indentation(line) < indent), None)
            return NESTED, (stray, UNREADABLE_ENTRY) if stray else None
    try:
        value = Frontmatter([f"{key}: {inline}", *(line for _, line in content)]).value(key)
    except FrontmatterError as error:
        if inline.startswith("[") and not content:
            # Kept as text, as Claude Code reads an argument-hint such as [pr-number] [priority].
            return inline, None
        return inline or NESTED, (0, f"Frontmatter entry cannot be read as YAML: {error}")
    return ("" if value is None else value), None


# A block scalar header: | (literal) or > (folded), then a chomping and an indentation indicator in either order.
BLOCK_SCALAR_HEADER = re.compile(
    r"(?P<style>[|>])(?:(?P<chomp>[+-])?(?P<indent>[1-9])?|(?P<indent2>[1-9])(?P<chomp2>[+-]))"
)


def _block_scalar_indent(lines: list[str], header: re.Match[str]) -> int:
    """The block's content indentation: the indicator's, or the first non-blank line's."""
    explicit = header.group("indent") or header.group("indent2")
    if explicit:
        return int(explicit)
    for line in lines:
        if line.strip():
            return len(line) - len(line.lstrip(" "))
    return 0


def _block_scalar_lines(lines: list[str], header: re.Match[str]) -> list[str]:
    """The lines a block scalar spans: blank lines and lines indented at least as far as its content."""
    indent = _block_scalar_indent(lines, header)
    body: list[str] = []
    if indent == 0:
        return body
    for line in lines:
        if line.strip() and not line.startswith(" " * indent):
            break
        body.append(line)
    return body


def _block_scalar_value(body: list[str], header: re.Match[str]) -> str:
    """The value YAML gives a block scalar, with its chomping applied."""
    indent = _block_scalar_indent(body, header)
    content = [line[indent:] if line.strip() else "" for line in body]
    trailing = len(content)
    while trailing and not content[trailing - 1]:
        trailing -= 1
    text = "\n".join(content[:trailing]) if header.group("style") == "|" else _fold(content[:trailing])
    chomp = header.group("chomp") or header.group("chomp2")
    if not text or chomp == "-":
        return text
    if chomp == "+":
        return text + "\n" * (len(content) - trailing + 1)
    return text + "\n"


def _fold(content: list[str]) -> str:
    """Folded style: a break between two lines of text becomes a space, each blank line a newline, and the breaks
    around a more-indented line are kept."""
    text = ""
    previous: str | None = None
    blank = 0
    for line in content:
        if not line:
            blank += 1
            continue
        if previous is None:
            text += "\n" * blank
        elif previous.startswith((" ", "\t")) or line.startswith((" ", "\t")):
            text += "\n" * (blank + 1)
        else:
            text += "\n" * blank if blank else " "
        text += line
        previous = line
        blank = 0
    return text


def _text(values: dict[str, FrontmatterValue], key: str) -> str:
    """A key's value when it is text, and an empty string when it is absent, a list, or a nested mapping."""
    value = values.get(key)
    return value if isinstance(value, str) else ""


def _is_tool_list(tools: FrontmatterValue) -> bool:
    """A non-empty list of non-empty names, or text other than a flow list the reader could not read."""
    if isinstance(tools, list):
        return bool(tools) and all(tools)
    return isinstance(tools, str) and not tools.startswith("[")


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
    elif name != directory_name or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", directory_name):
        findings.append(
            Finding(
                "ERROR", "copilot-skill", rel, message="Skill name must be lowercase hyphenated and match its directory"
            )
        )
    if not _text(values, "description").strip():
        findings.append(Finding("ERROR", "copilot-skill", rel, message="Skill frontmatter requires description"))
    if values.get("allowed-tools") == "":
        findings.append(Finding("ERROR", "copilot-skill", rel, message="allowed-tools must not be empty"))
    if directory_name in COPILOT_BUILTIN_NAMES:
        findings.append(
            Finding(
                "ERROR",
                "collision",
                rel,
                message=f"Repository skill '{directory_name}' collides with a Copilot built-in agent",
            )
        )
    return findings


def _validate_agent(path: Path, root: Path) -> list[Finding]:
    rel = path.relative_to(root).as_posix()
    findings: list[Finding] = []
    if not (path.name.endswith(".agent.md") or path.suffix == ".md"):
        return [Finding("ERROR", "copilot-agent", rel, message="Custom agent filenames must end in .md or .agent.md")]
    agent_id = _identifier_from_agent_filename(path)
    if not agent_id or not re.fullmatch(r"[A-Za-z0-9._-]+", agent_id):
        findings.append(
            Finding("ERROR", "copilot-agent", rel, message="Custom agent filename contains unsupported characters")
        )
    values, frontmatter_findings = _frontmatter(path)
    for finding in frontmatter_findings:
        finding.path = rel
    findings.extend(frontmatter_findings)
    if not _text(values, "description").strip():
        findings.append(Finding("ERROR", "copilot-agent", rel, message="Agent frontmatter requires description"))
    if "target" in values and _text(values, "target") not in {"vscode", "github-copilot"}:
        findings.append(Finding("ERROR", "copilot-agent", rel, message="target must be 'vscode' or 'github-copilot'"))
    for key in ("include-custom-instructions", "infer", "disable-model-invocation", "user-invocable"):
        if key in values and _text(values, key) not in {"true", "false"}:
            findings.append(Finding("ERROR", "copilot-agent", rel, message=f"{key} must be a boolean"))
    tools = values.get("tools")
    if tools == "":
        findings.append(Finding("ERROR", "copilot-agent", rel, message="tools must not be empty"))
    elif tools is not None and not _is_tool_list(tools):
        findings.append(
            Finding(
                "ERROR",
                "copilot-agent",
                rel,
                message="tools must be a non-empty string list or comma-separated string",
            )
        )
    if "modelPolicy" in values and _text(values, "modelPolicy") not in {"preferred", "required"}:
        findings.append(Finding("ERROR", "copilot-agent", rel, message="modelPolicy must be 'preferred' or 'required'"))
    if agent_id.lower() in COPILOT_BUILTIN_NAMES:
        findings.append(
            Finding(
                "ERROR", "collision", rel, message=f"Custom agent '{agent_id}' collides with a Copilot built-in agent"
            )
        )
    return findings


def _provenance(path: Path, rel: str, kind: str, manifest_paths: set[str]) -> list[Finding]:
    """A Copilot projection carries the ownership marker exactly when the manifest owns it.

    An unreadable file is skipped: it is an ERROR from its frontmatter check, and from parity when the manifest owns it.
    """
    content = read_text_or_none(path)
    if content is None:
        return []
    marked = has_ownership_marker(content)
    if marked and rel not in manifest_paths:
        message = f"Generated Copilot {kind} projection is not owned by the manifest"
    elif rel in manifest_paths and not marked:
        message = f"Manifest-owned Copilot {kind} projection lacks an ownership marker"
    else:
        return []
    return [Finding("ERROR", "provenance", rel, message=message)]


def check_copilot_configuration(root: Path, manifest: dict[str, Any]) -> list[Finding]:
    """Statically validate Copilot-discovered repository skills and agents."""
    findings: list[Finding] = []
    manifest_paths = {
        artifact["path"]
        for artifact in manifest.get("artifacts", [])
        if isinstance(artifact, dict) and isinstance(artifact.get("path"), str)
    }
    for skill_dir in (".github/skills", ".claude/skills", ".agents/skills"):
        directory = root / skill_dir
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("SKILL.md")):
            findings.extend(_validate_skill(path, root))
            if skill_dir == ".github/skills":
                findings.extend(_provenance(path, path.relative_to(root).as_posix(), "skill", manifest_paths))
    for agent_dir in (".github/agents", ".claude/agents"):
        directory = root / agent_dir
        if not directory.is_dir():
            continue
        for path in sorted(p for p in directory.iterdir() if p.is_file()):
            findings.extend(_validate_agent(path, root))
            findings.extend(_provenance(path, path.relative_to(root).as_posix(), "agent", manifest_paths))
    return findings


def derive_generator_scope(root: Path) -> tuple[dict[str, list[str]] | None, str]:
    """Safely extract literal TARGET_* declarations from the local generator."""
    for candidate in GENERATOR_CANDIDATES:
        path = root / candidate
        if not path.is_file():
            continue
        try:
            tree = ast.parse(read_text(path), filename=candidate)
        except (SyntaxError, ValueError, OSError):  # ValueError covers UnicodeError and, before 3.12, a null byte
            return None, "generator-scope-unreadable"
        values: dict[str, list[str]] = {}
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {
                    "TARGET_RUNTIMES",
                    "TARGET_SURFACES",
                    "TARGET_FEATURES",
                }:
                    value = node.value
                    if value is None:  # an annotation without a value
                        return None, "generator-scope-not-literal"
                    try:
                        literal = ast.literal_eval(value)
                    except (ValueError, TypeError):
                        return None, "generator-scope-not-literal"
                    if not isinstance(literal, list) or not all(isinstance(item, str) for item in literal):
                        return None, "generator-scope-invalid"
                    values[target.id] = literal
        if len(values) != 3:
            return None, "generator-scope-incomplete"
        return {
            "runtimes": values["TARGET_RUNTIMES"],
            "surfaces": values["TARGET_SURFACES"],
            "features": values["TARGET_FEATURES"],
        }, "independently-derived"
    return None, "manifest-declared-only"


def check_scope_and_roles(root: Path, manifest: dict[str, Any]) -> tuple[str, list[Finding]]:
    findings: list[Finding] = []
    derived, status = derive_generator_scope(root)
    if derived is None and not (root / ".github/ai-config-manifest.json").is_file():
        # With no manifest there is no editable scope that could narrow checks; say what was skipped.
        status = "no-declared-scope"
        findings.append(
            Finding(
                "INFO",
                "scope",
                message=(
                    "No AI-config manifest or recognized generator declares target runtimes or surfaces; "
                    "target-specific checks were not run"
                ),
            )
        )
    elif derived is None:
        findings.append(
            Finding(
                "WARNING",
                "scope",
                message=(
                    f"Audit scope is manifest-declared only; editable manifest scope can suppress checks ({status})"
                ),
            )
        )
    else:
        for field, expected in derived.items():
            actual = manifest.get(field)
            if actual != expected:
                findings.append(
                    Finding(
                        "ERROR",
                        "scope",
                        ".github/ai-config-manifest.json",
                        message=f"Manifest {field} differs from independently derived generator scope",
                    )
                )
        status = "independently-derived"
    roles = manifest.get("runtimeRoles")
    copilot_surfaces = set(manifest.get("surfaces", [])) & set(COPILOT_ROLE_BY_SURFACE)
    if copilot_surfaces and not isinstance(roles, dict):
        findings.append(
            Finding(
                "WARNING",
                "runtime-role",
                ".github/ai-config-manifest.json",
                message="Copilot surfaces lack explicit runtimeRoles declarations",
            )
        )
    elif isinstance(roles, dict):
        for surface in sorted(copilot_surfaces):
            role = roles.get(surface)
            expected_role = COPILOT_ROLE_BY_SURFACE[surface]
            if role not in VALID_COPILOT_ROLES:
                findings.append(
                    Finding(
                        "ERROR",
                        "runtime-role",
                        ".github/ai-config-manifest.json",
                        message=f"{surface} has an unknown runtime role",
                    )
                )
            elif role != expected_role:
                findings.append(
                    Finding(
                        "ERROR",
                        "runtime-role",
                        ".github/ai-config-manifest.json",
                        message=f"{surface} must declare role '{expected_role}', not '{role}'",
                    )
                )
    return status, findings


# ---------------------------------------------------------------------------
# Check 2: Authority classification
# ---------------------------------------------------------------------------


def _validate_manifest_schema(data: dict[str, Any]) -> list[str]:
    """Validate manifest structure recursively. Returns error messages."""
    return [
        *_schema_version_errors(data),
        *_string_list_errors(data),
        *_artifact_errors(data),
        *_mcp_server_errors(data),
        *_runtime_role_errors(data),
        *_copilot_section_errors(data),
    ]


def _schema_version_errors(data: dict[str, Any]) -> list[str]:
    if type(data.get("schemaVersion")) is not int:
        return ["schemaVersion must be an integer"]
    if data["schemaVersion"] != 1:
        return [f"schemaVersion {data['schemaVersion']} is not supported (expected 1)"]
    return []


def _string_list_errors(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for name in ("runtimes", "surfaces", "features"):
        val = data.get(name)
        if val is None:
            errors.append(f"{name} is required")
            continue
        if not isinstance(val, list):
            errors.append(f"{name} must be a list")
            continue
        for i, item in enumerate(val):
            if not isinstance(item, str):
                errors.append(f"{name}[{i}] must be a string")
    return errors


def _artifact_errors(data: dict[str, Any]) -> list[str]:
    artifacts = data.get("artifacts")
    if artifacts is None:
        return ["artifacts is required"]
    if not isinstance(artifacts, list):
        return ["artifacts must be a list"]
    return [error for i, art in enumerate(artifacts) for error in _artifact_entry_errors(i, art)]


def _artifact_entry_errors(i: int, art: Any) -> list[str]:
    if not isinstance(art, dict):
        return [f"artifacts[{i}] must be an object"]
    path_val = art.get("path")
    if not isinstance(path_val, str):
        return [f"artifacts[{i}].path must be a string"]
    hash_val = art.get("hash")
    is_json = path_val.endswith(".json") and path_val != ".github/ai-config-manifest.json"
    if is_json:
        if not isinstance(hash_val, str) or not hash_val:
            return [f"artifacts[{i}].hash is required for JSON artifact '{path_val}'"]
    elif hash_val is not None and not isinstance(hash_val, str):
        return [f"artifacts[{i}].hash must be a string or absent"]
    return []


def _mcp_server_errors(data: dict[str, Any]) -> list[str]:
    mcp_servers = data.get("mcp_servers")
    if mcp_servers is None:
        return ["mcp_servers is required"]
    if not isinstance(mcp_servers, list):
        return ["mcp_servers must be a list"]
    return [error for i, srv in enumerate(mcp_servers) for error in _mcp_server_entry_errors(i, srv)]


def _mcp_server_entry_errors(i: int, srv: Any) -> list[str]:
    if not isinstance(srv, dict):
        return [f"mcp_servers[{i}] must be an object"]
    errors: list[str] = []
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
                errors.append(f"mcp_servers[{i}].targets[{j}] must be a string")
    return errors


def _runtime_role_errors(data: dict[str, Any]) -> list[str]:
    roles = data.get("runtimeRoles")
    if roles is None:
        return []
    if not isinstance(roles, dict):
        return ["runtimeRoles must be an object when present"]
    errors: list[str] = []
    for surface, role in roles.items():
        if surface not in COPILOT_ROLE_BY_SURFACE:
            errors.append(f"runtimeRoles has unknown Copilot surface '{surface}'")
        if not isinstance(role, str):
            errors.append(f"runtimeRoles.{surface} must be a string")
    return errors


def _copilot_section_errors(data: dict[str, Any]) -> list[str]:
    copilot_sections = data.get("copilot_sections")
    if copilot_sections is not None and (
        not isinstance(copilot_sections, list) or not all(isinstance(section, str) for section in copilot_sections)
    ):
        return ["copilot_sections must be a list of strings when present"]
    return []


def _manifest_authority_finding(severity: str, message: str) -> Finding:
    return Finding(severity, "authority", ".github/ai-config-manifest.json", message=message)


def _manifest_signal(root: Path) -> tuple[str | None, dict[str, Any], list[Finding]]:
    """Return ("manifest", "alternative", or None), the manifest data that signal carries, and findings."""
    manifest_path = root / ".github/ai-config-manifest.json"
    if not manifest_path.is_file():
        return None, {}, []
    try:
        return _judge_manifest(root, json.loads(read_text(manifest_path)))
    except (json.JSONDecodeError, KeyError, OSError, UnicodeError):
        return None, {}, [_manifest_authority_finding("WARNING", "Manifest exists but is malformed")]


def _judge_manifest(root: Path, data: Any) -> tuple[str | None, dict[str, Any], list[Finding]]:
    if not isinstance(data, dict):
        return None, {}, [_manifest_authority_finding("WARNING", "Manifest is not a JSON object")]
    canonical = data.get("canonicalSource")
    if not data.get("generatedBy") or not canonical:
        return None, {}, []
    if canonical != "CLAUDE.md":
        return (
            "alternative",
            data,
            [_manifest_authority_finding("INFO", f"Manifest declares alternative canonical source: {canonical}")],
        )
    if data["generatedBy"] != "ai_config.py":
        return None, {}, []
    return _validate_claude_manifest(root, data)


def _validate_claude_manifest(root: Path, data: dict[str, Any]) -> tuple[str | None, dict[str, Any], list[Finding]]:
    """A manifest that names CLAUDE.md is a signal only when its schema, source, and artifacts hold."""
    schema_errors = _validate_manifest_schema(data)
    if schema_errors:
        return None, {}, [_manifest_authority_finding("ERROR", f"Manifest schema: {msg}") for msg in schema_errors]
    if not (root / "CLAUDE.md").is_file():
        return (
            None,
            {},
            [
                _manifest_authority_finding(
                    "ERROR", "Manifest declares canonical source 'CLAUDE.md' but it does not exist"
                )
            ],
        )
    if not any(
        isinstance(a, dict) and isinstance(a.get("path"), str) and a["path"] != ".github/ai-config-manifest.json"
        for a in data.get("artifacts", [])
    ):
        return None, {}, [_manifest_authority_finding("ERROR", "Manifest declares no derived artifacts")]
    return "manifest", data, []


def _has_claude_md(root: Path) -> bool:
    return (root / "CLAUDE.md").is_file()


def _has_maintaining_section(root: Path) -> bool:
    claude_path = root / "CLAUDE.md"
    if not claude_path.is_file():
        return False
    try:
        content = read_text(claude_path)
    except (OSError, UnicodeError):
        return False
    return re.search(r"^##\s+Maintaining\s+AI\s+Agent\s+Config", content, re.MULTILINE | re.IGNORECASE) is not None


def _has_generator_script(root: Path) -> bool:
    """True when a generator candidate references CLAUDE.md."""
    for candidate in GENERATOR_CANDIDATES:
        gen_path = root / candidate
        if not gen_path.is_file():
            continue
        try:
            if "CLAUDE.md" in read_text(gen_path):
                return True
        except (OSError, UnicodeError):
            pass
    return False


def _has_ci_parity_workflow(root: Path) -> bool:
    """True when a workflow runs the generator's parity check."""
    workflows_dir = root / ".github/workflows"
    if not workflows_dir.is_dir():
        return False
    for pattern in ("*.yml", "*.yaml"):
        for wf in workflows_dir.glob(pattern):
            try:
                wf_content = read_text(wf)
            except (OSError, UnicodeError):
                continue
            if "ai_config" in wf_content and "--check" in wf_content:
                return True
    return False


def _classify_signals(signals: list[str]) -> str:
    if "manifest" in signals or len(signals) >= 2:
        return "conforming"
    if not signals:
        return "unconfigured"
    return "ambiguous"


def classify_authority(root: Path) -> tuple[str, dict[str, Any], list[Finding]]:
    """Return (classification, manifest_data_or_empty, findings)."""
    manifest_signal, manifest_data, findings = _manifest_signal(root)
    if manifest_signal == "alternative":
        return "alternative", manifest_data, findings
    signals = [manifest_signal] if manifest_signal else []
    signals.extend(
        name
        for name, present in (
            ("claude_md_exists", _has_claude_md(root)),
            ("maintaining_section", _has_maintaining_section(root)),
            ("generator_script", _has_generator_script(root)),
            ("ci_parity", _has_ci_parity_workflow(root)),
        )
        if present
    )
    classification = _classify_signals(signals)
    findings.append(
        Finding(
            severity="INFO",
            check="authority",
            message=f"Authority classification: {classification} (signals: {', '.join(signals) or 'none'})",
        )
    )
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

COPILOT_BANNER = "> AUTO-GENERATED from CLAUDE.md. Do not edit directly — update CLAUDE.md instead."


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


def _expected_copilot_sections_body(root: Path, manifest: dict[str, Any]) -> str | None:
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
    if not isinstance(section_names, list) or not all(isinstance(section, str) for section in section_names):
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
    root: Path,
    skill_name: str,
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
    name_lines = [line for line in fm if line.startswith("name:")]
    desc_lines = [line for line in fm if line.startswith("description:")]
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


def _read_artifact(root: Path, path_str: str) -> tuple[str | None, str | None]:
    """Return (content, None) for a readable artifact, or (None, why it cannot be compared)."""
    full_path = root / path_str
    if not full_path.is_file():
        return None, f"Generated artifact missing: {path_str}"
    try:
        return read_text(full_path), None
    except (OSError, UnicodeError):
        return None, "Generated artifact exists but could not be read"


def _hash_problem(content: str, stored_hash: Any) -> str | None:
    if stored_hash and content_hash(content) != stored_hash:
        return "JSON artifact modified (hash mismatch with manifest)"
    return None


def _marker_problem(path_str: str, content: str) -> str | None:
    """Comment-supporting formats must carry the ownership marker."""
    if not path_str.endswith(".json") and not has_ownership_marker(content):
        return "Generated file missing ownership marker"
    return None


def _copilot_content_problem(root: Path, manifest: dict[str, Any], content: str) -> str | None:
    expected_body = _expected_copilot_sections_body(root, manifest)
    if expected_body is None:
        return None
    if COPILOT_BANNER not in content:
        return "Copilot instructions missing banner"
    after_banner = content[content.index(COPILOT_BANNER) + len(COPILOT_BANNER) :].strip()
    if after_banner != expected_body.strip():
        return "Copilot instructions sections do not match CLAUDE.md content"
    return None


def _expected_artifact(root: Path, path_str: str) -> str | None:
    """The deterministic content of AGENTS.md or a skill shim, or None for other artifacts."""
    if path_str == "AGENTS.md":
        return _expected_agents_adapter(root.name)
    match = re.match(r"\.agents/skills/([^/]+)/SKILL\.md$", path_str)
    return _expected_skill_shim(root, match.group(1)) if match else None


def _content_problem(root: Path, manifest: dict[str, Any], path_str: str, content: str) -> str | None:
    """Compare known comment-supporting artifacts with the content they are generated to hold."""
    if path_str.endswith(".json"):
        return None
    if path_str == ".github/copilot-instructions.md":
        return _copilot_content_problem(root, manifest, content)
    expected = _expected_artifact(root, path_str)
    if expected is not None and content != expected:
        return "Content does not match deterministic template"
    return None


def _artifact_problem(root: Path, manifest: dict[str, Any], artifact: dict[str, Any]) -> str | None:
    """The first check a manifest artifact fails, in order: path, existence, hash, marker, content."""
    path_str = artifact.get("path", "")
    path_error = validate_manifest_path(path_str)
    if path_error:
        return f"Manifest path rejected: {path_error}"
    content, read_problem = _read_artifact(root, path_str)
    if content is None:
        return read_problem
    return (
        _hash_problem(content, artifact.get("hash"))
        or _marker_problem(path_str, content)
        or _content_problem(root, manifest, path_str, content)
    )


def check_parity(
    root: Path,
    manifest: dict[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    for artifact in manifest.get("artifacts", []):
        problem = _artifact_problem(root, manifest, artifact)
        if problem:
            findings.append(Finding("ERROR", "parity", artifact.get("path", ""), message=problem))
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
            canonical_names = {p.parent.name for p in claude_dir.glob("*/SKILL.md")}
        for shim in agents_dir.glob("*/SKILL.md"):
            if shim.parent.name not in canonical_names:
                rel = shim.relative_to(root).as_posix()
                orphan_shims.add(rel)
                findings.append(
                    Finding(
                        severity="WARNING",
                        check="orphan",
                        path=rel,
                        message="Skill shim has no matching canonical skill",
                    )
                )

    # Marker-bearing generated files the manifest no longer lists. Copilot skill and
    # agent projections are reported by the provenance check instead.
    if manifest:
        listed = {artifact.get("path") for artifact in manifest.get("artifacts", []) if isinstance(artifact, dict)}
        candidates = [
            root / pattern for pattern in MANIFEST_ALLOWED_PATHS if "*" not in pattern and not pattern.endswith(".json")
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
                findings.append(
                    Finding(
                        severity="WARNING",
                        check="orphan",
                        path=rel,
                        message="Generated file is no longer listed in the manifest",
                    )
                )

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
        findings.append(
            Finding(
                severity="ERROR",
                check="mcp",
                path=rel_path,
                message=f"Could not read or parse: {e}",
            )
        )
        return {}, findings
    if not isinstance(data, dict):
        findings.append(
            Finding(
                severity="ERROR",
                check="mcp",
                path=rel_path,
                message="Top-level value must be an object",
            )
        )
        return {}, findings
    if wrapper_key not in data:
        findings.append(
            Finding(
                severity="WARNING",
                check="mcp",
                path=rel_path,
                message=f"'{wrapper_key}' key not found",
            )
        )
        return {}, findings
    servers = data[wrapper_key]
    if not isinstance(servers, dict):
        findings.append(
            Finding(
                severity="ERROR",
                check="mcp",
                path=rel_path,
                message=f"'{wrapper_key}' must be an object",
            )
        )
        return {}, findings
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            findings.append(
                Finding(
                    severity="WARNING",
                    check="mcp",
                    path=rel_path,
                    message=f"Server '{name}': entry must be an object, got {type(entry).__name__}",
                )
            )
            continue
        has_command = isinstance(entry.get("command"), str) and entry["command"].strip()
        has_url = isinstance(entry.get("url"), str) and entry["url"].strip()
        if not has_command and not has_url:
            findings.append(
                Finding(
                    severity="ERROR",
                    check="mcp",
                    path=rel_path,
                    message=f"Server '{name}': must have 'command' (STDIO/local) or 'url' (HTTP/SSE)",
                )
            )
    return servers, findings


def _check_duplicate_mcp_names(
    mcp_servers: dict[str, Any],
    github_mcp_servers: dict[str, Any],
) -> list[Finding]:
    """A .github/mcp.json server that .mcp.json also names is unreachable."""
    return [
        Finding(
            severity="ERROR",
            check="mcp",
            path=".github/mcp.json",
            message=f"Duplicate server name '{dup}' — .mcp.json takes "
            "precedence, making .github/mcp.json entry unreachable",
        )
        for dup in sorted(set(mcp_servers) & set(github_mcp_servers))
    ]


def _read_vscode_mcp(root: Path) -> tuple[dict[str, Any], list[Finding]]:
    """Parse .vscode/mcp.json, which wraps its servers in 'servers', not 'mcpServers'."""
    path = root / ".vscode/mcp.json"
    if not path.is_file():
        return {}, []
    try:
        data = json.loads(read_text(path))
    except (json.JSONDecodeError, OSError, UnicodeError) as e:
        message = f"Could not read or parse: {e}"
    else:
        if not isinstance(data, dict):
            message = "Top-level value must be an object"
        elif "servers" not in data:
            message = "VS Code MCP must use 'servers' wrapper (not 'mcpServers')"
        elif not isinstance(data["servers"], dict):
            message = "'servers' must be an object"
        else:
            return data["servers"], []
    return {}, [Finding(severity="ERROR", check="mcp", path=".vscode/mcp.json", message=message)]


def _read_codex_mcp(root: Path) -> tuple[dict[str, dict[str, Any]], list[Finding]]:
    """Read the object-valued mcp_servers entries of .codex/config.toml."""
    path = root / ".codex/config.toml"
    if not path.is_file():
        return {}, []
    try:
        with path.open("rb") as handle:
            config = tomllib.load(handle)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        message = f"Invalid TOML: {e}"
    except OSError as e:
        message = f"Error reading config: {e}"
    else:
        servers = config.get("mcp_servers", {})
        if not isinstance(servers, dict):
            return {}, []
        return {name: server for name, server in servers.items() if isinstance(server, dict)}, []
    return {}, [Finding(severity="ERROR", check="mcp", path=".codex/config.toml", message=message)]


def _check_codex_http_env(codex_servers: dict[str, dict[str, Any]]) -> list[Finding]:
    """env and env_vars configure a STDIO process, so a Codex server with a url must not carry them."""
    return [
        Finding(
            severity="ERROR",
            check="mcp",
            path=".codex/config.toml",
            message=f"Server '{name}': {key} is STDIO-only, must not be present on HTTP transport",
        )
        for name, server in codex_servers.items()
        if "url" in server
        for key in ("env", "env_vars")
        if key in server
    ]


def _check_manifest_transports(manifest: dict[str, Any]) -> list[Finding]:
    """Every manifest server's transport must be known and support each of its targets."""
    findings: list[Finding] = []
    for server in manifest.get("mcp_servers", []):
        name = server.get("name", "<unnamed>")
        transport = server.get("transport", "")
        if transport not in TRANSPORT_COMPATIBILITY:
            findings.append(
                Finding(
                    severity="ERROR",
                    check="mcp",
                    message=f"Server '{name}': unknown transport '{transport}'",
                )
            )
            continue
        supported = TRANSPORT_COMPATIBILITY[transport]
        findings.extend(
            Finding(
                severity="ERROR",
                check="mcp",
                message=f"Server '{name}': transport '{transport}' not supported for target '{target}'",
            )
            for target in server.get("targets", [])
            if target not in supported
        )
    return findings


def _check_copilot_local_tools(manifest: dict[str, Any], mcp_servers: dict[str, Any]) -> list[Finding]:
    """The shared .mcp.json cannot enforce a Copilot tool allowlist."""
    if "copilot_local" not in manifest_mcp_targets(manifest):
        return []
    return [
        Finding(
            severity="ERROR",
            check="mcp",
            path=".mcp.json",
            message=f"Server '{name}': copilot_local.tools allowlist cannot be enforced in shared .mcp.json",
        )
        for name, server in mcp_servers.items()
        if isinstance(server, dict) and server.get("tools") is not None and server.get("tools") != ["*"]
    ]


def _copilot_repository_mcp_warnings(manifest: dict[str, Any]) -> list[Finding]:
    """Repository MCP lives in repository settings, which a static audit cannot read."""
    if "copilot_repository" not in manifest_mcp_targets(manifest):
        return []
    findings = [
        Finding(
            severity="WARNING",
            check="mcp",
            message="Copilot repository MCP (cloud agent/code review) "
            "configured via repository settings — cannot validate statically",
        )
    ]
    if "code_review" in manifest.get("surfaces", []):
        findings.append(
            Finding(
                severity="WARNING",
                check="mcp",
                message="Code-review tool set derived from repository allowlist "
                "intersected with readOnlyHint: true — cannot verify "
                "tool annotations statically",
            )
        )
    return findings


def _normalize_server(server: dict[str, Any]) -> dict[str, Any]:
    """The connection fields that every copy of one server must agree on."""
    return {key: server[key] for key in ("command", "args", "url", "cwd", "env") if key in server}


def _check_mcp_parity(sources: list[tuple[str, dict[str, Any]]]) -> list[Finding]:
    """Warn when one server name has different connection fields in two configuration files."""
    copies: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for source, servers in sources:
        for name, server in servers.items():
            if isinstance(server, dict):
                copies.setdefault(name, []).append((source, _normalize_server(server)))
    findings: list[Finding] = []
    for name, entries in copies.items():
        base_source, base = entries[0]
        for other_source, other in entries[1:]:
            if base != other:
                findings.append(
                    Finding(
                        severity="WARNING",
                        check="mcp",
                        message=f"Server '{name}': connection fields differ between {base_source} and {other_source}",
                    )
                )
    return findings


def check_mcp(root: Path, manifest: dict[str, Any]) -> list[Finding]:
    mcp_servers, findings = _parse_mcp_json(root / ".mcp.json", ".mcp.json", "mcpServers")
    github_mcp_servers, github_findings = _parse_mcp_json(root / ".github/mcp.json", ".github/mcp.json", "mcpServers")
    findings.extend(github_findings)
    findings.extend(_check_duplicate_mcp_names(mcp_servers, github_mcp_servers))
    vscode_servers, vscode_findings = _read_vscode_mcp(root)
    findings.extend(vscode_findings)
    codex_servers, codex_findings = _read_codex_mcp(root)
    findings.extend(codex_findings)
    findings.extend(_check_codex_http_env(codex_servers))
    findings.extend(_check_manifest_transports(manifest))
    findings.extend(_check_copilot_local_tools(manifest, mcp_servers))
    findings.extend(_copilot_repository_mcp_warnings(manifest))
    findings.extend(
        _check_mcp_parity(
            [
                (".mcp.json", mcp_servers),
                (".github/mcp.json", github_mcp_servers),
                (".vscode/mcp.json", vscode_servers),
                (".codex/config.toml", codex_servers),
            ]
        )
    )
    return findings


# ---------------------------------------------------------------------------
# Check 6: Instruction layering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstructionSources:
    """The instruction files at a repository's root that the layering checks consider."""

    claude_md: bool
    agents_md: bool
    agents_redirects: bool
    agents_override: bool
    copilot_instructions: bool
    gemini_md: bool
    path_instructions: bool


def _instruction_sources(root: Path) -> InstructionSources:
    agents_md = (root / "AGENTS.md").is_file()
    agents_redirects = False
    if agents_md:
        with contextlib.suppress(OSError, UnicodeError):
            agents_redirects = is_redirecting_agents_md(read_text(root / "AGENTS.md"))
    instructions = root / ".github/instructions"
    return InstructionSources(
        claude_md=(root / "CLAUDE.md").is_file(),
        agents_md=agents_md,
        agents_redirects=agents_redirects,
        agents_override=(root / "AGENTS.override.md").is_file(),
        copilot_instructions=(root / ".github/copilot-instructions.md").is_file(),
        gemini_md=(root / "GEMINI.md").is_file(),
        path_instructions=instructions.is_dir() and any(instructions.rglob("*.instructions.md")),
    )


def _nested_files(root: Path, name: str) -> list[Path]:
    """Every file with this name below the repository root, outside SKIPPED_DIRECTORIES."""
    return [path for path in named_files(root, name) if path != root / name]


def _layering_codex(sources: InstructionSources) -> list[Finding]:
    """How Codex reaches CLAUDE.md from the repository root."""
    if sources.agents_override and sources.agents_md and sources.agents_redirects:
        return [
            Finding(
                severity="ERROR",
                check="layering",
                path="AGENTS.override.md",
                message="AGENTS.override.md masks the generated AGENTS.md adapter"
                " — Codex will not load CLAUDE.md through the adapter",
            )
        ]
    if sources.agents_md and sources.agents_redirects:
        return [Finding(severity="INFO", check="layering", message="Codex: AGENTS.md adapter redirects to CLAUDE.md")]
    if sources.agents_md:
        return [
            Finding(
                severity="WARNING",
                check="layering",
                message="Codex: non-redirecting AGENTS.md — CLAUDE.md not loaded through adapter",
            )
        ]
    if sources.claude_md:
        return [Finding(severity="INFO", check="layering", message="Codex: no AGENTS.md, CLAUDE.md used via fallback")]
    return [
        Finding(
            severity="ERROR",
            check="layering",
            message="Codex: no AGENTS.md or CLAUDE.md — no instructions available",
        )
    ]


def _nested_codex_instructions(root: Path, sources: InstructionSources) -> list[Finding]:
    """Codex concatenates instruction files from the root to the working directory."""
    findings = [
        Finding(
            severity="WARNING",
            check="layering",
            path=override.relative_to(root).as_posix(),
            message="Nested AGENTS.override.md takes precedence over "
            "AGENTS.md in this subtree — review for conflicting "
            "guidance with the root adapter",
        )
        for override in _nested_files(root, "AGENTS.override.md")
    ]
    if not (sources.agents_md and sources.agents_redirects):
        return findings
    for nested in _nested_files(root, "AGENTS.md"):
        try:
            if is_redirecting_agents_md(read_text(nested)):
                continue
        except (OSError, UnicodeError):
            continue
        findings.append(
            Finding(
                severity="WARNING",
                check="layering",
                path=nested.relative_to(root).as_posix(),
                message="Nested AGENTS.md adds instructions alongside "
                "the root adapter in this subtree — review "
                "for conflicting or redundant guidance",
            )
        )
    return findings


def _layering_copilot_cli(sources: InstructionSources) -> list[Finding]:
    """The instruction files Copilot CLI and the Copilot app load."""
    effective = [
        name
        for name, present in (
            ("CLAUDE.md", sources.claude_md),
            ("AGENTS.md", sources.agents_md),
            (".github/copilot-instructions.md", sources.copilot_instructions),
            ("GEMINI.md", sources.gemini_md),
        )
        if present
    ]
    if effective:
        first = Finding(
            severity="INFO",
            check="layering",
            message=f"Copilot CLI/app: effective sources: {', '.join(effective)}",
        )
    else:
        first = Finding(severity="ERROR", check="layering", message="Copilot CLI/app: no instruction sources available")
    return [
        first,
        Finding(
            severity="WARNING",
            check="layering",
            message="Copilot CLI/app folder trust status cannot be determined "
            "statically — .mcp.json silently skipped in untrusted directories",
        ),
    ]


def _layering_jetbrains(sources: InstructionSources) -> list[Finding]:
    """JetBrains cannot read CLAUDE.md, only Copilot instruction files."""
    if sources.copilot_instructions:
        return [
            Finding(severity="INFO", check="layering", message="JetBrains: .github/copilot-instructions.md available")
        ]
    if sources.path_instructions:
        return [
            Finding(
                severity="INFO",
                check="layering",
                message="JetBrains: path-specific instructions only (no copilot-instructions.md)",
            )
        ]
    return [
        Finding(
            severity="ERROR",
            check="layering",
            message="JetBrains: no .github/copilot-instructions.md or "
            "path-specific instructions — JetBrains cannot "
            "load CLAUDE.md directly",
        )
    ]


def _cloud_agent_selection(sources: InstructionSources) -> Finding:
    """The one root instruction file the cloud agent selects."""
    if sources.agents_md and sources.agents_redirects:
        return Finding(
            severity="INFO", check="layering", message="Cloud agent: AGENTS.md adapter redirects to CLAUDE.md"
        )
    if sources.agents_md:
        return Finding(
            severity="INFO",
            check="layering",
            message="Cloud agent: non-redirecting AGENTS.md — CLAUDE.md not loaded directly",
        )
    if sources.claude_md:
        return Finding(
            severity="INFO", check="layering", message="Cloud agent: no AGENTS.md, CLAUDE.md selected directly"
        )
    if sources.gemini_md:
        return Finding(
            severity="INFO",
            check="layering",
            message="Cloud agent: no AGENTS.md or CLAUDE.md, GEMINI.md selected as alternative",
        )
    return Finding(
        severity="ERROR",
        check="layering",
        message="Cloud agent: no AGENTS.md, CLAUDE.md, or GEMINI.md — no instructions available",
    )


def _layering_cloud_agent(root: Path, sources: InstructionSources) -> list[Finding]:
    """The cloud agent's root selection, and the nested AGENTS.md files that supersede the root adapter."""
    findings = [_cloud_agent_selection(sources)]
    if sources.agents_md and sources.agents_redirects:
        findings.extend(
            Finding(
                severity="WARNING",
                check="layering",
                path=nested.relative_to(root).as_posix(),
                message="Nested AGENTS.md supersedes root adapter for cloud agent sessions in this subtree",
            )
            for nested in _nested_files(root, "AGENTS.md")
        )
    return findings


def _layering_code_review(root: Path, sources: InstructionSources) -> list[Finding]:
    """Code review reads AGENTS.md, copilot-instructions, and path-specific instructions, but never CLAUDE.md or
    GEMINI.md, so a redirect-only AGENTS.md adds nothing."""
    review_sources = [
        name
        for name, present in (
            ("AGENTS.md", sources.agents_md and not sources.agents_redirects),
            (".github/copilot-instructions.md", sources.copilot_instructions),
            (".github/instructions", sources.path_instructions),
        )
        if present
    ]
    if review_sources:
        first = Finding(
            severity="INFO",
            check="layering",
            message=f"Code review: effective sources: {', '.join(review_sources)}",
        )
    else:
        first = Finding(
            severity="ERROR",
            check="layering",
            message="Code review: no project instructions it can read — it ignores "
            "CLAUDE.md, and AGENTS.md " + ("only redirects to CLAUDE.md" if sources.agents_md else "is absent"),
        )
    findings = [
        first,
        Finding(
            severity="WARNING",
            check="layering",
            message="Code-review custom-instructions enablement cannot be verified statically",
        ),
        Finding(
            severity="WARNING",
            check="trust-boundary",
            message="Copilot code review loads instructions, agents, and skills from the PR head; "
            "this is advisory context, not a trusted-base or trusted-ref review contract",
        ),
    ]
    if (root / ".github/skills").is_dir():
        findings.append(
            Finding(
                severity="INFO",
                check="layering",
                path=".github/skills",
                message="Code review can use relevant .github/skills entries; .claude/skills "
                "and .agents/skills are not its documented automatic skill location",
            )
        )
    return findings


def check_instruction_layering(
    root: Path,
    manifest: dict[str, Any],
) -> list[Finding]:
    surfaces = set(manifest.get("surfaces", []))
    sources = _instruction_sources(root)
    findings: list[Finding] = []
    if "codex" in set(manifest.get("runtimes", [])):
        findings.extend(_layering_codex(sources))
        findings.extend(_nested_codex_instructions(root, sources))
    if {"copilot_cli", "copilot_app"} & surfaces:
        findings.extend(_layering_copilot_cli(sources))
    if "jetbrains" in surfaces:
        findings.extend(_layering_jetbrains(sources))
    if "cloud_agent" in surfaces:
        findings.extend(_layering_cloud_agent(root, sources))
    if "code_review" in surfaces:
        findings.extend(_layering_code_review(root, sources))
    if {"copilot_cli", "copilot_app", "cloud_agent", "code_review"} & surfaces:
        findings.append(
            Finding(
                severity="WARNING",
                check="runtime",
                message="Copilot repository settings, organization policy, authentication, model availability, "
                "runtime enablement, and actual operational use cannot be verified statically",
            )
        )
    if "vscode" in surfaces:
        findings.append(
            Finding(
                severity="WARNING",
                check="layering",
                message="VS Code instruction settings (chat.useClaudeMdFile, "
                "chat.useAgentsMdFile, useInstructionFiles, "
                "includeApplyingInstructions) cannot be verified statically",
            )
        )
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
        findings.append(
            Finding(
                severity="WARNING",
                check="behavioral",
                path="CLAUDE.md",
                message="No 'never disable/suppress' rule found",
            )
        )

    if not re.search(r"\b(?:never|do not|don't)\s+work\s*around\b", lower):
        findings.append(
            Finding(
                severity="WARNING",
                check="behavioral",
                path="CLAUDE.md",
                message=(
                    "No rule forbidding workarounds for failing checks "
                    "(skip or expected-failure markers, weakened gates, TODO comments)"
                ),
            )
        )

    if not any(pattern.search(content) for pattern in QUALITY_GATE_PATTERNS):
        findings.append(
            Finding(
                severity="WARNING",
                check="behavioral",
                path="CLAUDE.md",
                message=(
                    "No quality gate found (a coverage threshold, a lint severity level, "
                    "or a rule that every check must pass)"
                ),
            )
        )

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
            findings.append(
                Finding(
                    severity="ERROR",
                    check="ownership",
                    path=path_str,
                    message="Generated artifact exists but could not be read",
                )
            )
            continue

        if path_str.endswith(".json"):
            # JSON: ownership tracked via manifest hash
            stored_hash = artifact.get("hash")
            if not stored_hash and path_str != ".github/ai-config-manifest.json":
                findings.append(
                    Finding(
                        severity="ERROR",
                        check="ownership",
                        path=path_str,
                        message="JSON artifact has no hash in manifest",
                    )
                )
        else:
            # Comment-supporting: ownership tracked via embedded marker
            if not has_ownership_marker(content):
                findings.append(
                    Finding(
                        severity="ERROR",
                        check="ownership",
                        path=path_str,
                        message="Generated file missing embedded ownership marker",
                    )
                )

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
                findings.append(
                    Finding(
                        severity="ERROR",
                        check="collision",
                        path="AGENTS.md",
                        message="User-authored AGENTS.md would conflict with generated adapter",
                    )
                )

    copilot_path = root / ".github/copilot-instructions.md"
    if copilot_path.is_file():
        try:
            content = read_text(copilot_path)
        except (OSError, UnicodeError):
            content = ""
        if not has_ownership_marker(content):
            surfaces = manifest.get("surfaces", [])
            copilot_surfaces = {
                "vscode",
                "jetbrains",
                "copilot_app",
                "copilot_cli",
                "cloud_agent",
                "code_review",
            }
            if copilot_surfaces & set(surfaces):
                findings.append(
                    Finding(
                        severity="ERROR",
                        check="collision",
                        path=".github/copilot-instructions.md",
                        message="User-authored copilot-instructions.md would conflict with generated projection",
                    )
                )

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
        artifact["path"]
        for artifact in manifest.get("artifacts", [])
        if isinstance(artifact, dict) and isinstance(artifact.get("path"), str)
    ]
    for path in sorted(artifact_paths):
        if path.endswith((".toml", ".yml", ".yaml")):
            note(path, "Only the ownership marker is checked; content drift inside this file is not detected")
    if manifest and "copilot_sections" not in manifest and ".github/copilot-instructions.md" in artifact_paths:
        note(
            ".github/ai-config-manifest.json",
            "Manifest predates copilot_sections; Copilot parity assumes the default sections",
        )
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
    for nested in _nested_files(root, ".mcp.json"):
        note(nested.relative_to(root).as_posix(), "Nested .mcp.json files are not validated")
    return findings


# ---------------------------------------------------------------------------
# Main audit orchestrator
# ---------------------------------------------------------------------------


def audit(root: Path) -> AuditResult:
    """Run all audit checks and return the result."""
    root = root.resolve()  # a relative root such as "." has no name of its own
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
    lines = [f"SUMMARY {severity} {severities[severity]}" for severity in SEVERITY_ORDER]
    lines.extend(f"SUMMARY INFO {check} {info_checks[check]}" for check in sorted(info_checks))
    return lines


def format_markdown(result: AuditResult) -> str:
    sorted_findings = result.sorted_findings()
    lines = [
        f"## AI Config Audit — {result.repository}",
        "",
        f"Authority: **{result.authority}**",
        f"Scope: **{result.scope_status}**",
        "",
        f"RESULT {result.result}",
        *summary_lines(sorted_findings),
        "",
    ]

    if not sorted_findings:
        lines.append("No findings.")
        return "\n".join(lines) + "\n"

    lines.append("### Findings")
    lines.append("")
    lines.append("| Severity | Check | Path | Line | Message |")
    lines.append("|---|---|---|---|---|")
    for f in sorted_findings:
        line = "" if f.line is None else str(f.line)
        lines.append(f"| {f.severity} | {f.check} | {f.path or ''} | {line} | {f.message} |")
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
    parser = argparse.ArgumentParser(description="Read-only audit of AI agent configuration")
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
        print(f"FAILED {args.root} is not a Git repository (no .git found)")
        return 1

    result = audit(args.root)

    if args.json:
        print(format_json(result), end="")
    else:
        print(format_markdown(result), end="")

    return result.exit_code


if __name__ == "__main__":
    use_utf8_output()
    raise SystemExit(main())

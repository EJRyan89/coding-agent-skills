"""Render selected skills in memory and validate the rendered output."""

from __future__ import annotations

import json
import re
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import frontmatter, hashing, platform_support
from .config import DERIVED_VARIABLES
from .errors import DeployError
from .source import Source

TOKEN = re.compile(r"\{\{([A-Z_][A-Z0-9_]*)\}\}")
SUFFIX_CONTEXT = {
    ".sh": "shell",
    ".bash": "shell",
    ".ps1": "powershell",
    ".py": "python",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".md": "markdown",
    ".txt": "text",
}
FENCE_CONTEXT = {
    "bash": "shell",
    "sh": "shell",
    "shell": "shell",
    "powershell": "powershell",
    "ps1": "powershell",
    "pwsh": "powershell",
    "python": "python",
    "yaml": "yaml",
    "yml": "yaml",
    "json": "json",
    "toml": "toml",
}
UNSAFE_CHARACTERS = {
    "shell": "\"'$`\\\n\r",
    "powershell": "\"'$`\n\r",
    "python": "\"'\\\n\r",
    "yaml": "\"'\\#\n\r",
}
SHELLCHECK_HEADER = "# shellcheck shell=bash\n# shellcheck disable=SC2034,SC2154\n"


ADAPTER_STAGING = ".agent-adapters"
AGENT_STAGING = ".claude-agents"


@dataclass
class Staged:
    skills: dict[str, dict[str, bytes]] = field(default_factory=dict)
    adapters: dict[str, dict[str, bytes]] = field(default_factory=dict)
    shared: dict[str, bytes] = field(default_factory=dict)
    agents: dict[str, bytes] = field(default_factory=dict)  # file name -> Claude Code subagent definition

    def skill_hash(self, name: str) -> str:
        return hashing.tree_hash(self.skills[name])

    def adapter_hash(self, name: str) -> str:
        return hashing.tree_hash(self.adapters[name])

    def shared_hash(self, name: str) -> str:
        return hashing.file_hash(self.shared[name])

    def agent_hash(self, name: str) -> str:
        return hashing.file_hash(self.agents[name])

    def logical_files(self) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for name, tree in self.skills.items():
            files.update({f"{name}/{relative}": content for relative, content in tree.items()})
        for name, tree in self.adapters.items():
            files.update({f"{ADAPTER_STAGING}/{name}/{relative}": content for relative, content in tree.items()})
        files.update(self.shared)
        files.update({f"{AGENT_STAGING}/{name}": content for name, content in self.agents.items()})
        return files

    def write(self, staging_dir: Path) -> None:
        for logical, content in self.logical_files().items():
            target = staging_dir / logical
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (staging_dir / ADAPTER_STAGING).mkdir(parents=True, exist_ok=True)
        (staging_dir / AGENT_STAGING).mkdir(parents=True, exist_ok=True)


def _escape(key: str, value: str, context: str, logical: str) -> str:
    if context in ("json", "toml"):
        return json.dumps(value, ensure_ascii=False)[1:-1]
    unsafe = UNSAFE_CHARACTERS.get(context, "")
    offending = next((char for char in value if char in unsafe), None)
    if offending is None and context == "yaml" and ": " in value:
        offending = ": "
    if offending is not None:
        raise DeployError(
            f"ERROR: Variable {key} cannot be safely substituted into {logical} "
            f"({context} context): value contains {offending!r}"
        )
    return value


def _substitute(text: str, values: dict[str, str], context: str, logical: str) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        return _escape(key, values[key], context, logical)

    return TOKEN.sub(replace, text)


def _fence_context(opening: str) -> str:
    """The substitution context of a fence from its opening line, such as shell for ```bash or ```shell."""
    words = opening[3:].split()
    return FENCE_CONTEXT.get(words[0].casefold(), "text") if words else "text"


def _substitute_markdown(text: str, values: dict[str, str], logical: str) -> str:
    lines = text.split("\n")
    rendered: list[str] = []
    try:
        found = frontmatter.split(lines)
    except frontmatter.FrontmatterError:
        found = None  # An opening rule that is never closed is Markdown, not frontmatter.
    if found is not None:
        # The frontmatter is YAML, and the deployer re-reads it to write the runtime adapters.
        rendered = [_substitute(line, values, "yaml", logical) for line in lines[: found[1]]]
        lines = lines[found[1] :]
    context: str | None = None
    for line in lines:
        trimmed = line.strip()
        if context is None and trimmed.startswith("```"):
            context = _fence_context(trimmed)
            rendered.append(_substitute(line, values, "text", logical))
            continue
        if context is not None and trimmed == "```":
            context = None
            rendered.append(line)
            continue
        rendered.append(_substitute(line, values, context or "text", logical))
    return "\n".join(rendered)


def render_file(logical: str, content: bytes, values: dict[str, str]) -> bytes:
    context = SUFFIX_CONTEXT.get(Path(logical).suffix.casefold())
    if context is None or not values:
        return content
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    if not TOKEN.search(text):
        return content
    if context == "markdown":
        rendered = _substitute_markdown(text, values, logical)
    else:
        rendered = _substitute(text, values, context, logical)
    if rendered != text and context == "json":
        try:
            json.loads(rendered)
        except json.JSONDecodeError as exc:
            raise DeployError(f"ERROR: Rendered JSON validation failed: {logical}: {exc}") from exc
    if rendered != text and context == "toml":
        try:
            tomllib.loads(rendered)
        except tomllib.TOMLDecodeError as exc:
            raise DeployError(f"ERROR: Rendered TOML validation failed: {logical}: {exc}") from exc
    return rendered.encode("utf-8")


MAX_DESCRIPTION = 1024  # The Agent Skills limit; GitHub Copilot CLI refuses to load a longer description.
# Codex reads a skill's invocation policy from this file beside SKILL.md, not from its frontmatter.
CODEX_METADATA = "agents/openai.yaml"
CODEX_USER_ONLY = b"policy:\n  allow_implicit_invocation: false\n"


def _adapter_frontmatter(skill: str, rendered: bytes) -> tuple[str, bool]:
    """The adapter's description and whether only the user may start the skill, from its rendered SKILL.md."""
    try:
        document = frontmatter.parse(rendered.decode("utf-8-sig"))
        description = document.string("description")
        user_only = (document.string("disable-model-invocation") or "").casefold() == "true"
    except (UnicodeError, frontmatter.FrontmatterError) as exc:
        raise DeployError(f"ERROR: Skill '{skill}' frontmatter cannot be read: {exc}") from exc
    if not description or not description.strip():
        raise DeployError(f"ERROR: Skill '{skill}' has no description for its runtime adapter")
    if len(description) > MAX_DESCRIPTION:
        raise DeployError(
            f"ERROR: Skill '{skill}' description is {len(description)} characters; "
            f"runtime adapters allow at most {MAX_DESCRIPTION}"
        )
    return description, user_only


def _adapter(skill: str, home: str, guidance: list[str], rendered: bytes) -> dict[str, bytes]:
    """The adapter another runtime loads. Claude reads the authoritative skill directly and never sees it.

    It carries the skill's description, which Codex and Copilot match requests against. A skill only the user
    may start keeps a description that names only the skill, so no runtime can select it by purpose, and
    also sets each runtime's own switch for explicit-only invocation. `allowed-tools` is not carried: Codex
    ignores it, and Copilot matches a shell grant by command name, so the closest grant to a skill's scripts
    would pre-approve every Python command ("Granting tools" in docs/adding-a-skill.md). Each runtime asks as
    it normally would.

    A shared Markdown asset the skill declares is runtime guidance (such as the Claude tool and model
    mapping), so only the adapter points to it; the skill itself does not spend a read on it under Claude.

    Skills name their own files through `${CLAUDE_SKILL_DIR}`, which Claude Code substitutes when it loads a
    skill. Other runtimes do not, and their base directory is this adapter's, so the adapter states the value.
    """
    description, user_only = _adapter_frontmatter(skill, rendered)
    if user_only:
        description = f"Runtime adapter for the authoritative {skill} skill, which only the user starts."
    directory = f"{home}/.claude/skills/{skill}"
    authoritative = f"{directory}/SKILL.md"
    lines = [
        "---",
        f"name: {skill}",
        f"description: {json.dumps(description, ensure_ascii=False)}",
        *(["disable-model-invocation: true"] if user_only else []),
        "---",
        "",
        "# Runtime skill adapter",
        "",
        *(
            f"Before anything else, read and apply `{home}/.claude/skills/{asset}`, which maps the skill's "
            "Claude tool, model, and path conventions to this runtime."
            for asset in guidance
        ),
        f"Read and follow `{authoritative}` as the authoritative skill instructions.",
        f"In that skill, `${{CLAUDE_SKILL_DIR}}` stands for `{directory}`, its own directory; "
        "substitute it in every path before running a command or reading a file.",
        "Use this runtime's native tools for equivalent operations. Do not copy, summarize, "
        "or independently extend the workflow in this adapter.",
    ]
    tree = {"SKILL.md": ("\n".join(lines) + "\n").encode("utf-8")}
    if user_only:
        tree[CODEX_METADATA] = CODEX_USER_ONLY
    return tree


def render(
    source: Source,
    selected: list[str],
    adapters: list[str],
    owned_shared: list[str],
    config: dict[str, str],
    skills_src: Path,
    agents: list[str] = (),
) -> Staged:
    staged = Staged()
    for name in selected:
        skill = source.skills[name]
        values = {key: config[key] for key in skill.required_vars if key in config}
        staged.skills[name] = {
            relative: render_file(f"{name}/{relative}", content, values)
            for relative, content in hashing.read_tree(skill.directory).items()
        }
    for name in adapters:
        guidance = sorted(asset for asset in source.skills[name].shared_deps if asset.endswith(".md"))
        staged.adapters[name] = _adapter(name, config["HOME"], guidance, staged.skills[name]["SKILL.md"])
    derived = {key: config[key] for key in DERIVED_VARIABLES}
    for asset in owned_shared:
        staged.shared[asset] = render_file(asset, (skills_src / asset).read_bytes(), derived)
    # Agent definitions take no variables: their frontmatter (tools, model) is executable configuration.
    for file_name in agents:
        staged.agents[file_name] = source.agents[file_name[: -len(".md")]].read_bytes()
    return staged


def reject_unexpanded_tokens(staged: Staged) -> None:
    matches: list[str] = []
    for logical, content in sorted(staged.logical_files().items()):
        text = content.decode("utf-8", "replace")
        for number, line in enumerate(text.split("\n"), start=1):
            if TOKEN.search(line):
                matches.append(f"{logical}:{number}:{line}")
    if matches:
        raise DeployError("ERROR: Unexpanded tokens found in staged output:", *matches)


@dataclass(frozen=True)
class ShellUnit:
    path: Path
    origin: str
    is_block: bool


def _extract_units(staged: Staged, workspace: Path) -> list[ShellUnit]:
    files = staged.logical_files()
    units: list[ShellUnit] = []
    for logical in sorted(files):
        if Path(logical).suffix.casefold() in (".sh", ".bash"):
            target = workspace / "files" / logical
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(files[logical])
            units.append(ShellUnit(target, logical, False))
    for logical in sorted(files):
        if Path(logical).suffix.casefold() != ".md":
            continue
        block_number = 0
        block: list[str] | None = None
        for line in files[logical].decode("utf-8", "replace").split("\n"):
            trimmed = line.strip()
            if block is None:
                # Validate every fence substituted as shell, whichever name its info string uses.
                if trimmed.startswith("```") and _fence_context(trimmed) == "shell":
                    block_number += 1
                    block = []
            elif trimmed == "```":
                target = workspace / "blocks" / f"{len(units)}.sh"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(
                    (SHELLCHECK_HEADER + "".join(f"{text}\n" for text in block)).encode("utf-8")
                )
                units.append(ShellUnit(target, f"{logical} block {block_number}", True))
                block = None
            else:
                block.append(line)
        if block is not None:
            raise DeployError(f"ERROR: Unclosed Bash code fence in {logical}")
    return units


TOOL_PURPOSES = {
    "Git Bash": "checks rendered Bash syntax",
    "ShellCheck": "lints rendered Bash",
    "PowerShell": "parses rendered .ps1 files",
}


def _require_tools(units: list[ShellUnit], has_powershell: bool) -> dict[str, str]:
    """Locate every validation tool this content needs, reporting all missing tools at once."""
    found: dict[str, str | None] = {}
    if units:
        found["Git Bash"] = platform_support.find_bash()
        found["ShellCheck"] = platform_support.find_executable("shellcheck")
    if has_powershell:
        found["PowerShell"] = platform_support.find_powershell()
    missing = [tool for tool, path in found.items() if path is None]
    if missing:
        raise DeployError(
            "ERROR: Tools required to validate the rendered skills were not found:",
            *(
                f"  - {tool} ({TOOL_PURPOSES[tool]}): {platform_support.install_hint(tool)}"
                for tool in missing
            ),
            *platform_support.INSTALL_HELP,
            "This run installed nothing.",
        )
    return {tool: path for tool, path in found.items() if path is not None}


def _check_bash_syntax(units: list[ShellUnit], bash: str) -> None:
    with ThreadPoolExecutor(max_workers=min(8, len(units))) as pool:
        results = list(
            pool.map(
                lambda unit: platform_support.run_tool(
                    [bash, "-n", platform_support.normalize(unit.path)]
                ),
                units,
            )
        )
    for unit, result in zip(units, results):
        if result.returncode != 0:
            kind = "block syntax" if unit.is_block else "syntax"
            raise DeployError(
                f"ERROR: Rendered Bash {kind} validation failed: {unit.origin}",
                *result.output.rstrip().splitlines(),
            )


def _run_shellcheck(units: list[ShellUnit], shellcheck: str) -> None:
    arguments = [platform_support.normalize(unit.path) for unit in units]
    result = platform_support.run_tool([shellcheck, "--format=gcc", *arguments])
    if result.returncode == 0:
        return
    origins = {platform_support.normalize(unit.path): unit for unit in units}
    reported: dict[str, list[str]] = {}
    for line in result.output.splitlines():
        for path, unit in origins.items():
            if line.startswith(path + ":"):
                reported.setdefault(path, []).append(unit.origin + line[len(path) :])
                break
    for unit in units:
        path = platform_support.normalize(unit.path)
        if path in reported:
            prefix = "ShellCheck failed for rendered Bash block" if unit.is_block else (
                "ShellCheck failed for rendered content"
            )
            raise DeployError(f"ERROR: {prefix}: {unit.origin}", *reported[path])
    raise DeployError("ERROR: ShellCheck failed for rendered content", *result.output.splitlines())


POWERSHELL_PARSE = (
    "$ErrorActionPreference = 'Stop'; "
    "foreach ($file in (ConvertFrom-Json $env:RENDERED_PS1_FILES)) { "
    "$tokens = $null; $errors = $null; "
    "[System.Management.Automation.Language.Parser]::ParseFile($file, [ref]$tokens, [ref]$errors) | Out-Null; "
    "if ($errors.Count -gt 0) { [Console]::Out.WriteLine($file); "
    "$errors | ForEach-Object { [Console]::Out.WriteLine($_.Message) }; exit 1 } }"
)


def _powershell_files(staged: Staged) -> dict[str, bytes]:
    return {logical: content for logical, content in staged.logical_files().items() if logical.endswith(".ps1")}


def _parse_powershell(files: dict[str, bytes], workspace: Path, powershell: str) -> None:
    origins: dict[str, str] = {}
    for logical, content in sorted(files.items()):
        target = workspace / "powershell" / logical
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        origins[str(target)] = logical
    result = platform_support.run_tool(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_PARSE],
        {"RENDERED_PS1_FILES": json.dumps(list(origins))},
    )
    if result.returncode != 0:
        lines = result.output.strip().splitlines()
        failed = origins.get(lines[0].strip(), lines[0].strip()) if lines else "(unknown file)"
        raise DeployError(f"ERROR: Rendered PowerShell syntax validation failed: {failed}", *lines[1:])


def validate_executables(staged: Staged) -> None:
    with tempfile.TemporaryDirectory(prefix="deploy-render-") as temporary:
        workspace = Path(temporary)
        units = _extract_units(staged, workspace)
        powershell_files = _powershell_files(staged)
        tools = _require_tools(units, bool(powershell_files))
        if units:
            _check_bash_syntax(units, tools["Git Bash"])
            _run_shellcheck(units, tools["ShellCheck"])
        if powershell_files:
            _parse_powershell(powershell_files, workspace, tools["PowerShell"])

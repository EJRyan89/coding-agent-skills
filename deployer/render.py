"""Render selected skills in memory and validate the rendered output."""

from __future__ import annotations

import json
import re
import tempfile
import tomllib
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import frontmatter
from markdown_sections import FENCE_LANGUAGES, Fence, find_fences
from suite_names import is_suite_name

from . import fsops, hashing, platform_support
from .config import DERIVED_VARIABLES, INTERPRETER_VARIABLES
from .errors import DeployError
from .source import AGENT_SKILLS_RULE, XML_TAG_REMEDY, Source, xml_tag

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
# A fence's substitution context, by its label: the executable language skill-core's table names it by, which is the
# table validation reads executable fences by, or a data format.
FENCE_CONTEXT = {
    **{label: language for language, labels in FENCE_LANGUAGES.items() for label in labels},
    "yaml": "yaml",
    "yml": "yaml",
    "json": "json",
    "toml": "toml",
}
# The executable languages whose quoting the renderer knows. A token in a fence of any other is refused whatever its
# value, since no escaping is known to keep the value literal there.
ESCAPED_LANGUAGES = frozenset({"shell", "powershell", "python"})
UNESCAPED_CONTEXTS = frozenset(FENCE_LANGUAGES) - ESCAPED_LANGUAGES
UNSAFE_CHARACTERS = {
    "shell": "\"'$`\\\n\r",
    "powershell": "\"'$`\n\r",
    "python": "\"'\\\n\r",
    "yaml": "\"'\\#\n\r",
}
SHELLCHECK_HEADER = "# shellcheck shell=bash\n# shellcheck disable=SC2034,SC2154\n"
# One ShellCheck command line's length, kept a margin below the platform's limit; more units run as further batches.
SHELLCHECK_COMMAND_BUDGET = platform_support.COMMAND_LINE_LIMIT - 1_024
# Filesystem writes that tests/run_validation.py allows outside deployer/fsops.py, with the reason.
FSOPS_ALLOWED = {
    "tempfile.TemporaryDirectory": "validate_executables checks rendered scripts in a throwaway directory under the "
    "system temporary directory, outside every managed root, and a failure there changes no deployed state",
}


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
            _write(staging_dir / logical, content)
        fsops.make_directories(staging_dir / ADAPTER_STAGING)
        fsops.make_directories(staging_dir / AGENT_STAGING)


def _write(target: Path, content: bytes) -> None:
    fsops.make_directories(target.parent)
    fsops.write_file(target, content)


def _escape(key: str, value: str, context: str, logical: str) -> str:
    # An interpreter renders as a bare command word, so it is held to its fixed set in every context, never escaped.
    if key in INTERPRETER_VARIABLES and value not in INTERPRETER_VARIABLES[key]:
        raise DeployError(
            f"ERROR: Variable {key} cannot be substituted into {logical}: {value!r} is not one of "
            f"{', '.join(sorted(INTERPRETER_VARIABLES[key]))}"
        )
    if context in UNESCAPED_CONTEXTS:
        raise DeployError(
            f"ERROR: Variable {key} cannot be substituted into {logical}: the deployer knows no escaping for a "
            f"{context} fence; write the example in a Bash, PowerShell, or Python fence"
        )
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


def fence_context(fence: Fence) -> str:
    """A fence's substitution context, such as shell for ```bash, ~~~zsh, or ````console, and text for an unknown
    label."""
    return FENCE_CONTEXT.get(fence.language.casefold(), "text")


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
    # Each body line takes its fence's context; an opening or closing line, and prose, take the text context.
    contexts = ["text"] * len(lines)
    for fence in find_fences(lines):
        for index in fence.body():
            contexts[index] = fence_context(fence)
    rendered += [_substitute(line, values, context, logical) for line, context in zip(lines, contexts, strict=True)]
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
    tag = xml_tag(description)
    if tag:
        raise DeployError(
            f"ERROR: Skill '{skill}' description contains the XML tag '{tag}', {AGENT_SKILLS_RULE}", XML_TAG_REMEDY
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


def shipped_files(directory: Path) -> dict[str, bytes]:
    """A skill's files as a deployment holds them: every file under its directory but its regression suites.

    A suite runs in validation from the source tree, never where the skill is deployed, so it stays out of the staged
    tree, and the hash the manifest records, the dry run's comparison, and the uninstall all read the same files.
    """
    return {
        relative: content
        for relative, content in hashing.read_tree(directory).items()
        if not is_suite_name(PurePosixPath(relative).name)
    }


def render(
    source: Source,
    selected: list[str],
    adapters: list[str],
    owned_shared: list[str],
    config: dict[str, str],
    skills_src: Path,
    agents: Sequence[str] = (),
) -> Staged:
    staged = Staged()
    for name in selected:
        skill = source.skills[name]
        values = {key: config[key] for key in skill.required_vars if key in config}
        staged.skills[name] = {
            relative: render_file(f"{name}/{relative}", content, values)
            for relative, content in shipped_files(skill.directory).items()
        }
    for name in adapters:
        guidance = sorted(asset for asset in source.skills[name].shared_deps if asset.endswith(".md"))
        staged.adapters[name] = _adapter(name, config["HOME"], guidance, staged.skills[name]["SKILL.md"])
    derived = {key: config[key] for key in DERIVED_VARIABLES}
    for asset in owned_shared:
        staged.shared[asset] = render_file(asset, (skills_src / asset).read_bytes(), derived)
    # Agent definitions take no configured or path variables: their frontmatter (tools, model, hooks) is executable
    # configuration. They take the interpreter alone, a value from a fixed set, so a hook names Python as the platform
    # does; any other token is left for reject_unexpanded_tokens to refuse.
    interpreter = {key: config[key] for key in INTERPRETER_VARIABLES}
    for file_name in agents:
        content = source.agents[file_name[: -len(".md")]].read_bytes()
        staged.agents[file_name] = render_file(f"{AGENT_STAGING}/{file_name}", content, interpreter)
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
            _write(target, files[logical])
            units.append(ShellUnit(target, logical, False))
    for logical in sorted(files):
        if Path(logical).suffix.casefold() != ".md":
            continue
        lines = files[logical].decode("utf-8", "replace").split("\n")
        # Validate every fence substituted as shell, whichever name its info string uses.
        shell = [fence for fence in find_fences(lines) if fence_context(fence) == "shell"]
        for block_number, fence in enumerate(shell, start=1):
            if fence.closer is None:
                raise DeployError(f"ERROR: Unclosed Bash code fence in {logical}")
            target = workspace / "blocks" / f"{len(units)}.sh"
            body = "".join(f"{fence.text(lines[index])}\n" for index in fence.body())
            _write(target, (SHELLCHECK_HEADER + body).encode("utf-8"))
            units.append(ShellUnit(target, f"{logical} block {block_number}", True))
    return units


TOOL_PURPOSES = {
    platform_support.BASH_NAME: "checks rendered Bash syntax",
    "ShellCheck": "lints rendered Bash",
    "PowerShell": "parses rendered .ps1 files",
}


def _require_tools(units: list[ShellUnit], has_powershell: bool) -> dict[str, str]:
    """Locate every validation tool this content needs, reporting all missing tools at once."""
    found: dict[str, str | None] = {}
    if units:
        found[platform_support.BASH_NAME] = platform_support.find_bash()
        found["ShellCheck"] = platform_support.find_executable("shellcheck")
    if has_powershell:
        found["PowerShell"] = platform_support.find_powershell()
    missing = [tool for tool, path in found.items() if path is None]
    if missing:
        raise DeployError(
            "ERROR: Tools required to validate the rendered skills were not found:",
            *(f"  - {tool} ({TOOL_PURPOSES[tool]}): {platform_support.install_hint(tool)}" for tool in missing),
            *platform_support.INSTALL_HELP,
            "This run installed nothing.",
        )
    return {tool: path for tool, path in found.items() if path is not None}


def _check_bash_syntax(units: list[ShellUnit], bash: str) -> None:
    with ThreadPoolExecutor(max_workers=min(8, len(units))) as pool:
        results = list(
            pool.map(
                lambda unit: platform_support.run_tool([bash, "-n", platform_support.normalize(unit.path)]),
                units,
            )
        )
    for unit, result in zip(units, results, strict=True):
        if result.returncode != 0:
            kind = "block syntax" if unit.is_block else "syntax"
            raise DeployError(
                f"ERROR: Rendered Bash {kind} validation failed: {unit.origin}",
                *result.output.rstrip().splitlines(),
            )


def _shellcheck_batches(command: list[str], paths: list[str]) -> list[list[str]]:
    """The paths in order, split so each command line stays within SHELLCHECK_COMMAND_BUDGET."""
    batches: list[list[str]] = []
    batch: list[str] = []
    length = platform_support.command_line_length(command)
    for path in paths:
        added = 1 + platform_support.command_line_length([path])
        if batch and length + added > SHELLCHECK_COMMAND_BUDGET:
            batches.append(batch)
            batch, length = [], platform_support.command_line_length(command)
        batch.append(path)
        length += added
    return [*batches, batch] if batch else batches


def _run_shellcheck(units: list[ShellUnit], shellcheck: str) -> None:
    """Lint every unit, in batches when one command line cannot hold them all, and report every finding once."""
    command = [shellcheck, "--format=gcc"]
    origins = {platform_support.normalize(unit.path): unit for unit in units}
    reported: dict[str, list[str]] = {}
    unattributed: list[str] = []
    failed = False
    for batch in _shellcheck_batches(command, list(origins)):
        result = platform_support.run_tool([*command, *batch])
        if result.returncode == 0:
            continue
        failed = True
        for line in result.output.splitlines():
            path = next((path for path in batch if line.startswith(path + ":")), None)
            if path is None:
                unattributed.append(line)
            else:
                reported.setdefault(path, []).append(origins[path].origin + line[len(path) :])
    failing = [unit for unit in units if platform_support.normalize(unit.path) in reported]
    if failing:
        first = failing[0]
        prefix = "ShellCheck failed for rendered " + ("Bash block" if first.is_block else "content")
        findings = (line for unit in failing for line in reported[platform_support.normalize(unit.path)])
        raise DeployError(f"ERROR: {prefix}: {first.origin}", *findings)
    if failed:
        raise DeployError("ERROR: ShellCheck failed for rendered content", *unattributed)


POWERSHELL_PARSE = (
    "$ErrorActionPreference = 'Stop'; "
    "foreach ($file in (ConvertFrom-Json $env:RENDERED_PS1_FILES)) { "
    "$tokens = $null; $errors = $null; "
    "[System.Management.Automation.Language.Parser]::ParseFile($file, [ref]$tokens, [ref]$errors) | Out-Null; "
    "if ($errors.Count -gt 0) { [Console]::Out.WriteLine($file); "
    "$errors | ForEach-Object { [Console]::Out.WriteLine($_.Message) }; exit 1 } }"
)


def _powershell_files(staged: Staged) -> dict[str, bytes]:
    return {
        logical: content
        for logical, content in staged.logical_files().items()
        if Path(logical).suffix.casefold() == ".ps1"
    }


def _parse_powershell(files: dict[str, bytes], workspace: Path, powershell: str) -> None:
    origins: dict[str, str] = {}
    for logical, content in sorted(files.items()):
        target = workspace / "powershell" / logical
        _write(target, content)
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
            _check_bash_syntax(units, tools[platform_support.BASH_NAME])
            _run_shellcheck(units, tools["ShellCheck"])
        if powershell_files:
            _parse_powershell(powershell_files, workspace, tools["PowerShell"])

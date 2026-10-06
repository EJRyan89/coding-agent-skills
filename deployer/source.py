"""Discovery and validation of the deployable source tree."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import frontmatter, platform_support
from .config import CONFIGURED_VARIABLES, DERIVED_VARIABLES
from .errors import DeployError
from .paths import Paths
from .tools import SKILL_TOOLS

SOURCE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
RESERVED_NAME_PATTERN = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])$")
# The Agent Skills frontmatter rules ("YAML frontmatter requirements" in Anthropic's skill authoring best
# practices): a skill name may not contain these words, and neither its name nor its description an XML tag.
# The naming grammar already keeps angle brackets out of a name.
RESERVED_WORDS = ("anthropic", "claude")
XML_TAG_PATTERN = re.compile(r"</?[A-Za-z][^<>]*>")
AGENT_SKILLS_RULE = "which the Agent Skills frontmatter rules forbid"


@dataclass
class Skill:
    name: str
    directory: Path
    required_vars: list[str]
    shared_deps: list[str]
    skill_deps: list[str]
    selectable: bool
    tools: list[str] = field(default_factory=list)
    opt_in: bool = False
    agent_deps: list[str] = field(default_factory=list)
    optional_tools: list[str] = field(default_factory=list)


@dataclass
class Source:
    source_id: str
    name: str = ""
    skills: dict[str, Skill] = field(default_factory=dict)
    bundles: dict[str, list[str]] = field(default_factory=dict)
    skill_bundle: dict[str, str] = field(default_factory=dict)
    opt_in_bundles: set[str] = field(default_factory=set)
    shared_assets: dict[str, Any] = field(default_factory=dict)
    agents: dict[str, Path] = field(default_factory=dict)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def load_source_id(paths: Paths) -> str:
    if not paths.source_file.is_file():
        raise DeployError(f"ERROR: source.json not found at {platform_support.normalize(paths.source_dir)}")
    document = _load_json(paths.source_file)
    source_id = document.get("id") if isinstance(document, dict) else None
    shown = source_id if isinstance(source_id, str) else "null"
    if not isinstance(source_id, str) or not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise DeployError(f"ERROR: Invalid source ID format: {shown}")
    if len(source_id) > 128:
        raise DeployError("ERROR: Source ID exceeds 128 characters")
    return source_id


def is_linked_worktree(source_dir: Path) -> bool:
    """Whether source_dir is a linked git worktree: its .git file names a git directory holding a commondir file.

    A submodule also has a .git file, but its git directory has no commondir, so it is not mistaken for one.
    """
    marker = source_dir / ".git"
    if not marker.is_file():
        return False
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return False
    if not text.startswith("gitdir:"):
        return False
    git_dir = Path(text[len("gitdir:") :].strip())
    if not git_dir.is_absolute():
        git_dir = source_dir / git_dir
    return (git_dir / "commondir").is_file()


def reject_linked_worktree(paths: Paths) -> None:
    """A deployment records its source path, so it must not come from a worktree that is removed when its task ends."""
    if is_linked_worktree(paths.source_dir):
        raise DeployError(
            f"ERROR: {platform_support.normalize(paths.source_dir)} is a linked git worktree. Deploy from the "
            "main checkout on main instead; --dry-run is allowed here. See docs/parallel-sessions.md."
        )


def _source_name(document: dict[str, Any]) -> str:
    name = document.get("name")
    return name.strip() if isinstance(name, str) and name.isprintable() else ""


def load_source_name(paths: Paths) -> str:
    document = _load_json(paths.source_file)
    return _source_name(document) if isinstance(document, dict) else ""


def label(source_id: str, name: str) -> str:
    return f"{name} ({source_id})" if name else source_id


NAME_RULES = (
    "Rename it with only lowercase letters, digits, and hyphens, no leading or trailing hyphen, at most 64 characters, "
    "not a Windows device name such as con or nul, and, for a skill, without the reserved words anthropic or claude; "
    'see "Files" in docs/adding-a-skill.md.'
)
XML_TAG_REMEDY = 'Remove the tag, or write it without angle brackets; see "Files" in docs/adding-a-skill.md.'
METADATA_SHAPE = (
    "It must be a JSON object whose required_vars, shared_deps, skill_deps, tools, optional_tools, and agent_deps, "
    "where present, are lists of strings, and whose selectable and opt_in, where present, are true or false. "
    "docs/adding-a-skill.md describes each key."
)


def is_valid_name(name: str) -> bool:
    return (
        len(name) <= 64
        and bool(SKILL_NAME_PATTERN.fullmatch(name))
        and not RESERVED_NAME_PATTERN.fullmatch(name.upper())
    )


def reserved_word(name: str) -> str | None:
    """The first reserved word a skill name contains, or None."""
    return next((word for word in RESERVED_WORDS if word in name.casefold()), None)


def xml_tag(text: str) -> str | None:
    """The first XML tag in a skill's name or description, or None."""
    match = XML_TAG_PATTERN.search(text)
    return match.group() if match else None


def _reject_links(skills_src: Path) -> None:
    pending = [skills_src]
    if platform_support.is_reparse_point(skills_src) and not platform_support.is_link(skills_src):
        raise DeployError(
            f"ERROR: Reparse point found in deployable source tree: {platform_support.normalize(skills_src)}"
        )
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise DeployError(f"ERROR: Could not validate reparse points in deployable source tree: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink():
                raise DeployError(f"ERROR: Symlink found in deployable source tree: {platform_support.normalize(path)}")
            if platform_support.is_reparse_point(path):
                raise DeployError(
                    f"ERROR: Reparse point found in deployable source tree: {platform_support.normalize(path)}"
                )
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)


def frontmatter_name(path: Path, owner: str) -> str:
    """The `name` a skill or agent definition declares, or an empty string when it declares none."""
    try:
        return frontmatter.read(path).string("name") or ""
    except (OSError, UnicodeError, frontmatter.FrontmatterError) as exc:
        raise DeployError(f"ERROR: {owner} frontmatter cannot be read: {exc}") from exc


def _string_list(value: Any) -> list[str] | None:
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _discover_directories(paths: Paths) -> dict[str, Path]:
    skills_src = paths.skills_src
    directories: dict[str, Path] = {}
    for skill_md in sorted(skills_src.rglob("SKILL.md")):
        if not skill_md.is_file():
            continue
        found_dir = skill_md.parent
        relative = found_dir.relative_to(skills_src).parts
        shown = platform_support.normalize(skill_md)
        if len(relative) not in (1, 2):
            raise DeployError(
                f"ERROR: SKILL.md must be directly under skills/<name> or skills/<category>/<name>: {shown}"
            )
        if len(relative) == 2 and (skills_src / relative[0] / "SKILL.md").is_file():
            raise DeployError(f"ERROR: Skill directory '{relative[0]}' contains another SKILL.md: {shown}")
        name = found_dir.name
        if name in directories:
            raise DeployError(
                f"ERROR: Duplicate skill directory name '{name}': "
                f"{platform_support.normalize(directories[name])} and {platform_support.normalize(found_dir)}"
            )
        if not (paths.meta_dir / f"{name}.json").is_file():
            raise DeployError(f"ERROR: Skill '{name}' has no matching deploy-meta/{name}.json")
        directories[name] = found_dir
    return directories


def _load_skill(paths: Paths, name: str, directories: dict[str, Path]) -> Skill:
    if len(name) > 64:
        raise DeployError(f"ERROR: Skill name '{name}' exceeds 64 characters", NAME_RULES)
    if not SKILL_NAME_PATTERN.fullmatch(name):
        raise DeployError(f"ERROR: Skill name '{name}' does not match naming grammar", NAME_RULES)
    if RESERVED_NAME_PATTERN.fullmatch(name.upper()):
        raise DeployError(f"ERROR: Skill name '{name}' is a Windows reserved name", NAME_RULES)
    word = reserved_word(name)
    if word:
        raise DeployError(
            f"ERROR: Skill name '{name}' contains the reserved word '{word}', {AGENT_SKILLS_RULE}", NAME_RULES
        )
    directory = directories.get(name)
    if directory is None:
        raise DeployError(f"ERROR: deploy-meta/{name}.json has no matching skill directory")
    declared = frontmatter_name(directory / "SKILL.md", f"Skill '{name}'")
    if declared != name:
        raise DeployError(f"ERROR: Skill '{name}' frontmatter name '{declared}' does not match directory")
    metadata = _load_json(paths.meta_dir / f"{name}.json")
    shape_error = DeployError(f"ERROR: deploy-meta/{name}.json has an invalid metadata shape", METADATA_SHAPE)
    if not isinstance(metadata, dict):
        raise shape_error
    required = _string_list(metadata.get("required_vars"))
    shared = _string_list(metadata.get("shared_deps"))
    dependencies = _string_list(metadata.get("skill_deps"))
    tools = _string_list(metadata.get("tools"))
    optional_tools = _string_list(metadata.get("optional_tools"))
    agents = _string_list(metadata.get("agent_deps"))
    selectable = metadata.get("selectable", True)
    opt_in = metadata.get("opt_in", False)
    if (
        required is None
        or shared is None
        or dependencies is None
        or tools is None
        or optional_tools is None
        or agents is None
        or not isinstance(selectable, bool)
        or not isinstance(opt_in, bool)
    ):
        raise shape_error
    if opt_in and not selectable:
        raise DeployError(f"ERROR: Skill '{name}' is opt-in but not selectable; only menu items can be opt-in")
    known = set(DERIVED_VARIABLES) | set(CONFIGURED_VARIABLES)
    for variable in required:
        if variable not in known:
            raise DeployError(f"ERROR: Skill '{name}' requires unknown variable '{variable}'")
    for tool in [*tools, *optional_tools]:
        if tool not in SKILL_TOOLS:
            known_tools = ", ".join(sorted(SKILL_TOOLS))
            raise DeployError(f"ERROR: Skill '{name}' declares unknown tool '{tool}' (known tools: {known_tools})")
    both = sorted(set(tools) & set(optional_tools))
    if both:
        raise DeployError(f"ERROR: Skill '{name}' declares tool '{both[0]}' both required and optional")
    return Skill(
        name,
        directory,
        required,
        shared,
        dependencies,
        selectable,
        sorted(set(tools)),
        opt_in,
        agents,
        sorted(set(optional_tools)),
    )


def _load_bundles(document: dict[str, Any], source: Source) -> None:
    bundles = document.get("bundles", {})
    if bundles is None:
        bundles = {}
    if not isinstance(bundles, dict):
        raise DeployError("ERROR: source.json bundles must be an object")
    for bundle in sorted(bundles):
        if not is_valid_name(bundle):
            raise DeployError(f"ERROR: Bundle name '{bundle}' is invalid", NAME_RULES)
        if bundle in source.skills:
            raise DeployError(f"ERROR: Bundle '{bundle}' collides with a skill name")
        definition = bundles[bundle]
        members = definition.get("members") if isinstance(definition, dict) else None
        if not isinstance(members, list) or not members or not all(isinstance(member, str) for member in members):
            raise DeployError(f"ERROR: Bundle '{bundle}' must contain a non-empty string members array")
        duplicates = sorted({member for member in members if members.count(member) > 1})
        if duplicates:
            raise DeployError(f"ERROR: Bundle '{bundle}' contains duplicate member '{duplicates[0]}'")
        for member in members:
            if member not in source.skills:
                raise DeployError(f"ERROR: Bundle '{bundle}' references unknown member '{member}'")
            if not source.skills[member].selectable:
                raise DeployError(f"ERROR: Bundle '{bundle}' member '{member}' is not selectable")
            if member in source.skill_bundle:
                raise DeployError(
                    f"ERROR: Skill '{member}' belongs to both '{source.skill_bundle[member]}' and '{bundle}'"
                )
            if source.skills[member].opt_in:
                raise DeployError(
                    f"ERROR: Skill '{member}' is opt-in but belongs to bundle '{bundle}'; mark the bundle opt-in instead"
                )
            source.skill_bundle[member] = bundle
        opt_in = definition.get("opt_in", False)
        if not isinstance(opt_in, bool):
            raise DeployError(f"ERROR: Bundle '{bundle}' opt_in must be true or false")
        if opt_in:
            source.opt_in_bundles.add(bundle)
        source.bundles[bundle] = list(members)


def _validate_dependencies(source: Source) -> None:
    for name in sorted(source.skills):
        seen: set[str] = set()
        for dependency in source.skills[name].skill_deps:
            if dependency not in source.skills:
                raise DeployError(f"ERROR: Skill '{name}' depends on unknown skill '{dependency}'")
            if dependency in seen:
                raise DeployError(f"ERROR: Skill '{name}' contains duplicate skill dependency '{dependency}'")
            seen.add(dependency)
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(name: str) -> None:
        if state.get(name) == 1:
            raise DeployError(f"ERROR: Skill dependency cycle detected: {' '.join(stack)} {name}")
        if state.get(name) == 2:
            return
        state[name] = 1
        stack.append(name)
        for dependency in source.skills[name].skill_deps:
            visit(dependency)
        stack.pop()
        state[name] = 2

    for name in sorted(source.skills):
        visit(name)


def _discover_agents(paths: Paths) -> dict[str, Path]:
    """Claude Code subagent definitions: one `agents/<name>.md` file each, whose frontmatter names it."""
    root = paths.agents_src
    if not os.path.lexists(root):
        return {}
    if not root.is_dir():
        raise DeployError(f"ERROR: agents is not a directory: {platform_support.normalize(root)}")
    _reject_links(root)
    agents: dict[str, Path] = {}
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        shown = platform_support.normalize(entry)
        if not entry.is_file() or entry.suffix != ".md":
            raise DeployError(f"ERROR: agents may contain only <name>.md agent definitions: {shown}")
        name = entry.stem
        if not is_valid_name(name):
            raise DeployError(f"ERROR: Agent name '{name}' does not match naming grammar", NAME_RULES)
        declared = frontmatter_name(entry, f"Agent '{name}'")
        if declared != name:
            raise DeployError(f"ERROR: Agent '{name}' frontmatter name '{declared}' does not match its file name")
        agents[name] = entry
    return agents


def discover(paths: Paths, source_id: str) -> Source:
    _reject_links(paths.skills_src)
    directories = _discover_directories(paths)
    source = Source(source_id)
    for meta_file in sorted(paths.meta_dir.glob("*.json")):
        if meta_file.is_file():
            source.skills[meta_file.stem] = _load_skill(paths, meta_file.stem, directories)
    document = _load_json(paths.source_file)
    document = document if isinstance(document, dict) else {}
    source.name = _source_name(document)
    _load_bundles(document, source)
    _validate_dependencies(source)
    source.agents = _discover_agents(paths)
    for name in sorted(source.skills):
        for agent in source.skills[name].agent_deps:
            if agent not in source.agents:
                raise DeployError(f"ERROR: Skill '{name}' depends on unknown agent '{agent}'")
    shared = document.get("shared_assets", {})
    source.shared_assets = shared if isinstance(shared, dict) else {}
    return source


def expand(source: Source, bundles: list[str], skills: list[str]) -> list[str]:
    """Depth-first preorder expansion of bundle members, then skills, through dependencies."""
    ordered: list[str] = []

    def visit(name: str) -> None:
        if name in ordered:
            return
        ordered.append(name)
        for dependency in source.skills[name].skill_deps:
            visit(dependency)

    for bundle in bundles:
        for member in source.bundles[bundle]:
            visit(member)
    for skill in skills:
        visit(skill)
    return ordered


def agent_closure(source: Source, selected: list[str]) -> list[str]:
    """The agents the selected skills declare, as the file names deployed to ~/.claude/agents."""
    return sorted({f"{agent}.md" for name in selected for agent in source.skills[name].agent_deps})


def root_names(source: Source) -> list[str]:
    """Selectable skills that are not part of a bundle, which the menu offers on their own."""
    return sorted(name for name, skill in source.skills.items() if skill.selectable and name not in source.skill_bundle)


def is_opt_in(source: Source, root: str) -> bool:
    """Whether a menu item, a bundle or a root skill, is left out of --all unless chosen."""
    return root in source.opt_in_bundles or (root in source.skills and source.skills[root].opt_in)


def _closures(source: Source, bundles: list[str], skills: list[str]) -> list[tuple[str, list[str]]]:
    roots = [*((bundle, source.bundles[bundle]) for bundle in bundles), *((skill, [skill]) for skill in skills)]
    return [(root, expand(source, [], members)) for root, members in roots]


def tool_users(source: Source, bundles: list[str], skills: list[str]) -> dict[str, list[str]]:
    """Map each tool declared, required or optional, in these bundles' and skills' dependency closures to the roots
    that use it."""
    users: dict[str, list[str]] = {}
    for root, closure in _closures(source, bundles, skills):
        members = [source.skills[name] for name in closure]
        declared = {tool for member in members for tool in (*member.tools, *member.optional_tools)}
        for tool in sorted(declared):
            users.setdefault(tool, []).append(root)
    return {tool: sorted(names) for tool, names in sorted(users.items())}


def required_tools(source: Source, bundles: list[str], skills: list[str]) -> set[str]:
    """The tools some skill in these closures requires, rather than using only when it is installed."""
    closures = _closures(source, bundles, skills)
    return {tool for _, closure in closures for name in closure for tool in source.skills[name].tools}

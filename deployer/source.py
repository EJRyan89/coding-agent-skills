"""Discovery and validation of the deployable source tree.

The menu's closures, expand, root_names, and is_opt_in, are ejryan89-skill-core's skill_menu, which audit-skill's script
reads a source's plugins through too; they are imported here for the deployer's modules.
"""

from __future__ import annotations

import fnmatch
import graphlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import file_presence
import frontmatter
import json_writer
import runtime_support
from agent_skills import MAX_NAME, NAME_PATTERN, RESERVED_WORDS, XML_TAG_PATTERN
from skill_menu import expand as expand
from skill_menu import is_opt_in as is_opt_in
from skill_menu import root_names as root_names

from . import platform_support, state_files
from .config import CONFIGURED_VARIABLES, DERIVED_VARIABLES
from .errors import DEBUG_HINT, DeployError, os_error
from .names import safe_name_problem
from .paths import Paths
from .tools import SKILL_TOOLS

SOURCE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")
SOURCE_ID_MAX_LENGTH = 128
# The Agent Skills rules, NAME_PATTERN, RESERVED_WORDS, and XML_TAG_PATTERN from ejryan89-skill-core: every skill,
# bundle, agent, and renamed old name follows the specification's naming grammar, a skill name may not contain a
# reserved word, and neither its name nor its description an XML tag. The naming grammar already keeps angle brackets
# out of a name.
AGENT_SKILLS_RULE = "which the Agent Skills frontmatter rules forbid"
# The step a source.json the deployer cannot read takes: the checkout's own copy is the one to put back. It names a
# git command a guarded hub allows, where a deployment runs, rather than one that writes the work tree.
SOURCE_JSON_REMEDY = (
    "Put back the checkout's source.json as its last commit holds it, which 'git show HEAD:source.json' prints, "
    "then retry."
)
# The names the deployer reads a source by, each spelled only as here: Windows opens another case's spelling as the
# name, where Linux passes it over, so either platform would deploy a source the other refuses or reads otherwise.
SOURCE_JSON = "source.json"
SKILL_FILE = "SKILL.md"
METADATA_SUFFIX = ".json"


PRESENCE_ALLOWED = {
    "_reject_links": (
        "a DirEntry's is_dir, which answers from the listing and raises every error but not found, as "
        "file_presence's own listing does, so a failed stat is never read as no folder"
    ),
    "_discover_agents": (
        "names what agents/ may hold, only <name>.md agent definitions, which an entry of another kind breaks as "
        "much as a misnamed one; a stat that fails is refused with its reason before it"
    ),
}


@dataclass(frozen=True, kw_only=True)
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
    # Each runtime's support, when the metadata declares runtime_support; repository validation requires it.
    runtime_support: dict[str, runtime_support.Support] | None = None


@dataclass
class Source:
    source_id: str
    name: str = ""
    skills: dict[str, Skill] = field(default_factory=dict)
    bundles: dict[str, list[str]] = field(default_factory=dict)
    skill_bundle: dict[str, str] = field(default_factory=dict)
    opt_in_bundles: set[str] = field(default_factory=set)
    shared_assets: dict[str, str] = field(default_factory=dict)
    agents: dict[str, Path] = field(default_factory=dict)
    # source.json's renamed: each name a skill had in an earlier release, mapped to the skill's name now
    renamed: dict[str, str] = field(default_factory=dict)


def _load_json(path: Path) -> object:
    """A JSON file's content, or None when it is not JSON; an OSError while reading is raised, never read as None."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeError:
        return None
    return state_files.parse_json(text)


def any_case(name: str) -> str:
    """A glob pattern that matches `name` spelled in any case, on a file system that keeps case as on one that folds
    it, so a name spelled in another case is found on every platform."""
    return "".join(
        f"[{letter.upper()}{letter.lower()}]" if letter.upper() != letter.lower() else letter for letter in name
    )


def refuse_other_spellings(folder: Path, name: str, what: str, declared: frozenset[str] = frozenset()) -> None:
    """Refuse an entry of `folder` that spells `name` in another case, naming it as `what`. An entry spelled as a name
    among `declared`, one the source declares itself, is left to the plan, which refuses the two names as one. A stat
    or listing that fails raises its OSError."""
    for found in file_presence.matching(folder, any_case(name)):
        if found.name != name and found.name not in declared:
            raise DeployError(
                f"ERROR: {what} {platform_support.normalize(found)} is spelled in another case than {name}.",
                f"Rename it {name}, then retry.",
            )


def _load_source_document(paths: Paths) -> dict[str, object]:
    """source.json's object, refused by its fault: a read that fails as unreadable, and content that is not UTF-8 or
    not a JSON object the deployer reads, such as one that repeats a key or holds NaN, as malformed."""
    shown = platform_support.normalize(paths.source_file)
    try:
        text = paths.source_file.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise os_error(exc, "read source.json") from exc
    except UnicodeError:
        raise DeployError(f"ERROR: source.json is malformed: {shown} is not UTF-8 text.", SOURCE_JSON_REMEDY) from None
    document = state_files.parse_json(text)
    if not isinstance(document, dict):
        raise DeployError(
            f"ERROR: source.json is malformed: {shown} is not a JSON object the deployer reads.", SOURCE_JSON_REMEDY
        )
    return document


def load_source_id(paths: Paths) -> str:
    try:
        refuse_other_spellings(paths.tree, SOURCE_JSON, "The source file")
    except OSError as exc:
        raise os_error(exc, "read the source's folder") from exc
    if not state_files.is_present(paths.source_file, "source.json", SOURCE_JSON_REMEDY):
        raise DeployError(
            f"ERROR: source.json not found at {platform_support.normalize(paths.tree)}", SOURCE_JSON_REMEDY
        )
    document = _load_source_document(paths)
    source_id = document.get("id")
    shown = source_id if isinstance(source_id, str) else "null"
    if not isinstance(source_id, str) or not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise DeployError(f"ERROR: Invalid source ID format: {shown}", SOURCE_JSON_REMEDY)
    if len(source_id) > SOURCE_ID_MAX_LENGTH:
        raise DeployError(f"ERROR: Source ID exceeds {SOURCE_ID_MAX_LENGTH} characters", SOURCE_JSON_REMEDY)
    return source_id


def checkout_gone(directory: Path) -> str:
    """What a refusal adds after a checkout the manifest records: nothing while a folder is there, that it no longer
    exists when nothing is, a link to nothing included, that it is no longer a folder when something else is, and the
    reason when its stat fails, so neither of the last two is read as gone."""
    try:
        return "" if file_presence.is_directory(directory) else ", which no longer exists"
    except file_presence.NotADirectory:
        return ", which is no longer a folder"
    except OSError as exc:
        return f", which could not be read ({exc.strerror or exc})"


def is_linked_worktree(source_dir: Path) -> bool:
    """Whether source_dir is a linked git worktree: its .git file names a git directory holding a commondir file.

    A submodule also has a .git file, but its git directory has no commondir, so it is not mistaken for one. A .git
    file, or a commondir, that cannot be read is refused, never read as no worktree, since the answer opens a guard.
    """
    marker = source_dir / ".git"
    try:
        if not file_presence.is_present(marker):
            return False
        text = marker.read_text(encoding="utf-8").strip()
    except file_presence.NotAFile:
        # A .git folder is a main checkout's.
        return False
    except OSError as exc:
        raise os_error(exc, "read the source's .git file") from exc
    except UnicodeError:
        raise DeployError(
            f"ERROR: Could not read the source's .git file: {platform_support.normalize(marker)}: it is not UTF-8 text",
            "Put back the gitdir line Git wrote there, or deploy from a fresh clone.",
        ) from None
    if not text.startswith("gitdir:"):
        return False
    git_dir = Path(text[len("gitdir:") :].strip())
    if not git_dir.is_absolute():
        git_dir = source_dir / git_dir
    commondir = git_dir / "commondir"
    try:
        return file_presence.is_present(commondir)
    except OSError as exc:
        raise os_error(exc, "read the git directory the source's .git file names") from exc


def reject_linked_worktree(paths: Paths) -> None:
    """A deployment records its source path, so it must not come from a worktree that is removed when its task ends."""
    if is_linked_worktree(paths.source_dir):
        raise DeployError(
            f"ERROR: {platform_support.normalize(paths.source_dir)} is a linked git worktree.",
            "Deploy from the main checkout on main instead; --dry-run is allowed here. See docs/parallel-sessions.md.",
        )


def _source_name(document: dict[str, object]) -> str:
    name = document.get("name")
    return name.strip() if isinstance(name, str) and name.isprintable() else ""


def load_source_name(paths: Paths) -> str:
    return _source_name(_load_source_document(paths))


def label(source_id: str, name: str) -> str:
    return f"{name} ({source_id})" if name else source_id


NAME_RULES = (
    "Rename it with only lowercase letters, digits, and hyphens, no leading, trailing, or consecutive hyphens, at most "
    f"{MAX_NAME} characters, not a Windows device name such as con or nul, and, for a skill, without the reserved "
    'words anthropic or claude; see "Files" in docs/adding-a-skill.md.'
)
XML_TAG_REMEDY = 'Remove the tag, or write it without angle brackets; see "Files" in docs/adding-a-skill.md.'
METADATA_SHAPE = (
    "It must be a JSON object whose required_vars, shared_deps, skill_deps, tools, optional_tools, and agent_deps, "
    "where present, are lists of strings, whose selectable and opt_in, where present, are true or false, and "
    "whose runtime_support, where present, declares each runtime. docs/adding-a-skill.md describes each key."
)


def is_valid_name(name: str) -> bool:
    return (
        len(name) <= MAX_NAME
        and bool(NAME_PATTERN.fullmatch(name))
        and not platform_support.is_reserved_device_name(name)
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
    if platform_support.is_junction(skills_src):
        raise DeployError(
            f"ERROR: Reparse point found in deployable source tree: {platform_support.normalize(skills_src)}",
            REAL_FOLDER,
        )
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise DeployError(
                f"ERROR: Could not validate reparse points in deployable source tree: {exc}", COMPLETE_CHECKOUT
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            if platform_support.is_link_or_junction(path):
                found = "Reparse point" if platform_support.is_junction(path) else "Symlink"
                raise DeployError(
                    f"ERROR: {found} found in deployable source tree: {platform_support.normalize(path)}",
                    REAL_FOLDER,
                )
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)


def frontmatter_name(path: Path, owner: str) -> str:
    """The `name` a skill or agent definition declares, or an empty string when it declares none."""
    try:
        return frontmatter.read(path).string("name") or ""
    except (OSError, UnicodeError, frontmatter.FrontmatterError) as exc:
        raise DeployError(
            f"ERROR: {owner} frontmatter cannot be read: {exc}",
            f'Correct the frontmatter of {platform_support.normalize(path)}; see "Files" in docs/adding-a-skill.md.',
        ) from exc


def _string_list(value: object) -> list[str] | None:
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _is_file(path: Path) -> bool:
    """Whether an entry a listing found is a regular file: anything else there is passed over, and a stat that fails
    raises its OSError, which discover refuses. Passing over a SKILL.md and a metadata file whose stats fail would
    drop their skill from the source, and the deployment would remove it as one the source no longer offers."""
    try:
        return file_presence.is_present(path)
    except file_presence.NotAFile:
        return False


def _discover_directories(paths: Paths, metadata: set[str]) -> dict[str, Path]:
    """Each skill directory by its name: one holding a SKILL.md directly under skills/, with a deploy-meta/<name>.json
    among `metadata`, the names the metadata listing found. SKILL.md is matched in any case, so a skill whose entry
    file is spelled otherwise is refused by name on every platform, rather than passed over where names keep their
    case and read where they fold it, then missed by the files staged under their own spelling."""
    skills_src = paths.skills_src
    directories: dict[str, Path] = {}
    for skill_md in file_presence.matching(skills_src, f"**/{any_case(SKILL_FILE)}"):
        if not _is_file(skill_md):
            continue
        found_dir = skill_md.parent
        relative = found_dir.relative_to(skills_src).parts
        shown = platform_support.normalize(skill_md)
        if len(relative) != 1:
            raise DeployError(
                f"ERROR: SKILL.md must be directly under skills/<name>: {shown}",
                "Move its skill to a folder of its own directly under skills/, and group skills with a bundle instead; "
                'see "Files" in docs/adding-a-skill.md.',
            )
        name = found_dir.name
        if skill_md.name != SKILL_FILE:
            raise DeployError(
                f"ERROR: Skill '{name}' entry file {shown} is spelled in another case than {SKILL_FILE}.",
                f"Rename it {SKILL_FILE}, then retry.",
            )
        if name not in metadata:
            raise DeployError(
                f"ERROR: Skill '{name}' has no matching deploy-meta/{name}.json",
                f'Add deploy-meta/{name}.json; see "Metadata" in docs/adding-a-skill.md.',
            )
        directories[name] = found_dir
    return directories


def _skill_directory(paths: Paths, name: str, directories: dict[str, Path]) -> Path:
    """The skill directory discovery found for a metadata file's name. A skill's folder whose SKILL.md is gone, or is
    no file, is refused as that, and something else in the folder's place as no directory, never as a folder that is
    not there; a stat that fails reaches the caller as its OSError."""
    directory = directories.get(name)
    if directory is not None:
        return directory
    try:
        folder = file_presence.is_directory(paths.skills_src / name)
    except file_presence.NotADirectory:
        raise DeployError(
            f"ERROR: skills/{name} is not a directory",
            f'Make skills/{name} the skill\'s folder, or remove deploy-meta/{name}.json; see "Files" in '
            "docs/adding-a-skill.md.",
        ) from None
    if folder:
        raise DeployError(
            f"ERROR: Skill '{name}' has no {SKILL_FILE} file in skills/{name}",
            f'Add skills/{name}/{SKILL_FILE}, or remove deploy-meta/{name}.json; see "Files" in '
            "docs/adding-a-skill.md.",
        )
    raise DeployError(
        f"ERROR: deploy-meta/{name}.json has no matching skill directory",
        f'Add skills/{name}/{SKILL_FILE}, or remove deploy-meta/{name}.json; see "Files" in docs/adding-a-skill.md.',
    )


def _load_skill(paths: Paths, name: str, directories: dict[str, Path]) -> Skill:
    if len(name) > MAX_NAME:
        raise DeployError(f"ERROR: Skill name '{name}' exceeds {MAX_NAME} characters", NAME_RULES)
    if not NAME_PATTERN.fullmatch(name):
        raise DeployError(f"ERROR: Skill name '{name}' does not match naming grammar", NAME_RULES)
    if platform_support.is_reserved_device_name(name):
        raise DeployError(f"ERROR: Skill name '{name}' is a Windows reserved name", NAME_RULES)
    word = reserved_word(name)
    if word:
        raise DeployError(
            f"ERROR: Skill name '{name}' contains the reserved word '{word}', {AGENT_SKILLS_RULE}", NAME_RULES
        )
    directory = _skill_directory(paths, name, directories)
    declared = frontmatter_name(directory / "SKILL.md", f"Skill '{name}'")
    if declared != name:
        raise DeployError(
            f"ERROR: Skill '{name}' frontmatter name '{declared}' does not match directory",
            f"Set name to '{name}' in skills/{name}/{SKILL_FILE}'s frontmatter; see \"Files\" in "
            "docs/adding-a-skill.md.",
        )
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
    metadata_file = f"deploy-meta/{name}.json"
    if opt_in and not selectable:
        raise DeployError(
            f"ERROR: Skill '{name}' is opt-in but not selectable; only menu items can be opt-in",
            f'Set selectable to true or opt_in to false in {metadata_file}; see "Opt-in skills and bundles" in '
            "docs/adding-a-skill.md.",
        )
    known = set(DERIVED_VARIABLES) | set(CONFIGURED_VARIABLES)
    for variable in required:
        if variable not in known:
            raise DeployError(
                f"ERROR: Skill '{name}' requires unknown variable '{variable}'",
                f"Remove it from required_vars in {metadata_file}, or declare it in deployer/config.py; see "
                '"Template values" in docs/adding-a-skill.md.',
            )
    for tool in [*tools, *optional_tools]:
        if tool not in SKILL_TOOLS:
            known_tools = ", ".join(sorted(SKILL_TOOLS))
            raise DeployError(
                f"ERROR: Skill '{name}' declares unknown tool '{tool}' (known tools: {known_tools})",
                f"Name a known tool in {metadata_file}, or add this one to deployer/tools.py; see "
                '"Commands skills may run" in docs/adding-a-skill.md.',
            )
    both = sorted(set(tools) & set(optional_tools))
    if both:
        raise DeployError(
            f"ERROR: Skill '{name}' declares tool '{both[0]}' both required and optional",
            f'Keep it in tools or in optional_tools in {metadata_file}, not both; see "Metadata" in '
            "docs/adding-a-skill.md.",
        )
    return Skill(
        name=name,
        directory=directory,
        required_vars=required,
        shared_deps=shared,
        skill_deps=dependencies,
        selectable=selectable,
        tools=sorted(set(tools)),
        opt_in=opt_in,
        agent_deps=agents,
        optional_tools=sorted(set(optional_tools)),
        runtime_support=_runtime_support(name, metadata),
    )


def _runtime_support(name: str, metadata: dict[str, object]) -> dict[str, runtime_support.Support] | None:
    if "runtime_support" not in metadata:
        return None
    try:
        return runtime_support.parse(metadata["runtime_support"])
    except runtime_support.RuntimeSupportError as exc:
        raise DeployError(f"ERROR: deploy-meta/{name}.json: {exc}", runtime_support.SHAPE) from exc


def _load_bundles(document: dict[str, object], source: Source) -> None:
    bundles = document.get("bundles", {})
    if bundles is None:
        bundles = {}
    if not isinstance(bundles, dict):
        raise DeployError("ERROR: source.json bundles must be an object", BUNDLE_SHAPE)
    for bundle in sorted(bundles):
        if not is_valid_name(bundle):
            raise DeployError(f"ERROR: Bundle name '{bundle}' is invalid", NAME_RULES)
        if bundle in source.skills:
            raise DeployError(f"ERROR: Bundle '{bundle}' collides with a skill name", RENAME_BUNDLE)
        definition = bundles[bundle]
        members = definition.get("members") if isinstance(definition, dict) else None
        if not isinstance(members, list) or not members or not all(isinstance(member, str) for member in members):
            raise DeployError(f"ERROR: Bundle '{bundle}' must contain a non-empty string members array", BUNDLE_SHAPE)
        duplicates = sorted({member for member in members if members.count(member) > 1})
        if duplicates:
            raise DeployError(
                f"ERROR: Bundle '{bundle}' contains duplicate member '{duplicates[0]}'",
                f"List '{duplicates[0]}' once in the bundle's members in source.json.",
            )
        for member in members:
            if member not in source.skills:
                raise DeployError(
                    f"ERROR: Bundle '{bundle}' references unknown member '{member}'",
                    f"Remove '{member}' from the bundle's members in source.json, or add the skill.",
                )
            if not source.skills[member].selectable:
                raise DeployError(
                    f"ERROR: Bundle '{bundle}' member '{member}' is not selectable",
                    f"Set selectable to true in deploy-meta/{member}.json, or remove '{member}' from the "
                    "bundle's members in source.json.",
                )
            if member in source.skill_bundle:
                raise DeployError(
                    f"ERROR: Skill '{member}' belongs to both '{source.skill_bundle[member]}' and '{bundle}'",
                    f"Remove '{member}' from one of the two bundles' members in source.json.",
                )
            if source.skills[member].opt_in:
                raise DeployError(
                    f"ERROR: Skill '{member}' is opt-in but belongs to bundle '{bundle}'",
                    f"Set opt_in to false in deploy-meta/{member}.json and mark the bundle opt-in instead; "
                    'see "Opt-in skills and bundles" in docs/adding-a-skill.md.',
                )
            source.skill_bundle[member] = bundle
        opt_in = definition.get("opt_in", False)
        if not isinstance(opt_in, bool):
            raise DeployError(f"ERROR: Bundle '{bundle}' opt_in must be true or false", BUNDLE_SHAPE)
        if opt_in:
            source.opt_in_bundles.add(bundle)
        source.bundles[bundle] = list(members)


def _load_renamed(document: dict[str, object], source: Source) -> None:
    """source.json's renamed: each old name a skill of this source had, mapped to the name it has now.

    The deployer carries an owned item under an old name to its new one, so an old name must be a valid name the
    source no longer uses, and its new name one of the source's skills that is no old name itself.
    """
    renamed = document.get("renamed", {})
    if not isinstance(renamed, dict):
        raise DeployError(
            "ERROR: source.json renamed must be an object mapping each old skill name to its new name", RENAMED_SHAPE
        )
    for old in sorted(renamed):
        new = renamed[old]
        if not is_valid_name(old):
            raise DeployError(f"ERROR: source.json renamed names an invalid old name '{old}'", NAME_RULES)
        if old in source.skills or old in source.bundles:
            raise DeployError(
                f"ERROR: source.json renames '{old}', which is still a skill or bundle of this source",
                f"Remove '{old}' from renamed in source.json, or finish the rename; "
                'see "Renaming a skill" in docs/adding-a-skill.md.',
            )
        if not isinstance(new, str) or new not in source.skills:
            raise DeployError(
                f"ERROR: source.json renames '{old}' to {json_writer.dumps(new)}, which is no skill of this source",
                RENAMED_SHAPE,
            )
    for old, new in sorted(renamed.items()):
        source.renamed[old] = new


def _validate_dependencies(source: Source) -> None:
    for name in sorted(source.skills):
        seen: set[str] = set()
        for dependency in source.skills[name].skill_deps:
            if dependency not in source.skills:
                raise DeployError(
                    f"ERROR: Skill '{name}' depends on unknown skill '{dependency}'",
                    f"Remove '{dependency}' from skill_deps in deploy-meta/{name}.json, or add the skill; "
                    'see "Metadata" in docs/adding-a-skill.md.',
                )
            if dependency in seen:
                raise DeployError(
                    f"ERROR: Skill '{name}' contains duplicate skill dependency '{dependency}'",
                    f"List '{dependency}' once in skill_deps in deploy-meta/{name}.json.",
                )
            seen.add(dependency)
    graph: graphlib.TopologicalSorter[str] = graphlib.TopologicalSorter()
    for name in sorted(source.skills):
        graph.add(name, *source.skills[name].skill_deps)
    try:
        graph.prepare()
    except graphlib.CycleError as exc:
        # graphlib lists each skill before the one that depends on it; reversed, each depends on the next.
        cycle = " ".join(reversed(exc.args[1]))
        raise DeployError(
            f"ERROR: Skill dependency cycle detected: {cycle}",
            "Remove one of these skill_deps entries from its skill's metadata in deploy-meta/ to break the cycle.",
        ) from exc


def _discover_agents(paths: Paths) -> dict[str, Path]:
    """Claude Code subagent definitions: one `agents/<name>.md` file each, whose frontmatter names it."""
    root = paths.agents_src
    try:
        if not file_presence.is_directory(root):
            return {}
    except file_presence.NotADirectory:
        raise DeployError(
            f"ERROR: agents is not a directory: {platform_support.normalize(root)}",
            f"Replace it with a folder of agent definitions, or remove it; {SEE_SUBAGENTS}",
        ) from None
    except OSError as exc:
        raise os_error(exc, "read the agents folder") from exc
    _reject_links(root)
    agents: dict[str, Path] = {}
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        shown = platform_support.normalize(entry)
        # An entry whose stat fails is refused with its reason, never as something other than an agent definition.
        try:
            is_file = file_presence.is_present(entry, other_is_absent=True)
        except OSError as exc:
            raise os_error(exc, "examine an entry of the agents folder") from exc
        if not is_file or entry.suffix != ".md":
            raise DeployError(
                f"ERROR: agents may contain only <name>.md agent definitions: {shown}",
                f"Move it out of agents/; {SEE_SUBAGENTS}",
            )
        name = entry.stem
        if not is_valid_name(name):
            raise DeployError(f"ERROR: Agent name '{name}' does not match naming grammar", NAME_RULES)
        declared = frontmatter_name(entry, f"Agent '{name}'")
        if declared != name:
            raise DeployError(
                f"ERROR: Agent '{name}' frontmatter name '{declared}' does not match its file name",
                f"Set name to '{name}' in agents/{name}.md's frontmatter; {SEE_SUBAGENTS}",
            )
        agents[name] = entry
    return agents


# What a refusal of the source's links, bundles, renamed skills, agents, and shared assets asks of its author.
REAL_FOLDER = "Replace it with the file or folder it points at, or remove it, then retry."
BUNDLE_SHAPE = (
    "Make bundles in source.json an object mapping each bundle's name to an object with a non-empty members list "
    'of skill names and, where present, an opt_in of true or false; see "Opt-in skills and bundles" in '
    "docs/adding-a-skill.md."
)
RENAME_BUNDLE = (
    'Rename the bundle in source.json so no skill has its name; see "Opt-in skills and bundles" in '
    "docs/adding-a-skill.md."
)
RENAMED_SHAPE = (
    "Make renamed in source.json map each old name to the name of one of the source's skills; see \"Renaming a "
    'skill" in docs/adding-a-skill.md.'
)
SEE_SUBAGENTS = 'see "Subagent definitions" in docs/adding-a-skill.md.'
SHARED_SHAPE = (
    "Make shared_assets in source.json map each asset's file name to its role, owner or dependency; see "
    '"Shared assets" in docs/adding-a-skill.md.'
)
# What a failure to read the source asks of the user.
COMPLETE_CHECKOUT = "Check that this checkout is complete and that you can read it, then retry."


def discover(paths: Paths, source_id: str) -> Source:
    try:
        return _discover(paths, source_id)
    except json_writer.JsonWriteRefused as exc:
        raise DeployError(f"ERROR: Could not read the skill source: {exc}", COMPLETE_CHECKOUT, DEBUG_HINT) from exc
    except OSError as exc:
        raise os_error(exc, "read the skill source", COMPLETE_CHECKOUT) from exc


def _metadata_files(meta_dir: Path) -> list[Path]:
    """deploy-meta's *.json files, sorted, or none when there is no deploy-meta. It is listed with iterdir, whose
    listing error raises, where Path.glob answers a folder it cannot list with no entries, which would drop every skill
    from the source and remove each deployed one. The suffix is matched in any case, and a file whose suffix is spelled
    otherwise is refused, which one platform would read and the other pass over. An entry that is not a regular file is
    passed over, and one whose stat fails raises its OSError."""
    if not file_presence.is_directory(meta_dir):
        return []
    suffix = f"*{any_case(METADATA_SUFFIX)}"
    found = sorted(entry for entry in meta_dir.iterdir() if fnmatch.fnmatchcase(entry.name, suffix))
    for entry in found:
        if not entry.name.endswith(METADATA_SUFFIX):
            spelled = f"{entry.name[: -len(METADATA_SUFFIX)]}{METADATA_SUFFIX}"
            raise DeployError(
                f"ERROR: Metadata file {platform_support.normalize(entry)} is spelled in another case than {spelled}.",
                f"Rename it {spelled}, then retry.",
            )
    return [entry for entry in found if _is_file(entry)]


def _discover(paths: Paths, source_id: str) -> Source:
    for folder in (paths.skills_src, paths.meta_dir, paths.agents_src):
        refuse_other_spellings(paths.tree, folder.name, "The source folder")
    _reject_links(paths.skills_src)
    metadata = _metadata_files(paths.meta_dir)
    directories = _discover_directories(paths, {meta_file.stem for meta_file in metadata})
    source = Source(source_id)
    for meta_file in metadata:
        source.skills[meta_file.stem] = _load_skill(paths, meta_file.stem, directories)
    document = _load_source_document(paths)
    source.name = _source_name(document)
    _load_bundles(document, source)
    _load_renamed(document, source)
    _validate_dependencies(source)
    source.agents = _discover_agents(paths)
    for name in sorted(source.skills):
        for agent in source.skills[name].agent_deps:
            if agent not in source.agents:
                raise DeployError(
                    f"ERROR: Skill '{name}' depends on unknown agent '{agent}'",
                    f"Add agents/{agent}.md, or remove '{agent}' from agent_deps in deploy-meta/{name}.json; "
                    f"{SEE_SUBAGENTS}",
                )
    source.shared_assets = _shared_assets(document)
    # An owned asset is read from skills/, beside each skill's folder and every other asset. An unsafe name is refused
    # by the plan before any path is built from it, and so is a name that is one with a skill's or another asset's.
    declared = frozenset((*source.skills, *source.shared_assets))
    for asset, role in sorted(source.shared_assets.items()):
        if role == "owner" and safe_name_problem(asset, "shared asset name") is None:
            refuse_other_spellings(paths.skills_src, asset, f"Shared asset '{asset}' file", declared)
    return source


def _shared_assets(document: dict[str, object]) -> dict[str, str]:
    """source.json's shared assets and their roles, refused when malformed rather than dropped or stringified."""
    shared = document.get("shared_assets", {})
    if not isinstance(shared, dict):
        raise DeployError(
            "ERROR: source.json shared_assets must be an object mapping each asset to its role", SHARED_SHAPE
        )
    for name, role in shared.items():
        if not isinstance(role, str):
            raise DeployError(
                f"ERROR: source.json shared_assets '{name}' has a role that is not a string", SHARED_SHAPE
            )
    return shared


def agent_closure(source: Source, selected: list[str]) -> list[str]:
    """The agents the selected skills declare, as the file names deployed to ~/.claude/agents."""
    return sorted({f"{agent}.md" for name in selected for agent in source.skills[name].agent_deps})


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

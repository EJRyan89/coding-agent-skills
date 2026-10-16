"""Scaffold a new skill: its SKILL.md frontmatter, its deploy metadata, and its section in the skill reference.

Usage:
  python tools/new_skill.py <name> --description TEXT [--argument-hint TEXT] [--user-only] [--opt-in]
                                   [--tool NAME ...] [--skill-dep NAME ...] [--allowed-tools ENTRY,ENTRY]
  python tools/new_skill.py <name> --repository --description TEXT [--argument-hint TEXT] [--user-only]
                                   [--allowed-tools ENTRY,ENTRY]

A model-invocable skill's description must say when to use it, and allowed-tools may grant Bash and PowerShell
only as twin patterns that end each path before any *, by default the skill's script named after it,
scripts/<name with underscores>.py: the rules validation applies, from skill_reference and the analyze-skill-cost
inventory.

It writes skills/<name>/SKILL.md (frontmatter and a title only), deploy-meta/<name>.json, and the generated
part of the skill's docs/skills.md section, then prints one REMAINING line for each thing validation still needs
from a person: each problem the skill reference check reports, such as the section's hand-written explanation and
the README entry; each allowed-tools grant that no step uses or whose pattern matches no command the skill shows,
by the analyze-skill-cost inventory's rules, which validation applies; and each --tool, which nothing the skill runs
yet runs. It refuses, changing nothing, when the name is invalid or already a shipped skill, a bundle, or a
repository skill under .claude/skills, which would make the cost audit's lookup by name ambiguous, when the name or
description breaks the Agent Skills frontmatter rules, a description over the length a runtime adapter allows
included, when a tool is not in deployer/tools.py, when a --skill-dep names no skill in the source, or when the
skill reference cannot be read or lacks a generated block; a write that fails partway removes what it wrote.

With --repository it scaffolds one of this repository's own skills instead: .claude/skills/<name>/SKILL.md, with
no allowed-tools unless given, since a repository skill runs the repository's tools rather than scripts of its own,
and its .agents/skills shim, written by tools/skill_shims.py, then prints the REMAINING lines for its grants. A
repository skill has no metadata or reference section, so it takes no --tool or --opt-in, and its name must not be a
shipped skill's, which would make the cost audit's lookup by name ambiguous.

It writes no scripts: validation requires a regression suite for any script under scripts/, so a stub would be
a placeholder that lands untested. Given --skill-dep, it records the skill, with skill-core, in skill_deps and prints
one SCRIPT_HEADER line for each line a script of the skill starts with: the line that puts skill-core on sys.path,
then a sibling_scripts call for each other dependency.

--root DIR scaffolds into another checkout of this repository, as the regression suite does. It refuses, as a usage
error, a directory that does not hold source.json and .claude/skills, or that lies in a skills directory.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "skill-core" / "scripts"))

from console import run_main
from skill_roots import sibling_scripts

from deployer import runtime_support
from deployer import source as deploy_source
from deployer.render import MAX_DESCRIPTION
from deployer.tools import SKILL_TOOLS
from tools import skill_reference, skill_shims
from tools.checkout import ROOT_HELP, checkout_root

# The analyze-skill-cost inventory owns the shell-grant rules. A deployed skill cannot import from this
# repository, so this imports the skill's script rather than keeping a second copy here.
sys.path.insert(0, str(sibling_scripts("analyze-skill-cost")))
import skill_inventory

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
# A shipped skill runs Python as {{PYTHON}}, which the deployer renders as the platform names it.
OWN_SCRIPTS = '{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/'


def default_allowed_tools(name: str) -> list[str]:
    """Both shells for the skill's script named after it, ending at the script's closing quote so no `..` path out
    of scripts/ matches, and the file-reading tool."""
    script = f'{OWN_SCRIPTS}{name.replace("-", "_")}.py" *'
    return [f"Bash({script})", f"PowerShell({script})", "Read"]


# A script finds skill-core by this one fixed line, and every other sibling through skill-core's lookup.
SKILL_CORE = "skill-core"
SKILL_CORE_LINE = f'sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "{SKILL_CORE}" / "scripts"))'


class ScaffoldError(Exception):
    """The skill cannot be scaffolded as asked; nothing was written."""


def _yaml_string(value: str) -> str:
    # A JSON string is a valid YAML double-quoted scalar, and skill_reference reads it back the same way.
    return json.dumps(value, ensure_ascii=False)


def skill_markdown(
    name: str, description: str, argument_hint: str | None, user_only: bool, allowed_tools: list[str] | None
) -> str:
    lines = ["---", f"name: {name}", f"description: {_yaml_string(description)}"]
    if argument_hint is not None:
        lines.append(f"argument-hint: {_yaml_string(argument_hint)}")
    if allowed_tools is not None:
        lines.append(f"allowed-tools: {json.dumps(allowed_tools)}")
    if user_only:
        lines.append("disable-model-invocation: true")
    title = name.replace("-", " ").capitalize()
    return "\n".join([*lines, "---", "", f"# {title}", ""])


def shell_grant_problems(allowed_tools: list[str]) -> list[str]:
    """Why each Bash or PowerShell entry grants every command, leaves one shell prompting, leaves a path open before
    its *, or names no script before it; empty when none."""
    found = []
    for line in skill_inventory.grant_findings(skill_inventory.allowed_entries(allowed_tools), []):
        kind, entry = line.split(" ", 1)
        if kind == "UNSCOPED_ALLOWED":
            found.append(
                f"allowed-tools entry {entry} grants {entry} for every command; scope it to what the skill runs"
            )
        elif kind in {"OPEN_PATH_ALLOWED", "SIBLING_FOLDER_ALLOWED"}:
            found.append(
                f"allowed-tools entry {entry} leaves a path open before its *, so it matches every script through "
                "..; grant each script by name, ending at its closing quote"
            )
        elif kind == "NO_SCRIPT_ALLOWED":
            found.append(
                f"allowed-tools entry {entry} names no script or git or gh subcommand before its *, so it pre-approves "
                "any code the * supplies; grant each script by name"
            )
        else:
            twin = "PowerShell" if entry.startswith("Bash") else "Bash"
            found.append(
                f"allowed-tools entry {entry} has no {twin} twin; on Windows the model may run a command "
                "through either shell"
            )
    return found


def metadata_text(document: dict[str, object]) -> str:
    """The canonical text of a deploy-meta file: one key per line, indented four spaces, each value on that line."""
    lines = [f"    {json.dumps(key)}: {json.dumps(value)}" for key, value in document.items()]
    return "{\n" + ",\n".join(lines) + "\n}\n"


def declared_dependencies(skill_deps: list[str]) -> list[str]:
    """The skill_deps a skill whose scripts import these skills declares: them and skill-core, sorted."""
    return sorted(set(skill_deps) | ({SKILL_CORE} if skill_deps else set()))


def script_header(skill_deps: list[str]) -> list[str]:
    """The lines a script of a skill with these skill_deps starts with, after its imports; none without any."""
    if not skill_deps:
        return []
    siblings = [name for name in skill_deps if name != SKILL_CORE]
    lookups = [f'sys.path.insert(0, str(sibling_scripts("{name}")))' for name in siblings]
    return [SKILL_CORE_LINE, *(["from skill_roots import sibling_scripts", *lookups] if siblings else [])]


def metadata(
    tools: list[str],
    opt_in: bool,
    user_only: bool = False,
    allowed: list[str] | None = None,
    skill_deps: list[str] | None = None,
) -> str:
    # A grant that runs Python through the interpreter token needs it rendered, so the skill declares it.
    interpreter = ["PYTHON"] if any("{{PYTHON}}" in grant for grant in allowed or []) else []
    document: dict[str, object] = {"required_vars": interpreter, "shared_deps": ["runtime-compatibility.md"]}
    if skill_deps:
        document["skill_deps"] = skill_deps
    if tools:
        document["tools"] = tools
    if opt_in:
        document["opt_in"] = True
    # The support that follows from what the frontmatter shows the skill needs, as validation derives it, with each
    # partial's default reason for the author to sharpen.
    document["runtime_support"] = runtime_support.declaration(
        runtime_support.frontmatter_needs(allowed or [], user_only)
    )
    return metadata_text(document)


def _one_line(label: str, value: str) -> str:
    value = value.strip()
    if not value or "\n" in value or "\r" in value:
        raise ScaffoldError(f"{label} must be one non-empty line")
    return value


def _frontmatter_values(
    name: str, description: str, argument_hint: str | None, user_only: bool, allowed_tools: list[str] | None
) -> tuple[str, str | None, list[str] | None]:
    """The description, argument hint, and allowed-tools as the frontmatter will carry them, each checked against
    the rules a shipped and a repository skill share."""
    if not deploy_source.is_valid_name(name):
        raise ScaffoldError(f"'{name}' is not a valid skill name; see docs/adding-a-skill.md")
    word = deploy_source.reserved_word(name)
    if word:
        raise ScaffoldError(f"'{name}' contains the reserved word '{word}', {deploy_source.AGENT_SKILLS_RULE}")
    description = _one_line("--description", description)
    tag = deploy_source.xml_tag(description)
    if tag:
        raise ScaffoldError(f"--description contains the XML tag '{tag}', {deploy_source.AGENT_SKILLS_RULE}")
    if len(description) > MAX_DESCRIPTION:
        raise ScaffoldError(
            f"--description is {len(description)} characters; runtime adapters allow at most {MAX_DESCRIPTION}"
        )
    if not user_only and not skill_reference.says_when(description):
        raise ScaffoldError(f"--description must {skill_reference.WHEN_RULE}")
    if argument_hint is not None:
        argument_hint = _one_line("--argument-hint", argument_hint)
    if allowed_tools is None:
        return description, argument_hint, None
    allowed = [tool.strip() for tool in allowed_tools]
    if not allowed or not all(allowed):
        raise ScaffoldError("--allowed-tools must name at least one tool")
    grant_problems = shell_grant_problems(allowed)
    if grant_problems:
        raise ScaffoldError(grant_problems[0])
    return description, argument_hint, allowed


def scaffold(
    root: Path,
    name: str,
    description: str,
    argument_hint: str | None = None,
    user_only: bool = False,
    opt_in: bool = False,
    tools: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    skill_deps: list[str] | None = None,
) -> list[str]:
    """Write the new skill's files and return the reference problems a person still has to resolve.

    A skill given skill_deps also depends on skill-core, whose line finds every other sibling.
    """
    description, argument_hint, allowed = _frontmatter_values(
        name,
        description,
        argument_hint,
        user_only,
        default_allowed_tools(name) if allowed_tools is None else allowed_tools,
    )
    tools = sorted(set(tools or []))
    unknown = [tool for tool in tools if tool not in SKILL_TOOLS]
    if unknown:
        raise ScaffoldError(f"unknown tool {unknown[0]}; add it to SKILL_TOOLS in deployer/tools.py first")
    existing = skill_reference.load_source(root)
    dependencies = declared_dependencies(skill_deps or [])
    absent = [dependency for dependency in dependencies if dependency not in existing.skills]
    if absent:
        raise ScaffoldError(f"--skill-dep {absent[0]} names no skill in the source")
    skill_md = root / "skills" / name / "SKILL.md"
    meta = root / "deploy-meta" / f"{name}.json"
    if name in existing.skills or name in existing.bundles or skill_md.parent.exists() or meta.exists():
        raise ScaffoldError(f"'{name}' is already a skill, a bundle, or an existing path")
    if (root / skill_shims.SKILLS / name).exists():
        raise ScaffoldError(
            f"'{name}' is already a repository skill under {skill_shims.SKILLS.as_posix()}, "
            "which would make the cost audit's lookup by name ambiguous"
        )
    reference = _current_reference(root, existing)

    try:
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text(
            skill_markdown(name, description, argument_hint, user_only, allowed),
            encoding="utf-8",
            newline="\n",
        )
        meta.write_text(metadata(tools, opt_in, user_only, allowed, dependencies), encoding="utf-8", newline="\n")
        skill_reference.write(root)
        found = skill_reference.problems(root)
    except BaseException:
        _remove_scaffold(root, skill_md, meta, reference)
        raise
    unrun = [
        f"deploy-meta/{name}.json declares the tool {tool}, which nothing the skill runs yet runs; run it from a "
        "script or a step, or remove it from tools"
        for tool in tools
    ]
    return [*found, *grant_remaining(root, skill_md.relative_to(root)), *unrun]


def _current_reference(root: Path, source: deploy_source.Source) -> str:
    """The skill reference's text, once it is known to hold every generated block and the reference check can read
    everything it reads, so a scaffold never stops after writing for want of them."""
    reference = root / skill_reference.REFERENCE
    if not reference.is_file():
        raise ScaffoldError(f"{skill_reference.REFERENCE.as_posix()} does not exist")
    try:
        document = reference.read_text(encoding="utf-8")
        skill_reference.regenerate(document, skill_reference.entries(source))
        skill_reference.problems(root)
    except UnicodeError as exc:
        raise ScaffoldError(f"{skill_reference.REFERENCE.as_posix()} or a file it reads is not UTF-8: {exc}") from exc
    return document


def _remove_scaffold(root: Path, skill_md: Path, meta: Path, reference: str) -> None:
    """Undo a scaffold that failed after it started writing: its two files, its folder, and the reference's update."""
    for path in (skill_md, meta):
        path.unlink(missing_ok=True)
    if skill_md.parent.is_dir() and not any(skill_md.parent.iterdir()):
        skill_md.parent.rmdir()
    path = root / skill_reference.REFERENCE
    if path.read_text(encoding="utf-8") != reference:
        path.write_text(reference, encoding="utf-8", newline="\n")


# What validation still needs of a fresh skill's grants, by the inventory line that says so.
GRANT_REMAINING = {
    "UNUSED_ALLOWED": "grants {detail}, which no step uses yet; use it in a step or remove it from allowed-tools",
    "IDLE_ALLOWED": "grants {detail}, which matches no command in its fences or code spans yet; show the command it "
    "covers or remove the grant",
}


def grant_remaining(root: Path, skill_md: Path) -> list[str]:
    """Each allowed-tools grant of the skill at skill_md, relative to root, that validation's grant check still fails,
    read by the analyze-skill-cost inventory as that check reads it."""
    found = []
    for line in skill_inventory.tools(root / skill_md):
        kind, _, detail = line.partition(" ")
        if kind in GRANT_REMAINING:
            found.append(f"{skill_md.as_posix()} " + GRANT_REMAINING[kind].format(detail=detail))
    return found


def scaffold_repository(
    root: Path,
    name: str,
    description: str,
    argument_hint: str | None = None,
    user_only: bool = False,
    allowed_tools: list[str] | None = None,
) -> list[Path]:
    """Write a new repository skill's SKILL.md and its shim, and return the paths written, relative to root, its
    SKILL.md first."""
    description, argument_hint, allowed = _frontmatter_values(
        name, description, argument_hint, user_only, allowed_tools
    )
    skill_md = skill_shims.SKILLS / name / "SKILL.md"
    shim = skill_shims.shim_path(name)
    if name in skill_reference.load_source(root).skills or (root / skill_md.parent).exists():
        raise ScaffoldError(f"'{name}' is already a shipped or repository skill, or an existing path")
    if (root / shim.parent).exists():
        raise ScaffoldError(f"{shim.parent.as_posix()} already exists")

    (root / skill_md.parent).mkdir(parents=True)
    (root / skill_md).write_text(
        skill_markdown(name, description, argument_hint, user_only, allowed), encoding="utf-8", newline="\n"
    )
    return [skill_md, *skill_shims.write(root, [name])]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("name")
    parser.add_argument("--description", required=True, help="what the skill does and when to use it, on one line")
    parser.add_argument("--argument-hint", help="the arguments, in the notation docs/skills.md explains")
    parser.add_argument(
        "--user-only", action="store_true", help="only the user may start it (disable-model-invocation)"
    )
    parser.add_argument("--opt-in", action="store_true", help="leave it out of deploy.py --all unless chosen")
    parser.add_argument(
        "--repository", action="store_true", help="scaffold a repository skill under .claude/skills, and its shim"
    )
    parser.add_argument("--tool", action="append", default=[], help="an external tool it runs, from deployer/tools.py")
    parser.add_argument(
        "--skill-dep", action="append", default=[], help="a skill in the source whose scripts its scripts import"
    )
    parser.add_argument(
        "--allowed-tools",
        help="comma-separated allowed-tools entries (default: Bash and PowerShell for scripts/<name with "
        "underscores>.py, and Read; none for a repository skill)",
    )
    parser.add_argument("--root", type=checkout_root, default=REPOSITORY_ROOT, metavar="DIR", help=ROOT_HELP)
    arguments = parser.parse_args(argv)
    allowed = None if arguments.allowed_tools is None else arguments.allowed_tools.split(",")
    if arguments.repository:
        return _main_repository(arguments, allowed)
    try:
        remaining = scaffold(
            arguments.root,
            arguments.name,
            arguments.description,
            arguments.argument_hint,
            arguments.user_only,
            arguments.opt_in,
            arguments.tool,
            allowed,
            arguments.skill_dep,
        )
    except (OSError, ScaffoldError, skill_reference.ReferenceError) as exc:
        print(f"FAILED {exc}", file=sys.stderr)
        return 2
    print(f"CREATED skills/{arguments.name}/SKILL.md")
    print(f"CREATED deploy-meta/{arguments.name}.json")
    print(f"UPDATED {skill_reference.REFERENCE.as_posix()}")
    for line in script_header(declared_dependencies(arguments.skill_dep)):
        print(f"SCRIPT_HEADER {line}")
    for problem in remaining:
        print(f"REMAINING {problem}")
    return 0


def _main_repository(arguments: argparse.Namespace, allowed: list[str] | None) -> int:
    try:
        if arguments.tool or arguments.opt_in or arguments.skill_dep:
            raise ScaffoldError(
                "a repository skill has no deploy metadata, so it takes no --tool, --opt-in, or --skill-dep"
            )
        written = scaffold_repository(
            arguments.root, arguments.name, arguments.description, arguments.argument_hint, arguments.user_only, allowed
        )
    except (OSError, ScaffoldError, skill_reference.ReferenceError) as exc:
        print(f"FAILED {exc}", file=sys.stderr)
        return 2
    for path in written:
        print(f"CREATED {path.as_posix()}")
    for problem in grant_remaining(arguments.root, written[0]):
        print(f"REMAINING {problem}")
    return 0


if __name__ == "__main__":
    run_main(main, errors="backslashreplace")

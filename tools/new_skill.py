"""Scaffold a new skill: its SKILL.md frontmatter, its deploy metadata, and its section in the skill reference.

Usage:
  python tools/new_skill.py <name> --description TEXT [--argument-hint TEXT] [--user-only] [--opt-in]
                                   [--tool NAME ...] [--allowed-tools ENTRY,ENTRY]

A model-invocable skill's description must say when to use it, and allowed-tools may grant Bash and PowerShell
only as twin patterns, by default the skill's own scripts: the rules validation applies, from skill_reference and
the analyze-skill-cost inventory.

It writes skills/<name>/SKILL.md (frontmatter and a title only), deploy-meta/<name>.json, and the generated
part of the skill's docs/skills.md section, then prints one REMAINING line for each thing validation still needs
from a person, such as the reference's hand-written explanation and the README entry. It refuses, changing
nothing, when the name is invalid or already used, when the name or description breaks the Agent Skills
frontmatter rules, or when a tool is not in deployer/tools.py.

It writes no scripts: validation requires a regression suite for any script under scripts/, so a stub would be
a placeholder that lands untested.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deployer import source as deploy_source
from deployer.tools import SKILL_TOOLS
from tools import skill_reference

# The analyze-skill-cost inventory owns the shell-grant rules. A deployed skill cannot import from this
# repository, so this imports the skill's script rather than keeping a second copy here.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "analyze-skill-cost" / "scripts"))
import skill_inventory

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
OWN_SCRIPTS = 'python -B "${CLAUDE_SKILL_DIR}/scripts/*'
DEFAULT_ALLOWED_TOOLS = [f"Bash({OWN_SCRIPTS})", f"PowerShell({OWN_SCRIPTS})", "Read"]


class ScaffoldError(Exception):
    """The skill cannot be scaffolded as asked; nothing was written."""


def _yaml_string(value: str) -> str:
    # A JSON string is a valid YAML double-quoted scalar, and skill_reference reads it back the same way.
    return json.dumps(value, ensure_ascii=False)


def skill_markdown(
    name: str, description: str, argument_hint: str | None, user_only: bool, allowed_tools: list[str]
) -> str:
    lines = ["---", f"name: {name}", f"description: {_yaml_string(description)}"]
    if argument_hint is not None:
        lines.append(f"argument-hint: {_yaml_string(argument_hint)}")
    lines.append(f"allowed-tools: {json.dumps(allowed_tools)}")
    if user_only:
        lines.append("disable-model-invocation: true")
    title = name.replace("-", " ").capitalize()
    return "\n".join([*lines, "---", "", f"# {title}", ""])


def shell_grant_problems(allowed_tools: list[str]) -> list[str]:
    """Why each Bash or PowerShell entry grants every command or leaves one shell prompting; empty when none."""
    found = []
    for line in skill_inventory.grant_findings(skill_inventory.allowed_entries(allowed_tools), []):
        kind, entry = line.split(" ", 1)
        if kind == "UNSCOPED_ALLOWED":
            found.append(
                f"allowed-tools entry {entry} grants {entry} for every command; scope it to what the skill runs"
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


def metadata(tools: list[str], opt_in: bool) -> str:
    document: dict[str, object] = {"required_vars": [], "shared_deps": ["runtime-compatibility.md"]}
    if tools:
        document["tools"] = tools
    if opt_in:
        document["opt_in"] = True
    return metadata_text(document)


def _one_line(label: str, value: str) -> str:
    value = value.strip()
    if not value or "\n" in value or "\r" in value:
        raise ScaffoldError(f"{label} must be one non-empty line")
    return value


def scaffold(
    root: Path,
    name: str,
    description: str,
    argument_hint: str | None = None,
    user_only: bool = False,
    opt_in: bool = False,
    tools: list[str] | None = None,
    allowed_tools: list[str] | None = None,
) -> list[str]:
    """Write the new skill's files and return the reference problems a person still has to resolve."""
    if not deploy_source.is_valid_name(name):
        raise ScaffoldError(f"'{name}' is not a valid skill name; see docs/adding-a-skill.md")
    word = deploy_source.reserved_word(name)
    if word:
        raise ScaffoldError(f"'{name}' contains the reserved word '{word}', {deploy_source.AGENT_SKILLS_RULE}")
    description = _one_line("--description", description)
    tag = deploy_source.xml_tag(description)
    if tag:
        raise ScaffoldError(f"--description contains the XML tag '{tag}', {deploy_source.AGENT_SKILLS_RULE}")
    if not user_only and not skill_reference.says_when(description):
        raise ScaffoldError(f"--description must {skill_reference.WHEN_RULE}")
    if argument_hint is not None:
        argument_hint = _one_line("--argument-hint", argument_hint)
    tools = sorted(set(tools or []))
    unknown = [tool for tool in tools if tool not in SKILL_TOOLS]
    if unknown:
        raise ScaffoldError(f"unknown tool {unknown[0]}; add it to SKILL_TOOLS in deployer/tools.py first")
    allowed = [tool.strip() for tool in (allowed_tools if allowed_tools is not None else DEFAULT_ALLOWED_TOOLS)]
    if not allowed or not all(allowed):
        raise ScaffoldError("--allowed-tools must name at least one tool")
    grant_problems = shell_grant_problems(allowed)
    if grant_problems:
        raise ScaffoldError(grant_problems[0])
    existing = skill_reference.load_source(root)
    skill_md = root / "skills" / name / "SKILL.md"
    meta = root / "deploy-meta" / f"{name}.json"
    if name in existing.skills or name in existing.bundles or skill_md.parent.exists() or meta.exists():
        raise ScaffoldError(f"'{name}' is already a skill, a bundle, or an existing path")
    if not (root / skill_reference.REFERENCE).is_file():
        raise ScaffoldError(f"{skill_reference.REFERENCE.as_posix()} does not exist")

    skill_md.parent.mkdir(parents=True)
    skill_md.write_text(
        skill_markdown(name, description, argument_hint, user_only, allowed),
        encoding="utf-8",
        newline="\n",
    )
    meta.write_text(metadata(tools, opt_in), encoding="utf-8", newline="\n")
    skill_reference.write(root)
    return skill_reference.problems(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("name")
    parser.add_argument("--description", required=True, help="what the skill does and when to use it, on one line")
    parser.add_argument("--argument-hint", help="the arguments, in the notation docs/skills.md explains")
    parser.add_argument(
        "--user-only", action="store_true", help="only the user may start it (disable-model-invocation)"
    )
    parser.add_argument("--opt-in", action="store_true", help="leave it out of deploy.py --all unless chosen")
    parser.add_argument("--tool", action="append", default=[], help="an external tool it runs, from deployer/tools.py")
    parser.add_argument(
        "--allowed-tools",
        default=",".join(DEFAULT_ALLOWED_TOOLS),
        help="comma-separated allowed-tools entries (default: Bash and PowerShell for the skill's own scripts, and Read)",
    )
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        remaining = scaffold(
            arguments.root,
            arguments.name,
            arguments.description,
            arguments.argument_hint,
            arguments.user_only,
            arguments.opt_in,
            arguments.tool,
            arguments.allowed_tools.split(","),
        )
    except (OSError, ScaffoldError, skill_reference.ReferenceError) as exc:
        print(f"FAILED {exc}", file=sys.stderr)
        return 2
    print(f"CREATED skills/{arguments.name}/SKILL.md")
    print(f"CREATED deploy-meta/{arguments.name}.json")
    print(f"UPDATED {skill_reference.REFERENCE.as_posix()}")
    for problem in remaining:
        print(f"REMAINING {problem}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Keep docs/skills.md, the skill reference, in step with each skill's frontmatter and deploy metadata.

Usage:
  python tools/skill_reference.py           report every stale or missing part; exit 1 if there is one
  python tools/skill_reference.py --write   regenerate the generated blocks and add a section for each new skill

docs/skills.md holds a generated summary table and one section per selectable skill, headed `` ## `<name>` ``.
Each section starts with a generated block, built from the skill's SKILL.md frontmatter and deploy-meta, and
continues with hand-written prose that explains its arguments. --write rewrites only the generated blocks and
adds missing sections; it never changes the prose or removes a section. Skill sections end the file.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deployer import frontmatter
from deployer import source as deploy_source
from deployer.config import CONFIGURED_VARIABLES
from deployer.errors import DeployError
from deployer.paths import Paths
from deployer.tools import SKILL_TOOLS

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

REFERENCE = Path("docs") / "skills.md"
README = Path("README.md")
SUMMARY = "summary"
SECTION_HEADING = re.compile(r"^## `([^`]+)`$")
TOKEN = re.compile(r"\{\{([A-Z_]+)\}\}")
# Frontmatter keys this tool reads; the shared reader never parses the others.
READ_KEYS = ("name", "description", "argument-hint", "disable-model-invocation")
# The clause that tells the model when to start a skill, as "Description" in docs/adding-a-skill.md asks.
WHEN_CLAUSE = re.compile(r"(?i)\buse (?:it |this skill )?(?:when|whenever|before|after)\b")
WHEN_RULE = 'say what it does, then when to use it ("Use it when ..."); see "Description" in docs/adding-a-skill.md'


def says_when(description: str) -> bool:
    return WHEN_CLAUSE.search(description) is not None


class ReferenceError(Exception):
    """The reference or a skill's frontmatter cannot be read in the shape this tool maintains."""


@dataclass(frozen=True)
class Entry:
    name: str
    description: str
    argument_hint: str | None
    user_only: bool
    bundle: str | None
    opt_in: bool
    needs: list[str]


def begin_marker(block: str) -> str:
    return f"<!-- generated:{block} -->"


def end_marker(block: str) -> str:
    return f"<!-- /generated:{block} -->"


def read_frontmatter(skill_md: Path) -> dict[str, str]:
    """The values of READ_KEYS that the skill declares, each of which must be a single value."""
    try:
        document = frontmatter.read(skill_md)
        values = {key: document.string(key) for key in READ_KEYS}
    except frontmatter.FrontmatterError as exc:
        raise ReferenceError(f"{skill_md}: {exc}") from exc
    return {key: value for key, value in values.items() if value is not None}


def _needs(source: deploy_source.Source, name: str) -> list[str]:
    # Only the skill's own tools: validation holds that list to what the skill runs, including through the
    # dependency scripts it reaches, while a dependency's other tools are not the skill's to need.
    skills = deploy_source.expand(source, [], [name])
    own = source.skills[name]
    optional = set(own.optional_tools) | {tool for tool in own.tools if SKILL_TOOLS[tool].optional}
    needs = [f"`{tool}` (optional)" if tool in optional else f"`{tool}`" for tool in sorted({*own.tools, *optional})]
    settings = sorted(
        {var for skill in skills for var in source.skills[skill].required_vars if var in CONFIGURED_VARIABLES}
    )
    needs.extend(f"the `{var}` setting" for var in settings)
    return needs


def load_source(root: Path) -> deploy_source.Source:
    paths = Paths(root, root)
    try:
        return deploy_source.discover(paths, deploy_source.load_source_id(paths))
    except DeployError as exc:
        raise ReferenceError(str(exc)) from exc


def entries(source: deploy_source.Source) -> list[Entry]:
    """One entry per selectable skill, in name order."""
    result = []
    for name in sorted(source.skills):
        skill = source.skills[name]
        if not skill.selectable:
            continue
        frontmatter = read_frontmatter(skill.directory / "SKILL.md")
        description = frontmatter.get("description", "").strip()
        if not description:
            raise ReferenceError(f"skills/{name}/SKILL.md: no description")
        hint = frontmatter.get("argument-hint", "").strip() or None
        bundle = source.skill_bundle.get(name)
        result.append(
            Entry(
                name=name,
                description=TOKEN.sub(r"`\1`", description),
                argument_hint=hint,
                user_only=frontmatter.get("disable-model-invocation", "").strip() == "true",
                bundle=bundle,
                opt_in=deploy_source.is_opt_in(source, bundle or name),
                needs=_needs(source, name),
            )
        )
    return result


def _started_by(entry: Entry) -> str:
    return "you" if entry.user_only else "you or the agent"


def _installed_cell(entry: Entry) -> str:
    if entry.bundle is None:
        return "Opt-in" if entry.opt_in else "By default"
    return f"{'Opt-in, ' if entry.opt_in else ''}`{entry.bundle}` bundle"


def _installed_sentence(entry: Entry) -> str:
    root = entry.bundle or entry.name
    if entry.opt_in:
        within = f" with the `{entry.bundle}` bundle" if entry.bundle else ""
        return f"Opt-in{within}: deploy it with `--include {root}`."
    if entry.bundle:
        return f"Installed with the `{entry.bundle}` bundle."
    return "Installed by default."


def _join(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def summary_block(skills: list[Entry]) -> str:
    rows = [
        "| Skill | Started by | Installed | Needs |",
        "|---|---|---|---|",
        *(
            f"| [`{entry.name}`](#{entry.name}) | {_started_by(entry).capitalize()} | "
            f"{_installed_cell(entry)} | {', '.join(entry.needs) or 'Nothing extra'} |"
            for entry in skills
        ),
    ]
    return "\n".join(rows)


def section_block(entry: Entry) -> str:
    invocation = f"/{entry.name} {entry.argument_hint}" if entry.argument_hint else f"/{entry.name}"
    facts = [f"Started by {_started_by(entry)}.", _installed_sentence(entry)]
    if entry.needs:
        facts.append(f"Needs {_join(entry.needs)}.")
    if entry.argument_hint is None:
        facts.append("Takes no arguments.")
    return "\n".join([entry.description, "", "```text", invocation, "```", "", " ".join(facts)])


@dataclass
class Section:
    name: str
    body: str  # everything after the heading line, up to the next skill heading


def _wrap(block: str, content: str) -> str:
    return f"{begin_marker(block)}\n{content}\n{end_marker(block)}"


def _replace_block(text: str, block: str, content: str) -> str | None:
    """`text` with the named block's content replaced, or None when the block is missing."""
    begin, end = begin_marker(block), end_marker(block)
    start = text.find(begin)
    stop = text.find(end, start + len(begin)) if start >= 0 else -1
    if start < 0 or stop < 0:
        return None
    return text[:start] + _wrap(block, content) + text[stop + len(end) :]


def _split(document: str) -> tuple[str, list[Section]]:
    lines = document.split("\n")
    headings = [(index, match.group(1)) for index, line in enumerate(lines) if (match := SECTION_HEADING.match(line))]
    preamble = "\n".join(lines[: headings[0][0]] if headings else lines)
    sections = []
    for position, (start, name) in enumerate(headings):
        stop = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        sections.append(Section(name, "\n".join(lines[start + 1 : stop])))
    return preamble, sections


def _prose(section: Section) -> str:
    end = end_marker(section.name)
    index = section.body.find(end)
    return section.body[index + len(end) :] if index >= 0 else section.body


def regenerate(document: str, skills: list[Entry]) -> str:
    """The document with every generated block current and a section for every skill."""
    preamble, sections = _split(document)
    summary = _replace_block(preamble, SUMMARY, summary_block(skills))
    if summary is None:
        raise ReferenceError(
            f"{REFERENCE.as_posix()}: no summary block; add {begin_marker(SUMMARY)} and {end_marker(SUMMARY)}"
        )
    known = {section.name for section in sections}
    for entry in skills:
        if entry.name not in known:
            sections.append(Section(entry.name, ""))
    by_name = {entry.name: entry for entry in skills}
    # Sections for unknown skills keep their text and sort last, where problems() reports them.
    ordered = sorted(sections, key=lambda section: (section.name not in by_name, section.name))
    parts = [summary.rstrip("\n")]
    for section in ordered:
        known_entry = by_name.get(section.name)
        body = section.body
        if known_entry is not None:
            replaced = _replace_block(body, known_entry.name, section_block(known_entry))
            body = (
                replaced
                if replaced is not None
                else "\n" + _wrap(known_entry.name, section_block(known_entry)) + "\n" + body.lstrip("\n")
            )
        parts.append(f"\n\n## `{section.name}`\n" + body.rstrip("\n"))
    return "".join(parts) + "\n"


def readme_problems(readme: str, roots: list[str], skills: list[str]) -> list[str]:
    """What keeps the README's 'Included skills' table from having one row per menu item, linked to live sections."""
    match = re.search(r"^## Included skills\n(.*?)(?=^## |\Z)", readme, re.MULTILINE | re.DOTALL)
    if match is None:
        return [f"{README.as_posix()}: no 'Included skills' section"]
    section = match.group(1)
    # A row is named by the first code span in its first cell; the header and separator rows have none.
    rows = [
        found.group(1)
        for line in section.splitlines()
        if line.startswith("|")
        if (found := re.search(r"`([^`]+)`", line.split("|")[1]))
    ]
    problems = [f"{README.as_posix()}: 'Included skills' has no row for `{root}`" for root in roots if root not in rows]
    problems += [
        f"{README.as_posix()}: 'Included skills' row `{row}` names no selectable skill or bundle; remove it"
        for row in rows
        if row not in roots
    ]
    for anchor in re.findall(rf"\({re.escape(REFERENCE.as_posix())}#([^)]+)\)", section):
        if anchor not in skills:
            problems.append(
                f"{README.as_posix()}: 'Included skills' links {REFERENCE.as_posix()}#{anchor}, which has no section"
            )
    if f"({REFERENCE.as_posix()})" not in section:
        problems.append(f"{README.as_posix()}: 'Included skills' does not link {REFERENCE.as_posix()}")
    return problems


def problems(root: Path) -> list[str]:
    """Everything that keeps the reference from matching the skills; empty when it is current."""
    source = load_source(root)
    skills = entries(source)
    reference = root / REFERENCE
    if not reference.is_file():
        return [f"{REFERENCE.as_posix()} does not exist"]
    document = reference.read_text(encoding="utf-8")
    found: list[str] = []
    for entry in skills:
        if entry.argument_hint is None and "$ARGUMENTS" in (root / "skills" / entry.name / "SKILL.md").read_text(
            encoding="utf-8"
        ):
            found.append(f"skills/{entry.name}/SKILL.md reads $ARGUMENTS but declares no argument-hint")
        if not entry.user_only and not says_when(entry.description):
            found.append(f"skills/{entry.name}/SKILL.md: the model may start it, so its description must {WHEN_RULE}")
    try:
        current = regenerate(document, skills)
    except ReferenceError as exc:
        return [*found, str(exc)]
    names = {entry.name for entry in skills}
    _, sections = _split(document)
    for section in sections:
        if section.name not in names:
            found.append(f"{REFERENCE.as_posix()}: section `{section.name}` names no selectable skill; remove it")
    present = {section.name for section in sections}
    for entry in skills:
        if entry.name not in present:
            found.append(f"{REFERENCE.as_posix()}: no section for `{entry.name}`")
    for section in sections:
        if section.name in names and not _prose(section).strip():
            found.append(
                f"{REFERENCE.as_posix()}: section `{section.name}` has "
                "no hand-written explanation after its generated block"
            )
    if current != document:
        found.append(f"{REFERENCE.as_posix()}: generated content is stale; run python tools/skill_reference.py --write")
    roots = sorted([*deploy_source.root_names(source), *source.bundles])
    readme = root / README
    found.extend(readme_problems(readme.read_text(encoding="utf-8") if readme.is_file() else "", roots, sorted(names)))
    return found


def write(root: Path) -> None:
    reference = root / REFERENCE
    document = reference.read_text(encoding="utf-8")
    updated = regenerate(document, entries(load_source(root)))
    if updated != document:
        reference.write_text(updated, encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--write", action="store_true", help="regenerate the generated blocks and add missing sections")
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        if arguments.write:
            write(arguments.root)
        found = problems(arguments.root)
    except (OSError, ReferenceError) as exc:
        print(f"FAILED {exc}", file=sys.stderr)
        return 2
    for problem in found:
        print(problem)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())

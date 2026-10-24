"""The deterministic steps of change-skill: print one section of a contract document, and name the regenerations and
manual gates a change to a skill owes, so the agent reads only what a step cites and decides only what is judgment.

    section "<title>" [--document <path>]   print the section of a Markdown document in this checkout whose heading
                                            reads <title>, docs/adding-a-skill.md unless --document names another
    gates [--base <ref>]                    name what the files changed since the merge base with <ref> (origin/main
                                            unless given), committed, uncommitted, or untracked, owe before the pull
                                            request

A section runs from its heading to the next heading of any level, so "Files" stops at its first subsection as
"Granting tools" stops at the next. A heading-shaped line inside a fenced block is not a heading. A title matches a
heading's text as written, or with its backticks removed, as in `audit-skill`.

A skill is touched by a file in its folder or its metadata, and a shipped skill also by a change to an agent it
declares. The reference is regenerated after a shipped skill's frontmatter or metadata changes, the shims after a
repository skill's frontmatter changes, and the runtime prompt samples after a file they are rendered from changes.
The cost audit is owed for each touched skill still present; the runtime canary for each present shipped skill that
is new, changes a frontmatter key its runtime adapter or grants read, changes the `${CLAUDE_SKILL_DIR}` paths it names
its own files by, changes the metadata its agents, guidance, or declared runtime support come from or whether it
is selectable, which decides whether it has a runtime adapter, or declares a changed agent; the evaluation for each
skill with a review scenario, any scenario but a trigger one, whose skill_deps reach a touched skill; and the trigger
evaluation for each present shipped skill the model may start whose description is new or changed, since the
description is all the model reads when deciding whether to start it.

Every line is one fact:

    SECTION "<document>" <first>-<last> <heading>   section: where the section is, then its lines as the document holds
                                                    them, the heading's own line left out
    HEADING <line> <heading>                        section: each heading of the document, printed before the FAILED
                                                    line that names a title it holds no section of, or more than one
    BASE <ref> <commit>                             gates: the merge base the change is read from
    CHANGED <count>                                 gates: the files changed since it
    REGENERATE <command>                            gates: a generated file the change may have left stale; run it
    GATE audit-skill <skill>                        gates: a skill the change touches, shipped or repository
    GATE runtime-canary <skill> ...                 gates: the shipped skills whose discovery the change touches
    GATE evaluate-skill <skill> if-prompt           gates: a skill with a review scenario the change reaches, owed only
                                                    when it touches a prompt, model guidance, or reviewer instructions
    GATE evaluate-skill <skill> trigger             gates: a shipped skill the model may start whose description is new
                                                    or changed, owed always: its trigger scenario runs before the pull
                                                    request
    OWED <count>                                    gates: the REGENERATE and GATE lines above

Exit status 0 means the section or the gates were printed, "nothing owed" included; 1 a last line FAILED <reason>; 2 a
usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))

import frontmatter
import json_reader
from console import run_main
from flat_text import flat_text
from git_client import GitClient, GitError
from markdown_sections import find_fences

# The paths and commands validation and the tools also name: this script runs from the checkout without the
# deployer, which tests/validation and tools/skill_shims.py import.
DUPLICATION_ALLOWED = {
    "CONTRACT": (
        "tests/validation/validation_support.py cites the skill contract in its problems, and this script "
        "cannot import it, which needs the deployer"
    ),
    "REPOSITORY": (
        "tests/validation/validation_support.py names the repository skills' folder, and this script "
        "cannot import it, which needs the deployer"
    ),
    "SKILL_SHIMS": (
        "tools/skill_shims.py names the command that writes the shims, and this script "
        "cannot import it, which needs the deployer"
    ),
}

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
CONTRACT = "docs/adding-a-skill.md"
ATX_HEADING = re.compile(r" {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*")
# Where a shipped skill names its own files, as Claude Code fills it in; a change to what it names moves a script.
SKILL_DIR_PATH = re.compile(r"\$\{CLAUDE_SKILL_DIR\}/[^\s\"'`)<>]*")

SHIPPED = "skills"
REPOSITORY = ".claude/skills"
METADATA = "deploy-meta"
AGENTS = "agents"
SCENARIOS = "tests/fixtures/skill-evals"
# The mode of a scenario that judges whether a skill's description starts it rather than what the skill does.
TRIGGER_MODE = "trigger"
# The frontmatter keys the runtime adapter carries or a runtime grants by, and the metadata the deployer installs a
# skill's agents, guidance, and declared support from, and whether it is selectable, which decides whether the
# deployer writes its adapter: a change to any of them changes what a runtime finds.
CANARY_KEYS = ("name", "description", "disable-model-invocation", "allowed-tools")
CANARY_METADATA = ("agent_deps", "shared_deps", "runtime_support", "selectable")
# What tools/runtime_prompts.py renders its samples from: the skills whose scripts write the prompts, the fixtures it
# renders them over, and the tools themselves, the skill-evals command line among them with the modules it reads a
# run's configuration from. The suite holds this list to that module's imports.
RUNTIME_PROMPT_SOURCES = (
    "skills/ejryan89-code-review-core/",
    "skills/review-document/",
    "skills/review-insights/",
    "tests/fixtures/skill-evals/review-prs/",
    "tools/runtime_prompts.py",
    "tools/skill_evals.py",
    "tools/skill_evals_records.py",
    "tools/skill_evals_runs.py",
    "tools/skill_evals_scenarios.py",
)
SKILL_REFERENCE = "python tools/skill_reference.py --write"
SKILL_SHIMS = "python tools/skill_shims.py --write"
RUNTIME_PROMPTS = "python tools/runtime_prompts.py --write"
UNREADABLE = object()  # a frontmatter or metadata value that cannot be read, which equals no other value


class StepError(Exception):
    """A step the script refuses, with the reason its FAILED line gives."""


@dataclass(frozen=True)
class Heading:
    index: int  # the heading's line, counted from 0
    level: int
    text: str

    def matches(self, title: str) -> bool:
        return title in (self.text, self.text.replace("`", ""))

    def shown(self) -> str:
        return f"{'#' * self.level} {self.text}"


def headings(lines: list[str]) -> list[Heading]:
    """Every ATX heading outside a fenced block, in document order."""
    fenced = {index for fence in find_fences(lines) for index in fence.lines()}
    found = []
    for index, line in enumerate(lines):
        match = ATX_HEADING.fullmatch(line)
        if match and index not in fenced:
            found.append(Heading(index, len(match.group(1)), match.group(2)))
    return found


def section(lines: list[str], title: str) -> tuple[Heading, list[str]]:
    """The one heading that reads `title` and the lines under it up to the next heading, trailing blank lines left
    out; StepError when no heading or more than one reads it."""
    found = headings(lines)
    matching = [heading for heading in found if heading.matches(title)]
    if len(matching) != 1:
        where = "no heading" if not matching else f"{len(matching)} headings"
        raise StepError(f'{where} of the document reads "{flat_text(title)}"')
    heading = matching[0]
    end = next((later.index for later in found if later.index > heading.index), len(lines))
    body = lines[heading.index + 1 : end]
    while body and not body[-1].strip():
        body.pop()
    return heading, body


def document_path(root: Path, relative: str) -> Path:
    """The Markdown file in the checkout `relative` names; StepError for any other path, so the script reads nothing
    outside the checkout it ships in."""
    top = root.resolve()
    path = (top / relative).resolve()
    if path.suffix.lower() != ".md" or not path.is_relative_to(top) or not path.is_file():
        raise StepError(f"{flat_text(relative)} is not a Markdown file in this checkout")
    return path


def print_section(root: Path, document: str, title: str) -> int:
    path = document_path(root, document)
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    try:
        heading, body = section(lines, title)
    except StepError:
        for each in headings(lines):
            print(f"HEADING {each.index + 1} {each.shown()}")
        raise
    shown = path.relative_to(root.resolve()).as_posix()
    print(f'SECTION "{shown}" {heading.index + 1}-{heading.index + 1 + len(body)} {heading.shown()}')
    for line in body:
        print(line)
    return 0


@dataclass
class Tree:
    """The text of a file at the merge base and in the working tree, each base read once, None where it is absent."""

    root: Path
    commit: str
    client: GitClient
    _base: dict[str, str | None] = field(default_factory=dict, init=False)

    def base(self, path: str) -> str | None:
        if path not in self._base:
            listed = self.client.output(["ls-tree", "-z", self.commit, "--", path], directory=self.root)
            blob = f"{self.commit}:{path}"
            present = bool(listed.strip("\0"))
            self._base[path] = self.client.output(["cat-file", "blob", blob], directory=self.root) if present else None
        return self._base[path]

    def head(self, path: str) -> str | None:
        file = self.root / path
        return file.read_text(encoding="utf-8-sig") if file.is_file() else None


def frontmatter_block(text: str | None) -> list[str] | None:
    """A document's frontmatter lines, empty when it has none, all its lines when they cannot be read as one, and None
    when the document is absent."""
    if text is None:
        return None
    try:
        found = frontmatter.split(text.splitlines())
    except frontmatter.FrontmatterError:
        return text.splitlines()
    return found[0] if found else []


def frontmatter_value(text: str | None, key: str) -> object:
    """A frontmatter key's value, None when the document or the key is absent, UNREADABLE when it cannot be read."""
    if text is None:
        return None
    try:
        found = frontmatter.split(text.splitlines())
        return frontmatter.Frontmatter(found[0]).value(key) if found else None
    except frontmatter.FrontmatterError:
        return UNREADABLE


def metadata(text: str | None) -> dict[str, object] | None:
    """A metadata file's object, empty when the file is absent, None when it is not a JSON object."""
    if text is None:
        return {}
    try:
        value = json_reader.loads(text)
    except (json.JSONDecodeError, json_reader.JsonRefused):
        return None
    return value if isinstance(value, dict) else None


def metadata_value(text: str | None, key: str) -> object:
    found = metadata(text)
    return UNREADABLE if found is None else found.get(key)


def skill_of(path: str) -> tuple[str, str] | None:
    """The kind of skill folder a changed path sits in and the skill's name, a shipped skill's metadata included."""
    parts = path.split("/")
    if parts[0] == SHIPPED and len(parts) > 2:
        return SHIPPED, parts[1]
    if "/".join(parts[:2]) == REPOSITORY and len(parts) > 3:
        return REPOSITORY, parts[2]
    if parts[0] == METADATA and len(parts) == 2 and parts[1].endswith(".json"):
        return SHIPPED, parts[1].removesuffix(".json")
    return None


def agent_of(path: str) -> str | None:
    """The agent a changed path defines, when it is an agent definition."""
    parts = path.split("/")
    if len(parts) == 2 and parts[0] == AGENTS and parts[1].endswith(".md"):
        return parts[1].removesuffix(".md")
    return None


def head_metadata(root: Path) -> dict[str, dict[str, object]]:
    """Every shipped skill's metadata in the working tree, by name; metadata that cannot be read reads as empty."""
    files = (root / METADATA).glob("*.json")
    return {path.stem: metadata(path.read_text(encoding="utf-8-sig")) or {} for path in files}


def _names(value: object) -> set[str]:
    return {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()


def reach(name: str, meta: dict[str, dict[str, object]]) -> set[str]:
    """The skill and every skill its skill_deps reach, by the working tree's metadata."""
    reached, pending = {name}, [name]
    while pending:
        for dependency in _names(meta.get(pending.pop(), {}).get("skill_deps")) - reached:
            reached.add(dependency)
            pending.append(dependency)
    return reached


@dataclass(frozen=True)
class Owed:
    regenerate: list[str]
    cost: list[str]
    canary: list[str]
    evaluate: list[str]
    trigger: list[str]

    def lines(self) -> list[str]:
        found = [f"REGENERATE {command}" for command in self.regenerate]
        found += [f"GATE audit-skill {flat_text(name)}" for name in self.cost]
        if self.canary:
            found.append("GATE runtime-canary " + " ".join(flat_text(name) for name in self.canary))
        found += [f"GATE evaluate-skill {flat_text(name)} if-prompt" for name in self.evaluate]
        found += [f"GATE evaluate-skill {flat_text(name)} trigger" for name in self.trigger]
        return found


def _frontmatter_changed(tree: Tree, kind: str, names: Iterable[str], paths: set[str]) -> bool:
    files = [f"{kind}/{name}/SKILL.md" for name in names]
    return any(
        frontmatter_block(tree.base(file)) != frontmatter_block(tree.head(file)) for file in files if file in paths
    )


def regenerations(tree: Tree, touched: dict[str, str], paths: set[str]) -> list[str]:
    """The generators whose sources changed: a shipped skill's frontmatter or metadata feeds the reference, a
    repository skill's frontmatter its shim, and the runtime prompt sources the samples."""
    shipped = [name for name, kind in touched.items() if kind == SHIPPED]
    repository = [name for name, kind in touched.items() if kind == REPOSITORY]
    found = []
    metadata_changed = any(f"{METADATA}/{name}.json" in paths for name in shipped)
    if metadata_changed or _frontmatter_changed(tree, SHIPPED, shipped, paths):
        found.append(SKILL_REFERENCE)
    if _frontmatter_changed(tree, REPOSITORY, repository, paths):
        found.append(SKILL_SHIMS)
    if any(path.startswith(RUNTIME_PROMPT_SOURCES) for path in paths):
        found.append(RUNTIME_PROMPTS)
    return found


def needs_canary(tree: Tree, name: str, paths: set[str]) -> bool:
    """Whether a change to the shipped skill can change what a runtime finds or runs: a new skill, a key its adapter
    or grants read, a path it names its own files by, or metadata its agents, guidance, or declared support come
    from or that decides whether it has an adapter."""
    skill_file, meta_file = f"{SHIPPED}/{name}/SKILL.md", f"{METADATA}/{name}.json"
    if skill_file in paths:
        base, head = tree.base(skill_file), tree.head(skill_file) or ""
        if base is None or any(frontmatter_value(base, key) != frontmatter_value(head, key) for key in CANARY_KEYS):
            return True
        if set(SKILL_DIR_PATH.findall(base)) != set(SKILL_DIR_PATH.findall(head)):
            return True
    if meta_file in paths:
        base_meta, head_meta = tree.base(meta_file), tree.head(meta_file)
        return any(metadata_value(base_meta, key) != metadata_value(head_meta, key) for key in CANARY_METADATA)
    return False


def reviews(root: Path, name: str) -> bool:
    """Whether the skill has a review scenario: one whose scenario.json is not a trigger scenario's, an unreadable one
    counted, so the gate is owed rather than lost."""
    for path in (root / SCENARIOS / name).glob("*/scenario.json"):
        found = metadata(path.read_text(encoding="utf-8-sig"))
        if found is None or found.get("mode") != TRIGGER_MODE:
            return True
    return False


def needs_trigger(tree: Tree, name: str, paths: set[str]) -> bool:
    """Whether the shipped skill's description is new or changed while the model may start it, which only a trigger
    run judges; frontmatter that cannot be read counts as both."""
    skill_file = f"{SHIPPED}/{name}/SKILL.md"
    if skill_file not in paths:
        return False
    head = tree.head(skill_file)
    user_only = frontmatter_value(head, "disable-model-invocation")
    if isinstance(user_only, str) and user_only.casefold() == "true":
        return False
    return frontmatter_value(tree.base(skill_file), "description") != frontmatter_value(head, "description")


def owed(tree: Tree, changed: Iterable[str]) -> Owed:
    """What the changed paths owe, by the rules the module's docstring states."""
    paths = set(changed)
    meta = head_metadata(tree.root)
    touched: dict[str, str] = {}
    for path in sorted(paths):
        found = skill_of(path)
        if found is not None:
            touched.setdefault(found[1], found[0])
    agents = {agent for agent in map(agent_of, paths) if agent is not None}
    declaring = {name for name, value in meta.items() if _names(value.get("agent_deps")) & agents}
    for name in declaring:
        touched.setdefault(name, SHIPPED)
    present = sorted(name for name, kind in touched.items() if tree.head(f"{kind}/{name}/SKILL.md") is not None)
    canary = [
        name for name in present if touched[name] == SHIPPED and (name in declaring or needs_canary(tree, name, paths))
    ]
    scenario_skills = sorted(path.name for path in (tree.root / SCENARIOS).glob("*") if path.is_dir())
    evaluate = [name for name in scenario_skills if reach(name, meta) & set(touched) and reviews(tree.root, name)]
    trigger = [name for name in present if touched[name] == SHIPPED and needs_trigger(tree, name, paths)]
    return Owed(regenerations(tree, touched, paths), present, canary, evaluate, trigger)


def print_gates(root: Path, base: str, client: GitClient) -> int:
    commit = client.output(["merge-base", base, "HEAD"], directory=root).strip()
    listings = (
        client.output(["diff", "--name-only", "--no-renames", "-z", commit, "--"], directory=root),
        client.output(["ls-files", "--others", "--exclude-standard", "-z"], directory=root),
    )
    changed = sorted({path for listing in listings for path in listing.split("\0") if path})
    print(f"BASE {flat_text(base)} {commit}")
    print(f"CHANGED {len(changed)}")
    lines = owed(Tree(root, commit, client), changed).lines()
    for line in lines:
        print(line)
    print(f"OWED {len(lines)}")
    return 0


def _ref(value: str) -> str:
    if not value or value.startswith("-"):
        raise argparse.ArgumentTypeError(f"{value!r} is not a ref")
    return value


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="The deterministic steps of change-skill.")
    commands = parser.add_subparsers(dest="command", required=True)
    reading = commands.add_parser("section", help="print one section of a Markdown document in this checkout")
    reading.add_argument("title")
    reading.add_argument("--document", default=CONTRACT)
    gating = commands.add_parser("gates", help="name the regenerations and manual gates the change owes")
    gating.add_argument("--base", type=_ref, default="origin/main")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path = REPOSITORY_ROOT,
    client_factory: Callable[[], GitClient] = GitClient,
) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "section":
            return print_section(root, arguments.document, arguments.title)
        return print_gates(root, arguments.base, client_factory())
    except (StepError, GitError, OSError, UnicodeDecodeError) as error:
        print(f"FAILED {flat_text(str(error))}")
        return 1


if __name__ == "__main__":
    run_main(main)

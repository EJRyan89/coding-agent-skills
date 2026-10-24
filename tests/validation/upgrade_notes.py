"""The upgrade-notes guard: each contract item changed since the last tag is named by an upgrade note since then."""

from __future__ import annotations

import ast
import functools
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# release_levels is imported before validation_support, which puts ejryan89-skill-core on sys.path too, so this module
# states the path itself.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from release_levels import RELEASING_DOC, VERSION, VERSIONING_HEADING, level_word, versioning_levels
from validation_support import (
    OPERATIONS_CONTRACT,
    REPOSITORY_ROOT,
    PullRequestChecks,
    is_skill_file,
    listed,
    markdown_section,
    module_statements,
    parse_python,
    repository_files,
)

from tools.release_notes import (
    BODY_HEADING,
    RELEASE_BRANCH,
    UPGRADE_NOTES,
    ReleaseNotesError,
    SourcedEntry,
    body_entries,
    git,
    git_output,
    range_entries,
    release_reaching,
)

# The upgrade-notes guard: the contract files the Versioning section of docs/releasing.md judges a release by, read at
# the last tag and in the working tree. Each is compared by its contract value, so an edit that leaves the value alone
# needs no entry. The entries are the upgrade notes tools/release_notes.py reads from the commits since the tag, and
# the pull request body the runner was given with --pr-body. Every git call runs through tools/release_notes.py's
# git and release_reaching, both over its one GitClient, so the tag lookup and the commit range read the same
# repository whatever GIT_DIR the shell exported.
PROFILE_DOC = "docs/implementing-changes.md"
CONTRACT_FILES_HEADING = "## Contract files"
MANIFEST_MODULE = "deployer/manifest.py"
MANIFEST_CONTRACT_NAMES = ("MANIFEST_VERSION", "OLDEST_READABLE_VERSION")
TOOLS_MODULE = "deployer/tools.py"
# The class deployer/tools.py declares each tool with, and its fields that hold a version floor.
TOOL_CLASS = "Tool"
TOOL_FLOOR_FIELDS = frozenset({"minimum", "provider_minimum"})
PDF_POLICY_MODULE = "skills/ejryan89-skill-core/scripts/pdf_tool.py"
DEPLOY_META = re.compile(r"deploy-meta/[^/]+\.json")
REVIEW_SCHEMA = re.compile(r"skills/ejryan89-code-review-core/references/.+\.schema\.json")
FORMATS_DOC = OPERATIONS_CONTRACT
FORMATS_HEADING = "## Formats"
NOTES_FIELDS = ("Level", "Contract", "User action")
# The only sections the notes file has below its title are versions', which tools/release_notes.py writes.
NOTES_SECTION = re.compile(r"^## (.*)$", re.MULTILINE)
PULL_REQUESTS = re.compile(r"#\d+(?:,? (?:and )?#\d+)*")


@dataclass(frozen=True)
class Snapshot:
    """A tree's file names and a reader for their text: the working tree, or a tag read through git show."""

    names: frozenset[str]
    read: Callable[[str], str]

    def text(self, name: str) -> str | None:
        return self.read(name).replace("\r\n", "\n") if name in self.names else None


def last_release_tag(root: Path) -> tuple[str | None, str | None]:
    """The last release on origin/main, as ejryan89-skill-core's release_tags reads it, and a problem; no release tag
    reachable means nothing has been released yet."""
    try:
        if git(root, "rev-parse", "--verify", "--quiet", f"{RELEASE_BRANCH}^{{commit}}").returncode != 0:
            return None, (
                f"{RELEASE_BRANCH} is missing, so the upgrade-notes check cannot find the last tag; "
                "fetch it with `git fetch origin main --tags`"
            )
        if git(root, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
            return None, (
                "this clone is shallow, so the upgrade-notes check cannot see the last tag; "
                "fetch the history with `git fetch --unshallow --tags`"
            )
        return release_reaching(root, RELEASE_BRANCH), None
    except ReleaseNotesError as error:
        return None, f"the upgrade-notes check cannot find the last tag: {error}"


def working_snapshot(root: Path) -> Snapshot:
    names = frozenset(path.relative_to(root).as_posix() for path in repository_files(root))
    return Snapshot(names, lambda name: (root / name).read_text(encoding="utf-8"))


def tag_snapshot(root: Path, tag: str) -> Snapshot:
    """The tag's tree; listing it, and reading a file from it, raise ReleaseNotesError when git cannot."""
    listed = git_output(root, "ls-tree", "-r", "--name-only", "-z", tag)
    return Snapshot(
        frozenset(name for name in listed.split("\0") if name), lambda name: git_output(root, "show", f"{tag}:{name}")
    )


def _module_values(tree: ast.Module) -> dict[str, ast.expr]:
    """The value each name a module-level statement assigns, with or without an annotation, one in a module-level if,
    try, or with block included; the last such assignment of a name holds."""
    values: dict[str, ast.expr] = {}
    for node in module_statements(tree.body):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                if isinstance(target, ast.Name):
                    values[target.id] = node.value
    return values


def _value_source(node: ast.expr, values: dict[str, ast.expr], seen: frozenset[str] = frozenset()) -> str:
    """An expression's text, a module-level name followed to the value it is assigned, so a floor held in a constant
    compares by the constant's value."""
    if isinstance(node, ast.Name) and node.id in values and node.id not in seen:
        return _value_source(values[node.id], values, seen | {node.id})
    return ast.unparse(node)


def _assigned_sources(tree: ast.Module, names: tuple[str, ...]) -> dict[str, str]:
    values = _module_values(tree)
    return {name: _value_source(values[name], values) for name in names if name in values}


def _manifest_versions(snapshot: Snapshot) -> dict[str, object]:
    source = snapshot.text(MANIFEST_MODULE)
    return {} if source is None else {MANIFEST_MODULE: _assigned_sources(ast.parse(source), MANIFEST_CONTRACT_NAMES)}


def _tool_floors(snapshot: Snapshot) -> dict[str, object]:
    source = snapshot.text(TOOLS_MODULE)
    if source is None:
        return {}
    tree = ast.parse(source)
    values = _module_values(tree)
    floors: dict[str, object] = dict(_assigned_sources(tree, ("MINIMUM_PYTHON",)))
    fields = _tool_fields(tree)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and ast.unparse(node.func).rsplit(".", 1)[-1] == TOOL_CLASS):
            continue
        given: dict[str | None, ast.expr] = {
            **dict(zip(fields, node.args, strict=False)),
            **{item.arg: item.value for item in node.keywords},
        }
        name = given.get("name")
        floors[_value_source(name, values) if name is not None else f"{TOOL_CLASS} at line {node.lineno}"] = {
            field: _value_source(value, values) for field, value in given.items() if field in TOOL_FLOOR_FIELDS
        }
    return {TOOLS_MODULE: floors}


def _tool_fields(tree: ast.Module) -> list[str]:
    """The fields of deployer/tools.py's Tool in their order, which a Tool(...) call's positional arguments fill: the
    snapshot's own class, or this checkout's when the snapshot declares none."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == TOOL_CLASS:
            return [
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            ]
    checkout = _checkout_tools()
    return [] if tree is checkout else _tool_fields(checkout)


@functools.cache
def _checkout_tools() -> ast.Module:
    return parse_python(REPOSITORY_ROOT / TOOLS_MODULE)


def _pdf_provider_floors(snapshot: Snapshot) -> dict[str, object]:
    source = snapshot.text(PDF_POLICY_MODULE)
    return {} if source is None else {PDF_POLICY_MODULE: _assigned_sources(ast.parse(source), ("MINIMUMS",))}


def _required_variables(snapshot: Snapshot) -> dict[str, object]:
    # A missing file or an empty list leaves the item out, so a skill added or removed without variables changes
    # nothing here; its directory is the contract item.
    values: dict[str, object] = {}
    for name in sorted(filter(DEPLOY_META.fullmatch, snapshot.names)):
        try:
            variables = json.loads(snapshot.text(name) or "").get("required_vars")
        except (ValueError, AttributeError):
            variables = "unreadable"
        if variables:
            values[name] = sorted(variables) if isinstance(variables, list) else variables
    return values


def _review_schemas(snapshot: Snapshot) -> dict[str, object]:
    values: dict[str, object] = {}
    for name in sorted(filter(REVIEW_SCHEMA.fullmatch, snapshot.names)):
        try:
            values[name] = json.loads(snapshot.text(name) or "")
        except ValueError:
            values[name] = snapshot.text(name)
    return values


def _format_tables(snapshot: Snapshot) -> dict[str, object]:
    section = markdown_section(snapshot.text(FORMATS_DOC) or "", FORMATS_HEADING)
    if section is None:
        return {}
    rows = [line.strip().strip("|") for line in section.split("\n") if line.lstrip().startswith("|")]
    return {FORMATS_DOC: [[cell.strip() for cell in row.split("|")] for row in rows]}


def _skill_directories(snapshot: Snapshot) -> dict[str, object]:
    """Each skill directory in the tree, skills/<name> holding SKILL.md."""
    return {path.parent.as_posix(): True for path in map(PurePosixPath, snapshot.names) if is_skill_file(path)}


# Each reader with what about its item is the contract and the file or directory it reads, which the contract lists
# in docs/implementing-changes.md and the Versioning section of docs/releasing.md must both name.
CONTRACT_READERS: tuple[tuple[str, str, Callable[[Snapshot], dict[str, object]]], ...] = (
    (" or ".join(MANIFEST_CONTRACT_NAMES), MANIFEST_MODULE, _manifest_versions),
    ("the required_vars list", "deploy-meta/", _required_variables),
    ("the schema", "skills/ejryan89-code-review-core/references/", _review_schemas),
    (f"a format table under {FORMATS_HEADING}", FORMATS_DOC, _format_tables),
    ("a skill directory name", "skills/", _skill_directories),
    ("a tool floor", TOOLS_MODULE, _tool_floors),
    ("a PDF provider floor", PDF_POLICY_MODULE, _pdf_provider_floors),
)
CONTRACT_LISTS = ((PROFILE_DOC, CONTRACT_FILES_HEADING), (RELEASING_DOC, VERSIONING_HEADING))


def changed_contract_items(released: Snapshot, current: Snapshot) -> list[tuple[str, str]]:
    """Each contract item whose value differs between the two trees, with what about it is the contract."""
    changed: list[tuple[str, str]] = []
    for description, _, read in CONTRACT_READERS:
        before, after = read(released), read(current)
        changed += [(item, description) for item in sorted(before | after) if before.get(item) != after.get(item)]
    return changed


def contract_list_problems(document: str, text: str, heading: str) -> list[str]:
    """Each file or directory the check reads that a document's contract list, under heading, does not name in
    backticks, so the list a contributor reads and the one validation holds stay the same."""
    section = markdown_section(text.replace("\r\n", "\n"), heading)
    if section is None:
        return [f"{document} has no {heading} section naming the contract files the upgrade-notes check reads"]
    return [
        f"{document} does not name `{path}` under {heading}, though the upgrade-notes check reads {description} from it"
        for description, path, _ in CONTRACT_READERS
        if f"`{path}`" not in section
    ]


def notes_entry_problems(item: SourcedEntry, levels: list[str]) -> list[str]:
    """Each field an entry leaves out or fills wrongly; one written by hand in the notes file also names its pull
    request, which the release script otherwise takes from the squash title."""
    entry = item.entry
    where = f"{item.where} entry '{entry.heading}'"
    fields = (*NOTES_FIELDS, "Pull request") if item.from_notes else NOTES_FIELDS
    problems = [
        f"{where} is missing '- {field}:' or leaves it empty" for field in fields if not entry.fields.get(field)
    ]
    # The Level is a sentence that starts with a level word. A value that starts with anything else, such as a
    # version number, punctuation, or a word run into a digit, is named by its first token, so it fails the same way.
    level = level_word(entry.fields.get("Level", ""))
    if level and level.lower() not in levels:
        problems.append(
            f"{where} starts its Level with '{level}', not one of the levels in the Versioning section of "
            f"{RELEASING_DOC}: {listed(levels)}"
        )
    contract = entry.fields.get("Contract")
    if contract and contract.lower() not in ("none", "none.") and not entry.contract_items:
        problems.append(f"{where} names no contract item in backticks, nor none, in '- Contract:'")
    pull_request = entry.fields.get("Pull request")
    if pull_request and not PULL_REQUESTS.fullmatch(pull_request.rstrip(".")):
        problems.append(f"{where} names no pull request as #N in '- Pull request:'")
    return problems


def notes_layout_problems(notes: str | None) -> list[str]:
    """A section of the notes file that is not a version's: entries now live in pull request bodies."""
    return [
        f"{UPGRADE_NOTES} has a '## {heading}' section; an entry goes under {BODY_HEADING} in the pull request "
        "body, and tools/release_notes.py writes each version's section from the merged commits when it is released"
        for heading in NOTES_SECTION.findall(notes or "")
        if not VERSION.fullmatch(heading.strip())
    ]


def upgrade_notes_problems(root: Path, body: str | None = None) -> list[str]:
    """Report each contract item changed since the last tag that no upgrade note since then names.

    The last tag is the highest release tag reachable from origin/main, as ejryan89-skill-core's release_tags defines
    one; with none reachable, nothing has been released and every change is free. The notes are the entries
    tools/release_notes.py reads from the commits since the tag and, given a pull request body, from its
    `## Upgrade note`. With a body, the commits are origin/main's,
    because the body, not the branch's own commit messages, becomes the squash commit's message; without one, they
    are HEAD's, so a local run reads the branch's commits. Each entry must give its level in the Versioning section's
    own words.
    """
    try:
        current = working_snapshot(root)
    except ReleaseNotesError as error:
        return [f"the upgrade-notes check cannot list the checkout's files: {error}"]
    problems = notes_layout_problems(current.text(UPGRADE_NOTES))
    tag, problem = last_release_tag(root)
    if problem is not None:
        return [*problems, problem]
    if tag is None:
        return problems
    levels = versioning_levels((root / RELEASING_DOC).read_text(encoding="utf-8"))
    if not levels:
        return [f"{RELEASING_DOC} has no **Levels** list under {VERSIONING_HEADING} for upgrade-notes entries to use"]
    try:
        sourced = range_entries(root, tag, RELEASE_BRANCH if body is not None else "HEAD")
        changed = changed_contract_items(tag_snapshot(root, tag), current)
    except ReleaseNotesError as error:
        return [*problems, f"the upgrade-notes check cannot read the commits and the tree since {tag}: {error}"]
    if body is not None:
        sourced += [
            SourcedEntry(entry, f"the pull request body's {BODY_HEADING}", None, from_notes=False)
            for entry in body_entries(body)
        ]
    problems += [problem for item in sourced for problem in notes_entry_problems(item, levels)]
    named = {item for sourced_entry in sourced for item in sourced_entry.entry.contract_items}
    problems += [
        f"{item}: {description} changed since {tag}, and no upgrade note since then names it; add an entry under "
        f"{BODY_HEADING} in the pull request body naming `{item}` with its level ({listed(levels)}, per the "
        f"Versioning section of {RELEASING_DOC}) and the user action or none (a local run reads the body from "
        "--pr-body <file>, and otherwise the branch's commit messages)"
        for item, description in changed
        if item not in named
    ]
    return problems


class UpgradeNotesPolicies(PullRequestChecks):
    def test_the_repository_names_every_contract_change_since_its_last_tag(self) -> None:
        self.assertEqual([], upgrade_notes_problems(REPOSITORY_ROOT, self.pull_request.body_text()))

    def test_the_contract_lists_name_every_file_the_check_reads(self) -> None:
        problems = [
            problem
            for document, heading in CONTRACT_LISTS
            for problem in contract_list_problems(
                document, (REPOSITORY_ROOT / document).read_text(encoding="utf-8"), heading
            )
        ]
        self.assertEqual([], problems)

    def test_levels_are_the_words_of_the_versioning_section(self) -> None:
        releasing = (REPOSITORY_ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
        self.assertEqual(["patch", "minor", "major"], versioning_levels(releasing))
        self.assertEqual([], versioning_levels("# Releasing\n\n## Before tagging\n"))

"""The upgrade-notes guard: each contract item changed since the last tag is named by a new upgrade-notes entry."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from validation_support import REPOSITORY_ROOT, _markdown_section, repository_files

# The upgrade-notes guard: the contract files the Versioning section of docs/releasing.md judges a release by, read at
# the last tag and in the working tree. Each is compared by its contract value, so an edit that leaves the value alone
# needs no entry.
UPGRADE_NOTES = "docs/upgrade-notes.md"
RELEASING_DOC = "docs/releasing.md"
RELEASE_BRANCH = "origin/main"
MANIFEST_MODULE = "deployer/manifest.py"
MANIFEST_CONTRACT_NAMES = ("MANIFEST_VERSION", "OLDEST_READABLE_VERSION")
TOOLS_MODULE = "deployer/tools.py"
DEPLOY_META = re.compile(r"deploy-meta/[^/]+\.json")
REVIEW_SCHEMA = re.compile(r"skills/code-review-core/references/.+\.schema\.json")
FORMATS_DOC = "docs/code-review-operations-contract.md"
FORMATS_HEADING = "## Formats"
VERSIONING_HEADING = "## Versioning"
LEVEL_ITEM = re.compile(r"- \*\*([A-Za-z]+)\.\*\*")
NOTES_FIELDS = ("Level", "Contract", "User action", "Pull request")
NOTES_FIELD = re.compile(r"^- (Level|Contract|User action|Pull request):[ \t]*(.*?)[ \t]*$", re.MULTILINE)
NOTES_ENTRY_START = re.compile(r"^(?=#{1,3} )", re.MULTILINE)
PULL_REQUESTS = re.compile(r"#\d+(?:,? (?:and )?#\d+)*")


@dataclass(frozen=True)
class Snapshot:
    """A tree's file names and a reader for their text: the working tree, or a tag read through git show."""

    names: frozenset[str]
    read: Callable[[str], str]

    def text(self, name: str) -> str | None:
        return self.read(name).replace("\r\n", "\n") if name in self.names else None


@dataclass(frozen=True)
class NotesEntry:
    heading: str
    fields: dict[str, str]
    text: str

    @property
    def contract_items(self) -> set[str]:
        return {item.rstrip("/") for item in re.findall(r"`([^`]+)`", self.fields.get("Contract", ""))}


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True, encoding="utf-8")


def last_release_tag(root: Path) -> tuple[str | None, str | None]:
    """The last tag reachable from origin/main and a problem; no tag at all means nothing has been released yet."""
    if _git(root, "rev-parse", "--verify", "--quiet", f"{RELEASE_BRANCH}^{{commit}}").returncode != 0:
        return None, (
            f"{RELEASE_BRANCH} is missing, so the upgrade-notes check cannot find the last tag; "
            "fetch it with `git fetch origin main --tags`"
        )
    if _git(root, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        return None, (
            "this clone is shallow, so the upgrade-notes check cannot see the last tag; "
            "fetch the history with `git fetch --unshallow --tags`"
        )
    described = _git(root, "describe", "--tags", "--abbrev=0", RELEASE_BRANCH)
    if described.returncode == 0:
        return described.stdout.strip(), None
    if _git(root, "tag", "--merged", RELEASE_BRANCH).stdout.strip():
        return None, f"git describe could not name the last tag on {RELEASE_BRANCH}: {described.stderr.strip()}"
    return None, None


def working_snapshot(root: Path) -> Snapshot:
    names = frozenset(path.relative_to(root).as_posix() for path in repository_files(root))
    return Snapshot(names, lambda name: (root / name).read_text(encoding="utf-8"))


def tag_snapshot(root: Path, tag: str) -> Snapshot:
    listed = _git(root, "ls-tree", "-r", "--name-only", "-z", tag).stdout
    return Snapshot(
        frozenset(name for name in listed.split("\0") if name), lambda name: _git(root, "show", f"{tag}:{name}").stdout
    )


def _assigned_sources(tree: ast.Module, names: tuple[str, ...]) -> dict[str, str]:
    values: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    values[target.id] = ast.unparse(node.value)
    return values


def _manifest_versions(snapshot: Snapshot) -> dict[str, object]:
    source = snapshot.text(MANIFEST_MODULE)
    return {} if source is None else {MANIFEST_MODULE: _assigned_sources(ast.parse(source), MANIFEST_CONTRACT_NAMES)}


def _tool_floors(snapshot: Snapshot) -> dict[str, object]:
    source = snapshot.text(TOOLS_MODULE)
    if source is None:
        return {}
    tree = ast.parse(source)
    floors = _assigned_sources(tree, ("MINIMUM_PYTHON",))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Tool" and node.args:
            minimum = next((ast.unparse(keyword.value) for keyword in node.keywords if keyword.arg == "minimum"), "()")
            floors[ast.unparse(node.args[0])] = minimum
    return {TOOLS_MODULE: floors}


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
    section = _markdown_section(snapshot.text(FORMATS_DOC) or "", FORMATS_HEADING)
    if section is None:
        return {}
    rows = [line.strip().strip("|") for line in section.split("\n") if line.lstrip().startswith("|")]
    return {FORMATS_DOC: [[cell.strip() for cell in row.split("|")] for row in rows]}


def _skill_directories(snapshot: Snapshot) -> dict[str, object]:
    return {"/".join(name.split("/")[:2]): True for name in snapshot.names if re.match(r"skills/[^/]+/", name)}


CONTRACT_READERS: tuple[tuple[str, Callable[[Snapshot], dict[str, object]]], ...] = (
    (" or ".join(MANIFEST_CONTRACT_NAMES), _manifest_versions),
    ("the required_vars list", _required_variables),
    ("the schema", _review_schemas),
    (f"a format table under {FORMATS_HEADING}", _format_tables),
    ("a skill directory name", _skill_directories),
    ("a tool floor", _tool_floors),
)


def changed_contract_items(released: Snapshot, current: Snapshot) -> list[tuple[str, str]]:
    """Each contract item whose value differs between the two trees, with what about it is the contract."""
    changed: list[tuple[str, str]] = []
    for description, read in CONTRACT_READERS:
        before, after = read(released), read(current)
        changed += [(item, description) for item in sorted(before | after) if before.get(item) != after.get(item)]
    return changed


def versioning_levels(releasing: str) -> list[str]:
    """The level names of the **Levels** list in the Versioning section, lowercased, in the order written."""
    section = _markdown_section(releasing.replace("\r\n", "\n"), VERSIONING_HEADING) or ""
    _, found, rest = section.partition("**Levels**")
    levels: list[str] = []
    for line in rest.split("\n")[1:] if found else []:
        if match := LEVEL_ITEM.match(line):
            levels.append(match.group(1).lower())
        elif line.strip() and not line.startswith(("- ", "  ")):
            break
    return levels


def notes_entries(text: str | None) -> list[NotesEntry]:
    """The upgrade-notes entries: each ### heading with its field lines."""
    entries: list[NotesEntry] = []
    for block in NOTES_ENTRY_START.split(text or ""):
        if block.startswith("### "):
            heading, _, body = block.partition("\n")
            fields = {match.group(1): match.group(2) for match in NOTES_FIELD.finditer(body)}
            normalized = "\n".join(line.rstrip() for line in block.strip().split("\n"))
            entries.append(NotesEntry(heading[4:].strip(), fields, normalized))
    return entries


def _either(words: list[str]) -> str:
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])}, or {words[-1]}"


def notes_entry_problems(entry: NotesEntry, levels: list[str]) -> list[str]:
    where = f"{UPGRADE_NOTES} entry '{entry.heading}'"
    problems = [
        f"{where} is missing '- {field}:' or leaves it empty" for field in NOTES_FIELDS if not entry.fields.get(field)
    ]
    level = re.match(r"[A-Za-z]+", entry.fields.get("Level", ""))
    if level and level.group(0).lower() not in levels:
        problems.append(
            f"{where} starts its Level with '{level.group(0)}', not one of the levels in the Versioning section of "
            f"{RELEASING_DOC}: {_either(levels)}"
        )
    contract = entry.fields.get("Contract")
    if contract and contract.lower() != "none" and not entry.contract_items:
        problems.append(f"{where} names no contract item in backticks, nor none, in '- Contract:'")
    pull_request = entry.fields.get("Pull request")
    if pull_request and not PULL_REQUESTS.fullmatch(pull_request.rstrip(".")):
        problems.append(f"{where} names no pull request as #N in '- Pull request:'")
    return problems


def upgrade_notes_problems(root: Path) -> list[str]:
    """Report each contract item changed since the last tag that no upgrade-notes entry added since then names.

    The last tag is the one `git describe --tags --abbrev=0 origin/main` names; with none reachable, nothing has been
    released and every change is free. A new entry must give its level in the Versioning section's own words.
    """
    tag, problem = last_release_tag(root)
    if problem is not None:
        return [problem]
    if tag is None:
        return []
    levels = versioning_levels((root / RELEASING_DOC).read_text(encoding="utf-8"))
    if not levels:
        return [f"{RELEASING_DOC} has no **Levels** list under {VERSIONING_HEADING} for upgrade-notes entries to use"]
    released, current = tag_snapshot(root, tag), working_snapshot(root)
    old_entries = {entry.text for entry in notes_entries(released.text(UPGRADE_NOTES))}
    added = [entry for entry in notes_entries(current.text(UPGRADE_NOTES)) if entry.text not in old_entries]
    problems = [problem for entry in added for problem in notes_entry_problems(entry, levels)]
    named = {item for entry in added for item in entry.contract_items}
    problems += [
        f"{item}: {description} changed since {tag}, and no entry added to {UPGRADE_NOTES} since then names it; add "
        f"one under ## Unreleased naming `{item}` with its level ({_either(levels)}, per the Versioning section of "
        f"{RELEASING_DOC}), the user action or none, and the pull request"
        for item, description in changed_contract_items(released, current)
        if item not in named
    ]
    return problems


class UpgradeNotesPolicies(unittest.TestCase):
    def test_the_repository_names_every_contract_change_since_its_last_tag(self) -> None:
        self.assertEqual([], upgrade_notes_problems(REPOSITORY_ROOT))

    def test_levels_are_the_words_of_the_versioning_section(self) -> None:
        releasing = (REPOSITORY_ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
        self.assertEqual(["patch", "minor", "major"], versioning_levels(releasing))
        self.assertEqual([], versioning_levels("# Releasing\n\n## Before tagging\n"))

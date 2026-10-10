"""The upgrade-notes guard: each contract item changed since the last tag is named by an upgrade note since then."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from validation_support import REPOSITORY_ROOT, _markdown_section, is_skill_file, repository_files

from tools.release_notes import BODY_HEADING, UPGRADE_NOTES, SourcedEntry, body_entries, range_entries

# The upgrade-notes guard: the contract files the Versioning section of docs/releasing.md judges a release by, read at
# the last tag and in the working tree. Each is compared by its contract value, so an edit that leaves the value alone
# needs no entry. The entries are the upgrade notes tools/release_notes.py reads from the commits since the tag, and
# the pull request body the runner was given with --pr-body.
RELEASING_DOC = "docs/releasing.md"
PROFILE_DOC = "docs/implementing-changes.md"
CONTRACT_FILES_HEADING = "## Contract files"
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
NOTES_FIELDS = ("Level", "Contract", "User action")
# The only sections the notes file has below its title are versions', which tools/release_notes.py writes.
NOTES_SECTION = re.compile(r"^## (.*)$", re.MULTILINE)
VERSION = re.compile(r"v\d+\.\d+\.\d+")
PULL_REQUESTS = re.compile(r"#\d+(?:,? (?:and )?#\d+)*")


@dataclass(frozen=True)
class Snapshot:
    """A tree's file names and a reader for their text: the working tree, or a tag read through git show."""

    names: frozenset[str]
    read: Callable[[str], str]

    def text(self, name: str) -> str | None:
        return self.read(name).replace("\r\n", "\n") if name in self.names else None


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
    """Each skill directory in the tree, skills/<name> or skills/<category>/<name>, so a category is not a skill."""
    return {path.parent.as_posix(): True for path in map(PurePosixPath, snapshot.names) if is_skill_file(path)}


# Each reader with what about its item is the contract and the file or directory it reads, which the contract lists
# in docs/implementing-changes.md and the Versioning section of docs/releasing.md must both name.
CONTRACT_READERS: tuple[tuple[str, str, Callable[[Snapshot], dict[str, object]]], ...] = (
    (" or ".join(MANIFEST_CONTRACT_NAMES), MANIFEST_MODULE, _manifest_versions),
    ("the required_vars list", "deploy-meta/", _required_variables),
    ("the schema", "skills/code-review-core/references/", _review_schemas),
    (f"a format table under {FORMATS_HEADING}", FORMATS_DOC, _format_tables),
    ("a skill directory name", "skills/", _skill_directories),
    ("a tool floor", TOOLS_MODULE, _tool_floors),
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
    section = _markdown_section(text.replace("\r\n", "\n"), heading)
    if section is None:
        return [f"{document} has no {heading} section naming the contract files the upgrade-notes check reads"]
    return [
        f"{document} does not name `{path}` under {heading}, though the upgrade-notes check reads {description} from it"
        for description, path, _ in CONTRACT_READERS
        if f"`{path}`" not in section
    ]


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


def _either(words: list[str]) -> str:
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])}, or {words[-1]}"


def notes_entry_problems(item: SourcedEntry, levels: list[str]) -> list[str]:
    """Each field an entry leaves out or fills wrongly; one written by hand in the notes file also names its pull
    request, which the release script otherwise takes from the squash title."""
    entry = item.entry
    where = f"{item.where} entry '{entry.heading}'"
    fields = (*NOTES_FIELDS, "Pull request") if item.from_notes else NOTES_FIELDS
    problems = [
        f"{where} is missing '- {field}:' or leaves it empty" for field in fields if not entry.fields.get(field)
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

    The last tag is the one `git describe --tags --abbrev=0 origin/main` names; with none reachable, nothing has been
    released and every change is free. The notes are the entries tools/release_notes.py reads from the commits since
    the tag and, given a pull request body, from its `## Upgrade note`. With a body, the commits are origin/main's,
    because the body, not the branch's own commit messages, becomes the squash commit's message; without one, they
    are HEAD's, so a local run reads the branch's commits. Each entry must give its level in the Versioning section's
    own words.
    """
    current = working_snapshot(root)
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
    except ValueError as error:
        return [*problems, f"the upgrade-notes check cannot read the commits since {tag}: {error}"]
    if body is not None:
        sourced += [
            SourcedEntry(entry, f"the pull request body's {BODY_HEADING}", None, from_notes=False)
            for entry in body_entries(body)
        ]
    problems += [problem for item in sourced for problem in notes_entry_problems(item, levels)]
    named = {item for sourced_entry in sourced for item in sourced_entry.entry.contract_items}
    problems += [
        f"{item}: {description} changed since {tag}, and no upgrade note since then names it; add an entry under "
        f"{BODY_HEADING} in the pull request body naming `{item}` with its level ({_either(levels)}, per the "
        f"Versioning section of {RELEASING_DOC}) and the user action or none (a local run reads the body from "
        "--pr-body <file>, and otherwise the branch's commit messages)"
        for item, description in changed_contract_items(tag_snapshot(root, tag), current)
        if item not in named
    ]
    return problems


# The pull request body file tests/run_validation.py was given with --pr-body, read by the policy check below.
pull_request_body: Path | None = None


class UpgradeNotesPolicies(unittest.TestCase):
    def test_the_repository_names_every_contract_change_since_its_last_tag(self) -> None:
        body = None if pull_request_body is None else pull_request_body.read_text(encoding="utf-8")
        self.assertEqual([], upgrade_notes_problems(REPOSITORY_ROOT, body))

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

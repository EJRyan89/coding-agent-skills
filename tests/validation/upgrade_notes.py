"""The upgrade-notes guard: each contract item changed since the last tag is named by an upgrade note since then."""

from __future__ import annotations

import ast
import functools
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# release_levels is imported before validation_support, which puts ejryan89-skill-core on sys.path too, so this module
# states the path itself.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import json_reader
import python_reader
from git_client import GitError, output_line
from implementation_profile import CONTRACT_FILES_HEADING, PROFILE
from memoized_walk import MemoizedWalk
from release_levels import RELEASING_DOC, VERSION, VERSIONING_HEADING, level_word, versioning_levels
from skill_menu import name_list
from text_lines import LINE_BREAK
from validation_support import (
    OPERATIONS_CONTRACT,
    REPOSITORY_ROOT,
    CallableResolver,
    PullRequestChecks,
    find_fences,
    is_skill_file,
    listed,
    markdown_section,
    parse_python,
    repository_files,
)

from tools.release_notes import (
    BODY_HEADING,
    CLIENT,
    RELEASE_BRANCH,
    UPGRADE_NOTES,
    ReleaseNotesError,
    SourcedEntry,
    body_entries,
    git_output,
    range_entries,
    release_reaching,
)

# The upgrade-notes guard: the contract files the Versioning section of docs/releasing.md judges a release by, read at
# the last tag, at the commit where HEAD meets origin/main, and in the working tree. Each is compared by its contract
# value, so an edit that leaves the value alone needs no entry. The entries are the upgrade notes tools/release_notes.py
# reads from the commits since the tag, and the pull request body the runner was given with --pr-body; an item the
# pull request itself changes is covered only by the pull request's own entries, never by an earlier one since the
# tag. Every git call runs through tools/release_notes.py's git and release_reaching, both over its one GitClient, so
# the tag lookup and the commit range read the same repository whatever GIT_DIR the shell exported.
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
NOTES_SECTION = re.compile(r"## (.*)")
PULL_REQUESTS = re.compile(r"#\d+(?:,? (?:and )?#\d+)*")


@dataclass(frozen=True)
class Snapshot:
    """A tree's file names and a reader for their text: the working tree, or a commit read through git show."""

    names: frozenset[str]
    read: Callable[[str], str]

    def text(self, name: str) -> str | None:
        return self.read(name).replace("\r\n", "\n") if name in self.names else None

    @functools.cached_property
    def python(self) -> ContractValues:
        """The tree's Python contract values, read once however many readers ask."""
        return ContractValues(self)


def last_release_tag(root: Path) -> tuple[str | None, str | None]:
    """The last release on origin/main, as ejryan89-skill-core's release_tags reads it, and a problem; no release tag
    reachable means nothing has been released yet."""
    try:
        # Only git's answer that it names no commit means the branch is missing; any other failure names git's reason.
        if CLIENT.commit_of(RELEASE_BRANCH, directory=root) is None:
            return None, (
                f"{RELEASE_BRANCH} is missing, so the upgrade-notes check cannot find the last tag; "
                "fetch it with `git fetch origin main --tags`"
            )
        # output raises on a failure, so a clone git cannot read is named, never taken for one that is not shallow.
        if output_line(CLIENT.output(["rev-parse", "--is-shallow-repository"], directory=root)) == "true":
            return None, (
                "this clone is shallow, so the upgrade-notes check cannot see the last tag; "
                "fetch the history with `git fetch --unshallow --tags`"
            )
        return release_reaching(root, RELEASE_BRANCH), None
    except (GitError, ReleaseNotesError) as error:
        return None, f"the upgrade-notes check cannot find the last tag: {error}"


def working_snapshot(root: Path) -> Snapshot:
    names = frozenset(path.relative_to(root).as_posix() for path in repository_files(root))
    return Snapshot(names, lambda name: (root / name).read_text(encoding="utf-8"))


def revision_snapshot(root: Path, revision: str) -> Snapshot:
    """A commit's tree, the tag's or the merge base's; listing it, and reading a file from it, raise ReleaseNotesError
    when git cannot."""
    listed = git_output(root, "ls-tree", "-r", "--name-only", "-z", revision)
    return Snapshot(
        frozenset(name for name in listed.split("\0") if name),
        lambda name: git_output(root, "show", f"{revision}:{name}"),
    )


# The folders a module a contract file imports by name is looked for in, in the order sys.path holds them for the
# contract files: ejryan89-skill-core's scripts, which deployer/__init__.py puts first and which is pdf_tool.py's own
# folder, and then the repository root, from which the deployer runs. The importing file's own folder is never
# searched for the deployer's modules, since Python does not search it for an absolute import. A name a relative
# import binds is refused, a stated limit, since no contract value is reached through one.
MODULE_FOLDERS = ("skills/ejryan89-skill-core/scripts", "")
# The methods that change a list, a dict, or a set in place, which a followed name's module must not call on it.
MUTATING_METHODS = frozenset(
    {
        "update",
        "append",
        "extend",
        "insert",
        "setdefault",
        "pop",
        "popitem",
        "clear",
        "remove",
        "add",
        "discard",
        "sort",
        "reverse",
        "difference_update",
        "intersection_update",
        "symmetric_difference_update",
        "__setitem__",
        "__delitem__",
        "__iadd__",
        "__ior__",
        "__iand__",
        "__isub__",
        "__ixor__",
        "__imul__",
    }
)
# CallableResolver's mark for a name bound by a definition, a parameter, or a relative import, which names no module.
LOCAL_BINDING = "<local>."


class UnresolvedValue(ValueError):
    """The part of a contract value the check cannot follow to a literal, as its source text."""


# What each binding a contract value is followed through resolves to, once a question, by its file, its scope's id, and
# its name; None for one reached again while it is resolved.
BindingWalk = MemoizedWalk[tuple[str, int, str], ast.expr | None]


class ContractValues:
    """A snapshot's Python contract values, each read as the literal it holds, wherever it is defined.

    A name is followed to the one value the scope that owns it binds it to, a function's or a class's included, and a
    name an import binds, or a module's attribute, to the one value that module binds the name to at its top level,
    when the snapshot holds the module's file. A value that leads anywhere else is refused: a name bound more than
    once, or not at all, or by a parameter, a definition, a loop over anything but a literal, or a relative import, a
    call, a subscript, an operator a literal does not hold, or a module the snapshot does not hold. So is a name its
    own module updates in place, anywhere: an item or an attribute of it assigned, augmented, or deleted, or one of
    MUTATING_METHODS called on it. `refused` names the file and the expression, so an indirection cannot hide a
    change, and the value compares by its own text meanwhile.

    Stated limits, since no code here writes them: a write to a followed name from another module, as
    `versions.GH_MIN = ...` in an importer, or an update through another name bound to the same object, is not seen;
    a star import in a followed module after the name's assignment is not seen rebinding it; a comprehension in a
    class body resolves a name to the class's binding, as CallableResolver reads one, where Python reads the module's;
    a Tool built through an alias of the class, dataclasses.replace, or functools.partial is not read at all; and an
    update through a function or an unbound method that takes the name as an argument, as `dict.update(X, ...)` or
    `operator.setitem(X, ...)`, is not seen.
    """

    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self.refused: list[str] = []
        self._resolvers: dict[str, CallableResolver | None] = {}
        self._unparsed: dict[str, str] = {}
        # Each file's parameter defaults, which CallableResolver binds a parameter to, though a caller may pass another.
        self._defaults: dict[str, set[int]] = {}
        # Each file's names updated in place, by the id of the scope that owns the name, with the update's text.
        self._updates: dict[str, dict[tuple[int, str], str]] = {}
        # The one value each binding a question resolves is bound to, by its file, its scope's id, and its name.
        self._bound: dict[tuple[str, int, str], ast.expr | str | None] = {}

    def _resolver(self, path: str) -> CallableResolver | None:
        """The bindings of a Python file of the snapshot, or None when it holds no such file or the file does not
        parse, whose reason is kept."""
        if path not in self._resolvers:
            source = self.snapshot.text(path)
            resolver = None
            if source is not None:
                try:
                    resolver = CallableResolver(python_reader.parse(source))
                except python_reader.PythonRefused as error:
                    self._unparsed[path] = str(error)
                else:
                    self._defaults[path] = {
                        id(default)
                        for node in ast.walk(resolver.tree)
                        if isinstance(node, ast.arguments)
                        for default in [*node.defaults, *node.kw_defaults]
                        if default is not None
                    }
                    self._updates[path] = _updates(resolver)
            self._resolvers[path] = resolver
        return self._resolvers[path]

    def module(self, path: str) -> CallableResolver | None:
        """A contract file's bindings, None when the snapshot holds no such file, and refused when it does not parse."""
        resolver = self._resolver(path)
        if path in self._unparsed:
            self.record(
                f"{path} does not parse, so the upgrade-notes check cannot read its contract values: "
                f"{self._unparsed[path]}"
            )
        return resolver

    def source(self, path: str, node: ast.expr, what: str) -> str:
        """The text of the literal an expression of a contract file holds, or, when the check cannot follow it to one,
        the expression's own text, with the refusal recorded."""
        try:
            return self._checked(self._resolve(path, node, MemoizedWalk(self._bound_value, None)), ast.unparse(node))
        except UnresolvedValue as error:
            self._refuse(path, f"{what}, `{ast.unparse(node)}`,", error)
            return ast.unparse(node)

    def module_sources(self, path: str, names: tuple[str, ...]) -> dict[str, str]:
        """The text of the literal each of names holds, for each name a contract file binds at its top level."""
        resolver = self.module(path)
        if resolver is None:
            return {}
        scope = resolver.scopes[id(resolver.tree)]
        sources: dict[str, str] = {}
        for name in names:
            if not scope.bindings.get(name):
                continue
            try:
                binding = self._binding(
                    path, scope.bindings[name], id(scope), name, MemoizedWalk(self._bound_value, None)
                )
                sources[name] = self._checked(binding, name)
            except UnresolvedValue as error:
                self._refuse(path, f"`{name}`", error)
                sources[name] = f"{name}, unresolved at {error}"
        return sources

    def record(self, problem: str) -> None:
        # A snapshot's values are read once for each comparison, so a refusal is kept once.
        if problem not in self.refused:
            self.refused.append(problem)

    def _refuse(self, path: str, what: str, error: UnresolvedValue) -> None:
        self.record(
            f"{path}: the upgrade-notes check cannot follow {what} to a literal past `{error}`; write it as a "
            "literal, or as a name this file, or a module it imports by name, binds once to one, so a change to it "
            "is compared by its value"
        )

    @staticmethod
    def _checked(value: ast.expr, shown: str) -> str:
        try:
            python_reader.literal(value)
        except python_reader.PythonRefused:
            raise UnresolvedValue(shown) from None
        return ast.unparse(value)

    def _resolve(self, path: str, node: ast.expr, walk: BindingWalk) -> ast.expr:
        """A copy of an expression of the file with each name and attribute in it replaced by the value it holds."""
        resolver = self._resolver(path)
        if resolver is None:
            raise UnresolvedValue(ast.unparse(node))
        if isinstance(node, ast.Name):
            scope = resolver.scopes.get(id(node))
            owner = scope.owner(node.id) if scope is not None else None
            if owner is None:
                raise UnresolvedValue(node.id)
            return self._binding(path, owner.bindings.get(node.id, []), id(owner), node.id, walk)
        if isinstance(node, ast.Attribute):
            dotted = resolver.name(node)
            if not dotted or dotted.startswith(LOCAL_BINDING):
                raise UnresolvedValue(ast.unparse(node))
            return self._qualified(path, dotted, ast.unparse(node), walk)
        return self._rebuilt(path, node, walk)

    def _rebuilt(self, path: str, node: ast.expr, walk: BindingWalk) -> ast.expr:
        """A literal's node, as ast.literal_eval reads one, built again from its parts once each is resolved."""

        def each(part: ast.expr) -> ast.expr:
            return self._resolve(path, part, walk)

        if isinstance(node, ast.Constant):
            return node
        if isinstance(node, ast.Tuple):
            return ast.Tuple([each(item) for item in node.elts], ast.Load())
        if isinstance(node, ast.List):
            return ast.List([each(item) for item in node.elts], ast.Load())
        if isinstance(node, ast.Set):
            return ast.Set([each(item) for item in node.elts])
        if isinstance(node, ast.Dict):
            keys = [None if key is None else each(key) for key in node.keys]
            return ast.Dict(keys, [each(value) for value in node.values])
        if isinstance(node, ast.UnaryOp):
            return ast.UnaryOp(node.op, each(node.operand))
        if isinstance(node, ast.BinOp):
            return ast.BinOp(each(node.left), node.op, each(node.right))
        raise UnresolvedValue(ast.unparse(node))

    def _binding(
        self,
        path: str,
        values: list[ast.expr | str | None],
        scope: int,
        name: str,
        walk: BindingWalk,
    ) -> ast.expr:
        """The value of the one binding a scope of the file, by its id, holds for a name: an expression, or the
        module-level value an import binds the name to, resolved once for the question by its walk; a binding reached
        again while it is resolved leads back to itself, and is refused."""
        key = (path, scope, name)
        if len(values) != 1:
            raise UnresolvedValue(name)
        self._bound[key] = values[0]
        resolved = walk(key)
        if resolved is None:
            raise UnresolvedValue(name)
        return resolved

    def _bound_value(self, key: tuple[str, int, str], walk: BindingWalk) -> ast.expr | None:
        """What a binding resolves to, read once for the question by its walk."""
        path, scope, name = key
        update = self._updates.get(path, {}).get((scope, name))
        if update is not None:
            raise UnresolvedValue(update)
        value = self._bound[key]
        if isinstance(value, ast.expr) and id(value) not in self._defaults.get(path, set()):
            return self._resolve(path, value, walk)
        if isinstance(value, str) and not value.startswith(LOCAL_BINDING):
            return self._qualified(path, value, value, walk)
        raise UnresolvedValue(name)

    def _qualified(self, path: str, dotted: str, shown: str, walk: BindingWalk) -> ast.expr:
        """The value of a dotted name an import reaches: the name its last part is, bound at the top level of the
        module the rest names, found as the importing file finds it."""
        module, _, name = dotted.rpartition(".")
        found = self._module_file(module) if module else None
        resolver = self._resolver(found) if found is not None else None
        if found is None or resolver is None:
            raise UnresolvedValue(shown)
        module_scope = resolver.scopes[id(resolver.tree)]
        return self._binding(found, module_scope.bindings.get(name, []), id(module_scope), name, walk)

    def _module_file(self, module: str) -> str | None:
        """The file an absolute import of a module finds, on the sys.path every contract file shares."""
        relative = f"{module.replace('.', '/')}.py"
        candidates = [f"{folder}/{relative}" if folder else relative for folder in MODULE_FOLDERS]
        return next((name for name in candidates if name in self.snapshot.names), None)


def _updates(resolver: CallableResolver) -> dict[tuple[int, str], str]:
    """Each name a module updates in place, by the id of the scope that owns it and the name, with the first update's
    text: an item or an attribute of it, at any depth, assigned, augmented, or deleted, or one of MUTATING_METHODS
    called on it or on an item of it."""
    found: dict[tuple[int, str], str] = {}

    def note(target: ast.expr, text: str) -> None:
        root = target
        while isinstance(root, (ast.Subscript, ast.Attribute)):
            root = root.value
        if not isinstance(root, ast.Name):
            return
        scope = resolver.scopes.get(id(root))
        owner = scope.owner(root.id) if scope is not None else None
        if owner is not None:
            found.setdefault((id(owner), root.id), text)

    # ast.walk meets a statement before its targets, so the statement's text is kept where there is one.
    for node in ast.walk(resolver.tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for part in (part for target in targets for part in ast.walk(target)):
                if isinstance(part, (ast.Subscript, ast.Attribute)) and isinstance(part.ctx, (ast.Store, ast.Del)):
                    note(part, ast.unparse(node))
        elif isinstance(node, (ast.Subscript, ast.Attribute)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            note(node, ast.unparse(node))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in MUTATING_METHODS:
            note(node.func.value, ast.unparse(node))
    return found


def _manifest_versions(snapshot: Snapshot) -> dict[str, object]:
    if snapshot.text(MANIFEST_MODULE) is None:
        return {}
    return {MANIFEST_MODULE: snapshot.python.module_sources(MANIFEST_MODULE, MANIFEST_CONTRACT_NAMES)}


def _tool_floors(snapshot: Snapshot) -> dict[str, object]:
    values = snapshot.python
    resolver = values.module(TOOLS_MODULE)
    if resolver is None:
        return {}
    tree = resolver.tree
    floors: dict[str, object] = dict(values.module_sources(TOOLS_MODULE, ("MINIMUM_PYTHON",)))
    fields = _tool_fields(tree)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and ast.unparse(node.func).rsplit(".", 1)[-1] == TOOL_CLASS):
            continue
        place = f"the {TOOL_CLASS} at line {node.lineno}"
        if any(isinstance(argument, ast.Starred) for argument in node.args) or any(
            item.arg is None for item in node.keywords
        ):
            values.record(
                f"{TOOLS_MODULE}: {place} unpacks its arguments, so the upgrade-notes check cannot tell which is a "
                "floor; pass each by position or by keyword"
            )
        given: dict[str | None, ast.expr] = {
            **dict(zip(fields, node.args, strict=False)),
            **{item.arg: item.value for item in node.keywords},
        }
        name = given.get("name")
        key = place if name is None else values.source(TOOLS_MODULE, name, f"the name of {place}")
        floors[key] = {
            field: values.source(TOOLS_MODULE, value, f"the {field} of {TOOL_CLASS} {key}")
            for field, value in given.items()
            if field in TOOL_FLOOR_FIELDS
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
    if snapshot.text(PDF_POLICY_MODULE) is None:
        return {}
    return {PDF_POLICY_MODULE: snapshot.python.module_sources(PDF_POLICY_MODULE, ("MINIMUMS",))}


def _required_variables(snapshot: Snapshot) -> dict[str, object]:
    # A missing file or an empty list leaves the item out, so a skill added or removed without variables changes
    # nothing here; its directory is the contract item.
    values: dict[str, object] = {}
    for name in sorted(filter(DEPLOY_META.fullmatch, snapshot.names)):
        try:
            variables = json_reader.loads(snapshot.text(name) or "").get("required_vars")
        # ValueError covers json_reader's whole family, nesting too deep and an over-long integer included;
        # AttributeError, a document that is no object.
        except (ValueError, AttributeError):
            variables = "unreadable"
        # A list of names, read by skill_menu's name_list as the deployer's loader reads it, so null is none,
        # compares in any order; any other value, a mixed list included, compares as it is.
        names = name_list(variables)
        if names:
            values[name] = sorted(names)
        elif names is None and variables:
            values[name] = variables
    return values


def _review_schemas(snapshot: Snapshot) -> dict[str, object]:
    values: dict[str, object] = {}
    for name in sorted(filter(REVIEW_SCHEMA.fullmatch, snapshot.names)):
        try:
            values[name] = json_reader.loads(snapshot.text(name) or "")
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
CONTRACT_LISTS = ((PROFILE, CONTRACT_FILES_HEADING), (RELEASING_DOC, VERSIONING_HEADING))


def changed_contract_items(released: Snapshot, current: Snapshot) -> list[tuple[str, str]]:
    """Each contract item whose value differs between the two trees, with what about it is the contract."""
    changed: list[tuple[str, str]] = []
    for description, _, read in CONTRACT_READERS:
        before, after = read(released), read(current)
        changed += [(item, description) for item in sorted(before | after) if before.get(item) != after.get(item)]
    return changed


def contract_value_problems(snapshot: Snapshot) -> list[str]:
    """Each Python contract value of a tree that the check cannot follow to a literal, by file and expression."""
    for _, _, read in CONTRACT_READERS:
        read(snapshot)
    return list(snapshot.python.refused)


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


def notes_sections(notes: str) -> list[str]:
    """The heading of each `## ` section of the notes file, read outside every code fence: a fenced `## ` line is the
    fence's text, as GitHub renders it and tools/release_notes.py reads it inside an entry."""
    lines = LINE_BREAK.split(notes)
    fenced = {index for fence in find_fences(lines) for index in fence.lines()}
    matches = (NOTES_SECTION.fullmatch(line) for index, line in enumerate(lines) if index not in fenced)
    return [match.group(1) for match in matches if match is not None]


def notes_layout_problems(notes: str | None) -> list[str]:
    """A section of the notes file that is not a version's: entries now live in pull request bodies."""
    return [
        f"{UPGRADE_NOTES} has a '## {heading}' section; an entry goes under {BODY_HEADING} in the pull request "
        "body, and tools/release_notes.py writes each version's section from the merged commits when it is released"
        for heading in notes_sections(notes or "")
        if not VERSION.fullmatch(heading.strip())
    ]


def shared_heading_problems(sourced: list[SourcedEntry], body: list[SourcedEntry]) -> list[str]:
    """Each entry of the pull request body whose heading an entry since the tag, or an earlier one of the body, already
    has: tools/release_notes.py keeps both, and the release preparer must merge them, so the author names the change
    under a heading of its own."""
    seen = {item.entry.heading: item.where for item in reversed(sourced)}
    problems: list[str] = []
    for item in body:
        heading = item.entry.heading
        if heading in seen:
            problems.append(
                f"{item.where} entry '{heading}' shares its heading with the entry in {seen[heading]}; give this "
                "change's entry a heading of its own, since the release section keeps one entry per heading"
            )
        else:
            seen[heading] = item.where
    return problems


def _entry_advice(item: str, levels: list[str]) -> str:
    return (
        f"add an entry under {BODY_HEADING} in the pull request body naming `{item}` with its level ({listed(levels)}, "
        f"per the Versioning section of {RELEASING_DOC}) and the user action or none (a local run reads the body from "
        "--pr-body <file>, and otherwise the branch's commit messages)"
    )


def own_change_problems(
    changed: list[tuple[str, str]], own: list[SourcedEntry], base: str, tag: str | None, levels: list[str]
) -> list[str]:
    """Each contract item the pull request itself changes, against the commit where HEAD meets origin/main, that the
    pull request's own entries do not name in backticks in '- Contract:'. That one form is accepted: an entry merged
    since the tag that names the item covers that earlier change, and a `none`, a prose mention, or an entry for
    another item covers nothing, so the release notes tell a user each time the item changes. With no tag, nothing
    has been released, so no entry since one is read."""
    named = {item for sourced_entry in own for item in sourced_entry.entry.contract_items}
    earlier = "" if tag is None else f", whatever an entry since {tag} names"
    return [
        f"{item}: {description} changed in this pull request, against {base[:7]} where HEAD meets {RELEASE_BRANCH}, "
        f"and the pull request's own upgrade note does not name it{earlier}; " + _entry_advice(item, levels)
        for item, description in changed
        if item not in named
    ]


def upgrade_notes_problems(root: Path, body: str | None = None) -> list[str]:
    """Report each contract item the pull request changes that its own upgrade note does not name, and each other
    contract item changed since the last tag that no upgrade note since then names.

    The last tag is the highest release tag reachable from origin/main, as ejryan89-skill-core's release_tags defines
    one; with none reachable, nothing has been released, so no change since a release is charged, while the pull
    request is still held to naming its own changes, a comparison that needs no tag. The notes are the entries
    tools/release_notes.py reads from the commits since the tag and, given a pull request body, from its
    `## Upgrade note`. With a body, the commits are origin/main's,
    because the body, not the branch's own commit messages, becomes the squash commit's message; without one, they
    are HEAD's, so a local run reads the branch's commits. The pull request's own changes are the working tree's
    against the merge base of HEAD and origin/main, so a branch behind origin/main is not charged with main's later
    changes, and its own entries are the body's, or without one the branch's commits since that merge base. Each
    entry must give its level in the Versioning section's own words, and each Python contract value must be one the
    check can follow to a literal, so an indirection cannot hide a change.
    """
    try:
        current = working_snapshot(root)
    except ReleaseNotesError as error:
        return [f"the upgrade-notes check cannot list the checkout's files: {error}"]
    problems = notes_layout_problems(current.text(UPGRADE_NOTES)) + contract_value_problems(current)
    tag, problem = last_release_tag(root)
    if problem is not None:
        return [*problems, problem]
    levels = versioning_levels((root / RELEASING_DOC).read_text(encoding="utf-8"))
    if not levels:
        return [f"{RELEASING_DOC} has no **Levels** list under {VERSIONING_HEADING} for upgrade-notes entries to use"]
    sourced: list[SourcedEntry] = []
    changed: list[tuple[str, str]] = []
    try:
        # With no release, the release comparison has nothing to compare with; the pull request's own still runs.
        if tag is not None:
            sourced = range_entries(root, tag, RELEASE_BRANCH if body is not None else "HEAD")
            changed = changed_contract_items(revision_snapshot(root, tag), current)
        base = output_line(git_output(root, "merge-base", "HEAD", RELEASE_BRANCH))
        own_changed = changed_contract_items(revision_snapshot(root, base), current)
        own = [] if body is not None else range_entries(root, base, "HEAD")
    except ReleaseNotesError as error:
        since = f"since {tag}" if tag is not None else f"where HEAD meets {RELEASE_BRANCH}"
        return [*problems, f"the upgrade-notes check cannot read the commits and the tree {since}: {error}"]
    if body is not None:
        own = [
            SourcedEntry(entry, f"the pull request body's {BODY_HEADING}", None, from_notes=False)
            for entry in body_entries(body)
        ]
        problems += shared_heading_problems(sourced, own)
        sourced += own
    # With no tag and no body, the branch's own commits are the only entries read, so their fields are held too.
    checked = [*sourced, *own] if tag is None and body is None else sourced
    problems += [problem for item in checked for problem in notes_entry_problems(item, levels)]
    problems += own_change_problems(own_changed, own, base, tag, levels)
    charged = {item for item, _ in own_changed}
    named = {item for sourced_entry in sourced for item in sourced_entry.entry.contract_items}
    problems += [
        f"{item}: {description} changed since {tag}, and no upgrade note since then names it; "
        + _entry_advice(item, levels)
        for item, description in changed
        if item not in named and item not in charged
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

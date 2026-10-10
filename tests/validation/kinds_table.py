"""Every deployer pass over the kinds of item reads deployer/kinds.py's table, except where a kind differs and the
module says why beside the code.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from validation_support import REPOSITORY_ROOT, docstring_ids, import_aliases, module_allowance, qualified_name

# Each kind's manifest key, report label, and journal root, written out here rather than read from deployer/kinds.py,
# so that a change to the table cannot also change what this policy looks for.
KIND_STRINGS = frozenset(
    {
        "skills",
        "shared",
        "wrappers",
        "agents",
        "shared asset",
        "runtime adapter",
        "agent",
        "claude",
        "claude-agents",
    }
)
# Strings in KIND_STRINGS that are also other things: "claude", the skills root, is the Claude Code command's name and
# a word a skill's name may not contain. The policy finds these only where a value is looked up or compared: as a
# subscript, a dict display's key, a comparison's operand, or an argument of get, setdefault, or pop.
AMBIGUOUS_KIND_STRINGS = frozenset({"claude"})
LOOKUP_METHODS = frozenset({"get", "setdefault", "pop"})
# The names deployer/kinds.py gives one kind, its key, or its label. A pass that names one of these in a comparison
# or beside another in a table treats that kind apart from the others.
KIND_NAMES = frozenset(
    f"deployer.kinds.{name}"
    for name in ("SKILL", "SHARED", "ADAPTER_KIND", "AGENT_KIND", "ADAPTERS", "ADAPTER", "SHARED_ASSET", "AGENT")
)
# A module states why a pass treats a kind apart beside the code, as a module-level KINDS_ALLOWED = {pass: reason},
# where a pass is a function's qualified name, such as "Manifest.ownership", or a module-level assignment's target.
KINDS_ALLOWANCE = "KINDS_ALLOWED"
# The table itself, which names every kind on purpose.
KINDS_MODULE = "deployer/kinds.py"
MODULE_SCOPE = "<module>"


def _scanned_files(root: Path) -> list[Path]:
    """The deployer's code, other than the table."""
    files = [root / "deploy.py", *(root / "deployer").rglob("*.py")]
    return [path for path in files if path.is_file() and path.relative_to(root).as_posix() != KINDS_MODULE]


def _relative_import_aliases(tree: ast.Module, package: str) -> dict[str, str]:
    """What each name a relative import binds stands for, such as deployer.kinds.SKILL for `from .kinds import SKILL`
    in a module of the deployer package."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level == 0:
            continue
        parts = package.split(".")
        base = ".".join(parts[: len(parts) - (node.level - 1)])
        module = f"{base}.{node.module}" if node.module else base
        for alias in node.names:
            aliases[alias.asname or alias.name] = f"{module}.{alias.name}"
    return aliases


def _names_a_kind(node: ast.AST, aliases: dict[str, str]) -> bool:
    """Whether an expression is one of KIND_NAMES or an attribute of one, such as SKILL.key."""
    qualified = qualified_name(node, aliases)
    return any(qualified == name or qualified.startswith(f"{name}.") for name in KIND_NAMES)


def _compares_a_kind(node: ast.AST, aliases: dict[str, str]) -> bool:
    """Whether a comparison's operand is a kind, or a display holding one, as in `kind in (SKILL, ADAPTER_KIND)`."""
    return any(_names_a_kind(part, aliases) for part in (node, *_table_entries(node)))


def _table_entries(node: ast.AST) -> list[ast.expr]:
    """A display's keys, or its elements: what a hand-written table of kinds would name each kind in."""
    if isinstance(node, ast.Dict):
        return [key for key in node.keys if key is not None]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return list(node.elts)
    return []


def _path_components(tree: ast.Module) -> set[int]:
    """The id of each string joined into a path with /, such as the "skills" in `home / ".claude" / "skills"`: a
    directory's name, which some kinds share with their manifest key."""
    return {
        id(operand)
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
        for operand in (node.left, node.right)
        if isinstance(operand, ast.Constant) and isinstance(operand.value, str)
    }


def _lookups(tree: ast.Module) -> set[int]:
    """The id of each expression a value is looked up or compared by: a subscript's index, a dict display's key, a
    comparison's operand or an element of a display there, and each argument of a get, setdefault, or pop call."""
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            found.append(node.slice)
        elif isinstance(node, ast.Dict):
            found += [key for key in node.keys if key is not None]
        elif isinstance(node, ast.Compare):
            for operand in (node.left, *node.comparators):
                found += [operand, *_table_entries(operand)]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in LOOKUP_METHODS:
            found += [*node.args, *(keyword.value for keyword in node.keywords)]
    return {id(node) for node in found}


def _node_findings(
    node: ast.AST, skipped: set[int], lookups: set[int], aliases: dict[str, str]
) -> list[tuple[int, str]]:
    """What node itself does that treats a kind apart, as (line, description), without its children's."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skipped:
        named = node.value in KIND_STRINGS and (node.value not in AMBIGUOUS_KIND_STRINGS or id(node) in lookups)
        return [(node.lineno, f"names the kind literal {node.value!r}")] if named else []
    if isinstance(node, ast.Compare) and any(
        _compares_a_kind(operand, aliases) for operand in (node.left, *node.comparators)
    ):
        return [(node.lineno, "branches on a kind")]
    if isinstance(node, ast.expr) and sum(1 for entry in _table_entries(node) if _names_a_kind(entry, aliases)) >= 2:
        return [(node.lineno, "lists kinds by hand")]
    return []


def _statement_scope(node: ast.stmt) -> str:
    """The pass a module-level statement is: the name it assigns, or the module itself."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
    if len(targets) == 1 and isinstance(targets[0], ast.Name):
        return targets[0].id
    return MODULE_SCOPE


def kind_findings(tree: ast.Module, package: str) -> list[tuple[int, str, str]]:
    """Each place a module treats a kind apart, as (line, pass, description), ignoring docstrings, path components,
    and the module's allowance. A pass is the innermost function or class holding it, by qualified name, or the
    module-level assignment holding it, by its target."""
    skipped = docstring_ids(tree) | _path_components(tree)
    lookups = _lookups(tree)
    aliases = {**import_aliases(tree), **_relative_import_aliases(tree, package)}
    found: list[tuple[int, str, str]] = []

    def visit(node: ast.AST, scope: str) -> None:
        found.extend(
            (line, scope, description) for line, description in _node_findings(node, skipped, lookups, aliases)
        )
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, f"{scope}.{child.name}")
            else:
                visit(child, scope)

    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)) and _statement_scope(statement) == KINDS_ALLOWANCE:
            continue
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            visit(statement, statement.name)
        else:
            visit(statement, _statement_scope(statement))
    return found


def kinds_table_problems(root: Path) -> list[str]:
    """Report each deployer pass that names a kind by literal, branches on one, or lists kinds by hand without
    stating why in its module's KINDS_ALLOWED, and each allowance no longer needed.

    CLAUDE.md describes the four kinds once, in deployer/kinds.py, and has every pass over them iterate it, so a kind
    added or changed there reaches every pass.
    """
    problems: list[str] = []
    found: list[str] = []
    for path in sorted(_scanned_files(root), key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = module_allowance(tree, KINDS_ALLOWANCE)
        if allowed is None:
            problems.append(f"{name}: {KINDS_ALLOWANCE} must map each pass to the reason it treats a kind apart")
            allowed = {}
        package = ".".join(Path(name).parent.parts)
        findings = kind_findings(tree, package)
        found += [
            f"{name}:{line} {description} in {scope}; iterate deployer/kinds.py's KINDS, or state why in "
            f"{KINDS_ALLOWANCE}"
            for line, scope, description in findings
            if scope not in allowed
        ]
        problems += [
            f"{name}: {KINDS_ALLOWANCE} allows {scope}, which no longer treats a kind apart"
            for scope in sorted(set(allowed) - {scope for _, scope, _ in findings})
        ]
    return found + problems


class KindsTablePolicies(unittest.TestCase):
    def test_deployer_passes_iterate_the_kinds_table(self) -> None:
        # A kind added to or changed in deployer/kinds.py then reaches every pass, or the pass says why not.
        self.assertEqual([], kinds_table_problems(REPOSITORY_ROOT))

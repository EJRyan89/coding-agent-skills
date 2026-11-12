"""Every deployer refusal ends with the step to take, on a line of its own, except where its module says why not
beside the code.

A refusal's lines unpacked from elsewhere are followed to where they are written (`_Refusals.ending`): a list or tuple
display, a name its function or its module binds once to one and never changes in place, or a caught refusal's own
lines, and a refusal whose followed lines are one is refused like a refusal written with one. The limit: lines unpacked
from anything else, such as a parameter, a call's result, or a list grown by append, are known only when the deployer
runs, so the policy accepts such a call unread rather than guess where its step is.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

import file_presence
from validation_support import (
    REPOSITORY_ROOT,
    import_aliases,
    module_allowance,
    python_trees,
    qualified_name,
    relative_import_aliases,
    scoped_nodes,
    statement_scope,
)

# The deployer's refusal. A class the deployer derives from it is a refusal too, found by its bases.
REFUSAL = "deployer.errors.DeployError"
# The file that defines it, which the policy must have read for any finding, or none, to mean anything.
REFUSAL_MODULE = "deployer/errors.py"
# A refusal whose one line is the whole report, written out here rather than read from the deployer, so a change there
# cannot also change what this policy looks for: a cancellation is the user's own act, which needs no step.
STEPLESS = {"deployer.errors.Cancelled": "a cancellation is the user's own act, so it asks nothing of them"}
# A module states why a refusal ends without a step beside the code, as a module-level STEP_ALLOWED = {scope: reason},
# where a scope is a function's qualified name, such as "Journal.append", or a module-level assignment's target: a
# refusal its every caller adds the step to, or one caught beside it that never reaches the user.
STEP_ALLOWANCE = "STEP_ALLOWED"


def _scanned_files(root: Path) -> list[Path]:
    """The deployer's code: deploy.py and every module of the deployer package."""
    files = [root / "deploy.py", *file_presence.matching(root / "deployer", "**/*.py")]
    return [path for path in files if file_presence.is_present(path, other_is_absent=True)]


def _aliases(tree: ast.Module, name: str) -> dict[str, str]:
    """What each name a module binds by import stands for, its relative imports included."""
    package = ".".join(Path(name).parent.parts)
    return {**import_aliases(tree), **relative_import_aliases(tree, package)}


def _module_name(name: str) -> str:
    """A file's dotted module name, such as deployer.errors for deployer/errors.py."""
    return ".".join(Path(name).with_suffix("").parts)


def refusal_classes(trees: list[tuple[str, ast.Module]]) -> set[str]:
    """DeployError and every class the scanned modules derive from it, directly or through another, by dotted name."""
    bases: dict[str, set[str]] = {}
    for name, tree in trees:
        aliases = _aliases(tree, name)
        module = _module_name(name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                found = {qualified_name(base, aliases) or "" for base in node.bases}
                # A base named in its own module, such as DeployError in deployer/errors.py, stands for that module's.
                bases[f"{module}.{node.name}"] = {base if "." in base else f"{module}.{base}" for base in found if base}
    refusals = {REFUSAL}
    grown = True
    while grown:
        derived = {name for name, parents in bases.items() if parents & refusals}
        grown = not derived <= refusals
        refusals |= derived
    return refusals


def _defines_refusal(trees: list[tuple[str, ast.Module]]) -> bool:
    """Whether the module REFUSAL names was read and defines the class at its top level."""
    module, _, class_name = REFUSAL.rpartition(".")
    return any(
        _module_name(name) == module
        and any(isinstance(node, ast.ClassDef) and node.name == class_name for node in tree.body)
        for name, tree in trees
    )


# How a refusal's lines end, as _Refusals.ending reads them.
STEP, ONE_LINE, COVERED, UNFOLLOWED = "step", "one line", "covered", "unfollowed"
# A list method that changes the list in place, so its display no longer shows its last line.
LIST_MUTATORS = frozenset({"append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse"})
# The definitions whose bodies an `except` clause around them does not reach.
DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


class _Refusals:
    """How one module's refusal calls are read: which class a name stands for, and where an unpacked argument's lines
    come from."""

    def __init__(self, tree: ast.Module, name: str, refusals: set[str]) -> None:
        self.aliases = _aliases(tree, name)
        self.module = _module_name(name)
        self.refusals = refusals
        self.parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    def called(self, node: ast.expr) -> str | None:
        """The dotted class or function a call's target names, a bare name standing for its own module's."""
        called = qualified_name(node, self.aliases)
        return f"{self.module}.{called}" if called and "." not in called else called

    def ending(self, call: ast.Call) -> str:
        """How a refusal's lines end: STEP, ONE_LINE, COVERED, or UNFOLLOWED.

        The last positional argument is the refusal's last line, and a step when another argument comes before it to
        give the ERROR line. An unpacked last argument is followed to its lines: a list or tuple display is read
        element by element, and so is a name its function binds once to such a display, or, when the function neither
        binds nor changes it, one the module binds once to one, and that nothing changes in place. `*exc.lines` in an
        `except` clause that catches only refusals is COVERED: that refusal was built where this policy reads it, so
        its lines already end with a step, or its module states why they need none. Anything else unpacked, such as a
        parameter, a call's result, or a list grown by append, is UNFOLLOWED: its lines are known only when the
        deployer runs, so this policy cannot tell whether they end with a step, and that is its limit.
        """
        arguments = list(call.args)
        followed: set[str] = set()
        while arguments and isinstance(arguments[-1], ast.Starred):
            value = arguments[-1].value
            elements = self._displayed(value, call, followed)
            if elements is not None:
                arguments = [*arguments[:-1], *elements]
            elif self._caught_lines(value, call):
                return COVERED
            else:
                return UNFOLLOWED
        return STEP if len(arguments) > 1 else ONE_LINE

    def _displayed(self, value: ast.expr, call: ast.Call, followed: set[str]) -> list[ast.expr] | None:
        """The elements an unpacked value holds, read from its display, or None when they cannot be read there."""
        if isinstance(value, (ast.List, ast.Tuple)):
            return list(value.elts)
        if not isinstance(value, ast.Name) or value.id in followed:
            return None
        followed.add(value.id)
        holder = self._enclosing(call, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.Module))
        bindings = self._bindings(holder, value.id)
        if bindings == [] and not isinstance(holder, ast.Module):
            # A name the function neither binds nor changes is the module's, such as a remedy held in a constant.
            bindings = self._bindings(self._enclosing(holder, (ast.Module,)), value.id)
        statement = self.parents.get(bindings[0]) if bindings and len(bindings) == 1 else None
        if isinstance(statement, (ast.Assign, ast.AnnAssign)) and isinstance(statement.value, (ast.List, ast.Tuple)):
            return list(statement.value.elts)
        return None

    @staticmethod
    def _bindings(holder: ast.AST, name: str) -> list[ast.Name] | None:
        """Each place the holder binds the name, or None when it changes the name's list in place or takes it as a
        parameter, whose lines no display shows."""
        bindings: list[ast.Name] = []
        for node in ast.walk(holder):
            if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, (ast.Store, ast.Del)):
                bindings.append(node)
            elif _changes_in_place(node, name):
                return None
        return bindings

    def _caught_lines(self, value: ast.expr, call: ast.Call) -> bool:
        """Whether the value is `<name>.lines` of the innermost `except` clause holding the call that binds the name,
        when that clause catches only refusals and does not bind the name again."""
        if not (isinstance(value, ast.Attribute) and value.attr == "lines" and isinstance(value.value, ast.Name)):
            return False
        bound = value.value.id
        node = self.parents.get(call)
        while node is not None and not isinstance(node, DEFINITIONS):
            # A bare `except:` binds no name, so only a clause with a type can bind the one the lines are read from.
            if isinstance(node, ast.ExceptHandler) and node.name == bound and node.type is not None:
                caught = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
                rebound = any(
                    isinstance(child, ast.Name) and child.id == bound and isinstance(child.ctx, ast.Store)
                    for statement in node.body
                    for child in ast.walk(statement)
                )
                return not rebound and all(self.called(expression) in self.refusals for expression in caught)
            node = self.parents.get(node)
        return False

    def _enclosing(self, node: ast.AST, kinds: tuple[type[ast.AST], ...]) -> ast.AST:
        """The innermost ancestor of one of the kinds; the module holds every node."""
        while not isinstance(node, kinds):
            node = self.parents[node]
        return node


def _changes_in_place(node: ast.AST, name: str) -> bool:
    """Whether the node changes the list the name holds in place: a mutating method, a subscript stored or deleted, or
    a global or nonlocal declaration that lets another scope do so."""
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return name in node.names
    if isinstance(node, ast.Attribute):
        return isinstance(node.value, ast.Name) and node.value.id == name and node.attr in LIST_MUTATORS
    if isinstance(node, ast.Subscript):
        return isinstance(node.value, ast.Name) and node.value.id == name and isinstance(node.ctx, (ast.Store, ast.Del))
    return isinstance(node, ast.arg) and node.arg == name


def stepless_refusals(tree: ast.Module, name: str, refusals: set[str]) -> list[tuple[int, str, str]]:
    """Each refusal a module builds from one line, written or followed, as (line, scope, class), ignoring its
    allowance."""
    reader = _Refusals(tree, name, refusals)
    statements = [
        statement
        for statement in tree.body
        if not (isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement_scope(statement) == STEP_ALLOWANCE)
    ]
    found: list[tuple[int, str, str]] = []
    for node, scope in scoped_nodes(statements):
        if not isinstance(node, ast.Call):
            continue
        called = reader.called(node.func)
        if called not in refusals or called in STEPLESS:
            continue
        if reader.ending(node) == ONE_LINE:
            found.append((node.lineno, scope, called.rsplit(".", 1)[-1]))
    return found


def refusal_step_problems(root: Path) -> list[str]:
    """Report each deployer refusal built from one line, which ends at its ERROR line, without stating why in its
    module's STEP_ALLOWED, and each allowance no longer needed.

    A refusal that names what is wrong and not what to do leaves its reader stuck, so each one ends with the step to
    take on a line of its own, or its module says why it need not: every caller adds the step, or it is caught beside
    it and never reaches the user.
    """
    problems: list[str] = []
    ordered = sorted(_scanned_files(root), key=lambda path: path.relative_to(root).as_posix())
    trees = [(name, tree) for _, name, tree in python_trees(ordered, root, problems)]
    if not _defines_refusal(trees):
        # Without the class no call is read as a refusal, so an empty pass would only mean nothing was looked at.
        problems.append(
            f"{REFUSAL_MODULE} defines no {REFUSAL.rsplit('.', 1)[-1]} class, so no deployer refusal could be read; "
            "the policy checked nothing"
        )
    refusals = refusal_classes(trees)
    found: list[str] = []
    for name, tree in trees:
        allowed = module_allowance(tree, STEP_ALLOWANCE)
        if allowed is None:
            problems.append(f"{name}: {STEP_ALLOWANCE} must map each scope to the reason its refusals need no step")
            allowed = {}
        findings = stepless_refusals(tree, name, refusals)
        found += [
            f"{name}:{line} {called} in {scope} ends at its ERROR line; add the step to take as its "
            f"next line, or state why it needs none in {STEP_ALLOWANCE}"
            for line, scope, called in findings
            if scope not in allowed
        ]
        problems += [
            f"{name}: {STEP_ALLOWANCE} allows {scope}, which no longer builds a refusal from one line"
            for scope in sorted(set(allowed) - {scope for _, scope, _ in findings})
        ]
    return found + problems


class RefusalStepPolicies(unittest.TestCase):
    def test_every_deployer_refusal_ends_with_a_step(self) -> None:
        # A refusal added without the step to take fails here, or its module says why it needs none.
        self.assertEqual([], refusal_step_problems(REPOSITORY_ROOT))

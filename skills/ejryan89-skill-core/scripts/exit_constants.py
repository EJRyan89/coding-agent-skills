"""The one reading of the number a name stands for when a program exits with it: a module's int constants and the
constants its imports name, read from the syntax tree without running the module.

Validation's exit tables (`tests/validation/exit_tables.py`) and audit-skill's inventory
(`skills/audit-skill/scripts/skill_inventory.py`) read an exit code given by a name here, so a form either learns
reaches both. `read` takes a module's constants: each name its own scope binds, at the top or in a module-level `if`,
`try`, `with`, loop, or `match` block, by a plain, chained, or annotated assignment of an int literal, `Final` or not.
A name a binding of any other form reaches, an unpacking, an augmented assignment, a loop or `with` target, a walrus,
a `del`, an `except ... as`, a match capture, a function or class definition, an import beside an assignment, or a
`global` statement in any function, is unread, never the literal another binding gives it. What a name stands for is
every value it can hold once the module has run, never fewer: where a block runs whole, a later binding in it replaces
an earlier one, as Python binds it, and the arms of an `if` or a `match` each give theirs; a block that may stop
partway, a `try`'s body, a `with`'s body, which a context manager may leave by suppressing an error, and a loop's body
and `else`, which `break` and `continue` may leave, keeps every value the name held before or was bound in it, so a
handler, the code after the loop, and the code after the `with` see each. `resolve` gives what an expression stands for:
an int literal, a name of the module's own constants, or one an import names, followed through each module a caller's
`locate` finds, re-exports included; None for anything it cannot read, which the caller reports as unread, never as a
code the program does not exit with.

Stated limits, each a form no repository code writes: a name rebound through `globals()`, `setattr` on the module, or
`exec`, a star import after the name's assignment, a `type` statement that binds the name, or an attribute write from
another module, such as `codes.STALE = 9` in the module that imports `codes`, is read as the literals its own
assignments give it, since no binding statement this reader reads shows the change. A relative import, which
python_names leaves out of a module's aliases, beside an assignment of the same name is read as the assignment's
literal, unless the caller adds the relative import's aliases, as validation's exit tables do and audit-skill's
inventory does not. An import inside a function is read as the module's own, as python_names reads it, so a constant
whose name it shares is unread: the reader fails closed there.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from python_names import qualified_name

# The nodes that open a scope of their own, whose bindings are not the module's.
SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


@dataclass(frozen=True)
class ModuleConstants:
    """What a module's names stand for: each name bound only to int literals with every value they give it, each name
    some other binding reaches, and what each name its imports bind stands for, as python_names reads it."""

    values: dict[str, frozenset[int]]
    unread: frozenset[str]
    aliases: dict[str, str]


# Finds the constants of a module by its dotted name, or None for one the caller does not find or read.
Locate = Callable[[str], ModuleConstants | None]


def _int_literal(node: ast.expr | None) -> int | None:
    """The value of an int literal, a bool, which is an int to Python but no exit code anyone writes, excluded."""
    return node.value if isinstance(node, ast.Constant) and type(node.value) is int else None


def _stored(node: ast.AST) -> Iterator[str]:
    """Each name a target expression binds or deletes, through a tuple, a list, or a starred item."""
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Store | ast.Del):
            yield inner.id


def _module_scope(node: ast.AST) -> Iterator[ast.AST]:
    """Every node that runs in the module's own scope: the module's statements, the blocks they hold, and the
    expressions of each, outside the bodies of functions, lambdas, and classes, whose names are each definition's own,
    whose binding forms read() reads apart from a comprehension's targets, which bind in the comprehension's scope."""
    pending: list[ast.AST] = [node]
    while pending:
        current = pending.pop()
        yield current
        if not isinstance(current, SCOPE_NODES):
            pending.extend(ast.iter_child_nodes(current))


# What each name the module binds to int literals can stand for at one point of its run.
State = dict[str, frozenset[int]]


def _merge(*states: State) -> State:
    """What each name can stand for after any of several paths, each ending in one of the states."""
    merged: dict[str, set[int]] = {}
    for state in states:
        for name, numbers in state.items():
            merged.setdefault(name, set()).update(numbers)
    return {name: frozenset(numbers) for name, numbers in merged.items()}


def _flow(body: list[ast.stmt], state: State, whole: bool) -> State:
    """What each name can stand for after a block runs from `state`. In a block that runs `whole`, a statement replaces
    what an earlier one bound, as Python does; in one that may stop partway, a binding adds its value to what the name
    held, so the state after it holds every value the name had at any point of the block."""
    for statement in body:
        state = _step(statement, state, whole)
    return state


def _step(statement: ast.stmt, state: State, whole: bool) -> State:
    """What each name can stand for after one statement: an int literal's assignment binds its names to it, replacing
    what they held only where the block runs `whole`; an `if`'s and a `match`'s arms each give theirs, or the state
    before when no arm runs; a loop's body and `else`, a `try`'s body, and a `with`'s body may stop partway, so each
    keeps what it is given and adds what it binds; a `try`'s handlers start from what its body held at any point, its
    `else` from what the body leaves, and its `finally` from what either leaves."""
    if isinstance(statement, ast.Assign | ast.AnnAssign):
        number = _int_literal(statement.value)
        if number is None:
            return state
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        bound = {target.id: frozenset({number}) for target in targets if isinstance(target, ast.Name)}
        return {**state, **bound} if whole else _merge(state, bound)
    if isinstance(statement, ast.If):
        return _merge(_flow(statement.body, state, whole), _flow(statement.orelse, state, whole))
    if isinstance(statement, ast.Match):
        return _merge(state, *(_flow(case.body, state, whole) for case in statement.cases))
    if isinstance(statement, ast.With | ast.AsyncWith):
        return _flow(statement.body, state, False)
    if isinstance(statement, ast.For | ast.AsyncFor | ast.While):
        return _flow(statement.orelse, _flow(statement.body, state, False), False)
    if isinstance(statement, ast.Try | ast.TryStar):
        tried = _flow(statement.body, state, False)
        ends = [
            _flow(statement.orelse, tried, whole),
            *(_flow(handler.body, tried, whole) for handler in statement.handlers),
        ]
        return _flow(statement.finalbody, _merge(tried, *ends), whole)
    return state


def read(tree: ast.Module, aliases: dict[str, str]) -> ModuleConstants:
    """The constants of a module, given what its imports bind (python_names.import_aliases, with any relative import
    the caller resolves): what each name bound only to int literals in the module's own scope can stand for once the
    module has run, and each name some other binding reaches, as the module's docstring lists them."""
    unread: set[str] = set()
    for node in _module_scope(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            if node.value is None or _int_literal(node.value) is not None:
                continue
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                unread.update(_stored(target))
        elif isinstance(node, ast.AugAssign | ast.For | ast.AsyncFor | ast.NamedExpr):
            unread.update(_stored(node.target))
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            unread.update(_stored(node.optional_vars))
        elif isinstance(node, ast.Delete):
            unread.update(name for target in node.targets for name in _stored(target))
        elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) and node.name:
            unread.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            unread.add(node.rest)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            unread.add(node.name)
    # A function that declares a name global may bind it, so no literal is what the name stands for.
    unread.update(name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names)
    values = _flow(tree.body, {}, True)
    # A name an import binds and an assignment binds too stands for either.
    unread.update(name for name in values if name in aliases)
    return ModuleConstants(
        {name: numbers for name, numbers in values.items() if name not in unread}, frozenset(unread), aliases
    )


def _bound(module: ModuleConstants, name: str) -> bool:
    """Whether a module's own scope binds a name otherwise than by import: to int literals alone, or otherwise."""
    return name in module.values or name in module.unread


def resolve(node: ast.expr, module: ModuleConstants, locate: Locate) -> frozenset[int] | None:
    """The numbers an expression of `module` stands for: an int literal's value, the values a name of the module's
    own constants is bound to, or those of the constant an import names, `from codes import USAGE` or `codes.USAGE`
    after `import codes`, followed through each module `locate` finds by its dotted name, as many re-exports deep as
    they go, and through none twice. None for any other expression, and for a name no module it finds binds to int
    literals alone: one bound otherwise, one of a module `locate` does not find, a relative import's, or a builtin's.
    """
    number = _int_literal(node)
    if number is not None:
        return frozenset({number})
    if isinstance(node, ast.Name) and _bound(module, node.id):
        return module.values.get(node.id)
    dotted = qualified_name(node, module.aliases)
    seen: set[str] = set()
    while dotted and dotted not in seen:
        seen.add(dotted)
        owner, _, name = dotted.rpartition(".")
        other = locate(owner) if owner else None
        if other is None:
            return None
        if _bound(other, name):
            return other.values.get(name)
        dotted = other.aliases.get(name, "")
    return None

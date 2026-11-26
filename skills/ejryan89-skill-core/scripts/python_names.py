"""The one reader of what a Python module's names stand for through its imports.

Validation's policies read a module's imports and resolve each call's dotted name here, through
`tests/validation/validation_support.py`, and audit-skill's inventory (`skills/audit-skill/scripts/skill_inventory.py`)
resolves an exit call here, so both read `import sys as system` and `from os import _exit` alike. It also names the
forms that choose a call's function by a value, which validation's indirection policy refuses and runtime_support
reads a git call through, so both decide the same forms.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleImport:
    """One module an import statement imports, and what it binds each name to: `import a.b as c` imports a.b and
    binds c to a.b, `import a.b` binds a to a, and `from a import b as c` imports a and binds c to a.b."""

    line: int
    module: str
    bindings: tuple[tuple[str, str], ...]


def module_imports(tree: ast.AST) -> list[ModuleImport]:
    """Every absolute import anywhere in a module, in the order ast.walk meets it. Every reader of what a module
    imports reads it here; a relative import names no module a reader can find, so it is left out."""
    found: list[ModuleImport] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                binding = (alias.asname, alias.name) if alias.asname else (top, top)
                found.append(ModuleImport(node.lineno, alias.name, (binding,)))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            bindings = tuple((alias.asname or alias.name, f"{node.module}.{alias.name}") for alias in node.names)
            found.append(ModuleImport(node.lineno, node.module, bindings))
    return found


def import_aliases(tree: ast.AST) -> dict[str, str]:
    """What each name a module binds by import stands for, anywhere in it: `import shutil as sh` binds sh to shutil,
    `from tempfile import TemporaryFile as T` binds T to tempfile.TemporaryFile, and `import os.path` binds os."""
    return {name: target for found in module_imports(tree) for name, target in found.bindings}


# The type parameters a function, a class, or a type statement declares (PEP 695). Each binds its `name`, a string and
# not a Name node, in an annotation scope around what declares it: the body, a class's methods, the annotations, a
# class's bases, and a type statement's value see it, and a function's decorators and defaults do not.
TYPE_PARAMETERS = (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)
# The nodes that declare type parameters, each in its `type_params`.
GENERIC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.TypeAlias)


def type_parameter_names(node: ast.AST) -> list[str]:
    """Each name the type parameters of a function, a class, or a type statement bind, in order, as `def f[T, *Ts,
    **P]()` binds T, Ts, and P; [] for a node that declares none. Every reader of what a scope binds reads them here,
    since no Name node spells them."""
    if not isinstance(node, GENERIC_NODES):
        return []
    return [parameter.name for parameter in node.type_params if isinstance(parameter, TYPE_PARAMETERS)]


def qualified_name(node: ast.AST, aliases: dict[str, str]) -> str:
    """The dotted name an expression stands for through the module's imports, such as shutil.rmtree for sh.rmtree
    after `import shutil as sh`, or "" when it is not a dotted name. A name bound otherwise stands for itself."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = qualified_name(node.value, aliases)
        return f"{base}.{node.attr}" if base else ""
    return ""


# The expressions that choose a call's function by a value, by their ast node: an `and` or an `or`, an if-expression,
# and an assignment expression, a subscript of a literal (LITERAL_DISPLAYS), and an attribute of an assignment
# expression's value, which chosen_callee reads. Python indirection is restricted rather than modelled (docs/design.md):
# validation's indirection policy (tests/validation/python_checks.py) refuses each at the source as a call's function,
# in every module the claim policies read, and runtime_support records a git call whose function one chooses as one
# whose subcommand no literal names, both from this one list.
REFUSED_CALLEES = {
    "BoolOp": "an `and` or an `or`",
    "IfExp": "an if-expression",
    "NamedExpr": "an assignment expression",
}
LITERAL_DISPLAYS: dict[type[ast.expr], str] = {ast.Tuple: "tuple", ast.List: "list", ast.Set: "set", ast.Dict: "dict"}


def chosen_callee(function: ast.expr) -> str:
    """How a call's function is chosen by a value, as REFUSED_CALLEES and LITERAL_DISPLAYS word it: "calls the
    function an if-expression chooses" for `(print if quiet else os.system)(x)`, "calls the function a subscript of a
    literal list chooses" for `[os.system][0](x)`, and "calls an attribute of the value an assignment expression
    binds" for `(fs := shutil).rmtree(p)`, however deep the attribute; "" for any other function. The shared readers
    name nothing such a call runs, so the indirection policy refuses it."""
    if (chosen := REFUSED_CALLEES.get(type(function).__name__)) is not None:
        return f"calls the function {chosen} chooses"
    if isinstance(function, ast.Subscript) and (display := LITERAL_DISPLAYS.get(type(function.value))) is not None:
        return f"calls the function a subscript of a literal {display} chooses"
    receiver = function
    while isinstance(receiver, ast.Attribute):
        receiver = receiver.value
    if receiver is not function and isinstance(receiver, ast.NamedExpr):
        return "calls an attribute of the value an assignment expression binds"
    return ""


def chosen_callables(function: ast.expr) -> list[ast.expr]:
    """The callables a call's function that chosen_callee reads may be, as written: each operand of an `and` or an
    `or`, each branch of an if-expression, an assignment expression's value, each item and dict value of a subscripted
    literal, read through each form again, and an attribute of an assignment expression's value read as that attribute
    of each callable the value may be, as `services.git.run` for `(g := services.git).run`; [function] for any other
    function. It says which callables a value chooses among and never which one it chooses, so a reader that must know
    what such a call runs records it as unread; a starred item or a `**` value stands as itself."""
    if isinstance(function, ast.BoolOp):
        return [callable_ for value in function.values for callable_ in chosen_callables(value)]
    if isinstance(function, ast.IfExp):
        return chosen_callables(function.body) + chosen_callables(function.orelse)
    if isinstance(function, ast.NamedExpr):
        return chosen_callables(function.value)
    if isinstance(function, ast.Subscript) and type(function.value) in LITERAL_DISPLAYS:
        literal = function.value
        items = literal.values if isinstance(literal, ast.Dict) else _items(literal)
        return [callable_ for item in items for callable_ in chosen_callables(item)]
    attributes: list[str] = []
    receiver = function
    while isinstance(receiver, ast.Attribute):
        attributes.append(receiver.attr)
        receiver = receiver.value
    if not attributes or not isinstance(receiver, ast.NamedExpr):
        return [function]
    found: list[ast.expr] = []
    for value in chosen_callables(receiver.value):
        read = value
        for attribute in reversed(attributes):
            read = ast.Attribute(value=read, attr=attribute, ctx=ast.Load())
        found.append(read)
    return found


def _items(literal: ast.expr) -> list[ast.expr]:
    """The items of a tuple, a list, or a set display, as written; [] for any other expression."""
    return literal.elts if isinstance(literal, (ast.Tuple, ast.List, ast.Set)) else []

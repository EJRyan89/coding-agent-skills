"""The one reader of what a Python module's names stand for through its imports.

Validation's policies read a module's imports and resolve each call's dotted name here, through
`tests/validation/validation_support.py`, and audit-skill's inventory (`skills/audit-skill/scripts/skill_inventory.py`)
resolves an exit call here, so both read `import sys as system` and `from os import _exit` alike.
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


def qualified_name(node: ast.AST, aliases: dict[str, str]) -> str:
    """The dotted name an expression stands for through the module's imports, such as shutil.rmtree for sh.rmtree
    after `import shutil as sh`, or "" when it is not a dotted name. A name bound otherwise stands for itself."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = qualified_name(node.value, aliases)
        return f"{base}.{node.attr}" if base else ""
    return ""

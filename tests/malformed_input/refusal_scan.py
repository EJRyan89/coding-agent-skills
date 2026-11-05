"""Every place in the repository's code that catches json_writer's refusal, found by reading the code, so
test_write_refusals.py can fail one that no row of its table drives.

A place counts when it names `json_writer.JsonWriteRefused` or `json_writer.CONTENT_ERRORS`, on the module or by a name
imported from it, as `from json_writer import JsonWriteRefused`: an except clause, a `contextlib.suppress`, or a
module-level tuple of errors an entry point catches, such as an `EXPECTED_ERRORS`. A place is named as reader_scan.py
names a reader: the function that holds it, a method by `Class.method` and a function nested in another by the
outermost function, prefixed `tools.` under tools/, `deployer.` under deployer/, and by the module alone under a skill's
or a repository skill's scripts/ or tests/validation/; a module-level tuple by its own name in place of a function's.
json_writer itself, which raises the refusal, is left out.
"""

from __future__ import annotations

import ast
from pathlib import Path

from reader_scan import REPOSITORY, code_files

WRITER = "json_writer"
# The names of json_writer's that stand for its refusal.
REFUSAL_NAMES = frozenset({"JsonWriteRefused", "CONTENT_ERRORS"})


def _refusal_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """The names a module binds json_writer to, and the names it imports the refusal by."""
    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.asname or alias.name for alias in node.names if alias.name == WRITER}
        elif isinstance(node, ast.ImportFrom) and node.module == WRITER and node.level == 0:
            names |= {alias.asname or alias.name for alias in node.names if alias.name in REFUSAL_NAMES}
    return modules, names


def _names_the_refusal(node: ast.AST, modules: set[str], names: set[str]) -> bool:
    """Whether anything below node names json_writer's refusal."""
    for each in ast.walk(node):
        if (
            isinstance(each, ast.Attribute)
            and each.attr in REFUSAL_NAMES
            and isinstance(each.value, ast.Name)
            and each.value.id in modules
        ):
            return True
        if isinstance(each, ast.Name) and each.id in names:
            return True
    return False


def catching_places(module: str, text: str) -> list[str]:
    """The places of one module's source that name json_writer's refusal, each named as a table row names it."""
    tree = ast.parse(text)
    modules, names = _refusal_names(tree)
    if not modules and not names:
        return []
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _names_the_refusal(node, modules, names):
            found.append(f"{module}.{node.name}")
        elif isinstance(node, ast.ClassDef):
            found += [
                f"{module}.{node.name}.{method.name}"
                for method in node.body
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                and _names_the_refusal(method, modules, names)
            ]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and _names_the_refusal(node, modules, names):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found += [f"{module}.{target.id}" for target in targets if isinstance(target, ast.Name)]
    return found


def repository_places(root: Path = REPOSITORY) -> list[str]:
    """Every place of the repository's code that catches json_writer's refusal."""
    return [
        name
        for module, path in code_files(root)
        if module != WRITER
        for name in catching_places(module, path.read_text(encoding="utf-8"))
    ]

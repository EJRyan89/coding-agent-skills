"""The one reading of a module's CATALOGUE_TESTS: the catalogues of published entries the module decides, such as a
shell's reserved words, a tool's options, or a language's import forms, each with the one test that holds every entry
of it read, refused, or named with why in the module's tables, as NOT_READ.

A module declares it beside the code that reads the catalogues, as a module-level
`CATALOGUE_TESTS = {"<catalogue>": "<suite path>::<test>"}`, the suite's path from the top of the repository and the
test a top-level function of it or a method of one of its top-level classes. Repository validation, in
tests/validation/workarounds.py, holds each value to a test its suite defines, and holds a module that binds NOT_READ
to naming one; implement-change's fix brief lists the catalogues of each file it names, so an author names the test
for a construct among them rather than the catalogue by hand. Which entries a named test holds is review's judgment:
nothing here reads the test's body.
"""

from __future__ import annotations

import ast
import re

import python_reader

# The module-level table a module declares its catalogue tests in.
TABLE = "CATALOGUE_TESTS"
# What separates a reference's suite path from its test, as pytest names a test.
SEPARATOR = "::"
# What a test function's or test method's name starts with, as pytest and unittest collect one.
TEST_PREFIX = "test"
# A test's qualified name: a function's name, or a class's and its method's.
QUALIFIED_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?")


def declared(tree: ast.Module) -> dict[str, str] | None:
    """The module's CATALOGUE_TESTS from the first module-level assignment that binds it, with or without an
    annotation; empty when the module binds none, and None when it is no literal dictionary of non-empty texts."""
    for node in tree.body:
        if not isinstance(node, ast.Assign | ast.AnnAssign) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == TABLE for target in targets):
            continue
        try:
            table = python_reader.literal(node.value)
        except python_reader.CONTENT_ERRORS:
            return None
        texts = isinstance(table, dict) and all(
            isinstance(text, str) and text.strip() for pair in table.items() for text in pair
        )
        return table if texts else None
    return {}


def reference(value: str) -> tuple[str, str] | None:
    """A reference's suite path and test name, or None when it is not `<suite path>::<test>`: the path relative,
    written with `/`, and holding no `..` part, and the test one name or a class's and its method's."""
    path, separator, name = value.partition(SEPARATOR)
    parts = path.split("/")
    if (
        not separator
        or not path
        or "\\" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or not QUALIFIED_NAME.fullmatch(name)
    ):
        return None
    return path, name


def defines(tree: ast.Module, name: str) -> bool:
    """Whether a suite defines the test a qualified name names: a top-level function, or a method of a top-level
    class, whose name starts with `test`, as pytest and unittest collect one; never a function nested in another, a
    helper of another name, or an assignment."""
    owner, _, method = name.rpartition(".")
    if not method.startswith(TEST_PREFIX):
        return False
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    if not owner:
        return any(isinstance(node, functions) and node.name == method for node in tree.body)
    return any(
        isinstance(node, ast.ClassDef)
        and node.name == owner
        and any(isinstance(child, functions) and child.name == method for child in node.body)
        for node in tree.body
    )

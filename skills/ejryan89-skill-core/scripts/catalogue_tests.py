"""The one reading of a module's CATALOGUE_TESTS: the catalogues of published entries the module decides, such as a
shell's reserved words, a tool's options, or a language's import forms, each with the one test that holds every entry
of it read, refused, or named with why in the module's tables, as NOT_READ.

A module declares it beside the code that reads the catalogues, as a module-level
`CATALOGUE_TESTS = {"<catalogue>": "<suite path>::<test>"}`, the suite's path from the top of the repository and the
test one pytest collects from it: a top-level function, or a method of a top-level class pytest collects. Repository
validation, in tests/validation/workarounds.py, holds each value to a test its suite defines, and holds a module that
binds NOT_READ to naming one; implement-change's fix brief lists the catalogues of each file it names, so an author
names the test for a construct among them rather than the catalogue by hand. Which entries a named test holds is
review's judgment: nothing here reads the test's body.
"""

from __future__ import annotations

import ast
import re

import python_names
import python_reader

# The module-level table a module declares its catalogue tests in.
TABLE = "CATALOGUE_TESTS"
# What separates a reference's suite path from its test, as pytest names a test.
SEPARATOR = "::"
# What a test function's or test method's name starts with, as pytest and unittest collect one.
TEST_PREFIX = "test"
# The unittest classes pytest's unittest plugin collects every subclass of, whatever its name or constructor, by each
# name the unittest package exports them under.
TEST_CASES = frozenset(
    {
        "unittest.TestCase",
        "unittest.case.TestCase",
        "unittest.IsolatedAsyncioTestCase",
        "unittest.async_case.IsolatedAsyncioTestCase",
    }
)
# What the name of any other class pytest collects starts with, as its default python_classes reads one; the
# repository's pytest configuration, which validation pins, sets no other.
TEST_CLASS_PREFIX = "Test"
# The methods whose definition in a class or its lineage keeps pytest from collecting a class that is no TestCase.
CONSTRUCTORS = frozenset({"__init__", "__new__"})
# The attribute a class binds to tell pytest whether to collect it.
COLLECT_FLAG = frozenset({"__test__"})
# The base every class has, which adds nothing to what pytest decides.
OBJECT = frozenset({"object", "builtins.object"})
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


# Why a suite does not define a test a reference names, each read after "whose suite": it defines nothing of that name
# pytest could collect; it defines the method in a class pytest does not collect; or it defines the method in a class
# whose collection the suite alone does not settle.
NO_TEST = "defines no test of that name"
UNCOLLECTED = (
    "defines it in a class pytest does not collect, which is no unittest TestCase and either has a name not starting "
    "with `Test` or a lineage that defines __init__ or __new__, or binds __test__"
)
UNSETTLED = (
    "defines it in a class whose collection the suite alone does not settle, through a base imported from another "
    "module or defined later in the suite, a metaclass or another class keyword, or a decorator"
)


def refusal(tree: ast.Module, name: str) -> str | None:
    """Why a suite does not define the test a qualified name names as pytest, at its defaults, collects one, as
    NO_TEST, UNCOLLECTED, or UNSETTLED; None when it does: a top-level function, or a method of a top-level class
    pytest collects, whose name starts with `test`. Never a function nested in another, a helper of another name, or
    an assignment.

    pytest collects a class that is a unittest TestCase, through any import of one or through the suite's own subclass
    of one, and any other class whose name starts with `Test` and whose lineage defines no __init__ or __new__; it
    collects neither when the lineage binds __test__. A class whose lineage the suite alone does not settle is refused
    though pytest may collect it: one with a base imported from another module or defined only later in the suite, a
    metaclass or another class keyword, or a decorator. The method is read in the class's own body alone, so one it
    inherits is refused too."""
    owner, _, method = name.rpartition(".")
    if not method.startswith(TEST_PREFIX):
        return NO_TEST
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    if not owner:
        return None if any(isinstance(node, functions) and node.name == method for node in tree.body) else NO_TEST
    classes = {node.name: (index, node) for index, node in enumerate(tree.body) if isinstance(node, ast.ClassDef)}
    if owner not in classes:
        return NO_TEST
    index, node = classes[owner]
    if not any(isinstance(child, functions) and child.name == method for child in node.body):
        return NO_TEST
    lineage = _lineage(classes, python_names.import_aliases(tree), index, node)
    if lineage is None:
        return UNSETTLED
    members, test_case = lineage
    collected = not any(_binds(member, COLLECT_FLAG) for member in members) and (
        test_case
        or (node.name.startswith(TEST_CLASS_PREFIX) and not any(_binds(member, CONSTRUCTORS) for member in members))
    )
    return None if collected else UNCOLLECTED


# A suite's top-level classes by name, each with its position in the suite's body.
Classes = dict[str, tuple[int, ast.ClassDef]]


def _lineage(
    classes: Classes, aliases: dict[str, str], index: int, node: ast.ClassDef
) -> tuple[list[ast.ClassDef], bool] | None:
    """The suite's own classes in a class's lineage, itself first, and whether a unittest TestCase is among its bases;
    None when a class in it has a decorator or a keyword, or a base that is neither object, a TestCase, nor a top-level
    class the suite defines before it. Each base comes earlier in the body than the class it bases, so the walk ends."""
    if node.decorator_list or node.keywords:
        return None
    members = [node]
    test_case = False
    for base in node.bases:
        qualified = python_names.qualified_name(base, aliases)
        if qualified in TEST_CASES:
            test_case = True
            continue
        if qualified in OBJECT:
            continue
        found = classes.get(base.id) if isinstance(base, ast.Name) and base.id not in aliases else None
        if found is None or found[0] >= index:
            return None
        inherited = _lineage(classes, aliases, *found)
        if inherited is None:
            return None
        members += inherited[0]
        test_case = test_case or inherited[1]
    return members, test_case


def _binds(node: ast.ClassDef, names: frozenset[str]) -> bool:
    """Whether a class's own body binds any of names, by a method or an assignment."""
    for child in node.body:
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and child.name in names:
            return True
        targets = (
            child.targets
            if isinstance(child, ast.Assign)
            else [child.target]
            if isinstance(child, ast.AnnAssign)
            else []
        )
        if any(isinstance(target, ast.Name) and target.id in names for target in targets):
            return True
    return False

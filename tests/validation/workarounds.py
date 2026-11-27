"""The rule against working around a failing check: no test marked skipped or an expected failure, and no comment
that leaves work for later, in the Python under the format roots outside the fixture trees.

A skipped test and an expected failure both let a suite exit 0, which is all the pool judges a suite by, so either
hides a failing check. A test that cannot run on a platform is not a workaround: a unittest skipIf or skipUnless, or
a pytest.mark.skipif decorator, whose condition reads only the platform is left alone, as is a test a class body or a
module defines under an `if` on the platform.
Any other skip that must stay states its reason beside the code, in a module-level SKIP_ALLOWED keyed by the qualified
name of the function or class that holds it, `<module>` for module level; an entry that sanctions nothing fails.

A reader or a policy that leaves an entry of a language's published catalogue unread names it in a module-level
NOT_READ = {entry: reason} beside the code, which its catalogue test reads, as bash_syntax.py's reserved words; each
such table maps every entry to its reason, as SKIP_ALLOWED does, in that one literal: a later write to it, by another
assignment at module level, an augmented one, a subscript, a `del`, or a call of a method that changes a dictionary,
fails, since its entries would escape the reading. What it does not see: a write through another name bound to the
table, through globals(), vars(), or setattr, or through a call that takes the table as an argument, as
`dict.update(NOT_READ, ...)` or `operator.setitem(NOT_READ, ...)`; and a rebinding of the name by an import, a `def`,
a `class`, an `except ... as`, or a `match` capture. It takes a write to a class's own NOT_READ through an attribute,
as `self.NOT_READ[key] = reason`, for one to a module's table.

Such a module names each catalogue it decides, with the test that holds every entry of it, in a module-level
CATALOGUE_TESTS = {catalogue: "<suite path>::<test>"}, which ejryan89-skill-core's catalogue_tests.py reads: a module
that binds NOT_READ names at least one, each value names a test its tracked suite defines, a top-level function or a
method of a top-level class, and the table is held to its one literal as NOT_READ is. A module that decides a
catalogue under another table's name, as the hub guard's builtins, declares it too, and is held once it does. Which
entries a named test holds is review's judgment: the policy reads no test's body, so a test of another module passes.
A test counts only by a name starting with `test`, as pytest and unittest collect one. Stated limits: a suite under
tests/fixtures/, which no run collects, is still a tracked suite a reference may name; and a test defined under a
platform `if` is no top-level function or method, so it cannot be named.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

import catalogue_tests
import python_reader
from duplication import DUPLICATION_ROOTS
from path_containment import posix_within
from python_checks import FORMAT_ROOTS, ISSUE_REFERENCE_EXCLUDED, file_comments
from suite_names import is_suite_name
from validation_support import (
    MODULE_SCOPE,
    REPOSITORY_ROOT,
    import_aliases,
    is_test_script,
    module_allowance,
    python_trees,
    qualified_name,
    repository_files,
)

ALLOWANCE = "SKIP_ALLOWED"
# The table of the entries of a published catalogue a reader leaves unread, each with why. It stands beside the reader
# or the policy it excuses, in the repository's own Python but its suites and fixtures, DUPLICATION_ROOTS; a suite and
# a fault driver under tests/ name their own rows, which may share the name, as a fault driver's NOT_READ reason does.
NOT_READ_TABLE = "NOT_READ"
# The table in which such a module names each catalogue it decides with the test that holds it.
CATALOGUE_TESTS_TABLE = catalogue_tests.TABLE
# The methods of a dictionary that change nothing; a call of any other on the table is a write to it.
READING_METHODS = frozenset({"get", "keys", "values", "items", "copy", "__contains__", "__getitem__", "__len__"})
# What skips a test or expects it to fail, by the dotted name the module's imports resolve it to.
UNITTEST_MARKERS = frozenset({"skip", "skipIf", "skipUnless", "expectedFailure", "SkipTest"})
UNITTEST_MODULES = ("unittest", "unittest.case")
PYTEST_MARKERS = frozenset(
    {
        "pytest.skip",
        "pytest.xfail",
        "pytest.importorskip",
        "pytest.mark.skip",
        "pytest.mark.skipif",
        "pytest.mark.xfail",
    }
)
# A TestCase's own skip, on whatever names the case.
SKIP_METHOD = "skipTest"
# The skips whose condition may read the platform alone, as a decorator.
CONDITIONAL_SKIPS = frozenset(
    {
        *(f"{module}.{name}" for module in UNITTEST_MODULES for name in ("skipIf", "skipUnless")),
        "pytest.mark.skipif",
    }
)
# What reads the platform: these names, and a call of platform.system.
PLATFORM_NAMES = frozenset({"sys.platform", "os.name"})
PLATFORM_CALLS = frozenset({"platform.system"})
# A comment that leaves work for later. Upper case only, as a word, so a template such as tmpXXXXXX is not one.
LATER_COMMENT = re.compile(r"\b(?:TODO|FIXME|XXX)\b")


def marker_name(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """The skip or expected-failure marker a name or attribute stands for, or None."""
    if isinstance(node, ast.Attribute) and node.attr == SKIP_METHOD:
        return SKIP_METHOD
    if not isinstance(node, ast.Name | ast.Attribute):
        return None
    name = qualified_name(node, aliases)
    module, _, last = name.rpartition(".")
    if (module in UNITTEST_MODULES and last in UNITTEST_MARKERS) or name in PYTEST_MARKERS:
        return name
    return None


def reads_platform(node: ast.expr, aliases: dict[str, str]) -> bool:
    if isinstance(node, ast.Call):
        return not node.args and not node.keywords and qualified_name(node.func, aliases) in PLATFORM_CALLS
    return qualified_name(node, aliases) in PLATFORM_NAMES


def is_text(node: ast.expr) -> bool:
    """A string literal, or a literal tuple, list, or set of them."""
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return all(is_text(element) for element in node.elts)
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def platform_condition(node: ast.expr, aliases: dict[str, str]) -> bool:
    """Whether a condition reads the platform alone: a comparison of what reads it with text, its startswith or
    endswith, and and, or, and not of those."""
    if isinstance(node, ast.BoolOp):
        return all(platform_condition(value, aliases) for value in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return platform_condition(node.operand, aliases)
    if isinstance(node, ast.Compare):
        return (
            reads_platform(node.left, aliases)
            and all(isinstance(op, ast.Eq | ast.NotEq | ast.In | ast.NotIn) for op in node.ops)
            and all(is_text(comparator) for comparator in node.comparators)
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return (
            node.func.attr in ("startswith", "endswith")
            and reads_platform(node.func.value, aliases)
            and len(node.args) == 1
            and not node.keywords
            and is_text(node.args[0])
        )
    return False


def platform_skip(decorator: ast.expr, aliases: dict[str, str]) -> bool:
    """Whether a decorator skips a test on a platform it cannot run on, and nowhere else."""
    return (
        isinstance(decorator, ast.Call)
        and qualified_name(decorator.func, aliases) in CONDITIONAL_SKIPS
        and bool(decorator.args)
        and platform_condition(decorator.args[0], aliases)
    )


def markers(tree: ast.Module) -> list[tuple[int, str, str]]:
    """Each skip or expected-failure marker in a module, as its line, the qualified name of the function or class
    that holds it (a decorator belongs to what it decorates), and the marker."""
    aliases = import_aliases(tree)
    found: list[tuple[int, str, str]] = []

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            inner = (*scope, node.name)
            for decorator in node.decorator_list:
                if not platform_skip(decorator, aliases):
                    visit(decorator, inner)
            for statement in node.body:
                visit(statement, inner)
            for child in ast.iter_child_nodes(node):
                if not any(child is held for held in (*node.decorator_list, *node.body)):
                    visit(child, scope)
            return
        marker = marker_name(node, aliases)
        if marker:
            found.append((getattr(node, "lineno", 0), ".".join(scope) or MODULE_SCOPE, marker))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, ())
    return found


def checked(root: Path, path: Path) -> bool:
    """Whether the rule holds a file: Python under the format roots, outside the fixture trees."""
    name = path.relative_to(root).as_posix()
    return (
        path.suffix == ".py"
        and any(posix_within(name, prefix) for prefix in FORMAT_ROOTS)
        and not any(posix_within(name, prefix, strict=True) for prefix in ISSUE_REFERENCE_EXCLUDED)
    )


def skip_problems(root: Path, files: list[Path]) -> list[str]:
    """Each skip or expected-failure marker no SKIP_ALLOWED entry sanctions, each entry that sanctions nothing, and
    each SKIP_ALLOWED that is not a dictionary of reasons."""
    problems: list[str] = []
    paths = sorted((path for path in files if checked(root, path)), key=lambda path: path.relative_to(root).as_posix())
    for _, name, tree in python_trees(paths, root, problems):
        allowance = module_allowance(tree, ALLOWANCE)
        if allowance is None:
            problems.append(f"{name} {ALLOWANCE} must map each function or class to its reason")
            allowance = {}
        found = markers(tree)
        problems += [
            f"{name}:{line} marks {scope} with {marker}, so its suite passes without it; make it pass, or state "
            f"why beside the code in {ALLOWANCE}"
            for line, scope, marker in found
            if scope not in allowance
        ]
        held = {scope for _, scope, _ in found}
        problems += [
            f"{name} {ALLOWANCE} names {scope}, which holds no skip or expected failure; remove the entry"
            for scope in sorted(set(allowance) - held)
        ]
    return problems


def table_modules(root: Path, files: list[Path]) -> list[Path]:
    """The modules whose NOT_READ and CATALOGUE_TESTS validation reads: the repository's own Python under
    DUPLICATION_ROOTS but its suites and fixtures."""
    return sorted(
        (
            path
            for path in files
            if checked(root, path)
            and not is_test_script(path)
            and any(posix_within(path.relative_to(root).as_posix(), prefix) for prefix in DUPLICATION_ROOTS)
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def not_read_problems(root: Path, files: list[Path]) -> list[str]:
    """Each module-level NOT_READ of a reader or a policy that does not map each catalogue entry to its reason."""
    problems: list[str] = []
    for _, name, tree in python_trees(table_modules(root, files), root, problems):
        if module_allowance(tree, NOT_READ_TABLE) is None:
            problems.append(
                f"{name} {NOT_READ_TABLE} must map each catalogue entry the module leaves unread to its reason, as a "
                "literal dictionary of non-empty texts"
            )
        problems += [
            f"{name}:{line} writes {NOT_READ_TABLE} after the assignment validation reads its reasons from; add each "
            "entry to that literal instead"
            for line in table_writes(tree)
        ]
    return problems


def suite_defines(root: Path, suites: set[str], path: str, test: str) -> str | None:
    """Why a reference's suite does not define its test: the suite is no tracked suite, cannot be read, does not parse,
    or does not define the test, each named with its reason; None when it does."""
    if path not in suites:
        return "which is no tracked suite"
    try:
        text = (root / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return f"whose suite cannot be read ({error})"
    try:
        tree = python_reader.parse(text, filename=path)
    except python_reader.PythonRefused as error:
        return f"whose suite does not parse ({error})"
    return None if catalogue_tests.defines(tree, test) else "whose suite defines no test of that name"


def catalogue_test_problems(root: Path, files: list[Path]) -> list[str]:
    """Each module-level CATALOGUE_TESTS that is no literal dictionary of texts, names a test its tracked suite does not
    define, or is written after its assignment, and each module that binds NOT_READ but names no catalogue test."""
    problems: list[str] = []
    suites = {path.relative_to(root).as_posix() for path in files if path.suffix == ".py" and is_suite_name(path.name)}
    for _, name, tree in python_trees(table_modules(root, files), root, problems):
        declared = catalogue_tests.declared(tree)
        if declared is None:
            problems.append(
                f"{name} {CATALOGUE_TESTS_TABLE} must map each catalogue the module decides to the test that holds "
                f"it, as a literal dictionary of non-empty texts, each <suite path>{catalogue_tests.SEPARATOR}<test>"
            )
            declared = {}
        elif not declared and table_assignment(tree) is not None:
            problems.append(
                f"{name} binds {NOT_READ_TABLE} but names no catalogue test; map each catalogue it decides to the "
                f"test that holds it in {CATALOGUE_TESTS_TABLE}"
            )
        for catalogue, value in declared.items():
            found = catalogue_tests.reference(value)
            fault = (
                f"which is not <suite path>{catalogue_tests.SEPARATOR}<test>"
                if found is None
                else suite_defines(root, suites, *found)
            )
            if fault is not None:
                problems.append(
                    f"{name} {CATALOGUE_TESTS_TABLE} names {value} for {catalogue}, {fault}; name the test that "
                    "decides every entry of that catalogue"
                )
        problems += [
            f"{name}:{line} writes {CATALOGUE_TESTS_TABLE} after the assignment validation reads it from; add each "
            "entry to that literal instead"
            for line in table_writes(tree, CATALOGUE_TESTS_TABLE)
        ]
    return problems


def names_table(node: ast.expr, table: str = NOT_READ_TABLE) -> bool:
    """Whether an expression names a module's table, NOT_READ unless `table` names another: the name, or an attribute
    of that name on a module."""
    return (isinstance(node, ast.Name) and node.id == table) or (isinstance(node, ast.Attribute) and node.attr == table)


def table_assignment(tree: ast.Module, table: str = NOT_READ_TABLE) -> ast.stmt | None:
    """The module-level assignment module_allowance reads the table from: the first that binds the name."""
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == table for target in targets):
                return node
    return None


def binds_table_name(scope: ast.AST, table: str = NOT_READ_TABLE) -> bool:
    """Whether a function's or a class's own body binds the name, making it a local one there rather than the table: a
    parameter of any kind, or a name stored or deleted."""
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        if isinstance(node, ast.arg) and node.arg == table:
            return True
        if isinstance(node, ast.Name) and node.id == table and isinstance(node.ctx, ast.Store | ast.Del):
            return True
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            pending.extend(ast.iter_child_nodes(node))
    return False


def declares_table_global(scope: ast.AST, table: str = NOT_READ_TABLE) -> bool:
    return any(isinstance(node, ast.Global) and table in node.names for node in ast.walk(scope))


def written_expression(node: ast.AST) -> ast.expr | None:
    """What a node writes, where it may be the table: a name or attribute it binds or deletes, the container a
    subscript it stores or deletes indexes, or the dictionary a call of a method that changes one calls it on."""
    stored = isinstance(getattr(node, "ctx", None), ast.Store | ast.Del)
    if stored and isinstance(node, ast.Name | ast.Attribute):
        return node
    if stored and isinstance(node, ast.Subscript):
        return node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr not in READING_METHODS:
        return node.func.value
    return None


def table_writes(tree: ast.Module, name: str = NOT_READ_TABLE) -> list[int]:
    """The line of each write to a module's table, NOT_READ unless `name` names another, after the assignment its
    entries are read from: a binding of the name at module level, or in a function that declares it global; a
    subscript stored or deleted; and a call of a method that changes a dictionary. The bare name is the table wherever
    Python resolves it to the module's: not in a function or a class whose own body binds it without declaring it
    global."""
    first = table_assignment(tree, name)
    held = set() if first is None else {id(target) for target in ast.walk(first)}
    lines: list[int] = []

    def table(node: ast.expr, table_here: bool) -> bool:
        return names_table(node, name) and (table_here or isinstance(node, ast.Attribute))

    def visit(node: ast.AST, table_here: bool, functions_inherit: bool) -> None:
        # A class's own names are not seen from the functions it defines, so a method resolves the bare name as the
        # scope around the class does: functions_inherit carries that past a class body.
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            inner = declares_table_global(node, name) or (functions_inherit and not binds_table_name(node, name))
            for child in ast.iter_child_nodes(node):
                visit(child, inner, inner)
            return
        if isinstance(node, ast.ClassDef):
            inner = declares_table_global(node, name) or (table_here and not binds_table_name(node, name))
            for child in ast.iter_child_nodes(node):
                visit(child, inner, functions_inherit)
            return
        written = written_expression(node)
        if id(node) not in held and written is not None and table(written, table_here):
            lines.append(written.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, table_here, functions_inherit)

    visit(tree, True, True)
    return sorted(set(lines))


def later_comment_problems(root: Path, files: list[Path]) -> list[str]:
    """Each comment that leaves work for later, as path:line and the word."""
    problems: list[str] = []
    for path in sorted(files, key=lambda path: path.relative_to(root).as_posix()):
        if not checked(root, path):
            continue
        name = path.relative_to(root).as_posix()
        problems += [
            f"{name}:{token.start[0]} leaves {match.group(0)} in a comment; finish the work in this change, or file "
            "it as an issue and leave no comment"
            for token in file_comments(root, path, problems)
            if (match := LATER_COMMENT.search(token.string))
        ]
    return problems


class WorkaroundPolicies(unittest.TestCase):
    def test_repository_skips_no_test_and_expects_no_failure(self) -> None:
        self.assertEqual([], skip_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_repository_gives_every_catalogue_entry_left_unread_its_reason(self) -> None:
        self.assertEqual([], not_read_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_repository_names_a_test_its_suite_defines_for_every_catalogue_a_module_decides(self) -> None:
        self.assertEqual([], catalogue_test_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_repository_leaves_no_work_for_later_in_a_comment(self) -> None:
        self.assertEqual([], later_comment_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

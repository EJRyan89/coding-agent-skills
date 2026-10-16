"""The rule against working around a failing check: no test marked skipped or an expected failure, and no comment
that leaves work for later, in the Python under the format roots outside the fixture trees.

A skipped test and an expected failure both let a suite exit 0, which is all the pool judges a suite by, so either
hides a failing check. A test that cannot run on a platform is not a workaround: a unittest skipIf or skipUnless
whose condition reads only the platform is left alone, as is a test a class body defines under an `if` on the platform.
Any other skip that must stay states its reason beside the code, in a module-level SKIP_ALLOWED keyed by the qualified
name of the function or class that holds it, `<module>` for module level; an entry that sanctions nothing fails.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from python_checks import FORMAT_ROOTS, ISSUE_REFERENCE_EXCLUDED, file_comments
from validation_support import (
    REPOSITORY_ROOT,
    import_aliases,
    module_allowance,
    python_trees,
    qualified_name,
    repository_files,
)

ALLOWANCE = "SKIP_ALLOWED"
MODULE_SCOPE = "<module>"
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
# The skips whose condition may read the platform alone.
CONDITIONAL_SKIPS = frozenset(f"{module}.{name}" for module in UNITTEST_MODULES for name in ("skipIf", "skipUnless"))
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
        and any(name == prefix or name.startswith(f"{prefix}/") for prefix in FORMAT_ROOTS)
        and not any(name.startswith(f"{prefix}/") for prefix in ISSUE_REFERENCE_EXCLUDED)
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

    def test_repository_leaves_no_work_for_later_in_a_comment(self) -> None:
        self.assertEqual([], later_comment_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

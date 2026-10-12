"""No definition is copied between files, and shared code lives in skill-core and is imported from there."""

from __future__ import annotations

import ast
import re
import sys
import unittest
from collections.abc import Iterator
from pathlib import Path

from validation_support import (
    REPOSITORY_ROOT,
    CallableResolver,
    is_test_script,
    module_allowance,
    module_imports,
    scripts_put_on_path,
)

# A module sanctions a copy beside the code it excuses, as a module-level DUPLICATION_ALLOWED = {name: reason}, and
# every module holding the copy states it under its own name for the copy. Nothing sanctions a whole module: shared
# code lives in skill-core. An allowance the module no longer needs is reported.
DUPLICATION_ALLOWANCE = "DUPLICATION_ALLOWED"
DUPLICATION_ROOTS = ("skills", "deployer", "tools")
MINIMUM_DUPLICATED_LINES = 4
# A string literal shorter than this is a common word or file name, such as "unknown" or "run.json", that two modules
# may hold for separate decisions; a collection of literals is held to it by the characters its items hold together.
# A compiled pattern has no floor, because any pattern states a format.
MINIMUM_CONSTANT_LENGTH = 12
# __all__ lists a module's own exports, and another module may export the same names.
UNCOMPARED_CONSTANTS = frozenset({"__all__"})
# A sanction dictionary, such as PLATFORM_ALLOWED or DUPLICATION_ALLOWED itself, names what its module excuses, and
# another module may excuse the same thing.
SANCTION_SUFFIX = "_ALLOWED"
# A constant's group key starts with this, so a constant and a definition never share a group.
CONSTANT_KEY = "constant:"
# The calls that build a collection from a literal. A collection compares by its members in any order, so a tuple and a
# set of one vocabulary, such as the severities a record allows, are one decision however each module holds it.
COLLECTION_CALLS = frozenset({"tuple", "list", "set", "frozenset"})

Copy = tuple[str, int, str]


def _definition_key(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    """The definition's syntax tree without docstrings or line numbers, equal for two copies of one body."""
    for child in ast.walk(node):
        if (
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and isinstance(child.body[0], ast.Expr)
            and isinstance(child.body[0].value, ast.Constant)
            and isinstance(child.body[0].value.value, str)
        ):
            child.body = child.body[1:] or [ast.Pass()]
    return ast.dump(node)


def _blocks(node: ast.stmt) -> list[list[ast.stmt]]:
    """The blocks of a module-level statement that also run at module level: an if's, a try's, and a with's."""
    if isinstance(node, ast.If):
        return [node.body, node.orelse]
    if isinstance(node, ast.With):
        return [node.body]
    if isinstance(node, (ast.Try, ast.TryStar)):
        return [node.body, *(handler.body for handler in node.handlers), node.orelse, node.finalbody]
    return []


def _module_statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Each statement that runs at module level, including one in a module-level if, try, or with block, which binds
    its names in the module as a top-level statement does."""
    for node in body:
        yield node
        for block in _blocks(node):
            yield from _module_statements(block)


def _argument(call: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(call.args) > position:
        return call.args[position]
    return next((found.value for found in call.keywords if found.arg == keyword), None)


def _flags(node: ast.expr | None, resolver: CallableResolver) -> int | str:
    """A pattern's flags by value, so re.I and re.IGNORECASE, in either order, compare equal; a flag the policy
    cannot read compares by its syntax."""
    if node is None:
        return 0
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left, right = _flags(node.left, resolver), _flags(node.right, resolver)
        if isinstance(left, int) and isinstance(right, int):
            return left | right
    name = resolver.name(node)
    flag = getattr(re, name.removeprefix("re."), None) if name.startswith("re.") else None
    return int(flag) if isinstance(flag, re.RegexFlag) else ast.dump(node)


def _literals(node: ast.expr) -> list[str | bytes] | None:
    """The items of a tuple, list, or set literal that holds only string or bytes literals, or None."""
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return None
    items: list[str | bytes] = [
        item.value for item in node.elts if isinstance(item, ast.Constant) and isinstance(item.value, (str, bytes))
    ]
    return items if len(items) == len(node.elts) else None


def _constant_key(value: ast.expr, resolver: CallableResolver) -> str | None:
    """The group key of a module-level value the policy compares, or None for any other value: a long string or bytes
    literal, a pattern compiled from a literal, or a collection of literals that together are long."""
    if isinstance(value, ast.Constant) and isinstance(value.value, (str, bytes)):
        return f"{CONSTANT_KEY}{value.value!r}" if len(value.value) >= MINIMUM_CONSTANT_LENGTH else None
    called = resolver.name(value.func) if isinstance(value, ast.Call) else ""
    if isinstance(value, ast.Call) and called == "re.compile":
        pattern = _argument(value, 0, "pattern")
        if not isinstance(pattern, ast.Constant) or not isinstance(pattern.value, (str, bytes)):
            return None
        return f"{CONSTANT_KEY}re.compile{(pattern.value, _flags(_argument(value, 1, 'flags'), resolver))!r}"
    if isinstance(value, ast.Call) and called in COLLECTION_CALLS and len(value.args) == 1 and not value.keywords:
        value = value.args[0]
    items = _literals(value)
    if not items or sum(len(item) for item in items) < MINIMUM_CONSTANT_LENGTH:
        return None
    return f"{CONSTANT_KEY}{tuple(sorted(set(items), key=repr))!r}"


def _module_copies(name: str, tree: ast.Module) -> Iterator[tuple[str, Copy]]:
    """Each definition and constant the module may share with another, as its group key and where it is."""
    resolver = CallableResolver(tree)
    for node in _module_statements(tree.body):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and (node.end_lineno or node.lineno) - node.lineno + 1 >= MINIMUM_DUPLICATED_LINES
        ):
            yield _definition_key(node), (name, node.lineno, node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = [
                target.id
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                if isinstance(target, ast.Name)
                and target.id not in UNCOMPARED_CONSTANTS
                and not target.id.endswith(SANCTION_SUFFIX)
            ]
            key = _constant_key(node.value, resolver) if targets else None
            if key is not None:
                yield from ((key, (name, node.lineno, target)) for target in targets)


def _joined(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _copy_report(key: str, group: list[Copy]) -> str:
    fix = f"import one copy, or sanction it in each copy's module with {DUPLICATION_ALLOWANCE}"
    if key.startswith(CONSTANT_KEY):
        return f"{_joined([f'{copy} at {module}:{line}' for module, line, copy in group])} hold the same value; {fix}"
    return (
        f"{group[0][2]} is defined identically in {_joined([f'{module}:{line}' for module, line, _ in group])}; {fix}"
    )


def duplicated_definition_problems(root: Path) -> list[str]:
    """Report a module-level function or class defined identically in two files, a module-level literal or compiled
    pattern assigned identically in two files, and allowances no longer needed.

    Copied helpers began identical and then drifted, and so did copied patterns; a copy caught while it is still
    identical is caught before it diverges. Two scripts of one skill count, because one can import the other.
    """
    copies: dict[str, list[Copy]] = {}
    allowances: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    files = [path for top in DUPLICATION_ROOTS for path in (root / top).rglob("*.py") if not is_test_script(path)]
    for path in sorted(files, key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = module_allowance(tree, DUPLICATION_ALLOWANCE)
        if allowed is None:
            problems.append(f"{name}: {DUPLICATION_ALLOWANCE} must map each name to the reason it is allowed")
            allowed = {}
        allowances[name] = allowed
        for key, place in _module_copies(name, tree):
            copies.setdefault(key, []).append(place)
    found: list[str] = []
    copied: dict[str, set[str]] = {}
    for key, group in copies.items():
        modules = sorted({module for module, _, _ in group})
        if len(modules) < 2:
            continue
        # Each module sanctions the copy under its own name for it, which a constant's copies need not share.
        held = sorted({(module, copy) for module, _, copy in group})
        for module, copy in held:
            copied.setdefault(module, set()).add(copy)
        if all(copy in allowances[module] for module, copy in held):
            # Each reason names the other copies, so a reader of one copy finds every place a change must reach.
            problems += [
                f"{module}: {DUPLICATION_ALLOWANCE} gives a reason for {copy} that does not name "
                f"every other copy: {_joined(missing)}"
                for module, copy in held
                if (
                    missing := [other for other in modules if other != module and other not in allowances[module][copy]]
                )
            ]
            continue
        found.append(_copy_report(key, group))
    for module, allowed in allowances.items():
        problems += [
            f"{module}: {DUPLICATION_ALLOWANCE} allows {entry}, which no other file defines identically"
            for entry in sorted(allowed)
            if entry not in copied.get(module, set())
        ]
    return sorted(found) + sorted(problems)


# skills/skill-core/scripts is the one home of the repository's shared modules: skill scripts, the deployer, and
# tools/ import them from there. deployer/__init__.py puts the directory on sys.path for every deployer module, since a
# package runs its __init__.py before any of its modules; a tools module states the same path statement itself, above
# its imports, because ruff sorts a skill-core import above the deployer's.
SKILL_CORE = "skill-core"
SKILL_CORE_SCRIPTS = f"skills/{SKILL_CORE}/scripts"
SKILL_CORE_DOC = '"Validation" in docs/adding-a-skill.md'
DEPLOYER_PACKAGE = "deployer/__init__.py"


def skill_core_modules(root: Path) -> set[str]:
    """The names of skill-core's shared modules, which skill scripts, the deployer, and tools/ import."""
    return {path.stem for path in (root / SKILL_CORE_SCRIPTS).glob("*.py") if not is_test_script(path)}


def skill_core_copy_problems(root: Path) -> list[str]:
    """Report a module outside skill-core with the name of a skill-core module: another copy of shared code.

    A renamed copy is caught by the duplication policy, which sanctions no whole module, and a same-named one here
    even after it drifts, as the frontmatter reader's deployer copy would have.
    """
    shared = skill_core_modules(root)
    problems: list[str] = []
    for top in DUPLICATION_ROOTS:
        for path in sorted((root / top).rglob("*.py")):
            name = path.relative_to(root).as_posix()
            if path.stem in shared and not is_test_script(path) and not name.startswith(f"{SKILL_CORE_SCRIPTS}/"):
                problems.append(
                    f"{name} has the name of {SKILL_CORE_SCRIPTS}/{path.stem}.py; import {path.stem} from "
                    f"{SKILL_CORE} instead of keeping another copy; see {SKILL_CORE_DOC}"
                )
    return sorted(problems)


def _skill_core_statement_lines(tree: ast.Module) -> list[int]:
    """Lines of the module-level statements that put skill-core's scripts first on sys.path, located from __file__."""
    lines: list[int] = []
    for node in tree.body:
        call = node.value if isinstance(node, ast.Expr) else None
        if (
            isinstance(call, ast.Call)
            and ast.unparse(call.func) == "sys.path.insert"
            and len(call.args) == 2
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == 0
            and "__file__" in ast.unparse(call.args[1])
            and SKILL_CORE in scripts_put_on_path(ast.unparse(call))
        ):
            lines.append(node.lineno)
    return lines


def _imported_modules(tree: ast.Module) -> list[tuple[int, str]]:
    """Each absolute import in the module, as its line and the dotted module name it imports."""
    return sorted({(found.line, found.module) for found in module_imports(tree)})


def skill_core_import_problems(root: Path) -> list[str]:
    """Report a deployer or tools module that imports skill-core before putting it on sys.path, and an import that
    breaks the direction or the standard-library rule.

    deployer/__init__.py holds the deployer's one path statement, located from its own file and never through the
    configured source path; a tools module states it above its skill-core imports. Skill-core imports nothing from the
    deployer or tools/, so a deployed skill never needs the checkout, and the deployer and skill-core import only the
    standard library, the deployer, and skill-core.
    """
    shared = skill_core_modules(root)
    problems: list[str] = []
    package = root / DEPLOYER_PACKAGE
    package_lines = (
        _skill_core_statement_lines(ast.parse(package.read_text(encoding="utf-8"))) if package.is_file() else []
    )
    if not package_lines:
        problems.append(
            f"{DEPLOYER_PACKAGE} does not put {SKILL_CORE_SCRIPTS} first on sys.path, located from its own file; "
            f"see {SKILL_CORE_DOC}"
        )
    allowed = set(sys.stdlib_module_names) | shared | {"deployer"}
    for top in ("deployer", "tools", SKILL_CORE_SCRIPTS):
        for path in sorted((root / top).rglob("*.py")):
            name = path.relative_to(root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            statements = _skill_core_statement_lines(tree)
            if top == "deployer" and name != DEPLOYER_PACKAGE:
                problems += [
                    f"{name}:{line} puts {SKILL_CORE} on sys.path; {DEPLOYER_PACKAGE} is the deployer's one place; "
                    f"see {SKILL_CORE_DOC}"
                    for line in statements
                ]
                statements = [0] if package_lines else []
            for line, module in _imported_modules(tree):
                first = module.split(".")[0]
                if top == SKILL_CORE_SCRIPTS and first in {"deployer", "tools"}:
                    problems.append(
                        f"{name}:{line} imports {module}; {SKILL_CORE} imports nothing from the deployer or tools/; "
                        f"see {SKILL_CORE_DOC}"
                    )
                elif top != "tools" and not is_test_script(path) and first not in allowed:
                    problems.append(
                        f"{name}:{line} imports {module}, which is neither the standard library, the deployer, nor "
                        f"{SKILL_CORE}; see {SKILL_CORE_DOC}"
                    )
                elif top != SKILL_CORE_SCRIPTS and first in shared and not any(at < line for at in statements):
                    problems.append(
                        f"{name}:{line} imports {module} before putting {SKILL_CORE_SCRIPTS} on sys.path; see "
                        f"{SKILL_CORE_DOC}"
                    )
    return sorted(problems)


class DuplicationPolicies(unittest.TestCase):
    def test_repository_copies_only_what_it_sanctions(self) -> None:
        # Copied helpers began identical and then drifted, so a copy is caught while it is still identical.
        self.assertEqual([], duplicated_definition_problems(REPOSITORY_ROOT))

    def test_repository_keeps_shared_modules_in_skill_core(self) -> None:
        self.assertEqual([], skill_core_copy_problems(REPOSITORY_ROOT))
        self.assertEqual([], skill_core_import_problems(REPOSITORY_ROOT))

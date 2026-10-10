"""No definition is copied between files, and shared code lives in skill-core and is imported from there."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

from fsops_platform import _platform_allowance
from validation_support import REPOSITORY_ROOT, is_test_script, module_imports, scripts_put_on_path

# A module sanctions a copy beside the code it excuses, as a module-level DUPLICATION_ALLOWED = {name: reason}, and
# every module holding the copy states it. Nothing sanctions a whole module: shared code lives in skill-core. An
# allowance the module no longer needs is reported.
DUPLICATION_ALLOWANCE = "DUPLICATION_ALLOWED"
DUPLICATION_ROOTS = ("skills", "deployer", "tools")
MINIMUM_DUPLICATED_LINES = 4


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


def _joined(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def duplicated_definition_problems(root: Path) -> list[str]:
    """Report a top-level function or class defined identically in two files, and allowances no longer needed.

    Copied helpers began identical and then drifted; a copy caught while it is still identical is caught before
    it diverges. Two scripts of one skill count, because one can import the other.
    """
    copies: dict[str, list[tuple[str, int, str]]] = {}
    allowances: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    files = [path for top in DUPLICATION_ROOTS for path in (root / top).rglob("*.py") if not is_test_script(path)]
    for path in sorted(files, key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = _platform_allowance(tree, DUPLICATION_ALLOWANCE)
        if allowed is None:
            problems.append(f"{name}: {DUPLICATION_ALLOWANCE} must map each name to the reason it is allowed")
            allowed = {}
        allowances[name] = allowed
        for node in tree.body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and (node.end_lineno or node.lineno) - node.lineno + 1 >= MINIMUM_DUPLICATED_LINES
            ):
                copies.setdefault(_definition_key(node), []).append((name, node.lineno, node.name))
    found: list[str] = []
    copied: dict[str, set[str]] = {}
    for group in copies.values():
        modules = sorted({module for module, _, _ in group})
        if len(modules) < 2:
            continue
        definition = group[0][2]
        for module in modules:
            copied.setdefault(module, set()).add(definition)
        if all(definition in allowances[module] for module in modules):
            # Each reason names the other copies, so a reader of one copy finds every place a change must reach.
            problems += [
                f"{module}: {DUPLICATION_ALLOWANCE} gives a reason for {definition} that does not name "
                f"every other copy: {_joined(missing)}"
                for module in modules
                if (
                    missing := [
                        other for other in modules if other != module and other not in allowances[module][definition]
                    ]
                )
            ]
            continue
        places = _joined([f"{module}:{line}" for module, line, _ in group])
        found.append(
            f"{definition} is defined identically in {places}; import one copy, or sanction it in each copy's module "
            f"with {DUPLICATION_ALLOWANCE}"
        )
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

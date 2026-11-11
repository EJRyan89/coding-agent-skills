"""Every deployer refusal ends with the step to take, on a line of its own, except where its module says why not
beside the code.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

import file_presence
from validation_support import (
    REPOSITORY_ROOT,
    import_aliases,
    module_allowance,
    python_trees,
    qualified_name,
    relative_import_aliases,
    scoped_nodes,
    statement_scope,
)

# The deployer's refusal. A class the deployer derives from it is a refusal too, found by its bases.
REFUSAL = "deployer.errors.DeployError"
# A refusal whose one line is the whole report, written out here rather than read from the deployer, so a change there
# cannot also change what this policy looks for: a cancellation is the user's own act, which needs no step.
STEPLESS = {"deployer.errors.Cancelled": "a cancellation is the user's own act, so it asks nothing of them"}
# A module states why a refusal ends without a step beside the code, as a module-level STEP_ALLOWED = {scope: reason},
# where a scope is a function's qualified name, such as "Journal.append", or a module-level assignment's target: a
# refusal its every caller adds the step to, or one caught beside it that never reaches the user.
STEP_ALLOWANCE = "STEP_ALLOWED"


def _scanned_files(root: Path) -> list[Path]:
    """The deployer's code: deploy.py and every module of the deployer package."""
    files = [root / "deploy.py", *file_presence.matching(root / "deployer", "**/*.py")]
    return [path for path in files if file_presence.is_present(path, other_is_absent=True)]


def _aliases(tree: ast.Module, name: str) -> dict[str, str]:
    """What each name a module binds by import stands for, its relative imports included."""
    package = ".".join(Path(name).parent.parts)
    return {**import_aliases(tree), **relative_import_aliases(tree, package)}


def _module_name(name: str) -> str:
    """A file's dotted module name, such as deployer.errors for deployer/errors.py."""
    return ".".join(Path(name).with_suffix("").parts)


def refusal_classes(trees: list[tuple[str, ast.Module]]) -> set[str]:
    """DeployError and every class the scanned modules derive from it, directly or through another, by dotted name."""
    bases: dict[str, set[str]] = {}
    for name, tree in trees:
        aliases = _aliases(tree, name)
        module = _module_name(name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                found = {qualified_name(base, aliases) or "" for base in node.bases}
                # A base named in its own module, such as DeployError in deployer/errors.py, stands for that module's.
                bases[f"{module}.{node.name}"] = {base if "." in base else f"{module}.{base}" for base in found if base}
    refusals = {REFUSAL}
    grown = True
    while grown:
        derived = {name for name, parents in bases.items() if parents & refusals}
        grown = not derived <= refusals
        refusals |= derived
    return refusals


def _ends_with_a_step(call: ast.Call) -> bool:
    """Whether a refusal's lines can hold a step after its first: a second positional argument, or an unpacking that
    may give one, such as `*lines`."""
    return len(call.args) > 1 or any(isinstance(argument, ast.Starred) for argument in call.args)


def stepless_refusals(tree: ast.Module, name: str, refusals: set[str]) -> list[tuple[int, str, str]]:
    """Each refusal a module builds from one line, as (line, scope, class), ignoring its allowance."""
    aliases = _aliases(tree, name)
    module = _module_name(name)
    statements = [
        statement
        for statement in tree.body
        if not (isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement_scope(statement) == STEP_ALLOWANCE)
    ]
    found: list[tuple[int, str, str]] = []
    for node, scope in scoped_nodes(statements):
        if not isinstance(node, ast.Call):
            continue
        called = qualified_name(node.func, aliases)
        if called and "." not in called:
            called = f"{module}.{called}"
        if called in refusals and called not in STEPLESS and not _ends_with_a_step(node):
            found.append((node.lineno, scope, called.rsplit(".", 1)[-1]))
    return found


def refusal_step_problems(root: Path) -> list[str]:
    """Report each deployer refusal built from one line, which ends at its ERROR line, without stating why in its
    module's STEP_ALLOWED, and each allowance no longer needed.

    A refusal that names what is wrong and not what to do leaves its reader stuck, so each one ends with the step to
    take on a line of its own, or its module says why it need not: every caller adds the step, or it is caught beside
    it and never reaches the user.
    """
    problems: list[str] = []
    ordered = sorted(_scanned_files(root), key=lambda path: path.relative_to(root).as_posix())
    trees = [(name, tree) for _, name, tree in python_trees(ordered, root, problems)]
    refusals = refusal_classes(trees)
    found: list[str] = []
    for name, tree in trees:
        allowed = module_allowance(tree, STEP_ALLOWANCE)
        if allowed is None:
            problems.append(f"{name}: {STEP_ALLOWANCE} must map each scope to the reason its refusals need no step")
            allowed = {}
        findings = stepless_refusals(tree, name, refusals)
        found += [
            f"{name}:{line} {called} in {scope} ends at its ERROR line; add the step to take as its "
            f"next line, or state why it needs none in {STEP_ALLOWANCE}"
            for line, scope, called in findings
            if scope not in allowed
        ]
        problems += [
            f"{name}: {STEP_ALLOWANCE} allows {scope}, which no longer builds a refusal from one line"
            for scope in sorted(set(allowed) - {scope for _, scope, _ in findings})
        ]
    return found + problems


class RefusalStepPolicies(unittest.TestCase):
    def test_every_deployer_refusal_ends_with_a_step(self) -> None:
        # A refusal added without the step to take fails here, or its module says why it needs none.
        self.assertEqual([], refusal_step_problems(REPOSITORY_ROOT))

"""No definition is copied between files, and shared code lives in ejryan89-skill-core and is imported from there:
among it the one containment test of a path in a tree.
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePath

from path_containment import posix_within
from python_checks import claim_files, formatted_modules, sanctioned_problems
from skill_roots import SKILL_CORE
from validation_support import (
    MODULE_LOADERS,
    REPOSITORY_ROOT,
    VALIDATION_DOC,
    CallableResolver,
    call_argument,
    import_aliases,
    is_test_script,
    listed,
    module_allowance,
    module_imports,
    module_statements,
    python_trees,
    qualified_name,
    repository_files,
    scope_problems,
    scoped_nodes,
    scripts_put_on_path,
    skill_dependency_closure,
)

# A module sanctions a copy beside the code it excuses, as a module-level DUPLICATION_ALLOWED = {name: reason}, and
# every module holding the copy states it under its own name for the copy. Nothing sanctions a whole module: shared
# code lives in ejryan89-skill-core. An allowance the module no longer needs is reported.
DUPLICATION_ALLOWANCE = "DUPLICATION_ALLOWED"
# Every tree of the repository's own Python but its suites and fixtures: the shipped skills, the deployer, the tools,
# the repository skills, which meet the shipped-skill standard, and the policies, which share what they repeat through
# validation_support.
DUPLICATION_ROOTS = ("skills", "deployer", "tools", ".claude/skills", "tests/validation")
MINIMUM_DUPLICATED_LINES = 4
# A string literal shorter than this is a common word or file name, such as "unknown" or "run.json", that two modules
# may hold for separate decisions; a collection of literals is held to it by the characters its items hold together.
# A compiled pattern has no floor, because any pattern states a format.
MINIMUM_CONSTANT_LENGTH = 12
# __all__ lists a module's own exports, and another module may export the same names.
UNCOMPARED_CONSTANTS = frozenset({"__all__"})
# A sanction dictionary, such as PLATFORM_ALLOWED or DUPLICATION_ALLOWED itself, names what its module excuses, and
# another module may excuse the same thing. Only a dictionary under such a name is one; any other value is compared.
SANCTION_SUFFIX = "_ALLOWED"
# A constant's group key starts with this, so a constant and a definition never share a group.
CONSTANT_KEY = "constant:"
# The calls that build a collection from a literal. A collection compares by its members in any order, so a tuple and a
# set of one vocabulary, such as the severities a record allows, are one decision however each module holds it.
COLLECTION_CALLS = frozenset({"tuple", "list", "set", "frozenset"})

# A restatement of a shared fact's vocabulary holds at least this many of its members: fewer is a command that passes
# one of them, such as a git invocation with -C.
MINIMUM_SHARED_MEMBERS = 3

Copy = tuple[str, int, str]
# A shared fact's restatement test: the node, the fact's members as its owner holds them, whether the node's module
# imports the owner, and the fact's name, which a module that imports it reads the fact through.
Restates = Callable[[ast.AST, frozenset[str], bool, str], bool]


def duplication_omissions() -> dict[str, str]:
    """The parts of the formatted Python the duplication policy leaves out, each with its reason."""
    reason = (
        "outside the trees the rule names: the suites and their support state what they test as literals of their "
        "own, and the root modules share nothing a tree imports"
    )
    return {"tests": reason, "deploy.py": reason, "conftest.py": reason}


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
        pattern = call_argument(value, 0, "pattern")
        if not isinstance(pattern, ast.Constant) or not isinstance(pattern.value, (str, bytes)):
            return None
        return f"{CONSTANT_KEY}re.compile{(pattern.value, _flags(call_argument(value, 1, 'flags'), resolver))!r}"
    if isinstance(value, ast.Call) and called in COLLECTION_CALLS and len(value.args) == 1 and not value.keywords:
        value = value.args[0]
    items = _literals(value)
    if not items or sum(len(item) for item in items) < MINIMUM_CONSTANT_LENGTH:
        return None
    return f"{CONSTANT_KEY}{tuple(sorted(set(items), key=repr))!r}"


def _module_copies(name: str, tree: ast.Module) -> Iterator[tuple[str, Copy]]:
    """Each definition and constant the module may share with another, as its group key and where it is."""
    resolver = CallableResolver(tree)
    for node in module_statements(tree.body):
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
                and not (target.id.endswith(SANCTION_SUFFIX) and isinstance(node.value, ast.Dict))
            ]
            key = _constant_key(node.value, resolver) if targets else None
            if key is not None:
                yield from ((key, (name, node.lineno, target)) for target in targets)


def _copy_report(key: str, group: list[Copy]) -> str:
    fix = f"import one copy, or sanction it in each copy's module with {DUPLICATION_ALLOWANCE}"
    if key.startswith(CONSTANT_KEY):
        held = listed([f"{copy} at {module}:{line}" for module, line, copy in group], "and")
        return f"{held} hold the same value; {fix}"
    places = listed([f"{module}:{line}" for module, line, _ in group], "and")
    return f"{group[0][2]} is defined identically in {places}; {fix}"


def _shares_members(node: ast.AST, members: frozenset[str], _imports_owner: bool, _name: str) -> bool:
    """A tuple, list, or set literal holding several of the fact's members: a copy of its vocabulary, however far it
    has drifted from the owner's."""
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return False
    held = {item.value for item in node.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)}
    return len(held & members) >= MINIMUM_SHARED_MEMBERS


def _holds_member(node: ast.AST, members: frozenset[str], _imports_owner: bool, _name: str) -> bool:
    """A string literal holding one of the fact's members, as a glob or a message re-derives a rule from it."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(member in node.value for member in members)
    )


def _equals_in_importer(node: ast.AST, members: frozenset[str], imports_owner: bool, _name: str) -> bool:
    """The fact's own value as a literal in a module that imports its owner, which could import the value too. A
    short value such as a file name recurs for unrelated reasons elsewhere, so only an importer is read."""
    return imports_owner and isinstance(node, ast.Constant) and node.value in members


def _states_number(node: ast.AST, members: frozenset[str], imports_owner: bool, _name: str) -> bool:
    """A limit's own number as a literal in a module that imports its owner, or stated as "at most" the number in any
    text, as a refusal's message re-derives it."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return imports_owner and str(node.value) in members
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(f"at most {member}" in node.value for member in members)
    )


def _names_key(node: ast.AST, members: frozenset[str], name: str) -> bool:
    """The key as a literal, or the owner's name for it, as a module reads it."""
    if isinstance(node, ast.Constant):
        return node.value in members
    return (isinstance(node, ast.Name) and node.id == name) or (isinstance(node, ast.Attribute) and node.attr == name)


def _reads_key(node: ast.AST, members: frozenset[str], _imports_owner: bool, name: str) -> bool:
    """A reading of a key outside its owner's: the key's literal passed to a call, as a reader looks it up, or the key
    compared, after any call, with true or false, as a reader judges its value."""
    if isinstance(node, ast.Call):
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        return any(isinstance(argument, ast.Constant) and argument.value in members for argument in arguments)
    if not isinstance(node, ast.Compare):
        return False
    operands = [node.left, *node.comparators]
    judged = any(
        isinstance(operand, ast.Constant)
        and isinstance(operand.value, str)
        and operand.value.strip().casefold() in {"true", "false"}
        for operand in operands
    )
    return judged and any(_names_key(inner, members, name) for operand in operands for inner in ast.walk(operand))


def _patterns_member(node: ast.AST, members: frozenset[str], _imports_owner: bool, _name: str) -> bool:
    """A string literal holding a member as a regular expression escapes it, as a pattern re-derives a spelling; a
    member that needs no escape reads alike in prose and is left to the prose."""
    escaped = {re.escape(member) for member in members} - members
    return (
        isinstance(node, ast.Constant) and isinstance(node.value, str) and any(form in node.value for form in escaped)
    )


def _reads_title_or_body(node: ast.AST, members: frozenset[str], _imports_owner: bool, _name: str) -> bool:
    """A pull request title or body file read with a codec of its own: read_text or open given an encoding other than
    the owner's, on a path whose expression names a title or a body."""
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute) and node.func.attr == "read_text":
        path, encoding = node.func.value, call_argument(node, 0, "encoding")
    elif isinstance(node.func, ast.Name) and node.func.id == "open" and node.args:
        path, encoding = node.args[0], call_argument(node, 2, "encoding")
    else:
        return False
    named = any(word in ast.unparse(path).casefold() for word in ("title", "body"))
    return named and isinstance(encoding, ast.Constant) and encoding.value not in members


@dataclass(frozen=True)
class SharedFact:
    """One value the repository keeps in one module, its owner, under one module-level name, and how another module
    restates it instead of importing it."""

    owner: str
    name: str
    restates: Restates


def _shared_facts() -> tuple[SharedFact, ...]:
    """The facts a diverged or re-derived copy was found of. The gate above compares copies by value, so a copy that
    has since drifted from its owner's, or a value re-derived from the same rule, is outside its reach; each fact here
    is read from its owner, never restated, and a second literal copy of it fails."""
    return (
        SharedFact(f"{SKILL_CORE_SCRIPTS}/git_client.py", "GLOBAL_VALUE_OPTIONS", _shares_members),
        SharedFact(f"{SKILL_CORE_SCRIPTS}/suite_names.py", "SUITE_NAME_PATTERNS", _holds_member),
        SharedFact(
            "skills/ejryan89-code-review-core/scripts/review_config.py", "DEFAULT_CONFIG_NAME", _equals_in_importer
        ),
        SharedFact(f"{SKILL_CORE_SCRIPTS}/agent_skills.py", "MAX_NAME", _states_number),
        SharedFact(f"{SKILL_CORE_SCRIPTS}/frontmatter.py", "USER_ONLY_KEY", _reads_key),
        SharedFact(f"{SKILL_CORE_SCRIPTS}/typed_text.py", "TYPED_ENCODING", _reads_title_or_body),
        SharedFact("tests/validation/skill_layout.py", "HOME_SPELLINGS", _patterns_member),
    )


def _fact_members(tree: ast.Module, name: str) -> frozenset[str] | None:
    """The strings a module-level name holds, as a string literal, a whole number written in decimal, or a collection
    of string literals, or None."""
    resolver = CallableResolver(tree)
    for node in module_statements(tree.body):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return frozenset({value.value})
        if isinstance(value, ast.Constant) and isinstance(value.value, int) and not isinstance(value.value, bool):
            return frozenset({str(value.value)})
        if isinstance(value, ast.Call) and resolver.name(value.func) in COLLECTION_CALLS and len(value.args) == 1:
            value = value.args[0]
        items = _literals(value)
        return frozenset(item for item in items if isinstance(item, str)) if items else None
    return None


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """The docstrings of a module and its definitions, by node identity: prose about a fact, not a copy of it."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def shared_fact_owner_problems(root: Path) -> list[str]:
    """Report a shared fact whose owner no longer holds it as a literal, so the restatement check cannot go quiet
    after a rename or a move."""
    problems: list[str] = []
    for fact in _shared_facts():
        path = root / fact.owner
        trees = list(python_trees([path] if path.is_file() else [], root, problems))
        if not trees or _fact_members(trees[0][2], fact.name) is None:
            problems.append(
                f"{fact.owner} no longer holds {fact.name} as a literal; name its new home in the shared facts of "
                "tests/validation/duplication.py"
            )
    return problems


def _restating_lines(tree: ast.Module, fact: SharedFact, members: frozenset[str]) -> list[int]:
    """The lines where the module restates the fact, docstrings aside."""
    stem = PurePath(fact.owner).stem
    imports_owner = any(stem in found.module.split(".") for found in module_imports(tree))
    prose = _docstring_nodes(tree)
    return sorted(
        {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.expr)
            and id(node) not in prose
            and fact.restates(node, members, imports_owner, fact.name)
        }
    )


def _restatement_problems(
    trees: list[tuple[str, ast.Module]], allowances: dict[str, dict[str, str]], copied: dict[str, set[str]]
) -> tuple[list[str], list[str]]:
    """Each restatement of a shared fact outside its owner, and each sanction of one whose reason does not name the
    owner. A restating module sanctions the copy under the fact's name, and copied records the sanction as needed."""
    owners = dict(trees)
    found: list[str] = []
    problems: list[str] = []
    for fact in _shared_facts():
        members = _fact_members(owners[fact.owner], fact.name) if fact.owner in owners else None
        for module, tree in trees:
            lines = _restating_lines(tree, fact, members) if members and module != fact.owner else []
            if not lines:
                continue
            copied.setdefault(module, set()).add(fact.name)
            reason = allowances[module].get(fact.name)
            if reason is None:
                found += [
                    f"{module}:{line} restates {fact.name}, which {fact.owner} holds; import it from there, or "
                    f"sanction it in {module} with {DUPLICATION_ALLOWANCE}"
                    for line in lines
                ]
            elif fact.owner not in reason:
                problems.append(
                    f"{module}: {DUPLICATION_ALLOWANCE} gives a reason for {fact.name} that does not name {fact.owner}"
                )
    return found, problems


def duplicated_definition_problems(root: Path) -> list[str]:
    """Report a module-level function or class defined identically in two files, a module-level literal or compiled
    pattern assigned identically in two files, a shared fact restated outside its owner, and allowances no longer
    needed.

    Copied helpers began identical and then drifted, and so did copied patterns; a copy caught while it is still
    identical is caught before it diverges. Two scripts of one skill count, because one can import the other.
    """
    copies: dict[str, list[Copy]] = {}
    allowances: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    files = [path for top in DUPLICATION_ROOTS for path in (root / top).rglob("*.py") if not is_test_script(path)]
    ordered = sorted(files, key=lambda path: path.relative_to(root).as_posix())
    trees: list[tuple[str, ast.Module]] = []
    for _, name, tree in python_trees(ordered, root, problems):
        trees.append((name, tree))
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
                f"every other copy: {listed(missing, 'and')}"
                for module, copy in held
                if (
                    missing := [other for other in modules if other != module and other not in allowances[module][copy]]
                )
            ]
            continue
        found.append(_copy_report(key, group))
    restated, unnamed = _restatement_problems(trees, allowances, copied)
    found += restated
    problems += unnamed
    for module, allowed in allowances.items():
        problems += [
            f"{module}: {DUPLICATION_ALLOWANCE} allows {entry}, which no other file defines identically or restates "
            "as a shared fact"
            for entry in sorted(allowed)
            if entry not in copied.get(module, set())
        ]
    return sorted(found) + sorted(problems)


# skills/ejryan89-skill-core/scripts is the one home of the repository's shared modules: skill scripts, the deployer,
# and tools/ import them from there. deployer/__init__.py puts the directory on sys.path for every deployer module,
# since a package runs its __init__.py before any of its modules; a tools module states the same path statement itself,
# above its imports, because ruff sorts an ejryan89-skill-core import above the deployer's.
SKILL_CORE_SCRIPTS = f"skills/{SKILL_CORE}/scripts"
SKILL_CORE_DOC = VALIDATION_DOC
DEPLOYER_PACKAGE = "deployer/__init__.py"


def skill_core_modules(root: Path) -> set[str]:
    """The names of ejryan89-skill-core's shared modules, which skill scripts, the deployer, and tools/ import."""
    return {path.stem for path in (root / SKILL_CORE_SCRIPTS).glob("*.py") if not is_test_script(path)}


def skill_core_copy_problems(root: Path) -> list[str]:
    """Report a module outside ejryan89-skill-core with the name of an ejryan89-skill-core module: another copy of
    shared code.

    A renamed copy is caught by the duplication policy, which sanctions no whole module, and a same-named one here
    even after it drifts, as the frontmatter reader's deployer copy would have.
    """
    shared = skill_core_modules(root)
    problems: list[str] = []
    for top in DUPLICATION_ROOTS:
        for path in sorted((root / top).rglob("*.py")):
            name = path.relative_to(root).as_posix()
            if (
                path.stem in shared
                and not is_test_script(path)
                and not posix_within(name, SKILL_CORE_SCRIPTS, strict=True)
            ):
                problems.append(
                    f"{name} has the name of {SKILL_CORE_SCRIPTS}/{path.stem}.py; import {path.stem} from "
                    f"{SKILL_CORE} instead of keeping another copy; see {SKILL_CORE_DOC}"
                )
    return sorted(problems)


def _skill_core_statement_lines(tree: ast.Module) -> list[int]:
    """Lines of the module-level statements that put ejryan89-skill-core's scripts first on sys.path, located from
    __file__."""
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
            and SKILL_CORE in scripts_put_on_path(node)
        ):
            lines.append(node.lineno)
    return lines


def _imported_modules(tree: ast.Module) -> list[tuple[int, str]]:
    """Each absolute import in the module, and each module it imports by name through importlib.import_module or
    __import__, as its line and the dotted module name it imports; "" for a name that is not one string literal."""
    found = {(found.line, found.module) for found in module_imports(tree)}
    resolver = CallableResolver(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and resolver.name(node.func) in MODULE_LOADERS:
            name = call_argument(node, 0, "name")
            literal = name.value if isinstance(name, ast.Constant) and isinstance(name.value, str) else ""
            found.add((node.lineno, literal))
    return sorted(found)


def _unreadable_import(name: str, line: int) -> str:
    return (
        f"{name}:{line} imports a module by a name it cannot read; import it by a statement or one string literal; "
        f"see {SKILL_CORE_DOC}"
    )


def skill_core_import_problems(root: Path) -> list[str]:
    """Report a deployer or tools module that imports ejryan89-skill-core before putting it on sys.path, and an import
    that breaks the direction or the standard-library rule.

    deployer/__init__.py holds the deployer's one path statement, located from its own file and never through the
    configured source path; a tools module states it above its ejryan89-skill-core imports. `ejryan89-skill-core`
    imports nothing from the deployer or tools/, so a deployed skill never needs the checkout, and the deployer and
    ejryan89-skill-core import only the standard library, the deployer, and ejryan89-skill-core.
    """
    shared = skill_core_modules(root)
    problems: list[str] = []
    package = root / DEPLOYER_PACKAGE
    package_lines = [
        line
        for _, _, tree in python_trees([package] if package.is_file() else [], root, [])
        for line in _skill_core_statement_lines(tree)
    ]
    if not package_lines:
        problems.append(
            f"{DEPLOYER_PACKAGE} does not put {SKILL_CORE_SCRIPTS} first on sys.path, located from its own file; "
            f"see {SKILL_CORE_DOC}"
        )
    allowed = set(sys.stdlib_module_names) | shared | {"deployer"}
    for top in ("deployer", "tools", SKILL_CORE_SCRIPTS):
        for path, name, tree in python_trees(sorted((root / top).rglob("*.py")), root, problems):
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
                if not module:
                    problems.append(_unreadable_import(name, line))
                elif top == SKILL_CORE_SCRIPTS and first in {"deployer", "tools"}:
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


def _module_names(scripts: Path) -> set[str]:
    return {path.stem for path in scripts.glob("*.py") if not is_test_script(path)}


def skill_script_import_problems(root: Path) -> list[str]:
    """Report a shipped skill script outside ejryan89-skill-core that imports a module other than the standard
    library's, ejryan89-skill-core's, its own skill's, or one of a skill its skill_deps reach, by a statement or by
    name.

    A deployed skill runs on a machine with the prerequisites and nothing else, so a module beyond these is missing
    wherever the skill is deployed, and an installed development dependency would hide that until then.
    """
    allowed = set(sys.stdlib_module_names) | skill_core_modules(root)
    problems: list[str] = []
    skills = root / "skills"
    for scripts in sorted(path for path in skills.glob("*/scripts") if (path.parent / "SKILL.md").is_file()):
        skill = scripts.parent.name
        if skill == SKILL_CORE:
            continue
        reachable = allowed | _module_names(scripts)
        for dependency in skill_dependency_closure(root, skill):
            reachable |= _module_names(skills / dependency / "scripts")
        files = sorted(path for path in scripts.glob("*.py") if not is_test_script(path))
        for _, name, tree in python_trees(files, root, problems):
            for line, module in _imported_modules(tree):
                if not module:
                    problems.append(_unreadable_import(name, line))
                elif module.split(".")[0] not in reachable:
                    problems.append(
                        f"{name}:{line} imports {module}, which is neither the standard library, {SKILL_CORE}, the "
                        f"skill's own scripts, nor a skill its skill_deps reach; see {SKILL_CORE_DOC}"
                    )
    return sorted(problems)


# ejryan89-skill-core's path_containment is the one test of whether a path lies in a tree, so a fix to it reaches every
# caller. Any other containment test, an is_relative_to, an os.path.commonpath, or a prefix test written by hand, states
# why in a module-level CONTAINMENT_ALLOWED = {scope: reason}.
PATH_CONTAINMENT = "skills/ejryan89-skill-core/scripts/path_containment.py"
CONTAINMENT_ALLOWANCE = "CONTAINMENT_ALLOWED"


def _containment_test(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """The containment test `node` writes, or None: an is_relative_to, an os.path.commonpath, a startswith of a text
    ending in a separator, or a comparison of a path's leading parts."""

    def separator(value: ast.expr) -> bool:
        return (isinstance(value, ast.Constant) and value.value in {"/", "\\"}) or qualified_name(value, aliases) in {
            "os.sep",
            "os.altsep",
            "os.path.sep",
            "os.path.altsep",
        }

    def ends_in_separator(value: ast.expr) -> bool:
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            return separator(value.right) or (
                isinstance(value.right, ast.Constant)
                and isinstance(value.right.value, str)
                and value.right.value.endswith(("/", "\\"))
            )
        return (
            isinstance(value, ast.JoinedStr)
            and bool(value.values)
            and isinstance(last := value.values[-1], ast.Constant)
            and isinstance(last.value, str)
            and last.value.endswith(("/", "\\"))
        )

    def leading_parts(value: ast.expr) -> bool:
        return (
            isinstance(value, ast.Subscript)
            and isinstance(value.value, ast.Attribute)
            and value.value.attr == "parts"
            and isinstance(value.slice, ast.Slice)
            and value.slice.lower is None
            and isinstance(value.slice.upper, ast.Call)
            and qualified_name(value.slice.upper.func, aliases) == "len"
        )

    if isinstance(node, ast.Attribute) and node.attr == "is_relative_to":
        return "is_relative_to"
    if isinstance(node, ast.Attribute | ast.Name) and qualified_name(node, aliases) in {
        "os.path.commonpath",
        "os.path.commonprefix",
    }:
        return qualified_name(node, aliases).removeprefix("os.path.")
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "startswith"
        and node.args
        and ends_in_separator(node.args[0])
    ):
        return "a startswith of a folder and its separator"
    if isinstance(node, ast.Compare) and any(leading_parts(side) for side in (node.left, *node.comparators)):
        return "a comparison of a path's leading parts"
    return None


def containment_problems(root: Path, files: list[Path]) -> list[str]:
    """Each containment test outside path_containment that no CONTAINMENT_ALLOWED entry sanctions."""
    problems: list[str] = []
    paths = [path for path in claim_files(root, files) if path.relative_to(root).as_posix() != PATH_CONTAINMENT]
    for _, name, tree in python_trees(paths, root, problems):
        aliases = import_aliases(tree)
        found = [
            (
                node.lineno,
                scope,
                f"tests containment with {test}; call ejryan89-skill-core's path_containment, the one containment "
                f"test, or state why in {CONTAINMENT_ALLOWANCE}",
            )
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.expr) and (test := _containment_test(node, aliases))
        ]
        problems += sanctioned_problems(name, tree, CONTAINMENT_ALLOWANCE, found)
    return problems


class DuplicationPolicies(unittest.TestCase):
    def test_repository_copies_only_what_it_sanctions(self) -> None:
        # Copied helpers began identical and then drifted, so a copy is caught while it is still identical.
        self.assertEqual([], duplicated_definition_problems(REPOSITORY_ROOT))

    def test_repository_owners_hold_every_shared_fact(self) -> None:
        self.assertEqual([], shared_fact_owner_problems(REPOSITORY_ROOT))

    def test_repository_keeps_shared_modules_in_skill_core(self) -> None:
        self.assertEqual([], skill_core_copy_problems(REPOSITORY_ROOT))
        self.assertEqual([], skill_core_import_problems(REPOSITORY_ROOT))

    def test_shipped_skill_scripts_import_only_what_a_deployment_holds(self) -> None:
        self.assertEqual([], skill_script_import_problems(REPOSITORY_ROOT))

    def test_path_containment_is_the_one_containment_test(self) -> None:
        self.assertEqual([], containment_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_the_duplication_policy_reads_every_module_its_rule_speaks_of(self) -> None:
        modules = formatted_modules(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT))
        self.assertEqual(
            [], scope_problems("the duplication policy", modules, DUPLICATION_ROOTS, duplication_omissions())
        )

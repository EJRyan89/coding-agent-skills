"""The Python static checks: ruff format and lint, mypy and its mypy_path, the noqa and type: ignore rules, the rule
against issue numbers in comments and docstrings, and the policies that hold the code's universal claims: every process
starts without git's repository-selecting variables, link_probe is the one link test, every ctypes foreign function a
module calls is declared, no process runner's result is discarded unchecked, and a handler around a shared reader
catches the whole family of errors the reader raises on content."""

from __future__ import annotations

import ast
import builtins
import contextlib
import functools
import hashlib
import importlib
import io
import os
import re
import sys
import tempfile
import tokenize
import tomllib
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from job_pool import UNSPLIT_SUITE_WEIGHT, Job, run_process
from path_containment import posix_within, within
from toolchain import find_mypy, find_ruff
from validation_support import (
    ATTRIBUTE_LOOKUPS,
    REPOSITORY_ROOT,
    CallableResolver,
    UnreadablePython,
    call_argument,
    import_aliases,
    is_test_script,
    module_allowance,
    module_imports,
    parse_source,
    path_order,
    python_source,
    python_trees,
    qualified_name,
    relative,
    repository_files,
    scope_problems,
    scoped_nodes,
    scripts_put_on_path,
    skill_script_directories,
    under,
)

from deployer import platform_support

DUPLICATION_ALLOWED = {
    "SUITE_NAME_PATTERNS": (
        "the lint policy pins pyproject.toml's ruff per-file-ignores as ruff reads them, globs in a TOML file that "
        "cannot import skills/ejryan89-skill-core/scripts/suite_names.py"
    ),
}
# The folder each type check keeps mypy's cache under, in a folder of its own, so no two mypy processes of a run share
# one. CI names a folder actions/cache restores between runs, so mypy reparses no standard-library stubs; unset, each
# check caches in a temporary folder removed when it ends, so a run writes nothing into the tree.
MYPY_CACHE_VARIABLE = "VALIDATION_MYPY_CACHE"
# Every Python file under these is checked with `ruff format --check` and `ruff check`; none is excluded, since
# pyproject.toml empties ruff's default exclusions.
FORMAT_ROOTS = ("deployer", "tools", "tests", "skills", ".claude/skills", "deploy.py", "conftest.py")
# A noqa comment names the codes it suppresses and says why after a dash, as in `noqa: F401 - <reason>`.
NOQA = re.compile(r"#\s*noqa\b", re.IGNORECASE)
NOQA_WITH_REASON = re.compile(r"#\s*noqa:\s*[A-Z]+[0-9]+(?:\s*,\s*[A-Z]+[0-9]+)*\s+-\s+\S")
# The codes a noqa comment names, separated by commas or spaces as ruff reads them.
NOQA_CODES = re.compile(r"#\s*noqa:\s*((?:[A-Z]+[0-9]+[\s,]*)+)", re.IGNORECASE)
# Function complexity and length take no noqa: a function over either ceiling is split instead.
CEILING_CODES = frozenset({"C901", "PLR0915"})
# mypy checks these as one root from the repository root, and each skill's scripts/ directory from inside it, where
# the deployed skill's own imports resolve. pyproject.toml's [tool.mypy] holds the configuration; it excludes nothing.
TYPE_CHECK_ROOTS = ("deployer", "tools", "deploy.py", "tests", "conftest.py")
# The areas mypy checks in strict mode, which pyproject.toml's [tool.validation] mypy-strict lists. Every type-check
# root is one, and none leaves, so each one here must stay listed there; a pull request that adds a root, such as a new
# skill's scripts, lists it there and here.
STRICT_TYPE_CHECK_AREAS = (
    "skills/ejryan89-skill-core/scripts",
    "deployer",
    "deploy.py",
    "tools",
    "tests",
    "conftest.py",
    "skills/audit-ai-config/scripts",
    "skills/audit-skill/scripts",
    "skills/curate-agent-memory/scripts",
    "skills/dotnet-dependencies/scripts",
    "skills/dotnet-format/scripts",
    "skills/ejryan89-code-review-core/scripts",
    "skills/flag-review-finding/scripts",
    "skills/github-activity-report/scripts",
    "skills/repo-cleanup/scripts",
    "skills/review-document/scripts",
    "skills/review-insights/scripts",
    "skills/update-pr-dashboard/scripts",
    ".claude/skills/audit-repository/scripts",
    ".claude/skills/change-skill/scripts",
    ".claude/skills/implement-change/scripts",
    ".claude/skills/review-repository/scripts",
    ".claude/skills/shepherd-pull-requests/scripts",
)
# The folders mypy passes over when it collects the files under a root it is given, by their names, which no setting
# changes: a file in one is type-checked by nothing.
MYPY_SKIPPED_FOLDERS = frozenset({"__pycache__", "site-packages", "node_modules"})
# A type: ignore names its error codes and states its reason in a comment after it: `# type: ignore[code]  # <why>`.
TYPE_IGNORE = re.compile(r"#\s*type:\s*ignore\b")
TYPE_IGNORE_WITH_REASON = re.compile(r"#\s*type:\s*ignore\[[a-z-]+(?:\s*,\s*[a-z-]+)*\]\s*#\s*\S")
# A comment or docstring states its reason in words, never as an issue number: a number after a hash sign, alone, in
# parentheses, or possessive, or after the word issue. A hash sign after a word or a slash, as in a fixture's
# owner/repo pull selector, is not one. It holds the Python under FORMAT_ROOTS except the fixture trees under
# tests/fixtures, which are review inputs, not this code.
ISSUE_REFERENCE = re.compile(r"(?<![\w/&#])#\d+\b|\bissues?\s+#?\d+\b", re.IGNORECASE)
ISSUE_REFERENCE_EXCLUDED = ("tests/fixtures",)


def comments(source: str) -> list[tokenize.TokenInfo]:
    """Each comment in the Python source, as tokenize reads it, so text in a string never counts as one."""
    return [token for token in tokenize.generate_tokens(io.StringIO(source).readline) if token.type == tokenize.COMMENT]


def file_comments(root: Path, path: Path, found: list[str]) -> list[tokenize.TokenInfo]:
    """Each comment in a Python file, or none when no policy can read it, which found then names."""
    try:
        return comments(python_source(path))
    except UnreadablePython as error:
        found.append(f"{path.relative_to(root).as_posix()} {error}")
    except (SyntaxError, tokenize.TokenError) as error:
        found.append(f"{path.relative_to(root).as_posix()} does not tokenize: {error}")
    return []


def noqa_without_reason(root: Path, files: list[Path]) -> list[str]:
    """Each `# noqa` comment that does not name its codes and state its reason after ` - `, as path:line."""
    found: list[str] = []
    for path in sorted(files):
        if path.suffix != ".py":
            continue
        found.extend(
            f"{path.relative_to(root).as_posix()}:{token.start[0]}"
            for token in file_comments(root, path, found)
            if NOQA.search(token.string) and not NOQA_WITH_REASON.search(token.string)
        )
    return found


def ceiling_noqa(root: Path, files: list[Path]) -> list[str]:
    """Each noqa comment that suppresses the complexity or length ceiling, as path:line and the codes it names."""
    found: list[str] = []
    for path in sorted(files):
        if path.suffix != ".py":
            continue
        for token in file_comments(root, path, found):
            named = sorted(noqa_codes(token.string) & CEILING_CODES)
            if named:
                found.append(
                    f"{path.relative_to(root).as_posix()}:{token.start[0]} suppresses {' and '.join(named)}; "
                    "split the function instead"
                )
    return found


def noqa_codes(comment: str) -> set[str]:
    """The codes the noqa comments in a comment name, in upper case."""
    return {
        code.upper() for match in NOQA_CODES.finditer(comment) for code in re.split(r"[\s,]+", match.group(1)) if code
    }


# A ratchet only goes down. The ceilings pyproject.toml sets on a function's complexity, statements, and arguments,
# and the number of blind excepts kept with a noqa, may fall as the code improves and never rise; a pull request that
# lowers one lowers its literal here in the same change.
CEILINGS = {"mccabe": {"max-complexity": 15}, "pylint": {"max-statements": 50, "max-args": 17}}
BLIND_EXCEPT_CEILING = 14
BLIND_EXCEPT = "BLE001"


def blind_except_noqa(root: Path, files: list[Path]) -> list[str]:
    """Each noqa comment that keeps a blind except, as path:line."""
    found: list[str] = []
    for path in sorted(files):
        if path.suffix != ".py":
            continue
        found.extend(
            f"{path.relative_to(root).as_posix()}:{token.start[0]}"
            for token in file_comments(root, path, found)
            if BLIND_EXCEPT in noqa_codes(token.string)
        )
    return found


def ratchet_problems(lint: dict[str, Any], blind_excepts: list[str]) -> list[str]:
    """Each ceiling of the lint configuration missing, raised past its literal, or lowered below it, and the blind
    excepts kept with a noqa when there are more or fewer of them than theirs: a value below its literal could rise
    back to it unrefused, so the literal is lowered in the same change."""
    found: list[str] = []
    for table, ceilings in CEILINGS.items():
        for key, ceiling in ceilings.items():
            value = lint.get(table, {}).get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value > ceiling:
                found.append(
                    f"[tool.ruff.lint.{table}] {key} is {value!r}; the ratchet holds it at {ceiling} or lower, so "
                    "split the function instead"
                )
            elif value < ceiling:
                found.append(
                    f"[tool.ruff.lint.{table}] {key} is {value}, below the ratchet's {ceiling}; lower its literal in "
                    f"CEILINGS to {value} in the same change"
                )
    if len(blind_excepts) > BLIND_EXCEPT_CEILING:
        found.append(
            f"{len(blind_excepts)} noqa comments keep a blind except, past the ratchet's {BLIND_EXCEPT_CEILING}; "
            f"catch the exceptions the code expects instead: {', '.join(blind_excepts)}"
        )
    elif len(blind_excepts) < BLIND_EXCEPT_CEILING:
        found.append(
            f"{len(blind_excepts)} noqa comments keep a blind except, below the ratchet's {BLIND_EXCEPT_CEILING}; "
            f"lower BLIND_EXCEPT_CEILING to {len(blind_excepts)} in the same change"
        )
    return found


# A comment that turns a check off for a file or a region instead of one finding on its line: mypy's inline
# configuration, such as ignore-errors; ruff's file-level noqa, which it also reads in flake8's spelling; ruff's range
# suppression, which turns the codes it names off until the comment that turns them on again, the complexity and
# length ceilings included; an isort skip, which ruff's import rules honor; and the formatter's off and skip markers.
PYTHON_SUPPRESSION = re.compile(
    r"#\s*(?:mypy:|(?:ruff|flake8):\s*noqa\b|ruff:\s*disable\b|(?:ruff:\s*)?isort:\s*(?:skip_file|off)\b"
    r"|fmt:\s*(?:off|skip)\b|yapf:\s*disable\b)",
    re.IGNORECASE,
)
# ruff reads its configuration from the nearest of these to each file, so one anywhere but the root's pyproject.toml
# would replace the rule set validation pins for every file beneath it.
RUFF_CONFIGURATION_FILES = frozenset({"ruff.toml", ".ruff.toml", "pyproject.toml"})


def python_suppression_problems(root: Path, files: list[Path]) -> list[str]:
    """Each comment that turns a Python check off beyond one finding on its line, and each ruff configuration file
    other than the root's pyproject.toml."""
    found: list[str] = []
    for path in sorted(files, key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        if path.name in RUFF_CONFIGURATION_FILES and name != "pyproject.toml":
            found.append(f"{name} configures ruff; pyproject.toml at the repository root is its one configuration")
        if path.suffix != ".py":
            continue
        found += [
            f"{name}:{token.start[0]} turns a check off with {match.group(0)!r}; fix each finding at its cause"
            for token in file_comments(root, path, found)
            if (match := PYTHON_SUPPRESSION.search(token.string))
        ]
    return found


def type_ignore_without_reason(root: Path, files: list[Path]) -> list[str]:
    """Each `# type: ignore` comment that does not name its codes and state its reason after them, as path:line."""
    found: list[str] = []
    for path in sorted(files):
        if path.suffix != ".py":
            continue
        found.extend(
            f"{path.relative_to(root).as_posix()}:{token.start[0]}"
            for token in file_comments(root, path, found)
            if TYPE_IGNORE.search(token.string) and not TYPE_IGNORE_WITH_REASON.search(token.string)
        )
    return found


def docstring_lines(source: str) -> list[tuple[int, str]]:
    """Each line of the module's, every class's, and every function's docstring, with its line number in the source."""
    lines: list[tuple[int, str]] = []
    for node in ast.walk(parse_source(source, "<docstrings>")):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) or not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            segment = ast.get_source_segment(source, first.value) or ""
            lines += [(first.lineno + offset, line) for offset, line in enumerate(segment.splitlines())]
    return lines


def issue_numbers_in_comments(root: Path, files: list[Path]) -> list[str]:
    """Each comment or docstring line under the checked roots that cites an issue number, as path:line."""
    found: list[str] = []
    for path in sorted(files):
        name = path.relative_to(root).as_posix()
        if (
            path.suffix != ".py"
            or not any(posix_within(name, prefix) for prefix in FORMAT_ROOTS)
            or any(posix_within(name, prefix, strict=True) for prefix in ISSUE_REFERENCE_EXCLUDED)
        ):
            continue
        try:
            source = python_source(path)
            lines = {number for number, line in docstring_lines(source) if ISSUE_REFERENCE.search(line)}
        except UnreadablePython as error:
            found.append(f"{name} {error}")
            continue
        lines |= {
            token.start[0]
            for token in comments(source)
            # The comment's own leading hash sign is not a citation, so the search starts after it.
            if ISSUE_REFERENCE.search(token.string[1:])
        }
        found += [f"{name}:{number}" for number in sorted(lines)]
    return found


def _type_check_root(root: Path, path: Path, skill_scripts: list[Path]) -> Path | None:
    """The folder mypy collects a file from: its skill's scripts/, or the root of TYPE_CHECK_ROOTS holding it."""
    for scripts in skill_scripts:
        if within(path, scripts):
            return scripts
    for top in TYPE_CHECK_ROOTS:
        if within(path, root / top):
            return root / top
    return None


def type_check_skipped_problems(root: Path, files: list[Path]) -> list[str]:
    """Each Python file under a root mypy checks that it would pass over: one in a folder named like a cache, an
    installed package, or a Node module, or in a folder whose name starts with a dot, which mypy never reads."""
    skill_scripts = skill_script_directories(root)
    problems: list[str] = []
    for path in sorted(files, key=lambda path: path.relative_to(root).as_posix()):
        checked_from = _type_check_root(root, path, skill_scripts) if path.suffix == ".py" else None
        if checked_from is None or path == checked_from:
            continue
        skipped = [
            part
            for part in path.parent.relative_to(checked_from).parts
            if part in MYPY_SKIPPED_FOLDERS or part.startswith(".")
        ]
        if skipped:
            problems.append(
                f"{path.relative_to(root).as_posix()} is in a folder named {skipped[0]}, which mypy passes over, so "
                "no type check reads it; move it"
            )
    return problems


def imports_a_sibling(path: Path, tree: ast.Module) -> bool:
    """Whether the module imports, by its bare name, another module in its own directory."""
    return any((path.parent / f"{found.module.split('.')[0]}.py").is_file() for found in module_imports(tree))


def mypy_path_problems(root: Path, files: list[Path]) -> list[str]:
    """mypy_path must name exactly the directories that modules reach outside mypy's own module resolution.

    Those are the skills' scripts directories a module or suite puts on sys.path, as a skill reaches a skill_deps
    sibling and the repository's tools reach audit-skill's inventory, and each directory outside a skill's
    scripts/ whose modules import a module beside them by its bare name, as the deployer suites import harness. A
    skill's scripts/ is checked from inside it, where such imports already resolve.
    """
    configuration = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    configured = configuration.get("tool", {}).get("mypy", {}).get("mypy_path", [])
    if not isinstance(configured, list):
        return ["pyproject.toml: [tool.mypy] mypy_path must be a list"]
    expected: dict[str, str] = {}
    unreadable: list[str] = []
    skill_scripts = set(skill_script_directories(root))
    for path, _, tree in python_trees(sorted(path for path in files if path.suffix == ".py"), root, unreadable):
        relative_path = path.relative_to(root)
        for name in scripts_put_on_path(tree):
            expected.setdefault(
                f"$MYPY_CONFIG_FILE_DIR/skills/{name}/scripts", f"{relative_path.as_posix()} puts on sys.path"
            )
        if len(relative_path.parts) > 1 and path.parent not in skill_scripts and imports_a_sibling(path, tree):
            expected.setdefault(
                f"$MYPY_CONFIG_FILE_DIR/{relative_path.parent.as_posix()}",
                f"{relative_path.as_posix()} imports a module from by its bare name",
            )
    problems = unreadable + [
        f"pyproject.toml: [tool.mypy] mypy_path lacks {entry}, which {expected[entry]}"
        for entry in sorted(set(expected) - set(configured))
    ]
    problems += [
        f"pyproject.toml: [tool.mypy] mypy_path names {entry}, which no module puts on sys.path"
        for entry in sorted(set(configured) - set(expected))
    ]
    return problems


def strict_type_check_areas(root: Path) -> list[str] | None:
    """The areas pyproject.toml's [tool.validation] mypy-strict lists, or None when it holds no list of paths."""
    configured = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    areas = configured.get("tool", {}).get("validation", {}).get("mypy-strict")
    if not isinstance(areas, list) or not all(isinstance(area, str) for area in areas):
        return None
    return areas


def strict_area_problems(root: Path, pinned: tuple[str, ...] = STRICT_TYPE_CHECK_AREAS) -> list[str]:
    """The strict list must be a list of type-check roots, each a skill's scripts/ directory or one of
    TYPE_CHECK_ROOTS, holding every pinned area, since an area never leaves it, and every root the tree has, since
    every root is checked in strict mode."""
    areas = strict_type_check_areas(root)
    if areas is None:
        return ["pyproject.toml: [tool.validation] mypy-strict must be a list of paths from the repository root"]
    skill_roots = {scripts.relative_to(root).as_posix() for scripts in type_check_skill_roots(root)}
    roots = skill_roots | set(TYPE_CHECK_ROOTS)
    problems = [
        f"pyproject.toml: [tool.validation] mypy-strict lacks {area}, which is checked in strict mode; an area "
        "joins the list and never leaves it, so fix the errors strict mode reports instead"
        for area in pinned
        if area not in areas
    ]
    present = skill_roots | {top for top in TYPE_CHECK_ROOTS if (root / top).exists()}
    problems += [
        f"pyproject.toml: [tool.validation] mypy-strict lacks {area}, a type-check root; every root is checked in "
        "strict mode, so list it and fix the errors strict mode reports there"
        for area in sorted(present - set(areas) - set(pinned))
    ]
    problems += [
        f"pyproject.toml: [tool.validation] mypy-strict names {area}, which is no root validation type-checks"
        for area in areas
        if area not in roots
    ]
    problems += [
        f"pyproject.toml: [tool.validation] mypy-strict names {area} more than once"
        for area in sorted({area for area in areas if areas.count(area) > 1})
    ]
    return problems


# The Python whose universal claims the policies below hold: every non-test module under these folders and these files,
# the policies and their runner among them. claim_omissions states why the rest of the formatted Python is left out.
CLAIM_ROOTS = (
    "skills",
    "deployer",
    "deploy.py",
    "tools",
    ".claude/skills",
    "tests/validation",
    "tests/run_validation.py",
    "conftest.py",
)


def claim_files(root: Path, files: list[Path]) -> list[Path]:
    """The non-test Python modules under CLAIM_ROOTS among files, in path order."""
    found = [
        path
        for path in files
        if path.suffix == ".py" and not is_test_script(path) and under(path.relative_to(root).as_posix(), CLAIM_ROOTS)
    ]
    return sorted(found, key=lambda path: path_order(path.relative_to(root)))


def formatted_modules(root: Path, files: list[Path]) -> list[str]:
    """The repository path of each non-test Python module ruff checks: the Python a policy whose rule speaks of the
    repository's Python is held to."""
    names = [path.relative_to(root).as_posix() for path in files if path.suffix == ".py" and not is_test_script(path)]
    return [name for name in names if under(name, FORMAT_ROOTS)]


def claim_omissions() -> dict[str, str]:
    """The parts of the formatted Python the claim policies leave out, each with its reason."""
    return {
        "tests": "the suites' support and fixture trees start processes, make links, and parse the input the code "
        "under test refuses, to test that it refuses them",
    }


def sanctioned_problems(name: str, tree: ast.Module, allowance: str, found: list[tuple[int, str, str]]) -> list[str]:
    """Each finding, as its line, the scope holding it, and its refusal, that the module's allowance does not
    sanction, then each entry that sanctions nothing, and an allowance that is no dictionary of reasons."""
    allowed = module_allowance(tree, allowance)
    problems: list[str] = []
    if allowed is None:
        problems.append(f"{name}: {allowance} must map each function, class, or assignment to its reason")
        allowed = {}
    problems += [f"{name}:{line} {refusal}" for line, scope, refusal in found if scope not in allowed]
    problems += [
        f"{name}: {allowance} allows {scope}, which holds nothing it sanctions; remove the entry"
        for scope in sorted(set(allowed) - {scope for _, scope, _ in found})
    ]
    return problems


def resolves_to(qualified: str, name: str) -> bool:
    """Whether a qualified name is `name`, reached as itself or through the package a module imports it from."""
    return qualified == name or qualified.endswith(f".{name}")


# Every process the repository starts runs without git's repository-selecting variables. ejryan89-skill-core's
# bounded_process starts it so; a start anywhere else passes an environment that module's without_repository_variables
# built, or states why in a module-level PROCESS_ENV_ALLOWED = {scope: reason}.
PROCESS_STARTS = frozenset(
    {
        *(f"subprocess.{start}" for start in ("run", "Popen", "call", "check_call", "check_output")),
        *(
            f"{module}.create_subprocess_{kind}"
            for module in ("asyncio", "asyncio.subprocess")
            for kind in ("exec", "shell")
        ),
    }
)
CLEARED_ENVIRONMENT = "bounded_process.without_repository_variables"
PROCESS_RUNNER = "skills/ejryan89-skill-core/scripts/bounded_process.py"
PROCESS_ENV_ALLOWANCE = "PROCESS_ENV_ALLOWED"
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _holders(tree: ast.Module) -> dict[int, ast.AST]:
    """The innermost function or lambda holding each node, by the node's id, or the module for a node in none."""
    holders: dict[int, ast.AST] = {}

    def walk(node: ast.AST, holder: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            holders[id(child)] = holder
            walk(child, child if isinstance(child, FUNCTIONS) else holder)

    walk(tree, tree)
    return holders


def _clears(value: ast.expr | None, resolver: CallableResolver) -> bool:
    """Whether an expression is a call of without_repository_variables."""
    return isinstance(value, ast.Call) and resolves_to(resolver.name(value.func), CLEARED_ENVIRONMENT)


def _cleared_names(tree: ast.Module, holders: dict[int, ast.AST], resolver: CallableResolver) -> dict[int, set[str]]:
    """The names each function, by its id, binds only to an environment without_repository_variables builds. A
    parameter is bound by the caller, so it is none; an update in place, such as `env.update(...)` or `env |= ...`,
    keeps the name bound to that environment."""
    assigned: dict[int, ast.expr] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            assigned |= {id(target): node.value for target in node.targets if isinstance(target, ast.Name)}
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
            assigned[id(node.target)] = node.value
    bound: dict[int, dict[str, list[bool]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, FUNCTIONS):
            parameters = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            for parameter in [*parameters, node.args.vararg, node.args.kwarg]:
                if parameter is not None:
                    bound.setdefault(id(node), {}).setdefault(parameter.arg, []).append(False)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            cleared = _clears(assigned.get(id(node)), resolver)
            bound.setdefault(id(holders[id(node)]), {}).setdefault(node.id, []).append(cleared)
    return {holder: {name for name, values in names.items() if all(values)} for holder, names in bound.items()}


def process_environment_problems(root: Path, files: list[Path]) -> list[str]:
    """Each process start outside bounded_process whose environment may keep git's repository-selecting variables,
    that no PROCESS_ENV_ALLOWED entry sanctions: one given no env, which inherits this process's, or an env that is
    neither a without_repository_variables call nor a name its function binds only to one."""
    problems: list[str] = []
    paths = [path for path in claim_files(root, files) if path.relative_to(root).as_posix() != PROCESS_RUNNER]
    for _, name, tree in python_trees(paths, root, problems):
        resolver = CallableResolver(tree)
        holders = _holders(tree)
        cleared = _cleared_names(tree, holders, resolver)
        found: list[tuple[int, str, str]] = []
        for node, scope in scoped_nodes(tree.body):
            if not isinstance(node, ast.Call) or (start := resolver.name(node.func)) not in PROCESS_STARTS:
                continue
            env = call_argument(node, None, "env")
            if _clears(env, resolver) or (
                isinstance(env, ast.Name) and env.id in cleared.get(id(holders[id(node)]), set())
            ):
                continue
            given = "no env, so it keeps" if env is None else "an env that may keep"
            found.append(
                (
                    node.lineno,
                    scope,
                    f"starts {start} with {given} git's repository-selecting variables; run it through "
                    "ejryan89-skill-core's bounded_process, pass env=without_repository_variables(...), or state why "
                    f"in {PROCESS_ENV_ALLOWANCE}",
                )
            )
        problems += sanctioned_problems(name, tree, PROCESS_ENV_ALLOWANCE, found)
    return problems


# ejryan89-skill-core's link_probe is the one test of whether a path is a link, and reads a symbolic link and a Windows
# junction alike, where is_symlink and islink see no junction. deployer/platform_support.py's is_junction delegates to
# it, so a call of that wrapper is no other test. Any other test states why in a module-level
# LINK_TEST_ALLOWED = {scope: reason}, as where a symbolic link and a junction must be told apart.
LINK_TESTS = frozenset({"is_symlink", "islink", "is_junction", "isjunction", "st_file_attributes"})
LINK_PROBE = "skills/ejryan89-skill-core/scripts/link_probe.py"
LINK_WRAPPER = "platform_support.is_junction"
LINK_TEST_ALLOWANCE = "LINK_TEST_ALLOWED"


def _link_test(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """The link test a node names, as an attribute, an imported name, or the literal a getattr or hasattr reads, or
    None."""
    named = ""
    if isinstance(node, ast.Attribute):
        named = qualified_name(node, aliases) or node.attr
    elif isinstance(node, ast.Name) and node.id in aliases:
        named = aliases[node.id]
    elif isinstance(node, ast.Call) and qualified_name(node.func, aliases) in {*ATTRIBUTE_LOOKUPS, "hasattr"}:
        literal = call_argument(node, 1, "name")
        named = literal.value if isinstance(literal, ast.Constant) and isinstance(literal.value, str) else ""
    test = named.rpartition(".")[2]
    return test if test in LINK_TESTS and not resolves_to(named, LINK_WRAPPER) else None


def link_test_problems(root: Path, files: list[Path]) -> list[str]:
    """Each link test outside link_probe that no LINK_TEST_ALLOWED entry sanctions: an is_symlink, islink,
    is_junction, or isjunction, or a read of st_file_attributes, which holds the reparse-point half of the test."""
    problems: list[str] = []
    paths = [path for path in claim_files(root, files) if path.relative_to(root).as_posix() != LINK_PROBE]
    for _, name, tree in python_trees(paths, root, problems):
        aliases = import_aliases(tree)
        found = [
            (
                node.lineno,
                scope,
                f"tests for a link with {test}; call ejryan89-skill-core's link_probe, the one link test, which reads "
                f"a symbolic link and a junction alike, or state why in {LINK_TEST_ALLOWANCE}",
            )
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.expr) and (test := _link_test(node, aliases))
        ]
        problems += sanctioned_problems(name, tree, LINK_TEST_ALLOWANCE, found)
    return problems


# ejryan89-skill-core's json_reader is the one parse of JSON, refusing a repeated key, a bare NaN or Infinity, and
# nesting past the recursion limit, where the json module's own readers keep a repeated key's last value and read NaN
# as a float. Any other parse states why in a module-level JSON_PARSE_ALLOWED = {scope: reason}, as where a function
# parses the text it just serialized.
JSON_PARSERS = frozenset({"json.loads", "json.load", "json.JSONDecoder", "json.decoder.JSONDecoder"})
JSON_READER = "skills/ejryan89-skill-core/scripts/json_reader.py"
JSON_PARSE_ALLOWANCE = "JSON_PARSE_ALLOWED"


def json_parse_problems(root: Path, files: list[Path]) -> list[str]:
    """Each use of the json module's own parsers outside json_reader, as an attribute or an imported name, that no
    JSON_PARSE_ALLOWED entry sanctions."""
    problems: list[str] = []
    paths = [path for path in claim_files(root, files) if path.relative_to(root).as_posix() != JSON_READER]
    for _, name, tree in python_trees(paths, root, problems):
        aliases = import_aliases(tree)
        found = [
            (
                node.lineno,
                scope,
                f"parses JSON with {parser}, which keeps a repeated key's last value and reads NaN; call "
                f"ejryan89-skill-core's json_reader, the one parse of JSON, or state why in {JSON_PARSE_ALLOWANCE}",
            )
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.Attribute | ast.Name)
            and isinstance(node.ctx, ast.Load)
            and (parser := qualified_name(node, aliases)) in JSON_PARSERS
        ]
        problems += sanctioned_problems(name, tree, JSON_PARSE_ALLOWANCE, found)
    return problems


# ejryan89-skill-core's whole_number is the one reader of a whole number from text, taking ASCII digits alone up to a
# cap and raising its caller's named error, where int() reads every Unicode digit, a sign, spaces, and underscores, and
# past the interpreter's digit limit raises a ValueError that names no input. So int() never converts a
# regular-expression group, or a value an isdigit(), isdecimal(), or isnumeric() test admitted, which passes a digit
# such as a superscript two that int() then refuses. Any other conversion states why in a module-level
# INT_PARSE_ALLOWED = {scope: reason}, as where the pattern itself bounds the digits.
WHOLE_NUMBER_READER = "skills/ejryan89-skill-core/scripts/whole_number.py"
INT_PARSE_ALLOWANCE = "INT_PARSE_ALLOWED"


def _method_call(node: ast.AST, *methods: str) -> ast.expr | None:
    """The receiver of a call of one of `methods` on it, or None."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in methods:
        return node.func.value
    return None


def _names(target: ast.expr) -> list[str]:
    """The names an assignment target binds, through tuples and lists."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Tuple | ast.List):
        return [name for element in target.elts for name in _names(element)]
    return []


def _branches(node: ast.expr) -> list[ast.expr]:
    """The values an expression may take: each branch of a conditional expression and each operand of `and` or `or`,
    through any nesting, or the expression itself."""
    if isinstance(node, ast.IfExp):
        return [*_branches(node.body), *_branches(node.orelse)]
    if isinstance(node, ast.BoolOp):
        return [branch for value in node.values for branch in _branches(value)]
    return [node]


def _iterated(node: ast.expr) -> list[ast.expr]:
    """The iterables a value's items come from: the value, and each argument of a zip(), enumerate(), sorted(), or
    reversed() call over it."""
    over = {"zip", "enumerate", "sorted", "reversed"}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in over:
        return [node, *(item for argument in node.args for item in _iterated(argument))]
    return [node]


def _bindings(node: ast.AST) -> list[tuple[list[str], ast.expr]]:
    """Each name list an assignment, a walrus, a for loop, or a comprehension binds, with the value or iterable it binds
    them from."""
    if isinstance(node, ast.Assign):
        return [(_names(target), node.value) for target in node.targets]
    if isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
        return [(_names(node.target), node.value)]
    if isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
        return [(_names(node.target), node.iter)]
    return []


class _ScopeNumbers:
    """What one scope binds that int() must not convert: the names bound to a regular-expression match, the names bound
    to a group of one, and each expression a digit test admitted, by its dump with any strip() calls taken off."""

    def __init__(self) -> None:
        self.matches: set[str] = set()
        self.groups: set[str] = set()
        self.tested: dict[str, str] = {}

    def is_group(self, node: ast.expr) -> bool:
        """Whether an expression is a group of a match: a group() call, an index of groups() or groupdict() or of a
        match, or a name bound to one, or a conditional expression or a boolean operation one of whose values is."""
        return any(self._group(branch) for branch in _branches(node))

    def _group(self, node: ast.expr) -> bool:
        if _method_call(node, "group") is not None:
            return True
        if isinstance(node, ast.Subscript):
            value = node.value
            return _method_call(value, "groups", "groupdict") is not None or (
                isinstance(value, ast.Name) and value.id in self.matches
            )
        return isinstance(node, ast.Name) and node.id in self.groups

    def bind(self, node: ast.AST) -> None:
        """Record what one node of the scope binds to a match or a group, in the order the scope's walk meets it."""
        for names, value in _bindings(node):
            if any(_method_call(branch, "match", "fullmatch", "search", "finditer") for branch in _branches(value)):
                self.matches.update(names)
            elif self.is_group(value) or any(_method_call(item, "groups", "findall") for item in _iterated(value)):
                self.groups.update(names)

    def test(self, node: ast.AST) -> None:
        """Record the expression a digit test or a regular-expression test on this node admits, with how to name the
        test."""
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            return
        method = node.func.attr
        receiver = _method_call(node, "isdigit", "isdecimal", "isnumeric")
        if receiver is not None:
            self.tested.setdefault(ast.dump(_unstripped(receiver)), f"an {method}() test")
            return
        pattern = _method_call(node, "match", "fullmatch", "search")
        # re.fullmatch(pattern, text) tests its second argument, and a compiled pattern's fullmatch(text) its first.
        index = 1 if isinstance(pattern, ast.Name) and pattern.id == "re" else 0
        if pattern is not None and len(node.args) > index:
            self.tested.setdefault(ast.dump(_unstripped(node.args[index])), f"a regular-expression {method}()")

    def refusal(self, call: ast.Call) -> str | None:
        """What a one-argument int() call converts that it must not, or None."""
        if not (
            isinstance(call.func, ast.Name) and call.func.id == "int" and len(call.args) == 1 and not call.keywords
        ):
            return None
        argument = call.args[0]
        if self.is_group(argument):
            return "a regular-expression group"
        test = self.tested.get(ast.dump(_unstripped(argument)))
        return f"a value {test} admitted" if test else None


def _unstripped(node: ast.expr) -> ast.expr:
    """An expression with each strip(), lstrip(), or rstrip() call on its end taken off."""
    while (receiver := _method_call(node, "strip", "lstrip", "rstrip")) is not None:
        node = receiver
    return node


def int_parse_problems(root: Path, files: list[Path]) -> list[str]:
    """Each one-argument int() call outside whole_number that converts a regular-expression group or a value a digit
    test admitted, that no INT_PARSE_ALLOWED entry sanctions."""
    problems: list[str] = []
    paths = [path for path in claim_files(root, files) if path.relative_to(root).as_posix() != WHOLE_NUMBER_READER]
    for _, name, tree in python_trees(paths, root, problems):
        nodes = list(scoped_nodes(tree.body))
        scopes: dict[str, _ScopeNumbers] = {}
        for node, scope in nodes:
            numbers = scopes.setdefault(scope, _ScopeNumbers())
            numbers.bind(node)
            numbers.test(node)
        found = [
            (
                node.lineno,
                scope,
                f"converts {what} with int(), which reads more than ASCII digits and past the digit limit raises a "
                "ValueError naming no input; call ejryan89-skill-core's whole_number.read, the one reader of a whole "
                f"number from text, or state why in {INT_PARSE_ALLOWANCE}",
            )
            for node, scope in nodes
            if isinstance(node, ast.Call) and (what := scopes[scope].refusal(node))
        ]
        problems += sanctioned_problems(name, tree, INT_PARSE_ALLOWANCE, found)
    return problems


# ejryan89-skill-core's json_writer is the one writer of JSON for a file, refusing at the write a non-finite number,
# a lone surrogate, and what json_reader would refuse on reading the text back, where the json module's own writer
# writes NaN and a lone surrogate a later strict read refuses. A json.dump, or a json.dumps whose text a write call
# takes, as its argument or through a name its function binds to it, states why in a module-level
# JSON_WRITE_ALLOWED = {scope: reason}, as where a function writes a message to a pipe.
JSON_DUMP = "json.dump"
JSON_DUMPS = "json.dumps"
JSON_WRITER = "skills/ejryan89-skill-core/scripts/json_writer.py"
# The methods that write to a file or a stream, and the functions that replace or append to a file.
WRITE_METHODS = frozenset({"write", "write_text", "write_bytes", "writelines"})
FILE_WRITERS = frozenset(
    {"replace_text", "replace_bytes", "atomic_write_text", "atomic_write_bytes", "write_atomic", "append_line"}
)
JSON_WRITE_ALLOWANCE = "JSON_WRITE_ALLOWED"


def _writes(call: ast.Call, aliases: dict[str, str]) -> bool:
    """Whether a call writes what it is given to a file or a stream."""
    if isinstance(call.func, ast.Attribute) and call.func.attr in WRITE_METHODS:
        return True
    return qualified_name(call.func, aliases).rpartition(".")[2] in FILE_WRITERS


def _json_text(node: ast.AST, aliases: dict[str, str], bound: set[str]) -> bool:
    """Whether an expression holds a json.dumps call, or a name its function binds to an expression that does."""
    return any(
        (isinstance(inner, ast.Call) and qualified_name(inner.func, aliases) == JSON_DUMPS)
        or (isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load) and inner.id in bound)
        for inner in ast.walk(node)
    )


def _json_bindings(nodes: list[tuple[ast.AST, str]], aliases: dict[str, str]) -> dict[str, set[str]]:
    """Each scope's names bound to an expression holding a json.dumps call, to the fixed point, so a name bound to
    one of them is one too."""
    assignments = [
        (scope, target.id, node.value)
        for node, scope in nodes
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    ]
    bound: dict[str, set[str]] = {}
    while True:
        found = {
            (scope, name)
            for scope, name, value in assignments
            if name not in bound.get(scope, set()) and _json_text(value, aliases, bound.get(scope, set()))
        }
        if not found:
            return bound
        for scope, name in found:
            bound.setdefault(scope, set()).add(name)


def json_write_problems(root: Path, files: list[Path]) -> list[str]:
    """Each json.dump, and each write call that takes json.dumps text, outside json_writer that no JSON_WRITE_ALLOWED
    entry sanctions."""
    problems: list[str] = []
    paths = [path for path in claim_files(root, files) if path.relative_to(root).as_posix() != JSON_WRITER]
    if not paths:
        # No module read is no evidence that none writes JSON outside the writer.
        return [f"could not measure: no module the claims read was given, so no {JSON_DUMPS} write was looked for"]
    remedy = (
        "; call ejryan89-skill-core's json_writer, the one writer of JSON, which refuses NaN and a lone surrogate at "
        f"the write, or state why in {JSON_WRITE_ALLOWANCE}"
    )
    for _, name, tree in python_trees(paths, root, problems):
        aliases = import_aliases(tree)
        nodes = list(scoped_nodes(tree.body))
        bound = _json_bindings(nodes, aliases)
        found: list[tuple[int, str, str]] = []
        for node, scope in nodes:
            if not isinstance(node, ast.Call):
                continue
            if qualified_name(node.func, aliases) == JSON_DUMP:
                found.append((node.lineno, scope, f"writes JSON with {JSON_DUMP}{remedy}"))
            elif _writes(node, aliases) and any(
                _json_text(argument, aliases, bound.get(scope, set()))
                for argument in [*node.args, *(keyword.value for keyword in node.keywords)]
            ):
                found.append((node.lineno, scope, f"writes {JSON_DUMPS} text to a file or a stream{remedy}"))
        problems += sanctioned_problems(name, tree, JSON_WRITE_ALLOWANCE, found)
    return problems


# ctypes passes an undeclared foreign function's arguments and its result as C ints, which cuts a pointer-sized handle
# short, so every function a module calls through a library it loads has its argtypes and restype assigned in that
# module. A function every call gives no arguments needs no argtypes. Any other exception states why in a module-level
# CTYPES_ALLOWED = {scope: reason}.
LIBRARY_LOADERS = frozenset(
    {
        "ctypes.WinDLL",
        "ctypes.CDLL",
        "ctypes.OleDLL",
        "ctypes.PyDLL",
        "ctypes.windll.LoadLibrary",
        "ctypes.cdll.LoadLibrary",
    }
)
LIBRARY_NAMESPACES = frozenset({"ctypes.windll", "ctypes.cdll", "ctypes.oledll"})
# What a library or a foreign function holds that is no foreign function.
FOREIGN_MEMBERS = frozenset({"LoadLibrary", "argtypes", "restype", "errcheck", "_handle", "_name"})
FOREIGN_DECLARATIONS = ("argtypes", "restype")
CTYPES_ALLOWANCE = "CTYPES_ALLOWED"


class _ForeignLibraries:
    """What a module binds to a library ctypes loads: the names bound to one, the module's functions that return one,
    and the names bound to a function of one, through the module's imports."""

    def __init__(self, tree: ast.Module) -> None:
        self.aliases = import_aliases(tree)
        self.names: set[str] = set()
        self.loaders: set[str] = set()
        self.functions: dict[str, str] = {}
        assignments = [
            (target.id, node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
        ]
        returns = [
            (function.name, node.value)
            for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
            for node in ast.walk(function)
            if isinstance(node, ast.Return) and node.value is not None
        ]
        # A name bound to a loader's result is a library, and so is a name bound to that name, to the fixed point.
        while True:
            names = {name for name, value in assignments if self.library(value)} - self.names
            loaders = {name for name, value in returns if self.library(value)} - self.loaders
            if not names and not loaders:
                break
            self.names |= names
            self.loaders |= loaders
        self.functions = {name: function for name, value in assignments if (function := self.foreign(value))}

    def library(self, node: ast.expr) -> bool:
        """Whether an expression is a library ctypes loads."""
        if isinstance(node, ast.Call):
            return qualified_name(node.func, self.aliases) in LIBRARY_LOADERS or (
                isinstance(node.func, ast.Name) and node.func.id in self.loaders
            )
        if isinstance(node, ast.Attribute):
            return node.attr not in FOREIGN_MEMBERS and qualified_name(node.value, self.aliases) in LIBRARY_NAMESPACES
        return isinstance(node, ast.Name) and node.id in self.names

    def foreign(self, node: ast.expr) -> str | None:
        """The name of the foreign function an expression is, or None."""
        if isinstance(node, ast.Attribute) and node.attr not in FOREIGN_MEMBERS and self.library(node.value):
            return node.attr
        return self.functions.get(node.id) if isinstance(node, ast.Name) else None


def ctypes_declaration_problems(root: Path, files: list[Path]) -> list[str]:
    """Each call of a ctypes foreign function whose module does not assign it both argtypes and restype, argtypes
    excepted for a function every call gives no arguments, that no CTYPES_ALLOWED entry sanctions."""
    problems: list[str] = []
    for _, name, tree in python_trees(claim_files(root, files), root, problems):
        libraries = _ForeignLibraries(tree)
        declared: dict[str, set[str]] = {}
        calls: list[tuple[ast.Call, str, str]] = []
        for node, scope in scoped_nodes(tree.body):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr in FOREIGN_DECLARATIONS:
                        function = libraries.foreign(target.value)
                        if function is not None:
                            declared.setdefault(function, set()).add(target.attr)
            elif isinstance(node, ast.Call) and (function := libraries.foreign(node.func)) is not None:
                calls.append((node, scope, function))
        given = {function for call, _, function in calls if call.args or call.keywords}
        found: list[tuple[int, str, str]] = []
        for call, scope, function in calls:
            missing = [
                declaration
                for declaration in FOREIGN_DECLARATIONS
                if declaration not in declared.get(function, set()) and (declaration == "restype" or function in given)
            ]
            if missing:
                found.append(
                    (
                        call.lineno,
                        scope,
                        f"calls {function} through ctypes with no {' or '.join(missing)} assigned in the module, so "
                        f"ctypes passes it C ints; assign both, or state why in {CTYPES_ALLOWANCE}",
                    )
                )
        problems += sanctioned_problems(name, tree, CTYPES_ALLOWANCE, found)
    return problems


# A process runner returns a result whose exit status its caller must judge, so a call of one is never a bare
# expression statement, which would read a failure as success. The runners are ejryan89-skill-core's run_bounded, the
# deployer's run_tool, and GitClient.run; GitClient.output and every GitHubClient method raise on a failure, so they are
# not among them. A call that is deliberately fire-and-forget states why in a module-level
# RESULT_DISCARD_ALLOWED = {scope: reason}.
RESULT_RUNNERS = ("bounded_process.run_bounded", "platform_support.run_tool")
RESULT_CLIENT = "git_client.GitClient"
RESULT_CLIENT_METHOD = "run"
RESULT_DISCARD_ALLOWANCE = "RESULT_DISCARD_ALLOWED"


class _GitClients:
    """What a module binds to a GitClient, through its imports: each name assigned a GitClient(...) call or annotated
    as one, a parameter's included, and each attribute name a class or an instance assigns or annotates so."""

    def __init__(self, tree: ast.Module, resolver: CallableResolver) -> None:
        self.resolver = resolver
        self.names: set[str] = set()
        self.attributes: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if self.constructed(node.value):
                        self._bind(target)
            elif isinstance(node, ast.AnnAssign) and (self.annotated(node.annotation) or self.constructed(node.value)):
                self._bind(node.target)
            elif isinstance(node, ast.arg) and node.annotation is not None and self.annotated(node.annotation):
                self.names.add(node.arg)
        # A name a class body binds, such as a dataclass field, is an attribute of its instances.
        self.attributes |= {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            for statement in node.body
            if isinstance(statement, ast.AnnAssign)
            and (self.annotated(statement.annotation) or self.constructed(statement.value))
            and isinstance(target := statement.target, ast.Name)
        }

    def _bind(self, target: ast.expr) -> None:
        if isinstance(target, ast.Name):
            self.names.add(target.id)
        elif isinstance(target, ast.Attribute):
            self.attributes.add(target.attr)

    def constructed(self, node: ast.expr | None) -> bool:
        """Whether an expression is a GitClient(...) call."""
        return isinstance(node, ast.Call) and resolves_to(self.resolver.name(node.func), RESULT_CLIENT)

    def annotated(self, annotation: ast.expr) -> bool:
        """Whether an annotation names GitClient, alone or in a union or a subscript."""
        return any(
            isinstance(part, ast.Name | ast.Attribute) and resolves_to(self.resolver.name(part), RESULT_CLIENT)
            for part in ast.walk(annotation)
        )

    def holds(self, node: ast.expr) -> bool:
        """Whether an expression is a GitClient: one constructed in place, a name, or an attribute bound to one."""
        if isinstance(node, ast.Name):
            return node.id in self.names
        if isinstance(node, ast.Attribute):
            return node.attr in self.attributes
        return self.constructed(node)


def _discarded_runner(call: ast.Call, resolver: CallableResolver, clients: _GitClients) -> str | None:
    """The runner a call discards the result of, or None for a call of no runner."""
    called = resolver.name(call.func)
    runner = next((runner for runner in RESULT_RUNNERS if resolves_to(called, runner)), None)
    if runner is not None:
        return runner
    function = call.func
    if isinstance(function, ast.Attribute) and function.attr == RESULT_CLIENT_METHOD and clients.holds(function.value):
        return f"GitClient.{RESULT_CLIENT_METHOD}"
    return None


def discarded_result_problems(root: Path, files: list[Path]) -> list[str]:
    """Each expression statement that discards a process runner's result, which no RESULT_DISCARD_ALLOWED entry
    sanctions."""
    problems: list[str] = []
    for _, name, tree in python_trees(claim_files(root, files), root, problems):
        resolver = CallableResolver(tree)
        clients = _GitClients(tree, resolver)
        found = [
            (
                node.lineno,
                scope,
                f"discards the result of {runner}, whose exit status the caller must judge; check it, or state why "
                f"in {RESULT_DISCARD_ALLOWANCE}",
            )
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and (runner := _discarded_runner(node.value, resolver, clients))
        ]
        problems += sanctioned_problems(name, tree, RESULT_DISCARD_ALLOWANCE, found)
    return problems


# A time limit read with type=float accepts nan, inf, zero, and a negative number, and none of them is a limit, so an
# option that names a time takes ejryan89-skill-core's seconds_option.positive_seconds as its type. Any other states
# why in a module-level TIME_OPTION_ALLOWED = {scope: reason}.
TIME_OPTION_ALLOWANCE = "TIME_OPTION_ALLOWED"


def _float_time_option(call: ast.Call, aliases: dict[str, str]) -> str | None:
    """The name of the time an add_argument call reads with type=float, among its option strings, dest, and metavar,
    or None when it reads no time so."""
    time_words = {"timeout", "timeouts", "seconds", "secs", "minutes", "mins", "hours", "deadline"}
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "add_argument"):
        return None
    types = [qualified_name(keyword.value, aliases) for keyword in call.keywords if keyword.arg == "type"]
    if "float" not in types:
        return None
    names = [
        *call.args,
        *(keyword.value for keyword in call.keywords if keyword.arg in ("dest", "metavar")),
    ]
    for name in names:
        if (
            isinstance(name, ast.Constant)
            and isinstance(name.value, str)
            and time_words & set(re.split(r"[-_\s]+", name.value.lower()))
        ):
            return name.value
    return None


def time_option_problems(root: Path, files: list[Path]) -> list[str]:
    """Each add_argument that reads an option naming a time with type=float, which no TIME_OPTION_ALLOWED entry
    sanctions."""
    problems: list[str] = []
    for _, name, tree in python_trees(claim_files(root, files), root, problems):
        aliases = import_aliases(tree)
        found = [
            (
                node.lineno,
                scope,
                f"reads {option} with type=float, which accepts nan, inf, 0, and a negative number; use "
                f"ejryan89-skill-core's seconds_option.positive_seconds, or state why in {TIME_OPTION_ALLOWANCE}",
            )
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.Call) and (option := _float_time_option(node, aliases))
        ]
        problems += sanctioned_problems(name, tree, TIME_OPTION_ALLOWANCE, found)
    return problems


# A caller catches the whole error family a shared reader raises on content. Each ejryan89-skill-core module that
# exports CONTENT_ERRORS, what its readers raise on bad content, names those readers in CONTENT_READERS. A handler
# around a call of one, directly or through a repository function that lets part of the family escape, that catches
# part of the family or the call's OSError catches all of it, or the scope states why in a module-level
# CONTENT_ERRORS_ALLOWED = {scope: reason}; a function the allowance names lets no family escape, for one that cannot
# meet bad content.
CONTENT_ERRORS = "CONTENT_ERRORS"
CONTENT_READERS = "CONTENT_READERS"
CONTENT_ERRORS_ALLOWANCE = "CONTENT_ERRORS_ALLOWED"
SKILL_CORE_SCRIPTS = "skills/ejryan89-skill-core/scripts"
SUPPRESS = "contextlib.suppress"
# A class the policy compares: a repository class by its qualified name, or a standard-library class itself.
ClassKey = str | type


def _module_name(root: Path, path: Path) -> str:
    """The name a module is imported by: a package's as dotted from the root, every other script by its file name."""
    relative = path.relative_to(root)
    if relative.parts[0] in ("deployer", "tools") and len(relative.parts) == 2:
        return relative.parts[0] if relative.stem == "__init__" else f"{relative.parts[0]}.{relative.stem}"
    return relative.stem


def _content_aliases(tree: ast.Module, module: str) -> dict[str, str]:
    """import_aliases, with what a package module's relative imports bind and each name the module defines itself."""
    aliases = {
        node.name: f"{module}.{node.name}"
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    aliases |= {name: f"{module}.{name}" for name in _module_tuples(tree)}
    package = module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and package:
            base = f"{package}.{node.module}" if node.module else package
            aliases |= {alias.asname or alias.name: f"{base}.{alias.name}" for alias in node.names}
    return aliases | import_aliases(tree)


def _module_tuples(tree: ast.Module) -> dict[str, ast.Tuple]:
    """Each module-level name assigned a literal tuple, such as a family of exception classes."""
    found: dict[str, ast.Tuple] = {}
    for node in tree.body:
        target: ast.expr
        value: ast.expr | None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and isinstance(value, ast.Tuple):
            found[target.id] = value
    return found


def _content_readers(tree: ast.Module) -> list[str] | None:
    """The function names a module's CONTENT_READERS tuple holds, or None when it is no tuple of names."""
    value = _module_tuples(tree).get(CONTENT_READERS)
    if value is None:
        return None
    names = [item.value for item in value.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)]
    return names if len(names) == len(value.elts) else None


def _standard_class(qualified: str) -> type | None:
    """The built-in or standard-library class a qualified name stands for, or None."""
    module, _, attribute = qualified.rpartition(".")
    if not module:
        found = getattr(builtins, qualified, None)
        return found if isinstance(found, type) else None
    top = module.split(".")[0]
    if top not in sys.stdlib_module_names:
        return None
    try:
        found = getattr(importlib.import_module(module), attribute, None)
    except ImportError:
        return None
    return found if isinstance(found, type) else None


class _ContentModel:
    """The repository's modules as the content-errors policy reads them: its classes, its tuples of classes, its
    module-level functions, and each skill-core reader's family."""

    def __init__(self, root: Path, files: list[Path], problems: list[str]) -> None:
        self.trees: dict[str, tuple[str, ast.Module]] = {}
        self.aliases: dict[str, dict[str, str]] = {}
        for path, name, tree in python_trees(claim_files(root, files), root, problems):
            module = _module_name(root, path)
            if module in self.trees:
                # A module the model held under the same name would go unread, so the claim would not reach it.
                problems.append(
                    f"{name}: {self.trees[module][0]} is imported as {module} too, so the content-errors policy, "
                    "which reads modules by the name they are imported as, cannot read both; rename one"
                )
                continue
            self.trees[module] = (name, tree)
            self.aliases[module] = _content_aliases(tree, module)
        self.bases = {
            f"{module}.{node.name}": [qualified_name(base, self.aliases[module]) for base in node.bases]
            for module, (_, tree) in self.trees.items()
            for node in tree.body
            if isinstance(node, ast.ClassDef)
        }
        self.tuples = {
            f"{module}.{name}": (module, value)
            for module, (_, tree) in self.trees.items()
            for name, value in _module_tuples(tree).items()
        }
        self.functions = {
            f"{module}.{node.name}": (module, node)
            for module, (_, tree) in self.trees.items()
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        self.families: dict[str, dict[ClassKey, str]] = {}
        for module, (name, tree) in self.trees.items():
            if not posix_within(name, SKILL_CORE_SCRIPTS) or f"{module}.{CONTENT_ERRORS}" not in self.tuples:
                continue
            family = self.classes(f"{module}.{CONTENT_ERRORS}")
            if not family:
                problems.append(f"{name}: {CONTENT_ERRORS} must be a tuple of exception classes its imports name")
                continue
            readers = _content_readers(tree)
            defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}
            if readers is None or not readers or not set(readers) <= defined:
                problems.append(
                    f"{name}: {CONTENT_READERS} must be a tuple naming each function the module defines that raises "
                    f"its {CONTENT_ERRORS} on content"
                )
                continue
            for reader in readers:
                self.families[f"{module}.{reader}"] = dict.fromkeys(family, f"{module}.{CONTENT_ERRORS}")
        if not self.families:
            # With no reader every handler passes, which would hold nothing; say so instead.
            problems.append(
                f"{SKILL_CORE_SCRIPTS} names no reader in a {CONTENT_READERS} beside its {CONTENT_ERRORS}, so the "
                "content-errors policy could check no handler"
            )
        self._ancestors: dict[ClassKey, frozenset[ClassKey]] = {}

    def ancestors(self, key: ClassKey) -> frozenset[ClassKey]:
        """A class and every class it derives from, as far as the repository and the standard library name them."""
        if key not in self._ancestors:
            found: set[ClassKey] = set(key.__mro__) if isinstance(key, type) else {key}
            for base in [] if isinstance(key, type) else self.bases.get(key, []):
                resolved = base if base in self.bases else _standard_class(base)
                if resolved is not None:
                    found |= self.ancestors(resolved)
            self._ancestors[key] = frozenset(found)
        return self._ancestors[key]

    def classes(self, qualified: str, depth: int = 0) -> list[ClassKey] | None:
        """The classes a qualified name stands for: a class, or each class a tuple of them holds; None for a name it
        cannot resolve."""
        if qualified in self.bases:
            return [qualified]
        if qualified in self.tuples and depth < len(self.tuples):
            module, value = self.tuples[qualified]
            return self.expression_classes(value, module, depth + 1)
        standard = _standard_class(qualified)
        return None if standard is None else [standard]

    def expression_classes(self, node: ast.expr | None, module: str, depth: int = 0) -> list[ClassKey] | None:
        """The classes an except clause or suppress argument names, through the module's imports, or None."""
        if node is None:
            return [BaseException]
        if isinstance(node, ast.Tuple):
            found: list[ClassKey] = []
            for element in node.elts:
                classes = self.expression_classes(
                    element.value if isinstance(element, ast.Starred) else element, module, depth
                )
                if classes is None:
                    return None
                found += classes
            return found
        qualified = qualified_name(node, self.aliases[module])
        return self.classes(qualified, depth) if qualified else None

    def callee(self, call: ast.Call, module: str) -> str:
        """The reader or repository function a call names, by its qualified name, or ""."""
        qualified = qualified_name(call.func, self.aliases[module])
        return qualified if qualified in self.families or qualified in self.functions else ""


def class_text(key: ClassKey) -> str:
    """A class as a finding names it: a repository class by its qualified name, a built-in one bare."""
    if isinstance(key, str):
        return key
    return key.__qualname__ if key.__module__ == "builtins" else f"{key.__module__}.{key.__qualname__}"


@dataclass(frozen=True)
class _Guard:
    """A handler, an except clause's or a suppress's, by its line and the classes it catches, None for one the policy
    cannot resolve, around what its body calls; what its handlers and other blocks call runs outside it."""

    line: int
    caught: tuple[ClassKey, ...] | None
    inner: tuple[_Guard | str, ...]


class _ContentEscapes:
    """What each repository function lets escape of the readers' families, and each handler that misses part of one.
    Each function is read once into the readers and functions it calls and the handlers around them, its shape, and
    what escapes is then derived from the shapes until no function's escapes grow."""

    def __init__(self, model: _ContentModel) -> None:
        self.model = model
        self.allowed = {
            module: module_allowance(tree, CONTENT_ERRORS_ALLOWANCE) or {} for module, (_, tree) in model.trees.items()
        }
        # Every function and method of each module, by the scope scoped_nodes names it, with its shape.
        self.scoped = {
            module: [
                (scope, node, self.shape(node.body, module))
                for node, scope in scoped_nodes(tree.body)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            ]
            for module, (_, tree) in model.trees.items()
        }
        self.shapes = {f"{module}.{scope}": shape for module, found in self.scoped.items() for scope, _, shape in found}
        self.escapes: dict[str, dict[ClassKey, str]] = {qualified: {} for qualified in model.functions}
        changed = True
        while changed:
            changed = False
            for qualified, (module, node) in model.functions.items():
                found = {} if node.name in self.allowed[module] else self.escaping(self.shapes[qualified], None, "")
                if found != self.escapes[qualified]:
                    self.escapes[qualified], changed = found, True

    def shape(self, body: Sequence[ast.AST], module: str) -> tuple[_Guard | str, ...]:
        """The calls of readers and repository functions in `body`, outside any nested definition, and the handlers
        around them, as `escaping` reads them."""
        found: list[_Guard | str] = []
        pending = list(body)
        while pending:
            node = pending.pop()
            if isinstance(node, ast.Try | ast.TryStar | ast.With | ast.AsyncWith):
                found += self.guard_shape(node, module)
                continue
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
                continue
            if isinstance(node, ast.Call) and (callee := self.model.callee(node, module)):
                found.append(callee)
            pending += ast.iter_child_nodes(node)
        return tuple(found)

    def guard_shape(self, node: ast.Try | ast.TryStar | ast.With | ast.AsyncWith, module: str) -> list[_Guard | str]:
        """The shape of a try statement or a with statement, whose suppress, if it enters one, is a handler."""
        if isinstance(node, ast.Try | ast.TryStar):
            inner = self.shape(node.body, module)
            outside = self.shape([*(s for h in node.handlers for s in h.body), *node.orelse, *node.finalbody], module)
            if not node.handlers:
                return [*inner, *outside]
            caught: list[ClassKey] | None = []
            for handler in node.handlers:
                classes = self.model.expression_classes(handler.type, module)
                reraised = len(handler.body) == 1 and isinstance(handler.body[0], ast.Raise) and not handler.body[0].exc
                if classes is None or caught is None:
                    caught = None
                elif not reraised:
                    caught += classes
            if not inner:
                return list(outside)
            return [_Guard(node.lineno, None if caught is None else tuple(caught), inner), *outside]
        entered = self.shape([item.context_expr for item in node.items], module)
        suppressed: list[ClassKey] | None = []
        for item in node.items:
            call = item.context_expr
            if isinstance(call, ast.Call) and qualified_name(call.func, self.model.aliases[module]) == SUPPRESS:
                classes = self.model.expression_classes(ast.Tuple(elts=list(call.args)), module)
                suppressed = None if classes is None or suppressed is None else suppressed + classes
        inner = self.shape(node.body, module)
        if suppressed == [] or not inner:
            return [*entered, *inner]
        return [*entered, _Guard(node.lineno, None if suppressed is None else tuple(suppressed), inner)]

    def handled(self, caught: tuple[ClassKey, ...] | None, inner: dict[ClassKey, str]) -> dict[ClassKey, str]:
        """The members of `inner` no class of `caught` catches; an unresolved handler is taken to catch them all."""
        if caught is None:
            return {}
        return {key: origin for key, origin in inner.items() if not set(caught) & self.model.ancestors(key)}

    def touches(self, caught: tuple[ClassKey, ...], inner: dict[ClassKey, str]) -> bool:
        """Whether a handler names part of a family that escapes into it, or the reader's OSError."""
        return any(
            OSError in self.model.ancestors(handler)
            or any(handler in self.model.ancestors(key) or key in self.model.ancestors(handler) for key in inner)
            for handler in caught
        )

    def escaping(
        self, shape: tuple[_Guard | str, ...], sites: list[tuple[int, str, str]] | None, scope: str
    ) -> dict[ClassKey, str]:
        """What escapes a shape, recording in `sites` each handler that catches part of a family or the OSError of a
        reader whose family it misses."""
        found: dict[ClassKey, str] = {}
        for item in shape:
            if isinstance(item, str):
                found |= self.model.families.get(item) or self.escapes.get(item, {})
                continue
            inner = self.escaping(item.inner, sites, scope)
            left = self.handled(item.caught, inner)
            if sites is not None and item.caught is not None and left and self.touches(item.caught, inner):
                missing = ", ".join(sorted(class_text(key) for key in left))
                families = ", ".join(sorted(set(left.values())))
                sites.append(
                    (
                        item.line,
                        scope,
                        f"catches part of what a reader it calls raises, or its OSError, but not {missing}; catch the "
                        f"whole family, {families}, or state why in {CONTENT_ERRORS_ALLOWANCE}",
                    )
                )
            found |= left
        return found


def content_error_problems(root: Path, files: list[Path]) -> list[str]:
    """Each handler around a reader that exports CONTENT_ERRORS, called directly or through a repository function,
    that catches part of the reader's family, or the call's OSError, without the rest, and no CONTENT_ERRORS_ALLOWED
    entry sanctions; then each entry that sanctions nothing."""
    problems: list[str] = []
    model = _ContentModel(root, files, problems)
    escapes = _ContentEscapes(model)
    for module, (name, tree) in model.trees.items():
        sites: list[tuple[int, str, str]] = []
        for scope, node, shape in escapes.scoped[module]:
            if scope not in escapes.allowed[module]:
                escapes.escaping(shape, sites, scope)
                continue
            # A sanctioned function is used when it holds a handler the policy would report or lets a family escape.
            held: list[tuple[int, str, str]] = []
            if escapes.escaping(shape, held, scope) or held:
                sites.append((node.lineno, scope, "is sanctioned"))
        problems += sanctioned_problems(name, tree, CONTENT_ERRORS_ALLOWANCE, sites)
    return problems


def ruff_format_check(root: Path, targets: list[str]) -> None:
    """Fail, naming each file, when ruff format would change any Python file under the targets in root."""
    ruff = find_ruff()
    if ruff is None:
        raise AssertionError(f"ruff was not found: {platform_support.install_hint('ruff')}")
    # Concise output names one file per line instead of printing each diff; no cache is written into the tree.
    try:
        run_process([ruff, "format", "--check", "--output-format", "concise", "--no-cache", *targets], cwd=root)
    except AssertionError as exc:
        raise AssertionError(
            f"{exc}\nRun `python -m ruff format` on the files named above, using requirements-dev.txt's ruff."
        ) from exc


def ruff_lint_check(root: Path, targets: list[str]) -> None:
    """Fail, naming each finding, when ruff check finds a violation of root's rule set under the targets."""
    ruff = find_ruff()
    if ruff is None:
        raise AssertionError(f"ruff was not found: {platform_support.install_hint('ruff')}")
    try:
        run_process([ruff, "check", "--output-format", "concise", "--no-cache", *targets], cwd=root)
    except AssertionError as exc:
        raise AssertionError(
            f"{exc}\nFix the findings named above; `python -m ruff check --fix` applies the ones ruff marks safe."
        ) from exc


def mypy_type_check(cwd: Path, targets: list[str], configuration: Path, *, strict: bool = False) -> None:
    """Fail, naming each error, when mypy finds a type error under the targets, checked from cwd, in strict mode when
    `strict` is set."""
    mypy = find_mypy()
    if mypy is None:
        raise AssertionError(f"mypy was not found: {platform_support.install_hint('mypy')}")
    with contextlib.ExitStack() as stack:
        root = mypy_cache_root(os.environ)
        if root is None:
            cache = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="mypy-cache-")))
        else:
            cache = mypy_cache_directory(root, cwd, targets, strict=strict)
        command = [mypy, "--config-file", str(configuration), "--cache-dir", str(cache)]
        try:
            run_process([*command, *(["--strict"] if strict else []), *targets], cwd=cwd)
        except AssertionError as exc:
            raise AssertionError(
                f"{exc}\nFix each error named above; a `# type: ignore[<code>]` that must stay states its reason in a "
                "comment after it."
            ) from exc


def mypy_cache_root(environment: Mapping[str, str]) -> Path | None:
    """The folder MYPY_CACHE_VARIABLE names in `environment`, or None when it is unset or empty. Raises AssertionError,
    naming the variable, for a relative path, which each check would read from its own working folder."""
    value = environment.get(MYPY_CACHE_VARIABLE, "")
    if not value:
        return None
    if not Path(value).is_absolute():
        raise AssertionError(f"{MYPY_CACHE_VARIABLE} must be an absolute path, not {value!r}")
    return Path(value)


def mypy_cache_directory(root: Path, cwd: Path, targets: list[str], *, strict: bool) -> Path:
    """The cache folder under `root` of the type check run from `cwd` on `targets`: one per check, since each check
    differs in one of them, and the same at every run, so a restored cache is found again."""
    place = cwd.relative_to(REPOSITORY_ROOT).as_posix() if within(cwd, REPOSITORY_ROOT) else cwd.as_posix()
    check = "\0".join([place, *targets, "strict" if strict else "default"])
    return root / hashlib.sha256(check.encode("utf-8")).hexdigest()[:16]


def type_check_skill_roots(root: Path = REPOSITORY_ROOT) -> list[Path]:
    """The scripts directory of each shipped or repository skill that holds a Python module or regression suite."""
    return [scripts for scripts in skill_script_directories(root) if any(scripts.glob("*.py"))]


def type_check_jobs() -> list[Job]:
    configuration = REPOSITORY_ROOT / "pyproject.toml"
    # A list strict_area_problems refuses checks no area in strict mode until it is fixed.
    strict = set(strict_type_check_areas(REPOSITORY_ROOT) or ())
    core = f"static type check (mypy {', '.join(TYPE_CHECK_ROOTS)})"
    jobs = [
        Job(
            core,
            core,
            UNSPLIT_SUITE_WEIGHT,
            functools.partial(mypy_type_check, REPOSITORY_ROOT, list(TYPE_CHECK_ROOTS), configuration, strict=False),
        )
    ]
    # The roots checked together stay in that run, which reads a strict one's modules anyway through the imports of
    # the others; a strict one also gets a run of its own, which adds --strict.
    for top in TYPE_CHECK_ROOTS:
        if top in strict:
            name = f"static type check (mypy --strict {top})"
            check = functools.partial(mypy_type_check, REPOSITORY_ROOT, [top], configuration, strict=True)
            jobs.append(Job(name, name, UNSPLIT_SUITE_WEIGHT, check))
    # A skill's job keeps its name in strict mode, so the seconds recorded for it still weigh it.
    for root in type_check_skill_roots():
        name = f"static type check (mypy {relative(root)})"
        check = functools.partial(mypy_type_check, root, ["."], configuration, strict=relative(root) in strict)
        jobs.append(Job(name, name, UNSPLIT_SUITE_WEIGHT, check))
    return jobs


def static_format_check() -> None:
    ruff_format_check(REPOSITORY_ROOT, list(FORMAT_ROOTS))


def static_lint_check() -> None:
    ruff_lint_check(REPOSITORY_ROOT, list(FORMAT_ROOTS))


class PythonChecksPolicies(unittest.TestCase):
    def test_lint_rule_set_is_pinned_and_exempts_only_assert_in_the_suites(self) -> None:
        configuration = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["ruff"]
        self.assertEqual(120, configuration["line-length"])
        lint = configuration["lint"]
        self.assertEqual(
            [
                *("E", "F", "W", "I", "UP", "B", "SIM", "C901", "PLR0915", "PLR0913", "BLE001", "PTH", "RUF"),
                *("S1", "S2", "S3", "S5", "S601", "S602", "S604", "S605", "S606", "S608", "S609", "S61", "S7"),
                *("PT001", "PT002", "PT003", "PT006", "PT007", "PT008", "PT01", "PT020", "PT021", "PT022"),
                *("PT023", "PT024", "PT025", "PT026", "PT028", "PT03"),
                *("RET", "PIE", "C4", "PERF", "A"),
            ],
            lint["select"],
        )
        # Every bandit rule but S603 and S607, and every stable pytest-style rule but PT009 and PT027, is selected;
        # "Python checks" in CONTRIBUTING.md says why.
        for unselected in ("S603", "S607", "PT009", "PT027"):
            self.assertEqual([], [prefix for prefix in lint["select"] if unselected.startswith(prefix)], unselected)
        for selected in (
            *("PT001", "PT002", "PT003", "PT006", "PT007", "PT008", "PT010", "PT011", "PT012", "PT013"),
            *("PT014", "PT015", "PT016", "PT017", "PT018", "PT019", "PT020", "PT021", "PT022", "PT023"),
            *("PT024", "PT025", "PT026", "PT028", "PT030", "PT031"),
        ):
            self.assertTrue(any(selected.startswith(prefix) for prefix in lint["select"]), selected)
        # A finding is fixed, or suppressed on its line with the reason beside it; no rule or file is exempt but S101
        # in the regression suites, which pytest runs and never under -O, and which no deployment ships.
        self.assertEqual(["mccabe", "per-file-ignores", "pylint", "select"], sorted(lint))
        self.assertEqual(
            {
                "tests/**/*.py": ["S101"],
                "skills/*/scripts/test_*.py": ["S101"],
                ".claude/skills/*/scripts/test_*.py": ["S101"],
            },
            lint["per-file-ignores"],
        )
        self.assertEqual(["max-complexity"], sorted(lint["mccabe"]))
        self.assertEqual(["max-args", "max-statements"], sorted(lint["pylint"]))
        self.assertEqual(["exclude", "format", "line-length", "lint", "target-version"], sorted(configuration))
        # ruff's default exclusions are emptied, so every Python file under the roots is formatted and linted.
        self.assertEqual([], configuration["exclude"])

    def test_type_check_configuration_is_pinned(self) -> None:
        configuration = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["mypy"]
        self.assertEqual("3.11", configuration["python_version"])
        self.assertIs(True, configuration["explicit_package_bases"])
        # Default strictness, and no module, suite, or import is exempt; every root is also checked with --strict, as
        # [tool.validation] lists them. No platform: each host checks the code as it runs there, so every platform's
        # branches are checked on the platform that runs them.
        # The optional error codes beyond --strict, each a check every root passes; warn_unreachable is not set.
        self.assertEqual(
            [
                "deprecated",
                "exhaustive-match",
                "ignore-without-code",
                "possibly-undefined",
                "redundant-expr",
                "redundant-self",
                "truthy-bool",
                "truthy-iterable",
                "unused-awaitable",
            ],
            configuration["enable_error_code"],
        )
        self.assertEqual(
            ["enable_error_code", "explicit_package_bases", "mypy_path", "python_version"], sorted(configuration)
        )

    def test_strict_type_check_areas_only_grow(self) -> None:
        # A pull request that makes an area strict adds it to STRICT_TYPE_CHECK_AREAS and to pyproject.toml together.
        configuration = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]
        self.assertEqual(["mypy-strict"], sorted(configuration["validation"]))
        self.assertEqual([], strict_area_problems(REPOSITORY_ROOT))

    def test_mypy_reads_every_python_file_under_its_roots(self) -> None:
        self.assertEqual([], type_check_skipped_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_mypy_path_names_each_scripts_directory_put_on_sys_path(self) -> None:
        self.assertEqual([], mypy_path_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_repository_has_no_type_ignore_without_a_reason(self) -> None:
        self.assertEqual([], type_ignore_without_reason(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_ratchets_only_go_down(self) -> None:
        # A pull request may lower a ceiling with its literal in CEILINGS or BLIND_EXCEPT_CEILING, never raise it.
        lint = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["ruff"]["lint"]
        blind_excepts = blind_except_noqa(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT))
        self.assertEqual([], ratchet_problems(lint, blind_excepts))

    def test_repository_has_no_noqa_without_a_reason(self) -> None:
        self.assertEqual([], noqa_without_reason(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_repository_turns_no_python_check_off(self) -> None:
        self.assertEqual([], python_suppression_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_repository_suppresses_neither_ceiling(self) -> None:
        self.assertEqual([], ceiling_noqa(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_repository_comments_state_reasons_instead_of_issue_numbers(self) -> None:
        self.assertEqual([], issue_numbers_in_comments(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_every_process_starts_without_git_repository_variables(self) -> None:
        self.assertEqual([], process_environment_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_link_probe_is_the_one_link_test(self) -> None:
        self.assertEqual([], link_test_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_claim_policies_read_every_module_their_rules_speak_of(self) -> None:
        modules = formatted_modules(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT))
        self.assertEqual([], scope_problems("the claim policies", modules, CLAIM_ROOTS, claim_omissions()))

    def test_json_reader_is_the_one_parse_of_json(self) -> None:
        self.assertEqual([], json_parse_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_each_handler_around_a_reader_catches_its_whole_content_family(self) -> None:
        self.assertEqual([], content_error_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_whole_number_is_the_one_reader_of_a_number_from_text(self) -> None:
        self.assertEqual([], int_parse_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_json_writer_is_the_one_writer_of_json(self) -> None:
        self.assertEqual([], json_write_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_every_ctypes_foreign_function_called_is_declared(self) -> None:
        self.assertEqual([], ctypes_declaration_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_no_process_runner_result_is_discarded_unchecked(self) -> None:
        self.assertEqual([], discarded_result_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_every_time_option_refuses_what_is_no_limit(self) -> None:
        self.assertEqual([], time_option_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

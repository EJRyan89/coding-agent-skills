"""The Python static checks: ruff format and lint, mypy and its mypy_path, the noqa and type: ignore rules, the rule
against issue numbers in comments and docstrings, and the policies that hold the code's universal claims: every process
starts without git's repository-selecting variables, link_probe is the one link test, every ctypes foreign function a
module calls is declared, no process runner's result is discarded unchecked, a handler around a shared reader
catches the whole family of errors the reader raises on content, and no name is reached by a value: no dynamic lookup,
no owner-only callable referenced outside its owners, and no policed call given an argument its readers cannot
follow."""

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
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

import file_presence
from bounded_process import REPOSITORY_VARIABLES
from call_tables import (
    EVENT_LOOP,
    EVENT_LOOP_STARTS,
    LAST,
    OTHER_OPENS,
    PATH_METHODS,
    PROCESS_STARTS,
    TEXT_METHODS,
    TEXT_OPENERS,
    ProcessStart,
    TextOpener,
)
from git_client import GLOBAL_VALUE_OPTIONS
from job_pool import UNSPLIT_SUITE_WEIGHT, Job
from path_containment import posix_within, relative_within, within
from runtime_support import names_git
from toolchain import find_mypy, find_ruff, mypy_job, ruff_job
from validation_support import (
    ATTRIBUTE_GETTERS,
    ATTRIBUTE_LOOKUPS,
    ATTRIBUTE_READERS,
    BUILTIN_NAMES,
    COMMON_PATH_FUNCTIONS,
    DEFINITIONS,
    INDIRECTION_ATTRIBUTES,
    INDIRECTION_CALLABLES,
    LOOKUP_HOOKS,
    MODULE_SCOPE,
    NAME_LOOKUPS,
    PARTIAL_CALLS,
    REPOSITORY_ROOT,
    CallableResolver,
    ResolvedCall,
    UnreadablePython,
    bound_values,
    call_argument,
    import_aliases,
    is_test_script,
    listed,
    module_allowance,
    module_imports,
    parse_source,
    path_order,
    place_key,
    python_source,
    python_trees,
    qualified_name,
    relative,
    repository_files,
    resolved_calls,
    resolved_references,
    scope_problems,
    scoped_nodes,
    scripts_put_on_path,
    skill_script_directories,
    under,
)

from deployer import platform_support

# Commands whose program the commands reader cannot read, by the scope that starts them, and why.
COMMANDS_ALLOWED = {
    "_standard_class": "looks a standard-library class up by its name for the policy reading it, and starts nothing",
}
INDIRECTION_ALLOWED = {
    "_standard_class": "looks the built-in or standard-library class a policy reads up by the qualified name the code "
    "under check gives it, and imports only a standard-library module",
}


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
        relative = relative_within(path.parent, checked_from)
        skipped = [
            part
            for part in (() if relative is None else relative.parts)
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
    return any(
        file_presence.is_present(path.parent / f"{found.module.split('.')[0]}.py", other_is_absent=True)
        for found in module_imports(tree)
    )


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
    present = skill_roots | {top for top in TYPE_CHECK_ROOTS if file_presence.is_occupied(root / top)}
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
# built, or states why in a module-level PROCESS_ENV_ALLOWED = {scope: reason}. The starts are call_tables'
# PROCESS_STARTS: one that takes no environment inherits this process's, and one that clears it itself needs none. A
# name holds a cleared environment only while every read of it in the module, a closure's included, leaves the
# mapping with no key from elsewhere; a reach through globals() or vars() is not read.
CLEARED_ENVIRONMENT = "bounded_process.without_repository_variables"
PROCESS_RUNNER = "skills/ejryan89-skill-core/scripts/bounded_process.py"
PROCESS_ENV_ALLOWANCE = "PROCESS_ENV_ALLOWED"
# The methods a cleared environment may be called by on its own name, which read it, copy it, or remove keys, and so
# add none.
ENVIRONMENT_READS = frozenset({"clear", "copy", "get", "items", "keys", "pop", "popitem", "values"})


def _clears(value: ast.expr | None, resolver: CallableResolver) -> bool:
    """Whether an expression is a call of without_repository_variables."""
    return isinstance(value, ast.Call) and resolves_to(resolver.name(value.func), CLEARED_ENVIRONMENT)


def _clear_key(key: ast.expr | None) -> bool:
    """Whether a key is a constant that names none of git's repository-selecting variables, in any case."""
    return (
        isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.upper() not in REPOSITORY_VARIABLES
    )


def _clear_mapping(value: ast.expr) -> bool:
    """Whether an expression is a dict literal whose every key is clear, so adding it to an environment adds none of
    git's repository-selecting variables; a `**` unpacking in it adds keys no one can read."""
    return isinstance(value, ast.Dict) and all(_clear_key(key) for key in value.keys)


def _updated_name(node: ast.AST) -> tuple[ast.Name, bool] | None:
    """The name a statement or call changes in place, with whether the change is proven to add no repository variable:
    an `update` of clear dict literals and clear keywords, a `setdefault` or a subscript assignment of a clear key, or
    `|=` a clear dict literal. None for a node that changes no name in place."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        if node.func.attr == "update":
            proven = all(_clear_mapping(argument) for argument in node.args) and all(
                keyword.arg is not None and keyword.arg.upper() not in REPOSITORY_VARIABLES for keyword in node.keywords
            )
            return node.func.value, proven
        if node.func.attr == "setdefault":
            return node.func.value, bool(node.args) and _clear_key(node.args[0])
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store) and isinstance(node.value, ast.Name):
        return node.value, _clear_key(node.slice)
    if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
        return node.target, isinstance(node.op, ast.BitOr) and _clear_mapping(node.value)
    return None


def _keeps_clear(node: ast.Name, parents: dict[int, ast.AST], given: set[int]) -> bool:
    """Whether a read of a name leaves the mapping bound to it without a key it did not hold, or with only clear ones:
    the environment a process start is given, a subscript that reads, deletes, or assigns a clear key, a membership
    test, an ENVIRONMENT_READS method called on it, or an update or a setdefault proven clear. Any other read hands the
    mapping on, to another name, a call, a container, or a stored or unbound method, which may change it unseen."""
    if id(node) in given:
        return True
    parent = parents.get(id(node))
    if isinstance(parent, ast.Subscript) and parent.value is node:
        updated = _updated_name(parent)
        return updated is None or updated[1]
    if isinstance(parent, ast.Compare):
        return any(
            item is node and isinstance(operator, ast.In | ast.NotIn)
            for operator, item in zip(parent.ops, parent.comparators, strict=True)
        )
    if not (isinstance(parent, ast.Attribute) and isinstance(call := parents.get(id(parent)), ast.Call)):
        return False
    if call.func is not parent:
        return False
    if parent.attr in ENVIRONMENT_READS:
        return True
    updated = _updated_name(call)
    return updated is not None and updated[1]


def _cleared_names(tree: ast.Module, resolver: CallableResolver, given: set[int]) -> set[tuple[int, str]]:
    """Each name, by the scope that binds it, bound only to an environment without_repository_variables builds and
    changed in place, if at all, only by keys that name none of git's repository-selecting variables, wherever in the
    module the name is read, closures included. A parameter is bound by the caller, so it is none. An update in place,
    such as `env.update(...)`, `env[key] = ...`, or `env |= ...`, keeps the name cleared only when every key it can add
    is such a constant, as `env.update({"LC_ALL": "C"})`; one that adds keys from elsewhere, such as
    `env.update(os.environ)`, does not, and neither does a read that hands the mapping on (_keeps_clear)."""
    assigned: dict[int, ast.expr] = {}
    augmented: dict[int, ast.AugAssign] = {}
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        parents |= {id(child): node for child in ast.iter_child_nodes(node)}
        if isinstance(node, ast.Assign):
            assigned |= {id(target): node.value for target in node.targets if isinstance(target, ast.Name)}
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
            assigned[id(node.target)] = node.value
        elif isinstance(node, ast.AugAssign):
            augmented[id(node.target)] = node
    bound: dict[tuple[int, str], list[bool]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.arg):
            name, cleared = node.arg, False
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            # A proven augmented union leaves the name as its bindings make it; an unproven one makes it not cleared.
            change = _updated_name(augmented[id(node)]) if id(node) in augmented else None
            name = node.id
            cleared = change[1] if change is not None else _clears(assigned.get(id(node)), resolver)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name, cleared = node.id, _keeps_clear(node, parents, given)
        else:
            continue
        scope = resolver.scopes.get(id(node))
        if scope is not None and (owner := scope.owner(name)) is not None:
            bound.setdefault((id(owner), name), []).append(cleared)
    return {key for key, values in bound.items() if all(values)}


def _cleared_here(env: ast.expr | None, resolver: CallableResolver, cleared: set[tuple[int, str]]) -> bool:
    """Whether a start's environment is a name cleared by the scope the start runs in, a comprehension's enclosing
    one for a start inside a comprehension, rather than by an enclosing function or the module."""
    if not isinstance(env, ast.Name) or (scope := resolver.scopes.get(id(env))) is None:
        return False
    while scope.is_comprehension and scope.parent is not None:
        scope = scope.parent
    return scope.owner(env.id) is scope and (id(scope), env.id) in cleared


def _process_start(call: ast.Call, resolver: CallableResolver) -> tuple[str, ProcessStart] | None:
    """The PROCESS_STARTS entry a call names, through the module's imports or, for an event loop's runner, by its
    method on any receiver, with its name, or None."""
    named = resolver.name(call.func)
    if named in PROCESS_STARTS:
        return named, PROCESS_STARTS[named]
    if isinstance(call.func, ast.Attribute) and call.func.attr in EVENT_LOOP_STARTS:
        method = f"{EVENT_LOOP}.{call.func.attr}"
        return method, PROCESS_STARTS[method]
    return None


def _environment_argument(call: ast.Call, start: ProcessStart) -> ast.expr | None:
    """The environment a process start is given, or None when it is given none or can take none."""
    if start.environment is None:
        return None
    position, keyword = start.environment
    if position != LAST:
        return call_argument(call, position, keyword)
    last = call.args[-1] if call.args and not isinstance(call.args[-1], ast.Starred) else None
    return call_argument(call, None, keyword) or last


def process_environment_problems(root: Path, files: list[Path]) -> list[str]:
    """Each process start outside bounded_process whose environment may keep git's repository-selecting variables,
    that no PROCESS_ENV_ALLOWED entry sanctions: one given no env, which inherits this process's, or an env that is
    neither a without_repository_variables call nor a name its function binds only to one."""
    problems: list[str] = []
    paths = [path for path in claim_files(root, files) if path.relative_to(root).as_posix() != PROCESS_RUNNER]
    for _, name, tree in python_trees(paths, root, problems):
        resolver = CallableResolver(tree)
        starts = [
            (node, scope, named, _environment_argument(node, named[1]))
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.Call) and (named := _process_start(node, resolver)) is not None
        ]
        cleared = _cleared_names(tree, resolver, {id(env) for *_, env in starts if isinstance(env, ast.Name)})
        found: list[tuple[int, str, str]] = []
        for node, scope, (start, table), env in starts:
            if table.clears or _clears(env, resolver) or _cleared_here(env, resolver, cleared):
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


# ejryan89-skill-core's file_presence is the one reader of whether a path a reader may find absent is there, and of what
# a folder holds, and lets a stat or a listing that fails through, where every presence predicate returns False on an
# OSError from Python 3.14 on and Path.glob matches nothing in a folder it cannot list from 3.15 on. A predicate whose
# False is itself the answer reads a path it could not stat as nothing there: `if not p.is_file(): return None`, `if not
# p.is_dir(): continue`, `if not (p.is_file() and q): continue`, `while not p.is_dir(): p = p.parent`, `v if p.exists()
# else []`, `return p.exists()`, `if p.is_file(): return p` falling through to `return None`, `[e for e in es if
# e.is_file()]`, `filter(os.path.isfile, es)`, or `p.exists() and any(p.iterdir())`, a name bound only to a predicate's
# answer included, and a branch that only logs before its empty answer. So does a glob, an os.walk or a path's walk that
# ignores the folders it cannot list, a removal that discards its errors, and an except clause or a contextlib.suppress
# that answers a failed stat or listing with nothing. An if on a predicate with no else is left out unless its branch
# returns a value and its False goes on to an empty return or the function's end, since its False otherwise goes on to
# code that meets the same path or gives a value. Any such site outside file_presence and link_probe states why in a
# module-level PRESENCE_ALLOWED = {scope: reason}, as where the caller just made the path.
# Stated limits, which the policy's next widening reads: `if not P and Q: return None`; a predicate handed to map, as
# `any(map(os.path.exists, ps))`; a predicate bound to a name this does not follow or reached through getattr; a
# returned sentinel name, as `result = []` then `if not P: return result`; `P == False` and `P is False`; an rmtree
# onerror or onexc that swallows the error; a read whose OSError handler answers nothing; a branch that does work other
# than logging before its empty answer, read as recording the case; an `if P:` with no else guarding work whose result
# is an answer, as `if path.is_file(): tokens.update(...)`; an except naming its exceptions through a name bound to a
# tuple, as `ERRS = (OSError,)` then `except ERRS:`, which this does not follow; a refusal whose reason says the path is
# not there, as `if not P: raise Error("not found")`, read as any refusal though it reports a failed stat as absent; and
# a `warn(...)` imported by `from warnings import warn`, which this does not read as a log, so a branch or handler
# holding one passes. The link tests are no presence predicates here: LINK_TESTS refuses each outside link_probe however
# its answer is used, and link_probe lets a failed stat through.
PRESENCE_PREDICATES = frozenset(
    {"exists", "is_file", "is_dir", "is_mount", "is_socket", "is_fifo", "is_block_device", "is_char_device"}
)
PRESENCE_FUNCTIONS = frozenset(
    {"os.path.exists", "os.path.lexists", "os.path.isfile", "os.path.isdir", "os.path.ismount", "os.access"}
)
PRESENCE_HOMES = frozenset({"skills/ejryan89-skill-core/scripts/file_presence.py", LINK_PROBE})
PRESENCE_ALLOWANCE = "PRESENCE_ALLOWED"
# The listings that raise for a folder they cannot list, which a presence test before them in an `and` passes by.
LISTING_METHODS = frozenset({"iterdir", "scandir", "listdir"})
LISTING_FUNCTIONS = frozenset({"os.scandir", "os.listdir", "os.walk", "os.fwalk"})
# The listings that match nothing in a folder they cannot list: a path's glob and rglob, and the glob module's.
GLOB_METHODS = frozenset({"glob", "rglob"})
GLOB_FUNCTIONS = frozenset({"glob.glob", "glob.iglob"})
# The walks whose onerror, at this position or by keyword, is None unless given, so a folder they cannot list is passed
# by.
WALKS = {"os.walk": 2, "os.fwalk": 2}
# A path's walk, from Python 3.12, whose on_error is the first argument after its top_down: any `walk` method but a
# module's function, as ast.walk is, or a class's own method, called on self or cls.
PATH_WALK_ERROR = (1, "on_error")
MODULE_WALKS = frozenset({"ast.walk", "os.walk", "os.fwalk"})
# The removals that discard every error when given this flag, at this position or by keyword.
DISCARDING_REMOVALS = {
    "shutil.rmtree": (1, "ignore_errors"),
    "tempfile.TemporaryDirectory": (3, "ignore_cleanup_errors"),
}
# The calls that hand each item of an iterable to a predicate they are given, as a comprehension's filter does.
FILTERS = frozenset({"filter", "itertools.filterfalse"})
EMPTY_CALLS = frozenset({"set", "frozenset", "dict", "list", "tuple", "str"})


@dataclass(frozen=True)
class _PresenceReader:
    """How one scope of a module reads presence: through the module's imports, and through the names the scope binds
    only to a presence predicate's answer."""

    aliases: dict[str, str]
    names: Mapping[str, str]
    # The ifs whose True branch returns a value and whose False goes on to an empty return or the function's end.
    falling: frozenset[int] = frozenset()

    def call(self, node: ast.expr) -> str | None:
        """The presence predicate whose answer an expression is, as `is_file` or `os.path.lexists`, or None."""
        if isinstance(node, ast.NamedExpr):
            return self.call(node.value)
        if isinstance(node, ast.Name):
            return self.names.get(node.id)
        return self.reference(node.func) if isinstance(node, ast.Call) else None

    def reference(self, node: ast.expr) -> str | None:
        """The presence predicate an expression names, called or not: `p.is_file`, `Path.exists`, or `os.path.isdir`."""
        named = qualified_name(node, self.aliases)
        if named in PRESENCE_FUNCTIONS:
            return named
        if isinstance(node, ast.Attribute) and node.attr in PRESENCE_PREDICATES:
            return node.attr
        return None

    def negated(self, test: ast.expr) -> str | None:
        """The predicate a test reads as absent: `not P`, `not (P and Q)`, `not (P or Q)`, or one of them as an operand
        of an `or`."""
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            return self.held(test.operand)
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
            return next((found for value in test.values if (found := self.negated(value))), None)
        return None

    def affirmed(self, test: ast.expr) -> str | None:
        """The predicate a test must find true: `P`, or `P` as an operand of an `and`."""
        if found := self.call(test):
            return found
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
            return next((found for value in test.values if (found := self.affirmed(value))), None)
        return None

    def held(self, node: ast.expr) -> str | None:
        """The predicate an expression holds unnegated: `P`, or `P` as an operand of an `and` or an `or`, at any
        depth."""
        if found := self.call(node):
            return found
        if isinstance(node, ast.BoolOp):
            return next((found for value in node.values if (found := self.held(value))), None)
        return None

    def truth(self, node: ast.expr) -> str | None:
        """The predicate an expression's truth turns on: `P`, `not P`, or either as an operand of an `and` or an `or`,
        at any depth."""
        if found := self.call(node):
            return found
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return self.truth(node.operand)
        if isinstance(node, ast.BoolOp):
            return next((found for value in node.values if (found := self.truth(value))), None)
        return None

    def lists_a_folder(self, node: ast.expr) -> bool:
        """Whether an expression holds a listing of a folder anywhere in it, as `any(p.iterdir())` does."""
        for call in ast.walk(node):
            if isinstance(call, ast.Call):
                method = call.func.attr if isinstance(call.func, ast.Attribute) else ""
                if method in LISTING_METHODS | GLOB_METHODS or qualified_name(call.func, self.aliases) in (
                    LISTING_FUNCTIONS | GLOB_FUNCTIONS
                ):
                    return True
        return False

    def answer(self, node: ast.AST) -> str | None:
        """The predicate whose False an if, a while, a conditional expression, a return, a lambda, a comprehension, a
        filter, or an `and` before a listing gives as the answer, or None."""
        if isinstance(node, (ast.If, ast.IfExp)):
            return self.branch(node)
        if isinstance(node, ast.While):
            return self.negated(node.test)
        if isinstance(node, (ast.Return, ast.Lambda)):
            value = node.body if isinstance(node, ast.Lambda) else node.value
            return None if value is None else self.truth(value)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            tests = [test for generator in node.generators for test in generator.ifs]
            if not isinstance(node, ast.DictComp):
                tests.insert(0, node.elt)
            return next((found for test in tests if (found := self.truth(test))), None)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            for index, value in enumerate(node.values):
                if (found := self.call(value)) and any(
                    self.lists_a_folder(later) for later in node.values[index + 1 :]
                ):
                    return found
            return None
        if isinstance(node, ast.Call) and qualified_name(node.func, self.aliases) in FILTERS and node.args:
            return self.reference(node.args[0])
        return None

    def branch(self, node: ast.If | ast.IfExp) -> str | None:
        """The predicate whose False an if or a conditional expression answers with an empty value or a `continue`."""
        if isinstance(node, ast.If):
            if (found := self.negated(node.test)) and _empty_branch(node.body):
                return found
            if id(node) in self.falling and (found := self.affirmed(node.test)):
                return found
            return found if (found := self.affirmed(node.test)) and _empty_branch(node.orelse) else None
        if (found := self.affirmed(node.test)) and _empty(node.orelse):
            return found
        return found if (found := self.negated(node.test)) and _empty(node.body) else None


def _empty(node: ast.expr | None) -> bool:
    """Whether an expression is an empty answer: nothing, None, False, zero, an empty string, an empty container, or a
    tuple, list, or set holding only empty answers, as `None, None` is."""
    if node is None:
        return True
    if isinstance(node, ast.Constant):
        return (
            node.value is None
            or node.value is False
            or node.value in ("", b"")
            or (type(node.value) is int and node.value == 0)
        )
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_empty(element) for element in node.elts)
    if isinstance(node, ast.Dict):
        return not node.keys
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in EMPTY_CALLS
        and not node.args
        and not node.keywords
    )


# The calls that only log a case, which a branch may make before its empty answer and still answer nothing: a log
# names no file or package to the caller, where a branch that records the case, as `problems.append(...)` or a
# report of its own does, answers with it.
LOG_FUNCTIONS = frozenset({"print", "warnings.warn", "sys.stderr.write", "sys.stdout.write"})
LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log"})


def _only_logs(statement: ast.stmt) -> bool:
    """Whether a statement only logs: `pass`, or a call of print, warnings.warn, a write to sys.stderr or sys.stdout,
    or a logging method, as `logger.warning(...)` or `logging.info(...)`."""
    if isinstance(statement, ast.Pass):
        return True
    if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)):
        return False
    func = statement.value.func
    named = qualified_name(func, {})
    return named in LOG_FUNCTIONS or (isinstance(func, ast.Attribute) and func.attr in LOG_METHODS)


def _empty_branch(body: list[ast.stmt]) -> bool:
    """Whether a branch answers nothing: it ends in a return of an empty value, a continue past the item at hand, or a
    break out of the search, and every statement before that only logs."""
    if not body or not all(_only_logs(statement) for statement in body[:-1]):
        return False
    last = body[-1]
    if isinstance(last, ast.Return):
        return _empty(last.value)
    return isinstance(last, (ast.Continue, ast.Break))


def _returns_a_value(block: list[ast.stmt]) -> bool:
    """Whether a block returns a value that is not empty anywhere in it, outside a function or class it defines."""
    pending: list[ast.AST] = list(block)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Return) and not _empty(node.value):
            return True
        pending.extend(ast.iter_child_nodes(node))
    return False


def _falling_ifs(tree: ast.Module) -> frozenset[int]:
    """Each if with no else whose branch ends by returning a value that is not empty, and whose False goes on to an
    empty return or the end of its function: the statement after it, or after the block, loop, `with`, or `try` that
    holds it. And each try that returns a value that is not empty, and goes on to an empty return or the end of its
    function when a handler takes its error, so a handler that only passes or logs answers nothing."""
    found: set[int] = set()

    def visit(block: list[ast.stmt], ends_empty: bool) -> None:
        for index, statement in enumerate(block):
            after = block[index + 1] if index + 1 < len(block) else None
            empty_after = ends_empty if after is None else isinstance(after, ast.Return) and _empty(after.value)
            if isinstance(statement, ast.If):
                last = statement.body[-1]
                if not statement.orelse and isinstance(last, ast.Return) and not _empty(last.value) and empty_after:
                    found.add(id(statement))
                visit(statement.body, empty_after)
                visit(statement.orelse, empty_after)
            elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
                visit(statement.body, empty_after)
                visit(getattr(statement, "orelse", []), empty_after)
            elif isinstance(statement, (ast.Try, ast.TryStar)):
                if empty_after and _returns_a_value(statement.body):
                    found.add(id(statement))
                visit(statement.body, empty_after and not statement.orelse)
                for handler in statement.handlers:
                    visit(handler.body, empty_after)
                visit(statement.orelse, empty_after)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            visit(node.body, True)
    return frozenset(found)


def _bound_answers(tree: ast.Module, aliases: dict[str, str]) -> dict[str, dict[str, str]]:
    """Each scope's names that every binding in the scope sets to one presence predicate's answer, by an assignment, an
    annotated assignment, or an assignment expression, with that predicate; a name bound any other way, or as a
    parameter, is none."""
    plain = _PresenceReader(aliases, {})
    answers: dict[int, str | None] = {}
    for node, _ in scoped_nodes(tree.body):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            answers[id(node.targets[0])] = plain.call(node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            answers[id(node.target)] = plain.call(node.value)
    seen: dict[str, dict[str, str | None]] = {}
    for node, scope in scoped_nodes(tree.body):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            name, answer = node.id, answers.get(id(node))
        elif isinstance(node, ast.arg):
            name, answer = node.arg, None
        else:
            continue
        names = seen.setdefault(scope, {})
        names[name] = answer if names.get(name, answer) == answer else None
    return {scope: {name: found for name, found in names.items() if found} for scope, names in seen.items()}


# The calls that stat or list a path, whose OSError a handler may read as nothing there: a path's stat, lstat, and
# listings, os's, and file_presence's own readers, besides the presence predicates.
STATTING_METHODS = frozenset({"stat", "lstat", "iterdir", "scandir", "listdir", "glob", "rglob"})
STATTING_FUNCTIONS = frozenset(
    {
        "os.stat",
        "os.lstat",
        "os.scandir",
        "os.listdir",
        "os.walk",
        "os.fwalk",
        "os.path.getsize",
        "os.path.getmtime",
        "os.path.getatime",
        "os.path.getctime",
    }
)
FILE_PRESENCE_READERS = frozenset({"is_present", "is_directory", "is_occupied", "matching"})
# The exceptions whose handler takes a failed stat or listing, beyond "not found": OSError, its aliases and the
# subclass a denied stat raises, and the classes above it.
SWALLOWING_ERRORS = frozenset(
    {"OSError", "EnvironmentError", "IOError", "PermissionError", "Exception", "BaseException"}
)


def _stats_or_lists(body: list[ast.stmt], aliases: dict[str, str]) -> str | None:
    """The first call in a block, outside a function it defines, that stats or lists a path, by its name, or None."""
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop(0)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Call):
            named = qualified_name(node.func, aliases)
            method = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if method in STATTING_METHODS | PRESENCE_PREDICATES:
                return method
            if named in STATTING_FUNCTIONS | PRESENCE_FUNCTIONS:
                return named
            if named.rpartition(".")[2] in FILE_PRESENCE_READERS and named.startswith("file_presence."):
                return named
        pending.extend(ast.iter_child_nodes(node))
    return None


def _swallows(types: ast.expr | None, aliases: dict[str, str]) -> bool:
    """Whether an except clause, or a suppress, takes a failed stat's OSError: a bare except, or one of
    SWALLOWING_ERRORS or `os.error`, OSError's alias, alone or in a tuple."""
    if types is None:
        return True
    names = types.elts if isinstance(types, ast.Tuple) else [types]
    return any(
        (name.attr if isinstance(name, ast.Attribute) else getattr(name, "id", "")) in SWALLOWING_ERRORS
        or qualified_name(name, aliases) == "os.error"
        for name in names
    )


def _empty_assignment(body: list[ast.stmt]) -> str | None:
    """The name a handler binds to an empty value, when it does that and otherwise only logs: `st = None`."""
    assignments = [statement for statement in body if not _only_logs(statement)]
    if len(assignments) != 1:
        return None
    statement = assignments[0]
    if (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and _empty(statement.value)
    ):
        return statement.targets[0].id
    return None


def _returned_names(tree: ast.Module) -> dict[int, frozenset[str]]:
    """For each try, the names its function returns as they are, `return name`, outside a function it defines."""
    found: dict[int, frozenset[str]] = {}
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names: set[str] = set()
        tries: list[ast.AST] = []
        pending: list[ast.AST] = list(function.body)
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Name):
                names.add(node.value.id)
            if isinstance(node, (ast.Try, ast.TryStar)):
                tries.append(node)
            pending.extend(ast.iter_child_nodes(node))
        for node in tries:
            found[id(node)] = frozenset(names)
    return found


def _handled_answer(
    node: ast.AST, aliases: dict[str, str], falling: frozenset[int], returned: Mapping[int, frozenset[str]]
) -> str | None:
    """How a handler around a stat or listing reads its OSError as nothing there: an except clause that answers nothing,
    as `_empty_branch` reads a branch, one that only passes or logs where the try goes on to an empty return or its
    function's end, one that binds an empty value to a name its function returns, or a contextlib.suppress of it."""
    if isinstance(node, (ast.Try, ast.TryStar)) and (call := _stats_or_lists(node.body, aliases)):
        for handler in node.handlers:
            if not _swallows(handler.type, aliases):
                continue
            falls = id(node) in falling and all(_only_logs(statement) for statement in handler.body)
            empty = _empty_assignment(handler.body)
            if _empty_branch(handler.body) or falls or (empty is not None and empty in returned.get(id(node), ())):
                return (
                    f"answers nothing from a handler of {call}'s OSError, which reads a path it cannot stat or list "
                    "as nothing there; let the error through, or report the path"
                )
    if isinstance(node, (ast.With, ast.AsyncWith)) and (call := _stats_or_lists(node.body, aliases)):
        for item in node.items:
            context = item.context_expr
            if (
                isinstance(context, ast.Call)
                and qualified_name(context.func, aliases) == "contextlib.suppress"
                and any(_swallows(argument, aliases) for argument in context.args)
            ):
                return (
                    f"suppresses {call}'s OSError, which reads a path it cannot stat or list as nothing there; let "
                    "the error through, or report the path"
                )
    return None


def _flag_given(node: ast.expr | None) -> bool:
    """Whether an argument may be true: given, and not the constant False, None, or zero."""
    return node is not None and not (isinstance(node, ast.Constant) and not node.value)


def _listing_answer(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """How a call lists or removes a folder so that one it cannot list or remove reads as empty or gone, or None."""
    if not isinstance(node, ast.Call):
        return None
    named = qualified_name(node.func, aliases)
    method = node.func.attr if isinstance(node.func, ast.Attribute) else ""
    if named in GLOB_FUNCTIONS or method in GLOB_METHODS:
        return (
            f"lists a folder with {named if named in GLOB_FUNCTIONS else method}, which matches nothing in a folder it "
            "cannot list; call ejryan89-skill-core's file_presence.matching, which raises there, or list it with "
            "iterdir or an os.walk whose onerror raises"
        )
    path_walk = (
        method == "walk"
        and named not in MODULE_WALKS
        and not (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in ("self", "cls")
        )
    )
    if named in WALKS or path_walk:
        onerror = call_argument(node, *PATH_WALK_ERROR) if path_walk else call_argument(node, WALKS[named], "onerror")
        # An unpacking may not hold one, so it shows no onerror.
        if (
            onerror is None
            or isinstance(onerror, ast.Starred)
            or (isinstance(onerror, ast.Constant) and onerror.value is None)
        ):
            walk = "a path's walk" if path_walk else named
            keyword = "on_error" if path_walk else "onerror"
            return (
                f"walks a folder with {walk} and no {keyword}, which passes by a folder it cannot list; give it an "
                f"{keyword} that raises"
            )
    if named in DISCARDING_REMOVALS:
        position, keyword = DISCARDING_REMOVALS[named]
        if _flag_given(call_argument(node, position, keyword)):
            return (
                f"removes a folder with {named} given {keyword}, which reads one it could not remove as gone; let "
                "the error through"
            )
    return None


def _presence_answers(tree: ast.Module, aliases: dict[str, str]) -> Iterator[tuple[ast.AST, str, str]]:
    """Each node where a failed stat, listing, or removal is read as nothing there, with its scope and its refusal."""
    bound = _bound_answers(tree, aliases)
    falling = _falling_ifs(tree)
    returned = _returned_names(tree)
    readers: dict[str, _PresenceReader] = {}
    for node, scope in scoped_nodes(tree.body):
        reader = readers.setdefault(scope, _PresenceReader(aliases, bound.get(scope, {}), falling))
        if predicate := reader.answer(node):
            yield (
                node,
                scope,
                f"reads {predicate}'s False as nothing there, which it also answers for a path it cannot stat; call "
                "ejryan89-skill-core's file_presence (is_present, is_directory, or is_occupied), which lets a failed "
                "stat through",
            )
        elif listing := _listing_answer(node, aliases):
            yield node, scope, listing
        elif handled := _handled_answer(node, aliases, falling, returned):
            yield node, scope, handled


def presence_problems(root: Path, files: list[Path]) -> list[str]:
    """Each presence predicate, glob, walk, or removal outside file_presence and link_probe that reads a path it
    cannot stat, list, or remove as nothing there, and that no PRESENCE_ALLOWED entry sanctions."""
    problems: list[str] = []
    paths = [path for path in claim_files(root, files) if path.relative_to(root).as_posix() not in PRESENCE_HOMES]
    for _, name, tree in python_trees(paths, root, problems):
        aliases = import_aliases(tree)
        found = [
            (getattr(node, "lineno", 0), scope, f"{refusal}, or state why in {PRESENCE_ALLOWANCE}")
            for node, scope, refusal in _presence_answers(tree, aliases)
        ]
        problems += sanctioned_problems(name, tree, PRESENCE_ALLOWANCE, found)
    return problems


# ejryan89-skill-core's json_reader is the one parse of JSON, refusing a repeated key, a bare NaN or Infinity, a number
# too large for a float, and nesting past the recursion limit, where the json module's own readers keep a repeated
# key's last value and read NaN as a float. Any other parse states why in a module-level
# JSON_PARSE_ALLOWED = {scope: reason}, as where a function parses the text it just serialized.
JSON_PARSERS = frozenset({"json.loads", "json.load", "json.JSONDecoder", "json.decoder.JSONDecoder"})
JSON_READER = "skills/ejryan89-skill-core/scripts/json_reader.py"
JSON_PARSE_ALLOWANCE = "JSON_PARSE_ALLOWED"


def _looked_up(node: ast.AST, aliases: dict[str, str]) -> str:
    """The qualified name a node reads: a name or an attribute it loads, or what a getattr of a constant name on one
    reads, as `getattr(json, "loads")` reads json.loads; "" for anything else."""
    if isinstance(node, ast.Attribute | ast.Name) and isinstance(node.ctx, ast.Load):
        return qualified_name(node, aliases)
    if isinstance(node, ast.Call) and qualified_name(node.func, aliases) in ATTRIBUTE_LOOKUPS and node.args:
        owner = qualified_name(node.args[0], aliases)
        attribute = call_argument(node, 1, "name")
        if owner and isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
            return f"{owner}.{attribute.value}"
    return ""


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
            if isinstance(node, ast.expr) and (parser := _looked_up(node, aliases)) in JSON_PARSERS
        ]
        problems += sanctioned_problems(name, tree, JSON_PARSE_ALLOWANCE, found)
    return problems


# ejryan89-skill-core's whole_number is the one reader of a whole number from text, taking ASCII digits alone up to a
# cap and raising its caller's named error, where int() reads every Unicode digit, a sign, spaces, and underscores, and
# past the interpreter's digit limit raises a ValueError that names no input. So int() never converts a
# regular-expression group, an item of a findall() list or of one of its tuples among them, or a value an isdigit(),
# isdecimal(), or isnumeric() test admitted, which passes a digit such as a superscript two that int() then refuses.
# Nor does an argparse option read its number with int() or float(): an add_argument's type is never int or float,
# through an alias, a getattr, a functools.partial, or a `**` literal (resolved_calls), and a function or lambda the
# module gives an add_argument as its type never converts its own parameter's text, through a method call on it, an
# index, or a local name bound to one, with int() or float(); the option takes
# whole_number.argument, or seconds_option.positive_seconds for a time. Any other conversion states why in a
# module-level INT_PARSE_ALLOWED = {scope: reason}, as where the pattern itself bounds the digits. A type built by a
# call, as a factory's inner function, and one another module defines, are read only by what they resolve to.
WHOLE_NUMBER_READER = "skills/ejryan89-skill-core/scripts/whole_number.py"
INT_PARSE_ALLOWANCE = "INT_PARSE_ALLOWED"
# What each number type an argparse option may name reads beyond a number's ASCII digits.
ARGUMENT_NUMBER_TYPES = {
    "int": "a sign, spaces, underscores, and every Unicode decimal digit",
    "float": "a sign, spaces, underscores, every Unicode decimal digit, nan, and inf",
}


# The objects whose functions and methods a call may name through the re module: a call on either names its pattern or
# its match first, as re.fullmatch(pattern, text) and re.Pattern.fullmatch(pattern, text) do.
REGEX_OWNERS = frozenset({"re", "re.Pattern", "re.Match"})
# The callables int() is reached by, through the module's imports.
INT_CALLABLES = frozenset({"int", "builtins.int"})
MAP_CALLABLES = frozenset({"map", "builtins.map"})


def _names(target: ast.expr) -> list[ast.Name]:
    """The names an assignment target binds, through tuples, lists, and a starred name."""
    if isinstance(target, ast.Name):
        return [target]
    if isinstance(target, ast.Starred):
        return _names(target.value)
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


def _bindings(node: ast.AST) -> list[tuple[ast.AST, list[ast.Name], ast.expr]]:
    """Each name list an assignment, a walrus, a for loop, or a comprehension binds, with the node whose scope reads
    the names and the value or iterable it binds them from. A walrus's target is read from the walrus itself, whose
    scope finds the one the name is bound in."""
    if isinstance(node, ast.Assign):
        return [(node, _names(target), node.value) for target in node.targets]
    if isinstance(node, ast.NamedExpr):
        return [(node, _names(node.target), node.value)]
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return [(node.target, _names(node.target), node.value)]
    if isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
        return [(node.target, _names(node.target), node.iter)]
    return []


def _unstripped(node: ast.expr) -> ast.expr:
    """An expression with each strip(), lstrip(), or rstrip() call on its end taken off."""
    while (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("strip", "lstrip", "rstrip")
    ):
        node = node.func.value
    return node


class _ModuleNumbers:
    """What a module binds that int() must not convert, each name by the scope that binds it, wherever in the module it
    is read: the names bound to a regular-expression match, the names bound to a group of one or to a list of them, and
    each expression a digit test admitted, by the scope of the name it reads and its dump with any strip() calls taken
    off. A function or method of re counts by the name the module's imports resolve it to, a bare import and an alias
    included, and a method of a compiled pattern or a match by its name on any receiver."""

    def __init__(self, tree: ast.Module, resolver: CallableResolver) -> None:
        self.resolver = resolver
        self.matches: set[tuple[int, str]] = set()
        self.groups: set[tuple[int, str]] = set()
        self.tested: dict[tuple[int, str], str] = {}
        nodes = list(ast.walk(tree))
        # A binding may read a name another binds later in the walk, so they are read until they add no name.
        while True:
            known = len(self.matches) + len(self.groups)
            for node in nodes:
                self.bind(node)
            if len(self.matches) + len(self.groups) == known:
                break
        for node in nodes:
            self.test(node)

    def _key(self, node: ast.AST, name: str) -> tuple[int, str]:
        """The scope that binds a name where a node reads or binds it, with the name."""
        scope = self.resolver.scopes.get(id(node))
        owner = scope.owner(name) if scope is not None else None
        return (id(owner) if owner is not None else 0, name)

    def regex_call(self, node: ast.AST, *methods: str) -> tuple[str, bool] | None:
        """The method a call of one of `methods` makes, and whether it is given the pattern or the match first, or None
        for any other call: re's function or a method named on re.Pattern or re.Match, through the module's imports
        or a getattr of a constant name on one of them, or a method by its name on any other receiver."""
        if not isinstance(node, ast.Call):
            return None
        function = node.func
        if isinstance(function, ast.Call) and self.resolver.name(function.func) in ATTRIBUTE_LOOKUPS:
            attribute = call_argument(function, 1, "name")
            if function.args and isinstance(attribute, ast.Constant) and attribute.value in methods:
                return str(attribute.value), self.resolver.name(function.args[0]) in REGEX_OWNERS
            return None
        owner, _, method = self.resolver.name(function).rpartition(".")
        if owner in REGEX_OWNERS and method in methods:
            return method, True
        if isinstance(function, ast.Attribute) and function.attr in methods:
            return function.attr, False
        return None

    def is_group(self, node: ast.expr) -> bool:
        """Whether an expression is a group of a match: a group() call, an index of groups(), groupdict(), or
        findall() or of a match, an index of a group, as of a findall tuple, or a name bound to one, or a conditional
        expression or a boolean operation one of whose values is."""
        return any(self._group(branch) for branch in _branches(node))

    def _group(self, node: ast.expr) -> bool:
        if self.regex_call(node, "group") is not None:
            return True
        if isinstance(node, ast.Subscript):
            value = node.value
            return (
                self.regex_call(value, "groups", "groupdict", "findall") is not None
                or (isinstance(value, ast.Name) and self._key(value, value.id) in self.matches)
                or self.is_group(_unstripped(value))
            )
        return isinstance(node, ast.Name) and self._key(node, node.id) in self.groups

    def _lists_groups(self, node: ast.expr) -> bool:
        """Whether an expression's items are groups: a groups() or findall() call, through a zip(), enumerate(),
        sorted(), or reversed() over one, or a name bound to one."""
        if isinstance(node, ast.Call) and self.resolver.name(node.func) in ("zip", "enumerate", "sorted", "reversed"):
            return any(self._lists_groups(argument) for argument in node.args)
        return self.regex_call(node, "groups", "findall") is not None or self.is_group(node)

    def bind(self, node: ast.AST) -> None:
        """Record what one node binds to a match or a group."""
        for place, names, value in _bindings(node):
            branches = _branches(value)
            if any(self.regex_call(branch, "match", "fullmatch", "search", "finditer") for branch in branches):
                self.matches.update(self._key(place, name.id) for name in names)
            elif any(self._lists_groups(branch) for branch in branches):
                self.groups.update(self._key(place, name.id) for name in names)

    def _place(self, node: ast.expr) -> tuple[int, str]:
        """Where a tested expression is read: the scope binding the name it reads, through its attributes, indexes,
        and calls, with its dump with any strip() calls taken off."""
        root: ast.expr = node
        while isinstance(root, ast.Attribute | ast.Subscript | ast.Call):
            root = root.func if isinstance(root, ast.Call) else root.value
        scope = self._key(root, root.id)[0] if isinstance(root, ast.Name) else id(self.resolver.scopes.get(id(node)))
        return scope, ast.dump(_unstripped(node))

    def test(self, node: ast.AST) -> None:
        """Record the expression a digit test or a regular-expression test on this node admits, with how to name the
        test."""
        if not isinstance(node, ast.Call):
            return
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("isdigit", "isdecimal", "isnumeric"):
            self.tested.setdefault(self._place(_unstripped(node.func.value)), f"an {node.func.attr}() test")
            return
        found = self.regex_call(node, "match", "fullmatch", "search")
        if found is None:
            return
        method, pattern_first = found
        # re.fullmatch(pattern, text) tests its second argument, and a compiled pattern's fullmatch(text) its first.
        index = 1 if pattern_first else 0
        if len(node.args) > index:
            self.tested.setdefault(self._place(node.args[index]), f"a regular-expression {method}()")

    def refusal(self, call: ast.Call) -> str | None:
        """What a one-argument int() call converts that it must not, or a map(int, ...) over groups, or None. A
        strip() leaves a group's digits as they were, so a stripped group is one too."""
        called = self.resolver.name(call.func)
        if called in MAP_CALLABLES and len(call.args) >= 2 and self.resolver.name(call.args[0]) in INT_CALLABLES:
            return "regular-expression groups" if any(self._lists_groups(item) for item in call.args[1:]) else None
        if called not in INT_CALLABLES or len(call.args) != 1 or call.keywords:
            return None
        argument = call.args[0]
        if self.is_group(_unstripped(argument)):
            return "a regular-expression group"
        test = self.tested.get(self._place(argument))
        return f"a value {test} admitted" if test else None


def _number_types(given: ast.expr, resolver: CallableResolver) -> set[str]:
    """The number types of ARGUMENT_NUMBER_TYPES an add_argument's type may be: one it names, through the module's
    imports and bindings, a getattr of builtins included, or the function a functools.partial of one binds."""
    kinds = {reference.name.removeprefix("builtins.") for reference in resolved_references(given, resolver)}
    for value in bound_values(given, resolver):
        if (
            isinstance(value, ast.Call)
            and value.args
            and any(reference.name in PARTIAL_CALLS for reference in resolved_references(value.func, resolver))
        ):
            kinds |= _number_types(value.args[0], resolver)
    return kinds & set(ARGUMENT_NUMBER_TYPES)


def _type_functions(
    given: ast.expr, resolver: CallableResolver, functions: list[ast.FunctionDef | ast.AsyncFunctionDef]
) -> list[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda]:
    """The functions and lambdas of this module an add_argument's type may be: a lambda given or bound to a name, and a
    function the module defines in the scope that owns the name the type reads."""
    found: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda] = []
    for value in bound_values(given, resolver):
        if isinstance(value, ast.Lambda):
            found.append(value)
        elif isinstance(value, ast.Name) and (scope := resolver.scopes.get(id(value))) is not None:
            owner = scope.owner(value.id)
            found += [
                function
                for function in functions
                if function.name == value.id and owner is not None and resolver.scopes.get(id(function)) is owner
            ]
    return found


def _argument_conversions(
    tree: ast.Module, resolver: CallableResolver
) -> tuple[dict[int, str], dict[int, tuple[str, str, str]]]:
    """Each add_argument call whose type is a number type, by the call, with the type; and each int() or float() call
    in a function or lambda the module gives an add_argument as its type that converts one of that function's own
    parameters, stripped or not, by the call, with the builtin, the parameter, and the function's name."""
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]
    typed: dict[int, str] = {}
    converted: dict[int, tuple[str, str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for read in resolved_calls(node, resolver):
            if read.attribute != "add_argument":
                continue
            for given in read.keywords.get("type", []):
                if kinds := _number_types(given, resolver):
                    typed.setdefault(id(node), sorted(kinds)[-1])
                for function in _type_functions(given, resolver, functions):
                    converted |= _parameter_conversions(function, resolver)
    return typed, converted


def _text_source(node: ast.expr) -> ast.Name | None:
    """The name an expression takes its text from through method calls on it, its attributes, and indexes into it, as
    `value.replace(",", "").strip()` and `value[1:]` take `value`'s, or None for any other expression: a function
    given the name, as `len(value)`, computes something else from it."""
    while isinstance(node, ast.Attribute | ast.Subscript | ast.Call):
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Attribute):
                return None
            node = node.func.value
        else:
            node = node.value
    return node if isinstance(node, ast.Name) else None


def _parameter_conversions(
    function: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, resolver: CallableResolver
) -> dict[int, tuple[str, str, str]]:
    """Each int() or float() call in a function that converts the text of one of its own parameters, by the call, with
    the builtin, the name it converts, and the function's name. The text is the parameter's, and that of a method call
    on it, an attribute or an index of it, or a name the function binds to one of these, as
    `text = value.lower()` binds `text`, each name by the scope that binds it."""
    arguments = function.args

    def key(name: ast.Name) -> tuple[int, str] | None:
        scope = resolver.scopes.get(id(name))
        owner = scope.owner(name.id) if scope is not None else None
        return (id(owner), name.id) if owner is not None else None

    received = {
        (id(scope), argument.arg)
        for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
        if (scope := resolver.scopes.get(id(argument))) is not None
    }
    bindings = [
        (target, value) for node in ast.walk(function) for _place, names, value in _bindings(node) for target in names
    ]
    # A binding may read a name another binds later in the walk, so they are read until they add no name.
    while True:
        known = len(received)
        for target, value in bindings:
            source = _text_source(value)
            if source is not None and key(source) in received and (bound := key(target)) is not None:
                received.add(bound)
        if len(received) == known:
            break
    label = function.name if not isinstance(function, ast.Lambda) else "a lambda"
    found: dict[int, tuple[str, str, str]] = {}
    for call in ast.walk(function):
        if not isinstance(call, ast.Call) or not call.args:
            continue
        builtin = resolver.name(call.func).removeprefix("builtins.")
        source = _text_source(call.args[0])
        if builtin in ARGUMENT_NUMBER_TYPES and source is not None and key(source) in received:
            found[id(call)] = (builtin, source.id, label)
    return found


def int_parse_problems(root: Path, files: list[Path]) -> list[str]:
    """Each one-argument int() call outside whole_number that converts a regular-expression group or a value a digit
    test admitted, each add_argument whose type is int or float, and each int() or float() of its own parameter in a
    function the module gives an add_argument as its type, that no INT_PARSE_ALLOWED entry sanctions."""
    problems: list[str] = []
    paths = [path for path in claim_files(root, files) if path.relative_to(root).as_posix() != WHOLE_NUMBER_READER]
    use = (
        "call ejryan89-skill-core's whole_number.argument, or seconds_option.positive_seconds for a time, as the "
        f"option's type, or state why in {INT_PARSE_ALLOWANCE}"
    )
    for _, name, tree in python_trees(paths, root, problems):
        resolver = CallableResolver(tree)
        numbers = _ModuleNumbers(tree, resolver)
        typed, converted = _argument_conversions(tree, resolver)
        found = [
            (
                node.lineno,
                scope,
                f"converts {what} with int(), which reads more than ASCII digits and past the digit limit raises a "
                "ValueError naming no input; call ejryan89-skill-core's whole_number.read, the one reader of a whole "
                f"number from text, or state why in {INT_PARSE_ALLOWANCE}",
            )
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.Call) and id(node) not in converted and (what := numbers.refusal(node))
        ]
        found += [
            (
                node.lineno,
                scope,
                f"reads an option with type={typed[id(node)]}, which reads "
                f"{ARGUMENT_NUMBER_TYPES[typed[id(node)]]}; {use}",
            )
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.Call) and id(node) in typed
        ]
        found += [
            (
                node.lineno,
                scope,
                f"converts {parameter} with {builtin}() in {function}, an option's argparse type, which reads "
                f"{ARGUMENT_NUMBER_TYPES[builtin]}; {use}",
            )
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.Call) and id(node) in converted
            for builtin, parameter, function in [converted[id(node)]]
        ]
        problems += sanctioned_problems(name, tree, INT_PARSE_ALLOWANCE, sorted(found, key=lambda item: item[0]))
    return problems


# ejryan89-skill-core's json_writer is the one writer of JSON for a file, refusing at the write a non-finite number,
# a lone surrogate, and what json_reader would refuse on reading the text back, where the json module's own writer
# writes NaN and a lone surrogate a later strict read refuses. A use of json.dump, or json.dumps text, or a
# JSONEncoder's encode or iterencode, that a write call takes, as its argument or through a name its function binds
# to it, states why in a module-level JSON_WRITE_ALLOWED = {scope: reason}, as where a function writes a message to
# a pipe. Each is read by its name, a name its scope binds once to it, or a getattr of a constant name, as the parse
# policy reads json.loads.
JSON_DUMP = "json.dump"
JSON_DUMPS = "json.dumps"
JSON_ENCODERS = frozenset({"json.JSONEncoder", "json.encoder.JSONEncoder"})
JSON_ENCODE_METHODS = frozenset({"encode", "iterencode"})
JSON_WRITER = "skills/ejryan89-skill-core/scripts/json_writer.py"
# The methods that write to a file or a stream, and the functions that replace or append to a file.
WRITE_METHODS = frozenset({"write", "write_text", "write_bytes", "writelines"})
FILE_WRITERS = frozenset(
    {"replace_text", "replace_bytes", "atomic_write_text", "atomic_write_bytes", "write_atomic", "append_line"}
)
JSON_WRITE_ALLOWANCE = "JSON_WRITE_ALLOWED"
PRINTS = frozenset({"print", "builtins.print"})


def _writes(call: ResolvedCall) -> bool:
    """Whether a call writes what it is given to a file or a stream: a write method, a file writer, or a print given a
    file= other than None, which prints to that stream."""
    if call.attribute in WRITE_METHODS:
        return True
    if call.name in PRINTS:
        return any(
            not (isinstance(stream, ast.Constant) and stream.value is None) for stream in call.keywords.get("file", [])
        )
    return call.name.rpartition(".")[2] in FILE_WRITERS


# The places a binding writes to: a name, an attribute such as self.text, or a subscript, each keyed by place_key.
JSON_PLACES = (ast.Name, ast.Attribute, ast.Subscript)
# The bucket a module-level binding, and an attribute or a subscript, go in, which every scope of the module reads.
MODULE_WIDE = "<module-wide>"


@dataclass
class _JsonNames:
    """A scope's places bound to JSON text, and to a JSONEncoder, to the fixed point, by place_key."""

    text: set[str]
    encoders: set[str]


class _JsonModel:
    """What a module binds to JSON text or a JSONEncoder, scope by scope, with a module-level binding, and an attribute
    or a subscript, read in every scope; and its JSONEncoder subclasses."""

    def __init__(self, tree: ast.Module, nodes: list[tuple[ast.AST, str]], resolver: CallableResolver) -> None:
        self.resolver = resolver
        self.scopes = {id(node): scope for node, scope in nodes}
        self.class_names = {scope for node, scope in nodes if isinstance(node, ast.ClassDef)}
        self.encoder_classes = set(JSON_ENCODERS)
        while True:
            added = {
                f"<local>.{node.name}"
                for node, _ in nodes
                if isinstance(node, ast.ClassDef)
                and any(
                    reference.name in self.encoder_classes
                    for base in node.bases
                    for reference in resolved_references(base, resolver)
                )
            } - self.encoder_classes
            if not added:
                break
            self.encoder_classes |= added
        module_level = {id(node) for node in _module_level_nodes(tree)}
        self.augmented: set[tuple[str, str]] = set()
        bindings = []
        for node, scope in nodes:
            for target, value in _bound_values(node):
                if isinstance(target, ast.Name) and isinstance(node, ast.AugAssign):
                    # An augmented assignment adds to a name's value, which bound_values cannot follow.
                    self.augmented.add((scope, target.id))
                key = place_key(target, scope, self.class_names)
                wide = id(node) in module_level or not isinstance(target, ast.Name) or scope in self.class_names
                bindings.append((MODULE_WIDE if wide else scope, key, value, scope))
        self.bound: dict[str, _JsonNames] = {}
        while True:
            added_any = False
            for bucket, key, value, scope in bindings:
                names = self.names(scope)
                own = self.bound.setdefault(bucket, _JsonNames(set(), set()))
                if key not in names.text and self.text(value, scope):
                    own.text.add(key)
                    added_any = True
                if key not in names.encoders and self.encoder(value, scope):
                    own.encoders.add(key)
                    added_any = True
            if not added_any:
                return

    def names(self, scope: str) -> _JsonNames:
        """A scope's places with the module's own, which every scope reads."""
        own = self.bound.get(scope, _JsonNames(set(), set()))
        wide = self.bound.get(MODULE_WIDE, _JsonNames(set(), set()))
        return _JsonNames(own.text | wide.text, own.encoders | wide.encoders)

    def held(self, node: ast.AST, scope: str, kind: str) -> bool:
        """Whether a place an expression reads is bound to JSON text ("text") or to a JSONEncoder ("encoders"). A
        name is read so only where an augmented assignment adds to it; any other is followed through bound_values,
        which keeps only the bindings that may reach it."""
        if not isinstance(node, JSON_PLACES) or not isinstance(node.ctx, ast.Load):
            return False
        if isinstance(node, ast.Name) and (scope, node.id) not in self.augmented:
            return False
        names = self.names(scope)
        return place_key(node, scope, self.class_names) in (names.text if kind == "text" else names.encoders)

    def encoder(self, node: ast.AST, scope: str) -> bool:
        """Whether an expression may be a JSONEncoder: a call of one, or of a subclass the module defines, or a place
        its scope binds to one."""
        if isinstance(node, ast.Call):
            return any(read.name in self.encoder_classes for read in resolved_calls(node, self.resolver))
        if isinstance(node, ast.expr):
            return any(
                self.held(value, scope, "encoders")
                or (isinstance(value, ast.Call) and self.encoder(value, scope) and value is not node)
                for value in bound_values(node, self.resolver)
            )
        return False

    def call(self, node: ast.AST, scope: str) -> bool:
        """Whether a node is a call that may return JSON text: json.dumps, or a JSONEncoder's encode or iterencode,
        bound or called on the class with the encoder first."""
        if not isinstance(node, ast.Call):
            return False
        for read in resolved_calls(node, self.resolver):
            if read.name == JSON_DUMPS:
                return True
            if (
                read.attribute in JSON_ENCODE_METHODS
                and read.receiver is not None
                and (
                    self.encoder(read.receiver, scope)
                    or any(
                        reference.name in self.encoder_classes
                        for reference in resolved_references(read.receiver, self.resolver)
                    )
                )
            ):
                return True
        return False

    def text(self, node: ast.AST, scope: str, seen: frozenset[int] = frozenset()) -> bool:
        """Whether an expression may hold JSON text: a call that returns it, or a place its scope binds to an
        expression that holds it, followed through each binding that may reach it, once each."""
        for inner in ast.walk(node):
            if self.call(inner, scope) or self.held(inner, scope, "text"):
                return True
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load):
                for value in bound_values(inner, self.resolver):
                    if (
                        value is not inner
                        and id(value) not in seen
                        and self.text(value, self.scopes.get(id(value), scope), seen | {id(value)})
                    ):
                        return True
        return False


def _module_level_nodes(tree: ast.Module) -> Iterator[ast.AST]:
    """Each node the module's top level runs, never one inside a function, a class, or a lambda."""

    def walk(node: ast.AST) -> Iterator[ast.AST]:
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (*DEFINITIONS, ast.Lambda)):
                yield from walk(child)

    for statement in tree.body:
        if not isinstance(statement, DEFINITIONS):
            yield from walk(statement)


def _target_values(target: ast.expr, value: ast.expr) -> Iterator[tuple[ast.expr, ast.expr]]:
    """Each place a binding target binds, with the expression it may take its value from: the matching item of a
    literal tuple or list of the same length that unpacks nothing, else the whole value, for a tuple, list, or starred
    target."""
    if isinstance(target, JSON_PLACES):
        yield target, value
    elif isinstance(target, ast.Starred):
        yield from _target_values(target.value, value)
    elif isinstance(target, ast.Tuple | ast.List):
        items = value.elts if isinstance(value, ast.Tuple | ast.List) else []
        paired = len(items) == len(target.elts) and not any(
            isinstance(item, ast.Starred) for item in [*items, *target.elts]
        )
        for index, element in enumerate(target.elts):
            yield from _target_values(element, items[index] if paired else value)


def _bound_values(node: ast.AST) -> Iterator[tuple[ast.expr, ast.expr]]:
    """Each place a node binds, with the expression its value comes from: an assignment, an annotated or augmented
    one, an assignment expression, a for loop or a comprehension's target from what it loops over, and a with item's
    target from its context expression."""
    if isinstance(node, ast.Assign):
        for target in node.targets:
            yield from _target_values(target, node.value)
    elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr) and node.value is not None:
        yield from _target_values(node.target, node.value)
    elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
        yield from _target_values(node.target, node.iter)
    elif isinstance(node, ast.With | ast.AsyncWith):
        for item in node.items:
            if item.optional_vars is not None:
                yield from _target_values(item.optional_vars, item.context_expr)


def json_write_problems(root: Path, files: list[Path]) -> list[str]:
    """Each use of json.dump, and each write call that takes JSON text, outside json_writer that no JSON_WRITE_ALLOWED
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
        resolver = CallableResolver(tree)
        nodes = list(scoped_nodes(tree.body))
        model = _JsonModel(tree, nodes, resolver)
        found: list[tuple[int, str, str]] = []
        for node, scope in nodes:
            if not isinstance(node, ast.expr):
                continue
            loads = isinstance(node, ast.Call) or (
                isinstance(node, ast.Name | ast.Attribute) and isinstance(node.ctx, ast.Load)
            )
            if loads and any(reference.name == JSON_DUMP for reference in resolved_references(node, resolver)):
                found.append((node.lineno, scope, f"writes JSON with {JSON_DUMP}{remedy}"))
            elif isinstance(node, ast.Call) and any(
                _writes(read)
                and any(
                    model.text(argument, scope)
                    for argument in [*read.args, *(value for values in read.keywords.values() for value in values)]
                )
                for read in resolved_calls(node, resolver)
            ):
                found.append((node.lineno, scope, f"writes JSON text to a file or a stream{remedy}"))
        problems += sanctioned_problems(name, tree, JSON_WRITE_ALLOWANCE, found)
    return problems


# Python 3.15 made UTF-8 the default text encoding where earlier releases read the locale's, so text I/O that names no
# encoding reads and writes one way on the supported floor and another on the latest release. Every call that opens,
# reads, or writes text in the repository's Python, suites included, passes encoding=, as "locale" where the locale's
# is meant; any other exception states why in a module-level ENCODING_ALLOWED = {scope: reason}.
ENCODING_ALLOWANCE = "ENCODING_ALLOWED"


# The opens and process starts come from call_tables: TEXT_OPENERS, PATH_METHODS, TEXT_METHODS, and OTHER_OPENS, and
# each PROCESS_STARTS entry that decodes its output as text, always or when one of TEXT_PROCESS_OPTIONS asks: subprocess
# opens its pipes in text when given text=, universal_newlines=, or errors=, in the locale's encoding unless given one.
TEXT_PROCESS_OPTIONS = ("text", "universal_newlines", "errors")


def _openers(call: ast.Call, aliases: dict[str, str]) -> tuple[TextOpener, ...]:
    """Each reading of where a call takes its mode and encoding when it may open text, or none for any other call: a
    module's opener, the method of the path class it names unbound, as in `Path.read_text(path)`, which takes the path
    first, so each of its other arguments sits one slot later, or, on any other receiver, the method of every path
    class that defines it."""
    qualified = qualified_name(call.func, aliases)
    if qualified in TEXT_OPENERS:
        return (TEXT_OPENERS[qualified],)
    if not isinstance(call.func, ast.Attribute) or qualified in OTHER_OPENS:
        return ()
    methods = PATH_METHODS.get(qualified_name(call.func.value, aliases))
    if methods is None:
        return TEXT_METHODS.get(call.func.attr, ())
    opener = methods.get(call.func.attr)
    if opener is None:
        return ()
    return (
        replace(
            opener,
            mode=None if opener.mode is None else opener.mode + 1,
            encoding=None if opener.encoding is None else opener.encoding + 1,
        ),
    )


def _opens_text(call: ast.Call, opener: TextOpener, method: bool) -> bool:
    """Whether a call opens text by its mode: a mode it cannot read counts as text for a module's opener, and as some
    other object's open, such as an archive member's, for a method."""
    if opener.mode is None and not opener.default_mode:
        return True
    given = call_argument(call, opener.mode, "mode")
    if given is None:
        return "b" not in opener.default_mode
    if isinstance(given, ast.Constant) and isinstance(given.value, str):
        return "b" not in given.value
    return not method


def _names_encoding(call: ast.Call, position: int | None, keywords: Sequence[str] = ("encoding",)) -> bool:
    """Whether a call passes an encoding, at its position or by one of its keywords, or an unpacking that may hold one:
    any value but a constant that is no string, as None, or a buffering size given where another class takes the
    encoding."""
    given = call_argument(call, position, *keywords)
    if isinstance(given, ast.UnaryOp) and isinstance(given.operand, ast.Constant):
        given = given.operand
    return given is not None and not (isinstance(given, ast.Constant) and not isinstance(given.value, str))


def _starts_text_process(call: ast.Call, aliases: dict[str, str]) -> bool:
    """Whether a call starts a process whose output it decodes as text: always, as os.popen and subprocess.getoutput
    do, or because its text=, universal_newlines=, or errors= asks."""
    start = PROCESS_STARTS.get(qualified_name(call.func, aliases))
    if start is None or start.text == "never":
        return False
    return start.text == "always" or any(
        not (isinstance(keyword.value, ast.Constant) and not keyword.value.value)
        for keyword in call.keywords
        if keyword.arg in TEXT_PROCESS_OPTIONS
    )


def _encoding_refusal(call: ast.Call, aliases: dict[str, str]) -> str | None:
    """What a text open, read, write, or process start that names no encoding is refused for, or None. A method whose
    receiver may be a path of more than one class is refused only when no class's reading of it names an encoding."""
    if _starts_text_process(call, aliases):
        if _names_encoding(call, None):
            return None
        return f"decodes {qualified_name(call.func, aliases)}'s output as text"
    qualified = qualified_name(call.func, aliases)
    method = qualified not in TEXT_OPENERS
    openers = _openers(call, aliases)
    if not openers or not all(
        _opens_text(call, opener, method) and not _names_encoding(call, opener.encoding, opener.encoding_keywords)
        for opener in openers
    ):
        return None
    what = call.func.attr if method and isinstance(call.func, ast.Attribute) else qualified
    return f"opens text with {what}()"


def encoding_problems(root: Path, files: list[Path]) -> list[str]:
    """Each call in the repository's Python, suites included, that opens, reads, or writes text or decodes a process's
    output without an encoding, that no ENCODING_ALLOWED entry sanctions."""
    problems: list[str] = []
    paths = [path for path in files if path.suffix == ".py" and under(path.relative_to(root).as_posix(), FORMAT_ROOTS)]
    if not paths:
        # No module read is no evidence that every text open names its encoding.
        return ["could not measure: no Python under the format roots was given, so no text open was looked for"]
    remedy = (
        " without an encoding, which reads the locale's before Python 3.15 and UTF-8 from it; pass encoding=, "
        f'"locale" where the locale\'s is meant, or state why in {ENCODING_ALLOWANCE}'
    )
    for _, name, tree in python_trees(
        sorted(paths, key=lambda path: path_order(path.relative_to(root))), root, problems
    ):
        aliases = import_aliases(tree)
        found = [
            (node.lineno, scope, f"{refusal}{remedy}")
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.Call) and (refusal := _encoding_refusal(node, aliases))
        ]
        problems += sanctioned_problems(name, tree, ENCODING_ALLOWANCE, found)
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
        "ctypes.oledll.LoadLibrary",
        "ctypes.pydll.LoadLibrary",
    }
)
LIBRARY_NAMESPACES = frozenset({"ctypes.windll", "ctypes.cdll", "ctypes.oledll", "ctypes.pydll"})
# The libraries ctypes loads itself, which a module reaches by name.
LIBRARY_OBJECTS = frozenset({"ctypes.pythonapi"})
# What a library or a foreign function holds that is no foreign function.
FOREIGN_MEMBERS = frozenset({"LoadLibrary", "argtypes", "restype", "errcheck", "_handle", "_name"})
FOREIGN_DECLARATIONS = ("argtypes", "restype")
CTYPES_ALLOWANCE = "CTYPES_ALLOWED"


# A foreign function is keyed by its library and its name, so a declaration on one library's function is none for
# another's of the same name. A library is keyed by where it is loaded: the call that loads it, by its position, so
# `lib` bound to one library in one function and to another in the next are two, or the name a library ctypes keeps
# for every caller is reached by, as ctypes.windll.kernel32.
ForeignFunction = tuple[str, str]
# The places an assignment binds a library or a function to beyond a name the resolver follows: an attribute such as
# self.kernel32, or a subscript such as libraries["k"], each keyed by place_key, and a name a class body binds.
HELD_PLACES = (ast.Attribute, ast.Subscript)


class _ForeignLibraries:
    """What a module binds to a library ctypes loads, through the shared resolution of names, getattr, and
    functools.partial (bound_values, resolved_references, resolved_calls): each binding of a name that may reach its
    read, an attribute, a subscript, or a class-level name an assignment binds to one, and the module's functions that
    return one. Every library an expression may be counts."""

    def __init__(self, tree: ast.Module, resolver: CallableResolver) -> None:
        self.resolver = resolver
        nodes = list(scoped_nodes(tree.body))
        self.scopes = {id(node): scope for node, scope in nodes}
        self.class_names = {scope for node, scope in nodes if isinstance(node, ast.ClassDef)}
        self.bases = {scope: node.bases for node, scope in nodes if isinstance(node, ast.ClassDef)}
        self.places: dict[str, list[str]] = {}
        self.loaders: dict[str, list[str]] = {}
        self.functions: dict[str, list[ForeignFunction]] = {}
        held = [
            (place_key(target, scope, self.class_names), node.value)
            for node, scope in nodes
            if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None
            for target in _assignment_targets(node)
            if isinstance(target, HELD_PLACES) or (isinstance(target, ast.Name) and scope in self.class_names)
        ]
        returns = [
            (f"<local>.{function.name}", node.value)
            for function in _top_level(tree.body)
            if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
            for node in ast.walk(function)
            if isinstance(node, ast.Return) and node.value is not None
        ]
        # A place bound to a loader's result is a library, and so is a place bound to that place, to the fixed point.
        while True:
            added = False
            for table, pairs in ((self.places, held), (self.loaders, returns)):
                for key, value in pairs:
                    for library in self.libraries(value):
                        if library not in table.setdefault(key, []):
                            table[key].append(library)
                            added = True
            if not added:
                break
        for key, value in held:
            self.functions.setdefault(key, []).extend(self.foreign(value))

    def scope(self, node: ast.AST) -> str:
        return self.scopes.get(id(node), MODULE_SCOPE)

    def libraries(self, node: ast.expr) -> list[str]:
        """The key of each library ctypes loads that an expression may be."""
        found: list[str] = []
        for value in bound_values(node, self.resolver):
            found.extend(
                reference.name
                for reference in resolved_references(value, self.resolver)
                if reference.name in LIBRARY_OBJECTS
                or (
                    reference.attribute
                    and reference.attribute not in FOREIGN_MEMBERS
                    and reference.receiver is not None
                    and any(
                        owner.name in LIBRARY_NAMESPACES
                        for owner in resolved_references(reference.receiver, self.resolver)
                    )
                )
            )
            if isinstance(value, ast.Call):
                for read in resolved_calls(value, self.resolver):
                    if read.name in LIBRARY_LOADERS:
                        found.append(f"{read.name}@{value.lineno}:{value.col_offset}")
                    found += self.loaders.get(read.name, [])
            elif isinstance(value, HELD_PLACES):
                found += self.places.get(place_key(value, self.scope(value), self.class_names), [])
        return list(dict.fromkeys(found))

    def foreign(self, node: ast.expr) -> list[ForeignFunction]:
        """The library and the name of each foreign function an expression may be: an attribute of a library, a getattr
        of a constant name on one, or a place bound to either."""
        found: list[ForeignFunction] = []
        for value in bound_values(node, self.resolver):
            for reference in resolved_references(value, self.resolver):
                if (
                    reference.attribute
                    and reference.attribute not in FOREIGN_MEMBERS
                    and reference.receiver is not None
                ):
                    found += [(library, reference.attribute) for library in self.libraries(reference.receiver)]
            if isinstance(value, HELD_PLACES):
                found += self.functions.get(place_key(value, self.scope(value), self.class_names), [])
        return list(dict.fromkeys(found))

    def called(self, call: ast.Call) -> dict[ForeignFunction, bool]:
        """Each foreign function a call may run, through its function or a functools.partial, a getattr, or an
        operator.attrgetter of one (resolved_calls), with whether any way of making it gives arguments."""
        found = dict.fromkeys(self.foreign(call.func), bool(call.args or call.keywords))
        for read in resolved_calls(call, self.resolver):
            if read.attribute and read.attribute not in FOREIGN_MEMBERS and read.receiver is not None:
                given = bool(read.args or read.keywords or read.unread)
                for library in self.libraries(read.receiver):
                    found[library, read.attribute] = found.get((library, read.attribute), False) or given
        return found

    def declarations(self, node: ast.AST) -> Iterator[tuple[ForeignFunction, str]]:
        """Each foreign function and declaration, argtypes or restype, a node assigns: by every target of an
        assignment, chained or unpacked, by an annotated assignment, or by a setattr of a constant name."""
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            for target in _assignment_targets(node):
                if isinstance(target, ast.Attribute) and target.attr in FOREIGN_DECLARATIONS:
                    for function in self.foreign(target.value):
                        yield function, target.attr
        if isinstance(node, ast.Call):
            for read in resolved_calls(node, self.resolver):
                declared = read.args[1] if read.name == "setattr" and len(read.args) == 3 else None
                if isinstance(declared, ast.Constant) and declared.value in FOREIGN_DECLARATIONS:
                    for function in self.foreign(read.args[0]):
                        yield function, str(declared.value)


def _assignment_targets(node: ast.Assign | ast.AnnAssign) -> Iterator[ast.expr]:
    """Each place an assignment stores to: every target of a chained assignment, and each item of an unpacked one."""
    pending: list[ast.expr] = list(node.targets) if isinstance(node, ast.Assign) else [node.target]
    while pending:
        target = pending.pop(0)
        if isinstance(target, ast.Tuple | ast.List):
            pending += target.elts
        elif isinstance(target, ast.Starred):
            pending.append(target.value)
        else:
            yield target


def _read_where_bound(node: ast.Attribute, scope: str, libraries: _ForeignLibraries) -> bool:
    """Whether an attribute read names the library its own class binds: a plain `self`, `cls`, or class name as the
    receiver, and that class's binding of the attribute. A read off ctypes's own namespaces, as
    `ctypes.windll.kernel32`, loads the library by its name, which the policy keys itself, so it is no class's read."""
    receiver = node.value
    if any(reference.name in LIBRARY_NAMESPACES for reference in resolved_references(receiver, libraries.resolver)):
        return True
    plain = isinstance(receiver, ast.Name) and (receiver.id in ("self", "cls") or receiver.id in libraries.class_names)
    return plain and place_key(node, scope, libraries.class_names) in libraries.places


class _InheritedLibraries:
    """The attributes each class binds to a ctypes library, in its own module and through every base it has, followed
    through the modules' imports into the module that defines the base, so a class that reads a library its base
    binds in another module is found. A module is reached by the last part of the name its import gives, among the
    modules read; every module of that name counts."""

    def __init__(self, models: list[tuple[str, _ForeignLibraries]]) -> None:
        self.modules: dict[str, list[_ForeignLibraries]] = {}
        for name, libraries in models:
            self.modules.setdefault(PurePosixPath(name).stem, []).append(libraries)

    def bound(self, libraries: _ForeignLibraries, class_name: str, seen: frozenset[tuple[int, str]]) -> set[str]:
        """The attributes a class of `libraries`, and each of its bases, binds to a library."""
        found = {
            attribute
            for key in libraries.places
            for owner, _, attribute in [key.rpartition(".")]
            if owner == class_name
        }
        for base in libraries.bases.get(class_name, []):
            module, _, base_name = libraries.resolver.name(base).rpartition(".")
            owners = [libraries] if module == "<local>" else self.modules.get(module.rpartition(".")[2], [])
            for owner in owners:
                if base_name in owner.class_names and (id(owner), base_name) not in seen:
                    found |= self.bound(owner, base_name, seen | {(id(owner), base_name)})
        return found

    def held(self, libraries: _ForeignLibraries) -> set[str]:
        """The attributes the module's classes bind to a library or inherit as one from a base, this module's or
        another's."""
        found: set[str] = set()
        for class_name in libraries.class_names:
            found |= self.bound(libraries, class_name, frozenset({(id(libraries), class_name)}))
        return found


def ctypes_declaration_problems(root: Path, files: list[Path]) -> list[str]:
    """Each call of a ctypes foreign function whose module does not assign that library's function both argtypes and
    restype, argtypes excepted for a function every call gives no arguments, that no CTYPES_ALLOWED entry
    sanctions."""
    problems: list[str] = []
    models = [
        (name, tree, _ForeignLibraries(tree, CallableResolver(tree)))
        for _, name, tree in python_trees(claim_files(root, files), root, problems)
    ]
    inherited = _InheritedLibraries([(name, libraries) for name, _, libraries in models])
    for name, tree, libraries in models:
        declared: dict[ForeignFunction, set[str]] = {}
        calls: list[tuple[ast.Call, str, ForeignFunction]] = []
        given: set[ForeignFunction] = set()
        for node, scope in scoped_nodes(tree.body):
            for declared_function, declaration in libraries.declarations(node):
                declared.setdefault(declared_function, set()).add(declaration)
            if isinstance(node, ast.Call):
                for function, arguments in libraries.called(node).items():
                    calls.append((node, scope, function))
                    if arguments:
                        given.add(function)
        # A library also hands out a foreign function by item lookup, which reads its name as a value; it is refused,
        # so every foreign function a module calls is an attribute this reader keys and checks.
        found: list[tuple[int, str, str]] = [
            (
                node.lineno,
                scope,
                f"reaches a foreign function of a ctypes library by subscript; name it as an attribute of the library, "
                f"or state why in {CTYPES_ALLOWANCE}",
            )
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.Subscript) and libraries.libraries(node.value)
        ]
        # A library a class binds to `self.x`, or in its body, is read only through that class. A read of the
        # attribute is accepted only when its receiver is a plain `self`, `cls`, or a class's name and that class
        # binds it; any other read, through a subclass, `super()`, `type(self)`, `self.__class__`, or any other
        # receiver, is refused rather than followed through the class's bases. A base another module defines counts:
        # an attribute it binds to a library is held here too, so a subclass's read of it is refused.
        held = {key.rpartition(".")[2] for key in libraries.places if key.partition(".")[0] in libraries.class_names}
        held |= inherited.held(libraries)
        found += [
            (
                node.lineno,
                scope,
                f"reads the ctypes library {ast.unparse(node)} other than through the class that binds it; read it "
                f"as self, cls, or that class's name in the class that binds it, or state why in {CTYPES_ALLOWANCE}",
            )
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and node.attr in held
            and not _read_where_bound(node, scope, libraries)
        ]
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
                        f"calls {function[1]} through ctypes with no {' or '.join(missing)} assigned in the module, "
                        f"so ctypes passes it C ints; assign both, or state why in {CTYPES_ALLOWANCE}",
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


def _client_bases(node: ast.ClassDef, resolver: CallableResolver) -> list[str]:
    """The qualified name of each base a class names, through the module's imports."""
    return [name for base in node.bases if (name := resolver.name(base))]


def git_client_classes(trees: list[tuple[str, ast.Module]]) -> set[str]:
    """GitClient and each class that subclasses it, at any depth, by the qualified name a caller reaches it by, and
    each name a module's top level binds to one, as a re-export does, followed through every module that binds it
    again; a class a module defines inside a function or another class is read by _GitClients alone."""
    classes = {RESULT_CLIENT}
    bases: list[tuple[str, list[str]]] = []
    bindings: list[tuple[str, str]] = []
    for name, tree in trees:
        resolver = CallableResolver(tree)
        key = _module_key(name)
        # A base the module defines itself is reached by its module's name, as a caller elsewhere reaches it.
        bases += [
            (
                f"{key}.{node.name}",
                [
                    f"{key}.{base.removeprefix('<local>.')}" if base.startswith("<local>.") else base
                    for base in _client_bases(node, resolver)
                ],
            )
            for node in _top_level(tree.body)
            if isinstance(node, ast.ClassDef)
        ]
        bindings += [(f"{_module_key(name)}.{bound}", target) for bound, target in _module_bindings(tree, resolver)]
    while found := {
        bound
        for bound, targets in [*bases, *((bound, [target]) for bound, target in bindings)]
        if bound not in classes and any(_reaches(target, known) for target in targets for known in classes)
    }:
        classes |= found
    return classes


class _GitClients:
    """What a module binds to a GitClient or a subclass of it, through its imports: each name assigned a call of one or
    annotated as one, a parameter's included, each attribute name a class or an instance assigns or annotates so, and
    `self` or `cls` in the methods of a subclass the module defines."""

    def __init__(self, tree: ast.Module, resolver: CallableResolver, classes: set[str] | None = None) -> None:
        self.resolver = resolver
        self.classes = {RESULT_CLIENT} if classes is None else classes
        self._read_subclasses(tree)
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

    def _read_subclasses(self, tree: ast.Module) -> None:
        """Add each class of the module that subclasses a client class, by the name the resolver gives it, and record
        the first parameter of each of its methods but a static one, which is an instance of it or the class."""
        definitions = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        self.instances: set[tuple[int, str]] = set()
        subclasses: list[ast.ClassDef] = []
        while found := [
            node
            for node in definitions
            if node not in subclasses and any(self._is_class(base) for base in _client_bases(node, self.resolver))
        ]:
            subclasses += found
            self.classes |= {f"<local>.{node.name}" for node in found}
        for node in subclasses:
            for function in node.body:
                if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                given = function.args.posonlyargs + function.args.args
                static = any(self.resolver.name(decorator) == "staticmethod" for decorator in function.decorator_list)
                owner = self.resolver.scopes[id(given[0])].owner(given[0].arg) if given and not static else None
                if owner is not None:
                    self.instances.add((id(owner), given[0].arg))

    def _is_class(self, name: str) -> bool:
        """Whether a qualified name is a client class: GitClient through its package, or a subclass of it."""
        return any(resolves_to(name, known) or _reaches(name, known) for known in self.classes)

    def _bind(self, target: ast.expr) -> None:
        if isinstance(target, ast.Name):
            self.names.add(target.id)
        elif isinstance(target, ast.Attribute):
            self.attributes.add(target.attr)

    def constructed(self, node: ast.expr | None) -> bool:
        """Whether an expression is a call of GitClient or a subclass of it."""
        return isinstance(node, ast.Call) and self._is_class(self.resolver.name(node.func))

    def annotated(self, annotation: ast.expr) -> bool:
        """Whether an annotation names GitClient or a subclass of it, alone or in a union or a subscript, by name or in
        quotes."""
        return any(self._is_class(name) for name in _annotation_names(annotation, self.resolver))

    def holds(self, node: ast.expr) -> bool:
        """Whether an expression is a client: one constructed in place, a name, or an attribute bound to one, or
        `self` or `cls` in a method of a subclass."""
        if isinstance(node, ast.Name):
            scope = self.resolver.scopes.get(id(node))
            owner = scope.owner(node.id) if scope is not None else None
            return node.id in self.names or (owner is not None and (id(owner), node.id) in self.instances)
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
    trees = [(name, tree) for _, name, tree in python_trees(claim_files(root, files), root, problems)]
    classes = git_client_classes(trees)
    for name, tree in trees:
        resolver = CallableResolver(tree)
        clients = _GitClients(tree, resolver, set(classes))
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


# A git command's exit status read outside ejryan89-skill-core's git_client.py is where a failure turns into absence:
# a nonzero exit taken for "not there". The client's forms give the three answers, found, absent as git reports it,
# and could not tell, so a module reads them instead. A git result is what GitClient.run returns on a client the
# module holds, a GitClient or a subclass's instance, what a function annotated to return git_client.GitResult
# returns, awaited or not, in any module the repository's Python reads, what a method so annotated returns on an
# instance of its class in the module defining it, and a name annotated as one. A read that must stay, such as a status
# reported with git's reason or one whose failure is refused, states why in a module-level
# GIT_STATUS_ALLOWED = {scope: reason}.
GIT_STATUS_OWNER = "skills/ejryan89-skill-core/scripts/git_client.py"
GIT_RESULT = "git_client.GitResult"
GIT_STATUS_ALLOWANCE = "GIT_STATUS_ALLOWED"
# The calls and the attributes that reach a dataclass instance's fields as a whole, returncode among them.
RESULT_FIELD_READERS = frozenset({"vars", "builtins.vars", "dataclasses.asdict", "dataclasses.astuple"})
RESULT_FIELD_ATTRIBUTES = frozenset({"__dict__", "__getstate__"})


def _module_key(name: str) -> str:
    """The name a module is imported by: a skill script's bare name, or the dotted path from the repository root."""
    path = PurePosixPath(name).with_suffix("")
    return path.name if "scripts" in path.parts else ".".join(path.parts)


def _annotation_names(annotation: ast.expr, resolver: CallableResolver) -> Iterator[str]:
    """The qualified name of each name and attribute an annotation holds, a quoted one's read from its text."""
    for part in ast.walk(annotation):
        if isinstance(part, ast.Name | ast.Attribute):
            yield resolver.name(part)
        elif isinstance(part, ast.Constant) and isinstance(part.value, str):
            try:
                quoted = ast.parse(part.value.strip(), mode="eval")
            except SyntaxError:
                continue
            aliases = resolver.aliases(part)
            yield from (
                qualified_name(name, aliases) for name in ast.walk(quoted) if isinstance(name, ast.Name | ast.Attribute)
            )


def _annotated_git_result(annotation: ast.expr | None, resolver: CallableResolver) -> bool:
    return annotation is not None and any(
        resolves_to(name, GIT_RESULT) for name in _annotation_names(annotation, resolver)
    )


def _returns_git_result(function: ast.FunctionDef | ast.AsyncFunctionDef, resolver: CallableResolver) -> bool:
    return _annotated_git_result(function.returns, resolver)


def _top_level(body: Sequence[ast.stmt]) -> Iterator[ast.stmt]:
    """Each statement a module's top level runs, through the blocks of an if, a try, a with, and a loop, whose bindings
    are the module's as an unindented statement's are; never into a function or a class."""
    for node in body:
        yield node
        if isinstance(node, ast.If | ast.For | ast.AsyncFor | ast.While):
            yield from _top_level([*node.body, *node.orelse])
        elif isinstance(node, ast.Try | ast.TryStar):
            handlers = [statement for handler in node.handlers for statement in handler.body]
            yield from _top_level([*node.body, *handlers, *node.orelse, *node.finalbody])
        elif isinstance(node, ast.With | ast.AsyncWith):
            yield from _top_level(node.body)


def _assigned(target: ast.expr, value: ast.expr) -> Iterator[tuple[ast.Name, ast.expr]]:
    """Each name an assignment target binds with the value it binds it to, pairing a tuple or a list target with a
    literal tuple or list of as many values, as `r, _ = client.run(...), 1` does."""
    if isinstance(target, ast.Name):
        yield target, value
    elif (
        isinstance(target, ast.Tuple | ast.List)
        and isinstance(value, ast.Tuple | ast.List)
        and len(target.elts) == len(value.elts)
        and not any(isinstance(element, ast.Starred) for element in (*target.elts, *value.elts))
    ):
        for element, item in zip(target.elts, value.elts, strict=True):
            yield from _assigned(element, item)


def _reaches(called: str, producer: str) -> bool:
    """Whether a call's qualified name reaches a producer's: the same name, or either one the other's dotted tail, as a
    module imported by its bare name reaches one keyed by its path from the repository root."""
    return "." in called and (called == producer or producer.endswith(f".{called}") or called.endswith(f".{producer}"))


def _module_bindings(tree: ast.Module, resolver: CallableResolver) -> list[tuple[str, str]]:
    """Each name a module's top level binds to another, with what it resolves to: an import, or a simple assignment of
    a name or an attribute, unindented or under an if, a try, a with, or a loop."""
    bindings: list[tuple[str, str]] = []
    for node in _top_level(tree.body):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            bindings += [(alias.asname or alias.name, f"{node.module}.{alias.name}") for alias in node.names]
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Name | ast.Attribute):
            bindings += [
                (target.id, resolver.name(node.value)) for target in node.targets if isinstance(target, ast.Name)
            ]
    return bindings


def git_result_producers(trees: list[tuple[str, ast.Module]]) -> set[str]:
    """Each function, by the qualified name a caller reaches it by, that is annotated to return a GitResult, and each
    name a module's top level binds to one, as a re-export does, followed through every module that binds it again."""
    producers: set[str] = set()
    bindings: list[tuple[str, str]] = []
    for name, tree in trees:
        resolver = CallableResolver(tree)
        producers |= {
            f"{_module_key(name)}.{node.name}"
            for node in _top_level(tree.body)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _returns_git_result(node, resolver)
        }
        bindings += [(f"{_module_key(name)}.{bound}", target) for bound, target in _module_bindings(tree, resolver)]
    while found := {
        bound
        for bound, target in bindings
        if bound not in producers and any(_reaches(target, producer) for producer in producers)
    }:
        producers |= found
    return producers


class _GitResults:
    """What a module holds as a git result: a call that returns one, awaited or not, and each name a scope binds to such
    a call, by an assignment or an assignment expression, or annotates as a GitResult, held for that scope alone. A
    call returns one when it reaches a producer, or calls a method that a class of the module annotates to return one
    on that class's instance: `self` or `cls` in its methods, a call of the class, or a name annotated as or bound to
    one."""

    def __init__(
        self, tree: ast.Module, resolver: CallableResolver, producers: set[str], classes: set[str] | None = None
    ) -> None:
        self.resolver = resolver
        self.clients = _GitClients(tree, resolver, None if classes is None else set(classes))
        self.local = {
            f"<local>.{node.name}"
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _returns_git_result(node, resolver)
        }
        self.producers = producers
        # Each class of the module, by the name the resolver gives it, with its methods annotated to return a result.
        self.methods: dict[str, set[str]] = {}
        # Each name bound to an instance of one of those classes, by the scope that binds it, with the class.
        self.instances: dict[tuple[int, str], str] = {}
        self._read_classes(tree)
        # Each name by the scope that binds it, so a name another function binds to a result is not one here.
        self.names: set[tuple[int, str]] = set()
        nodes = list(ast.walk(tree))
        # A name bound to one a scope holds is held too, so the bindings are read until they add no name.
        while True:
            held = len(self.names)
            for node in nodes:
                self._read_binding(node)
            if len(self.names) == held:
                break
        # Each class pattern, by its node, that reads returncode on a held subject.
        self.patterns = {id(pattern) for node in nodes if isinstance(node, ast.Match) for pattern in self._reads(node)}

    def _read_binding(self, node: ast.AST) -> None:
        """Record each name one node binds to a result: by an assignment, an assignment expression, an annotation, or
        a case that captures a held subject whole."""
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for name, value in _assigned(target, node.value):
                    if self.holds(value):
                        self._bind(name, name.id)
        elif isinstance(node, ast.NamedExpr) and self.holds(node.value):
            # The scope reader visits a walrus but not its target, and binds the name where the walrus sits.
            self._bind(node, node.target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if self.annotated(node.annotation) or (node.value is not None and self.holds(node.value)):
                self._bind(node.target, node.target.id)
        elif isinstance(node, ast.arg) and node.annotation is not None and self.annotated(node.annotation):
            self._bind(node, node.arg)
        elif isinstance(node, ast.Match) and self.holds(node.subject):
            for case in node.cases:
                for pattern in _case_patterns(case.pattern):
                    if isinstance(pattern, ast.MatchAs) and pattern.name is not None:
                        self._bind(node, pattern.name)

    def _reads(self, node: ast.Match) -> Iterator[ast.MatchClass]:
        """Each class pattern of a match on a held subject that reads returncode: by its keyword, or by position on
        GitResult, whose first match argument it is."""
        if not self.holds(node.subject):
            return
        for case in node.cases:
            for pattern in _case_patterns(case.pattern):
                if not isinstance(pattern, ast.MatchClass):
                    continue
                positional = bool(pattern.patterns) and resolves_to(self.resolver.name(pattern.cls), GIT_RESULT)
                if "returncode" in pattern.kwd_attrs or positional:
                    yield pattern

    def _key(self, node: ast.AST, name: str) -> tuple[int, str] | None:
        """The scope that binds a name where the node reads or binds it, with the name, or None when none does."""
        scope = self.resolver.scopes.get(id(node))
        owner = scope.owner(name) if scope is not None else None
        return (id(owner), name) if owner is not None else None

    def _bind(self, node: ast.AST, name: str) -> None:
        if (key := self._key(node, name)) is not None:
            self.names.add(key)

    def _read_classes(self, tree: ast.Module) -> None:
        """Record each class whose methods return a result, and each name bound to an instance of one."""
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            functions = [item for item in node.body if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)]
            returning = {function.name for function in functions if _returns_git_result(function, self.resolver)}
            if not returning:
                continue
            # The resolver names a class the module defines, bound once, as <local>.<name>.
            owner = f"<local>.{node.name}"
            self.methods[owner] = returning
            for function in functions:
                given = function.args.posonlyargs + function.args.args
                static = any(self.resolver.name(decorator) == "staticmethod" for decorator in function.decorator_list)
                if given and not static and (key := self._key(given[0], given[0].arg)) is not None:
                    self.instances[key] = owner
        for node in ast.walk(tree):
            bound: ast.arg | ast.Name | None = node if isinstance(node, ast.arg) else None
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                bound = node.target
            annotation = getattr(node, "annotation", None)
            if bound is not None and annotation is not None:
                classes = set(_annotation_names(annotation, self.resolver)) & set(self.methods)
                label = bound.arg if isinstance(bound, ast.arg) else bound.id
                if classes and (key := self._key(bound, label)) is not None:
                    self.instances[key] = min(classes)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    for assigned, value in _assigned(target, node.value):
                        constructed = self._constructed(value)
                        if constructed and (key := self._key(assigned, assigned.id)) is not None:
                            self.instances[key] = constructed

    def _constructed(self, node: ast.expr | None) -> str | None:
        """The class of the module a call constructs, when its methods return a result, or None."""
        if isinstance(node, ast.Call) and (called := self.resolver.name(node.func)) in self.methods:
            return called
        return None

    def _instance_of(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            key = self._key(node, node.id)
            return self.instances.get(key) if key is not None else None
        return self._constructed(node)

    def annotated(self, annotation: ast.expr) -> bool:
        return _annotated_git_result(annotation, self.resolver)

    def is_result(self, node: ast.expr | None) -> bool:
        """Whether an expression is a call that returns a git result, awaited or not."""
        if isinstance(node, ast.Await):
            node = node.value
        if not isinstance(node, ast.Call):
            return False
        function = node.func
        if isinstance(function, ast.Attribute) and function.attr == "run" and self.clients.holds(function.value):
            return True
        if isinstance(function, ast.Attribute) and (owner := self._instance_of(function.value)) is not None:
            return function.attr in self.methods[owner]
        called = self.resolver.name(function)
        return bool(called) and (called in self.local or any(_reaches(called, name) for name in self.producers))

    def holds(self, node: ast.expr) -> bool:
        if isinstance(node, ast.NamedExpr):
            return self.holds(node.value)
        if isinstance(node, ast.Name):
            return self._key(node, node.id) in self.names
        return self.is_result(node)

    def status_read(self, node: ast.AST) -> bool:
        """Whether a node reads a held result's returncode: as an attribute, through a lookup of an attribute by its
        name or an attrgetter given any name but a constant that names another attribute, by a class pattern, or by
        reaching the result's fields as a whole, which hold it."""
        if isinstance(node, ast.pattern):
            return id(node) in self.patterns
        if isinstance(node, ast.Attribute):
            if node.attr in RESULT_FIELD_ATTRIBUTES:
                return self.holds(node.value)
            return node.attr == "returncode" and isinstance(node.ctx, ast.Load) and self.holds(node.value)
        if not isinstance(node, ast.Call):
            return False
        called = self.resolver.name(node.func)
        if called in ATTRIBUTE_READERS:
            return len(node.args) >= 2 and _may_name_status(node.args[1]) and self.holds(node.args[0])
        if called in RESULT_FIELD_READERS:
            return bool(node.args) and self.holds(node.args[0])
        if isinstance(node.func, ast.Attribute) and node.func.attr == "__getattribute__":
            return bool(node.args) and _may_name_status(node.args[0]) and self.holds(node.func.value)
        getter = node.func
        return (
            isinstance(getter, ast.Call)
            and self.resolver.name(getter.func) in ATTRIBUTE_GETTERS
            and any(_may_name_status(name) for name in getter.args)
            and bool(node.args)
            and self.holds(node.args[0])
        )


def _case_patterns(pattern: ast.pattern) -> Iterator[ast.pattern]:
    """A case's pattern and each one it matches the whole subject with: an `as` pattern's own and each alternative of
    an `|` pattern, through any nesting."""
    yield pattern
    if isinstance(pattern, ast.MatchAs) and pattern.pattern is not None:
        yield from _case_patterns(pattern.pattern)
    elif isinstance(pattern, ast.MatchOr):
        for alternative in pattern.patterns:
            yield from _case_patterns(alternative)


def _may_name_status(name: ast.expr) -> bool:
    """Whether an attribute name a lookup is given may be returncode: that constant, a dotted path that starts with
    it, or anything that is not a constant."""
    if not isinstance(name, ast.Constant):
        return True
    return isinstance(name.value, str) and name.value.split(".", 1)[0] == "returncode"


def git_status_problems(root: Path, files: list[Path]) -> list[str]:
    """Each read of a git result's exit status outside git_client.py that no GIT_STATUS_ALLOWED entry sanctions."""
    problems: list[str] = []
    trees = [(name, tree) for _, name, tree in python_trees(claim_files(root, files), root, problems)]
    producers = git_result_producers(trees)
    classes = git_client_classes(trees)
    for name, tree in trees:
        if name == GIT_STATUS_OWNER:
            continue
        results = _GitResults(tree, CallableResolver(tree), producers, classes)
        found = [
            (
                node.lineno,
                scope,
                "reads a git command's exit status, where a failure can read as absence; use git_client's "
                f"quiet_output, commit_of, file_at, or rev_parse_paths, or state why in {GIT_STATUS_ALLOWANCE}",
            )
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.expr | ast.pattern) and results.status_read(node)
        ]
        problems += sanctioned_problems(name, tree, GIT_STATUS_ALLOWANCE, found)
    return problems


# A gh listing cut at its --limit reads as complete, so a listing asks github_client's capped_list for one more than
# it keeps and says when GitHub held more. A --limit any other way is refused outside github_client.py, and one that
# must stay states why in a module-level GH_LIMIT_ALLOWED = {scope: reason}.
GH_LIMIT_OWNER = "skills/ejryan89-skill-core/scripts/github_client.py"
GH_LIMIT_ALLOWANCE = "GH_LIMIT_ALLOWED"
# gh's limit option, long or short, alone, with its value after =, or with digits attached; an f-string's `-L{n}` holds
# the `-L` alone.
GH_LIMIT_OPTION = re.compile(r"--limit(?:=.*)?|-L[0-9]*")


def gh_limit_problems(root: Path, files: list[Path]) -> list[str]:
    """Each `--limit` a module passes gh outside github_client.py that no GH_LIMIT_ALLOWED entry sanctions."""
    problems: list[str] = []
    for _, name, tree in python_trees(claim_files(root, files), root, problems):
        if name == GH_LIMIT_OWNER:
            continue
        found = [
            (
                node.lineno,
                scope,
                f"passes gh a --limit by hand, which reads a cut listing as complete; use github_client's capped_list, "
                f"or state why in {GH_LIMIT_ALLOWANCE}",
            )
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and GH_LIMIT_OPTION.fullmatch(node.value) is not None
        ]
        problems += sanctioned_problems(name, tree, GH_LIMIT_ALLOWANCE, found)
    return problems


# git quotes a name holding a non-ASCII byte or a newline in a listing it prints without -z, and a byte a ref name, a
# path, or a message can hold joins a format's fields, so a reader that splits on lines or on that byte misreads the
# name. NUL is the one byte git refuses in them. The arguments that start a name listing, a call's positional ones or
# a list's or tuple's items, pass -z or go to GitClient.names, which adds it, and a for-each-ref or log format joins
# its fields with NUL. str.splitlines() also breaks on U+0085, U+2028, and other characters a ref name or a path can
# hold, so git's output, a call's result, its stdout, or a name a scope binds to either, is split on "\n" or on NUL,
# never with splitlines(), bound or unbound as str.splitlines(out), nor after what keeps its text, as
# out.strip().splitlines() does: a string method, a str() or a bytes(), a slice, a formatted string, a format(), a %,
# a concatenation, or a join. Nor is it trimmed or split by strip(), lstrip(), rstrip(), split(), or rsplit() given no
# separator or characters, or None for them, which drop or break on U+00A0, U+2028, and the other whitespace such a
# name can hold: git_client.output_line takes off the one "\n" that ends a line git wrote. Every subcommand's output
# counts for a call whose first element, the arguments read in order with each list among them read as its items,
# is the literal "git" or a name that says git, as subprocess.run(["git", ...]) or run_tool([git, "--exec-path"]), or
# whose function's name says git, one whose subcommand its arguments name only at
# run time included, whatever their shape. A listing, a format, a split, or a trim that must stay states why in a
# module-level GIT_SEPARATOR_ALLOWED = {scope: reason}.
GIT_SEPARATOR_OWNER = "skills/ejryan89-skill-core/scripts/git_client.py"
GIT_SEPARATOR_ALLOWANCE = "GIT_SEPARATOR_ALLOWED"
# The options that make a diff, or a log or a show of a commit's changes, list the paths it touches.
DIFF_NAME_OPTIONS = frozenset({"--name-only", "--name-status", "--raw", "--numstat"})
# Each subcommand that lists names, with the options that make it list them; none means it always does, as the
# plumbing diffs print raw output unless told otherwise, and grep, check-ignore, and check-attr print each path they
# match. An option counts alone or with a value after =, as --porcelain=v2.
GIT_NAME_LISTINGS: dict[str, frozenset[str]] = {
    "check-attr": frozenset(),
    "check-ignore": frozenset(),
    "diff": DIFF_NAME_OPTIONS,
    "diff-files": frozenset(),
    "diff-index": frozenset(),
    "diff-tree": frozenset(),
    "grep": frozenset(),
    "log": DIFF_NAME_OPTIONS,
    "ls-files": frozenset(),
    "ls-tree": frozenset(),
    "show": DIFF_NAME_OPTIONS,
    "status": frozenset({"--porcelain", "--short", "-s"}),
    "worktree": frozenset({"--porcelain"}),
}
# The options that end each name a listing prints with NUL: -z, and grep's own spelling of it.
GIT_NUL_OPTIONS = frozenset({"-z", "--null"})
# The options that make a listing print no name, as `check-ignore -q` and `diff-index --quiet` answer by their status.
GIT_SILENT_OPTIONS = frozenset({"-q", "--quiet"})
# The listings that do nothing without an argument after the subcommand, so a sequence that gives none, as a label
# beside an error does, is no such command.
GIT_ARGUMENT_LISTINGS = frozenset({"check-attr", "check-ignore", "grep"})
GIT_FORMAT_COMMANDS = frozenset({"for-each-ref", "log"})
# The str methods that, given no separator or characters, or None for them, drop or split on any whitespace,
# U+00A0 and U+2028 among it, with the keyword each takes its separator or characters by.
GIT_WHITESPACE_TRIMS = {"lstrip": "chars", "rstrip": "chars", "strip": "chars", "split": "sep", "rsplit": "sep"}
# What a git call whose subcommand only its arguments name at run time is reported as.
ANY_SUBCOMMAND = "<any subcommand>"
# The options a format follows, after = or as the next argument, and the prefixes --pretty's form may give it.
GIT_FORMAT_OPTIONS = frozenset({"--format", "--pretty"})
GIT_FORMAT_PREFIXES = ("format:", "tformat:")
# A format's placeholders: an atom such as %(refname), a log field such as %H or %an, NUL as for-each-ref (%00) and
# log (%x00) write it, and the escapes that print a byte, which join fields as literal text does.
GIT_FORMAT_TOKEN = re.compile(r"%%|%x00|%00|%x[0-9a-fA-F]{2}|%n|%\([^)]*\)|%[a-zA-Z][a-zA-Z?]?")
GIT_FORMAT_NUL = frozenset({"%00", "%x00"})
# The str and bytes methods whose result still holds the text they are given, so a split of it splits git's output.
TEXT_KEEPING_METHODS = frozenset(
    {
        "casefold",
        "decode",
        "expandtabs",
        "lower",
        "lstrip",
        "removeprefix",
        "removesuffix",
        "replace",
        "rstrip",
        "strip",
        "upper",
    }
)
# The git subcommands whose output names a ref, a path, an object, or a commit, which a split or a trim of that output
# reads when the call that runs them is not named for git, as a wrapper such as quiet(services, root, ...) is not. A
# call whose function's name has the word git in it, as runtime_support.names_git reads it, counts whatever its
# subcommand.
GIT_OUTPUT_COMMANDS = frozenset(
    {
        "branch",
        "cat-file",
        "check-attr",
        "check-ignore",
        "check-ref-format",
        "commit-tree",
        "config",
        "describe",
        "diff",
        "diff-files",
        "diff-index",
        "diff-tree",
        "fast-import",
        "for-each-ref",
        "grep",
        "hash-object",
        "log",
        "ls-files",
        "ls-remote",
        "ls-tree",
        "merge-base",
        "mktree",
        "name-rev",
        "remote",
        "rev-list",
        "rev-parse",
        "show",
        "show-ref",
        "status",
        "symbolic-ref",
        "tag",
        "update-index",
        "worktree",
        "write-tree",
    }
)
GIT_FORMAT_ESCAPES = re.compile(r"%%|%x[0-9a-fA-F]{2}|%n")
# The types whose call of a value holds its text, given an encoding or not, by the names they are reached by, and
# on which a string method may be named unbound, as in `str.strip(out)`.
TEXT_TYPES = frozenset({"str", "bytes", "builtins.str", "builtins.bytes"})
# The str and bytes methods whose result holds the text of its receiver and of each argument it is given, through the
# items of a literal sequence or mapping, as `"{}".format(out)` and `"".join([out])` hold out's.
TEXT_JOINING_METHODS = frozenset({"format", "format_map", "join"})


def _literal(node: ast.expr) -> object:
    """A constant's value, or None for anything else."""
    return node.value if isinstance(node, ast.Constant) else None


def _git_command(elements: Sequence[ast.expr]) -> tuple[str, list[str]] | None:
    """The git subcommand a sequence of arguments starts, and the strings after it, or None when it starts none: the
    first string that is not git itself, an option of git's own, or that option's value."""
    strings = [(element.value if isinstance(element, ast.Constant) else None) for element in elements]
    index = 0
    while index < len(strings):
        value = strings[index]
        if isinstance(value, str) and value in GLOBAL_VALUE_OPTIONS:
            index += 2
            continue
        if isinstance(value, str) and value != "git" and not value.startswith("-"):
            return value, [later for later in strings[index + 1 :] if isinstance(later, str)]
        index += 1
    return None


def _lists_names(command: str, options: list[str]) -> bool:
    """Whether a git command with these options lists names."""
    listing = GIT_NAME_LISTINGS.get(command)
    if listing is None or GIT_SILENT_OPTIONS & set(options) or (command in GIT_ARGUMENT_LISTINGS and not options):
        return False
    return not listing or any(option.split("=", 1)[0] in listing for option in options)


def _git_formats(options: list[str]) -> Iterator[str]:
    """Each format among a git command's options."""
    for index, option in enumerate(options):
        name, equals, value = option.partition("=")
        if name not in GIT_FORMAT_OPTIONS:
            continue
        text = value if equals else next(iter(options[index + 1 :]), None)
        if text is not None:
            yield text.removeprefix(GIT_FORMAT_PREFIXES[0]).removeprefix(GIT_FORMAT_PREFIXES[1])


def _format_joint(text: str) -> str | None:
    """The first text that joins two fields of a git format, when it is anything but NUL."""
    end: int | None = None
    for token in GIT_FORMAT_TOKEN.finditer(text):
        if GIT_FORMAT_ESCAPES.fullmatch(token.group()) or token.group() in GIT_FORMAT_NUL:
            continue
        if end is not None and text[end : token.start()] not in GIT_FORMAT_NUL:
            return text[end : token.start()]
        end = token.end()
    return None


def _separator_refusal(elements: Sequence[ast.expr]) -> str | None:
    """Why a sequence of arguments reads git output split on a byte the data can hold, or None when it does not."""
    started = _git_command(elements)
    if started is None:
        return None
    command, options = started
    if _lists_names(command, options) and not GIT_NUL_OPTIONS & set(options):
        return (
            f"lists names with git {command} without -z, so git quotes one holding a non-ASCII byte or a newline; "
            f"use GitClient.names, or pass -z and split on NUL, or state why in {GIT_SEPARATOR_ALLOWANCE}"
        )
    if command in GIT_FORMAT_COMMANDS:
        for text in _git_formats(options):
            if (joint := _format_joint(text)) is not None:
                return (
                    f"joins the fields of a git {command} format with {joint!r}, which a name or a message can hold; "
                    f"join them with NUL, %00 for for-each-ref or %x00 for log, or state why in "
                    f"{GIT_SEPARATOR_ALLOWANCE}"
                )
    return None


def _items(node: ast.expr) -> list[ast.expr]:
    """The values a literal tuple, list, set, or dict holds, or the expression itself for anything else."""
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return [item.value if isinstance(item, ast.Starred) else item for item in node.elts]
    if isinstance(node, ast.Dict):
        return list(node.values)
    return [node]


class _GitOutputs:
    """What a module holds as git's output, each name by the scope that binds it, wherever in the module it is read:
    the arguments a name is bound to as a literal list or tuple, and the git subcommand whose output a name holds. A
    name holds it when an assignment, an annotated assignment, an assignment expression, or an augmented one binds
    it to an expression whose text holds that output."""

    def __init__(self, tree: ast.Module, resolver: CallableResolver) -> None:
        self.resolver = resolver
        self.lists: dict[tuple[int, str], Sequence[ast.expr]] = {}
        self.outputs: dict[tuple[int, str], str] = {}
        nodes = list(ast.walk(tree))
        # A binding may read a name another binds later in the walk, so they are read until they add no name.
        while True:
            known = len(self.lists) + len(self.outputs)
            for node in nodes:
                for place, name, value in self._bindings(node):
                    key = self._key(place, name)
                    if isinstance(value, ast.List | ast.Tuple):
                        self.lists.setdefault(key, value.elts)
                    if key not in self.outputs and (command := self.command(value)) is not None:
                        self.outputs[key] = command
            if len(self.lists) + len(self.outputs) == known:
                break

    @staticmethod
    def _bindings(node: ast.AST) -> Iterator[tuple[ast.AST, str, ast.expr]]:
        """Each name a node binds, with the node whose scope reads it and the value it binds the name to."""
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield from ((name, name.id, value) for name, value in _assigned(target, node.value))
        elif isinstance(node, ast.AnnAssign | ast.AugAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                yield node.target, node.target.id, node.value
        elif isinstance(node, ast.NamedExpr):
            yield node, node.target.id, node.value

    def _key(self, node: ast.AST, name: str) -> tuple[int, str]:
        scope = self.resolver.scopes.get(id(node))
        owner = scope.owner(name) if scope is not None else None
        return (id(owner) if owner is not None else 0, name)

    def _unbound(self, method: ast.Attribute) -> bool:
        """Whether a method is named on str or bytes rather than on a value, as in `str.strip(out)`."""
        return self.resolver.name(method.value) in TEXT_TYPES

    def texts(self, node: ast.expr) -> list[ast.expr]:
        """Each expression whose text an expression still holds: through a string method's result, bound as
        `out.strip()` or unbound as `str.strip(out)`, a str() or a bytes() of it, given an encoding or not, a slice, a
        formatted string, a format() or a %, a concatenation or a repetition, a join of a literal sequence, and each
        branch of a conditional expression or of `and` or `or`; the expression itself when it is none of them."""
        found: list[ast.expr] = []
        pending = [node]
        while pending:
            current = pending.pop()
            beneath = self._beneath(current)
            if beneath is None:
                found.append(current)
            else:
                pending += beneath
        return found

    def _beneath(self, node: ast.expr) -> list[ast.expr] | None:
        """The expressions whose text one expression holds, a step down, or None when it holds no other's."""
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            return [node.value]
        if isinstance(node, ast.IfExp | ast.BoolOp):
            return _branches(node)
        if isinstance(node, ast.JoinedStr):
            return [part.value for part in node.values if isinstance(part, ast.FormattedValue)]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mult):
            return [node.left, node.right]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            return [node.left, *_items(node.right)]
        if not isinstance(node, ast.Call):
            return None
        if self.resolver.name(node.func) in TEXT_TYPES:
            given = call_argument(node, 0, "object")
            return [given] if given is not None and not isinstance(given, ast.Starred) else None
        method = node.func
        if not isinstance(method, ast.Attribute) or method.attr not in TEXT_JOINING_METHODS | TEXT_KEEPING_METHODS:
            return None
        arguments = node.args[1:] if self._unbound(method) else node.args
        receiver = node.args[:1] if self._unbound(method) else [method.value]
        if method.attr in TEXT_KEEPING_METHODS:
            return receiver or None
        extras = [*arguments, *(keyword.value for keyword in node.keywords)]
        return [*receiver, *(item for argument in extras for item in _items(argument))]

    def command(self, node: ast.expr) -> str | None:
        """The git subcommand whose output an expression's text holds, or None: a call whose arguments start it, a
        list or tuple among them, or a name bound to one, read in place; that call's stdout, or output as the
        deployer's run_tool gives it; or a name bound to either."""
        for text in self.texts(node):
            command = self._command(text)
            if command is not None:
                return command
        return None

    def _command(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Attribute) and node.attr in ("stdout", "output"):
            return self.command(node.value)
        if isinstance(node, ast.Name):
            return self.outputs.get(self._key(node, node.id))
        if not isinstance(node, ast.Call):
            return None
        elements: list[ast.expr] = []
        for argument in node.args:
            if isinstance(argument, ast.List | ast.Tuple):
                elements += argument.elts
            elif isinstance(argument, ast.Name) and (bound := self.lists.get(self._key(argument, argument.id))):
                elements += bound
            else:
                elements.append(argument)
        started = _git_command(elements)
        # A call names git by its function, or by its first argument: the literal "git", or a name or attribute that
        # says git, such as `run_tool([git, "--exec-path"])` given the path the locator found.
        first = elements[0] if elements else None
        named = names_git(node.func) or (
            first is not None
            and (_literal(first) == "git" or (isinstance(first, ast.Name | ast.Attribute) and names_git(first)))
        )
        if started is None:
            # A git call whose subcommand its arguments name only at run time, whatever their shape, as
            # `git_in(checkout, runner, *arguments)`, `git_in(checkout, runner, command)`, or `run(["git", name])`,
            # counts for any subcommand.
            return ANY_SUBCOMMAND if named else None
        return started[0] if named or started[0] in GIT_OUTPUT_COMMANDS else None

    def split(self, node: ast.AST) -> str | None:
        """The git subcommand whose output a splitlines() call splits, bound or unbound as `str.splitlines(out)`, or
        None."""
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "splitlines"):
            return None
        if self._unbound(node.func):
            return self.command(node.args[0]) if node.args else None
        return self.command(node.func.value)

    def trim(self, node: ast.AST) -> str | None:
        """The git subcommand whose output a strip(), lstrip(), rstrip(), split(), or rsplit() trims or splits on
        whitespace, given no separator or characters or None for them, positionally or by keyword, beside a maxsplit or
        not, bound or unbound as `str.strip(out)`, or None."""
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            return None
        keyword = GIT_WHITESPACE_TRIMS.get(node.func.attr)
        if keyword is None:
            return None
        unbound = self._unbound(node.func)
        given = node.args[1:] if unbound else node.args
        separator = given[0] if given else next((item.value for item in node.keywords if item.arg == keyword), None)
        if separator is not None and not (isinstance(separator, ast.Constant) and separator.value is None):
            return None
        receiver = (node.args[0] if node.args else None) if unbound else node.func.value
        if receiver is None or isinstance(receiver, ast.Constant):
            return None
        return self.command(receiver)


def _git_output_trims(tree: ast.Module) -> list[tuple[int, str, str]]:
    """Each strip(), lstrip(), rstrip(), or split() of git's output given no argument, by its line, its scope, and its
    refusal."""
    outputs = _GitOutputs(tree, CallableResolver(tree))
    return [
        (
            node.lineno,
            scope,
            f"trims or splits the output of git {command} on any whitespace, which also takes U+00A0, U+2028, and "
            'other characters a ref name or a path can hold; take off its last "\\n" with git_client.output_line, '
            f'split on "\\n" or NUL, or state why in {GIT_SEPARATOR_ALLOWANCE}',
        )
        for node, scope in scoped_nodes(tree.body)
        if isinstance(node, ast.Call) and (command := outputs.trim(node)) is not None
    ]


def _git_output_splits(tree: ast.Module) -> list[tuple[int, str, str]]:
    """Each splitlines() of git's output, by its line, its scope, and its refusal."""
    outputs = _GitOutputs(tree, CallableResolver(tree))
    return [
        (
            node.lineno,
            scope,
            f"splits the output of git {command} with splitlines(), which also breaks on U+0085, U+2028, and "
            'other characters a ref name or a path can hold; split on "\\n", or on NUL with -z or a %00 '
            f"format, or state why in {GIT_SEPARATOR_ALLOWANCE}",
        )
        for node, scope in scoped_nodes(tree.body)
        if isinstance(node, ast.Call) and (command := outputs.split(node)) is not None
    ]


def git_separator_problems(root: Path, files: list[Path]) -> list[str]:
    """Each name listing without -z, each git format whose fields a byte other than NUL joins, and each splitlines()
    of git's output, outside git_client.py, that no GIT_SEPARATOR_ALLOWED entry sanctions."""
    problems: list[str] = []
    for _, name, tree in python_trees(claim_files(root, files), root, problems):
        if name == GIT_SEPARATOR_OWNER:
            continue
        # GitClient.names adds -z itself, so neither its call nor a list it is given is read.
        named = {
            id(part)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "names"
            for part in (node, *node.args)
        }
        found = [
            (node.lineno, scope, refusal)
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.Call | ast.List | ast.Tuple)
            and id(node) not in named
            and (refusal := _separator_refusal(node.args if isinstance(node, ast.Call) else node.elts)) is not None
        ]
        found += _git_output_splits(tree)
        found += _git_output_trims(tree)
        problems += sanctioned_problems(name, tree, GIT_SEPARATOR_ALLOWANCE, sorted(found))
    return problems


# A GitError whose returncode is None is not one git could not run: git_client raises it, with no exit code, for an
# answer git gave in the wrong shape, such as a rev-parse that printed no object name. Whether git ran and finished is
# GitError.answered, so a handler of a GitError, or a function given one, reads that, never `returncode is None`,
# `returncode in (None, ...)`, or `not returncode`. A read that must stay states why in a module-level
# GIT_ANSWERED_ALLOWED = {scope: reason}.
GIT_ERROR = "git_client.GitError"
GIT_ANSWERED_ALLOWANCE = "GIT_ANSWERED_ALLOWED"


def _none_returncode_reads(tree: ast.Module, aliases: dict[str, str]) -> set[int]:
    """The comparisons, by id, that set the returncode of a GitError beside None: one an `except` clause catches,
    alone or among others, inside that clause's own body, or one a function takes as a parameter annotated as one,
    inside that function's body."""

    def is_git_error(kind: ast.expr | None) -> bool:
        kinds = kind.elts if isinstance(kind, ast.Tuple) else [kind] if kind is not None else []
        return any(resolves_to(qualified_name(item, aliases), GIT_ERROR) for item in kinds)

    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.name is not None and is_git_error(node.type):
            names, body = {node.name}, node.body
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            parameters = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            names, body = {argument.arg for argument in parameters if is_git_error(argument.annotation)}, node.body
        else:
            continue
        for statement in body if names else []:
            for compared in ast.walk(statement):
                if (isinstance(compared, ast.Compare) and _compares_returncode_with_none(compared, names)) or (
                    isinstance(compared, ast.UnaryOp)
                    and isinstance(compared.op, ast.Not)
                    and _is_returncode_of(compared.operand, names)
                ):
                    found.add(id(compared))
    return found


def _is_returncode_of(value: ast.expr, names: set[str]) -> bool:
    """Whether `value` reads the returncode of one of `names`."""
    return (
        isinstance(value, ast.Attribute)
        and value.attr == "returncode"
        and isinstance(value.value, ast.Name)
        and value.value.id in names
    )


def _compares_returncode_with_none(node: ast.Compare, names: set[str]) -> bool:
    """Whether a comparison sets one of `names`' returncode beside None, as a side or as a member of a literal tuple,
    list, or set it tests membership in."""
    sides = [node.left, *node.comparators]
    returncode = any(_is_returncode_of(side, names) for side in sides)

    def none(value: ast.expr) -> bool:
        if isinstance(value, ast.Tuple | ast.List | ast.Set):
            return any(map(none, value.elts))
        return isinstance(value, ast.Constant) and value.value is None

    return returncode and any(map(none, sides))


def git_answered_problems(root: Path, files: list[Path]) -> list[str]:
    """Each comparison of a caught GitError's returncode with None outside git_client.py that no GIT_ANSWERED_ALLOWED
    entry sanctions."""
    problems: list[str] = []
    for _, name, tree in python_trees(claim_files(root, files), root, problems):
        if name == GIT_STATUS_OWNER:
            continue
        reads = _none_returncode_reads(tree, import_aliases(tree))
        found = [
            (
                node.lineno,
                scope,
                "reads a GitError's returncode is None as git not running, though git_client raises one without an "
                f"exit code for an answer in the wrong shape; read GitError.answered, or state why in "
                f"{GIT_ANSWERED_ALLOWANCE}",
            )
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.Compare | ast.UnaryOp) and id(node) in reads
        ]
        problems += sanctioned_problems(name, tree, GIT_ANSWERED_ALLOWANCE, found)
    return problems


# A time limit read with type=float accepts nan, inf, zero, and a negative number, and one read with type=int accepts
# zero and a negative number, and none of them is a limit, so an option that names a time takes ejryan89-skill-core's
# seconds_option.positive_seconds as its type. Any other states why in a module-level
# TIME_OPTION_ALLOWED = {scope: reason}.
TIME_OPTION_ALLOWANCE = "TIME_OPTION_ALLOWED"
# The words, split from an option string, dest, or metavar at each hyphen, underscore, or space, that name a time: a
# limit's own words and its units. A bare "limit" or "min" names a count or a minimum as often as a time, and a "day",
# "week", or "month" a calendar span rather than a wait, so none of them is one; "--time-limit" is one by "time".
TIME_WORDS = frozenset(
    {
        "time",
        "timeout",
        "timeouts",
        "deadline",
        "wait",
        "delay",
        "interval",
        "ttl",
        "duration",
        "sleep",
        "backoff",
        "seconds",
        "second",
        "secs",
        "sec",
        "ms",
        "msec",
        "msecs",
        "millis",
        "millisecond",
        "milliseconds",
        "usec",
        "usecs",
        "microsecond",
        "microseconds",
        "minutes",
        "minute",
        "mins",
        "hours",
        "hour",
        "hrs",
    }
)
# What each type that reads a time option as no limit accepts that is none.
UNBOUNDED_TIME_TYPES = {"float": "nan, inf, 0, and a negative number", "int": "0 and a negative number"}


# The time words long enough to name a time inside a word written together, as `--sleeptime` and `--waitseconds` do,
# where a short unit such as "ms" or "sec" would match inside unrelated words.
TIME_WORD_STEMS = tuple(sorted((word for word in TIME_WORDS if len(word) >= 4), key=len, reverse=True))
# The words that start or end with a time word but name no time limit, as `--times` counts repetitions.
UNTIMED_COMPOUNDS = frozenset({"times", "timestamp", "timestamps", "timezone", "timezones", "runtime", "runtimes"})


def _time_words(text: str) -> set[str]:
    """The words of an option string, dest, or metavar, split at each hyphen, underscore, space, and lower-to-upper
    case change, lowered, with each word written together that starts or ends with a time word, as `sleeptime`,
    counted as that time word too."""
    words = {word.lower() for word in re.split(r"[-_\s]+|(?<=[a-z0-9])(?=[A-Z])", text) if word}
    for word in words - TIME_WORDS - UNTIMED_COMPOUNDS:
        words |= {stem for stem in TIME_WORD_STEMS if word.startswith(stem) or word.endswith(stem)}
    return words


def _unbounded_time_option(call: ast.Call, resolver: CallableResolver) -> tuple[str, str] | None:
    """The name of the time an add_argument call may read with type=float or type=int, among its option strings, dest,
    and metavar, with that type, or None when it reads no time so. The call is read each way it may run, through a
    name, a getattr, a functools.partial, and a `**` literal (resolved_calls), and any one that does is enough."""
    for read in resolved_calls(call, resolver):
        if read.attribute != "add_argument":
            continue
        kinds = {
            reference.name
            for given in read.keywords.get("type", [])
            for reference in resolved_references(given, resolver)
        }
        kind = next((name for name in UNBOUNDED_TIME_TYPES if name in kinds), None)
        if kind is None:
            continue
        names = [*read.args, *read.keywords.get("dest", []), *read.keywords.get("metavar", [])]
        for name in names:
            if isinstance(name, ast.Constant) and isinstance(name.value, str) and TIME_WORDS & _time_words(name.value):
                return name.value, kind
    return None


def time_option_problems(root: Path, files: list[Path]) -> list[str]:
    """Each add_argument that reads an option naming a time with type=float or type=int, which no TIME_OPTION_ALLOWED
    entry sanctions."""
    problems: list[str] = []
    for _, name, tree in python_trees(claim_files(root, files), root, problems):
        resolver = CallableResolver(tree)
        found = [
            (
                node.lineno,
                scope,
                f"reads {option} with type={kind}, which accepts {UNBOUNDED_TIME_TYPES[kind]}; use "
                f"ejryan89-skill-core's seconds_option.positive_seconds, or state why in {TIME_OPTION_ALLOWANCE}",
            )
            for node, scope in scoped_nodes(tree.body)
            if isinstance(node, ast.Call) and (read := _unbounded_time_option(node, resolver))
            for option, kind in [read]
        ]
        problems += sanctioned_problems(name, tree, TIME_OPTION_ALLOWANCE, found)
    return problems


# A caller catches the whole error family a shared reader raises on content. Each ejryan89-skill-core module that
# exports CONTENT_ERRORS, what its readers raise on bad content, names those readers in CONTENT_READERS, and
# core_reader_families names each reader of either core whose family is a class of its own, as review_io's read_json
# raises PersistenceError. A handler around a call of one, directly or through a repository function that lets part
# of the family escape, that catches part of the family or the call's OSError catches all of it, or the scope states
# why in a module-level CONTENT_ERRORS_ALLOWED = {scope: reason}; a function the allowance names lets no family
# escape, for one that cannot meet bad content.
CONTENT_ERRORS = "CONTENT_ERRORS"
CONTENT_READERS = "CONTENT_READERS"
CONTENT_ERRORS_ALLOWANCE = "CONTENT_ERRORS_ALLOWED"
SKILL_CORE_SCRIPTS = "skills/ejryan89-skill-core/scripts"
READER_ROOTS = (SKILL_CORE_SCRIPTS, "skills/ejryan89-code-review-core/scripts")
CONTENT_POLICY = "tests/validation/python_checks.py"
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
    """import_aliases, with what a package module's relative imports bind, each name the module defines itself, a
    lambda its top level binds among them, and each name its top level binds once to another name or an attribute, as
    `READ = json_reader.read_file` does, which stands for what that one does."""
    aliases = {
        node.name: f"{module}.{node.name}"
        for node in _top_level(tree.body)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    aliases |= {name: f"{module}.{name}" for name in [*_module_tuples(tree), *_module_lambdas(tree)]}
    package = module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and package:
            base = f"{package}.{node.module}" if node.module else package
            aliases |= {alias.asname or alias.name: f"{base}.{alias.name}" for alias in node.names}
    aliases |= import_aliases(tree)
    bound = [_module_binding(node) for node in _top_level(tree.body)]
    counts = Counter(name for name, _ in filter(None, bound))
    for name, value in filter(None, bound):
        if counts[name] == 1 and (target := qualified_name(value, aliases)):
            aliases[name] = target
    return aliases


def _module_binding(node: ast.stmt) -> tuple[str, ast.expr] | None:
    """The name a top-level statement binds to a name or an attribute, with that value, or None."""
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(target := node.targets[0], ast.Name)
        and isinstance(node.value, ast.Name | ast.Attribute)
    ):
        return target.id, node.value
    return None


def _module_lambdas(tree: ast.Module) -> dict[str, ast.Lambda]:
    """Each top-level name assigned a lambda, which a call of the name runs as a function."""
    return {
        target.id: node.value
        for node in _top_level(tree.body)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Lambda)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _module_tuples(tree: ast.Module) -> dict[str, ast.Tuple]:
    """Each module-level name assigned a literal tuple, such as a family of exception classes."""
    found: dict[str, ast.Tuple] = {}
    for node in _top_level(tree.body):
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


def core_reader_families() -> dict[str, str]:
    """Each reader of either core whose family on content is a class, or a tuple of classes, of its own rather than
    its module's CONTENT_ERRORS, by the name it is imported as, with that family's: review_io's read_json turns what it
    cannot read as JSON, a file past its size limit included, into PersistenceError. The table is this policy's alone,
    so it is kept here rather than in the core module every review suite imports."""
    return {"review_io.read_json": "review_io.PersistenceError"}


def _defined_functions(tree: ast.Module) -> set[str]:
    """The name of each function a module's top level defines."""
    return {node.name for node in _top_level(tree.body) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}


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


RepositoryFunctions = dict[str, tuple[str, str, Sequence[ast.AST]]]


def _repository_functions(
    trees: dict[str, tuple[str, ast.Module]],
) -> tuple[RepositoryFunctions, dict[str, dict[str, list[str]]]]:
    """Each repository function a call may run, by its qualified name, with its module, the scope an allowance names
    it by, and its body: a module-level function, a lambda the top level binds, and a class's method. And each method
    name a module's classes define, with the methods by that name, which a call of the name on any receiver may run,
    since the reader cannot tell which object a receiver is."""
    functions: RepositoryFunctions = {}
    methods: dict[str, dict[str, list[str]]] = {}
    for module, (_, tree) in trees.items():
        for node in _top_level(tree.body):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                functions[f"{module}.{node.name}"] = (module, node.name, node.body)
            elif isinstance(node, ast.ClassDef):
                for method in node.body:
                    if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
                        scope = f"{node.name}.{method.name}"
                        functions[f"{module}.{scope}"] = (module, scope, method.body)
                        methods.setdefault(module, {}).setdefault(method.name, []).append(f"{module}.{scope}")
        for name, function in _module_lambdas(tree).items():
            functions[f"{module}.{name}"] = (module, name, [function.body])
    return functions, methods


class _ContentModel:
    """The repository's modules as the content-errors policy reads them: its classes, its tuples of classes, its
    module-level functions, and each skill-core reader's family."""

    def __init__(self, root: Path, files: list[Path], problems: list[str], families: Mapping[str, str]) -> None:
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
            for node in _top_level(tree.body)
            if isinstance(node, ast.ClassDef)
        }
        self.tuples = {
            f"{module}.{name}": (module, value)
            for module, (_, tree) in self.trees.items()
            for name, value in _module_tuples(tree).items()
        }
        self.functions, self.methods = _repository_functions(self.trees)
        self.families: dict[str, dict[ClassKey, str]] = {}
        for module, (name, tree) in self.trees.items():
            if posix_within(name, SKILL_CORE_SCRIPTS) and f"{module}.{CONTENT_ERRORS}" in self.tuples:
                self.read_content_errors(module, name, tree, problems)
        for reader, family in families.items():
            self.read_content_family(reader, family, problems)
        if not self.families:
            # With no reader every handler passes, which would hold nothing; say so instead.
            problems.append(
                f"{SKILL_CORE_SCRIPTS} names no reader in a {CONTENT_READERS} beside its {CONTENT_ERRORS}, so the "
                "content-errors policy could check no handler"
            )
        self._ancestors: dict[ClassKey, frozenset[ClassKey]] = {}

    def read_content_errors(self, module: str, name: str, tree: ast.Module, problems: list[str]) -> None:
        """Give each reader a skill-core module names in its CONTENT_READERS the family its CONTENT_ERRORS names."""
        family = self.classes(f"{module}.{CONTENT_ERRORS}")
        if not family:
            problems.append(f"{name}: {CONTENT_ERRORS} must be a tuple of exception classes its imports name")
            return
        readers = _content_readers(tree)
        if readers is None or not readers or not set(readers) <= _defined_functions(tree):
            problems.append(
                f"{name}: {CONTENT_READERS} must be a tuple naming each function the module defines that raises "
                f"its {CONTENT_ERRORS} on content"
            )
            return
        for reader in readers:
            self.families[f"{module}.{reader}"] = dict.fromkeys(family, f"{module}.{CONTENT_ERRORS}")

    def read_content_family(self, reader: str, family: str, problems: list[str]) -> None:
        """Give a reader core_reader_families names the class, or the tuple of classes, it names for it."""
        module = reader.rpartition(".")[0]
        classes = self.classes(family)
        place = self.trees[module][0] if module in self.trees else ""
        if reader not in self.functions or not any(posix_within(place, root) for root in READER_ROOTS) or not classes:
            problems.append(
                f"{CONTENT_POLICY}: core_reader_families names {reader} with {family}, but each must name a "
                "module-level function of either core and the class, or the tuple of classes, it raises"
            )
            return
        if reader in self.families:
            # Its module's CONTENT_READERS gave it that module's CONTENT_ERRORS, which this entry would replace, so a
            # handler catching this family but part of that one would pass.
            problems.append(
                f"{CONTENT_POLICY}: core_reader_families names {reader}, which its module's {CONTENT_READERS} "
                f"already gives that module's {CONTENT_ERRORS}; name it in one of the two"
            )
            return
        self.families[reader] = dict.fromkeys(classes, family)

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

    def callees(self, call: ast.Call, module: str) -> list[str]:
        """The reader or repository function a call names, by its qualified name, or for a method call on a receiver
        the policy cannot resolve, each method of the module's classes by that name; none for any other call."""
        qualified = qualified_name(call.func, self.aliases[module])
        if qualified in self.families or qualified in self.functions:
            return [qualified]
        if isinstance(call.func, ast.Attribute):
            return list(self.methods.get(module, {}).get(call.func.attr, []))
        return []


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
        # Every function a call may run, a top-level lambda's included, with its shape.
        self.shapes = {qualified: self.shape(body, module) for qualified, (module, _, body) in model.functions.items()}
        self.escapes: dict[str, dict[ClassKey, str]] = {qualified: {} for qualified in model.functions}
        changed = True
        while changed:
            changed = False
            for qualified, (module, scope, _) in model.functions.items():
                found = {} if scope in self.allowed[module] else self.escaping(self.shapes[qualified], None, "")
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
            if isinstance(node, ast.Call):
                found += self.model.callees(node, module)
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


def content_error_problems(root: Path, files: list[Path], families: Mapping[str, str] | None = None) -> list[str]:
    """Each handler around a reader that exports CONTENT_ERRORS, called directly or through a repository function,
    that catches part of the reader's family, or the call's OSError, without the rest, and no CONTENT_ERRORS_ALLOWED
    entry sanctions; then each entry that sanctions nothing."""
    problems: list[str] = []
    model = _ContentModel(root, files, problems, core_reader_families() if families is None else families)
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
    try:
        ruff_job(ruff, ["format", "--check"], targets, root)
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
        ruff_job(ruff, ["check"], targets, root)
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
        try:
            mypy_job(mypy, configuration, ["--cache-dir", str(cache), *(["--strict"] if strict else []), *targets], cwd)
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
    relative = relative_within(cwd, REPOSITORY_ROOT)
    place = cwd.as_posix() if relative is None else relative.as_posix()
    check = "\0".join([place, *targets, "strict" if strict else "default"])
    return root / hashlib.sha256(check.encode("utf-8")).hexdigest()[:16]


def type_check_skill_roots(root: Path = REPOSITORY_ROOT) -> list[Path]:
    """The scripts directory of each shipped or repository skill that holds a Python module or regression suite."""
    return [scripts for scripts in skill_script_directories(root) if any(file_presence.matching(scripts, "*.py"))]


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


# Restrict indirection rather than model it. A policy that reads a call by its name holds only while the source spells
# the name, and Python reaches a name by a value in an open set of ways: a lookup by a computed name, a namespace, a
# rebinding, a parameter's default, a class attribute, or a `**` of a mapping. A policy widened to each spelling an
# issue named left the next one open, so the policies below refuse the ways at the source, in every module the
# claim policies read, and the policies that read a call read only its direct forms. Each names a module it cannot
# read, and refuses a listing that holds no module, rather than pass having checked nothing.
#
# 1. No dynamic name lookup. A NAME_LOOKUPS call is read only given a string literal for the name, and any other
#    reference to one is refused; so is a reference to any INDIRECTION_CALLABLES entry, an INDIRECTION_ATTRIBUTES
#    attribute on any receiver, a definition of a LOOKUP_HOOKS method, and a module an import statement binds held as a
#    value rather than as the receiver of an attribute or of a literal lookup. A class pattern's keyword is a binding
#    form (class_pattern_reads): one that binds such an attribute or callable from a match's subject is refused as
#    well. A site that must stay states why in a module-level INDIRECTION_ALLOWED = {scope: reason}.
# 2. Owner-only names. Each OWNER_ONLY_NAMES callable is referenced at all, called, bound, passed, defaulted, returned,
#    stored, bound by a class pattern's keyword, or reached by a literal lookup, only in the modules that own it, so a
#    rebinding, a default, a dictionary
#    value, or a class attribute holding one is refused without being followed. JSON is written by json_writer and
#    read by json_reader alone, so a direct call of json.dumps or json.loads elsewhere is refused too. An event loop's
#    process start is matched by its method's name on any receiver. A reference that must stay states why in a
#    module-level OWNER_ONLY_ALLOWED.
# 3. Policed calls spell their arguments. An add_argument, a json.dump or json.dumps, and a process start unpack
#    nothing into their arguments with `*` or `**`; an add_argument's option strings, `dest`, and `metavar` are string
#    literals; and its `type`, the `default` of one that names a time, and a JSON write's `cls` and `default` spell
#    their value where it is read (_direct): a literal, a builtin, a name the top level binds only by imports, only by
#    definitions, or once to a literal, an attribute of an imported module, or a call of such a callable given such
#    arguments. Nothing is followed: a parameter, a local, `self.x`, a class attribute, a module-level name bound any
#    other way, a lambda, and a lookup by name are refused. An add_argument is only called, never bound or passed. A
#    call that must stay states why in a module-level POLICED_CALL_ALLOWED.
INDIRECTION_ALLOWANCE = "INDIRECTION_ALLOWED"
OWNER_ONLY_ALLOWANCE = "OWNER_ONLY_ALLOWED"
POLICED_CALL_ALLOWANCE = "POLICED_CALL_ALLOWED"
# The modules that load a foreign library with ctypes, each declaring every function it calls.
CTYPES_OWNERS = frozenset(
    {
        "skills/ejryan89-skill-core/scripts/job_tree.py",
        "skills/ejryan89-skill-core/scripts/process_identity.py",
        "skills/ejryan89-code-review-core/scripts/review_io.py",
        "skills/ejryan89-code-review-core/scripts/review_runtime.py",
    }
)
# Each owner-only callable, by the name the module's imports resolve it to, with the modules that own it.
OWNER_ONLY_NAMES: dict[str, frozenset[str]] = {
    **{name: frozenset({JSON_WRITER}) for name in (JSON_DUMP, JSON_DUMPS, *JSON_ENCODERS)},
    **dict.fromkeys(JSON_PARSERS, frozenset({JSON_READER})),
    **dict.fromkeys(LIBRARY_LOADERS | LIBRARY_NAMESPACES | LIBRARY_OBJECTS, CTYPES_OWNERS),
    **{name: frozenset({"skills/ejryan89-skill-core/scripts/path_containment.py"}) for name in COMMON_PATH_FUNCTIONS},
    # bounded_process's own runners are the way every other module starts a process, so they are no owner's alone;
    # an event loop's starts are matched by their method's name, on any receiver (_event_loop_start).
    **{name: frozenset({PROCESS_RUNNER}) for name in PROCESS_STARTS if not name.startswith("bounded_process.")},
}
# The method each add_argument call is, on any receiver, and the functions whose calls rule 3 polices.
ADD_ARGUMENT = "add_argument"
POLICED_FUNCTIONS = frozenset({JSON_DUMP, JSON_DUMPS, *PROCESS_STARTS})
# The keywords of a policed call that take a string literal, and those that take a value spelled where it is read
# (_direct); an add_argument that names a time takes its default so too.
LITERAL_KEYWORDS = {ADD_ARGUMENT: frozenset({"dest", "metavar"})}
DIRECT_KEYWORDS = {
    ADD_ARGUMENT: frozenset({"type"}),
    JSON_DUMP: frozenset({"cls", "default"}),
    JSON_DUMPS: frozenset({"cls", "default"}),
}


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    """Each node's parent, by the child's id."""
    return {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _annotation_ids(tree: ast.AST) -> set[int]:
    """The id of every node inside an annotation: a parameter's, a return's, or an annotated assignment's."""
    roots: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            roots.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns is not None:
            roots.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
    return {id(part) for root in roots for part in ast.walk(root)}


def _called(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """Whether a node is the function of the call that holds it."""
    parent = parents.get(id(node))
    return isinstance(parent, ast.Call) and parent.func is node


def _literal_lookup(call: ast.Call) -> str | None:
    """The attribute a NAME_LOOKUPS call names by a string literal second argument, unpacking nothing, or None."""
    if len(call.args) < 2 or call.keywords or any(isinstance(argument, ast.Starred) for argument in call.args):
        return None
    name = call.args[1]
    return name.value if isinstance(name, ast.Constant) and isinstance(name.value, str) else None


def _reference(node: ast.AST, resolver: CallableResolver, aliases: dict[str, str]) -> str:
    """The dotted name a node refers to through the module's imports: a name or an attribute read, or a lookup of a
    literal name, as getattr(json, "dumps") refers to json.dumps; "" for any other node. A name the resolver cannot
    settle, as one two import statements bind, is read through the module's imports, and a name no scope binds is
    the builtin of that name."""
    if isinstance(node, ast.Name | ast.Attribute) and isinstance(node.ctx, ast.Load):
        if name := resolver.name(node):
            return name
        root: ast.expr = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if not isinstance(root, ast.Name):
            return ""
        if root.id in aliases:
            return qualified_name(node, aliases)
        return root.id if node is root and resolver.scopes[id(root)].owner(root.id) is None else ""
    if isinstance(node, ast.Call) and _reference(node.func, resolver, aliases) in NAME_LOOKUPS and node.args:
        attribute = _literal_lookup(node)
        owner = _reference(node.args[0], resolver, aliases)
        return f"{owner}.{attribute}" if owner and attribute else ""
    return ""


def _imported_modules(tree: ast.AST) -> set[str]:
    """The module each import statement binds a name to, as `import a.b` binds a and `import a.b as c` binds a.b."""
    return {
        target
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for found in module_imports(node)
        for _, target in found.bindings
    }


def _receives(node: ast.Name, parents: dict[int, ast.AST], resolver: CallableResolver, aliases: dict[str, str]) -> bool:
    """Whether a name is read as a receiver: of an attribute, or of a lookup of a literal name."""
    parent = parents.get(id(node))
    if isinstance(parent, ast.Attribute) and parent.value is node:
        return True
    return (
        isinstance(parent, ast.Call)
        and _reference(parent.func, resolver, aliases) in NAME_LOOKUPS
        and bool(parent.args)
        and parent.args[0] is node
        and _literal_lookup(parent) is not None
    )


def _indirection(
    node: ast.AST, parents: dict[int, ast.AST], resolver: CallableResolver, aliases: dict[str, str], modules: set[str]
) -> str:
    """Why a node looks a name up dynamically, or ""."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in LOOKUP_HOOKS:
        return f"defines {node.name}, which answers an attribute read by a name it is given"
    if isinstance(node, ast.Attribute) and node.attr in INDIRECTION_ATTRIBUTES:
        return f"reaches {node.attr}, which reads a namespace or a lookup by a name it is given"
    name = _reference(node, resolver, aliases) if isinstance(node, ast.Name | ast.Attribute) else ""
    if name in INDIRECTION_CALLABLES:
        return f"refers to {name}, which reaches a name, a namespace, or a module by a value"
    if name in NAME_LOOKUPS:
        call = parents.get(id(node))
        if not isinstance(call, ast.Call) or call.func is not node:
            return f"refers to {name} other than calling it"
        if _literal_lookup(call) is None:
            return f"calls {name} with a name that is not one string literal"
    if isinstance(node, ast.Name) and name in modules and not _receives(node, parents, resolver, aliases):
        return f"holds the module {name} as a value; name what it uses from it as an attribute instead"
    return ""


def _pattern_reads(pattern: ast.pattern, base: str) -> Iterator[tuple[ast.MatchClass, str, str]]:
    """Each keyword of a class pattern below `pattern`, with the dotted name it reads, the attribute of what `base`
    names, "" when the base names nothing, and the attribute's name. A keyword's own pattern reads that attribute's
    attributes in turn; an alternative or a capture keeps its subject, and a sequence's or a mapping's items do not."""
    if isinstance(pattern, ast.MatchClass):
        for attribute, inner in zip(pattern.kwd_attrs, pattern.kwd_patterns, strict=True):
            name = f"{base}.{attribute}" if base else ""
            yield pattern, name, attribute
            yield from _pattern_reads(inner, name)
        for inner in pattern.patterns:
            yield from _pattern_reads(inner, "")
    elif isinstance(pattern, ast.MatchAs) and pattern.pattern is not None:
        yield from _pattern_reads(pattern.pattern, base)
    elif isinstance(pattern, ast.MatchOr):
        for inner in pattern.patterns:
            yield from _pattern_reads(inner, base)
    elif isinstance(pattern, ast.MatchSequence | ast.MatchMapping):
        for inner in pattern.patterns:
            yield from _pattern_reads(inner, "")


def class_pattern_reads(
    tree: ast.Module, resolver: CallableResolver, aliases: dict[str, str]
) -> Iterator[tuple[ast.MatchClass, str, str]]:
    """Each attribute a class pattern's keyword reads from a match's subject and binds, as `case object(commonpath=c)`
    on `os.path` reads and binds os.path.commonpath, with the dotted name it reads through the module's imports ("" for
    a subject that names nothing) and the attribute's name: a binding form, as `c = os.path.commonpath` is."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Match):
            subject = _reference(node.subject, resolver, aliases)
            for case in node.cases:
                yield from _pattern_reads(case.pattern, subject)


def _reads_by_pattern(
    tree: ast.Module, resolver: CallableResolver, aliases: dict[str, str]
) -> dict[int, list[tuple[str, str]]]:
    """Each class pattern's keyword reads (class_pattern_reads), by the pattern's id."""
    reads: dict[int, list[tuple[str, str]]] = {}
    for pattern, read, attribute in class_pattern_reads(tree, resolver, aliases):
        reads.setdefault(id(pattern), []).append((read, attribute))
    return reads


def _policy_trees(root: Path, files: list[Path], problems: list[str]) -> Iterator[tuple[str, ast.Module]]:
    """Each module the claim policies read, as its repository path and its tree. A module that is not valid Python or
    not UTF-8 is reported by its name, and a listing that holds no module to read is reported too, since a policy
    that read nothing has checked nothing."""
    claimed = claim_files(root, files)
    if not claimed:
        problems.append(f"{root.as_posix()}: the listing holds no module the claim policies read, so none was checked")
    for _, name, tree in python_trees(claimed, root, problems):
        yield name, tree


def indirection_problems(root: Path, files: list[Path]) -> list[str]:
    """Each dynamic name lookup in a module the claim policies read that no INDIRECTION_ALLOWED entry sanctions."""
    problems: list[str] = []
    for name, tree in _policy_trees(root, files, problems):
        resolver = CallableResolver(tree)
        parents = _parents(tree)
        modules = _imported_modules(tree)
        aliases = import_aliases(tree)
        reads = _reads_by_pattern(tree, resolver, aliases)
        found: list[tuple[int, str, str]] = []
        for node, scope in scoped_nodes(tree.body):
            refusals = [_indirection(node, parents, resolver, aliases, modules)]
            refusals += [
                f"binds {read or attribute} by a class pattern's keyword, which reaches a namespace or a name by a "
                "value"
                for read, attribute in reads.get(id(node), [])
                if attribute in INDIRECTION_ATTRIBUTES or read in INDIRECTION_CALLABLES | NAME_LOOKUPS
            ]
            found += [
                (
                    getattr(node, "lineno", 0),
                    scope,
                    f"{refusal}, which no policy reading the code can follow; spell the name, or state why in "
                    f"{INDIRECTION_ALLOWANCE}",
                )
                for refusal in refusals
                if refusal
            ]
        problems += sanctioned_problems(name, tree, INDIRECTION_ALLOWANCE, found)
    return problems


def owner_only_problems(root: Path, files: list[Path]) -> list[str]:
    """Each reference to an OWNER_ONLY_NAMES callable outside its owners that no OWNER_ONLY_ALLOWED entry
    sanctions."""
    problems: list[str] = []
    for name, tree in _policy_trees(root, files, problems):
        resolver = CallableResolver(tree)
        annotations = _annotation_ids(tree)
        aliases = import_aliases(tree)
        reads = _reads_by_pattern(tree, resolver, aliases)
        found: list[tuple[int, str, str]] = []
        for node, scope in scoped_nodes(tree.body):
            reference = (
                "" if id(node) in annotations else _event_loop_start(node) or _reference(node, resolver, aliases)
            )
            # A class pattern's keyword binds the subject's attribute it names, as an assignment would.
            for candidate in [reference, *(read for read, _ in reads.get(id(node), []))]:
                owners = OWNER_ONLY_NAMES.get(candidate)
                if owners is None or name in owners:
                    continue
                refusal = f"refers to {candidate} outside {listed(sorted(owners), 'and')}, which own it"
                found.append(
                    (
                        getattr(node, "lineno", 0),
                        scope,
                        f"{refusal}; reach it through its owner, or state why in {OWNER_ONLY_ALLOWANCE}",
                    )
                )
        problems += sanctioned_problems(name, tree, OWNER_ONLY_ALLOWANCE, found)
    return problems


def _is_literal(node: ast.expr) -> bool:
    """Whether an expression is a literal: a constant, a sign or an operation on literals, or a tuple, list, set, or
    dict of them."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp):
        return _is_literal(node.operand)
    if isinstance(node, ast.BinOp):
        return _is_literal(node.left) and _is_literal(node.right)
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return all(not isinstance(item, ast.Starred) and _is_literal(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(key is not None and _is_literal(key) for key in node.keys) and all(map(_is_literal, node.values))
    return False


# How a module's top level binds a name a policed argument reads: by an import, by a def or a class, or once to a
# literal. A name bound any other way, or more than once to a literal, is neither.
IMPORTED, DEFINED, CONSTANT = "import", "definition", "constant"


def _stored_names(target: ast.expr | None) -> list[str]:
    """Each name an assignment target stores to, through a tuple, a list, or a starred item."""
    if target is None:
        return []
    return [node.id for node in ast.walk(target) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)]


def _top_level_bindings(node: ast.stmt) -> list[tuple[str, str]]:
    """Each name one top-level statement binds, with how: IMPORTED, DEFINED, CONSTANT, or "" for any other way."""
    if isinstance(node, ast.Import | ast.ImportFrom):
        return [(alias.asname or alias.name.split(".")[0], IMPORTED) for alias in node.names]
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return [(node.name, DEFINED)]
    if isinstance(node, ast.Assign | ast.AnnAssign):
        kind = CONSTANT if node.value is not None and _is_literal(node.value) else ""
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return [(name, kind) for target in targets for name in _stored_names(target)]
    if isinstance(node, ast.AugAssign | ast.For | ast.AsyncFor):
        return [(name, "") for name in _stored_names(node.target)]
    if isinstance(node, ast.With | ast.AsyncWith):
        return [(name, "") for item in node.items for name in _stored_names(item.optional_vars)]
    return []


def _spelled_names(tree: ast.Module) -> dict[str, str]:
    """Each name the module's top level binds only by imports, only by definitions, or once to a literal, with how;
    a name a function rebinds through `global`, or an assignment expression binds, is none of them."""
    kinds: dict[str, list[str]] = {}
    for node in _top_level(tree.body):
        for name, kind in _top_level_bindings(node):
            kinds.setdefault(name, []).append(kind)
    for walked in ast.walk(tree):
        names = list(walked.names) if isinstance(walked, ast.Global) else []
        names += _stored_names(walked.target) if isinstance(walked, ast.NamedExpr) else []
        for name in names:
            kinds.setdefault(name, []).append("")
    spelled: dict[str, str] = {}
    for name, found in kinds.items():
        if found == [CONSTANT]:
            spelled[name] = CONSTANT
        elif len(set(found)) == 1 and found[0] in (IMPORTED, DEFINED):
            spelled[name] = found[0]
    return spelled


@dataclass(frozen=True)
class _ModuleNames:
    """What rule 3 reads a module's names by: its resolver, its import aliases, which _reference falls back to,
    and the names its top level spells (_spelled_names)."""

    resolver: CallableResolver
    aliases: dict[str, str]
    spelled: dict[str, str]


def _direct(node: ast.expr, names: _ModuleNames) -> bool:
    """Whether an expression spells its value where it is read: a literal, a builtin, a name the module's top level
    binds by an import or a definition or once to a literal, an attribute of an imported module, an operation on such
    values, or a call of an imported, defined, or builtin callable given such arguments. A parameter, a local, a class
    attribute, a module-level name bound to anything else, a lambda, and a lookup by name are refused, never
    followed. A callee is named through _reference, the lookup rules 1 and 2 use, so a name two import statements
    bind is read through the module's imports here too."""
    resolver, spelled = names.resolver, names.spelled
    if _is_literal(node):
        return True
    if isinstance(node, ast.Name):
        owner = resolver.scopes[id(node)].owner(node.id)
        if owner is None:
            return node.id in BUILTIN_NAMES
        return owner.parent is None and node.id in spelled
    if isinstance(node, ast.Attribute):
        root: ast.expr = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if not isinstance(root, ast.Name):
            return False
        held = resolver.scopes[id(root)].owner(root.id)
        return held is not None and held.parent is None and spelled.get(root.id) == IMPORTED
    if isinstance(node, ast.UnaryOp):
        return _direct(node.operand, names)
    if isinstance(node, ast.BinOp):
        return _direct(node.left, names) and _direct(node.right, names)
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return all(not isinstance(item, ast.Starred) and _direct(item, names) for item in node.elts)
    if isinstance(node, ast.Call):
        callee = node.func
        constant = isinstance(callee, ast.Name) and spelled.get(callee.id) == CONSTANT
        return (
            _reference(callee, resolver, names.aliases) not in NAME_LOOKUPS | INDIRECTION_CALLABLES
            and not constant
            and _direct(callee, names)
            and all(not isinstance(item, ast.Starred) and _direct(item, names) for item in node.args)
            and all(keyword.arg is not None and _direct(keyword.value, names) for keyword in node.keywords)
        )
    return False


def _names_add_argument(node: ast.AST, resolver: CallableResolver, aliases: dict[str, str]) -> bool:
    """Whether a node reads an add_argument: as an attribute, or by a lookup of its literal name."""
    if isinstance(node, ast.Attribute):
        return node.attr == ADD_ARGUMENT and isinstance(node.ctx, ast.Load)
    return (
        isinstance(node, ast.Call)
        and _reference(node.func, resolver, aliases) in NAME_LOOKUPS
        and _literal_lookup(node) == ADD_ARGUMENT
    )


def _event_loop_start(node: ast.AST) -> str:
    """The event loop's process start a node reads as an attribute, on any receiver, as PROCESS_STARTS names it, or
    "": no receiver names the loop, so the method's name is what marks it."""
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr in EVENT_LOOP_STARTS:
        return f"{EVENT_LOOP}.{node.attr}"
    return ""


def _policed(call: ast.Call, resolver: CallableResolver, aliases: dict[str, str]) -> str:
    """The policed call a call is, ADD_ARGUMENT on any receiver, an event loop's process start on any receiver, or a
    POLICED_FUNCTIONS name, or ""."""
    if _names_add_argument(call.func, resolver, aliases):
        return ADD_ARGUMENT
    if started := _event_loop_start(call.func):
        return started
    name = _reference(call.func, resolver, aliases)
    return name if name in POLICED_FUNCTIONS else ""


def _names_a_time(call: ast.Call) -> bool:
    """Whether an add_argument names a time, by one of TIME_WORDS, in an option string, its dest, or its metavar."""
    names = [*call.args, *(keyword.value for keyword in call.keywords if keyword.arg in ("dest", "metavar"))]
    return any(
        isinstance(name, ast.Constant) and isinstance(name.value, str) and TIME_WORDS & _time_words(name.value)
        for name in names
    )


def _policed_refusals(call: ast.Call, policed: str, names: _ModuleNames) -> Iterator[str]:
    """Why a policed call does not spell its arguments as rule 3 asks."""
    if any(isinstance(item, ast.Starred) for item in call.args) or any(item.arg is None for item in call.keywords):
        yield f"unpacks arguments into {policed} with * or **"
    if policed == ADD_ARGUMENT and not all(
        isinstance(item, ast.Constant) and isinstance(item.value, str) for item in call.args
    ):
        yield f"gives {ADD_ARGUMENT} an option string that is not a string literal"
    direct = set(DIRECT_KEYWORDS.get(policed, ()))
    if policed == ADD_ARGUMENT and _names_a_time(call):
        direct.add("default")
    for keyword in call.keywords:
        value = keyword.value
        if keyword.arg in LITERAL_KEYWORDS.get(policed, ()):
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                yield f"gives {policed} a {keyword.arg}= that is not a string literal"
        elif keyword.arg in direct and not _direct(value, names):
            yield (
                f"gives {policed} a {keyword.arg}= that is neither a literal nor an imported, defined, builtin, or "
                "literal-bound name"
            )


def policed_call_problems(root: Path, files: list[Path]) -> list[str]:
    """Each policed call, an add_argument, a json.dump or json.dumps, or a process start, that unpacks its arguments or
    gives a policed argument anything but a literal or a module-bound name, and each add_argument read other than as
    the function of a call, that no POLICED_CALL_ALLOWED entry sanctions."""
    problems: list[str] = []
    for name, tree in _policy_trees(root, files, problems):
        resolver = CallableResolver(tree)
        parents = _parents(tree)
        aliases = import_aliases(tree)
        names = _ModuleNames(resolver, aliases, _spelled_names(tree))
        found: list[tuple[int, str, str]] = []
        for node, scope in scoped_nodes(tree.body):
            if _names_add_argument(node, resolver, aliases) and not _called(node, parents):
                found.append((getattr(node, "lineno", 0), scope, f"refers to {ADD_ARGUMENT} other than calling it"))
            elif isinstance(node, ast.Call) and (policed := _policed(node, resolver, aliases)):
                refusals = _policed_refusals(node, policed, names)
                found += [(node.lineno, scope, refusal) for refusal in refusals]
        found = [
            (
                line,
                scope,
                f"{refusal}, which the policies reading it cannot follow; spell it, or state why in "
                f"{POLICED_CALL_ALLOWANCE}",
            )
            for line, scope, refusal in found
        ]
        problems += sanctioned_problems(name, tree, POLICED_CALL_ALLOWANCE, found)
    return problems


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

    def test_file_presence_is_the_one_reader_of_absence(self) -> None:
        self.assertEqual([], presence_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

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

    def test_every_text_open_names_its_encoding(self) -> None:
        self.assertEqual([], encoding_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_every_ctypes_foreign_function_called_is_declared(self) -> None:
        self.assertEqual([], ctypes_declaration_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_no_git_exit_status_is_read_outside_the_client(self) -> None:
        self.assertEqual([], git_status_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_no_gh_listing_is_capped_outside_the_client(self) -> None:
        self.assertEqual([], gh_limit_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_no_process_runner_result_is_discarded_unchecked(self) -> None:
        self.assertEqual([], discarded_result_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_every_git_listing_and_format_separates_on_nul(self) -> None:
        self.assertEqual([], git_separator_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_whether_git_answered_is_read_through_git_error_answered(self) -> None:
        self.assertEqual([], git_answered_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_no_name_is_looked_up_dynamically(self) -> None:
        self.assertEqual([], indirection_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_owner_only_names_are_referenced_only_by_their_owners(self) -> None:
        self.assertEqual([], owner_only_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_policed_calls_take_literal_arguments(self) -> None:
        self.assertEqual([], policed_call_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_every_time_option_refuses_what_is_no_limit(self) -> None:
        self.assertEqual([], time_option_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

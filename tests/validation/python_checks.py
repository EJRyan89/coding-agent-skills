"""The Python static checks: ruff format and lint, mypy and its mypy_path, the noqa and type: ignore rules, and the
rule against issue numbers in comments and docstrings."""

from __future__ import annotations

import ast
import functools
import io
import os
import re
import tokenize
import tomllib
import unittest
from pathlib import Path
from typing import Any

from job_pool import UNSPLIT_SUITE_WEIGHT, Job, run_process
from toolchain import find_mypy, find_ruff
from validation_support import (
    REPOSITORY_ROOT,
    UnreadablePython,
    module_imports,
    parse_source,
    python_source,
    python_trees,
    relative,
    repository_files,
    scripts_put_on_path,
    skill_script_directories,
)

from deployer import platform_support

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
# The areas mypy checks in strict mode, which pyproject.toml's [tool.validation] mypy-strict lists. Areas join in stages
# and none leaves, so each one here must stay listed there; a pull request that makes an area strict adds it to both.
STRICT_TYPE_CHECK_AREAS = ("skills/ejryan89-skill-core/scripts",)
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
    """Each ceiling of the lint configuration missing or raised past its literal, and the blind excepts kept with a
    noqa when there are more of them than theirs."""
    found = [
        f"[tool.ruff.lint.{table}] {key} is {value!r}; the ratchet holds it at {ceiling} or lower, so split the "
        "function instead"
        for table, ceilings in CEILINGS.items()
        for key, ceiling in ceilings.items()
        if not (
            isinstance(value := lint.get(table, {}).get(key), int) and not isinstance(value, bool) and value <= ceiling
        )
    ]
    if len(blind_excepts) > BLIND_EXCEPT_CEILING:
        found.append(
            f"{len(blind_excepts)} noqa comments keep a blind except, past the ratchet's {BLIND_EXCEPT_CEILING}; "
            f"catch the exceptions the code expects instead: {', '.join(blind_excepts)}"
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
            or not any(name == prefix or name.startswith(f"{prefix}/") for prefix in FORMAT_ROOTS)
            or any(name.startswith(f"{prefix}/") for prefix in ISSUE_REFERENCE_EXCLUDED)
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
        if path.is_relative_to(scripts):
            return scripts
    for top in TYPE_CHECK_ROOTS:
        if path == root / top or path.is_relative_to(root / top):
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
    """The strict list must be a list of directories validation type-checks in a run of their own, and hold every
    pinned area, since an area joins the list and never leaves it."""
    areas = strict_type_check_areas(root)
    if areas is None:
        return ["pyproject.toml: [tool.validation] mypy-strict must be a list of paths from the repository root"]
    own_runs = {scripts.relative_to(root).as_posix() for scripts in type_check_skill_roots(root)}
    problems = [
        f"pyproject.toml: [tool.validation] mypy-strict lacks {area}, which is checked in strict mode; an area "
        "joins the list and never leaves it, so fix the errors strict mode reports instead"
        for area in pinned
        if area not in areas
    ]
    problems += [
        f"pyproject.toml: [tool.validation] mypy-strict names {area}, which validation does not type-check in a run "
        "of its own"
        for area in areas
        if area not in own_runs
    ]
    problems += [
        f"pyproject.toml: [tool.validation] mypy-strict names {area} more than once"
        for area in sorted({area for area in areas if areas.count(area) > 1})
    ]
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
    # The null device as the cache directory keeps mypy from writing a cache into the tree.
    command = [mypy, "--config-file", str(configuration), "--cache-dir", os.devnull, *(["--strict"] if strict else [])]
    try:
        run_process([*command, *targets], cwd=cwd)
    except AssertionError as exc:
        raise AssertionError(
            f"{exc}\nFix each error named above; a `# type: ignore[<code>]` that must stay states its reason in a "
            "comment after it."
        ) from exc


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
    # A job keeps its name in strict mode, so the seconds recorded for it still weigh it.
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
        # Default strictness, and no module, suite, or import is exempt; the areas [tool.validation] lists are checked
        # with --strict besides. No platform: each host checks the code as it runs there, so every platform's branches
        # are checked on the platform that runs them.
        self.assertEqual(["explicit_package_bases", "mypy_path", "python_version"], sorted(configuration))

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

"""Fixture tests for tests/validation/python_checks.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import importlib
import json
import os
import re
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, get_args
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))

import folder_walk
import git_client
import github_client
import pdf_tool
import python_checks
import review_vocabularies
import skill_scripts
import toolchain
import validation_support
from call_tables import TEXT_OPENERS, TextOpener
from job_selection import all_jobs
from python_checks import (
    BLIND_EXCEPT_CEILING,
    CEILINGS,
    FORMAT_ROOTS,
    blind_except_noqa,
    ceiling_noqa,
    comments,
    ctypes_declaration_problems,
    discarded_result_problems,
    gh_limit_problems,
    git_answered_problems,
    git_separator_problems,
    int_parse_problems,
    issue_numbers_in_comments,
    link_test_problems,
    mypy_path_problems,
    mypy_type_check,
    noqa_without_reason,
    python_suppression_problems,
    ratchet_problems,
    ruff_format_check,
    ruff_lint_check,
    strict_area_problems,
    time_option_problems,
    type_check_skill_roots,
    type_check_skipped_problems,
    type_ignore_without_reason,
)
from toolchain import PREREQUISITES, find_mypy, missing_prerequisites
from validation_support import REPOSITORY_ROOT, SKILLS_ROOT, relative, scope_problems, write_fixture_tree

from deployer import journal, platform_support, report

# Each closed vocabulary, a member of it and a misspelling of that member, and a site that takes one, as a program the
# type check reads writes it.
VOCABULARIES = (
    (git_client.GitErrorKind, "not_repository", "not_a_repository", 'GitError("failed", kind={})'),
    (github_client.GitHubErrorKind, "rate_limit", "rate_limited", 'GitHubError("failed", kind={})'),
    (folder_walk.NotEnteredKind, "nested-checkout", "nested_checkout", 'NotEntered(Path("folder"), {}, "why")'),
    (pdf_tool.PdfProvider, "Poppler", "poppler", "PdfIdentity({}, (26, 10, 0))"),
    (report.Action, "INSTALL", "INSTAL", 'PlanEntry("alpha", SKILL, "absent", {})'),
    (journal.Op, "preserve", "restore", "op: journal.Op = {}"),
    (review_vocabularies.Severity, "SHOULD_FIX", "SHOULD FIX", "SEVERITY_RANK[{}]"),
    (review_vocabularies.Verdict, "CHANGES_REQUESTED", "CHANGES REQUESTED", "verdict: Verdict = {}"),
    (review_vocabularies.Disposition, "unable_to_verify", "unverified", "LEDGER_STATES[{}]"),
    (review_vocabularies.LedgerState, "unverified", "unable_to_verify", "state: LedgerState = {}"),
    (
        review_vocabularies.DispatchMode,
        "copilot-host",
        "copilot",
        'negotiate_capabilities("claude-code", [], dispatch={})',
    ),
)
VOCABULARY_IMPORTS = (
    "from pathlib import Path",
    "from deployer import journal",
    "from deployer.kinds import SKILL",
    "from deployer.plan import PlanEntry",
    "from folder_walk import NotEntered",
    "from git_client import GitError",
    "from github_client import GitHubError",
    "from pdf_tool import PdfIdentity",
    "from review_runtime import negotiate_capabilities",
    "from review_vocabularies import LEDGER_STATES, SEVERITY_RANK, LedgerState, Verdict",
)


class PythonChecksFixtures(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        # The fixtures name Windows' install commands on every platform; test_platform_support pins each platform's.
        patcher = mock.patch.object(platform_support, "INSTALL_HINTS", platform_support.WINDOWS_INSTALL_HINTS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_format_check_names_an_unformatted_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text("[tool.ruff]\nline-length = 120\n", encoding="utf-8")
            (root / "clean.py").write_text('x = {"a": 1}\n', encoding="utf-8")
            ruff_format_check(root, ["clean.py"])
            (root / "module.py").write_text("x = {  'a':1 }\n", encoding="utf-8")
            with self.assertRaises(AssertionError) as raised:
                ruff_format_check(root, ["clean.py", "module.py"])
            message = str(raised.exception)
            self.assertRegex(message, r"(?m)^module\.py:\d+:\d+: unformatted: File would be reformatted$")
            self.assertNotRegex(message, r"(?m)^clean\.py:")
            self.assertEqual([], sorted(path.name for path in root.iterdir() if path.name.startswith(".")))
            self.assertIn("python -m ruff format", message)

    def test_format_check_covers_every_python_root(self) -> None:
        self.assertEqual(
            ("deployer", "tools", "tests", "skills", ".claude/skills", "deploy.py", "conftest.py"), FORMAT_ROOTS
        )
        self.assertIn("static format check (ruff format --check)", [job.name for job in all_jobs()])

    def test_lint_check_names_an_unused_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["F"]\n', encoding="utf-8")
            (root / "clean.py").write_text("import os\n\nprint(os.sep)\n", encoding="utf-8")
            ruff_lint_check(root, ["clean.py"])
            (root / "module.py").write_text("import os\n", encoding="utf-8")
            with self.assertRaises(AssertionError) as raised:
                ruff_lint_check(root, ["clean.py", "module.py"])
            message = str(raised.exception)
            self.assertRegex(message, r"(?m)^module\.py:1:8: F401 ")
            self.assertNotRegex(message, r"(?m)^clean\.py:")
            self.assertEqual([], sorted(path.name for path in root.iterdir() if path.name.startswith(".")))
            self.assertIn("python -m ruff check --fix", message)

    def test_ruff_checks_a_file_an_ignore_file_outside_the_repository_names(self) -> None:
        # Given a folder, ruff passes over a file an ignore file names: a .ignore above the repository, or the global
        # gitignore in the user's configuration folder, which ruff reads inside a git repository.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repository"
            write_fixture_tree(
                root, {"pyproject.toml": '[tool.ruff.lint]\nselect = ["F"]\n', "pkg/module.py": "x = {  'a':1 }\n"}
            )
            (root / ".git").mkdir()
            (root / "pkg" / "lint.py").write_text("import os\n", encoding="utf-8")
            configuration = Path(directory) / "configuration"
            write_fixture_tree(configuration, {"git/ignore": "module.py\nlint.py\n"})
            cases = {
                ".ignore above": (Path(directory) / ".ignore", {}),
                "global gitignore": (None, {"XDG_CONFIG_HOME": str(configuration)}),
            }
            for case, (ignore, environment) in cases.items():
                if ignore is not None:
                    ignore.write_text("module.py\nlint.py\n", encoding="utf-8")
                for check, named in ((ruff_format_check, "module.py"), (ruff_lint_check, "lint.py:1:8: F401")):
                    with self.subTest(case=case, check=check.__name__):
                        with mock.patch.dict(os.environ, environment), self.assertRaises(AssertionError) as raised:
                            check(root, ["pkg"])
                        self.assertIn(named, str(raised.exception).replace("\\", "/"))
                if ignore is not None:
                    ignore.unlink()

    def test_type_check_resolves_no_import_through_mypypath(self) -> None:
        # mypy resolves an import through MYPYPATH ahead of mypy_path, so a stub there would stand in for the module.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repository"
            write_fixture_tree(
                root,
                {
                    "pyproject.toml": '[tool.mypy]\nmypy_path = ["$MYPY_CONFIG_FILE_DIR/lib"]\n',
                    "lib/helper.py": "def answer() -> int:\n    return 42\n",
                    "pkg/module.py": "import helper\n\nx: str = helper.answer()\n",
                },
            )
            stubs = Path(directory) / "stubs"
            write_fixture_tree(stubs, {"helper.pyi": "from typing import Any\n\ndef answer() -> Any: ...\n"})
            with mock.patch.dict(os.environ, {"MYPYPATH": str(stubs)}), self.assertRaises(AssertionError) as raised:
                mypy_type_check(root / "pkg", ["module.py"], root / "pyproject.toml")
            self.assertIn("module.py:3: error: Incompatible types in assignment", str(raised.exception))

    def test_lint_lets_the_regression_suites_assert_and_no_shipped_module(self) -> None:
        refused = [
            "deploy.py",
            "conftest.py",
            "deployer/module.py",
            "tools/module.py",
            "skills/alpha/scripts/helper.py",
            "skills/alpha/test_beside_scripts.py",
            ".claude/skills/beta/scripts/helper.py",
        ]
        allowed = [
            "tests/test_top.py",
            "tests/validation/policy.py",
            "tests/deployer/test_deep.py",
            "skills/alpha/scripts/test_alpha.py",
            ".claude/skills/beta/scripts/test_beta.py",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # The repository's own configuration, so the fixture holds the globs ruff reads, not a copy of them.
            (root / "pyproject.toml").write_bytes((REPOSITORY_ROOT / "pyproject.toml").read_bytes())
            for name in refused + allowed:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("def check(value: int) -> None:\n    assert value\n", encoding="utf-8")
            ruff_lint_check(root, allowed)
            with self.assertRaises(AssertionError) as raised:
                ruff_lint_check(root, refused + allowed)
            named = re.findall(r"(?m)^(\S+?):2:5: S101 ", str(raised.exception).replace("\\", "/"))
            self.assertEqual(sorted(refused), sorted(named))

    def test_lint_check_covers_every_python_root(self) -> None:
        jobs = {job.name: job for job in all_jobs()}
        self.assertIn("static lint check (ruff check)", jobs)
        with mock.patch.object(python_checks, "ruff_lint_check") as lint:
            jobs["static lint check (ruff check)"].run()
        lint.assert_called_once_with(
            REPOSITORY_ROOT, ["deployer", "tools", "tests", "skills", ".claude/skills", "deploy.py", "conftest.py"]
        )

    def test_missing_mypy_is_reported_with_the_install_command(self) -> None:
        self.assertIn(("mypy", "mypy", find_mypy), PREREQUISITES)
        with (
            mock.patch.object(platform_support, "find_on_path", return_value=None),
            mock.patch.object(platform_support, "find_executable", return_value=None),
        ):
            self.assertIsNone(find_mypy())
        self.assertEqual(
            ["  - mypy: python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt"],
            missing_prerequisites((("mypy", "mypy", lambda: None),)),
        )
        with (
            mock.patch.object(python_checks, "find_mypy", return_value=None),
            self.assertRaises(AssertionError) as raised,
        ):
            mypy_type_check(REPOSITORY_ROOT, ["deploy.py"], REPOSITORY_ROOT / "pyproject.toml")
        self.assertIn(
            "mypy was not found: python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt",
            str(raised.exception),
        )

    def test_type_check_names_a_wrong_return_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = root / "pyproject.toml"
            configuration.write_text('[tool.mypy]\npython_version = "3.11"\n', encoding="utf-8")
            (root / "clean.py").write_text("def answer() -> int:\n    return 42\n", encoding="utf-8")
            mypy_type_check(root, ["clean.py"], configuration)
            (root / "module.py").write_text('def answer() -> int:\n    return "42"\n', encoding="utf-8")
            with self.assertRaises(AssertionError) as raised:
                mypy_type_check(root, ["clean.py", "module.py"], configuration)
            message = str(raised.exception)
            # mypy ends its lines with CRLF on Windows, and `$` does not match before a carriage return.
            named = r"(?m)^module\.py:2: error: Incompatible return value type .*\[return-value\]\r?$"
            self.assertRegex(message, named)
            self.assertNotRegex(message, r"(?m)^clean\.py:")
            self.assertIn("type: ignore[<code>]", message)
            # No cache or other state is written into the checked tree.
            self.assertEqual([], sorted(path.name for path in root.iterdir() if path.name.startswith(".")))

    def test_type_check_caches_in_a_folder_of_its_own_that_ends_with_it(self) -> None:
        seen: list[tuple[Path, bool]] = []

        def record(arguments: list[str], environment: dict[str, str], cwd: Path) -> None:
            cache = Path(arguments[arguments.index("--cache-dir") + 1])
            seen.append((cache, cache.is_dir()))

        with (
            mock.patch.dict("os.environ", {"VALIDATION_MYPY_CACHE": ""}),
            mock.patch.object(python_checks, "find_mypy", return_value="mypy"),
            mock.patch.object(toolchain, "run_process", side_effect=record),
        ):
            for _ in range(2):
                mypy_type_check(REPOSITORY_ROOT, ["deploy.py"], REPOSITORY_ROOT / "pyproject.toml")
        # A real folder, never the null device, outside the tree, made for the check and removed after it.
        self.assertEqual([True, True], [existed for _, existed in seen])
        self.assertNotEqual(seen[0][0], seen[1][0])
        for cache, _ in seen:
            self.assertFalse(cache.is_relative_to(REPOSITORY_ROOT))
            self.assertFalse(cache.exists())

    def test_type_checks_cache_under_the_named_folder_one_folder_each(self) -> None:
        commands: list[list[str]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "mypy cache"
            with (
                mock.patch.dict("os.environ", {"VALIDATION_MYPY_CACHE": str(root)}),
                mock.patch.object(python_checks, "find_mypy", return_value="mypy"),
                mock.patch.object(
                    toolchain,
                    "run_process",
                    side_effect=lambda arguments, environment, cwd: commands.append(arguments),
                ),
            ):
                checks = [job for job in all_jobs() if job.name.startswith("static type check (mypy")]
                for _ in range(2):
                    for job in checks:
                        job.run()
            caches = [Path(command[command.index("--cache-dir") + 1]) for command in commands]
            first, second = caches[: len(checks)], caches[len(checks) :]
            # Each check, the mypy processes the pool runs at once among them, has its own folder under the root,
            # and finds the same one at the next run, which is how a restored cache is read again.
            self.assertEqual(len(checks), len(set(first)))
            self.assertEqual({root}, {cache.parent for cache in first})
            self.assertEqual(first, second)
            with (
                mock.patch.dict("os.environ", {"VALIDATION_MYPY_CACHE": "relative/cache"}),
                mock.patch.object(python_checks, "find_mypy", return_value="mypy"),
                self.assertRaises(AssertionError) as raised,
            ):
                mypy_type_check(REPOSITORY_ROOT, ["deploy.py"], REPOSITORY_ROOT / "pyproject.toml")
            self.assertIn("VALIDATION_MYPY_CACHE must be an absolute path, not 'relative/cache'", str(raised.exception))

    def test_a_warm_cache_names_the_same_errors_as_a_cold_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"
            root.mkdir()
            configuration = root / "pyproject.toml"
            configuration.write_text('[tool.mypy]\npython_version = "3.11"\n', encoding="utf-8")
            (root / "module.py").write_text('def answer() -> int:\n    return "42"\n', encoding="utf-8")
            cache = Path(directory) / "cache"
            messages = []
            with mock.patch.dict("os.environ", {"VALIDATION_MYPY_CACHE": str(cache)}):
                for _ in range(2):
                    with self.assertRaises(AssertionError) as raised:
                        mypy_type_check(root, ["module.py"], configuration)
                    messages.append(re.findall(r"(?m)^module\.py:.*$", str(raised.exception)))
                    self.assertTrue(any(cache.iterdir()), "mypy wrote its cache under the named folder")
            self.assertEqual(1, len(messages[0]))
            self.assertEqual(messages[0], messages[1])
            self.assertEqual(["module.py", "pyproject.toml"], sorted(path.name for path in root.iterdir()))

    def test_type_check_in_strict_mode_names_an_error_only_strict_mode_reports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = root / "pyproject.toml"
            configuration.write_text('[tool.mypy]\npython_version = "3.11"\n', encoding="utf-8")
            (root / "module.py").write_text("def answer(value):\n    return value\n", encoding="utf-8")
            mypy_type_check(root, ["module.py"], configuration)
            with self.assertRaises(AssertionError) as raised:
                mypy_type_check(root, ["module.py"], configuration, strict=True)
            named = r"(?m)^module\.py:1: error: Function is missing a type annotation .*\[no-untyped-def\]\r?$"
            self.assertRegex(str(raised.exception), named)

    def test_type_check_runs_once_per_root(self) -> None:
        configuration = REPOSITORY_ROOT / "pyproject.toml"
        with mock.patch.object(python_checks, "mypy_type_check") as check:
            jobs = {job.name: job for job in all_jobs()}
            core = "static type check (mypy deployer, tools, deploy.py, tests, conftest.py)"
            self.assertIn(core, jobs)
            jobs[core].run()
            check.assert_called_once_with(
                REPOSITORY_ROOT, ["deployer", "tools", "deploy.py", "tests", "conftest.py"], configuration, strict=False
            )
            # A strict root of those also runs alone in strict mode, from the repository root, and the run above is
            # kept as it was. The strict roots are pinned as literals.
            strict_roots = ("deployer", "tools", "deploy.py", "tests", "conftest.py")
            self.assertEqual(
                {f"static type check (mypy --strict {top})" for top in strict_roots},
                {name for name in jobs if name.startswith("static type check (mypy --strict ")},
            )
            for top in strict_roots:
                with self.subTest(root=top):
                    check.reset_mock()
                    jobs[f"static type check (mypy --strict {top})"].run()
                    check.assert_called_once_with(REPOSITORY_ROOT, [top], configuration, strict=True)
            # Each skill whose scripts/ holds a module is its own root, checked from inside it as the skill imports.
            roots = type_check_skill_roots()
            self.assertIn(SKILLS_ROOT / "ejryan89-code-review-core" / "scripts", roots)
            self.assertNotIn(SKILLS_ROOT / "update-coding-agent-skills" / "scripts", roots)
            for root in roots:
                name = f"static type check (mypy {relative(root)})"
                with self.subTest(root=name):
                    check.reset_mock()
                    jobs[name].run()
                    # The strict skills, pinned as literals, run in strict mode under the names they always had.
                    strict = relative(root) in {
                        "skills/ejryan89-skill-core/scripts",
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
                    }
                    check.assert_called_once_with(root, ["."], configuration, strict=strict)

    def test_the_strict_list_holds_every_root_and_never_loses_one(self) -> None:
        pinned = ("skills/alpha/scripts",)
        files = {
            "skills/alpha/SKILL.md": "",
            "skills/alpha/scripts/run.py": "",
            "skills/beta/SKILL.md": "",
            "skills/beta/scripts/run.py": "",
            "deployer/__init__.py": "",
        }
        complete = '"skills/alpha/scripts", "skills/beta/scripts", "deployer"'

        def problems(table: str) -> list[str]:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_fixture_tree(root, {**files, "pyproject.toml": table})
                return strict_area_problems(root, pinned)

        # Every root the tree has, the skills' scripts and the roots checked together, is listed.
        self.assertEqual([], problems(f"[tool.validation]\nmypy-strict = [{complete}]\n"))
        # A root the others are checked with may be listed before the tree has it.
        self.assertEqual([], problems(f'[tool.validation]\nmypy-strict = [{complete}, "deploy.py"]\n'))
        # A pinned area removed fails, however the rest of the list is left.
        removed = (
            "pyproject.toml: [tool.validation] mypy-strict lacks skills/alpha/scripts, which is checked in strict "
            "mode; an area joins the list and never leaves it, so fix the errors strict mode reports instead"
        )
        self.assertEqual([removed], problems('[tool.validation]\nmypy-strict = ["skills/beta/scripts", "deployer"]\n'))

        # A root the list does not name, such as a new skill's scripts, fails until it is listed.
        def unlisted(area: str) -> str:
            return (
                f"pyproject.toml: [tool.validation] mypy-strict lacks {area}, a type-check root; every root is "
                "checked in strict mode, so list it and fix the errors strict mode reports there"
            )

        self.assertEqual(
            [unlisted("skills/beta/scripts")],
            problems('[tool.validation]\nmypy-strict = ["skills/alpha/scripts", "deployer"]\n'),
        )
        self.assertEqual(
            [removed, unlisted("deployer"), unlisted("skills/beta/scripts")],
            problems("[tool.validation]\nmypy-strict = []\n"),
        )
        # A list that is missing or holds anything but paths is refused whole.
        malformed = ["pyproject.toml: [tool.validation] mypy-strict must be a list of paths from the repository root"]
        for table in (
            "",
            "[tool.validation]\n",
            '[tool.validation]\nmypy-strict = "skills/alpha/scripts"\n',
            '[tool.validation]\nmypy-strict = ["skills/alpha/scripts", 1]\n',
        ):
            with self.subTest(table=table):
                self.assertEqual(malformed, problems(table))
        # An area that is no type-check root could not be checked in strict mode, such as a folder below a root or a
        # skill with no scripts, and an area is named once.
        self.assertEqual(
            [
                "pyproject.toml: [tool.validation] mypy-strict names deployer/sub, which is no root validation "
                "type-checks",
                "pyproject.toml: [tool.validation] mypy-strict names skills/gamma/scripts, which is no root "
                "validation type-checks",
                "pyproject.toml: [tool.validation] mypy-strict names skills/alpha/scripts more than once",
            ],
            problems(
                f'[tool.validation]\nmypy-strict = [{complete}, "deployer/sub", "skills/gamma/scripts", '
                '"skills/alpha/scripts"]\n'
            ),
        )

    def test_type_check_roots_are_the_scripts_of_every_shipped_and_repository_skill(self) -> None:
        files = {
            "skills/module/SKILL.md": "",
            "skills/module/scripts/run.py": "",
            "skills/suites/SKILL.md": "",
            "skills/suites/scripts/test_run.py": "",
            "skills/shell/SKILL.md": "",
            "skills/shell/scripts/test_run.sh": "",
            # A repository skill's scripts are checked like a shipped skill's.
            ".claude/skills/local/SKILL.md": "",
            ".claude/skills/local/scripts/run.py": "",
            # A folder without SKILL.md is no skill, and the skills directory is flat, so neither is one under it.
            "skills/loose/scripts/run.py": "",
            "skills/group/inner/SKILL.md": "",
            "skills/group/inner/scripts/run.py": "",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, files)
            self.assertEqual(
                [
                    root / "skills/module/scripts",
                    root / "skills/suites/scripts",
                    root / ".claude/skills/local/scripts",
                ],
                type_check_skill_roots(root),
            )

    def test_type_ignore_scan_requires_codes_and_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # The comments are assembled so this file does not carry the comments it tests.
            marker = "# type" + ": ignore"
            (root / "module.py").write_text(
                "\n".join(
                    [
                        f"a = 1  {marker}",
                        f"b = 1  {marker}[misc]",
                        f"c = 1  {marker}  # no codes",
                        f"d = 1  {marker}[misc]  # the stub omits it",
                        f"e = 1  {marker}[misc, arg-type]  # the stub omits both",
                        f'TEXT = "{marker}"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (root / "notes.md").write_text(f"{marker}\n", encoding="utf-8")
            self.assertEqual(
                ["module.py:1", "module.py:2", "module.py:3"],
                type_ignore_without_reason(root, [root / "module.py", root / "notes.md"]),
            )

    def test_the_comment_scanner_reads_comments_and_never_text_in_a_string(self) -> None:
        source = 'x = "# not a comment"  # first\n"""\n# inside a docstring\n"""\n# second\n'
        found = [(token.start[0], token.string) for token in comments(source)]
        self.assertEqual([(1, "# first"), (5, "# second")], found)

    def test_noqa_scan_requires_codes_and_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # The comments are assembled so this file does not carry the comments it tests.
            marker = "# no" + "qa"
            (root / "module.py").write_text(
                "\n".join(
                    [
                        f"import os  {marker}",
                        f"import re  {marker}: F401",
                        f"import io  {marker}: F401 -",
                        f"import sys  {marker}: F401 - imported for its effect",
                        f"import json  {marker}: F401, E402 - kept for the plugin loader",
                        f'TEXT = "{marker}: F401"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (root / "notes.md").write_text(f"{marker}\n", encoding="utf-8")
            self.assertEqual(
                ["module.py:1", "module.py:2", "module.py:3"],
                noqa_without_reason(root, [root / "module.py", root / "notes.md"]),
            )

    def test_ceiling_scan_refuses_a_noqa_for_complexity_or_length(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = "# no" + "qa"
            (root / "module.py").write_text(
                "\n".join(
                    [
                        f"def tangled():  {marker}: C901 - too many branches to split today",
                        f"def long():  {marker}: E501, PLR0915 - generated",
                        f"def both():  {marker}: c901 plr0915 - lower case still counts",
                        f"import sys  {marker}: F401 - imported for its effect",
                        f'TEXT = "{marker}: C901 - in a string"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                [
                    "module.py:1 suppresses C901; split the function instead",
                    "module.py:2 suppresses PLR0915; split the function instead",
                    "module.py:3 suppresses C901 and PLR0915; split the function instead",
                ],
                ceiling_noqa(root, [root / "module.py"]),
            )

    def test_blind_except_scan_names_each_noqa_that_keeps_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = "# no" + "qa"
            (root / "module.py").write_text(
                "\n".join(
                    [
                        f"KEPT = 1  {marker}: BLE001 - the last handler",
                        f"BOTH = 2  {marker}: S110, ble001 - lower case beside another code still counts",
                        f"import sys  {marker}: F401 - imported for its effect",
                        f'TEXT = "{marker}: BLE001 - in a string"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (root / "notes.md").write_text(f"{marker}: BLE001 - not Python\n", encoding="utf-8")
            self.assertEqual(
                ["module.py:1", "module.py:2"], blind_except_noqa(root, [root / "module.py", root / "notes.md"])
            )

    def test_ratchets_hold_each_ceiling_and_the_blind_excepts_at_their_literals_or_lower(self) -> None:
        held = [f"module.py:{line}" for line in range(1, BLIND_EXCEPT_CEILING + 1)]
        self.assertEqual([], ratchet_problems(copy.deepcopy(CEILINGS), held))
        # A value below its literal could rise back to it unrefused, so the literal is lowered with it.
        lowered = {table: {key: ceiling - 1 for key, ceiling in keys.items()} for table, keys in CEILINGS.items()}
        self.assertEqual(
            [
                f"[tool.ruff.lint.{table}] {key} is {ceiling - 1}, below the ratchet's {ceiling}; lower its literal "
                f"in CEILINGS to {ceiling - 1} in the same change"
                for table, keys in CEILINGS.items()
                for key, ceiling in keys.items()
            ]
            + [
                f"{BLIND_EXCEPT_CEILING - 1} noqa comments keep a blind except, below the ratchet's "
                f"{BLIND_EXCEPT_CEILING}; lower BLIND_EXCEPT_CEILING to {BLIND_EXCEPT_CEILING - 1} in the same change"
            ],
            ratchet_problems(lowered, held[1:]),
        )
        raised = copy.deepcopy(CEILINGS)
        raised["mccabe"]["max-complexity"] += 1
        raised["pylint"]["max-args"] = True
        del raised["pylint"]["max-statements"]
        self.assertEqual(
            [
                f"[tool.ruff.lint.mccabe] max-complexity is {CEILINGS['mccabe']['max-complexity'] + 1}; the ratchet "
                f"holds it at {CEILINGS['mccabe']['max-complexity']} or lower, so split the function instead",
                f"[tool.ruff.lint.pylint] max-statements is None; the ratchet holds it at "
                f"{CEILINGS['pylint']['max-statements']} or lower, so split the function instead",
                f"[tool.ruff.lint.pylint] max-args is True; the ratchet holds it at {CEILINGS['pylint']['max-args']} "
                "or lower, so split the function instead",
            ],
            ratchet_problems(raised, held),
        )
        another = [*held, "other.py:9"]
        self.assertEqual(
            [
                f"{BLIND_EXCEPT_CEILING + 1} noqa comments keep a blind except, past the ratchet's "
                f"{BLIND_EXCEPT_CEILING}; catch the exceptions the code expects instead: {', '.join(another)}"
            ],
            ratchet_problems(copy.deepcopy(CEILINGS), another),
        )

    def test_a_python_file_in_a_folder_mypy_passes_over_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            names = [
                "tests/site-packages/a.py",
                "tests/ok/node_modules/b.py",
                "tools/.hidden/c.py",
                "skills/alpha/scripts/__pycache__/d.py",
                # Read by mypy: an ordinary folder, a root's own name, a file named with a dot, and a folder outside
                # every root it checks.
                "tests/ok/e.py",
                "deployer/f.py",
                "skills/alpha/scripts/.g.py",
                "docs/.examples/h.py",
            ]
            write_fixture_tree(root, {"skills/alpha/SKILL.md": "", **dict.fromkeys(names, "")})
            moved = "which mypy passes over, so no type check reads it; move it"
            self.assertEqual(
                [
                    f"skills/alpha/scripts/__pycache__/d.py is in a folder named __pycache__, {moved}",
                    f"tests/ok/node_modules/b.py is in a folder named node_modules, {moved}",
                    f"tests/site-packages/a.py is in a folder named site-packages, {moved}",
                    f"tools/.hidden/c.py is in a folder named .hidden, {moved}",
                ],
                type_check_skipped_problems(root, [root / name for name in names]),
            )

    def test_suppression_scan_refuses_file_and_region_exemptions_and_extra_ruff_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Each comment is assembled so this file does not carry the comments it tests.
            files = {
                "pyproject.toml": "[tool.ruff]\n",
                "skills/alpha/scripts/pyproject.toml": "[tool.ruff]\n",
                "tools/ruff.toml": "line-length = 200\n",
                "tests/.ruff.toml": "line-length = 200\n",
                "skills/alpha/scripts/module.py": "\n".join(
                    [
                        "# my" + "py: ignore-errors",
                        "# ru" + "ff: noqa",
                        "# fla" + "ke8: noqa",
                        "# is" + "ort: skip_file",
                        "TABLE = [1,2]  # f" + "mt: skip",
                        "# f" + "mt: off",
                        "# ya" + "pf: disable",
                        "import sys  # no" + "qa: F401 - imported for its effect",
                        "# ru" + "ff: disable[C901, PLR0915]",
                        "# ru" + "ff: enable[C901, PLR0915]",
                        "# the mypy run and a fmt call are described here, not configured",
                        'TEXT = "# my' + 'py: ignore-errors"',
                        "",
                    ]
                ),
            }
            write_fixture_tree(root, files)
            module = "skills/alpha/scripts/module.py"
            fix = "; fix each finding at its cause"
            self.assertEqual(
                [
                    f"{module}:1 turns a check off with '# my" + f"py:'{fix}",
                    f"{module}:2 turns a check off with '# ru" + f"ff: noqa'{fix}",
                    f"{module}:3 turns a check off with '# fla" + f"ke8: noqa'{fix}",
                    f"{module}:4 turns a check off with '# is" + f"ort: skip_file'{fix}",
                    f"{module}:5 turns a check off with '# f" + f"mt: skip'{fix}",
                    f"{module}:6 turns a check off with '# f" + f"mt: off'{fix}",
                    f"{module}:7 turns a check off with '# ya" + f"pf: disable'{fix}",
                    f"{module}:9 turns a check off with '# ru" + f"ff: disable'{fix}",
                    "skills/alpha/scripts/pyproject.toml configures ruff; pyproject.toml at the repository root is its "
                    "one configuration",
                    "tests/.ruff.toml configures ruff; pyproject.toml at the repository root is its one configuration",
                    "tools/ruff.toml configures ruff; pyproject.toml at the repository root is its one configuration",
                ],
                python_suppression_problems(root, [root / name for name in files]),
            )

    def test_issue_number_scan_reads_comments_and_docstrings_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Each fixture line is a string literal here, so this file does not carry the citations it tests.
            violating = [
                '"""Pins the lock fix (#12)."""',
                "a = 1  # see #12",
                "# #12's reproduction left the lock behind.",
                "# Issue 12 says why.",
                "def f() -> None:",
                '    """Rejects a quoted path.',
                "",
                '    The reproduction came from #142."""',
                "class C:",
                '    """Kept for issue #7."""',
                "",
            ]
            conforming = [
                '"""Pins the lock fix: a dead holder blocked every review."""',
                "# example/one#12 is reviewed; approved#1 has no comparison.",
                'BODY = "Closes #12"',
                "def f() -> None:",
                '    """Returns the issue numbers in a body."""',
                '    print("#12", "issue 12")',
                "",
            ]
            write_fixture_tree(
                root,
                {
                    "tools/violating.py": "\n".join(violating),
                    "deploy.py": "# Fixed in #3.\n",
                    "skills/one/scripts/conforming.py": "\n".join(conforming),
                    "tests/fixtures/review/base/app.py": "# Fixes #12.\n",
                    "docs/notes.py": "# Fixes #12.\n",
                    "skills/one/SKILL.md": "Fixed in #12.\n",
                },
            )
            files = [path for path in root.rglob("*") if path.is_file()]
            self.assertEqual(
                [
                    "deploy.py:1",
                    "tools/violating.py:1",
                    "tools/violating.py:2",
                    "tools/violating.py:3",
                    "tools/violating.py:4",
                    "tools/violating.py:8",
                    "tools/violating.py:10",
                ],
                issue_numbers_in_comments(root, files),
            )

    def test_missing_ruff_fails_the_lint_check_with_the_install_command(self) -> None:
        with (
            mock.patch.object(python_checks, "find_ruff", return_value=None),
            self.assertRaises(AssertionError) as raised,
        ):
            ruff_lint_check(REPOSITORY_ROOT, ["deploy.py"])
        self.assertIn(
            "ruff was not found: python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt",
            str(raised.exception),
        )

    def test_mypy_path_policy_names_each_missing_and_stale_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "skills" / "user" / "scripts").mkdir(parents=True)
            (root / "skills" / "user" / "SKILL.md").write_text("# user\n", encoding="utf-8")
            sibling = 'sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core" / "scripts"))\n'
            looked_up = 'sys.path.insert(0, str(sibling_scripts("looked-up")))\n'
            (root / "skills" / "user" / "scripts" / "user.py").write_text(sibling + looked_up, encoding="utf-8")
            # A regression suite is type-checked too, so the directories it puts on sys.path count.
            suite = 'sys.path.insert(0, str(ROOT / "skills" / "suite-only" / "scripts"))\nimport user\n'
            (root / "skills" / "user" / "scripts" / "test_user.py").write_text(suite, encoding="utf-8")
            # Text that only names the call is not one.
            (root / "skills" / "user" / "scripts" / "notes.py").write_text(f"TEXT = {sibling!r}\n", encoding="utf-8")
            # A suite outside a skill's scripts/ that imports a module beside it by its bare name needs its directory.
            (root / "tests" / "suites").mkdir(parents=True)
            (root / "tests" / "suites" / "helper.py").write_text("import os\n", encoding="utf-8")
            (root / "tests" / "suites" / "test_a.py").write_text("import helper\nimport json\n", encoding="utf-8")
            (root / "tests" / "suites" / "test_b.py").write_text("from helper import x\n", encoding="utf-8")
            # A package import from the repository root is not a sibling import.
            (root / "tests" / "other").mkdir()
            (root / "tests" / "other" / "test_c.py").write_text("from tools import x\nimport os\n", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                '[tool.mypy]\nmypy_path = ["$MYPY_CONFIG_FILE_DIR/skills/stale/scripts"]\n', encoding="utf-8"
            )
            self.assertEqual(
                [
                    "pyproject.toml: [tool.mypy] mypy_path lacks $MYPY_CONFIG_FILE_DIR/skills/core/scripts, which "
                    "skills/user/scripts/user.py puts on sys.path",
                    "pyproject.toml: [tool.mypy] mypy_path lacks $MYPY_CONFIG_FILE_DIR/skills/looked-up/scripts, "
                    "which skills/user/scripts/user.py puts on sys.path",
                    "pyproject.toml: [tool.mypy] mypy_path lacks $MYPY_CONFIG_FILE_DIR/skills/suite-only/scripts, "
                    "which skills/user/scripts/test_user.py puts on sys.path",
                    "pyproject.toml: [tool.mypy] mypy_path lacks $MYPY_CONFIG_FILE_DIR/tests/suites, which "
                    "tests/suites/test_a.py imports a module from by its bare name",
                    "pyproject.toml: [tool.mypy] mypy_path names $MYPY_CONFIG_FILE_DIR/skills/stale/scripts, which "
                    "no module puts on sys.path",
                ],
                mypy_path_problems(root, sorted(root.rglob("*"))),
            )


def test_the_type_check_refuses_a_misspelled_member_of_each_closed_vocabulary() -> None:
    lines = list(VOCABULARY_IMPORTS)
    misspelled = []
    for vocabulary, member, misspelling, site in VOCABULARIES:
        assert member in get_args(vocabulary)
        assert misspelling not in get_args(vocabulary)
        lines.append(site.format(repr(member)))
        lines.append(site.format(repr(misspelling)))
        misspelled.append(len(lines))
    with pytest.raises(AssertionError) as raised:
        mypy_type_check(REPOSITORY_ROOT, ["-c", "\n".join(lines)], REPOSITORY_ROOT / "pyproject.toml")
    # mypy names a program given with -c as <string>, and ends its lines with CRLF on Windows.
    errors = re.findall(r"(?m)^(.*?):(\d+): error: .*?\r?$", str(raised.value))
    assert [("<string>", line) for line in misspelled] == [(path, int(line)) for path, line in errors]


# Each form a policy's documentation names, as a minimal file that must be refused, and each form it deliberately
# leaves out, as one that must pass, with the reason beside it. The documentation is the policy's paragraph in
# "Validation" in docs/adding-a-skill.md or "Python checks" in CONTRIBUTING.md; a new policy, or a change to what a
# paragraph names, adds or changes its rows in the same pull request.
FormsPolicy = Callable[[Path, list[Path]], list[str]]
SCRIPT = "skills/alpha/scripts/alpha.py"


def _form(policy: FormsPolicy, text: str, path: str = SCRIPT, *, refused: bool = True) -> tuple[Any, ...]:
    return (policy, path, text, refused)


# Where a form's statements sit: in a function given `t`, or at the module's top level, each statement its own, so a
# name one statement binds is read by a later one.
PLACES = ("in a function", "at module level")


def _placed(imports: str, body: str, place: str) -> str:
    """A module of `imports` and then `body`, its lines in a function given `t` or at the top level."""
    if place == PLACES[0]:
        body = "def f(t):\n" + "".join(f"    {line}\n" for line in body.splitlines())
    return f"{imports}\n\n\n{body}\n"


# Each expression whose text holds `X`'s, and each place git's output is read from, as the binding that reads it and
# the expression that holds it.
TEXT_WRAPPERS = (
    ("str() given an encoding", "str(X, 'utf-8')"),
    ("str() given an encoding by keyword", "str(X, encoding='utf-8', errors='strict')"),
    ("bytes() given an encoding", "bytes(X, 'utf-8')"),
    ("a formatted string", "f'{X}'"),
    ("format()", "'{}'.format(X)"),
    ("format() by keyword", "'{x}'.format(x=X)"),
    ("str.format() unbound", "str.format('{}', X)"),
    ("format_map()", "'{x}'.format_map({'x': X})"),
    ("%", "'%s' % X"),
    ("% of a tuple", "'%s %s' % (1, X)"),
    ("concatenation", "X + ''"),
    ("concatenation after", "'' + X"),
    ("repetition", "X * 1"),
    ("a join", "''.join([X])"),
    ("or", "X or ''"),
    ("a conditional expression", "X if X else ''"),
)
GIT_OUTPUT_SOURCES = (
    ("a name bound to client.output", "out = client.output(['describe', 'HEAD'])", "out"),
    ("a run result's stdout", "result = client.run(['describe', 'HEAD'])", "result.stdout"),
    ("the call", "", "client.output(['describe', 'HEAD'])"),
)


# Every spelling of a call of re's function `{name}`, as the module's imports and the callee: by the module, an alias
# of it, a bare import, an alias of that, a name bound to the function, and a getattr of it.
REGEX_SPELLINGS = (
    ("re.{name}", "import re", "re.{name}"),
    ("an alias of re", "import re as r", "r.{name}"),
    ("a bare import", "from re import {name}", "{name}"),
    ("an aliased bare import", "from re import {name} as found", "found"),
    ("a bound name", "import re\n\nfound = re.{name}", "found"),
    ("getattr", "import re", "getattr(re, '{name}')"),
)


def _regex_forms(name: str, bodies: dict[str, str]) -> dict[str, tuple[Any, ...]]:
    """A refused int() form for each body, spelling of re's `name`, and place: the body calls it as `F`."""
    return {
        f"int() of {form} through {spelling.format(name=name)} {place}": _form(
            int_parse_problems,
            _placed(imports.format(name=name), body.replace("F", callee.format(name=name)), place),
        )
        for form, body in bodies.items()
        for spelling, imports, callee in REGEX_SPELLINGS
        for place in PLACES
    }


DOCUMENTED_FORMS: dict[str, tuple[Any, ...]] = {
    # CONTRIBUTING.md: a finding that must stay is suppressed on its line by a noqa naming its codes and its reason.
    "noqa alone": _form(noqa_without_reason, "x = 1  # noqa\n"),
    "noqa with codes and no reason": _form(noqa_without_reason, "x = 1  # noqa: E501\n"),
    "noqa with codes and a reason": _form(noqa_without_reason, "x = 1  # noqa: E501 - a reason\n", refused=False),
    # "Function complexity (C901) and length in statements (PLR0915) ... take no `noqa`".
    "noqa of complexity": _form(ceiling_noqa, "def f():  # noqa: C901 - a reason\n    pass\n"),
    "noqa of length": _form(ceiling_noqa, "def f():  # noqa: PLR0915 - a reason\n    pass\n"),
    "noqa of another code": _form(ceiling_noqa, "def f():  # noqa: E501 - a reason\n    pass\n", refused=False),
    # "a comment that turns a check off beyond one finding on its line", each spelling the paragraph names.
    **{
        f"turned off by {comment}": _form(python_suppression_problems, f"{comment}\nx = 1\n")
        for comment in (
            "# ruff: noqa",
            "# flake8: noqa",
            "# ruff: disable[E501]",
            "# isort: skip_file",
            "# isort: off",
            "# fmt: off",
            "# fmt: skip",
            "# yapf: disable",
            "# mypy: ignore-errors",
        )
    },
    # "a `ruff.toml`, `.ruff.toml`, or `pyproject.toml` anywhere but the repository root".
    **{
        f"ruff configured by skills/alpha/{name}": _form(python_suppression_problems, "", f"skills/alpha/{name}")
        for name in ("ruff.toml", ".ruff.toml", "pyproject.toml")
    },
    "ruff configured by the root's pyproject.toml": _form(
        python_suppression_problems, "[tool.ruff]\n", "pyproject.toml", refused=False
    ),
    # "a `type: ignore` without its codes and reason".
    "type ignore alone": _form(type_ignore_without_reason, "x: int = ''  # type: ignore\n"),
    "type ignore with codes and no reason": _form(
        type_ignore_without_reason, "x: int = ''  # type: ignore[assignment]\n"
    ),
    "type ignore with codes and a reason": _form(
        type_ignore_without_reason, "x: int = ''  # type: ignore[assignment]  # a reason\n", refused=False
    ),
    # A reason is never an issue number, in each form "Python checks" names: alone, in parentheses, possessive, or after
    # the word issue, in a comment or a docstring.
    **{
        f"an issue number as {form}": _form(issue_numbers_in_comments, f"x = 1  # see {form}\n")
        for form in ("#12", "(#12)", "#12's", "issue 12")
    },
    "an issue number in a docstring": _form(issue_numbers_in_comments, '"""Fixes #12."""\n'),
    # "string literals ... are outside the rule".
    "an issue number in a string literal": _form(issue_numbers_in_comments, 'x = "Closes #12"\n', refused=False),
    # A hash sign after a slash is a fixture's owner/repo selector, not a citation.
    "an owner/repo selector": _form(issue_numbers_in_comments, "x = 1  # owner/repo#12\n", refused=False),
    # "an `is_symlink`, `islink`, `is_junction`, or `isjunction`, or a read of `st_file_attributes`".
    **{
        f"a link test by {test}": _form(link_test_problems, f"import os\n\n\ndef f(p):\n    return {call}\n")
        for test, call in (
            ("is_symlink", "p.is_symlink()"),
            ("islink", "os.path.islink(p)"),
            ("is_junction", "p.is_junction()"),
            ("isjunction", "os.path.isjunction(p)"),
            ("st_file_attributes", "os.stat(p).st_file_attributes"),
            ("getattr", "getattr(p, 'is_symlink')()"),
        )
    },
    # "A call of `deployer/platform_support.py`'s `is_junction`, which delegates to it, is none."
    "platform_support.is_junction": _form(
        link_test_problems,
        "from deployer import platform_support\n\n\ndef f(p):\n    return platform_support.is_junction(p)\n",
        refused=False,
    ),
    # "a foreign function, through a library the module loads with `ctypes.WinDLL`, `ctypes.CDLL`, `ctypes.windll`, or
    # `ctypes.cdll`, whose `argtypes` and `restype` the module does not both assign".
    **{
        f"an undeclared function of {library}": _form(
            ctypes_declaration_problems, f"import ctypes\n\n\ndef f():\n    return {library}.Probe(1)\n"
        )
        for library in ("ctypes.WinDLL('k')", "ctypes.CDLL('k')", "ctypes.windll.kernel32", "ctypes.cdll.k")
    },
    "a function declared both ways": _form(
        ctypes_declaration_problems,
        "import ctypes\n\nLIB = ctypes.CDLL('k')\nLIB.Probe.argtypes = [ctypes.c_int]\n"
        "LIB.Probe.restype = ctypes.c_int\n"
        "\n\ndef f():\n    return LIB.Probe(1)\n",
        refused=False,
    ),
    # "a function every call gives no arguments needs no `argtypes`".
    "a function given no arguments with its restype": _form(
        ctypes_declaration_problems,
        "import ctypes\n\nLIB = ctypes.CDLL('k')\nLIB.Probe.restype = ctypes.c_int\n\n\n"
        "def f():\n    return LIB.Probe()\n",
        refused=False,
    ),
    # "an expression statement that discards the result of ... `run_bounded`, ... `run_tool`, or `GitClient.run`, on a
    # `GitClient` the module constructs or binds by assignment or annotation".
    **{
        f"a discarded {runner}": _form(discarded_result_problems, f"{imports}\n\n\ndef f(client):\n    {call}\n")
        for runner, imports, call in (
            ("run_bounded", "from bounded_process import run_bounded", "run_bounded(['x'], 60)"),
            ("run_tool", "from deployer import platform_support", "platform_support.run_tool(['x'])"),
            (
                "GitClient.run on a constructed client",
                "from git_client import GitClient",
                "GitClient().run(['status'])",
            ),
        )
    },
    "a discarded run on a GitClient subclass": _form(
        discarded_result_problems,
        "from git_client import GitClient\n\n\nclass Mine(GitClient):\n    def f(self):\n"
        "        self.run(['status'])\n",
    ),
    "a discarded GitClient.run on an annotated client": _form(
        discarded_result_problems,
        "from git_client import GitClient\n\n\ndef f(client: GitClient):\n    client.run(['status'])\n",
    ),
    # "`GitClient.output` and the `GitHubClient` methods raise on a failure, so they are not runners here".
    "a discarded GitClient.output": _form(
        discarded_result_problems,
        "from git_client import GitClient\n\n\ndef f(client: GitClient):\n    client.output(['status'])\n",
        refused=False,
    ),
    "a discarded GitHubClient.run": _form(
        discarded_result_problems,
        "from github_client import GitHubClient\n\n\ndef f():\n    GitHubClient().run(['pr', 'list'])\n",
        refused=False,
    ),
    # "gh's limit option, `--limit` or `-L`", alone, after =, or with digits attached.
    **{
        f"gh given {option}": _form(
            gh_limit_problems, f"def f(client, n):\n    return client.json(['pr', 'list', {option}])\n"
        )
        for option in ("'--limit', '5'", "'--limit=5'", "'-L', '5'", "'-L5'", "f'-L{n}'")
    },
    # "the arguments of a git name listing ... that hold no `-z`".
    **{
        f"a git listing by {' '.join(listing)}": _form(
            git_separator_problems, f"def f(run):\n    return run({['git', *listing]!r})\n"
        )
        for listing in (
            ("ls-files",),
            ("ls-tree", "HEAD"),
            ("diff", "--name-only"),
            ("diff", "--name-status"),
            ("status", "--porcelain"),
            ("status", "--short"),
        )
    },
    "a git listing given -z": _form(
        git_separator_problems, "def f(run):\n    return run(['git', 'ls-files', '-z'])\n", refused=False
    ),
    # "`GitClient.names` adds `-z`, and a call of it is not read."
    "a listing through GitClient.names": _form(
        git_separator_problems,
        "from git_client import GitClient\n\n\n"
        "def f(client: GitClient):\n    return client.names(['ls-files'], directory='.')\n",
        refused=False,
    ),
    # "a `for-each-ref` or `log` format, after `--format` or `--pretty`, whose fields anything but NUL joins".
    **{
        f"a {command} format joined by a space": _form(
            git_separator_problems, f"def f(run):\n    return run(['git', '{command}', '{option}'])\n"
        )
        for command, option in (
            ("for-each-ref", "--format=%(refname) %(objectname)"),
            ("log", "--pretty=format:%H %s"),
        )
    },
    "a for-each-ref format joined by NUL": _form(
        git_separator_problems,
        "def f(run):\n    return run(['git', 'for-each-ref', '--format=%(refname)%00%(objectname)'])\n",
        refused=False,
    ),
    # "no split of git's output with `splitlines()`, bound or unbound as `str.splitlines(out)`, and after a string
    # method, a `str()`, or a slice that keeps the text, as `out.strip().splitlines()`".
    **{
        f"a split of git's output by {form}": _form(
            git_separator_problems, f"def f(client):\n    out = client.output(['describe', 'HEAD'])\n    {split}\n"
        )
        for form, split in (
            ("splitlines()", "return out.splitlines()"),
            ("splitlines() of the call", "return client.output(['describe', 'HEAD']).splitlines()"),
            ("str.splitlines(out)", "return str.splitlines(out)"),
            ("strip().splitlines()", "return out.strip().splitlines()"),
            ("rstrip().splitlines()", "return out.rstrip().splitlines()"),
            ("str.strip(out).splitlines()", "return str.strip(out).splitlines()"),
            ("str.splitlines(out.strip())", "return str.splitlines(out.strip())"),
            ("a name bound to strip()", "text = out.strip()\n    return text.splitlines()"),
            ("str(out).splitlines()", "return str(out).splitlines()"),
            ("a slice's splitlines()", "return out[1:].splitlines()"),
            ("decode().splitlines()", "return out.decode().splitlines()"),
        )
    },
    # "no `strip()`, `lstrip()`, `rstrip()`, or `split()` of git's output given no argument", bound or unbound.
    **{
        f"a trim of git's output by {form}": _form(
            git_separator_problems,
            f"def f(client):\n    out = client.output(['symbolic-ref', '--short', 'HEAD'])\n    return {trim}\n",
        )
        for form, trim in (
            ("strip()", "out.strip()"),
            ("lstrip()", "out.lstrip()"),
            ("rstrip()", "out.rstrip()"),
            ("split()", "out.split()"),
            ("str.strip(out)", "str.strip(out)"),
            ("str.split(out)", "str.split(out)"),
            ("strip() of the call", "client.output(['rev-parse', 'HEAD']).strip()"),
            ("strip() of a stdout", "client.run(['config', '--get', 'x']).stdout.strip()"),
            ("strip() of an answer or nothing", "(client.quiet_output(['symbolic-ref', 'HEAD']) or '').strip()"),
            ("rsplit()", "out.rsplit()"),
            ("strip(None)", "out.strip(None)"),
            ("split(None)", "out.split(None)"),
            ("split(maxsplit=1)", "out.split(maxsplit=1)"),
            ("split(None, 1)", "out.split(None, 1)"),
            ("split(sep=None)", "out.split(sep=None)"),
            ("strip(chars=None)", "out.strip(chars=None)"),
            ("str.split(out, None)", "str.split(out, None)"),
            ("str.rsplit(out)", "str.rsplit(out)"),
        )
    },
    # "a call whose first argument is a literal `git`, or whose function's name has the word git in it, counts for
    # any subcommand", one its arguments name only at run time included.
    "a trim of a git-named call's output whose subcommand its arguments name at run time": _form(
        git_separator_problems, "def f(git_in, arguments):\n    return git_in(*arguments).stdout.strip()\n"
    ),
    "a trim of a call starting with a literal git": _form(
        git_separator_problems, "def f(run):\n    return run(['git', 'update-ref', 'x']).strip()\n"
    ),
    "a trim of a git-named call whose subcommand is a name its caller gives": _form(
        git_separator_problems,
        "def f(git_in, checkout, runner, command):\n    return git_in(checkout, runner, command).stdout.strip()\n",
    ),
    "a trim of a literal git call whose subcommand is a name": _form(
        git_separator_problems, "def f(run, name):\n    return run(['git', name]).stdout.strip()\n"
    ),
    "a trim of a call whose arguments run no git at all": _form(
        git_separator_problems, "def f(run, arguments):\n    return run(*arguments).stdout.strip()\n", refused=False
    ),
    **{
        f"a {form} of git's output that keeps what a name holds": _form(
            git_separator_problems,
            f"def f(client):\n    out = client.output(['symbolic-ref', '--short', 'HEAD'])\n    return {trim}\n",
            refused=False,
        )
        for form, trim in (
            ("strip('\\n')", "out.strip('\\n')"),
            ("split on a tab", "out.split('\\t')"),
            ("split on a tab once", "out.split('\\t', maxsplit=1)"),
            ("rsplit on a tab", "out.rsplit(sep='\\t')"),
            ("split on NUL", "str.split(out, '\\0')"),
            ("removesuffix", "out.removesuffix('\\n')"),
            ("strip() of a literal", "' x '.strip()"),
        )
    },
    # "or any subcommand of a call whose function's dotted name has the word git in it", GitHub not counting.
    "a trim of a git-named call's update-index": _form(
        git_separator_problems, "def f(git_output):\n    return git_output('update-index', '--verbose').split()\n"
    ),
    "a splitlines() of a git-named call's update-index": _form(
        git_separator_problems,
        "def f(self):\n    return self.git('update-index', '--verbose', '-z', '--add').splitlines()\n",
    ),
    "a trim of a subcommand not catalogued, through a wrapper named otherwise": _form(
        git_separator_problems, "def f(quiet, root):\n    return quiet(root, 'fetch').strip()\n", refused=False
    ),
    "a trim of a catalogued subcommand through a wrapper named otherwise": _form(
        git_separator_problems, "def f(quiet, root):\n    return quiet(root, 'write-tree').strip()\n"
    ),
    "a trim of a GitHub call's output": _form(
        git_separator_problems,
        "from github_client import GitHubClient\n\n\n"
        "def f():\n    return GitHubClient().output(['pr', 'view']).strip()\n",
        refused=False,
    ),
    # "a comparison of a caught `GitError`'s `returncode` with `None` inside the `except` clause that catches it".
    **{
        f"a GitError's returncode {form}": _form(
            git_answered_problems,
            f"{imports}\n\n\ndef f(client):\n    try:\n        client.output(['status'])\n"
            f"    except {caught} as exc:\n        if {test}:\n            raise\n",
        )
        for form, imports, caught, test in (
            ("is None", "from git_client import GitError", "GitError", "exc.returncode is None"),
            ("is not None", "from git_client import GitError", "GitError", "exc.returncode is not None"),
            ("== None", "from git_client import GitError", "GitError", "exc.returncode == None"),
            ("None first", "from git_client import GitError", "GitError", "None is exc.returncode"),
            ("by the module", "import git_client", "git_client.GitError", "exc.returncode is None"),
            ("by an alias", "from git_client import GitError as Failed", "Failed", "exc.returncode is None"),
            ("among others", "from git_client import GitError", "(OSError, GitError)", "exc.returncode is None"),
            ("in a condition", "from git_client import GitError", "GitError", "ok and exc.returncode is None"),
            ("in a tuple", "from git_client import GitError", "GitError", "exc.returncode in (None, 128)"),
            ("in a list", "from git_client import GitError", "GitError", "exc.returncode not in [None, 128]"),
            ("in a set", "from git_client import GitError", "GitError", "exc.returncode in {128, None}"),
            ("read as false", "from git_client import GitError", "GitError", "not exc.returncode"),
        )
    },
    **{
        f"a {form}": _form(
            git_answered_problems,
            f"{imports}\n\n\ndef f(client, other):\n    try:\n        client.output(['status'])\n"
            f"    except {caught} as exc:\n        if {test}:\n            raise\n",
            refused=False,
        )
        for form, imports, caught, test in (
            ("GitError's answered", "from git_client import GitError", "GitError", "not exc.answered"),
            (
                "GitError's returncode set beside 0",
                "from git_client import GitError",
                "GitError",
                "exc.returncode == 0",
            ),
            (
                "GitHubError's returncode is None",
                "from github_client import GitHubError",
                "GitHubError",
                "exc.returncode is None",
            ),
            ("returncode of another value", "from git_client import GitError", "GitError", "other.returncode is None"),
        )
    },
    # "or a function given it as a parameter annotated `GitError`".
    **{
        f"a GitError parameter's returncode {form}": _form(
            git_answered_problems,
            f"{imports}\n\n\ndef could_not_run(error: {annotation}) -> bool:\n    return {test}\n",
        )
        for form, imports, annotation, test in (
            ("is None", "from git_client import GitError", "GitError", "error.returncode is None"),
            ("by the module", "import git_client", "git_client.GitError", "error.returncode is None"),
            ("in a tuple", "from git_client import GitError", "GitError", "error.returncode in (None, 1)"),
            ("read as false", "from git_client import GitError", "GitError", "not error.returncode"),
        )
    },
    "a parameter annotated otherwise": _form(
        git_answered_problems,
        "from github_client import GitHubError\n\n\n"
        "def f(error: GitHubError) -> bool:\n    return error.returncode is None\n",
        refused=False,
    ),
    "a trim of a git program's output passed by name": _form(
        git_separator_problems,
        "def f(run_tool, git):\n    return run_tool([git, '--exec-path']).output.strip()\n",
    ),
    "a GitError's name reused by a GitHubError clause": _form(
        git_answered_problems,
        "from git_client import GitError\nfrom github_client import GitHubError\n\n\n"
        "def f(client):\n    try:\n        client.output(['status'])\n    except GitError as exc:\n        raise\n"
        "    try:\n        client.json(['pr'])\n    except GitHubError as exc:\n"
        "        if exc.returncode is None:\n            raise\n",
        refused=False,
    ),
    # A str() given an encoding, a formatted string, format(), %, concatenation, and a join keep the text as a str()
    # does, through each place git's output is read: a name bound to client.output, a run result's stdout, and the call.
    **{
        f"a split of {source} through {form} {place}": _form(
            git_separator_problems, _placed("", f"{binding}\n({wrapper.replace('X', held)}).splitlines()", place)
        )
        for form, wrapper in TEXT_WRAPPERS
        for source, binding, held in GIT_OUTPUT_SOURCES
        for place in PLACES
    },
    # "an assignment expression" binds as an assignment does, and so do the other ways a name is bound.
    **{
        f"a split of git's output through {form} {place}": _form(
            git_separator_problems, _placed("", f"out = client.output(['describe', 'HEAD'])\n{body}", place)
        )
        for form, body in (
            ("a walrus", "if (text := str(out, 'utf-8')):\n    text.splitlines()"),
            ("a walrus of the call", "if (text := client.output(['tag'])):\n    text.splitlines()"),
            ("an annotated assignment", "text: str = str(out, 'utf-8')\ntext.splitlines()"),
            ("an augmented assignment", "text = ''\ntext += out\ntext.splitlines()"),
            ("a chained assignment", "text = other = str(out, 'utf-8')\nother.splitlines()"),
            ("a tuple assignment", "text, n = out.strip(), 1\ntext.splitlines()"),
            ("str.splitlines of a formatted string", "str.splitlines(f'{out}')"),
        )
        for place in PLACES
    },
    # A module-level binding is read by a function that reads the module's name.
    "a split in a function of git's output a module-level name holds": _form(
        git_separator_problems,
        "OUT = client.output(['tag'])\n\n\ndef f():\n    return OUT.splitlines()\n",
    ),
    # A count of git's output holds none of its text.
    "a split of a count of git's output": _form(
        git_separator_problems,
        "def f(client):\n    out = client.output(['tag'])\n    return str(len(out)).splitlines()\n",
        refused=False,
    ),
    # A name is git's output only in the scope that binds it: g's parameter is not f's output.
    "a split of a parameter another function's output shares the name of": _form(
        git_separator_problems,
        "def f(client):\n    out = client.output(['tag'])\n    return out\n\n\n"
        "def g(out):\n    return out.splitlines()\n",
        refused=False,
    ),
    "a split of git's output on a newline alone": _form(
        git_separator_problems,
        "from git_client import output_lines\n\n\n"
        "def f(client):\n    return output_lines(client.output(['describe', 'HEAD']))\n",
        refused=False,
    ),
    # "a one-argument `int()` ... that converts a regular-expression group, a stripped one and a `findall` result's
    # included": an item of the list findall returns, reached by an index, through a name bound to the list, by a loop
    # or a comprehension over it, or by an index into one of its tuples.
    **{
        f"int() of findall's {form}": _form(int_parse_problems, f"import re\n\n\ndef f(t):\n    {body}\n")
        for form, body in (
            ("first item", "return int(re.findall('[0-9]+', t)[0])"),
            ("last item, stripped", "return int(re.findall('[0-9 ]+', t)[-1].strip())"),
            ("item through a bound name", "values = re.findall('[0-9]+', t)\n    return int(values[0])"),
            (
                "item through an annotated name",
                "values: list[str] = re.findall('[0-9]+', t)\n    return int(values[0])",
            ),
            (
                "item through a compiled pattern",
                "pattern = re.compile('[0-9]+')\n    values = pattern.findall(t)\n    return int(values[0])",
            ),
            ("tuple element in a comprehension", "return [int(p[0]) for p in re.findall('([0-9]+)-([0-9]+)', t)]"),
            ("tuple element by index", "return int(re.findall('([0-9]+)-([0-9]+)', t)[0][1])"),
            ("tuple unpacked", "a, b = re.findall('([0-9]+)-([0-9]+)', t)[0]\n    return int(b)"),
            ("item in a loop", "for v in re.findall('[0-9]+', t):\n        return int(v)"),
            ("item in a comprehension", "return [int(v) for v in re.findall('[0-9]+', t)]"),
            ("items mapped through int", "return list(map(int, re.findall('[0-9]+', t)))"),
        )
    },
    # "resolving names through its imports": findall's result and a value fullmatch, match, or search admitted, by
    # every spelling of the call, read in a function and across module-level statements alike.
    **_regex_forms(
        "findall",
        {
            "findall's first item": "n = int(F('[0-9]+', t)[0])",
            "a list findall's result is bound to": "items = F('[0-9]+', t)\nn = int(items[0])",
            "an item of a loop over findall": "for v in F('[0-9]+', t):\n    n = int(v)",
            "items mapped over findall": "n = list(map(int, F('[0-9]+', t)))",
        },
    ),
    **{
        key: row
        for method in ("fullmatch", "match", "search")
        for key, row in _regex_forms(
            method, {f"a value {method} admitted": "if F('[0-9]+', t):\n    n = int(t)"}
        ).items()
    },
    **{
        f"int() of a value {form} admitted {place}": _form(int_parse_problems, _placed(imports, body, place))
        for form, imports, body in (
            (
                "a compiled pattern's fullmatch",
                "import re",
                "P = re.compile('[0-9]+')\nif P.fullmatch(t):\n    n = int(t)",
            ),
            ("re.Pattern.fullmatch", "import re", "if re.Pattern.fullmatch(P, t):\n    n = int(t)"),
            ("a walrus's match", "import re", "if m := re.match('([0-9]+)', t):\n    n = int(m.group(1))"),
        )
        for place in PLACES
    },
    # A module-level binding is read by a function that reads the module's name.
    "int() in a function of a module-level findall list": _form(
        int_parse_problems, "import re\n\nITEMS = re.findall('[0-9]+', T)\n\n\ndef f():\n    return int(ITEMS[0])\n"
    ),
    # A name is a group's only in the scope that binds it: g's parameter is not f's list.
    "int() of a parameter another function's findall list shares the name of": _form(
        int_parse_problems,
        "import re\n\n\ndef f(t):\n    items = re.findall('[0-9]+', t)\n    return items\n\n\n"
        "def g(items):\n    return int(items[0])\n",
        refused=False,
    ),
    # A function the module defines under re's name is no re function.
    "int() of a findall the module defines": _form(
        int_parse_problems,
        "def findall(p, t):\n    return [t]\n\n\ndef f(t):\n    return int(findall('[0-9]+', t)[0])\n",
        refused=False,
    ),
    # re's function tests its second argument, the text; the pattern it is given first is no value it admits.
    "int() of the pattern fullmatch was given": _form(
        int_parse_problems,
        "import re as r\n\n\ndef f(t, p):\n    if r.fullmatch(p, t):\n        return int(p)\n",
        refused=False,
    ),
    # A call such as next() or pop() hands back an item the policy does not follow; the paragraph names this limit.
    "int() of findall's item through next()": _form(
        int_parse_problems,
        "import re\n\n\ndef f(t):\n    return int(next(iter(re.findall('[0-9]+', t))))\n",
        refused=False,
    ),
    # A count of the list findall returns is no group.
    "int() of findall's count": _form(
        int_parse_problems, "import re\n\n\ndef f(t):\n    return int(len(re.findall('[0-9]+', t)))\n", refused=False
    ),
    # "an `add_argument` ... whose `type` is `int` or `float`, by name, through an alias, a `getattr` of a constant
    # name, or a `functools.partial` of one", read each way the call may run.
    **{
        f"an option read with {form}": _form(int_parse_problems, f"{imports}\n\n{body}\n")
        for form, imports, body in (
            ("type=int", "import argparse", "argparse.ArgumentParser().add_argument('--pull', type=int)"),
            ("type=float", "import argparse", "argparse.ArgumentParser().add_argument('--ratio', type=float)"),
            ("type=builtins.int", "import builtins", "PARSER.add_argument('--pull', type=builtins.int)"),
            ("an imported alias of int", "from builtins import int as whole", "P.add_argument('--n', type=whole)"),
            ("a name bound to int", "WHOLE = int", "P.add_argument('--n', type=WHOLE)"),
            ("a getattr of int", "import builtins", "P.add_argument('--n', type=getattr(builtins, 'int'))"),
            ("a partial of int", "import functools", "P.add_argument('--n', type=functools.partial(int, base=10))"),
            ("a ** of a literal", "import argparse", "P.add_argument('--n', **{'type': int})"),
            ("a dict() of keywords", "import argparse", "P.add_argument('--n', **dict(type=float))"),
            (
                "a partial of add_argument",
                "import functools",
                "add = functools.partial(P.add_argument, type=int)\nadd('--n')",
            ),
            ("getattr of add_argument", "import argparse", "getattr(P, 'add_argument')('--n', type=int)"),
            (
                "a subparser's add_argument",
                "import argparse",
                "argparse.ArgumentParser().add_subparsers().add_parser('a').add_argument('--n', type=int)",
            ),
        )
    },
    # "and on an `int()` or `float()` of its own parameter, stripped or not, in a function or a lambda the module
    # gives an `add_argument` as its `type`".
    **{
        f"an option's type function that converts its parameter {form}": _form(
            int_parse_problems, f"import argparse\n\n{body}\n"
        )
        for form, body in (
            ("with int()", "def count(value):\n    return int(value)\n\n\nP.add_argument('--n', type=count)"),
            ("stripped", "def count(value):\n    return int(value.strip())\n\n\nP.add_argument('--n', type=count)"),
            ("with a base", "def count(value):\n    return int(value, 10)\n\n\nP.add_argument('--n', type=count)"),
            ("with float()", "def ratio(value):\n    return float(value)\n\n\nP.add_argument('--r', type=ratio)"),
            (
                "through a method call",
                "def count(value):\n    return int(value.replace(',', ''))\n\n\nP.add_argument('--n', type=count)",
            ),
            ("through an index", "def count(value):\n    return int(value[1:])\n\n\nP.add_argument('--n', type=count)"),
            (
                "through a local name",
                "def count(value):\n    text = value.lower()\n    return int(text)\n\n\n"
                "P.add_argument('--n', type=count)",
            ),
            (
                "through a local name bound later in the walk",
                "def count(value):\n    if value:\n        return int(text)\n    text = value.strip()\n\n\n"
                "P.add_argument('--n', type=count)",
            ),
            ("in a lambda", "P.add_argument('--n', type=lambda value: int(value))"),
            ("in a lambda bound to a name", "COUNT = lambda value: int(value)\nP.add_argument('--n', type=COUNT)"),
            (
                "in a function defined in a function",
                "def build(p):\n    def count(value):\n        return int(value)\n\n"
                "    p.add_argument('--n', type=count)",
            ),
        )
    },
    **{
        f"an option {form}": _form(
            int_parse_problems, f"import argparse\n\nimport whole_number\n\n{body}\n", refused=False
        )
        for form, body in (
            ("read with whole_number.argument", "P.add_argument('--pull', type=whole_number.argument(1))"),
            ("read with type=str", "P.add_argument('--name', type=str)"),
            ("whose default is an int()", "P.add_argument('--n', default=int('3'), type=whole_number.argument())"),
            ("type keyword of a call that is no add_argument", "P.set_defaults(type=int)"),
            (
                "type function that reads through whole_number",
                "def count(value):\n    return whole_number.read(value, ValueError(value))\n\n\n"
                "P.add_argument('--n', type=count)",
            ),
            (
                "type function that converts what is not its parameter",
                "def count(value):\n    return int(len(value))\n\n\nP.add_argument('--n', type=count)",
            ),
            # A type built by a call, as a factory's inner function, is read only by what the call resolves to:
            # the policy's stated limit, so the inner function's int() passes.
            (
                "type a factory builds",
                "def factory():\n    def inner(value):\n        return int(value)\n\n    return inner\n\n\n"
                "P.add_argument('--n', type=factory())",
            ),
            # A type function another module defines is read only by what it resolves to: the policy reads one module
            # at a time, so an imported function's int() passes here, the policy's stated limit.
            ("type function another module defines", "from helpers import count\n\nP.add_argument('--n', type=count)"),
            # A function given the parameter computes something else from it, as len() does, so it is no text of it.
            (
                "type function that converts a function's result on its parameter",
                "def count(value):\n    return int(len(value.strip()))\n\n\nP.add_argument('--n', type=count)",
            ),
            # A function the module never gives as a type is held only to the group and digit-test rules.
            ("function given to no add_argument", "def count(value):\n    return int(value)"),
            # The parameter is the type function's own, not a name an inner function binds over it.
            (
                "type function whose inner function converts its own name",
                "def count(value):\n    def inner():\n        value = '1'\n        return int(value)\n\n"
                "    return inner()\n\n\nP.add_argument('--n', type=count)",
            ),
        )
    },
    # "an `add_argument` whose `type` is `float` and whose option string, `dest`, or `metavar` names a time".
    **{
        f"a float time by its {where}": _form(
            time_option_problems,
            f"import argparse\n\nparser = argparse.ArgumentParser()\nparser.add_argument({arguments}, type=float)\n",
        )
        for where, arguments in (
            ("option string", "'--timeout'"),
            ("dest", "'--wait', dest='timeout'"),
            ("metavar", "'--wait', metavar='SECONDS'"),
        )
    },
    "a float that names no time": _form(
        time_option_problems,
        "import argparse\n\nparser = argparse.ArgumentParser()\nparser.add_argument('--ratio', type=float)\n",
        refused=False,
    ),
}


@pytest.mark.parametrize(
    ("policy", "path", "text", "refused"), list(DOCUMENTED_FORMS.values()), ids=list(DOCUMENTED_FORMS)
)
def test_each_policy_refuses_every_form_its_documentation_names(
    policy: FormsPolicy, path: str, text: str, refused: bool
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {path: text})
        found = policy(root, sorted(file for file in root.rglob("*") if file.is_file()))
    if refused:
        assert found, f"{policy.__name__} passed {path}"
        assert all(problem.startswith(path) for problem in found), found
    else:
        assert found == []


def run_claim_policy(
    policy: Callable[[Path, list[Path]], list[str]], allowance: str, source: str, owner: str
) -> list[str]:
    """What a claim policy reports over the claim fixture: `source` in a skill's module, a suite, a tests/ helper, and
    the claim's owner `owner`, and a tools/ module whose `allowance` maps a scope to an empty reason."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(
            root,
            {
                "skills/alpha/scripts/alpha.py": source,
                "skills/alpha/scripts/test_alpha.py": source,
                "tests/helper.py": source,
                owner: source,
                "tools/broken.py": f"{allowance} = {{'scope': ''}}\n",
            },
        )
        return policy(root, sorted(root.rglob("*.py")))


PROCESS_SOURCE = """\
import os
import subprocess as sp
from asyncio import create_subprocess_exec

from bounded_process import without_repository_variables


def inherits():
    sp.run(["git", "status"])
    sp.Popen(["git"], env=dict(os.environ))


def cleared():
    sp.run(["git"], env=without_repository_variables(os.environ))
    env = without_repository_variables(os.environ)
    env.update({"LC_ALL": "C"})
    return sp.check_output(["git"], env=env)


def handed(env, options):
    sp.call(["git"], env=env)
    sp.check_call(["git"], **options)


def rebound():
    env = without_repository_variables(os.environ)
    env = {}
    sp.run(["git"], env=env)


async def started():
    await create_subprocess_exec("git", env=os.environ)


def sanctioned():
    sp.run(["git"])


PROCESS_ENV_ALLOWED = {"sanctioned": "a reason", "stale": "a reason"}
"""


GIT_ANSWERED_SOURCE = """\
from git_client import GitError


def handled(client):
    try:
        client.output(["status"])
    except GitError as exc:
        return exc.returncode is None


def given(error: GitError) -> bool:
    return error.returncode in (None, 128)


def sanctioned(error: GitError) -> bool:
    return error.returncode is None


GIT_ANSWERED_ALLOWED = {"sanctioned": "a reason", "stale": "a reason"}
"""


def test_git_answered_policy_holds_its_owner_suites_and_allowance() -> None:
    # git_client.py, the owner, a suite, and a tests/ helper are not read; a sanctioned scope passes; a stale or
    # empty entry fails.
    reads = (
        "reads a GitError's returncode is None as git not running, though git_client raises one without an exit code "
        "for an answer in the wrong shape; read GitError.answered, or state why in GIT_ANSWERED_ALLOWED"
    )
    alpha = "skills/alpha/scripts/alpha.py"
    found = run_claim_policy(
        python_checks.git_answered_problems,
        "GIT_ANSWERED_ALLOWED",
        GIT_ANSWERED_SOURCE,
        python_checks.GIT_STATUS_OWNER,
    )
    assert found == [
        f"{alpha}:8 {reads}",
        f"{alpha}:12 {reads}",
        f"{alpha}: GIT_ANSWERED_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
        "tools/broken.py: GIT_ANSWERED_ALLOWED must map each function, class, or assignment to its reason",
    ]


def test_process_environment_policy_refuses_a_start_that_may_keep_the_repository_variables() -> None:
    keeps = "git's repository-selecting variables; run it through ejryan89-skill-core's bounded_process, pass "
    keeps += "env=without_repository_variables(...), or state why in PROCESS_ENV_ALLOWED"
    alpha = "skills/alpha/scripts/alpha.py"
    assert run_claim_policy(
        python_checks.process_environment_problems,
        "PROCESS_ENV_ALLOWED",
        PROCESS_SOURCE,
        "skills/ejryan89-skill-core/scripts/bounded_process.py",
    ) == [
        f"{alpha}:9 starts subprocess.run with no env, so it keeps {keeps}",
        f"{alpha}:10 starts subprocess.Popen with an env that may keep {keeps}",
        f"{alpha}:21 starts subprocess.call with an env that may keep {keeps}",
        f"{alpha}:22 starts subprocess.check_call with an env that may keep {keeps}",
        f"{alpha}:28 starts subprocess.run with an env that may keep {keeps}",
        f"{alpha}:32 starts asyncio.create_subprocess_exec with an env that may keep {keeps}",
        f"{alpha}: PROCESS_ENV_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
        "tools/broken.py: PROCESS_ENV_ALLOWED must map each function, class, or assignment to its reason",
    ]


CLEARED_PREFIX = """\
import os
import subprocess

from bounded_process import without_repository_variables


def start():
    env = without_repository_variables(os.environ)
"""


# Each change in place of a cleared environment the design row's "whose environment may keep them" covers: one that
# can add a key from elsewhere, or adds a repository variable, is refused.
@pytest.mark.parametrize(
    "change",
    [
        pytest.param("env.update(os.environ)", id="an update from inherited input"),
        pytest.param("env.update(GIT_DIR='x')", id="an update naming a repository variable"),
        pytest.param("env.update({'git_dir': 'x'})", id="a dict naming a repository variable in another case"),
        pytest.param("env.update({**os.environ})", id="a dict that unpacks another"),
        pytest.param("env.update(os.environ, LC_ALL='C')", id="an update of a mapping and a clear keyword"),
        pytest.param("env['GIT_DIR'] = os.environ.get('GIT_DIR', '')", id="a subscript assignment of one"),
        pytest.param("env[name] = 'x'", id="a subscript assignment of a key no one can read"),
        pytest.param("env.setdefault('GIT_DIR', 'x')", id="a setdefault of one"),
        pytest.param("env |= os.environ", id="an augmented union from inherited input"),
    ],
)
def test_process_environment_policy_refuses_a_cleared_env_changed_from_an_unproven_source(change: str) -> None:
    source = f"{CLEARED_PREFIX}    name = 'x'\n    {change}\n    return subprocess.run(['x'], env=env, check=False)\n"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"skills/alpha/scripts/alpha.py": source})
        found = python_checks.process_environment_problems(root, sorted(root.rglob("*.py")))
    assert [problem.split(" ", 1)[0] for problem in found] == ["skills/alpha/scripts/alpha.py:11"]


# Changes in place proven to add no repository variable: every key they can add is a constant naming none.
@pytest.mark.parametrize(
    "change",
    [
        pytest.param("env.update({'LC_ALL': 'C'})", id="an update of a constant dict"),
        pytest.param("env.update(LC_ALL=os.environ.get('LANG', 'C'))", id="a clear keyword whatever its value"),
        pytest.param("env['LC_ALL'] = os.environ.get('LANG', 'C')", id="a subscript assignment of a clear key"),
        pytest.param("env.setdefault('LC_ALL', 'C')", id="a setdefault of a clear key"),
        pytest.param("env |= {'LC_ALL': 'C'}", id="an augmented union of a constant dict"),
        pytest.param("env.pop('LC_ALL', None)", id="a removal, which adds nothing"),
    ],
)
def test_process_environment_policy_passes_a_cleared_env_changed_only_by_clear_keys(change: str) -> None:
    source = f"{CLEARED_PREFIX}    {change}\n    return subprocess.run(['x'], env=env, check=False)\n"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"skills/alpha/scripts/alpha.py": source})
        assert python_checks.process_environment_problems(root, sorted(root.rglob("*.py"))) == []


# The paragraph's "a name its function binds only to one and changes in place, by an update, a setdefault, a subscript
# assignment, or |=, only with constant keys": the mapping reached any other way, through another name, a call it is
# passed to, a closure, an unbound or stored method, or a function changing a module-level name, is refused.
@pytest.mark.parametrize(
    "change",
    [
        pytest.param("e = env\n    e['GIT_DIR'] = 'x'", id="an alias"),
        pytest.param("e, _ = env, 1\n    e.update(os.environ)", id="an alias by a tuple assignment"),
        pytest.param("holder = [env]\n    holder[0].update(os.environ)", id="a container holding it"),
        pytest.param("dict.update(env, os.environ)", id="dict.update"),
        pytest.param("u = env.update\n    u(os.environ)", id="a stored bound method"),
        pytest.param("operator.setitem(env, 'GIT_DIR', 'x')", id="operator.setitem"),
        pytest.param("env.__setitem__('GIT_DIR', 'x')", id="a dunder setitem"),
        pytest.param("env.__ior__(os.environ)", id="a dunder union"),
        pytest.param("getattr(env, 'update')(os.environ)", id="getattr of a method"),
        pytest.param("change(env)", id="a helper given it"),
        pytest.param("change(environment=env)", id="a helper given it by keyword"),
        pytest.param("def g():\n        env.update(os.environ)\n\n    g()", id="a closure that updates it"),
        pytest.param("def g():\n        env['GIT_DIR'] = 'x'\n\n    g()", id="a closure that assigns a key"),
        pytest.param("(lambda: env.update(os.environ))()", id="a lambda that updates it"),
        pytest.param("env.update(os.environ) if name else None", id="an update in an expression"),
    ],
)
def test_process_environment_policy_refuses_a_cleared_env_reached_another_way(change: str) -> None:
    source = (
        "import operator\n"
        + CLEARED_PREFIX
        + f"    name = 'x'\n    {change}\n    return subprocess.run(['x'], env=env, check=False)  # read\n\n\n"
        "def change(environment=None):\n    return environment\n"
    )
    line = next(number for number, text in enumerate(source.splitlines(), 1) if text.endswith("# read"))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"skills/alpha/scripts/alpha.py": source})
        found = python_checks.process_environment_problems(root, sorted(root.rglob("*.py")))
    assert [problem.split(" ", 1)[0] for problem in found] == [f"skills/alpha/scripts/alpha.py:{line}"]


MODULE_CLEARED = """\
import os
import subprocess

from bounded_process import without_repository_variables

ENV = without_repository_variables(os.environ)
"""


# A name the module's top level binds is no function's, so a start in a function given it is refused as before; one at
# the top level passes only while nothing anywhere in the module changes the name but the paragraph's constant keys.
@pytest.mark.parametrize(
    ("body", "refused"),
    [
        pytest.param("subprocess.run(['x'], env=ENV, check=False)  # read\n", False, id="a start beside it"),
        pytest.param(
            "\n\ndef change():\n    ENV.update(os.environ)\n\n\nsubprocess.run(['x'], env=ENV, check=False)  # read\n",
            True,
            id="a function that updates it",
        ),
        pytest.param(
            "\n\ndef change():\n    ENV['LC_ALL'] = 'C'\n\n\nsubprocess.run(['x'], env=ENV, check=False)  # read\n",
            False,
            id="a function that sets a clear key",
        ),
        pytest.param(
            "\n\ndef read():\n    return ENV.get('PATH')\n\n\nsubprocess.run(['x'], env=ENV, check=False)  # read\n",
            False,
            id="a function that reads it",
        ),
        pytest.param(
            "\n\ndef start():\n    return subprocess.run(['x'], env=ENV, check=False)  # read\n",
            True,
            id="a start in a function",
        ),
    ],
)
def test_process_environment_policy_reads_a_module_level_cleared_env_across_the_module(
    body: str, refused: bool
) -> None:
    source = MODULE_CLEARED + body
    line = next(number for number, text in enumerate(source.splitlines(), 1) if text.endswith("# read"))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"skills/alpha/scripts/alpha.py": source})
        found = python_checks.process_environment_problems(root, sorted(root.rglob("*.py")))
    assert [problem.split(" ", 1)[0] for problem in found] == ([f"skills/alpha/scripts/alpha.py:{line}"] * refused)


# Reads, removals, and the start itself leave the mapping as its bindings make it.
@pytest.mark.parametrize(
    "use",
    [
        pytest.param("path = env.get('PATH')", id="a get"),
        pytest.param("path = env['PATH']", id="a subscript read"),
        pytest.param("known = 'PATH' in env", id="a membership test"),
        pytest.param("pairs = list(env.items())", id="an items view"),
        pytest.param("copied = env.copy()\n    copied.update(os.environ)", id="a copy changed apart"),
        pytest.param("del env['LC_ALL']", id="a deleted key"),
        pytest.param("def g():\n        return env.get('PATH')\n\n    g()", id="a closure that reads it"),
        pytest.param("subprocess.run(['y'], env=env, check=False)", id="another start given it"),
        # The paragraph's named limit: a reach through globals() or vars() is not read.
        pytest.param("vars()['env'].update(os.environ)", id="a reach through vars(), a named limit"),
    ],
)
def test_process_environment_policy_passes_a_cleared_env_only_read(use: str) -> None:
    source = f"{CLEARED_PREFIX}    {use}\n    return subprocess.run(['x'], env=env, check=False)\n"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"skills/alpha/scripts/alpha.py": source})
        assert python_checks.process_environment_problems(root, sorted(root.rglob("*.py"))) == []


LINK_SOURCE = """\
import os
from os.path import islink
from pathlib import Path

from deployer import platform_support
from deployer.platform_support import is_junction
from link_probe import is_link_or_junction


def tests(path: Path) -> list[bool]:
    return [path.is_symlink(), os.path.islink(path), islink(path), path.is_junction()]


def reads(path):
    return os.lstat(path).st_file_attributes, getattr(os.lstat(path), "st_file_attributes", 0)


def conforms(path):
    return is_link_or_junction(path), is_junction(path), platform_support.is_junction(path)


def told_apart(entry):
    return entry.is_symlink()


LINK_TEST_ALLOWED = {"told_apart": "names which kind of link it refuses", "stale": "a reason"}
"""


def test_link_test_policy_refuses_a_link_test_outside_link_probe() -> None:
    alpha = "skills/alpha/scripts/alpha.py"
    found = run_claim_policy(
        python_checks.link_test_problems,
        "LINK_TEST_ALLOWED",
        LINK_SOURCE,
        "skills/ejryan89-skill-core/scripts/link_probe.py",
    )
    remedy = (
        "; call ejryan89-skill-core's link_probe, the one link test, which reads a symbolic link and a junction alike, "
        "or state why in LINK_TEST_ALLOWED"
    )
    assert found == [
        *(f"{alpha}:11 tests for a link with {test}{remedy}" for test in ("is_symlink", "islink", "islink")),
        f"{alpha}:11 tests for a link with is_junction{remedy}",
        *(f"{alpha}:15 tests for a link with st_file_attributes{remedy}" for _ in range(2)),
        f"{alpha}: LINK_TEST_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
        "tools/broken.py: LINK_TEST_ALLOWED must map each function, class, or assignment to its reason",
    ]


JSON_SOURCE = """\
import json
import json as j
from json import JSONDecoder, load
from json.decoder import JSONDecoder as Decoder

import json_reader


def parses(text, handle):
    return json.loads(text), j.load(handle), load(handle), JSONDecoder(), Decoder(), json.decoder.JSONDecoder()


def conforms(text):
    return json_reader.loads(text), json_reader.raw_decode(text), json.dumps(text), json.JSONDecodeError


def own(value):
    return json.loads(json.dumps(value))


JSON_PARSE_ALLOWED = {"own": "parses the text it just serialized", "stale": "a reason"}
"""


def test_json_parse_policy_refuses_a_parse_outside_json_reader() -> None:
    alpha = "skills/alpha/scripts/alpha.py"
    found = run_claim_policy(
        python_checks.json_parse_problems,
        "JSON_PARSE_ALLOWED",
        JSON_SOURCE,
        "skills/ejryan89-skill-core/scripts/json_reader.py",
    )
    remedy = (
        ", which keeps a repeated key's last value and reads NaN; call ejryan89-skill-core's json_reader, the one "
        "parse of JSON, or state why in JSON_PARSE_ALLOWED"
    )
    parsers = ("json.loads", "json.load", "json.load", "json.JSONDecoder")
    assert found == [
        *(f"{alpha}:10 parses JSON with {parser}{remedy}" for parser in parsers),
        *(f"{alpha}:10 parses JSON with json.decoder.JSONDecoder{remedy}" for _ in range(2)),
        f"{alpha}: JSON_PARSE_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
        "tools/broken.py: JSON_PARSE_ALLOWED must map each function, class, or assignment to its reason",
    ]


JSON_WRITE_SOURCE = """\
import json
import json as j
from json import dump, dumps
from pathlib import Path

import json_writer
from atomic_file import replace_text


def writes(path, handle, value):
    json.dump(value, handle), j.dump(value, handle), dump(value, handle)
    path.write_text(json.dumps(value) + "\\n", encoding="utf-8")
    replace_text(path, dumps(value, indent=2))
    text = json.dumps(value)
    line = text + "\\n"
    handle.write(line.encode("utf-8"))


def conforms(path, handle, value):
    json_writer.write(path, value)
    path.write_text(json_writer.dumps(value), encoding="utf-8")
    message = f"cannot write {json.dumps(str(path))}"
    handle.write("plain text")
    return json.dumps(value), message


def other(path, value):
    text = "unrelated"
    path.write_text(text)


def piped(stream, value):
    stream.write(json.dumps(value))


def respelled(path, stream, data, values):
    if (text := json.dumps(data)): path.write_text(text)
    text2, other = json.dumps(data), ""; path.write_text(text2)
    for text3 in [json.dumps(data)]: path.write_text(text3)
    alias = json.dump; alias(data, stream)
    getattr(json, "dump")(data, stream)
    path.write_text(json.JSONEncoder().encode(data))
    encoder = json.JSONEncoder(indent=2); stream.writelines(encoder.iterencode(data))
    write = stream.write; write(json.dumps(data))
    with open_text(json.dumps(data)) as text4: path.write_text(text4)
    first, *rest = json.dumps(data), ""; path.write_text(first)
    path.write_text("".join(chunk for chunk in json.JSONEncoder().iterencode(data)))


def respelled_conforms(path, data):
    text5, other = json.dumps(data), "plain"; path.write_text(other)
    encoder = json.JSONEncoder(); path.write_text(str(encoder))
    for item in ["plain", "text"]: path.write_text(item)


JSON_WRITE_ALLOWED = {"piped": "writes a message to a pipe no file holds", "stale": "a reason"}
"""


def test_json_write_policy_refuses_a_json_write_outside_json_writer() -> None:
    alpha = "skills/alpha/scripts/alpha.py"
    found = run_claim_policy(
        python_checks.json_write_problems,
        "JSON_WRITE_ALLOWED",
        JSON_WRITE_SOURCE,
        "skills/ejryan89-skill-core/scripts/json_writer.py",
    )
    remedy = (
        "; call ejryan89-skill-core's json_writer, the one writer of JSON, which refuses NaN and a lone surrogate at "
        "the write, or state why in JSON_WRITE_ALLOWED"
    )
    assert found == [
        *(f"{alpha}:11 writes JSON with json.dump{remedy}" for _ in range(3)),
        *(f"{alpha}:{line} writes JSON text to a file or a stream{remedy}" for line in (12, 13, 16)),
        *(f"{alpha}:{line} writes JSON text to a file or a stream{remedy}" for line in (37, 38, 39)),
        *(f"{alpha}:40 writes JSON with json.dump{remedy}" for _ in range(2)),
        f"{alpha}:41 writes JSON with json.dump{remedy}",
        *(f"{alpha}:{line} writes JSON text to a file or a stream{remedy}" for line in (42, 43, 44, 45, 46, 47)),
        f"{alpha}: JSON_WRITE_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
        "tools/broken.py: JSON_WRITE_ALLOWED must map each function, class, or assignment to its reason",
    ]


def test_json_write_policy_with_no_module_to_read_could_not_measure() -> None:
    # Only the writer itself, a suite, and a file outside the claim roots: nothing the policy reads.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(
            root,
            {
                "skills/ejryan89-skill-core/scripts/json_writer.py": "import json\n\njson.dump(1, None)\n",
                "skills/alpha/scripts/test_alpha.py": "import json\n\njson.dump(1, None)\n",
                "docs/notes.py": "import json\n\njson.dump(1, None)\n",
            },
        )
        found = python_checks.json_write_problems(root, sorted(root.rglob("*.py")))
    assert found == ["could not measure: no module the claims read was given, so no json.dumps write was looked for"]


ENCODING_SOURCE = """\
import gzip
import io as text_io
import lzma
import os
import subprocess as sp
import tarfile
import tempfile
from io import TextIOWrapper as Wrapper
from pathlib import Path
from tempfile import NamedTemporaryFile


def opened(path: Path, mode: str, stream, fd: int, options: dict, archive) -> None:
    open(path)  # refused: opens text with open()
    open(path, "w")  # refused: opens text with open()
    open(path, mode=mode)  # refused: opens text with open()
    open(path, "w", encoding=None)  # refused: opens text with open()
    open(path, "rb")
    open(path, "w", -1, "utf-8")
    open(path, "w", encoding="locale")
    open(path, "w", **options)
    text_io.open(path, "a")  # refused: opens text with io.open()
    os.fdopen(fd, "w")  # refused: opens text with os.fdopen()
    Wrapper(stream)  # refused: opens text with io.TextIOWrapper()
    Wrapper(stream, "utf-8")
    path.read_text()  # refused: opens text with read_text()
    path.read_text("utf-8")
    path.write_text("x")  # refused: opens text with write_text()
    path.write_text("x", encoding="utf-8")
    path.open()  # refused: opens text with open()
    path.open("w", encoding="utf-8")
    path.open("rb")
    archive.open(path.name)
    tarfile.open(path)
    os.open(path, os.O_RDONLY)
    NamedTemporaryFile("w")  # refused: opens text with tempfile.NamedTemporaryFile()
    NamedTemporaryFile("w", encoding="utf-8")
    tempfile.TemporaryFile()
    tempfile.SpooledTemporaryFile(0, "w+")  # refused: opens text with tempfile.SpooledTemporaryFile()
    gzip.open(path, "rt")  # refused: opens text with gzip.open()
    gzip.open(path)
    lzma.open(path, "rt", encoding="utf-8")
    sp.run(["git"], text=True)  # refused: decodes subprocess.run's output as text
    sp.check_output(["git"], universal_newlines=True)  # refused: decodes subprocess.check_output's output as text
    sp.run(["git"], text=True, encoding="utf-8")
    sp.run(["git"], text=False)
    sp.run(["git"])


def sanctioned(path: Path) -> str:
    return path.read_text()


ENCODING_ALLOWED = {"sanctioned": "reads what a program wrote in the locale's encoding", "stale": "a reason"}
"""


def test_encoding_policy_refuses_text_io_that_names_no_encoding() -> None:
    remedy = (
        " without an encoding, which reads the locale's before Python 3.15 and UTF-8 from it; pass encoding=, "
        '"locale" where the locale\'s is meant, or state why in ENCODING_ALLOWED'
    )
    refused = [
        (number, line.partition("# refused: ")[2])
        for number, line in enumerate(ENCODING_SOURCE.splitlines(), start=1)
        if "# refused: " in line
    ]
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        # A skill's module, its suite, and a suites' support module are all read: suites are what drift by version.
        names = ["skills/alpha/scripts/alpha.py", "skills/alpha/scripts/test_alpha.py", "tests/helper.py"]
        write_fixture_tree(
            root,
            {
                **dict.fromkeys(names, ENCODING_SOURCE),
                "tools/broken.py": "ENCODING_ALLOWED = {'scope': ''}\n",
                "docs/outside.py": "open('notes.txt')\n",
            },
        )
        found = python_checks.encoding_problems(root, sorted(root.rglob("*.py")))
    assert found == [
        *(
            entry
            for name in names
            for entry in [
                *(f"{name}:{number} {what}{remedy}" for number, what in refused),
                f"{name}: ENCODING_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
            ]
        ),
        "tools/broken.py: ENCODING_ALLOWED must map each function, class, or assignment to its reason",
    ]


def test_encoding_policy_with_no_python_to_read_could_not_measure() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"docs/outside.py": "open('notes.txt')\n", "skills/alpha/SKILL.md": "open()\n"})
        found = python_checks.encoding_problems(root, sorted(root.rglob("*.*")))
    assert found == ["could not measure: no Python under the format roots was given, so no text open was looked for"]


def test_claim_policies_read_the_policies_their_runner_and_the_root_modules() -> None:
    # The claims speak of the repository's Python, so a policy's own reader is held to them as a skill's is; a suite
    # and the suites' support trees stay out.
    parse = "import json\n\njson.loads('1')\n"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        held = ["conftest.py", "deploy.py", "tests/run_validation.py", "tests/validation/policy.py"]
        write_fixture_tree(
            root, dict.fromkeys([*held, "tests/validation/test_policy.py", "tests/deployer/harness.py"], parse)
        )
        found = python_checks.json_parse_problems(root, sorted(root.rglob("*.py")))
    assert [problem.split(" ", 1)[0] for problem in found] == [f"{name}:3" for name in held]


def test_a_policy_scope_fails_for_each_module_it_skips_and_each_stale_omission() -> None:
    modules = ["deploy.py", "skills/a/scripts/a.py", "tests/deployer/harness.py", "tests/validation/policy.py"]
    assert scope_problems("p", modules, ("skills", "deploy.py", "tests/validation"), {"tests": "support"}) == []
    assert scope_problems("p", modules, ("skills", "tests"), {"tools": "gone", "deploy.py": " "}) == [
        "p leaves out deploy.py without a reason",
        "p leaves out tools, which holds no module the policy does not already read",
    ]
    assert scope_problems("p", modules, ("skills", "tests/validation"), {}) == [
        f"p does not read {name}; add its root to the policy's scope, or state why it is left out"
        for name in ("deploy.py", "tests/deployer/harness.py")
    ]


CONTENT_READER_SOURCE = """\
import json


class Refused(ValueError):
    pass


CONTENT_ERRORS = (Refused, json.JSONDecodeError)
CONTENT_READERS = ("read",)


def read(text):
    return text


def quote(text):
    return text


def _private(text):
    return text
"""

CONTENT_CALLER_SOURCE = """\
import contextlib
import json

import reader
from reader import read

FAMILY = (*reader.CONTENT_ERRORS, OSError)


class Mine(reader.Refused):
    pass


def narrow(text):
    try:
        return reader.read(text)
    except json.JSONDecodeError:
        return None


def io_only(path):
    with contextlib.suppress(OSError):
        read(path)


def through(text):
    return read(text)


def via(text):
    try:
        return through(text)
    except reader.Refused:
        return None


def subclass(text):
    try:
        return read(text)
    except Mine:
        return None


def reraised(text):
    try:
        return read(text)
    except reader.Refused:
        raise
    except json.JSONDecodeError:
        return None


def whole(text):
    try:
        return read(text)
    except FAMILY:
        return None


def wider(text):
    with contextlib.suppress(ValueError):
        return through(text)


def unrelated(text):
    try:
        return read(text)
    except KeyError:
        return None


def nested(text):
    try:
        def inner():
            return read(text)
        return inner
    except OSError:
        return None


def unread(text):
    try:
        return reader.quote(text), reader._private(text)
    except OSError:
        return None


def in_handler(text):
    try:
        return text.upper()
    except AttributeError:
        return read(text)


def own(text):
    try:
        return read(text)
    except reader.Refused:
        return None


def trusted(text):
    return read(text)


def sealed(text):
    try:
        return trusted(text)
    except OSError:
        return None


class Holder:
    def method(self, text):
        try:
            return read(text)
        except OSError:
            return None


def bare(text):
    try:
        return read(text)
    except:
        return None


def broad(text):
    try:
        return read(text)
    except Exception:
        return None


def grouped(text):
    try:
        read(text)
    except* json.JSONDecodeError:
        pass


async def waited(text):
    try:
        return read(text)
    except reader.Refused:
        return None


def finally_only(text):
    try:
        try:
            return read(text)
        finally:
            text = ""
    except json.JSONDecodeError:
        return None


def entered(text):
    try:
        with contextlib.nullcontext():
            return read(text)
    except reader.Refused:
        return None


def in_finally(text):
    try:
        try:
            text = text.strip()
        finally:
            read(text)
    except json.JSONDecodeError:
        return None


async def entered_async(text):
    try:
        async with contextlib.AsyncExitStack():
            return read(text)
    except reader.Refused:
        return None


CONTENT_ERRORS_ALLOWED = {
    "own": "reads text it wrote",
    "trusted": "reads text it wrote",
    "stale": "a reason",
}
"""


CONTENT_PACKAGE_CALLER_SOURCE = """\
import json

from . import load
from .helper import fetch


def first(text):
    try:
        return load(text)
    except json.JSONDecodeError:
        return None


def second(text):
    try:
        return fetch(text)
    except json.JSONDecodeError:
        return None
"""


def test_content_errors_policy_refuses_a_handler_that_catches_part_of_a_readers_family() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(
            root,
            {
                "skills/ejryan89-skill-core/scripts/reader.py": CONTENT_READER_SOURCE,
                "skills/ejryan89-skill-core/scripts/unresolved.py": "CONTENT_ERRORS = (missing.Error,)\n",
                # A tuple that names no reader at all.
                "skills/ejryan89-skill-core/scripts/empty.py": (
                    "CONTENT_ERRORS = (ValueError,)\nCONTENT_READERS = ()\n\n\ndef read(text):\n    return text\n"
                ),
                "skills/ejryan89-skill-core/scripts/unnamed.py": (
                    "CONTENT_ERRORS = (ValueError,)\nCONTENT_READERS = ('gone',)\n\n\n"
                    "def read(text):\n    return text\n"
                ),
                "skills/alpha/scripts/alpha.py": CONTENT_CALLER_SOURCE,
                "skills/alpha/scripts/test_alpha.py": CONTENT_CALLER_SOURCE,
                # Another skill's script imported by the same name, which the model could not tell apart.
                "skills/beta/scripts/alpha.py": "",
                # A package's own module and a relative import of each form.
                "tools/__init__.py": "from reader import read\n\n\ndef load(text):\n    return read(text)\n",
                "tools/helper.py": "from reader import read\n\n\ndef fetch(text):\n    return read(text)\n",
                "tools/caller.py": CONTENT_PACKAGE_CALLER_SOURCE,
                "tools/broken.py": "CONTENT_ERRORS_ALLOWED = {'scope': ''}\n",
            },
        )
        found = python_checks.content_error_problems(root, sorted(root.rglob("*.py")), {})
    alpha = "skills/alpha/scripts/alpha.py"

    def remedy(missing: str) -> str:
        return (
            f"catches part of what a reader it calls raises, or its OSError, but not {missing}; catch the whole "
            "family, reader.CONTENT_ERRORS, or state why in CONTENT_ERRORS_ALLOWED"
        )

    both = "json.decoder.JSONDecodeError, reader.Refused"
    assert found == [
        f"skills/beta/scripts/alpha.py: {alpha} is imported as alpha too, so the content-errors policy, which reads "
        "modules by the name they are imported as, cannot read both; rename one",
        "skills/ejryan89-skill-core/scripts/empty.py: CONTENT_READERS must be a tuple naming each function the "
        "module defines that raises its CONTENT_ERRORS on content",
        "skills/ejryan89-skill-core/scripts/unnamed.py: CONTENT_READERS must be a tuple naming each function the "
        "module defines that raises its CONTENT_ERRORS on content",
        "skills/ejryan89-skill-core/scripts/unresolved.py: CONTENT_ERRORS must be a tuple of exception classes its "
        "imports name",
        f"{alpha}:15 {remedy('reader.Refused')}",
        f"{alpha}:22 {remedy(both)}",
        f"{alpha}:31 {remedy('json.decoder.JSONDecodeError')}",
        f"{alpha}:38 {remedy(both)}",
        f"{alpha}:45 {remedy('reader.Refused')}",
        f"{alpha}:115 {remedy(both)}",
        f"{alpha}:136 {remedy('reader.Refused')}",
        f"{alpha}:143 {remedy('json.decoder.JSONDecodeError')}",
        f"{alpha}:150 {remedy('reader.Refused')}",
        f"{alpha}:160 {remedy('json.decoder.JSONDecodeError')}",
        # A finally block runs inside the try that holds its statement, so the outer handler judges its reader call.
        f"{alpha}:168 {remedy('reader.Refused')}",
        f"{alpha}:178 {remedy('json.decoder.JSONDecodeError')}",
        f"{alpha}: CONTENT_ERRORS_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
        "tools/broken.py: CONTENT_ERRORS_ALLOWED must map each function, class, or assignment to its reason",
        f"tools/caller.py:8 {remedy('reader.Refused')}",
        f"tools/caller.py:15 {remedy('reader.Refused')}",
    ]


# A module's top level binds under an if, a try, a with, or a loop as it does unindented: a reader a skill-core module
# defines there, a repository function that calls one, and a name bound once to it are each read.
CONTENT_BLOCK_READER_SOURCE = """\
import json
import sys


class Refused(ValueError):
    pass


CONTENT_ERRORS = (Refused, json.JSONDecodeError)
CONTENT_READERS = ("read",)

if sys.platform:

    def read(text):
        return text
"""
CONTENT_BLOCK_CALLER_SOURCE = """\
import sys

from reader import Refused, read

if sys.platform:

    def load(text):
        return read(text)

try:
    LOAD = load
except NameError:
    pass


def through_function(text):
    try:
        return load(text)
    except Refused:
        return None


def through_name(text):
    try:
        return LOAD(text)
    except Refused:
        return None
"""


# A family's class defined under a top-level try, its CONTENT_ERRORS tuple bound under an if, and a caller's lambda
# and tuple of classes bound under an if are each read too.
CONTENT_BLOCK_FAMILY_SOURCE = """import json
import sys

try:

    class Mangled(ValueError):
        pass

except NameError:
    pass

if sys.platform:
    CONTENT_ERRORS = (Mangled, json.JSONDecodeError)
CONTENT_READERS = ("parse",)


def parse(text):
    return text
"""
CONTENT_BLOCK_FAMILY_CALLER_SOURCE = """import sys

from blocks import Mangled, parse

if sys.platform:
    PARSE = lambda text: parse(text)
    NARROW = (Mangled,)


def through_lambda(text):
    try:
        return PARSE(text)
    except Mangled:
        return None


def through_tuple(text):
    try:
        return parse(text)
    except NARROW:
        return None
"""


def test_content_errors_policy_reads_what_a_module_binds_under_a_top_level_block() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(
            root,
            {
                "skills/ejryan89-skill-core/scripts/reader.py": CONTENT_BLOCK_READER_SOURCE,
                "skills/alpha/scripts/alpha.py": CONTENT_BLOCK_CALLER_SOURCE,
                "skills/ejryan89-skill-core/scripts/blocks.py": CONTENT_BLOCK_FAMILY_SOURCE,
                "skills/gamma/scripts/gamma.py": CONTENT_BLOCK_FAMILY_CALLER_SOURCE,
            },
        )
        found = python_checks.content_error_problems(root, sorted(root.rglob("*.py")), {})

    def remedy(family: str) -> str:
        return (
            "catches part of what a reader it calls raises, or its OSError, but not json.decoder.JSONDecodeError; "
            f"catch the whole family, {family}, or state why in CONTENT_ERRORS_ALLOWED"
        )

    assert found == [
        *(f"skills/alpha/scripts/alpha.py:{line} {remedy('reader.CONTENT_ERRORS')}" for line in (17, 24)),
        *(f"skills/gamma/scripts/gamma.py:{line} {remedy('blocks.CONTENT_ERRORS')}" for line in (11, 18)),
    ]


# A core module's reader whose family on content is its own class, or its own tuple of classes, as review_io's
# read_json raises PersistenceError.
CONTENT_FAMILY_READER_SOURCE = """\
class StoreError(RuntimeError):
    pass


class Unreadable(StoreError):
    pass


class Known(ValueError):
    pass


BOTH = (StoreError, KeyError)


def load(path):
    return path


def fetch(path):
    return path


class Holder:
    def get(self, path):
        return path


def peek(path):
    return path
"""
CONTENT_FAMILY_CALLER_SOURCE = """\
import kept
import store
from elsewhere import load as other
from store import fetch, load


def narrow(path):
    try:
        return load(path)
    except OSError:
        return None


def member(path):
    try:
        return store.load(path)
    except store.Unreadable:
        return None


def whole(path):
    try:
        return load(path)
    except (store.StoreError, OSError):
        return None


def part_of_a_tuple(path):
    try:
        return fetch(path)
    except KeyError:
        return None


def unrelated(path):
    try:
        return load(path)
    except AttributeError:
        return None


def outside_the_cores(path):
    try:
        return other(path)
    except OSError:
        return None


def in_skill_core(path):
    try:
        return kept.keep(path)
    except OSError:
        return None
"""


def test_content_errors_policy_reads_the_family_named_for_a_core_reader() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(
            root,
            {
                "skills/ejryan89-skill-core/scripts/reader.py": CONTENT_READER_SOURCE,
                "skills/ejryan89-skill-core/scripts/kept.py": (
                    "class Kept(ValueError):\n    pass\n\n\ndef keep(path):\n    return path\n"
                ),
                "skills/ejryan89-code-review-core/scripts/store.py": CONTENT_FAMILY_READER_SOURCE,
                "skills/beta/scripts/elsewhere.py": (
                    "class Lost(ValueError):\n    pass\n\n\ndef load(path):\n    return path\n"
                ),
                "skills/alpha/scripts/alpha.py": CONTENT_FAMILY_CALLER_SOURCE,
            },
        )
        families = {
            "store.load": "store.StoreError",
            "store.fetch": "store.BOTH",
            "kept.keep": "kept.Kept",
            # A reader the module does not define, a family that is no class or tuple, for a reader it does define
            # too, a method, a reader outside both cores, and a module the tree does not hold.
            "store.read": "store.Known",
            "store.fetch_all": "store.Missing",
            "store.peek": "store.Missing",
            "store.Holder.get": "store.Known",
            "elsewhere.load": "elsewhere.Lost",
            "gone.load": "gone.Lost",
            # A reader its module's CONTENT_READERS already gives that module's CONTENT_ERRORS, which this entry
            # would replace.
            "reader.read": "reader.Refused",
        }
        found = python_checks.content_error_problems(root, sorted(root.rglob("*.py")), families)

    def refused(reader: str) -> str:
        return (
            f"tests/validation/python_checks.py: core_reader_families names {reader} with {families[reader]}, but "
            "each must name a module-level function of either core and the class, or the tuple of classes, it raises"
        )

    def remedy(missing: str, family: str) -> str:
        return (
            f"catches part of what a reader it calls raises, or its OSError, but not {missing}; catch the whole "
            f"family, {family}, or state why in CONTENT_ERRORS_ALLOWED"
        )

    alpha = "skills/alpha/scripts/alpha.py"
    assert found == [
        *(
            refused(reader)
            for reader in ("store.read", "store.fetch_all", "store.peek", "store.Holder.get", "elsewhere.load")
        ),
        refused("gone.load"),
        "tests/validation/python_checks.py: core_reader_families names reader.read, which its module's CONTENT_READERS "
        "already gives that module's CONTENT_ERRORS; name it in one of the two",
        f"{alpha}:8 {remedy('store.StoreError', 'store.StoreError')}",
        f"{alpha}:15 {remedy('store.StoreError', 'store.StoreError')}",
        f"{alpha}:29 {remedy('store.StoreError', 'store.BOTH')}",
        f"{alpha}:50 {remedy('kept.Kept', 'kept.Kept')}",
    ]


def test_content_errors_policy_reads_read_json_as_raising_persistence_error() -> None:
    # The repository's own table, read as validation reads it: review_io's read_json raises PersistenceError.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(
            root,
            {
                "skills/ejryan89-skill-core/scripts/reader.py": CONTENT_READER_SOURCE,
                "skills/ejryan89-code-review-core/scripts/review_io.py": (
                    "class PersistenceError(RuntimeError):\n    pass\n\n\ndef read_json(path):\n    return path\n"
                ),
                "skills/alpha/scripts/alpha.py": (
                    "from review_io import read_json\n\n\ndef narrow(path):\n    try:\n"
                    "        return read_json(path)\n    except (OSError, ValueError):\n        return None\n"
                ),
            },
        )
        found = python_checks.content_error_problems(root, sorted(root.rglob("*.py")))
    assert found == [
        "skills/alpha/scripts/alpha.py:5 catches part of what a reader it calls raises, or its OSError, but not "
        "review_io.PersistenceError; catch the whole family, review_io.PersistenceError, or state why in "
        "CONTENT_ERRORS_ALLOWED"
    ]


def test_content_errors_policy_refuses_a_tree_whose_skill_core_names_no_reader() -> None:
    # With no reader every handler would pass, so a tree that lost its readers fails rather than passing empty.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"skills/alpha/scripts/alpha.py": CONTENT_CALLER_SOURCE})
        found = python_checks.content_error_problems(root, sorted(root.rglob("*.py")), {})
    assert found[0] == (
        "skills/ejryan89-skill-core/scripts names no reader in a CONTENT_READERS beside its CONTENT_ERRORS, so the "
        "content-errors policy could check no handler"
    )


def test_a_standard_class_is_found_only_through_a_module_that_imports_and_defines_it() -> None:
    assert python_checks._standard_class("ValueError") is ValueError
    assert python_checks._standard_class("json.JSONDecodeError") is json.JSONDecodeError
    # A name that is no class, a class the module lacks, and a module outside the standard library are none the policy
    # can compare, so a handler naming one is taken to catch everything.
    for name in ("print", "json.NoSuchError", "reader.Refused"):
        assert python_checks._standard_class(name) is None, name
    # Nor is a class of a standard-library module that will not import here, such as one for another platform.
    with mock.patch.object(importlib, "import_module", side_effect=ImportError("no module named winreg")):
        assert python_checks._standard_class("winreg.HKEYType") is None


INT_SOURCE = """\
import re

import whole_number

PATTERN = re.compile(r"v([0-9]+)\\\\.([0-9]+)")


def groups(text):
    match = PATTERN.fullmatch(text)
    first = match.group(1)
    major, minor = match.groups()
    return int(match.group(1)), int(match[2]), int(first), int(major), [int(part) for part in match.groups()]


def bound(text, label):
    found = PATTERN.search(text) if text else None
    if (other := re.match(r"([0-9]+)", label)) is not None:
        return int(found[1] or "0"), int(other.groupdict()["n"]), int(other.groups()[0])
    return [int(value) for value, _ in zip(found.groups(), "ab", strict=False)]


def scanned(text):
    return [int(match.group(1)) for match in PATTERN.finditer(text)]


def tested(token, count, size):
    if token.isdigit() and count.strip().isdecimal() and size.isnumeric():
        return int(token), int(count), int(size.strip())
    return None


def listed(text):
    pairs = re.findall(r"([0-9]+)-([0-9]+)", text)
    return [int(value) for value in PATTERN.findall(text)], [int(low) for low, _ in pairs]


def checked(text, size):
    if not re.fullmatch(r"[0-9]+", size.strip()):
        return None
    if PATTERN.match(text):
        return int(size.strip()), int(text)
    return None


def conforms(text, token):
    match = PATTERN.fullmatch(text)
    return (
        whole_number.read(match.group(1), ValueError(text)),
        whole_number.parse(token),
        int(match.group(2), 16),
        int(token),
        int(len(text)),
    )


def sanctioned(text):
    return int(PATTERN.fullmatch(text).group(1))


INT_PARSE_ALLOWED = {"sanctioned": "the pattern bounds the digits", "stale": "a reason"}
"""


def test_int_parse_policy_refuses_a_group_or_a_digit_tested_value_outside_whole_number() -> None:
    alpha = "skills/alpha/scripts/alpha.py"
    found = run_claim_policy(
        python_checks.int_parse_problems,
        "INT_PARSE_ALLOWED",
        INT_SOURCE,
        "skills/ejryan89-skill-core/scripts/whole_number.py",
    )
    remedy = (
        " with int(), which reads more than ASCII digits and past the digit limit raises a ValueError naming no input; "
        "call ejryan89-skill-core's whole_number.read, the one reader of a whole number from text, or state why in "
        "INT_PARSE_ALLOWED"
    )
    group = f"converts a regular-expression group{remedy}"
    assert found == [
        *(f"{alpha}:12 {group}" for _ in range(5)),
        *(f"{alpha}:18 {group}" for _ in range(3)),
        f"{alpha}:19 {group}",
        f"{alpha}:23 {group}",
        *(f"{alpha}:28 converts a value an {test}() test admitted{remedy}" for test in ("isdigit", "isdecimal")),
        f"{alpha}:28 converts a value an isnumeric() test admitted{remedy}",
        *(f"{alpha}:34 {group}" for _ in range(2)),
        f"{alpha}:41 converts a value a regular-expression fullmatch() admitted{remedy}",
        f"{alpha}:41 converts a value a regular-expression match() admitted{remedy}",
        f"{alpha}: INT_PARSE_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
        "tools/broken.py: INT_PARSE_ALLOWED must map each function, class, or assignment to its reason",
    ]


OPTION_SOURCE = """\
import argparse
import functools

import whole_number

READ = functools.partial(int, base=10)


def count(value):
    return int(value.strip())


def build():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pull", type=int)
    parser.add_argument("--ratio", type=float)
    parser.add_argument("--base", type=READ)
    parser.add_argument("--count", type=count)
    parser.add_argument("--size", type=lambda text: float(text))
    parser.add_argument("--version", type=whole_number.argument(1))
    return parser


def sanctioned(parser):
    parser.add_argument("--exit", type=int)


INT_PARSE_ALLOWED = {"sanctioned": "the workflow passes the code it was given", "stale": "a reason"}
"""


def test_a_type_functions_digit_tested_int_is_reported_once() -> None:
    # The int() converts a value an isdigit() test admitted, which the group and digit-test rule refuses too; the policy
    # names it once, as the option type function's conversion.
    source = (
        "import argparse\n\n\ndef count(value):\n    if value.isdigit():\n        return int(value)\n    return 0\n\n\n"
        "P = argparse.ArgumentParser()\nP.add_argument('--n', type=count)\n"
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"skills/alpha/scripts/alpha.py": source})
        found = python_checks.int_parse_problems(root, sorted(root.rglob("*.py")))
    assert found == [
        "skills/alpha/scripts/alpha.py:6 converts value with int() in count, an option's argparse type, which reads a "
        "sign, spaces, underscores, and every Unicode decimal digit; call ejryan89-skill-core's whole_number.argument, "
        "or seconds_option.positive_seconds for a time, as the option's type, or state why in INT_PARSE_ALLOWED"
    ]


def test_int_parse_policy_refuses_an_option_read_with_int_or_float() -> None:
    alpha = "skills/alpha/scripts/alpha.py"
    found = run_claim_policy(
        python_checks.int_parse_problems,
        "INT_PARSE_ALLOWED",
        OPTION_SOURCE,
        "skills/ejryan89-skill-core/scripts/whole_number.py",
    )
    use = (
        "; call ejryan89-skill-core's whole_number.argument, or seconds_option.positive_seconds for a time, as the "
        "option's type, or state why in INT_PARSE_ALLOWED"
    )
    reads_int = "a sign, spaces, underscores, and every Unicode decimal digit"
    reads_float = "a sign, spaces, underscores, every Unicode decimal digit, nan, and inf"
    assert found == [
        f"{alpha}:10 converts value with int() in count, an option's argparse type, which reads {reads_int}{use}",
        f"{alpha}:15 reads an option with type=int, which reads {reads_int}{use}",
        f"{alpha}:16 reads an option with type=float, which reads {reads_float}{use}",
        f"{alpha}:17 reads an option with type=int, which reads {reads_int}{use}",
        f"{alpha}:19 converts text with float() in a lambda, an option's argparse type, which reads {reads_float}{use}",
        f"{alpha}: INT_PARSE_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
        "tools/broken.py: INT_PARSE_ALLOWED must map each function, class, or assignment to its reason",
    ]


CLOSE_HANDLE_ARGUMENTS = "    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]\n"
CTYPES_SOURCE = """\
import ctypes
from ctypes import WinDLL, wintypes


def _kernel32():
    kernel32 = WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def probe(pid):
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(0x1000, False, pid)
    kernel32.CloseHandle(handle)
    kernel32.GetCurrentProcessId()
    return ctypes.windll.user32.GetForegroundWindow(), ctypes.get_last_error()


def query():
    enabled = ctypes.CDLL("ntdll").RtlAreLongPathsEnabled
    enabled.restype = ctypes.c_ubyte
    return enabled()


def sanctioned(handle):
    return ctypes.WinDLL("kernel32").GetHandleInformation(handle, None)


CTYPES_ALLOWED = {"sanctioned": "a reason", "stale": "a reason"}
"""


CTYPES_PLACES_SOURCE = """import ctypes
from ctypes import wintypes


class Probe:
    def __init__(self):
        self.kernel32 = ctypes.WinDLL("kernel32")

    def open(self, pid):
        return self.kernel32.OpenProcess(1, 0, pid)


def held(pid, libraries):
    libraries["k"] = ctypes.WinDLL("kernel32")
    return libraries["k"].OpenProcess(1, 0, pid)


def looked_up(pid):
    kernel32 = ctypes.WinDLL("kernel32")
    return getattr(kernel32, "OpenProcess")(1, 0, pid)


def other_library(handle):
    user32 = ctypes.WinDLL("user32")
    user32.CloseHandle.argtypes = [wintypes.HANDLE]
    user32.CloseHandle.restype = wintypes.BOOL
    windows = ctypes.windll.kernel32
    return windows.CloseHandle(handle)


def annotated(handle):
    library = ctypes.windll.ntdll
    library.NtClose.argtypes: list = [wintypes.HANDLE]
    library.NtClose.restype = wintypes.LONG
    return library.NtClose(handle)


def set_by_name(handle):
    library = ctypes.windll.advapi32
    setattr(library.CloseServiceHandle, "argtypes", [wintypes.HANDLE])
    setattr(library.CloseServiceHandle, "restype", wintypes.BOOL)
    function = getattr(library, "CloseServiceHandle")
    return function(handle)
"""


def test_ctypes_policy_keys_a_declaration_by_its_library_wherever_the_library_is_held() -> None:
    # A library on an attribute, in a subscript, or reached through getattr is read, and a declaration on user32's
    # CloseHandle is none for kernel32's; an annotated or a setattr declaration is a real one.
    alpha = "skills/alpha/scripts/alpha.py"
    found = run_claim_policy(
        python_checks.ctypes_declaration_problems, "CTYPES_ALLOWED", CTYPES_PLACES_SOURCE, "tests/fixtures/ctypes.py"
    )
    passes = "assigned in the module, so ctypes passes it C ints; assign both, or state why in CTYPES_ALLOWED"
    assert [line for line in found if line.startswith(alpha)] == [
        f"{alpha}:10 calls OpenProcess through ctypes with no argtypes or restype {passes}",
        f"{alpha}:15 calls OpenProcess through ctypes with no argtypes or restype {passes}",
        f"{alpha}:20 calls OpenProcess through ctypes with no argtypes or restype {passes}",
        f"{alpha}:28 calls CloseHandle through ctypes with no argtypes or restype {passes}",
    ]


def test_ctypes_policy_refuses_a_foreign_function_called_undeclared() -> None:
    # OpenProcess is declared, and RtlAreLongPathsEnabled, called with no arguments, needs only its restype.
    alpha = "skills/alpha/scripts/alpha.py"
    found = run_claim_policy(
        python_checks.ctypes_declaration_problems, "CTYPES_ALLOWED", CTYPES_SOURCE, "tests/fixtures/ctypes.py"
    )
    passes = "assigned in the module, so ctypes passes it C ints; assign both, or state why in CTYPES_ALLOWED"
    assert found == [
        f"{alpha}:16 calls CloseHandle through ctypes with no restype {passes}",
        f"{alpha}:17 calls GetCurrentProcessId through ctypes with no restype {passes}",
        f"{alpha}:18 calls GetForegroundWindow through ctypes with no restype {passes}",
        f"{alpha}: CTYPES_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
        "tools/broken.py: CTYPES_ALLOWED must map each function, class, or assignment to its reason",
    ]
    # Declared, CloseHandle passes; with neither declaration, it lacks both.
    restype = "    kernel32.CloseHandle.restype = wintypes.BOOL\n"
    declared = CTYPES_SOURCE.replace(CLOSE_HANDLE_ARGUMENTS, f"{CLOSE_HANDLE_ARGUMENTS}{restype}")
    assert run_claim_policy(python_checks.ctypes_declaration_problems, "CTYPES_ALLOWED", declared, "tests/x.py")[
        :2
    ] == [
        f"{alpha}:18 calls GetCurrentProcessId through ctypes with no restype {passes}",
        f"{alpha}:19 calls GetForegroundWindow through ctypes with no restype {passes}",
    ]
    undeclared = CTYPES_SOURCE.replace(CLOSE_HANDLE_ARGUMENTS, "")
    assert run_claim_policy(python_checks.ctypes_declaration_problems, "CTYPES_ALLOWED", undeclared, "tests/x.py")[
        0
    ] == (f"{alpha}:15 calls CloseHandle through ctypes with no argtypes or restype {passes}")


DISCARD_SOURCE = """\
from bounded_process import run_bounded
from git_client import GitClient

from deployer import platform_support


class Services:
    git: GitClient


def discards(services, client: GitClient | None, tree):
    run_bounded(["git", "status"], 60)
    platform_support.run_tool(["git", "status"])
    GitClient().run(["status"])
    client.run(["status"])
    services.git.run(["status"])
    local = GitClient()
    local.run(["status"])


def checks(client: GitClient, other):
    result = client.run(["status"])
    client.output(["status"])
    other.run(["status"])
    return result.returncode, run_bounded(["git"], 60).returncode


def sanctioned(client: GitClient):
    client.run(["worktree", "prune"])


RESULT_DISCARD_ALLOWED = {"sanctioned": "a reason", "stale": "a reason"}
"""


def test_discarded_result_policy_refuses_a_runner_result_left_unread() -> None:
    # output raises on a failure, and a run of what is no GitClient is no runner.
    alpha = "skills/alpha/scripts/alpha.py"
    found = run_claim_policy(
        python_checks.discarded_result_problems, "RESULT_DISCARD_ALLOWED", DISCARD_SOURCE, "tests/x.py"
    )
    judge = "whose exit status the caller must judge; check it, or state why in RESULT_DISCARD_ALLOWED"
    assert found == [
        f"{alpha}:12 discards the result of bounded_process.run_bounded, {judge}",
        f"{alpha}:13 discards the result of platform_support.run_tool, {judge}",
        *(f"{alpha}:{line} discards the result of GitClient.run, {judge}" for line in (14, 15, 16, 18)),
        f"{alpha}: RESULT_DISCARD_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
        "tools/broken.py: RESULT_DISCARD_ALLOWED must map each function, class, or assignment to its reason",
    ]


STATUS_SOURCE = """\
from git_client import GitClient, GitResult

from tools.helper import git as helper_git


def local(directory) -> GitResult:
    return GitClient().run(["status"], directory=directory)


def reads(client: GitClient, given: GitResult, other):
    direct = client.run(["status"]).returncode
    named = client.run(["status"])
    first = named.returncode
    second = local(".").returncode
    third = given.returncode
    fourth = helper_git(".").returncode
    fine = other.run(["status"]).returncode
    caught = client.output(["status"])
    held: GitResult = other.answer()
    fifth = held.returncode
    assigned: GitResult | None = client.run(["status"])
    sixth = assigned.returncode
    return direct, first, second, third, fourth, fine, caught, fifth, sixth


def sanctioned(client: GitClient):
    return client.run(["status"]).returncode


GIT_STATUS_ALLOWED = {"sanctioned": "a reason", "stale": "a reason"}
"""

STATUS_HELPER = """\
from git_client import GitClient, GitResult


def git(cwd) -> GitResult:
    return GitClient().run(["status"], directory=cwd)
"""


def test_git_status_policy_refuses_an_exit_status_read_outside_the_client() -> None:
    # A result from GitClient.run, from a function of this module or another annotated to return a GitResult, and a
    # name annotated as one are each git's; another object's run, and output, which raises, are not.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(
            root,
            {
                "skills/alpha/scripts/alpha.py": STATUS_SOURCE,
                "skills/alpha/scripts/test_alpha.py": STATUS_SOURCE,
                "tools/helper.py": STATUS_HELPER,
                python_checks.GIT_STATUS_OWNER: STATUS_SOURCE,
                "tools/broken.py": "GIT_STATUS_ALLOWED = {'scope': ''}\n",
            },
        )
        found = python_checks.git_status_problems(root, sorted(root.rglob("*.py")))
    alpha = "skills/alpha/scripts/alpha.py"
    refusal = (
        "reads a git command's exit status, where a failure can read as absence; use git_client's quiet_output, "
        "commit_of, file_at, or rev_parse_paths, or state why in GIT_STATUS_ALLOWED"
    )
    assert found == [
        *(f"{alpha}:{line} {refusal}" for line in (11, 13, 14, 15, 16, 20, 22)),
        f"{alpha}: GIT_STATUS_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
        "tools/broken.py: GIT_STATUS_ALLOWED must map each function, class, or assignment to its reason",
    ]


# Modules a git result can reach a caller through: tools/helper.py's producer, re-exported by a validation module the
# others import by its bare name, and re-exported again by another.
STATUS_REEXPORTS = {
    "tools/helper.py": STATUS_HELPER,
    "tests/validation/support.py": "from tools.helper import git as git\n",
    "tests/validation/again.py": "from support import git as git\n",
    # A subclass of GitClient another module defines, and one that subclasses it again there.
    "tools/clients.py": "from git_client import GitClient\n\n\nclass Mine(GitClient):\n    pass\n\n\n"
    "class Again(Mine):\n    pass\n",
}
STATUS_IMPORTS = "from git_client import GitClient\nfrom support import git\nfrom again import git as twice\n\n\n"
STATUS_CLASS = (
    "from git_client import GitResult\n\n\nclass Repo:\n    def produce(self) -> GitResult:\n"
    "        return GitResult(0, '', '')\n"
)


# Each way a read of the result `r` reaches its returncode, with what it imports: an attribute read by its name, a
# class pattern, and a reach of the result's fields as a whole.
STATUS_READS = [
    ("an attribute", "", "return r.returncode"),
    ("getattr", "", "return getattr(r, 'returncode')"),
    ("inspect.getattr_static", "import inspect\n\n\n", "return inspect.getattr_static(r, 'returncode')"),
    ("object.__getattribute__", "", "return object.__getattribute__(r, 'returncode')"),
    ("a bound __getattribute__", "", "return r.__getattribute__('returncode')"),
    ("operator.attrgetter", "import operator\n\n\n", "return operator.attrgetter('returncode')(r)"),
    ("attrgetter imported", "from operator import attrgetter\n\n\n", "return attrgetter('stdout', 'returncode')(r)"),
    ("attrgetter given a name", "import operator\n\n\n", "return operator.attrgetter(FIELD)(r)"),
    ("vars", "", "return vars(r)['returncode']"),
    ("__dict__", "", "return r.__dict__['returncode']"),
    ("__getstate__", "", "return r.__getstate__()"),
    ("dataclasses.asdict", "import dataclasses\n\n\n", "return dataclasses.asdict(r)['returncode']"),
    ("asdict imported", "from dataclasses import asdict\n\n\n", "return asdict(r).get('returncode')"),
    ("dataclasses.astuple", "import dataclasses\n\n\n", "return dataclasses.astuple(r)[0]"),
    *(
        (f"a class pattern {form}", "from git_client import GitResult\n\n\n", f"match r:\n        {case}: return 1")
        for form, case in (
            ("by keyword", "case GitResult(returncode=0)"),
            ("by position", "case GitResult(0, _, _)"),
            ("of another class by keyword", "case object(returncode=0)"),
            ("under as", "case GitResult(returncode=0) as s"),
            ("in an alternative", "case None | GitResult(returncode=1)"),
        )
    ),
]
# Each client a GitClient subclass gives, from this module, from another, and from a subclass of a subclass.
STATUS_CLIENTS = [
    ("a subclass the module defines", "class Mine(GitClient):\n    pass\n\n\n", "Mine()"),
    ("a subclass of a subclass", "class Mine(GitClient):\n    pass\n\n\nclass Again(Mine):\n    pass\n\n\n", "Again()"),
    ("a subclass another module defines", "from tools.clients import Mine\n\n\n", "Mine()"),
    ("a subclass of a subclass another module defines", "from tools.clients import Again\n\n\n", "Again()"),
    ("a subclass through a module import", "from tools import clients\n\n\n", "clients.Mine()"),
]


# Each form of a read of a git result's exit status, from the policy's paragraph in "Validation" in
# docs/adding-a-skill.md ("what a function annotated to return GitResult returns, in any module validation reads"):
# the line marked `# read` is refused.
@pytest.mark.parametrize(
    "body",
    [
        pytest.param("def f():\n    return git('.').returncode  # read\n", id="a producer re-exported"),
        pytest.param("def f():\n    return twice('.').returncode  # read\n", id="a producer re-exported twice"),
        pytest.param("run = git\n\n\ndef f():\n    return run('.').returncode  # read\n", id="a re-export aliased"),
        pytest.param(
            "def f(client: GitClient):\n    if (r := client.run(['status'])).returncode:  # read\n        return r\n",
            id="a walrus",
        ),
        pytest.param(
            "def f(client: GitClient):\n    if r := client.run(['status']):\n        return r.returncode  # read\n",
            id="a name a walrus binds",
        ),
        pytest.param(
            "def f(client: GitClient):\n    r = client.run(['status'])\n    return getattr(r, 'returncode')  # read\n",
            id="getattr of a name",
        ),
        pytest.param(
            "def f(client: GitClient):\n    return getattr(client.run(['status']), 'returncode')  # read\n",
            id="getattr of the call",
        ),
        # "by an assignment": a tuple assignment binds each name to its own value.
        pytest.param(
            "def f(client: GitClient):\n    r, _ = client.run(['status']), 1\n    return r.returncode  # read\n",
            id="a tuple assignment",
        ),
        pytest.param(
            "def f(client: GitClient):\n    [_, r] = 1, client.run(['status'])\n    return r.returncode  # read\n",
            id="a list target",
        ),
        pytest.param(
            "def f(client: GitClient):\n    r = s = client.run(['status'])\n    return s.returncode  # read\n",
            id="a chained assignment",
        ),
        # "what a function annotated to return GitResult returns", awaited, on a method, or quoted.
        pytest.param(
            "from git_client import GitResult\n\n\nasync def produce() -> GitResult:\n    return GitResult(0, '', '')\n"
            "\n\nasync def f():\n    return (await produce()).returncode  # read\n",
            id="an awaited producer",
        ),
        pytest.param(
            "from git_client import GitResult\n\n\nasync def produce() -> GitResult:\n    return GitResult(0, '', '')\n"
            "\n\nasync def f():\n    r = await produce()\n    return r.returncode  # read\n",
            id="a name bound to an awaited producer",
        ),
        pytest.param(
            f"{STATUS_CLASS}\n    def f(self):\n        return self.produce().returncode  # read\n",
            id="a method producer",
        ),
        pytest.param(
            f"{STATUS_CLASS}\n\ndef f(repo: Repo):\n    return repo.produce().returncode  # read\n",
            id="a method producer on an annotated name",
        ),
        pytest.param(
            f"{STATUS_CLASS}\n\ndef f():\n    repo = Repo()\n    return repo.produce().returncode  # read\n",
            id="a method producer on a constructed instance",
        ),
        pytest.param(
            f"{STATUS_CLASS}\n\ndef f():\n    return Repo().produce().returncode  # read\n",
            id="a method producer on a call of its class",
        ),
        pytest.param(
            "from git_client import GitResult\n\n\ndef produce() -> 'GitResult':\n    return GitResult(0, '', '')\n"
            "\n\ndef f():\n    return produce().returncode  # read\n",
            id="a quoted return annotation",
        ),
        pytest.param(
            "from git_client import GitResult\n\n\ndef f(r: 'GitResult | None'):\n    return r.returncode  # read\n",
            id="a quoted parameter annotation",
        ),
        # "a getattr of returncode is a read", by any spelling of getattr and any name that is not another attribute.
        pytest.param(
            "import builtins\n\n\ndef f(client: GitClient):\n    r = client.run(['status'])\n"
            "    return builtins.getattr(r, 'returncode')  # read\n",
            id="builtins.getattr",
        ),
        pytest.param(
            "FIELD = 'returncode'\n\n\ndef f(client: GitClient):\n    r = client.run(['status'])\n"
            "    return getattr(r, FIELD)  # read\n",
            id="getattr of a name bound to returncode",
        ),
        pytest.param(
            "def f(client: GitClient, field):\n    r = client.run(['status'])\n    return getattr(r, field)  # read\n",
            id="getattr of a name given",
        ),
        *(
            pytest.param(
                f"{imports}def f(client: GitClient):\n    r = client.run(['status'])\n    {read}  # read\n", id=form
            )
            for form, imports, read in STATUS_READS
        ),
        *(
            pytest.param(f"{imports}def f():\n    return {client}.run(['status']).returncode  # read\n", id=form)
            for form, imports, client in STATUS_CLIENTS
        ),
        # "a name is one only in the function that binds it": bound to a name that holds one, or captured by a case.
        pytest.param(
            "def f(client: GitClient):\n    r = client.run(['status'])\n    s = r\n    return s.returncode  # read\n",
            id="a name bound to a held name",
        ),
        pytest.param(
            "def f(client: GitClient):\n    match client.run(['status']):\n        case r:\n"
            "            return r.returncode  # read\n",
            id="a case capturing the result",
        ),
        pytest.param(
            "def f(client: 'GitClient'):\n    return client.run(['status']).returncode  # read\n",
            id="a client annotated in quotes",
        ),
        pytest.param(
            "def f(client: 'GitClient | None'):\n    return client.run(['status']).returncode  # read\n",
            id="a client annotated in a quoted union",
        ),
        # A subclass's own methods run on a client.
        pytest.param(
            "class Mine(GitClient):\n    def f(self):\n        return self.run(['status']).returncode  # read\n",
            id="self in a subclass's method",
        ),
    ],
)
def test_git_status_policy_refuses_every_form_its_paragraph_names(body: str) -> None:
    source = STATUS_IMPORTS + body
    line = next(number for number, text in enumerate(source.splitlines(), 1) if text.endswith("# read"))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {**STATUS_REEXPORTS, "skills/alpha/scripts/alpha.py": source})
        found = python_checks.git_status_problems(root, sorted(root.rglob("*.py")))
    assert [problem.split(" ", 1)[0] for problem in found] == [f"skills/alpha/scripts/alpha.py:{line}"]


# "through every module that imports it again": a module's top level binds a re-export under a try:, an if, an else, or
# a with as it does unindented, and a function annotated to return a GitResult defined there is a producer too.
@pytest.mark.parametrize(
    "support",
    [
        pytest.param(
            "try:\n    from tools.helper import git as git\nexcept ImportError:\n    git = None\n", id="under try"
        ),
        pytest.param(
            "try:\n    import os\nexcept ImportError:\n    pass\nelse:\n    from tools.helper import git as git\n",
            id="under try's else",
        ),
        pytest.param("if True:\n    from tools.helper import git as git\n", id="under if"),
        pytest.param("if False:\n    pass\nelse:\n    from tools.helper import git as git\n", id="under else"),
        pytest.param(
            "import contextlib\n\nwith contextlib.suppress(ImportError):\n    from tools.helper import git as git\n",
            id="under with",
        ),
        pytest.param(
            "from tools import helper\n\ntry:\n    git = helper.git\nexcept AttributeError:\n    raise\n",
            id="an assignment under try",
        ),
        pytest.param(
            "from git_client import GitClient, GitResult\n\nif True:\n\n    def git(cwd) -> GitResult:\n"
            "        return GitClient().run(['status'], directory=cwd)\n",
            id="a producer defined under if",
        ),
    ],
)
def test_git_status_policy_follows_a_re_export_under_a_block(support: str) -> None:
    source = STATUS_IMPORTS + "def f():\n    return git('.').returncode  # read\n"
    line = next(number for number, text in enumerate(source.splitlines(), 1) if text.endswith("# read"))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(
            root,
            {**STATUS_REEXPORTS, "tests/validation/support.py": support, "skills/alpha/scripts/alpha.py": source},
        )
        found = python_checks.git_status_problems(root, sorted(root.rglob("*.py")))
    assert [problem.split(" ", 1)[0] for problem in found] == [f"skills/alpha/scripts/alpha.py:{line}"]


# Forms that read no git result's exit status, each with why.
@pytest.mark.parametrize(
    "body",
    [
        # A name is a result only in the scope that binds it to one: `r` in g is g's parameter, whatever f binds.
        pytest.param(
            "def f(client: GitClient):\n    r = client.run(['status'])\n    return r.stdout\n\n\n"
            "def g(r):\n    return r.returncode\n",
            id="a name another function binds",
        ),
        # getattr of another attribute reads no status.
        pytest.param(
            "def f(client: GitClient):\n    r = client.run(['status'])\n    return getattr(r, 'stdout')\n",
            id="getattr of another attribute",
        ),
        *(
            pytest.param(f"{imports}def f(client: GitClient):\n    r = client.run(['status'])\n    {read}\n", id=form)
            for form, imports, read in (
                ("attrgetter of another attribute", "import operator\n\n\n", "return operator.attrgetter('stdout')(r)"),
                (
                    "a class pattern of another field",
                    "from git_client import GitResult\n\n\n",
                    "match r:\n        case GitResult(stdout=''): return 1",
                ),
                ("vars of another object", "", "return vars(client)"),
            )
        ),
        # A class that subclasses something other than GitClient runs no git.
        pytest.param(
            "class Other(dict):\n    pass\n\n\ndef f():\n    return Other().run(['status']).returncode\n",
            id="a class of another base",
        ),
        # A method returns a result only on an instance of the class that annotates it, not on another object whose
        # method shares its name.
        pytest.param(
            f"{STATUS_CLASS}\n\ndef f(other):\n    return other.produce().returncode\n",
            id="a method of the same name on another object",
        ),
        pytest.param(
            f"{STATUS_CLASS}\n    @staticmethod\n    def f(other):\n        return other.produce().returncode\n",
            id="a static method's first parameter",
        ),
        # The paragraph's named limit: a method of a class another module defines is not followed.
        pytest.param(
            "from tools.repo import Repo\n\n\ndef f():\n    return Repo().produce().returncode\n",
            id="a method of a class another module defines, a named limit",
        ),
        # A tuple target unpacks a result only from a literal tuple or list of as many values; unpacking a GitResult
        # itself raises, since it is no sequence.
        pytest.param(
            "def f(client: GitClient):\n    r, _ = 1, client.run(['status'])\n    return r.returncode\n",
            id="a tuple target whose name takes another value",
        ),
    ],
)
def test_git_status_policy_passes_what_reads_no_git_status(body: str) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(
            root,
            {**STATUS_REEXPORTS, "tools/repo.py": STATUS_CLASS, "skills/alpha/scripts/alpha.py": STATUS_IMPORTS + body},
        )
        assert python_checks.git_status_problems(root, sorted(root.rglob("*.py"))) == []


LIMIT_SOURCE = """\
def listings(client, count):
    client.json(["pr", "list", "--limit", "5"])
    client.json(["pr", "list", f"--limit={count}"])
    client.json(["run", "list", "-L", "5"])
    client.json(["run", "list", "-L5"])
    client.json(["run", "list", f"-L{count}"])
    client.json(["run", "list", "-Lx", "--limits"])
    client.capped_list(["pr", "list"], 5)


def sanctioned(client):
    client.json(["pr", "list", "--limit", "5"])


GH_LIMIT_ALLOWED = {"sanctioned": "a reason", "stale": "a reason"}
"""


def test_gh_limit_policy_refuses_a_limit_passed_by_hand() -> None:
    alpha = "skills/alpha/scripts/alpha.py"
    found = run_claim_policy(
        python_checks.gh_limit_problems, "GH_LIMIT_ALLOWED", LIMIT_SOURCE, python_checks.GH_LIMIT_OWNER
    )
    refusal = (
        "passes gh a --limit by hand, which reads a cut listing as complete; use github_client's capped_list, or "
        "state why in GH_LIMIT_ALLOWED"
    )
    assert found == [
        *(f"{alpha}:{line} {refusal}" for line in (2, 3, 4, 5, 6)),
        f"{alpha}: GH_LIMIT_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
        "tools/broken.py: GH_LIMIT_ALLOWED must map each function, class, or assignment to its reason",
    ]


GIT_SEPARATOR_SOURCE = """\
def listings(client, git_output, root):
    client.output(["diff", "--name-only", "main"])
    client.output(["-C", root, "status", "--porcelain=v2"])
    git_output(root, "ls-files", "--others")
    client.output(("ls-tree", "-r", "HEAD"))
    client.output(["git", "for-each-ref", "--format=%(refname) %(objectname)"])
    client.output(["log", "--format", "%H%x1f%s"])
    client.output(["log", "--pretty=tformat:%H%n%an"])
    client.output(["for-each-ref", "--format=%(refname)%0a%(upstream)"])
    client.output(["diff", "-z", "--name-status", "main"])
    client.names(["ls-files", "--others"])
    client.output(["diff", "--stat", "main"])
    client.output(["status", "--untracked-files=no"])
    client.output(["for-each-ref", "--format=%(refname)%00%(upstream)", "refs/heads"])
    client.output(["log", "-z", "--format=commit %H%x00%s%n"])
    client.output(["log", "--format=%H"])
    client.output(["archive", "--format=tar", "HEAD"])


def splits(client, git, run_tool, root):
    client.output(["rev-parse", "--show-toplevel"]).splitlines()
    listed = client.output(["for-each-ref", "--format=%(refname)", "refs/tags"])
    listed.splitlines()
    result = git(root, "rev-parse", "--git-common-dir")
    result.stdout.splitlines()
    run_tool(["git", "-C", root, "rev-parse", "HEAD"]).output.splitlines()
    arguments = ["--git-dir", root, "rev-parse", "--git-dir"]
    client.output(arguments).splitlines()
    output = result.stdout
    output.splitlines()
    client.output(["rev-parse", "HEAD"]).split("\\n")
    run_tool(["python", "-c", "print()"]).stdout.splitlines()
    root.read_text().splitlines()
    client.output(["archive", "--format=tar", "HEAD"]).splitlines()


def sanctioned(client):
    client.output(["ls-files"])
    client.output(["rev-parse", "HEAD"]).splitlines()


GIT_SEPARATOR_ALLOWED = {"sanctioned": "a reason", "stale": "a reason"}
"""


def test_git_separator_policy_refuses_a_listing_without_z_and_fields_joined_by_a_byte_but_nul() -> None:
    # A call's positional arguments and a list's or a tuple's items start a git command, after git itself and its own
    # options; GitClient.names adds -z, and text before the first field or after the last joins none. splitlines() of
    # a git call's result, its stdout or output, or a name bound to either is refused; a split on a newline, another
    # program's output, and a command that prints no names are not.
    alpha = "skills/alpha/scripts/alpha.py"
    found = run_claim_policy(
        python_checks.git_separator_problems,
        "GIT_SEPARATOR_ALLOWED",
        GIT_SEPARATOR_SOURCE,
        python_checks.GIT_SEPARATOR_OWNER,
    )

    def listing(command: str) -> str:
        return (
            f"lists names with git {command} without -z, so git quotes one holding a non-ASCII byte or a newline; "
            "use GitClient.names, or pass -z and split on NUL, or state why in GIT_SEPARATOR_ALLOWED"
        )

    def joined(command: str, joint: str) -> str:
        return (
            f"joins the fields of a git {command} format with {joint!r}, which a name or a message can hold; join "
            "them with NUL, %00 for for-each-ref or %x00 for log, or state why in GIT_SEPARATOR_ALLOWED"
        )

    def split(command: str) -> str:
        return (
            f"splits the output of git {command} with splitlines(), which also breaks on U+0085, U+2028, and other "
            'characters a ref name or a path can hold; split on "\\n", or on NUL with -z or a %00 format, or state '
            "why in GIT_SEPARATOR_ALLOWED"
        )

    assert found == [
        f"{alpha}:2 {listing('diff')}",
        f"{alpha}:3 {listing('status')}",
        f"{alpha}:4 {listing('ls-files')}",
        f"{alpha}:5 {listing('ls-tree')}",
        f"{alpha}:6 {joined('for-each-ref', ' ')}",
        f"{alpha}:7 {joined('log', '%x1f')}",
        f"{alpha}:8 {joined('log', '%n')}",
        f"{alpha}:9 {joined('for-each-ref', '%0a')}",
        f"{alpha}:21 {split('rev-parse')}",
        f"{alpha}:23 {split('for-each-ref')}",
        f"{alpha}:25 {split('rev-parse')}",
        f"{alpha}:26 {split('rev-parse')}",
        f"{alpha}:28 {split('rev-parse')}",
        f"{alpha}:30 {split('rev-parse')}",
        f"{alpha}: GIT_SEPARATOR_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
        "tools/broken.py: GIT_SEPARATOR_ALLOWED must map each function, class, or assignment to its reason",
    ]


# Each git name listing the separator policy's catalogue, GIT_NAME_LISTINGS, names, without -z and with it.
@pytest.mark.parametrize(
    ("call", "refused"),
    [
        pytest.param('client.output(["diff-tree", "--name-only", "-r", "HEAD"])', True, id="diff-tree --name-only"),
        pytest.param('client.output(["diff-tree", "-r", "HEAD~1", "HEAD"])', True, id="diff-tree raw by default"),
        pytest.param('client.output(["diff-index", "--cached", "HEAD"])', True, id="diff-index raw by default"),
        pytest.param('client.output(["diff-files"])', True, id="diff-files raw by default"),
        pytest.param('client.output(["diff", "--raw", "main"])', True, id="diff --raw"),
        pytest.param('client.output(["diff", "--numstat", "main"])', True, id="diff --numstat"),
        pytest.param('client.output(["log", "--name-status", "main"])', True, id="log --name-status"),
        pytest.param('client.output(["show", "--name-only", "HEAD"])', True, id="show --name-only"),
        pytest.param('client.output(["worktree", "list", "--porcelain"])', True, id="worktree list --porcelain"),
        pytest.param('client.output(["grep", "-n", "pattern"])', True, id="grep"),
        pytest.param('client.output(["check-ignore", "--stdin"])', True, id="check-ignore"),
        pytest.param('client.output(["check-attr", "eol", "--", "a"])', True, id="check-attr"),
        pytest.param('client.output(["diff-tree", "-z", "-r", "HEAD"])', False, id="diff-tree -z"),
        pytest.param('client.output(["worktree", "list", "--porcelain", "-z"])', False, id="worktree list -z"),
        pytest.param('client.output(["grep", "--null", "-n", "pattern"])', False, id="grep --null"),
        pytest.param('client.run(["check-ignore", "-q", "--no-index", "a"])', False, id="check-ignore -q is silent"),
        pytest.param('client.run(["diff-index", "--quiet", "HEAD"])', False, id="diff-index --quiet is silent"),
        pytest.param('client.output(["worktree", "list"])', False, id="worktree list without --porcelain"),
        pytest.param('client.output(["log", "--stat", "main"])', False, id="log --stat"),
        pytest.param('failure(root, "grep", error)', False, id="a label beside an error is no grep"),
    ],
)
def test_git_separator_policy_reads_every_name_listing_its_catalogue_names(call: str, refused: bool) -> None:
    source = f"def listing(client, failure, root, error):\n    {call}\n"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"skills/alpha/scripts/alpha.py": source})
        found = python_checks.git_separator_problems(root, sorted(root.rglob("*.py")))
    assert [problem.split(" ", 1)[0] for problem in found] == (["skills/alpha/scripts/alpha.py:2"] if refused else [])


# The forms the shared call resolution, validation_support.resolved_call, reads for every policy that uses it: a name
# bound to a getattr of a constant name or a functools.partial, a `**` of a literal dict, of a dict() call, or of a name
# bound to one, `builtins.X`, an assignment expression, a tuple target, and a with target. Each source marks the line
# its policy refuses with `# read`, or marks none where it passes.
RESOLVED_IMPORTS = (
    "import builtins\nimport ctypes\nimport functools\nimport json\nimport operator\nfrom ctypes import wintypes\n\n\n"
)


def resolved_rows(policy: Callable[[Path, list[Path]], list[str]], body: str) -> list[str]:
    """The lines a policy refuses in a skill's module holding body after RESOLVED_IMPORTS, by their numbers."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"skills/alpha/scripts/alpha.py": RESOLVED_IMPORTS + body})
        found = policy(root, sorted(root.rglob("*.py")))
    return sorted({problem.split(" ", 1)[0].rpartition(":")[2] for problem in found})


def marked_rows(body: str) -> list[str]:
    """The numbers of the lines a body marks `# read`, counted after RESOLVED_IMPORTS."""
    source = RESOLVED_IMPORTS + body
    return sorted(str(number) for number, line in enumerate(source.splitlines(), 1) if line.endswith("# read"))


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            "def f(p):\n    add = functools.partial(p.add_argument, type=float)\n    add('--timeout')  # read\n",
            id="a partial bound add_argument",
        ),
        pytest.param("def f(p):\n    p.add_argument('--timeout', **{'type': float})  # read\n", id="a ** literal"),
        pytest.param(
            "def f(p):\n    options = {'type': float}\n    p.add_argument('--timeout', **options)  # read\n",
            id="a ** name bound to a literal",
        ),
        pytest.param("def f(p):\n    p.add_argument('--timeout', **dict(type=int))  # read\n", id="a ** dict()"),
        pytest.param("def f(p):\n    p.add_argument('--timeout', type=builtins.float)  # read\n", id="builtins"),
        pytest.param(
            "def f(p):\n    add = getattr(p, 'add_argument')\n    add('--timeout', type=float)  # read\n",
            id="a getattr bound to a name",
        ),
        pytest.param("def f(p):\n    p.add_argument('--sleeptime', type=float)  # read\n", id="sleeptime"),
        pytest.param("def f(p):\n    p.add_argument('--waitSeconds', type=int)  # read\n", id="waitSeconds"),
        pytest.param("def f(p):\n    p.add_argument('--retryDelayMs', type=int)  # read\n", id="retryDelayMs"),
        pytest.param("def f(p):\n    p.add_argument('--times', type=int)\n", id="times counts repetitions"),
        pytest.param("def f(p):\n    p.add_argument('--timestamp', type=int)\n", id="a timestamp is no limit"),
        pytest.param("def f(p, options):\n    p.add_argument('--timeout', **options)\n", id="an unread ** passes"),
        # Any binding that may reach the read counts, a global's and a nonlocal's from their owner.
        pytest.param(
            "def f(p, q):\n    add = q.add_argument\n    add = p.add_argument\n"
            "    add('--timeout', type=float)  # read\n",
            id="add rebound in one scope",
        ),
        pytest.param(
            "def f(p):\n    kind = str\n    kind = float\n    p.add_argument('--timeout', type=kind)  # read\n",
            id="a type rebound in one scope",
        ),
        pytest.param(
            "def f(p):\n    kind = float\n    kind = str\n    p.add_argument('--timeout', type=kind)\n",
            id="a type rebound to str passes",
        ),
        pytest.param(
            "add = None\n\n\ndef setup(p):\n    global add\n    add = p.add_argument\n\n\n"
            "def f():\n    add('--timeout', type=float)  # read\n",
            id="a global add",
        ),
        pytest.param(
            "def f(p):\n    options = {}\n    options['type'] = float\n"
            "    p.add_argument('--timeout', **options)  # read\n",
            id="a ** dict given a key after binding",
        ),
        pytest.param(
            "def f(p):\n    options = {}\n    options.update(type=float)\n"
            "    p.add_argument('--timeout', **options)  # read\n",
            id="a ** dict updated after binding",
        ),
        pytest.param(
            "def f(p, other):\n    options = {}\n    options.update(other)\n"
            "    p.add_argument('--timeout', **options)\n",
            id="a ** dict updated from another mapping is unread",
        ),
        pytest.param(
            "def f(p):\n    operator.attrgetter('add_argument')(p)('--timeout', type=float)  # read\n",
            id="an attrgetter of add_argument",
        ),
        pytest.param(
            "def f(p):\n    options = {}\n    options['type'] = float\n    options = {}\n"
            "    p.add_argument('--timeout', **options)\n",
            id="a key added before the dict is bound again passes",
        ),
        pytest.param(
            "def f(p):\n    kind = str\n    for _ in range(2):\n"
            "        p.add_argument('--timeout', type=kind)  # read\n        kind = float\n",
            id="a type a loop binds after the read",
        ),
    ],
)
def test_time_option_policy_reads_each_form_resolved_call_follows(body: str) -> None:
    assert resolved_rows(python_checks.time_option_problems, body) == marked_rows(body)


# Each word of the time-word catalogue, as an option of its own, is a time.
@pytest.mark.parametrize("word", sorted(python_checks.TIME_WORDS))
def test_time_option_policy_reads_every_time_word(word: str) -> None:
    body = f"def f(p):\n    p.add_argument('--{word}', type=float)  # read\n"
    assert resolved_rows(python_checks.time_option_problems, body) == marked_rows(body)


def test_time_words_hold_each_limit_word_and_unit() -> None:
    # Pinned against literals, not the constant under test.
    assert {
        "ttl",
        "duration",
        "backoff",
        "sec",
        "secs",
        "millis",
        "msec",
        "millisecond",
        "usec",
        "microseconds",
        "mins",
        "hrs",
    } <= python_checks.TIME_WORDS


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("def f(s, d):\n    getattr(s, 'write')(json.dumps(d))  # read\n", id="a getattr write"),
        pytest.param(
            "def f(path, d):\n    dumps = getattr(json, 'dumps')\n    path.write_text(dumps(d))  # read\n",
            id="a getattr dumps",
        ),
        pytest.param(
            "def f(path, d):\n    dumps = functools.partial(json.dumps, indent=2)\n"
            "    path.write_text(dumps(d))  # read\n",
            id="a partial dumps",
        ),
        pytest.param(
            "class Store:\n    def __init__(self, d):\n        self.text = json.dumps(d)\n\n"
            "    def save(self, path):\n        path.write_text(self.text)  # read\n",
            id="JSON text on an attribute",
        ),
        pytest.param(
            "class Store:\n    def __init__(self):\n        self.encoder = json.JSONEncoder()\n\n"
            "    def save(self, path, d):\n        path.write_text(self.encoder.encode(d))  # read\n",
            id="an encoder on an attribute",
        ),
        pytest.param(
            "class Mine(json.JSONEncoder):\n    pass\n\n\ndef f(path, d):\n"
            "    path.write_text(Mine().encode(d))  # read\n",
            id="a JSONEncoder subclass",
        ),
        pytest.param(
            "TEXT = json.dumps({'a': 1})\n\n\ndef f(path):\n    path.write_text(TEXT)  # read\n",
            id="module-level text written in a function",
        ),
        pytest.param(
            "def f(path, d):\n    path.write_text(json.JSONEncoder.encode(json.JSONEncoder(), d))  # read\n",
            id="an unbound encode",
        ),
        pytest.param(
            "def f(path, d):\n    write = functools.partial(path.write_text, encoding='utf-8')\n"
            "    write(json.dumps(d))  # read\n",
            id="a partial write",
        ),
        pytest.param(
            "class Store:\n    def __init__(self, d):\n        self.text = 'plain'\n\n"
            "    def save(self, path):\n        path.write_text(self.text)\n",
            id="plain text on an attribute passes",
        ),
        pytest.param(
            "class A:\n    def __init__(self, d):\n        self.text = json.dumps(d)\n\n\n"
            "class B:\n    def __init__(self, t):\n        self.text = t\n\n"
            "    def save(self, s):\n        s.write(self.text)\n",
            id="another class's attribute of the same name passes",
        ),
        pytest.param(
            "def f(path, d, pretty):\n    if pretty:\n        dumps = functools.partial(json.dumps, indent=2)\n"
            "    else:\n        dumps = json.dumps\n    path.write_text(dumps(d))  # read\n",
            id="dumps bound in both branches",
        ),
        pytest.param(
            "dumps = None\n\n\ndef load():\n    global dumps\n    dumps = json.dumps\n\n\n"
            "def f(path, d):\n    path.write_text(dumps(d))  # read\n",
            id="a global dumps",
        ),
        pytest.param(
            "class C:\n    TEXT = json.dumps({})\n\n    def save(self, s):\n        s.write(self.TEXT)  # read\n"
            "        s.write(C.TEXT)  # read\n",
            id="class-level text through self and the class",
        ),
        pytest.param(
            "def f(path, d):\n    path.write_text(json.encoder.JSONEncoder().encode(d))  # read\n",
            id="json.encoder.JSONEncoder",
        ),
        pytest.param(
            "from json import JSONEncoder\n\n\ndef f(path, d):\n    path.write_text(JSONEncoder().encode(d))  # read\n",
            id="a from-imported JSONEncoder",
        ),
        pytest.param(
            "def f(stream, d):\n    print(json.dumps(d), file=stream)  # read\n    print(json.dumps(d), file=None)\n",
            id="a print to a stream, and to None",
        ),
        pytest.param(
            "import pkg.atomic_file\n\n\ndef f(path, d):\n"
            "    pkg.atomic_file.replace_text(path, json.dumps(d))  # read\n",
            id="a file writer reached through a dotted package",
        ),
        pytest.param(
            "T = None\n\n\nclass A:\n    def m(self):\n        global T\n        T = self.text\n\n"
            "    def __init__(self, d):\n        self.text = json.dumps(d)\n\n\n"
            "class B:\n    def n(self, s):\n        s.write(T)  # read\n",
            id="text another class's attribute gave a global",
        ),
        pytest.param(
            "class Mine(json.JSONEncoder):\n    pass\n\n\ndef f(path, d):\n"
            "    path.write_text(json.JSONEncoder().encode(d))  # read\n",
            id="JSONEncoder itself beside a subclass",
        ),
        pytest.param(
            "class Store:\n    def __init__(self, d):\n        self.text = json.dumps(d)\n\n"
            "    def save(self, path):\n        text = self.text\n        path.write_text(text)  # read\n",
            id="an attribute's text through a local name",
        ),
        pytest.param(
            "class Store:\n    def first(self):\n        self.b = self.a\n\n"
            "    def second(self, d):\n        self.a = json.dumps(d)\n\n"
            "    def save(self, s):\n        s.write(self.b)  # read\n",
            id="text an attribute bound later gives",
        ),
        pytest.param(
            "class Store:\n    def first(self):\n        self.enc = self.base\n\n"
            "    def second(self):\n        self.base = json.JSONEncoder()\n\n"
            "    def save(self, s, d):\n        s.write(self.enc.encode(d))  # read\n",
            id="an encoder an attribute bound later gives",
        ),
        pytest.param(
            "def f(s):\n    t = json.dumps({})\n    t = 'x'\n    s.write(t)\n",
            id="text bound again to plain text passes",
        ),
        pytest.param(
            "def f(s, d):\n    t = ''\n    for _ in range(2):\n        s.write(t)  # read\n        t = json.dumps(d)\n",
            id="text a loop binds after the write",
        ),
        pytest.param(
            "def f(s, d):\n    for _ in range(2):\n        t = 'x'\n        s.write(t)\n        t = json.dumps(d)\n",
            id="text a loop binds after the write but before it again passes",
        ),
    ],
)
def test_json_write_policy_reads_each_form_resolved_call_follows(body: str) -> None:
    assert resolved_rows(python_checks.json_write_problems, body) == marked_rows(body)


DECLARED = "    lib.CloseHandle.argtypes = [wintypes.HANDLE]\n    lib.CloseHandle.restype = wintypes.BOOL\n"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            f"def a(h):\n    lib = ctypes.WinDLL('user32')\n{DECLARED}    return lib.CloseHandle(h)\n\n\n"
            "def b(h):\n    lib = ctypes.WinDLL('kernel32')\n    return lib.CloseHandle(h)  # read\n",
            id="one name for two libraries in two functions",
        ),
        pytest.param(
            f"def a(h, name):\n    lib = ctypes.WinDLL(name)\n{DECLARED}    return lib.CloseHandle(h)\n\n\n"
            "def b(h, name):\n    lib = ctypes.WinDLL(name)\n    return lib.CloseHandle(h)  # read\n",
            id="one load's text in two functions",
        ),
        pytest.param(
            "def f(h):\n    if (lib := ctypes.WinDLL('k')):\n        lib.CloseHandle(h)  # read\n",
            id="an assignment expression",
        ),
        pytest.param(
            "def f(h):\n    lib, other = ctypes.WinDLL('k'), None\n    lib.CloseHandle(h)  # read\n",
            id="a tuple target",
        ),
        pytest.param(
            "def f(h):\n    with ctypes.WinDLL('k') as lib:\n        lib.CloseHandle(h)  # read\n", id="a with target"
        ),
        pytest.param("def f(h):\n    getattr(ctypes.windll, 'kernel32').CloseHandle(h)  # read\n", id="a getattr lib"),
        pytest.param("def f(h):\n    getattr(ctypes, 'WinDLL')('k').CloseHandle(h)  # read\n", id="a getattr loader"),
        pytest.param("def f(o):\n    ctypes.pythonapi.Py_IncRef(o)  # read\n", id="pythonapi"),
        pytest.param("def f(h):\n    ctypes.pydll.python3.Close(h)  # read\n", id="pydll"),
        pytest.param("def f(h):\n    ctypes.oledll.LoadLibrary('x').Close(h)  # read\n", id="oledll.LoadLibrary"),
        pytest.param(
            "def f(h):\n    lib = ctypes.WinDLL('k')\n"
            "    lib.Close.argtypes = lib.Open.argtypes = [wintypes.HANDLE]\n"
            "    lib.Close.restype = lib.Open.restype = wintypes.BOOL\n    lib.Close(h)\n    lib.Open(h)\n",
            id="a chained declaration passes",
        ),
        pytest.param(
            "def f(h):\n    lib = ctypes.WinDLL('k')\n"
            "    lib.Close.argtypes, lib.Close.restype = [wintypes.HANDLE], wintypes.BOOL\n    lib.Close(h)\n",
            id="an unpacked declaration passes",
        ),
        pytest.param(
            "def f(h):\n    try:\n        lib = ctypes.WinDLL('kernel32')\n    except OSError:\n"
            "        lib = ctypes.CDLL('libc.so.6')\n    lib.CloseHandle(h)  # read\n",
            id="a library bound in a try and its handler",
        ),
        pytest.param(
            "def f(h, windows):\n    if windows:\n        lib = ctypes.WinDLL('kernel32')\n    else:\n"
            "        lib = ctypes.CDLL('libc.so.6')\n    lib.CloseHandle(h)  # read\n",
            id="a library bound in both arms",
        ),
        pytest.param(
            "lib = None\n\n\ndef load():\n    global lib\n    lib = ctypes.WinDLL('kernel32')\n\n\n"
            "def use(h):\n    lib.CloseHandle(h)  # read\n",
            id="a global library",
        ),
        pytest.param(
            "def outer(h):\n    lib = None\n\n    def inner():\n        nonlocal lib\n"
            "        lib = ctypes.WinDLL('kernel32')\n\n    inner()\n    lib.CloseHandle(h)  # read\n",
            id="a nonlocal library",
        ),
        pytest.param(
            f"def f(h):\n    lib = ctypes.WinDLL('user32')\n{DECLARED}    lib = ctypes.WinDLL('kernel32')\n"
            "    lib.CloseHandle(h)  # read\n",
            id="a library rebound in one scope",
        ),
        pytest.param(
            "def f(h):\n    try:\n        lib = ctypes.WinDLL('kernel32')\n    except OSError:\n"
            f"        lib = ctypes.CDLL('libc.so.6')\n{DECLARED}    lib.CloseHandle(h)\n",
            id="a declaration after a try and its handler passes",
        ),
        pytest.param(
            "class K:\n    lib = ctypes.WinDLL('k')\n\n    def f(self, h):\n        self.lib.CloseHandle(h)  # read\n"
            "        K.lib.CloseHandle(h)  # read\n",
            id="a class-level library through self and the class",
        ),
        pytest.param(
            "class K:\n    def b(self):\n        self.lib = ctypes.WinDLL('kernel32')\n\n"
            "    def a(self):\n        user32 = ctypes.WinDLL('user32')\n"
            "        user32.F.argtypes = [wintypes.HANDLE]\n        user32.F.restype = wintypes.BOOL\n"
            "        self.lib = user32\n\n    def c(self, h):\n        self.lib.F(h)  # read\n",
            id="a place bound to two libraries counts both",
        ),
        pytest.param(
            "def f():\n    lib = ctypes.WinDLL('k')\n    g = functools.partial(lib.CloseHandle, 1)\n    g()  # read\n",
            id="a partial of a foreign function bound to a name",
        ),
        pytest.param(
            "def f():\n    lib = ctypes.WinDLL('k')\n    functools.partial(lib.CloseHandle, 1)()  # read\n",
            id="a partial of a foreign function called in place",
        ),
        pytest.param(
            "def f():\n    lib = ctypes.WinDLL('k')\n"
            "    functools.partial(operator.attrgetter('CloseHandle')(lib), 1)()  # read\n",
            id="a partial over an attrgetter of a foreign function",
        ),
        pytest.param(
            "def f(h):\n    lib = None\n    for _ in range(2):\n        if lib:\n"
            "            lib.CloseHandle(h)  # read\n        lib = ctypes.WinDLL('k')\n",
            id="a library a loop binds after the call",
        ),
        pytest.param(
            "if True:\n\n    def load():\n        return ctypes.WinDLL('k')\n\n\n"
            "def f(h):\n    load().CloseHandle(h)  # read\n",
            id="a loader defined under a top-level block",
        ),
    ],
)
def test_ctypes_policy_reads_each_form_resolved_call_follows(body: str) -> None:
    assert resolved_rows(python_checks.ctypes_declaration_problems, body) == marked_rows(body)


# Each name listing GIT_NAME_LISTINGS adds is also a command whose output the splitlines() check reads, -z or not.
@pytest.mark.parametrize("command", ["diff-tree", "diff-index", "diff-files", "check-ignore", "check-attr"])
def test_git_separator_policy_reads_a_split_of_each_listing_command(command: str) -> None:
    source = f"def split(client):\n    client.output([{command!r}, '-z', 'HEAD']).splitlines()\n"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"skills/alpha/scripts/alpha.py": source})
        found = python_checks.git_separator_problems(root, sorted(root.rglob("*.py")))
    assert [problem.split(" ", 1)[0] for problem in found] == ["skills/alpha/scripts/alpha.py:2"]
    assert f"splits the output of git {command} with splitlines()" in found[0]


TIME_OPTION_SOURCE = """\
import argparse

from seconds_option import positive_seconds


def parser():
    result = argparse.ArgumentParser()
    result.add_argument("--timeout", type=float, default=30.0)
    result.add_argument("--read-timeout", "-r", type=float)
    result.add_argument("--wait", type=float, metavar="SECONDS")
    result.add_argument("limit", type=float, dest="limit_minutes")
    result.add_argument_group("g").add_argument("--max_hours", type=float)
    result.add_argument("--timeout-ok", type=positive_seconds)
    result.add_argument("--ratio", type=float)
    result.add_argument("--attempts", type=int)
    result.add_argument("--seconds-listed", type=str)
    result.add_argument("--delay", type=float)
    result.add_argument("--interval", type=float)
    result.add_argument("--time-limit", type=float)
    result.add_argument("--max-time", type=float)
    result.add_argument("--timeout", type=int)
    add = result.add_argument
    add("--retry-wait", type=float)
    getattr(result, "add_argument")("--poll-ms", type=int)
    result.add_argument("--limit", type=int)
    result.add_argument("--min-count", type=int)
    result.add_argument("--days", type=int)
    result.add_argument("--times", type=int)
    result.add_argument("--wait-ok", type=positive_seconds)
    return result


def sanctioned(result):
    result.add_argument("--seconds", type=float)


TIME_OPTION_ALLOWED = {"sanctioned": "a reason", "stale": "a reason"}
"""


def test_time_option_policy_refuses_a_float_time_option() -> None:
    alpha = "skills/alpha/scripts/alpha.py"
    found = run_claim_policy(python_checks.time_option_problems, "TIME_OPTION_ALLOWED", TIME_OPTION_SOURCE, alpha)
    tail = "; use ejryan89-skill-core's seconds_option.positive_seconds, or state why in TIME_OPTION_ALLOWED"
    remedy = f"with type=float, which accepts nan, inf, 0, and a negative number{tail}"
    whole = f"with type=int, which accepts 0 and a negative number{tail}"
    assert found == [
        f"{alpha}:8 reads --timeout {remedy}",
        f"{alpha}:9 reads --read-timeout {remedy}",
        f"{alpha}:10 reads --wait {remedy}",
        f"{alpha}:11 reads limit_minutes {remedy}",
        f"{alpha}:12 reads --max_hours {remedy}",
        f"{alpha}:17 reads --delay {remedy}",
        f"{alpha}:18 reads --interval {remedy}",
        f"{alpha}:19 reads --time-limit {remedy}",
        f"{alpha}:20 reads --max-time {remedy}",
        f"{alpha}:21 reads --timeout {whole}",
        f"{alpha}:23 reads --retry-wait {remedy}",
        f"{alpha}:24 reads --poll-ms {whole}",
        f"{alpha}: TIME_OPTION_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
        "tools/broken.py: TIME_OPTION_ALLOWED must map each function, class, or assignment to its reason",
    ]


def content_errors_beside_the_fixture_reader(root: Path, files: list[Path]) -> list[str]:
    """The content-errors policy over a tree whose cores hold the fixture reader alone, so it names no core family."""
    return python_checks.content_error_problems(root, files, {})


# Each claim policy's forms its paragraph in "Validation" in docs/adding-a-skill.md names, beyond the plain one: a
# module in a skill's scripts beside ejryan89-skill-core's fixture reader, whose line marked `# read` is refused.
CLAIM_FORMS: list[tuple[str, Callable[[Path, list[Path]], list[str]], str]] = [
    # "directly or through a repository function that lets the reader's errors escape"
    (
        "a reader bound to a module-level name",
        content_errors_beside_the_fixture_reader,
        "import reader\n\nREAD = reader.read\n\n\ndef f(text):\n    try:  # read\n        return READ(text)\n"
        "    except OSError:\n        return None\n",
    ),
    (
        "a reader called through a module-level lambda",
        content_errors_beside_the_fixture_reader,
        "import reader\n\nREAD = lambda text: reader.read(text)\n\n\ndef f(text):\n    try:  # read\n"
        "        return READ(text)\n    except OSError:\n        return None\n",
    ),
    (
        "a reader called through a method of a class in the module",
        content_errors_beside_the_fixture_reader,
        "import reader\n\n\nclass Loader:\n    def load(self, text):\n        return reader.read(text)\n\n\n"
        "def f(text):\n    try:  # read\n        return Loader().load(text)\n"
        "    except OSError:\n        return None\n",
    ),
    # "every process the repository starts runs without git's repository-selecting variables"
    *(
        (f"a start through {start}", python_checks.process_environment_problems, f"import os\n\n\n{call}  # read\n")
        for start, call in (
            ("os.execve", "os.execve('x', ['x'], dict(os.environ))"),
            ("os.execle", "os.execle('x', 'x', dict(os.environ))"),
            ("os.spawnve", "os.spawnve(os.P_WAIT, 'x', ['x'], dict(os.environ))"),
            ("os.posix_spawn", "os.posix_spawn('x', ['x'], dict(os.environ))"),
            ("os.system", "os.system('x')"),
            ("an event loop's subprocess_exec", "loop.subprocess_exec(object, 'x', env=dict(os.environ))"),
        )
    ),
    # "a call that opens, reads, or writes text without an encoding="
    *(
        (f"text through {opener}", python_checks.encoding_problems, f"{imports}\n\n\n{call}  # read\n")
        for opener, imports, call in (
            ("codecs.open", "import codecs", "codecs.open('p').read()"),
            ("builtins.open", "import builtins", "builtins.open('p').read()"),
            ("os.popen", "import os", "os.popen('git status').read()"),
            ("subprocess.getoutput", "import subprocess", "subprocess.getoutput('git status')"),
            # "a path's read_text, write_text, or open in a text mode", named on the class with the path first, where
            # the path's slot is no encoding.
            ("Path.read_text unbound", "from pathlib import Path", "Path.read_text(p)"),
            ("Path.read_text unbound given the path's slot", "from pathlib import Path", "Path.read_text(p, None)"),
            ("pathlib.Path.write_text unbound", "import pathlib", "pathlib.Path.write_text(p, s)"),
            ("Path.open unbound", "from pathlib import Path", "Path.open(p, 'w')"),
            ("Path.open unbound in a text mode", "from pathlib import Path", "Path.open(p, 'r', -1)"),
            ("zipfile.Path.read_text unbound", "import zipfile", "zipfile.Path.read_text(p)"),
            # zipfile.Path.open takes its encoding first after the mode, a slot earlier than pathlib's.
            ("zipfile.Path.open unbound", "import zipfile", "zipfile.Path.open(p, 'r')"),
            ("zipfile.Path.open bound", "import zipfile", "zipfile.Path(z).open('r')"),
            # On a receiver that may be either class, a buffering size at zipfile's slot names no encoding.
            ("a path's open given a buffering size", "import pathlib", "pathlib.Path(p).open('r', -1)"),
            ("an open on any receiver given a buffering size", "import os", "p.open('r', -1)"),
            # subprocess opens its pipes in text when given errors=, as it does for text= or universal_newlines=.
            *(
                (f"subprocess.{start} given errors=", "import subprocess", f"subprocess.{start}(a, errors='strict')")
                for start in ("run", "Popen", "check_output")
            ),
        )
    ),
    # a write that takes json.dumps text "through a name its function binds to it"
    (
        "a print to a stream",
        python_checks.json_write_problems,
        "import json\n\n\ndef f(d, fh):\n    print(json.dumps(d), file=fh)  # read\n",
    ),
    (
        "text an augmented assignment adds to",
        python_checks.json_write_problems,
        "import json\n\n\ndef f(d, p):\n    text = ''\n    text += json.dumps(d)\n"
        "    p.write_text(text, encoding='utf-8')  # read\n",
    ),
    # "a use of json.loads"
    (
        "json.loads through getattr",
        python_checks.json_parse_problems,
        "import json\n\ngetattr(json, 'loads')('1')  # read\n",
    ),
    # a one-argument int() "that converts a regular-expression group"
    (
        "a stripped group",
        python_checks.int_parse_problems,
        "import re\n\n\ndef f(text):\n    m = re.match('(.*)', text)\n    return int(m.group(1).strip())  # read\n",
    ),
    (
        "groups mapped through int",
        python_checks.int_parse_problems,
        "import re\n\n\ndef f(text):\n    m = re.match('(.*)', text)\n    return list(map(int, m.groups()))  # read\n",
    ),
]


def run_claim_form(policy: Callable[[Path, list[Path]], list[str]], source: str) -> list[str]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(
            root,
            {
                "skills/ejryan89-skill-core/scripts/reader.py": CONTENT_READER_SOURCE,
                "skills/alpha/scripts/alpha.py": source,
            },
        )
        return policy(root, sorted(root.rglob("*.py")))


@pytest.mark.parametrize(("policy", "source"), [pytest.param(*form[1:], id=form[0]) for form in CLAIM_FORMS])
def test_each_claim_policy_refuses_every_form_its_paragraph_names(
    policy: Callable[[Path, list[Path]], list[str]], source: str
) -> None:
    line = next(number for number, text in enumerate(source.splitlines(), 1) if text.endswith("# read"))
    found = run_claim_form(policy, source)
    assert [problem.split(" ", 1)[0] for problem in found] == [f"skills/alpha/scripts/alpha.py:{line}"], found


# Forms the same policies pass, each with why.
CLAIM_PASSES: list[tuple[str, Callable[[Path, list[Path]], list[str]], str]] = [
    # A start given a cleared environment, at its own position, passes like subprocess.run's env=.
    (
        "os.execve given a cleared environment",
        python_checks.process_environment_problems,
        "import os\n\nfrom bounded_process import without_repository_variables\n\n\n"
        "os.execve('x', ['x'], without_repository_variables(os.environ))\n",
    ),
    # bounded_process clears the variables from whatever environment it is given.
    (
        "ejryan89-skill-core's runner",
        python_checks.process_environment_problems,
        "import os\n\nfrom bounded_process import run_bounded\n\n\nrun_bounded(['x'], 60, env=dict(os.environ))\n",
    ),
    # subprocess.getoutput and codecs.open each take an encoding.
    (
        "subprocess.getoutput with an encoding",
        python_checks.encoding_problems,
        "import subprocess\n\n\nsubprocess.getoutput('x', encoding='utf-8')\n",
    ),
    (
        "codecs.open with an encoding",
        python_checks.encoding_problems,
        "import codecs\n\n\ncodecs.open('p', encoding='utf-8').read()\n",
    ),
    # A path method named on its class takes its encoding one slot after the path, and a binary open needs none.
    *(
        (f"{call} unbound with its encoding", python_checks.encoding_problems, f"{imports}\n\n\n{call}\n")
        for imports, call in (
            ("from pathlib import Path", "Path.read_text(p, 'utf-8')"),
            ("import pathlib", "pathlib.Path.write_text(p, s, 'utf-8')"),
            ("from pathlib import Path", "Path.open(p, 'w', -1, 'utf-8')"),
            ("from pathlib import Path", "Path.open(p, 'rb')"),
        )
    ),
    # zipfile.Path.open names its encoding first after the mode, bound or unbound, and so does a pathlib path's open
    # at its own slot.
    *(
        (f"{call} with its encoding", python_checks.encoding_problems, f"{imports}\n\n\n{call}\n")
        for imports, call in (
            ("import zipfile", "zipfile.Path(z).open('r', 'utf-8')"),
            ("import zipfile", "zipfile.Path.open(p, 'r', 'utf-8')"),
            ("import zipfile", "zipfile.Path(z).open('r', encoding='utf-8')"),
            ("import pathlib", "pathlib.Path(p).open('r', -1, 'utf-8')"),
            ("import subprocess", "subprocess.run(a, errors='strict', encoding='utf-8')"),
            # An errors= that is empty or None leaves the pipes in bytes.
            ("import subprocess", "subprocess.run(a, errors=None)"),
            # fileinput hands each file to a hook that names its own encoding.
            ("import fileinput", "fileinput.input(f, openhook=fileinput.hook_encoded('utf-8'))"),
            ("import fileinput", "fileinput.input(f, mode='rb')"),
            ("import logging", "logging.FileHandler(p, 'a', 'utf-8')"),
        )
    ),
    # A print with no file= writes to standard output, the script's report, which is no file or stream it names.
    (
        "a print to standard output",
        python_checks.json_write_problems,
        "import json\n\n\ndef f(d):\n    print(json.dumps(d))\n",
    ),
    # getattr of another attribute reads no parser.
    (
        "getattr of json.dumps",
        python_checks.json_parse_problems,
        "import json\n\ngetattr(json, 'dumps')(1)\n",
    ),
]


@pytest.mark.parametrize(("policy", "source"), [pytest.param(*form[1:], id=form[0]) for form in CLAIM_PASSES])
def test_each_claim_policy_passes_the_forms_its_paragraph_leaves_out(
    policy: Callable[[Path, list[Path]], list[str]], source: str
) -> None:
    assert run_claim_form(policy, source) == []


def _opener_call(name: str, opener: TextOpener, encoding: str) -> str:
    """A module calling a TEXT_OPENERS entry in a text mode, with what else it is given."""
    module = name.rpartition(".")[0]
    imports = f"import {module}\n\n\n" if module else ""
    mode = "mode='wt'" if opener.default_mode else ""
    arguments = ", ".join(part for part in (mode, encoding) if part)
    return f"{imports}{name}({arguments})  # read\n"


# Every text opener the table names, in a text mode: refused without an encoding, and passed with one.
@pytest.mark.parametrize("name", sorted(TEXT_OPENERS))
def test_the_encoding_policy_reads_every_text_opener_in_the_table(name: str) -> None:
    opener = TEXT_OPENERS[name]
    found = run_claim_form(python_checks.encoding_problems, _opener_call(name, opener, ""))
    line = 4 if "." in name else 1
    assert [problem.split(" ", 1)[0] for problem in found] == [f"skills/alpha/scripts/alpha.py:{line}"], found
    for keyword in opener.encoding_keywords:
        source = _opener_call(name, opener, f"{keyword}=e")
        assert run_claim_form(python_checks.encoding_problems, source) == [], (name, keyword)


# The forms "A failure is never read as absence" names in its Search for, and the paragraph in docs/adding-a-skill.md
# that names what the presence policy refuses, for every presence predicate, each a module the policy must refuse: a
# predicate's False returned as an empty answer, skipped past with a continue, walked past with a while, chosen against
# an empty value, returned, or kept or dropped by a comprehension or a filter.
PRESENCE_PREDICATE_CALLS = {
    "exists": "p.exists()",
    "is_file": "p.is_file()",
    "is_dir": "p.is_dir()",
    "is_mount": "p.is_mount()",
    "is_socket": "p.is_socket()",
    "is_fifo": "p.is_fifo()",
    "is_block_device": "p.is_block_device()",
    "is_char_device": "p.is_char_device()",
    "os.path.exists": "os.path.exists(p)",
    "os.path.lexists": "os.path.lexists(p)",
    "os.path.isfile": "os.path.isfile(p)",
    "os.path.isdir": "os.path.isdir(p)",
    "os.path.ismount": "os.path.ismount(p)",
    "os.access": "os.access(p, os.F_OK)",
}
PRESENCE_EMPTIES_OF_EMPTIES = ("None, None", "(None, '')", "[None, 0]", "[[], {}]")
PRESENCE_EMPTIES = ("", "None", "False", "0", '""', 'b""', "[]", "()", "{}", "set()", "frozenset()", "dict()", "list()")
PRESENCE_SHAPES = {
    "if not P: return E": "def f(p):\n    if not {call}:\n        return {empty}\n    return p.read_text()\n",
    "if not P or Q: return E": "def f(p, q):\n    if q or not {call}:\n        return {empty}\n    return 1\n",
    "if P: ... else: return E": (
        "def f(p):\n    if {call}:\n        x = 1\n    else:\n        return {empty}\n    return x\n"
    ),
    "if not P: continue": (
        "def f(ps):\n    for p in ps:\n        if not {call}:\n            continue\n        print(p)\n"
    ),
    "while not P": "def f(p):\n    while not {call}:\n        p = p.parent\n    return p\n",
    "if not P: break": ("def f(ps):\n    for p in ps:\n        if not {call}:\n            break\n        print(p)\n"),
    "if not P: print, return E": (
        "def f(p):\n    if not {call}:\n        print('missing', p)\n        return {empty}\n    return 1\n"
    ),
    "if not P: log, continue": (
        "import logging\n\n\ndef f(ps):\n    for p in ps:\n        if not {call}:\n"
        "            logging.warning('missing %s', p)\n            continue\n        print(p)\n"
    ),
    "if not P: warnings.warn, return E": (
        "import warnings\n\n\ndef f(p):\n    if not {call}:\n        warnings.warn('missing')\n        return {empty}\n"
        "    return 1\n"
    ),
    "if not P: sys.stderr.write, return E": (
        "import sys\n\n\ndef f(p):\n    if not {call}:\n        sys.stderr.write('missing')\n        return {empty}\n"
        "    return 1\n"
    ),
    "if not P: sys.stdout.write, continue": (
        "import sys\n\n\ndef f(ps):\n    for p in ps:\n        if not {call}:\n"
        "            sys.stdout.write('skipped')\n            continue\n        print(p)\n"
    ),
    "if P: ... else: log, return E": (
        "def f(p, log):\n    if {call}:\n        return 1\n    else:\n        log.info('gone')\n        pass\n"
        "        return {empty}\n"
    ),
    "if not (P and Q): continue": (
        "def f(ps, q):\n    for p in ps:\n        if not ({call} and q):\n            continue\n        print(p)\n"
    ),
    "if not (Q or P): return E": "def f(p, q):\n    if not (q or {call}):\n        return {empty}\n    return 1\n",
    "if P: return V, then return E": "def f(p):\n    if {call}:\n        return p.read_text()\n    return {empty}\n",
    "if P: return V at the function's end": "def f(p):\n    if {call}:\n        return p.read_text()\n",
    "if P: return V in a loop, then return E": (
        "def f(ps):\n    for p in ps:\n        if {call}:\n            return p\n    return {empty}\n"
    ),
    "if P: return V in a with at the function's end": (
        "def f(p, lock):\n    with lock:\n        if {call}:\n            return p\n"
    ),
    "V if P else E": "def f(p):\n    return p.read_text() if {call} else {empty}\n",
    "V if P and Q else E": "def f(p, q):\n    return p.read_text() if q and {call} else {empty}\n",
    "E if not P else V": "def f(p):\n    return {empty} if not {call} else p.read_text()\n",
    "return P": "def f(p):\n    return {call}\n",
    "return not P": "def f(p):\n    return not {call}\n",
    "return Q and P": "def f(p, q):\n    return q and {call}\n",
    "lambda: P": "def f(ps):\n    return sorted(ps, key=lambda p: {call})\n",
    "[e for e in es if P]": "def f(ps):\n    return [p for p in ps if {call}]\n",
    "[e for e in es if not P]": "def f(ps):\n    return [p for p in ps if not {call}]\n",
    "{e for e in es if Q and P}": "def f(ps, q):\n    return {{p for p in ps if q and {call}}}\n",
    "{e: v for e in es if P}": "def f(ps):\n    return {{p: 1 for p in ps if {call}}}\n",
    "any(P for e in es)": "def f(ps):\n    return any({call} for p in ps)\n",
    "a name bound to P": "def f(p):\n    found = {call}\n    if not found:\n        return {empty}\n    return 1\n",
    "a name P's assignment expression binds": (
        "def f(p):\n    if not (found := {call}):\n        return {empty}\n    return found\n"
    ),
}


def _presence_form(shape: str, call: str, empty: str) -> str:
    body = PRESENCE_SHAPES[shape].format(call=call, empty=empty or "None")
    if not empty and shape == "if not P: return E":
        body = body.replace("return None\n", "return\n")
    return f"import os\nimport os.path as osp\nfrom os.path import isfile\n\n\n{body}"


# A stat or listing whose OSError a handler answers with nothing, each call by each handler the policy reads, and a
# contextlib.suppress of it.
PRESENCE_STAT_CALLS = {
    "a path's stat": "p.stat()",
    "a path's lstat": "p.lstat()",
    "a path's iterdir": "list(p.iterdir())",
    "os.stat": "os.stat(p)",
    "os.scandir": "list(os.scandir(p))",
    "os.listdir": "os.listdir(p)",
    "file_presence.is_present": "file_presence.is_present(p)",
    "file_presence.matching": "file_presence.matching(p, '*')",
    "a path's is_file": "p.is_file()",
    "a path's scandir": "list(p.scandir())",
    "a path's listdir": "p.listdir()",
    "a path's glob": "list(p.glob('*'))",
    "a path's rglob": "list(p.rglob('*'))",
    "os.lstat": "os.lstat(p)",
    "os.walk": "list(os.walk(p, onerror=fail))",
    "os.fwalk": "list(os.fwalk(p, onerror=fail))",
    "file_presence.is_directory": "file_presence.is_directory(p)",
    "file_presence.is_occupied": "file_presence.is_occupied(p)",
    "os.path.getsize": "os.path.getsize(p)",
    "os.path.getmtime": "os.path.getmtime(p)",
    "os.path.getatime": "os.path.getatime(p)",
    "os.path.getctime": "os.path.getctime(p)",
}
PRESENCE_HANDLERS = {
    "OSError": "except OSError:",
    "PermissionError": "except PermissionError:",
    "IOError": "except IOError:",
    "EnvironmentError": "except EnvironmentError:",
    "BaseException": "except BaseException:",
    "Exception": "except Exception:",
    "a bare except": "except:",
    "a tuple with OSError": "except (ValueError, OSError):",
    "os.error": "except os.error:",
}
PRESENCE_HANDLED = {
    **{
        f"{handler} around {call}": (
            f"import os\nimport file_presence\n\n\ndef f(p, fail):\n    try:\n        x = {expression}\n    {clause}\n"
            "        return []\n    return x\n"
        )
        for call, expression in PRESENCE_STAT_CALLS.items()
        for handler, clause in PRESENCE_HANDLERS.items()
    },
    "an OSError handler that logs and continues": (
        "import os\n\n\ndef f(ps):\n    for p in ps:\n        try:\n            os.stat(p)\n"
        "        except OSError:\n            print('skipped', p)\n            continue\n        print(p)\n"
    ),
    "a pass handler that falls through to return None": (
        "def f(p):\n    try:\n        return p.stat().st_size\n    except OSError:\n        pass\n    return None\n"
    ),
    "a pass handler at the end of a function that returns a value": (
        "def f(p):\n    try:\n        return p.stat()\n    except OSError:\n        pass\n"
    ),
    "a logging handler that falls through to return []": (
        "def f(p):\n    try:\n        return list(p.iterdir())\n    except OSError:\n        print('skipped', p)\n"
        "    return []\n"
    ),
    "a pass handler in a loop that falls through to return False": (
        "import file_presence\n\n\ndef f(ps):\n    for p in ps:\n        try:\n"
        "            if file_presence.is_present(p):\n                return True\n        except OSError:\n"
        "            pass\n    return False\n"
    ),
    "a handler that binds an empty value its function returns": (
        "def f(p):\n    try:\n        st = p.stat()\n    except OSError:\n        st = None\n    return st\n"
    ),
    "contextlib.suppress of OSError around a stat": (
        "import contextlib\n\n\ndef f(p):\n    with contextlib.suppress(OSError):\n        return p.stat()\n"
        "    return None\n"
    ),
    "suppress by its name around a listing": (
        "from contextlib import suppress\n\n\ndef f(p):\n    with suppress(PermissionError, ValueError):\n"
        "        return list(p.iterdir())\n    return []\n"
    ),
}
# The listings, walks, and removals the policy refuses, each written as the module's imports may reach it.
PRESENCE_LISTINGS = {
    "a path's glob": "def f(p):\n    return list(p.glob('*'))\n",
    "a path's rglob": "def f(p):\n    return sorted(p.rglob('*.py'))\n",
    "glob.glob": "import glob\n\n\ndef f(p):\n    return glob.glob(p)\n",
    "glob.iglob by its name": "from glob import iglob\n\n\ndef f(p):\n    return list(iglob(p))\n",
    "glob through an alias": "import glob as g\n\n\ndef f(p):\n    return g.glob(p)\n",
    "os.walk with no onerror": "import os\n\n\ndef f(p):\n    return list(os.walk(p))\n",
    "os.walk given onerror None": "import os\n\n\ndef f(p):\n    return list(os.walk(p, onerror=None))\n",
    "os.walk given None by position": "import os\n\n\ndef f(p):\n    return list(os.walk(p, True, None))\n",
    "walk by its name": "from os import walk\n\n\ndef f(p):\n    return list(walk(p))\n",
    "os.walk given an unpacking": "import os\n\n\ndef f(p, k):\n    return list(os.walk(p, **k))\n",
    "os.fwalk with no onerror": "import os\n\n\ndef f(p):\n    return list(os.fwalk(p))\n",
    "a path's walk with no on_error": "def f(p):\n    return list(p.walk())\n",
    "a path's walk given on_error None": "def f(p):\n    return list(p.walk(on_error=None))\n",
    "a path's walk given None by position": "def f(p):\n    return list(p.walk(True, None))\n",
    "shutil.rmtree given ignore_errors": "import shutil\n\n\ndef f(p):\n    shutil.rmtree(p, ignore_errors=True)\n",
    "shutil.rmtree given it by position": "import shutil\n\n\ndef f(p):\n    shutil.rmtree(p, True)\n",
    "shutil.rmtree given a name for it": "import shutil\n\n\ndef f(p, q):\n    shutil.rmtree(p, ignore_errors=q)\n",
    "rmtree through an alias": "from shutil import rmtree as r\n\n\ndef f(p):\n    r(p, ignore_errors=True)\n",
    "a TemporaryDirectory given ignore_cleanup_errors": (
        "import tempfile\n\n\ndef f():\n    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)\n"
    ),
    "filter given os.path.isfile": "import os\n\n\ndef f(ps):\n    return list(filter(os.path.isfile, ps))\n",
    "filter given Path.is_dir": (
        "from pathlib import Path\n\n\ndef f(ps):\n    return list(filter(Path.is_dir, ps))\n"
    ),
    "itertools.filterfalse given os.path.exists": (
        "import itertools\nimport os\n\n\ndef f(ps):\n    return list(itertools.filterfalse(os.path.exists, ps))\n"
    ),
    **{
        f"an emptiness check after {test} by {listing}": (
            f"import os\n\n\ndef f(p):\n    if {test} and any({listing}):\n        raise OSError(p)\n"
        )
        for test in ("p.exists()", "p.is_dir()", "os.path.isdir(p)")
        for listing in ("p.iterdir()", "os.scandir(p)", "os.listdir(p)")
    },
}
PRESENCE_REFUSED = {
    **{
        f"{shape} by {predicate}": _presence_form(shape, call, "None")
        for shape in PRESENCE_SHAPES
        for predicate, call in PRESENCE_PREDICATE_CALLS.items()
    },
    **{
        f"a return of {empty or 'nothing'}": _presence_form("if not P: return E", "p.is_file()", empty)
        for empty in PRESENCE_EMPTIES
    },
    **{
        f"a return of only empty values, {empty}": _presence_form("if not P: return E", "p.is_file()", empty)
        for empty in PRESENCE_EMPTIES_OF_EMPTIES
    },
    "os.path through an alias": _presence_form("V if P else E", "osp.isdir(p)", "[]"),
    "an imported isfile": _presence_form("V if P else E", "isfile(p)", "[]"),
    **{
        f"a continue over {listing}": (
            f"import os\n\n\ndef f(p):\n    for e in {listing}:\n        if not e.is_file():\n            continue\n"
            "        print(e)\n"
        )
        for listing in ("p.iterdir()", "reversed(list(os.scandir(p)))", "os.listdir(p)")
    },
    **PRESENCE_LISTINGS,
    **PRESENCE_HANDLED,
}
PRESENCE_PASSED = {
    # The file_presence forms, which let a failed stat or listing through.
    "file_presence": (
        "import file_presence\n\n\ndef f(p):\n    return p.read_text() if file_presence.is_present(p) else None\n"
    ),
    "file_presence in a filter": (
        "import file_presence\n\n\ndef f(p):\n"
        "    return [e for e in p.iterdir() if file_presence.is_present(e, other_is_absent=True)]\n"
    ),
    "file_presence.matching": "import file_presence\n\n\ndef f(p):\n    return file_presence.matching(p, '**/*.py')\n",
    "an emptiness check after file_presence": (
        "import file_presence\n\n\ndef f(p):\n    if file_presence.is_directory(p) and any(p.iterdir()):\n"
        "        raise OSError(p)\n"
    ),
    "os.walk given an onerror": "import os\n\n\ndef f(p, fail):\n    return list(os.walk(p, onerror=fail))\n",
    "os.walk given one by position": "import os\n\n\ndef f(p, fail):\n    return list(os.walk(p, True, fail))\n",
    "shutil.rmtree": "import shutil\n\n\ndef f(p):\n    shutil.rmtree(p)\n",
    "shutil.rmtree given ignore_errors False": (
        "import shutil\n\n\ndef f(p):\n    shutil.rmtree(p, ignore_errors=False)\n"
    ),
    "a TemporaryDirectory": "import tempfile\n\n\ndef f():\n    return tempfile.TemporaryDirectory()\n",
    "filter given None": "def f(ps):\n    return list(filter(None, ps))\n",
    # A predicate whose False leads to a refusal, a value, or more work is no empty answer, and an `if` whose True
    # branch does the work and that has no else goes on to the code after it, which meets the same path.
    "a refusal": "def f(p):\n    if not p.is_file():\n        raise OSError(p)\n    return 1\n",
    "a value": "def f(p):\n    return p.read_text() if p.is_file() else 'default'\n",
    "more work": "def f(p):\n    if not p.is_dir():\n        p.mkdir()\n        return None\n    return 1\n",
    "an operand of a wider test": "def f(p, q):\n    if q and p.is_file():\n        return 1\n    return 2\n",
    "an if with no else": "def f(p):\n    if p.exists():\n        p.unlink()\n    return 1\n",
    # A branch that records the case before its empty answer, as a report or a list of problems does, answers with
    # it; only a branch that logs and answers nothing reads it as nothing there.
    "a branch that records the case and continues": (
        "def f(ps, problems):\n    for p in ps:\n        if not p.is_file():\n"
        "            problems.append(f'{p} is missing')\n            continue\n        print(p)\n"
    ),
    "a branch that records the case and breaks": (
        "def f(ps, problems):\n    for p in ps:\n        if not p.is_file():\n"
        "            problems.append(p)\n            break\n        print(p)\n"
    ),
    "a return of a tuple holding a reason": (
        "def f(p):\n    if not p.is_file():\n        return None, 'missing'\n    return p, None\n"
    ),
    "a branch that reports and returns nothing": (
        "def f(self, p):\n    if not p.is_file():\n        self.not_read(p, 'not found')\n        return\n"
        "    return 1\n"
    ),
    # A handler that takes only "not found", lets the error through, or answers with a value, and a handler around
    # a read rather than a stat or a listing, are no answer of nothing for a path it cannot stat.
    "a FileNotFoundError handler": (
        "def f(p):\n    try:\n        return p.stat()\n    except FileNotFoundError:\n        return None\n"
    ),
    "an OSError handler that raises": (
        "def f(p):\n    try:\n        return p.stat()\n    except OSError as error:\n"
        "        raise RuntimeError(p) from error\n"
    ),
    "an OSError handler that answers with a reason": (
        "def f(p):\n    try:\n        return p.stat()\n    except OSError as error:\n        return str(error)\n"
    ),
    "an OSError handler around a read": (
        "def f(p):\n    try:\n        return p.read_text()\n    except OSError:\n        return None\n"
    ),
    "a handler that binds a reason its function returns": (
        "def f(p):\n    try:\n        st = p.stat()\n    except OSError as error:\n        st = str(error)\n"
        "    return st\n"
    ),
    "a handler that logs and raises": (
        "def f(p):\n    try:\n        return p.stat()\n    except OSError:\n        print('failed', p)\n        raise\n"
    ),
    "a pass handler that goes on to more work": (
        "def f(p):\n    try:\n        p.stat()\n    except OSError:\n        pass\n    p.mkdir()\n    return 1\n"
    ),
    "a handler that binds an empty value its function does not return": (
        "def f(p):\n    try:\n        st = p.stat()\n    except OSError:\n        st = None\n    return 1\n"
    ),
    "suppress of FileNotFoundError around a stat": (
        "import contextlib\n\n\ndef f(p):\n    with contextlib.suppress(FileNotFoundError):\n"
        "        return p.stat()\n    return None\n"
    ),
    # An if whose True branch returns and whose False goes on to a value, more work, or an empty return of its own.
    "an if P: return V, then a value": (
        "def f(p):\n    if p.is_file():\n        return p.read_text()\n    return 'default'\n"
    ),
    "an if P: return V, then more work": (
        "def f(p):\n    if p.is_file():\n        return 1\n    p.mkdir()\n    return 2\n"
    ),
    "an if P: return E at the function's end": "def f(p):\n    if p.is_file():\n        return None\n",
    # A name bound to a predicate's answer and to anything else, or a parameter, is not followed.
    "a name bound otherwise too": (
        "def f(p, q):\n    found = p.exists()\n    found = q\n    if not found:\n        return None\n    return 1\n"
    ),
    "a parameter of the same name": (
        "def f(p, found):\n    if not found:\n        return None\n    found = p.exists()\n    return found\n"
    ),
    # A predicate stored in a container or handed to a call other than a filter is read where that is read.
    "a predicate stored": "def f(p):\n    return {'present': p.exists()}\n",
    "a predicate handed to a call": "def f(p, report):\n    report(p.exists())\n    return 1\n",
    # A predicate that is not a presence test, and a walk that is a module's function or a class's own method, never a
    # path's.
    # no module can call.
    "another method": "def f(p):\n    return p.read_text() if p.is_absolute() else None\n",
    "ast.walk": "import ast\n\n\ndef f(tree):\n    return list(ast.walk(tree))\n",
    "a class's own walk": "class Walker:\n    def f(self, tree):\n        return list(self.walk(tree))\n",
    "a path's walk given an on_error": "def f(p, fail):\n    return list(p.walk(on_error=fail))\n",
    # Each stated limit, which the policy's next widening reads; until then each passes.
    "limit: not P as an operand of an and": (
        "def f(p, q):\n    if not p.is_file() and q:\n        return None\n    return 1\n"
    ),
    "limit: a predicate handed to map": "import os\n\n\ndef f(ps):\n    return any(map(os.path.exists, ps))\n",
    "limit: a predicate bound to a name it does not follow": (
        "from pathlib import Path\n\n\ndef f(p):\n    check = Path.exists\n    if not check(p):\n"
        "        return None\n    return 1\n"
    ),
    "limit: a predicate reached through getattr": (
        "def f(p):\n    if not getattr(p, 'exists')():\n        return None\n    return 1\n"
    ),
    "limit: a returned sentinel name": (
        "def f(p):\n    result = []\n    if not p.is_dir():\n        return result\n    return list(p.iterdir())\n"
    ),
    "limit: P == False": "def f(p):\n    if p.is_file() == False:\n        return None\n    return 1\n",
    "limit: P is False": "def f(p):\n    if p.is_file() is False:\n        return None\n    return 1\n",
    "limit: an rmtree onerror that swallows the error": (
        "import shutil\n\n\ndef f(p):\n    shutil.rmtree(p, onerror=lambda *error: None)\n"
    ),
    "limit: a branch that does other work before its empty answer": (
        "def f(p):\n    if not p.is_file():\n        count = 1\n        return None\n    return 1\n"
    ),
    "limit: an if P guard around work whose result is an answer": (
        "def f(ps, tokens):\n    for p in ps:\n        if p.is_file():\n            tokens.update(p.read_text())\n"
        "    return tokens\n"
    ),
    "limit: a handler tuple bound to a name": (
        "ERRS = (OSError,)\n\n\ndef f(p):\n    try:\n        return p.stat()\n    except ERRS:\n        return None\n"
    ),
    "limit: a warn imported from warnings": (
        "from warnings import warn\n\n\ndef f(p):\n    if not p.is_file():\n        warn('missing')\n"
        "        return None\n    return 1\n"
    ),
    "limit: a refusal with a not-found reason": (
        "def f(p):\n    if not p.is_file():\n        raise ValueError(f'{p} not found')\n    return p.read_text()\n"
    ),
    "limit: a link test, which the link-test policy holds": "def f(p):\n    return p.is_symlink()\n",
}


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        *((text, True) for text in PRESENCE_REFUSED.values()),
        *((text, False) for text in PRESENCE_PASSED.values()),
    ],
    ids=[*PRESENCE_REFUSED, *PRESENCE_PASSED],
)
def test_presence_policy_refuses_each_empty_answer_form(text: str, refused: bool) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = root / "skills" / "alpha" / "scripts" / "alpha.py"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
        found = python_checks.presence_problems(root, [path])
    assert bool(found) == refused, found


PRESENCE_SOURCE = """\
import os


def refused(p):
    return p.read_text() if p.is_file() else None


def made(p):
    p.mkdir()
    if not p.is_dir():
        return []
    return list(p.iterdir())


PRESENCE_ALLOWED = {"made": "the caller just made the folder", "stale": "a reason"}
"""


def test_presence_policy_sanctions_by_scope_and_leaves_file_presence_alone() -> None:
    alpha = "skills/alpha/scripts/alpha.py"
    found = run_claim_policy(
        python_checks.presence_problems,
        "PRESENCE_ALLOWED",
        PRESENCE_SOURCE,
        "skills/ejryan89-skill-core/scripts/file_presence.py",
    )
    assert found == [
        f"{alpha}:5 reads is_file's False as nothing there, which it also answers for a path it cannot stat; call "
        "ejryan89-skill-core's file_presence (is_present, is_directory, or is_occupied), which lets a failed stat "
        "through, or state why in PRESENCE_ALLOWED",
        f"{alpha}: PRESENCE_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
        "tools/broken.py: PRESENCE_ALLOWED must map each function, class, or assignment to its reason",
    ]


@contextlib.contextmanager
def _stat_failing(failing: Callable[[Path], bool]) -> Iterator[None]:
    """Patch Path.stat so it raises PermissionError for each path `failing` names and answers every other, and Path's
    presence predicates so they answer False for such a path, as they do from Python 3.14 on, so a reader left on a
    predicate reads it as absent rather than reaching another stat that raises."""
    real = Path.stat

    def answer(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if failing(self):
            raise PermissionError(13, "Access is denied", str(self))
        return real(self, follow_symlinks=follow_symlinks)

    def answering_false(real_predicate: Callable[..., bool]) -> Callable[..., bool]:
        def predicate(self: Path, *rest: Any, **options: Any) -> bool:
            return False if failing(self) else real_predicate(self, *rest, **options)

        return predicate

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(Path, "stat", autospec=True, side_effect=answer))
        for name in ("exists", "is_file", "is_dir"):
            stack.enter_context(mock.patch.object(Path, name, answering_false(getattr(Path, name))))
        yield


def _validation_readers() -> dict[str, tuple[Callable[[Path], bool], Callable[[], object]]]:
    """Each validation reader converted to file_presence, the paths whose stat its row fails, and its call over this
    checkout."""
    import duplication
    import fsops_platform
    import kinds_table
    import markdown_links
    import shell_targets
    import skill_grants
    import skill_layout
    import skill_scripts
    import suite_discovery
    import validation_support
    import workflows
    from skill_menu import PLUGIN_MANIFEST

    from tools.plugin_build import README as PLUGIN_README

    root = REPOSITORY_ROOT
    owner = root / duplication._shared_facts()[0].owner
    asset = root / "skills" / sorted(json.loads((root / "source.json").read_text(encoding="utf-8"))["shared_assets"])[0]
    skill = next(path.parent.name for path in sorted((root / "skills").glob("*/SKILL.md")))

    def at(*paths: Path) -> Callable[[Path], bool]:
        return lambda path: path in paths

    sibling = root / "tests" / "validation" / "validation_support.py"

    def resolve_alone() -> object:
        # _module_file, which stats its candidate, is stubbed to hand the candidate on without a stat, so resolve's own
        # read is the one stat of it on every Python, however many a version's own calls make.
        def module_file(base: Path, parts: list[str]) -> Path | None:
            return sibling if base == sibling.parent and parts == [sibling.stem] else None

        with mock.patch.object(suite_discovery.RepositoryModules, "_module_file", staticmethod(module_file)):
            return suite_discovery.RepositoryModules(root).resolve(sibling.parent / "python_checks.py", sibling.stem)

    def guard(span: str) -> object:
        # The index of tests lists the checkout, so it is built over no files, leaving the guard's own read of its file.
        with mock.patch.object(markdown_links, "repository_files", return_value=[]):
            return markdown_links._guard_problem(root, markdown_links._TestIndex(root, []), "docs/lessons.md", span)

    def own_read_of_tests() -> object:
        # regression_suites lists tests/ too, so it is held empty to leave suite_python_modules' own read of it.
        with mock.patch.object(suite_discovery, "regression_suites", return_value=[]):
            return suite_discovery.suite_python_modules(root)

    def granted_script(path: Path) -> bool:
        return path.suffix == ".py" and path.is_relative_to(root / "tools")

    return {
        "duplication.shared_fact_owner_problems": (at(owner), lambda: duplication.shared_fact_owner_problems(root)),
        "duplication.skill_core_import_problems": (
            at(root / duplication.DEPLOYER_PACKAGE),
            lambda: duplication.skill_core_import_problems(root),
        ),
        "markdown_links._TestIndex.locations": (
            at(root / "tests" / "validation" / "test_python_checks.py"),
            lambda: markdown_links._TestIndex(root, []).locations("doc.md", "tests/validation/test_python_checks.py"),
        ),
        "skill_grants.root_option_problems": (
            granted_script,
            lambda: skill_grants.root_option_problems(root, sorted((root / ".claude" / "skills").glob("*/SKILL.md"))),
        ),
        "skill_layout.deploy_variable_problems": (
            at(asset),
            lambda: skill_layout.deploy_variable_problems(root, set(), set(), set()),
        ),
        "skill_layout.own_needs metadata": (
            at(root / "deploy-meta" / f"{skill}.json"),
            lambda: skill_layout.own_needs(root, skill),
        ),
        "skill_layout.own_needs scripts": (
            at(root / "skills" / skill / "scripts"),
            lambda: skill_layout.own_needs(root, skill),
        ),
        "skill_layout.runtime_page_problems": (
            at(root / "docs" / "codex-support.md"),
            lambda: skill_layout.runtime_page_problems(root),
        ),
        "skill_layout._sibling_path_files": (at(root / "deploy.py"), lambda: skill_layout._sibling_path_files(root)),
        "skill_layout.skill_tree_problems skills": (
            at(root / "skills"),
            lambda: skill_layout.skill_tree_problems(root),
        ),
        "skill_layout.skill_tree_problems source.json": (
            at(root / "source.json"),
            lambda: skill_layout.skill_tree_problems(root),
        ),
        "suite_discovery.suite_python_modules": (
            at(root / "conftest.py"),
            lambda: suite_discovery.suite_python_modules(root),
        ),
        "suite_discovery.RepositoryModules": (
            at(root / "pyproject.toml"),
            lambda: suite_discovery.RepositoryModules(root),
        ),
        "suite_discovery.recorded_shard_problems": (
            at(root / next(iter(suite_discovery.RECORDED_TEST_SECONDS))),
            lambda: suite_discovery.recorded_shard_problems(root),
        ),
        "suite_discovery.RepositoryModules._module_file": (
            at(root / "tests" / "validation" / "python_checks.py"),
            lambda: suite_discovery.RepositoryModules._module_file(root / "tests" / "validation", ["python_checks"]),
        ),
        "suite_discovery.git_isolation_problems": (
            at(root / "conftest.py"),
            lambda: suite_discovery.git_isolation_problems(root),
        ),
        "markdown_links._guard_problem skill step": (
            at(root / "skills" / skill / "SKILL.md"),
            lambda: guard(f"skills/{skill}/SKILL.md#a-step"),
        ),
        "markdown_links._guard_problem workflow": (
            at(root / ".github" / "workflows" / "deployable.yml"),
            lambda: guard(".github/workflows/deployable.yml"),
        ),
        "markdown_links.lessons_ledger_problems": (
            at(root / "docs" / "lessons.md"),
            lambda: markdown_links.lessons_ledger_problems(root),
        ),
        "suite_discovery.pool_shared_state_problems": (
            at(*suite_discovery.policy_module_files(root)[:1]),
            lambda: suite_discovery.pool_shared_state_problems(root),
        ),
        "kinds_table._scanned_files": (at(root / "deploy.py"), lambda: kinds_table._scanned_files(root)),
        "fsops_platform._platform_scanned_files": (
            at(root / "tools" / "worktrees.py"),
            lambda: fsops_platform._platform_scanned_files(root),
        ),
        "fsops_platform._writes_scanned_files": (
            at(root / "deploy.py"),
            lambda: fsops_platform._writes_scanned_files(root),
        ),
        "python_checks.imports_a_sibling": (
            # python_checks imports call_tables, beside it, before any other sibling.
            at(root / "tests" / "validation" / "call_tables.py"),
            lambda: python_checks.imports_a_sibling(
                root / "tests" / "validation" / "python_checks.py",
                ast.parse((root / "tests" / "validation" / "python_checks.py").read_text(encoding="utf-8")),
            ),
        ),
        "python_checks.strict_area_problems": (
            at(root / "deployer"),
            lambda: python_checks.strict_area_problems(root),
        ),
        "skill_scripts.console_entry_points": (
            at(root / "deploy.py"),
            lambda: skill_scripts.console_entry_points(root),
        ),
        "shell_targets.shell_script_targets": (
            at(root / "skills" / skill / "SKILL.md"),
            lambda: shell_targets.shell_script_targets(root),
        ),
        "skill_layout.skill_tree_problems per skill folder": (
            at(root / "skills" / skill),
            lambda: skill_layout.skill_tree_problems(root),
        ),
        "skill_layout.skill_tree_problems per SKILL.md": (
            at(root / "skills" / skill / "SKILL.md"),
            lambda: skill_layout.skill_tree_problems(root),
        ),
        "skill_layout._template_tokens": (
            at(root / "skills" / skill / "SKILL.md"),
            lambda: skill_layout._template_tokens(root / "skills" / skill),
        ),
        "skill_layout.plugin_tree_problems manifest": (
            at(root / PLUGIN_MANIFEST),
            lambda: skill_layout.plugin_tree_problems(root, {}, "0.0.0"),
        ),
        "skill_layout.plugin_tree_problems README": (
            at(root / PLUGIN_README),
            lambda: skill_layout.plugin_tree_problems(root, {"alpha": {"README.md": b""}}, "0.0.0"),
        ),
        "suite_discovery.suite_python_modules tests": (at(root / "tests"), own_read_of_tests),
        "suite_discovery.pytest_configuration_problems": (
            at(root / "pytest.ini"),
            lambda: suite_discovery.pytest_configuration_problems(root),
        ),
        "suite_discovery.RepositoryModules.resolve": (at(sibling), resolve_alone),
        "validation_support.repository_files": (
            at(root / "deploy.py"),
            lambda: validation_support.repository_files(root),
        ),
        "validation_support.skill_script_directories": (
            at(root / "skills" / skill / "scripts"),
            lambda: validation_support.skill_script_directories(root),
        ),
        "workflows.unknown_workflow_problems": (
            at(root / ".github" / "workflows"),
            lambda: workflows.unknown_workflow_problems(root / ".github" / "workflows", ()),
        ),
    }


@pytest.mark.parametrize("reader", sorted(_validation_readers()))
def test_a_validation_reader_refuses_a_path_whose_stat_fails_rather_than_reading_it_as_absent(reader: str) -> None:
    failing, read = _validation_readers()[reader]
    with _stat_failing(failing), pytest.raises(PermissionError):
        read()


def _pruning_readers() -> dict[str, tuple[dict[str, str], str, str, Callable[[Path], object]]]:
    """Each validation reader that lists a tree and leaves a folder out: the files of its tree, a folder inside the
    left-out one, a folder elsewhere, and its call over the tree's root."""
    import skill_layout
    import suite_discovery

    suites = {"tests/fixtures/deep/test_fixture.py": "pass\n", "tests/other/test_real.py": "pass\n"}
    return {
        "skill_layout._sibling_path_files": (
            {"tests/fixtures/deep/fixture.py": "", "tests/other/real.py": ""},
            "tests/fixtures/deep",
            "tests/other",
            skill_layout._sibling_path_files,
        ),
        "skill_layout.script_layout_problems": (
            {
                "skills/alpha/SKILL.md": "---\nname: alpha\n---\n",
                "skills/alpha/scripts/__pycache__/deep/alpha.pyc": "",
                "skills/alpha/references/deep/notes.md": "",
            },
            "skills/alpha/scripts/__pycache__/deep",
            "skills/alpha/references/deep",
            skill_layout.script_layout_problems,
        ),
        "suite_discovery.regression_suites": (
            suites,
            "tests/fixtures/deep",
            "tests/other",
            suite_discovery.regression_suites,
        ),
        "suite_discovery.suite_python_modules": (
            suites,
            "tests/fixtures/deep",
            "tests/other",
            suite_discovery.suite_python_modules,
        ),
        "suite_discovery._suite_python_files": (
            {**suites, "tests/__pycache__/deep/cached.py": ""},
            "tests/__pycache__/deep",
            "tests/other",
            suite_discovery._suite_python_files,
        ),
    }


@contextlib.contextmanager
def _listing_refused(folder: Path) -> Iterator[None]:
    """`folder` as one whose listing is refused: os.scandir and os.listdir raise PermissionError naming it."""

    def refusing(real: Callable[..., Any]) -> Callable[..., Any]:
        def call(path: Any = ".", *rest: Any, **options: Any) -> Any:
            if isinstance(path, (str, os.PathLike)) and Path(path) == folder:
                raise PermissionError(13, "Access is denied", str(path))
            return real(path, *rest, **options)

        return call

    with contextlib.ExitStack() as stack:
        for name in ("scandir", "listdir"):
            stack.enter_context(mock.patch.object(os, name, refusing(getattr(os, name))))
        yield


@pytest.mark.parametrize("reader", sorted(_pruning_readers()))
def test_a_validation_reader_never_lists_the_folder_it_leaves_out_and_refuses_another_it_cannot_list(
    reader: str, tmp_path: Path
) -> None:
    files, left_out, elsewhere, read = _pruning_readers()[reader]
    write_fixture_tree(tmp_path, files)
    with _listing_refused(tmp_path / left_out):
        read(tmp_path)
    with _listing_refused(tmp_path / elsewhere), pytest.raises(PermissionError) as raised:
        read(tmp_path)
    assert raised.value.filename == str(tmp_path / elsewhere)


def _cache_key(root: Path, place: str) -> Path:
    """The cache folder a default type check of `a.py` run from `place` has under `root`."""
    import hashlib

    return root / hashlib.sha256(f"{place}\0a.py\0default".encode()).hexdigest()[:16]


def test_the_type_check_cache_is_keyed_by_the_check_s_place_under_the_repository(tmp_path: Path) -> None:
    inside = REPOSITORY_ROOT / "skills" / "x"
    outside = tmp_path / "elsewhere"
    assert python_checks.mypy_cache_directory(tmp_path, inside, ["a.py"], strict=False) == _cache_key(
        tmp_path, "skills/x"
    )
    assert python_checks.mypy_cache_directory(tmp_path, outside, ["a.py"], strict=False) == _cache_key(
        tmp_path, outside.as_posix()
    )


class PrefixedCacheTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "only Windows spells a path with an extended-length prefix")
    def test_a_run_from_the_prefixed_repository_shares_the_plain_spelling_s_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefixed = Path("\\\\?\\" + str(REPOSITORY_ROOT / "skills" / "x"))
            found = python_checks.mypy_cache_directory(root, prefixed, ["a.py"], strict=False)
            self.assertEqual(_cache_key(root, "skills/x"), found)


# The indirection policies: each row is a module body after RESTRICTED_IMPORTS, whose lines marked `# read` are those
# the policy refuses and no others.
RESTRICTED_IMPORTS = (
    "import builtins\nimport ctypes\nimport functools\nimport importlib\nimport importlib.util\nimport inspect\n"
    "import json\nimport operator\nimport os\nimport runpy\nimport subprocess\nimport sys\n\n"
    "import seconds_option\nimport whole_number\n\n\n"
)


def restricted_rows(
    policy: Callable[[Path, list[Path]], list[str]], body: str, path: str = "skills/alpha/scripts/alpha.py"
) -> tuple[list[str], list[str]]:
    """The lines a policy refuses in the module at `path` holding body after RESTRICTED_IMPORTS, and the lines the
    body marks `# read`, both by their numbers."""
    source = RESTRICTED_IMPORTS + body
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {path: source})
        found = policy(root, sorted(root.rglob("*.py")))
    refused = sorted({problem.split(" ", 1)[0].rpartition(":")[2] for problem in found})
    marked = sorted(str(number) for number, line in enumerate(source.splitlines(), 1) if line.endswith("# read"))
    return refused, marked


# Each construct of the dynamic-lookup catalogue, refused, and each direct form, read.
INDIRECTION_ROWS = {
    "getattr given a computed name": "def f(t, name):\n    return getattr(t, name)  # read\n",
    "getattr given a concatenation": "def f(t):\n    return getattr(t, 'ru' + 'n')  # read\n",
    "getattr given an unpacked name": "def f(t, names):\n    return getattr(t, *names)  # read\n",
    "builtins.getattr given a computed name": "def f(t, name):\n    return builtins.getattr(t, name)  # read\n",
    "setattr given a computed name": "def f(t, name):\n    setattr(t, name, 1)  # read\n",
    "delattr given a computed name": "def f(t, name):\n    delattr(t, name)  # read\n",
    "hasattr given a computed name": "def f(t, name):\n    return hasattr(t, name)  # read\n",
    "getattr bound to a name": "def f(t):\n    look = getattr  # read\n    return look(t, 'x')\n",
    "getattr passed as a value": "def f(t):\n    return map(getattr, t)  # read\n",
    "vars": "def f(t):\n    return vars(t)  # read\n",
    "globals": "def f():\n    return globals()  # read\n",
    "locals": "def f():\n    return locals()  # read\n",
    "eval": "def f(t):\n    return eval(t)  # read\n",
    "exec": "def f(t):\n    exec(t)  # read\n",
    "__import__": "def f():\n    return __import__('os')  # read\n",
    "__builtins__": "def f():\n    return __builtins__  # read\n",
    "importlib.import_module of a literal": "def f():\n    return importlib.import_module('os')  # read\n",
    "importlib.import_module of a name": "def f(t):\n    return importlib.import_module(t)  # read\n",
    "importlib.util.spec_from_file_location": (
        "def f(t):\n    return importlib.util.spec_from_file_location('m', t)  # read\n"
    ),
    "runpy.run_path": "def f(t):\n    runpy.run_path(t)  # read\n",
    "sys.modules indexed": "def f():\n    return sys.modules['os']  # read\n",
    "sys.modules read by get": "def f():\n    return sys.modules.get('os')  # read\n",
    "sys._getframe": "def f():\n    return sys._getframe()  # read\n",
    "inspect.currentframe": "def f():\n    return inspect.currentframe()  # read\n",
    "inspect.getattr_static": "def f(t):\n    return inspect.getattr_static(t, 'x')  # read\n",
    "operator.attrgetter applied": "def f(t):\n    return operator.attrgetter('x')(t)  # read\n",
    "operator.methodcaller applied": "def f(t):\n    return operator.methodcaller('x')(t)  # read\n",
    "an imported attrgetter": "from operator import attrgetter as ag\n\n\ndef f(t):\n    return ag('x')(t)  # read\n",
    "a namespace's __dict__": "def f(t):\n    return t.__dict__['x']  # read\n",
    "object.__getattribute__": "def f(t):\n    return object.__getattribute__(t, 'x')  # read\n",
    "a bound __getattribute__": "def f(t):\n    return t.__getattribute__('x')  # read\n",
    "a function's __globals__": "def f(t):\n    return t.__globals__  # read\n",
    "a frame's f_locals": "def f(t):\n    return t.f_locals  # read\n",
    "a module __getattr__": "def __getattr__(name):  # read\n    return name\n",
    "a class __getattribute__": "class A:\n    def __getattribute__(self, name):  # read\n        return name\n",
    "a module bound to a name": "def f(t):\n    m = json  # read\n    return m.loads(t)\n",
    "a module passed as a value": "def f(t):\n    return t(json)  # read\n",
    "a module twice imported": (
        "import importlib.machinery\n\n\ndef f(t):\n    return importlib.import_module(t)  # read\n"
    ),
    "getattr given a literal name": "def f(t):\n    return getattr(t, 'x', None), hasattr(t, 'x')\n",
    "setattr given a literal name": "def f(t):\n    setattr(t, 'x', 1)\n",
    "a literal lookup on a module": "def f(t):\n    return getattr(json, 'loads')(t)\n",
    "an attribute of a module": "def f(t):\n    return json.loads(t), os.path.join(t, 'x')\n",
    "a local of a catalogued name": "def f(t):\n    vars = t\n    return vars\n",
}


@pytest.mark.parametrize("body", INDIRECTION_ROWS.values(), ids=INDIRECTION_ROWS.keys())
def test_indirection_policy_refuses_each_dynamic_lookup(body: str) -> None:
    refused, marked = restricted_rows(python_checks.indirection_problems, body)
    assert refused == marked


# Each standard-library callable or name that resolves an object from a dotted name or a string given as a value,
# loads a module by a name or a path, unpickles, runs code given as a string, or hands out every live object or a
# frame, which the indirection policy refuses wherever a module refers to it.
STANDARD_RESOLVERS = (
    "pydoc.locate",
    "pydoc.resolve",
    "pkgutil.find_loader",
    "pkgutil.get_loader",
    "pkgutil.get_importer",
    "pkgutil.walk_packages",
    "logging.config.dictConfig",
    "logging.config.fileConfig",
    "logging.config.listen",
    "logging.config.BaseConfigurator",
    "logging.config.DictConfigurator",
    "unittest.mock.patch",
    "unittest.mock._importer",
    "unittest.mock._get_target",
    "typing.ForwardRef",
    "annotationlib.ForwardRef",
    "annotationlib.call_evaluate_function",
    "importlib.find_loader",
    "importlib.util.find_spec",
    "importlib.util.spec_from_loader",
    "importlib.util.LazyLoader",
    "importlib.machinery.SourcelessFileLoader",
    "importlib.machinery.ExtensionFileLoader",
    "importlib.machinery.PathFinder",
    "importlib.machinery.FileFinder",
    "importlib.metadata.entry_points",
    "importlib.metadata.EntryPoint",
    "importlib.metadata.EntryPoints",
    "zipimport.zipimporter",
    "imp.find_module",
    "imp.load_module",
    "imp.load_source",
    "imp.load_compiled",
    "imp.load_dynamic",
    "sys.meta_path",
    "sys.path_hooks",
    "sys.path_importer_cache",
    "pickle.load",
    "pickle.loads",
    "pickle.Unpickler",
    "shelve.open",
    "shelve.Shelf",
    "shelve.DbfilenameShelf",
    "shelve.BsdDbShelf",
    "timeit.timeit",
    "timeit.repeat",
    "timeit.Timer",
    "profile.run",
    "profile.runctx",
    "profile.Profile",
    "cProfile.run",
    "cProfile.runctx",
    "cProfile.Profile",
    "pdb.run",
    "pdb.runeval",
    "pdb.runctx",
    "pdb.Pdb",
    "code.interact",
    "code.InteractiveInterpreter",
    "code.InteractiveConsole",
    "trace.Trace",
    "doctest.testmod",
    "doctest.testfile",
    "doctest.run_docstring_examples",
    "doctest.DocTestRunner",
    "gc.get_objects",
    "gc.get_referrers",
    "gc.get_referents",
    "sys._current_frames",
    "inspect.stack",
    "inspect.trace",
    "inspect.getouterframes",
    "inspect.getinnerframes",
)


@pytest.mark.parametrize("name", STANDARD_RESOLVERS)
def test_indirection_policy_refuses_each_standard_resolver(name: str) -> None:
    module = name.rpartition(".")[0]
    body = f"import {module}\n\n\ndef f():\n    return {name}  # read\n"
    refused, marked = restricted_rows(python_checks.indirection_problems, body)
    assert refused == marked


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("def f(t):\n    return t.__subclasses__()  # read\n", id="__subclasses__"),
        pytest.param("def f(t):\n    return t.entry_points(group='x')  # read\n", id="a distribution's entry_points"),
    ],
)
def test_indirection_policy_refuses_each_enumerating_attribute(body: str) -> None:
    refused, marked = restricted_rows(python_checks.indirection_problems, body)
    assert refused == marked


# Each owner-only callable referenced outside its owners, refused, and each form the rule leaves alone, read.
OWNER_ONLY_ROWS = {
    "json.dumps bound to a name": "def f(t):\n    write = json.dumps  # read\n    return write(t)  # read\n",
    "json.dumps as a parameter's default": "def f(t, dumps=json.dumps):  # read\n    return dumps(t)  # read\n",
    "json.dumps as a lambda's default": "WRITE = lambda t, dumps=json.dumps: dumps(t)  # read\n",
    "json.dumps returned": "def f():\n    return json.dumps  # read\n",
    "json.dumps as a dict value": "WRITERS = {'json': json.dumps}  # read\n",
    "json.dumps as a class attribute": "class A:\n    TEXT = json.dumps  # read\n",
    "json.dump passed as a value": "def f(t):\n    return map(json.dump, t)  # read\n",
    "json.dumps by a literal lookup": "def f(t):\n    return getattr(json, 'dumps')(t)  # read\n",
    "json.dumps imported and bound": "from json import dumps\n\nWRITE = dumps  # read\n",
    "a JSONEncoder called": "def f(t):\n    return json.JSONEncoder().iterencode(t)  # read\n",
    "a JSONEncoder subclassed": "class E(json.JSONEncoder):  # read\n    pass\n",
    "a ctypes loader": "def f():\n    return ctypes.WinDLL('kernel32')  # read\n",
    "a ctypes namespace": "def f():\n    return ctypes.windll.kernel32  # read\n",
    "ctypes.pythonapi": "def f():\n    return ctypes.pythonapi  # read\n",
    "a subprocess runner called": "def f():\n    return subprocess.run(['git'])  # read\n",
    "a subprocess runner imported": "from subprocess import Popen\n\n\ndef f():\n    return Popen(['git'])  # read\n",
    "an os start": "def f(t):\n    return os.system(t)  # read\n",
    "a commonpath": "def f(t):\n    return os.path.commonpath(t)  # read\n",
    "json.dumps called directly": "def f(t):\n    return json.dumps(t, indent=2)  # read\n",
    "json.dumps imported and called": "from json import dumps\n\n\ndef f(t):\n    return dumps(t)  # read\n",
    "json.loads called": "def f(t):\n    return json.loads(t)  # read\n",
    "json.load as a default": "def f(t, load=json.load):  # read\n    return load(t)  # read\n",
    "a JSONDecoder subclassed": "class D(json.JSONDecoder):  # read\n    pass\n",
    "json_writer and json_reader called": (
        "import json_reader\nimport json_writer\n\n\ndef f(t):\n    return json_reader.loads(json_writer.dumps(t))\n"
    ),
    "a runner's class in an annotation": "def f(t: subprocess.Popen[str]) -> subprocess.Popen[str]:\n    return t\n",
    "a runner's class in an annotated assignment": (
        "PROCESS: subprocess.Popen[str] | None = None\n\n\ndef f():\n"
        "    local: subprocess.Popen[bytes] | None = None\n    return local\n"
    ),
}


@pytest.mark.parametrize("body", OWNER_ONLY_ROWS.values(), ids=OWNER_ONLY_ROWS.keys())
def test_owner_only_policy_refuses_each_reference_outside_its_owners(body: str) -> None:
    refused, marked = restricted_rows(python_checks.owner_only_problems, body)
    assert refused == marked


@pytest.mark.parametrize(
    ("path", "body"),
    [
        pytest.param("skills/ejryan89-skill-core/scripts/json_writer.py", "WRITE = json.dumps\n", id="json_writer"),
        pytest.param("skills/ejryan89-skill-core/scripts/json_reader.py", "READ = json.loads\n", id="json_reader"),
        pytest.param(
            "skills/ejryan89-skill-core/scripts/job_tree.py", "LIB = ctypes.WinDLL('kernel32')\n", id="a ctypes owner"
        ),
        pytest.param(
            "skills/ejryan89-skill-core/scripts/bounded_process.py", "RUN = subprocess.Popen\n", id="bounded_process"
        ),
        pytest.param(
            "skills/ejryan89-skill-core/scripts/path_containment.py", "COMMON = os.path.commonpath\n", id="containment"
        ),
    ],
)
def test_owner_only_policy_reads_each_owner_as_free_to_reference_its_names(path: str, body: str) -> None:
    refused, marked = restricted_rows(python_checks.owner_only_problems, body, path)
    assert refused == marked == []


# Each policed call given an argument its readers cannot follow, refused, and each spelled form, read.
POLICED_CALL_ROWS = {
    "a ** of a mapping": "def f(p, options):\n    p.add_argument('--timeout', **options)  # read\n",
    "a ** of a mapping updated": (
        "def f(p, cfg):\n    options = {}\n    options.update(cfg)\n"
        "    p.add_argument('--timeout', **options)  # read\n"
    ),
    "a ** of a literal": "def f(p):\n    p.add_argument('--timeout', **{'type': float})  # read\n",
    "a * of option strings": "def f(p, names):\n    p.add_argument(*names, type=float)  # read\n",
    "an option string held in a name": (
        "OPTION = '--timeout'\n\n\ndef f(p):\n    p.add_argument(OPTION, type=float)  # read\n"
    ),
    "a type given by a parameter's default": (
        "def f(p, kind=float):\n    p.add_argument('--timeout', type=kind)  # read\n"
    ),
    "a type held by a base class": (
        "class A:\n    kind = float\n\n\nclass B(A):\n    def f(self, p):\n"
        "        p.add_argument('--timeout', type=self.kind)  # read\n"
    ),
    "a type by a lookup of a builtin": (
        "def f(p):\n    p.add_argument('--timeout', type=getattr(builtins, 'float'))  # read\n"
    ),
    "a type given by a lambda": "def f(p):\n    p.add_argument('--count', type=lambda t: int(t))  # read\n",
    "a default given by a parameter": "def f(p, wait):\n    p.add_argument('--wait', default=wait)  # read\n",
    "a dest held in a name": "def f(p, name):\n    p.add_argument('--x', dest=name)  # read\n",
    "a metavar held in a name": "def f(p, name):\n    p.add_argument('--x', metavar=name)  # read\n",
    "add_argument bound to a name": "def f(p):\n    add = p.add_argument  # read\n    add('--timeout', type=float)\n",
    "add_argument in a partial": "def f(p):\n    return functools.partial(p.add_argument, type=float)  # read\n",
    "add_argument by a literal lookup and bound": "def f(p):\n    return getattr(p, 'add_argument')  # read\n",
    "a ** into json.dumps": "def f(t, options):\n    return json.dumps(t, **options)  # read\n",
    "an encoder class given by a parameter": "def f(t, encoder):\n    return json.dumps(t, cls=encoder)  # read\n",
    "a ** into a process start": "def f(options):\n    return subprocess.run(['git'], **options)  # read\n",
    "a * into a process start": "def f(command):\n    return subprocess.run(*command)  # read\n",
    "each policed argument spelled": (
        "DEFAULT = 30.0\n\n\ndef f(p):\n    p.add_argument('--timeout', '-t', type=seconds_option.positive_seconds, "
        "default=DEFAULT, dest='timeout', metavar='SECONDS')\n"
        "    p.add_argument('--count', type=whole_number.argument(1, 10), default=-1)\n"
        "    p.add_argument('--name', type=str, default=('a', 'b'), choices=['a', 'b'])\n"
    ),
    "a json write given a module's default": "def f(t):\n    return json.dumps(t, default=str, sort_keys=True)\n",
    "a constant an augmented assignment rebinds": (
        "WAIT = 30\nWAIT += 1\n\n\ndef f(p):\n"
        "    p.add_argument('--wait', type=seconds_option.positive_seconds, default=WAIT)  # read\n"
    ),
    "a type a loop binds": (
        "for KIND in (float,):\n    pass\n\n\ndef f(p):\n    p.add_argument('--timeout', type=KIND)  # read\n"
    ),
    "a type a with binds": (
        "with open('x') as KIND:\n    pass\n\n\ndef f(p):\n    p.add_argument('--timeout', type=KIND)  # read\n"
    ),
    "a constant a function rebinds through global": (
        "WAIT = 30\n\n\ndef g(wait):\n    global WAIT\n    WAIT = wait\n\n\ndef f(p):\n"
        "    p.add_argument('--wait', type=seconds_option.positive_seconds, default=WAIT)  # read\n"
    ),
    "a constant an assignment expression binds": (
        "if (WAIT := 30):\n    pass\n\n\ndef f(p):\n"
        "    p.add_argument('--wait', type=seconds_option.positive_seconds, default=WAIT)  # read\n"
    ),
    "a constant bound to an operation on literals": (
        "WAIT = 30 * 2\n\n\ndef f(p):\n"
        "    p.add_argument('--wait', type=seconds_option.positive_seconds, default=WAIT)\n"
    ),
    "a constant bound to a dict of literals": (
        "WAIT = {'a': 1, 'b': (2, 3)}\n\n\ndef f(p):\n"
        "    p.add_argument('--wait', type=seconds_option.positive_seconds, default=WAIT)\n"
    ),
    "a constant bound to a dict that spreads another": (
        "OTHER = {'a': 1}\nWAIT = {**OTHER}\n\n\ndef f(p):\n"
        "    p.add_argument('--wait', type=seconds_option.positive_seconds, default=WAIT)  # read\n"
    ),
    "a constant bound to a literal twice": (
        "WAIT = 30\nWAIT = 60\n\n\ndef f(p):\n"
        "    p.add_argument('--wait', type=seconds_option.positive_seconds, default=WAIT)  # read\n"
    ),
}


@pytest.mark.parametrize("body", POLICED_CALL_ROWS.values(), ids=POLICED_CALL_ROWS.keys())
def test_policed_call_policy_refuses_each_argument_its_readers_cannot_follow(body: str) -> None:
    refused, marked = restricted_rows(python_checks.policed_call_problems, body)
    assert refused == marked


# The policies a spelling can cross, by the label a fixture line marks it with: `# refused: policed, time-option`
# marks a line those two refuse, and every other policy here must refuse nothing on it.
CROSSED_POLICIES: dict[str, Callable[[Path, list[Path]], list[str]]] = {
    "indirection": python_checks.indirection_problems,
    "owner-only": python_checks.owner_only_problems,
    "policed": python_checks.policed_call_problems,
    "time-option": python_checks.time_option_problems,
    "ctypes": python_checks.ctypes_declaration_problems,
    "console": lambda root, _files: skill_scripts.console_setup_problems(root),
}
CROSSED_MARK = "# refused:"


def crossed_rows(body: str, path: str) -> dict[str, tuple[list[str], list[str]]]:
    """For each policy CROSSED_POLICIES names, the lines it refuses in the module at `path` holding body after
    RESTRICTED_IMPORTS, and the lines the body marks with its label, both by their numbers."""
    source = RESTRICTED_IMPORTS + body
    lines = source.splitlines()
    marks = {
        label: sorted(
            str(number)
            for number, line in enumerate(lines, 1)
            if CROSSED_MARK in line and label in {part.strip() for part in line.rpartition(CROSSED_MARK)[2].split(",")}
        )
        for label in CROSSED_POLICIES
    }
    found: dict[str, tuple[list[str], list[str]]] = {}
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {path: source})
        files = sorted(root.rglob("*.py"))
        for label, policy in CROSSED_POLICIES.items():
            refused = sorted({problem.split(" ", 1)[0].rpartition(":")[2] for problem in policy(root, files)})
            found[label] = (refused, marks[label])
    return found


SCRIPT = "skills/alpha/scripts/alpha.py"
CTYPES_OWNER = "skills/ejryan89-skill-core/scripts/job_tree.py"
# Each spelling found that the shared resolver did not follow, and each form the review of its fix found,
# through every policy it crosses, so no policy reading the call needs to follow it: each is refused at the source by
# the policy its line names, and passes every other.
SUPERSEDED_RESOLVER_ROWS = [
    pytest.param(
        "def f(t):\n    get = operator.attrgetter('x')  # refused: indirection\n    return get(t)\n",
        SCRIPT,
        id="a bound attrgetter",
    ),
    pytest.param(
        "def f(p, kind=float):\n    p.add_argument('--timeout', type=kind)  # refused: policed, time-option\n",
        SCRIPT,
        id="a parameter's default as a type",
    ),
    pytest.param(
        "WRITE = lambda t, dumps=json.dumps: dumps(t)  # refused: owner-only\n",
        SCRIPT,
        id="a lambda's default as a writer",
    ),
    pytest.param(
        "class A:\n    TEXT = json.dumps  # refused: owner-only\n\n\nclass B(A):\n    def f(self, t):\n"
        "        return self.TEXT(t)\n",
        SCRIPT,
        id="a base class attribute holding a writer",
    ),
    pytest.param(
        "class A:\n    kind = float\n\n\nclass B(A):\n    def f(self, p):\n"
        "        p.add_argument('--timeout', type=self.kind)  # refused: policed\n",
        SCRIPT,
        id="a base class attribute holding a type, read through self",
    ),
    pytest.param(
        "class A:\n    kind = float\n\n\nclass B(A):\n    pass\n\n\ndef f(p):\n"
        "    p.add_argument('--timeout', type=B.kind)  # refused: policed\n",
        SCRIPT,
        id="a base class attribute holding a type, read through the subclass's name",
    ),
    pytest.param(
        "class A:\n    def __init__(self):\n        self.lib = ctypes.WinDLL('kernel32')  # refused: owner-only\n",
        SCRIPT,
        id="a base class attribute holding a library, outside the ctypes owners",
    ),
    pytest.param(
        "class A:\n    def __init__(self):\n        self.lib = ctypes.WinDLL('kernel32')\n\n\nclass B(A):\n"
        "    def close(self, handle):\n        return self.lib.CloseHandle(handle)  # refused: ctypes\n",
        CTYPES_OWNER,
        id="a base class attribute holding a library, inside a ctypes owner",
    ),
    pytest.param(
        "class A:\n    lib = ctypes.WinDLL('kernel32')\n\n\nclass B(A):\n    def close(self, handle):\n"
        "        return B.lib.CloseHandle(handle)  # refused: ctypes\n",
        CTYPES_OWNER,
        id="a base class's library read through the subclass's name, inside a ctypes owner",
    ),
    pytest.param(
        "class A:\n    lib = ctypes.WinDLL('kernel32')\n\n\nclass B(A):\n    def close(self, handle):\n"
        "        return super().lib.CloseHandle(handle)  # refused: ctypes\n",
        CTYPES_OWNER,
        id="a base class's library read through super()",
    ),
    pytest.param(
        "class A:\n    lib = ctypes.WinDLL('kernel32')\n\n\nclass B(A):\n    def close(self, handle):\n"
        "        return type(self).lib.CloseHandle(handle)  # refused: ctypes\n",
        CTYPES_OWNER,
        id="a base class's library read through type(self)",
    ),
    pytest.param(
        "class A:\n    lib = ctypes.WinDLL('kernel32')\n\n\nclass B(A):\n    def close(self, handle):\n"
        "        return self.__class__.lib.CloseHandle(handle)  # refused: ctypes\n",
        CTYPES_OWNER,
        id="a base class's library read through self.__class__",
    ),
    pytest.param(
        "class A:\n    lib = ctypes.WinDLL('kernel32')\n\n\nclass B(A):\n    def close(self, handle):\n"
        "        return [self][0].lib.CloseHandle(handle)  # refused: ctypes\n",
        CTYPES_OWNER,
        id="a base class's library read through a receiver no list names",
    ),
    pytest.param(
        "class A:\n    lib = ctypes.WinDLL('kernel32')\n\n    def close(self, handle):\n"
        "        A.lib.CloseHandle.argtypes = [ctypes.c_void_p]\n        A.lib.CloseHandle.restype = ctypes.c_int\n"
        "        return self.lib.CloseHandle(handle), cls_read(A.lib)\n\n\ndef cls_read(lib):\n    return lib\n",
        CTYPES_OWNER,
        id="a library read through self and its own class's name, read",
    ),
    pytest.param(
        "def f(p):\n    p.add_argument('--x', type=importlib.import_module('m'))  # refused: indirection, policed\n",
        SCRIPT,
        id="a module loaded by name as a type, in a module that imports importlib twice",
    ),
    pytest.param(
        "def f(t):\n    match os.path:\n"
        "        case object(commonpath=common) | object(commonprefix=common):  # refused: owner-only\n"
        "            return common(t)\n",
        SCRIPT,
        id="an owner-only callable an alternative of a class pattern binds",
    ),
    pytest.param(
        "def f(t):\n    match t:\n        case [object(commonpath=common)]:\n            return common\n"
        "        case {'k': object(commonpath=common)}:\n            return common\n"
        "        case object(object(commonpath=common)):\n            return common\n",
        SCRIPT,
        id="a sequence's, a mapping's, and a positional pattern's keyword, which read no subject's attribute",
    ),
    pytest.param(
        "def f(fix=getattr(sys.stdout, 'reconfigure')):\n    fix(encoding='cp1252')  # refused: console\n",
        # A tool, which the console policy reads whatever its folder holds, as it reads a skill's scripts beside its
        # SKILL.md.
        "tools/alpha.py",
        id="a stream's reconfigure as a parameter's default",
    ),
    pytest.param(
        "def close(handle, lib=ctypes.WinDLL('kernel32')):\n    return lib.CloseHandle(handle)  # refused: ctypes\n",
        CTYPES_OWNER,
        id="a library loaded as a parameter's default, inside a ctypes owner",
    ),
    pytest.param(
        "def f(t):\n    match os.path:\n        case object(commonpath=common) as p:  # refused: owner-only\n"
        "            return common(t), p\n",
        SCRIPT,
        id="an owner-only callable a captured class pattern binds",
    ),
    pytest.param(
        "KIND = 'x'\n\n\ndef f(p):\n    p.add_argument('--x', type=KIND())  # refused: policed\n",
        SCRIPT,
        id="a literal-bound constant called as a type",
    ),
    pytest.param(
        "def f(t):\n    match os.path:\n        case object(commonpath=common):  # refused: owner-only\n"
        "            return common(t)\n",
        SCRIPT,
        id="an owner-only callable a class pattern binds",
    ),
    pytest.param(
        "def f(t):\n    match builtins:  # refused: indirection\n"
        "        case object(getattr=look):  # refused: indirection\n            return look(t, 'x')\n",
        SCRIPT,
        id="a name lookup a class pattern binds",
    ),
    pytest.param(
        "def f(t):\n    match t:\n        case object(__dict__=namespace):  # refused: indirection\n"
        "            return namespace\n",
        SCRIPT,
        id="a namespace a class pattern binds",
    ),
    pytest.param(
        "def f(t):\n    match os:  # refused: indirection\n"
        "        case object(path=object(commonpath=common)):  # refused: owner-only\n"
        "            return common(t)\n",
        SCRIPT,
        id="an owner-only callable a nested class pattern binds",
    ),
    pytest.param(
        "def f(p, cfg):\n    options = {}\n    options.update(cfg)\n"
        "    p.add_argument('--timeout', **options)  # refused: policed\n",
        SCRIPT,
        id="an update a ** cannot read",
    ),
    pytest.param(
        "def f(p, cfg):\n    p.add_argument('--timeout', **{**cfg})  # refused: policed\n",
        SCRIPT,
        id="a ** of a spread",
    ),
    pytest.param(
        "def f(p, cfg):\n    p.add_argument('--timeout', **(cfg | {}))  # refused: policed\n",
        SCRIPT,
        id="a ** of a union",
    ),
    pytest.param(
        "def f(t, s):\n    for chunk in json.JSONEncoder().iterencode(t):  # refused: owner-only\n"
        "        s.write(chunk)\n",
        SCRIPT,
        id="encoder text a loop takes",
    ),
    pytest.param(
        "OPTION = '--timeout'\n\n\ndef f(p):\n    p.add_argument(OPTION, type=float)  # refused: policed\n",
        SCRIPT,
        id="an option string held in a name",
    ),
    pytest.param(
        "def f(t):\n    m = json  # refused: indirection\n    return m.loads(t)  # refused: owner-only\n",
        SCRIPT,
        id="a module bound to a local name",
    ),
    pytest.param(
        "def f(t):\n    return operator.attrgetter('loads')(json)(t)  # refused: indirection\n",
        SCRIPT,
        id="an attrgetter of a parser",
    ),
    pytest.param(
        "def f():\n    object.__getattribute__(sys.stdout, 'reconfigure')(encoding='utf-8')  # refused: indirection\n",
        SCRIPT,
        id="a stream reconfigured through __getattribute__",
    ),
    pytest.param(
        "def f(name):\n    operator.attrgetter(name)(sys.stdout)(encoding='utf-8')  # refused: indirection\n",
        SCRIPT,
        id="a stream reconfigured through a computed attrgetter",
    ),
    pytest.param(
        "def f(p):\n    p.add_argument('--timeout', type=getattr(builtins, 'float'))  # refused: policed\n",
        SCRIPT,
        id="a getattr of a builtin as a type",
    ),
    pytest.param(
        "KIND = getattr(builtins, 'float')\n\n\ndef f(p):\n"
        "    p.add_argument('--timeout', type=KIND)  # refused: policed\n",
        SCRIPT,
        id="a module-level getattr of a builtin as a type",
    ),
    pytest.param(
        "KIND = float\n\n\ndef f(p):\n    p.add_argument('--timeout', type=KIND)  # refused: policed, time-option\n",
        SCRIPT,
        id="a module-level alias of a builtin as a type",
    ),
    pytest.param(
        "WAIT = float('inf')\n\n\ndef f(p):\n"
        "    p.add_argument('--wait', type=seconds_option.positive_seconds, default=WAIT)  # refused: policed\n",
        SCRIPT,
        id="a time option's default bound to a call",
    ),
    pytest.param(
        "WAIT = 30\n\n\ndef f(p):\n"
        "    p.add_argument('--wait', type=seconds_option.positive_seconds, default=WAIT)\n"
        "    p.add_argument('--count', type=whole_number.argument(1, WAIT), default=WAIT)\n",
        SCRIPT,
        id="a literal-bound constant, read",
    ),
    pytest.param(
        "async def f(loop):\n    return await loop.subprocess_exec('git')  # refused: owner-only\n",
        SCRIPT,
        id="an event loop's subprocess_exec",
    ),
    pytest.param(
        "async def f(loop, options):\n"
        "    return await loop.subprocess_shell('git', **options)  # refused: owner-only, policed\n",
        SCRIPT,
        id="a ** into an event loop's subprocess_shell",
    ),
]


@pytest.mark.parametrize(("body", "path"), SUPERSEDED_RESOLVER_ROWS)
def test_each_spelling_the_resolver_did_not_follow_is_refused_at_the_source(body: str, path: str) -> None:
    for label, (refused, marked) in crossed_rows(body, path).items():
        assert (label, refused) == (label, marked)


RESTRICTING_POLICIES = [
    pytest.param(python_checks.indirection_problems, id="rule 1"),
    pytest.param(python_checks.owner_only_problems, id="rule 2"),
    pytest.param(python_checks.policed_call_problems, id="rule 3"),
]


@pytest.mark.parametrize("policy", RESTRICTING_POLICIES)
@pytest.mark.parametrize(
    "content",
    [pytest.param(b"def f(:\n", id="not Python"), pytest.param(b"x = '\xff\xfe'\n", id="not UTF-8")],
)
def test_each_restricting_policy_names_a_module_it_cannot_read(
    policy: Callable[[Path, list[Path]], list[str]], content: bytes
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        module = root / "skills" / "alpha" / "scripts" / "alpha.py"
        module.parent.mkdir(parents=True)
        module.write_bytes(content)
        problems = policy(root, [module])
    assert len(problems) == 1, problems
    assert problems[0].startswith("skills/alpha/scripts/alpha.py "), problems


@pytest.mark.parametrize("policy", RESTRICTING_POLICIES)
def test_each_restricting_policy_refuses_a_listing_with_no_module_to_read(
    policy: Callable[[Path, list[Path]], list[str]],
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        assert policy(root, []) == [
            f"{root.as_posix()}: the listing holds no module the claim policies read, so none was checked"
        ]


# The foreign function a ctypes library hands out by item lookup is refused, inside the owners too.
def test_ctypes_policy_refuses_a_foreign_function_reached_by_subscript() -> None:
    body = (
        "from ctypes import wintypes\n\n\ndef f(t):\n    kernel32 = ctypes.WinDLL('kernel32')\n"
        "    function = kernel32.OpenProcess\n    function.argtypes = [wintypes.DWORD]\n"
        "    function.restype = wintypes.HANDLE\n    return kernel32['OpenProcess'](t)  # read\n"
    )
    refused, marked = restricted_rows(
        python_checks.ctypes_declaration_problems, body, "skills/ejryan89-skill-core/scripts/job_tree.py"
    )
    assert refused == marked


# A library a base class binds in another module is held in the module that subclasses it, through one import or
# through a base of a base, so a subclass's read of it is refused as a same-module one is.
CTYPES_BASE_OWNER = """import ctypes
from ctypes import wintypes


class Handles:
    def __init__(self):
        self.lib = ctypes.WinDLL("kernel32")
        self.lib.CloseHandle.argtypes = [wintypes.HANDLE]
        self.lib.CloseHandle.restype = wintypes.BOOL
        self.name = "kernel32"

    def close(self, handle):
        return self.lib.CloseHandle(handle)
"""


@pytest.mark.parametrize(
    ("modules", "refused"),
    [
        pytest.param(
            {
                "skills/alpha/scripts/alpha.py": "from job_tree import Handles\n\n\nclass B(Handles):\n"
                "    def open(self, pid):\n        return self.lib.OpenProcess(pid)\n"
            },
            ["skills/alpha/scripts/alpha.py:6"],
            id="a base imported by name",
        ),
        pytest.param(
            {
                "skills/alpha/scripts/alpha.py": "import job_tree\n\n\nclass B(job_tree.Handles):\n"
                "    def open(self, pid):\n        return self.lib.OpenProcess(pid)\n"
            },
            ["skills/alpha/scripts/alpha.py:6"],
            id="a base read off its imported module",
        ),
        pytest.param(
            {
                "skills/alpha/scripts/middle.py": (
                    "from job_tree import Handles\n\n\nclass Middle(Handles):\n    pass\n"
                ),
                "skills/alpha/scripts/alpha.py": "from middle import Middle\n\n\nclass B(Middle):\n"
                "    def open(self, pid):\n        return self.lib.OpenProcess(pid)\n",
            },
            ["skills/alpha/scripts/alpha.py:6"],
            id="a base of a base, each in its own module",
        ),
        pytest.param(
            {
                "skills/alpha/scripts/layers.py": (
                    "from job_tree import Handles\n\n\nclass Base(Handles):\n    pass\n\n\n"
                    "class Middle(Base):\n    pass\n"
                ),
                "skills/alpha/scripts/alpha.py": "from layers import Middle\n\n\nclass B(Middle):\n"
                "    def open(self, pid):\n        return self.lib.OpenProcess(pid)\n",
            },
            ["skills/alpha/scripts/alpha.py:6"],
            id="a chain of bases inside the other module",
        ),
        pytest.param(
            {
                "tools/owner.py": CTYPES_BASE_OWNER,
                "skills/alpha/scripts/alpha.py": "from tools.owner import Handles\n\n\nclass B(Handles):\n"
                "    def open(self, pid):\n        return self.lib.OpenProcess(pid)\n",
            },
            ["skills/alpha/scripts/alpha.py:6"],
            id="a base imported from a package's module",
        ),
        pytest.param(
            {
                "skills/alpha/scripts/middle.py": "from job_tree import Handles\n",
                "skills/alpha/scripts/alpha.py": "from middle import Handles\n\n\nclass B(Handles):\n"
                "    def open(self, pid):\n        return self.lib.OpenProcess(pid)\n",
            },
            ["skills/alpha/scripts/alpha.py:6"],
            id="a base a third module only re-exports",
        ),
        pytest.param(
            {
                "skills/alpha/scripts/middle.py": "import job_tree\n\nRenamed = job_tree.Handles\n",
                "skills/alpha/scripts/alpha.py": "import middle\n\n\nclass B(middle.Renamed):\n"
                "    def open(self, pid):\n        return self.lib.OpenProcess(pid)\n",
            },
            ["skills/alpha/scripts/alpha.py:6"],
            id="a base a third module binds to another name",
        ),
        pytest.param(
            {
                "skills/alpha/scripts/nested.py": "import ctypes\n\n\nclass Outer:\n    class Handles:\n"
                "        def __init__(self):\n            self.lib = ctypes.WinDLL('kernel32')\n",
                "skills/alpha/scripts/middle.py": "from nested import Outer\n",
                "skills/alpha/scripts/alpha.py": "import middle\n\n\nclass B(middle.Outer.Handles):\n"
                "    def open(self, pid):\n        return self.lib.OpenProcess(pid)\n",
            },
            ["skills/alpha/scripts/alpha.py:6"],
            id="a nested class of a re-exported class",
        ),
        pytest.param(
            {
                "tools/kit/__init__.py": CTYPES_BASE_OWNER,
                "skills/alpha/scripts/alpha.py": "from kit import Handles\n\n\nclass B(Handles):\n"
                "    def open(self, pid):\n        return self.lib.OpenProcess(pid)\n",
            },
            ["skills/alpha/scripts/alpha.py:6"],
            id="a base a package's __init__ defines",
        ),
        pytest.param(
            {
                "skills/alpha/scripts/first.py": "from second import Thing\n",
                "skills/alpha/scripts/second.py": "from first import Thing\n",
                "skills/alpha/scripts/alpha.py": "from first import Thing\n\n\nclass B(Thing):\n"
                "    def open(self, pid):\n        return self.lib\n",
            },
            [],
            id="a cycle of re-exports",
        ),
        pytest.param(
            {
                "skills/alpha/scripts/middle.py": "from job_tree import close\n",
                "skills/alpha/scripts/alpha.py": "from middle import close\n\n\nclass B(close):\n"
                "    def open(self, pid):\n        return self.lib\n",
            },
            [],
            id="a re-export of a name no module defines as a class",
        ),
        pytest.param(
            {
                "skills/alpha/scripts/nested.py": "import ctypes\n\n\nclass Outer:\n    class Handles:\n"
                "        def __init__(self):\n            self.lib = ctypes.WinDLL('kernel32')\n",
                "skills/alpha/scripts/alpha.py": "from nested import Outer\n\n\nclass B(Outer.Handles):\n"
                "    def open(self, pid):\n        return self.lib.OpenProcess(pid)\n",
            },
            ["skills/alpha/scripts/alpha.py:6"],
            id="a nested class as the base",
        ),
        pytest.param(
            {
                "skills/alpha/scripts/alpha.py": "from job_tree import Handles\n\n\nclass Other:\n    pass\n\n\n"
                "class B(Handles, Other):\n    def open(self, pid):\n        return self.lib.OpenProcess(pid)\n"
            },
            ["skills/alpha/scripts/alpha.py:10"],
            id="the library's base before another",
        ),
        pytest.param(
            {
                "skills/alpha/scripts/alpha.py": "from job_tree import Handles\n\n\nclass B(Handles):\n"
                "    def label(self):\n        return self.name\n"
            },
            [],
            id="an attribute the base binds to no library",
        ),
        pytest.param(
            {
                "skills/alpha/scripts/alpha.py": "from elsewhere import Thing\n\n\nclass B(Thing):\n"
                "    def open(self, pid):\n        return self.lib\n"
            },
            [],
            id="a base from a module the policy does not read",
        ),
        pytest.param(
            {
                "skills/alpha/scripts/first.py": "from second import B\n\n\nclass A(B):\n    pass\n",
                "skills/alpha/scripts/second.py": "from first import A\n\n\nclass B(A):\n    def f(self):\n"
                "        return self.lib\n",
            },
            [],
            id="a cycle of bases across modules",
        ),
        pytest.param(
            {
                "skills/alpha/scripts/alpha.py": "from job_tree import Other\n\n\nclass B(Other):\n"
                "    def open(self, pid):\n        return self.lib\n"
            },
            [],
            id="a base the other module does not define",
        ),
    ],
)
def test_ctypes_policy_refuses_a_library_a_base_class_binds_in_another_module(
    modules: dict[str, str], refused: list[str]
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"skills/ejryan89-skill-core/scripts/job_tree.py": CTYPES_BASE_OWNER, **modules})
        found = python_checks.ctypes_declaration_problems(root, sorted(root.rglob("*.py")))
    assert [problem.split(" ", 1)[0] for problem in found] == refused
    assert all("other than through the class that binds it" in problem for problem in found), found


def test_ctypes_policy_names_a_module_it_cannot_read() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"skills/alpha/scripts/alpha.py": "def broken(:\n"})
        found = python_checks.ctypes_declaration_problems(root, sorted(root.rglob("*.py")))
    assert [problem.startswith("skills/alpha/scripts/alpha.py") for problem in found] == [True], found


@pytest.mark.parametrize(
    ("policy", "allowance", "body"),
    [
        pytest.param(python_checks.indirection_problems, "INDIRECTION_ALLOWED", "    return vars(t)\n", id="rule 1"),
        pytest.param(python_checks.owner_only_problems, "OWNER_ONLY_ALLOWED", "    return json.dumps\n", id="rule 2"),
        pytest.param(
            python_checks.policed_call_problems, "POLICED_CALL_ALLOWED", "    t.add_argument('--x', **t)\n", id="rule 3"
        ),
    ],
)
def test_indirection_policies_take_a_sanction_and_report_a_stale_one(
    policy: Callable[[Path, list[Path]], list[str]], allowance: str, body: str
) -> None:
    source = (
        f"{allowance} = {{'kept': 'a reason', 'stale': 'a reason'}}\n\n\ndef kept(t):\n{body}\n\n\ndef bare(t):\n{body}"
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"skills/alpha/scripts/alpha.py": RESTRICTED_IMPORTS + source})
        found = policy(root, sorted(root.rglob("*.py")))
    bare = (RESTRICTED_IMPORTS + source).splitlines().index("def bare(t):") + 2
    assert [problem.split(" ", 1)[0] for problem in found] == [
        f"skills/alpha/scripts/alpha.py:{bare}",
        "skills/alpha/scripts/alpha.py:",
    ]
    assert f"{allowance} allows stale" in found[1]


def test_indirection_catalogues_hold_each_construct() -> None:
    # Pinned against literals, not the constants under test.
    assert {
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "builtins.getattr",
        "builtins.setattr",
        "builtins.delattr",
        "builtins.hasattr",
    } == set(validation_support.NAME_LOOKUPS)
    assert {
        "vars",
        "globals",
        "locals",
        "eval",
        "exec",
        "builtins.vars",
        "builtins.globals",
        "builtins.locals",
        "builtins.eval",
        "builtins.exec",
        "__import__",
        "builtins.__import__",
        "__builtins__",
        "importlib.import_module",
        "importlib.__import__",
        "importlib.reload",
        "importlib.util.spec_from_file_location",
        "importlib.util.module_from_spec",
        "importlib.machinery.SourceFileLoader",
        "runpy.run_path",
        "runpy.run_module",
        "pkgutil.resolve_name",
        "operator.attrgetter",
        "operator.methodcaller",
        "inspect.getattr_static",
        "inspect.getmembers",
        "inspect.getmembers_static",
        "inspect.currentframe",
        "sys._getframe",
        "sys.modules",
        *STANDARD_RESOLVERS,
    } == set(validation_support.INDIRECTION_CALLABLES)
    assert {
        "__dict__",
        "__getattribute__",
        "__getattr__",
        "__setattr__",
        "__delattr__",
        "__globals__",
        "__builtins__",
        "f_globals",
        "f_locals",
        "f_builtins",
        "__subclasses__",
        "entry_points",
    } == set(validation_support.INDIRECTION_ATTRIBUTES)
    assert {"__getattr__", "__getattribute__"} == set(validation_support.LOOKUP_HOOKS)
    assert {
        "skills/ejryan89-skill-core/scripts/job_tree.py",
        "skills/ejryan89-skill-core/scripts/process_identity.py",
        "skills/ejryan89-code-review-core/scripts/review_io.py",
        "skills/ejryan89-code-review-core/scripts/review_runtime.py",
    } == set(python_checks.CTYPES_OWNERS)
    owners = python_checks.OWNER_ONLY_NAMES
    assert (
        {"skills/ejryan89-skill-core/scripts/json_writer.py"} == set(owners["json.dumps"]) == set(owners["json.dump"])
    )
    assert {"skills/ejryan89-skill-core/scripts/bounded_process.py"} == set(owners["subprocess.run"])
    assert {"skills/ejryan89-skill-core/scripts/path_containment.py"} == set(owners["os.path.commonpath"])
    reader = {"skills/ejryan89-skill-core/scripts/json_reader.py"}
    assert reader == set(owners["json.loads"]) == set(owners["json.load"]) == set(owners["json.JSONDecoder"])

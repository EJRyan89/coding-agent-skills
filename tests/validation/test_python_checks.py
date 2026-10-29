"""Fixture tests for tests/validation/python_checks.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import copy
import importlib
import json
import re
import sys
import tempfile
import unittest
from collections.abc import Callable
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
    git_separator_problems,
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

        def record(arguments: list[str], cwd: Path) -> None:
            cache = Path(arguments[arguments.index("--cache-dir") + 1])
            seen.append((cache, cache.is_dir()))

        with (
            mock.patch.dict("os.environ", {"VALIDATION_MYPY_CACHE": ""}),
            mock.patch.object(python_checks, "find_mypy", return_value="mypy"),
            mock.patch.object(python_checks, "run_process", side_effect=record),
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
                    python_checks, "run_process", side_effect=lambda arguments, cwd: commands.append(arguments)
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
        *(f"{alpha}:{line} writes json.dumps text to a file or a stream{remedy}" for line in (12, 13, 16)),
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
        found = python_checks.content_error_problems(root, sorted(root.rglob("*.py")))
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


def test_content_errors_policy_refuses_a_tree_whose_skill_core_names_no_reader() -> None:
    # With no reader every handler would pass, so a tree that lost its readers fails rather than passing empty.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"skills/alpha/scripts/alpha.py": CONTENT_CALLER_SOURCE})
        found = python_checks.content_error_problems(root, sorted(root.rglob("*.py")))
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
}
STATUS_IMPORTS = "from git_client import GitClient\nfrom support import git\nfrom again import git as twice\n\n\n"


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
    ],
)
def test_git_status_policy_passes_what_reads_no_git_status(body: str) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {**STATUS_REEXPORTS, "skills/alpha/scripts/alpha.py": STATUS_IMPORTS + body})
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
    return result


def sanctioned(result):
    result.add_argument("--seconds", type=float)


TIME_OPTION_ALLOWED = {"sanctioned": "a reason", "stale": "a reason"}
"""


def test_time_option_policy_refuses_a_float_time_option() -> None:
    alpha = "skills/alpha/scripts/alpha.py"
    found = run_claim_policy(python_checks.time_option_problems, "TIME_OPTION_ALLOWED", TIME_OPTION_SOURCE, alpha)
    remedy = (
        "with type=float, which accepts nan, inf, 0, and a negative number; use ejryan89-skill-core's "
        "seconds_option.positive_seconds, or state why in TIME_OPTION_ALLOWED"
    )
    assert found == [
        f"{alpha}:8 reads --timeout {remedy}",
        f"{alpha}:9 reads --read-timeout {remedy}",
        f"{alpha}:10 reads SECONDS {remedy}",
        f"{alpha}:11 reads limit_minutes {remedy}",
        f"{alpha}:12 reads --max_hours {remedy}",
        f"{alpha}: TIME_OPTION_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
        "tools/broken.py: TIME_OPTION_ALLOWED must map each function, class, or assignment to its reason",
    ]


# Each claim policy's forms its paragraph in "Validation" in docs/adding-a-skill.md names, beyond the plain one: a
# module in a skill's scripts beside ejryan89-skill-core's fixture reader, whose line marked `# read` is refused.
CLAIM_FORMS: list[tuple[str, Callable[[Path, list[Path]], list[str]], str]] = [
    # "directly or through a repository function that lets the reader's errors escape"
    (
        "a reader bound to a module-level name",
        python_checks.content_error_problems,
        "import reader\n\nREAD = reader.read\n\n\ndef f(text):\n    try:  # read\n        return READ(text)\n"
        "    except OSError:\n        return None\n",
    ),
    (
        "a reader called through a module-level lambda",
        python_checks.content_error_problems,
        "import reader\n\nREAD = lambda text: reader.read(text)\n\n\ndef f(text):\n    try:  # read\n"
        "        return READ(text)\n    except OSError:\n        return None\n",
    ),
    (
        "a reader called through a method of a class in the module",
        python_checks.content_error_problems,
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

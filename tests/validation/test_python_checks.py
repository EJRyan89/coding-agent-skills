"""Fixture tests for tests/validation/python_checks.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import copy
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import get_args
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
    issue_numbers_in_comments,
    mypy_path_problems,
    mypy_type_check,
    noqa_without_reason,
    python_suppression_problems,
    ratchet_problems,
    ruff_format_check,
    ruff_lint_check,
    type_check_skill_roots,
    type_check_skipped_problems,
    type_ignore_without_reason,
)
from toolchain import PREREQUISITES, find_mypy, missing_prerequisites
from validation_support import REPOSITORY_ROOT, SKILLS_ROOT, relative, write_fixture_tree

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
            ["  - mypy: python -m pip install -r requirements-dev.txt"],
            missing_prerequisites((("mypy", "mypy", lambda: None),)),
        )
        with (
            mock.patch.object(python_checks, "find_mypy", return_value=None),
            self.assertRaises(AssertionError) as raised,
        ):
            mypy_type_check(REPOSITORY_ROOT, ["deploy.py"], REPOSITORY_ROOT / "pyproject.toml")
        self.assertIn("mypy was not found: python -m pip install -r requirements-dev.txt", str(raised.exception))

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

    def test_type_check_runs_once_per_root(self) -> None:
        configuration = REPOSITORY_ROOT / "pyproject.toml"
        with mock.patch.object(python_checks, "mypy_type_check") as check:
            jobs = {job.name: job for job in all_jobs()}
            core = "static type check (mypy deployer, tools, deploy.py, tests, conftest.py)"
            self.assertIn(core, jobs)
            jobs[core].run()
            check.assert_called_once_with(
                REPOSITORY_ROOT, ["deployer", "tools", "deploy.py", "tests", "conftest.py"], configuration
            )
            # Each skill whose scripts/ holds a module is its own root, checked from inside it as the skill imports.
            roots = type_check_skill_roots()
            self.assertIn(SKILLS_ROOT / "ejryan89-code-review-core" / "scripts", roots)
            self.assertNotIn(SKILLS_ROOT / "update-coding-agent-skills" / "scripts", roots)
            for root in roots:
                name = f"static type check (mypy {relative(root)})"
                with self.subTest(root=name):
                    check.reset_mock()
                    jobs[name].run()
                    check.assert_called_once_with(root, ["."], configuration)

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
        lowered = {table: {key: ceiling - 1 for key, ceiling in keys.items()} for table, keys in CEILINGS.items()}
        self.assertEqual([], ratchet_problems(lowered, held[1:]))
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
        self.assertIn("ruff was not found: python -m pip install -r requirements-dev.txt", str(raised.exception))

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

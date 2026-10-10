"""Fixture tests for tests/validation/python_checks.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import python_checks
from job_selection import all_jobs
from python_checks import (
    FORMAT_ROOTS,
    ceiling_noqa,
    comments,
    issue_numbers_in_comments,
    mypy_path_problems,
    mypy_type_check,
    noqa_without_reason,
    python_suppression_problems,
    ruff_format_check,
    ruff_lint_check,
    type_check_skill_roots,
    type_ignore_without_reason,
)
from toolchain import PREREQUISITES, find_mypy, missing_prerequisites
from validation_support import REPOSITORY_ROOT, SKILLS_ROOT, relative, write_fixture_tree

from deployer import platform_support


class PythonChecksFixtures(unittest.TestCase):
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
        self.assertEqual(("deployer", "tools", "tests", "skills", ".claude/skills", "deploy.py"), FORMAT_ROOTS)
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

    def test_lint_check_covers_every_python_root(self) -> None:
        jobs = {job.name: job for job in all_jobs()}
        self.assertIn("static lint check (ruff check)", jobs)
        with mock.patch.object(python_checks, "ruff_lint_check") as lint:
            jobs["static lint check (ruff check)"].run()
        lint.assert_called_once_with(
            REPOSITORY_ROOT, ["deployer", "tools", "tests", "skills", ".claude/skills", "deploy.py"]
        )

    def test_missing_mypy_is_reported_with_the_install_command(self) -> None:
        self.assertIn(("mypy", "mypy", find_mypy), PREREQUISITES)
        with (
            mock.patch.object(shutil, "which", return_value=None),
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
            core = "static type check (mypy deployer, tools, deploy.py, tests)"
            self.assertIn(core, jobs)
            jobs[core].run()
            check.assert_called_once_with(REPOSITORY_ROOT, ["deployer", "tools", "deploy.py", "tests"], configuration)
            # Each skill whose scripts/ holds a module is its own root, checked from inside it as the skill imports.
            roots = type_check_skill_roots()
            self.assertIn(SKILLS_ROOT / "code-review-core" / "scripts", roots)
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
            # A category holds skills, as deployer/source.py reads skills/<category>/<name>.
            "skills/group/inner/SKILL.md": "",
            "skills/group/inner/scripts/run.py": "",
            # A repository skill's scripts are checked like a shipped skill's.
            ".claude/skills/local/SKILL.md": "",
            ".claude/skills/local/scripts/run.py": "",
            # A folder without SKILL.md is no skill.
            "skills/loose/scripts/run.py": "",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, files)
            self.assertEqual(
                [
                    root / "skills/group/inner/scripts",
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
            (root / "skills" / "user" / "scripts" / "user.py").write_text(sibling, encoding="utf-8")
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
                    "pyproject.toml: [tool.mypy] mypy_path lacks $MYPY_CONFIG_FILE_DIR/skills/suite-only/scripts, "
                    "which skills/user/scripts/test_user.py puts on sys.path",
                    "pyproject.toml: [tool.mypy] mypy_path lacks $MYPY_CONFIG_FILE_DIR/tests/suites, which "
                    "tests/suites/test_a.py imports a module from by its bare name",
                    "pyproject.toml: [tool.mypy] mypy_path names $MYPY_CONFIG_FILE_DIR/skills/stale/scripts, which "
                    "no module puts on sys.path",
                ],
                mypy_path_problems(root, sorted(root.rglob("*"))),
            )


if __name__ == "__main__":
    unittest.main()

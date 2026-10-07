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
import validation_support
from job_selection import all_jobs
from python_checks import (
    FORMAT_ROOTS,
    mypy_type_check,
    noqa_without_reason,
    ruff_format_check,
    ruff_lint_check,
    type_check_skill_roots,
    type_ignore_without_reason,
)
from toolchain import PREREQUISITES, find_mypy, missing_prerequisites
from validation_support import REPOSITORY_ROOT, SKILLS_ROOT, relative

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
        self.assertEqual(("deployer", "tools", "tests", "skills", "deploy.py"), FORMAT_ROOTS)
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
        lint.assert_called_once_with(REPOSITORY_ROOT, ["deployer", "tools", "tests", "skills", "deploy.py"])

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

    def test_type_check_roots_include_a_skill_whose_scripts_are_all_suites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skills = Path(temporary)
            for name, files in {"module": ["run.py"], "suites": ["test_run.py"], "shell": ["test_run.sh"]}.items():
                (skills / name / "scripts").mkdir(parents=True)
                for file in files:
                    (skills / name / "scripts" / file).write_text("", encoding="utf-8")
            (skills / "none").mkdir()
            with mock.patch.object(validation_support, "SKILLS_ROOT", skills):
                self.assertEqual(
                    [skills / "module" / "scripts", skills / "suites" / "scripts"], type_check_skill_roots()
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

    def test_missing_ruff_fails_the_lint_check_with_the_install_command(self) -> None:
        with (
            mock.patch.object(python_checks, "find_ruff", return_value=None),
            self.assertRaises(AssertionError) as raised,
        ):
            ruff_lint_check(REPOSITORY_ROOT, ["deploy.py"])
        self.assertIn("ruff was not found: python -m pip install -r requirements-dev.txt", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

"""Regression tests for the type-check roots, the strict list's reader, and the type-check jobs' names."""

from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import override

sys.path.insert(0, str(Path(__file__).resolve().parent))

import type_check_roots
from type_check_roots import (
    every_job_reasons,
    file_type_check_jobs,
    imported_folders,
    repository_skill_directories,
    shared_job_name,
    skill_directories,
    skill_job_name,
    skill_script_directories,
    strict_job_name,
    strict_type_check_areas,
    type_check_root,
    type_check_skill_roots,
)

# The job names continuous integration runs, as literals, so a change to a builder fails here rather than renaming a
# job whose recorded seconds weigh it.
SHARED_JOB = "static type check (mypy deployer, tools, deploy.py, tests, conftest.py)"
TREE = {
    "deployer/__init__.py": "",
    "deployer/plan.py": "",
    "tools/gate.py": "",
    "tests/tools/test_gate.py": "",
    "deploy.py": "",
    "conftest.py": "",
    "skills/alpha/SKILL.md": "",
    "skills/alpha/scripts/run.py": "",
    "skills/alpha/scripts/test_run.py": "",
    "skills/shell/SKILL.md": "",
    "skills/shell/scripts/run.sh": "",
    ".claude/skills/local/SKILL.md": "",
    ".claude/skills/local/scripts/run.py": "",
    "docs/example.py": "",
}


def write_tree(root: Path, files: Mapping[str, str]) -> None:
    for name, text in files.items():
        path = root.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class Fixture(unittest.TestCase):
    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        write_tree(self.root, TREE)


class JobNameTests(unittest.TestCase):
    def test_each_job_keeps_the_name_continuous_integration_runs_it_under(self) -> None:
        self.assertEqual(SHARED_JOB, shared_job_name())
        self.assertEqual("static type check (mypy --strict tools)", strict_job_name("tools"))
        self.assertEqual(
            "static type check (mypy .claude/skills/local/scripts)", skill_job_name(".claude/skills/local/scripts")
        )

    def test_the_roots_checked_together_are_the_repository_s_top_level_python(self) -> None:
        self.assertEqual(("deployer", "tools", "deploy.py", "tests", "conftest.py"), type_check_roots.TYPE_CHECK_ROOTS)


class SkillRootTests(Fixture):
    def test_shipped_and_repository_skills_are_found_and_a_scripts_folder_without_python_is_no_root(self) -> None:
        self.assertEqual([self.root / "skills/alpha", self.root / "skills/shell"], skill_directories(self.root))
        self.assertEqual([self.root / ".claude/skills/local"], repository_skill_directories(self.root))
        self.assertEqual(
            [
                self.root / "skills/alpha/scripts",
                self.root / "skills/shell/scripts",
                self.root / ".claude/skills/local/scripts",
            ],
            skill_script_directories(self.root),
        )
        self.assertEqual(
            [self.root / "skills/alpha/scripts", self.root / ".claude/skills/local/scripts"],
            type_check_skill_roots(self.root),
        )

    def test_a_file_is_checked_from_its_skill_s_scripts_or_the_top_root_holding_it(self) -> None:
        skills = type_check_skill_roots(self.root)
        self.assertEqual(
            self.root / "skills/alpha/scripts",
            type_check_root(self.root, self.root / "skills/alpha/scripts/run.py", skills),
        )
        self.assertEqual(
            self.root / "tests", type_check_root(self.root, self.root / "tests/tools/test_gate.py", skills)
        )
        self.assertEqual(self.root / "deploy.py", type_check_root(self.root, self.root / "deploy.py", skills))
        self.assertIsNone(type_check_root(self.root, self.root / "docs/example.py", skills))


class StrictListTests(unittest.TestCase):
    def read(self, text: str) -> list[str] | None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.toml"
            path.write_text(text, encoding="utf-8")
            return strict_type_check_areas(path)

    def test_the_list_is_read_from_the_file_it_is_given(self) -> None:
        self.assertEqual(
            ["tools", "skills/alpha/scripts"],
            self.read('[tool.validation]\nmypy-strict = ["tools", "skills/alpha/scripts"]\n'),
        )

    def test_a_list_missing_or_holding_anything_but_paths_is_none(self) -> None:
        for text in (
            "",
            "[tool.validation]\n",
            '[tool.validation]\nmypy-strict = "tools"\n',
            '[tool.validation]\nmypy-strict = ["tools", 1]\n',
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.read(text))

    def test_a_file_that_is_not_toml_raises_tomllib_s_error(self) -> None:
        with self.assertRaises(tomllib.TOMLDecodeError):
            self.read("[tool.validation\n")


class FileJobTests(Fixture):
    def jobs(self, *paths: str, strict: tuple[str, ...] = ("tools", "tests", "deployer")) -> list[str]:
        return file_type_check_jobs(self.root, paths, strict)

    def test_a_module_and_its_suite_reach_the_shared_run_and_each_root_s_strict_run(self) -> None:
        self.assertEqual(
            [SHARED_JOB, "static type check (mypy --strict tools)", "static type check (mypy --strict tests)"],
            self.jobs("tools/gate.py", "tests/tools/test_gate.py"),
        )

    def test_a_root_the_strict_list_leaves_out_reaches_the_shared_run_alone(self) -> None:
        self.assertEqual([SHARED_JOB], self.jobs("deploy.py", "conftest.py"))
        self.assertEqual([SHARED_JOB], self.jobs("deployer/plan.py", strict=()))

    def test_a_skill_s_scripts_reach_its_own_run_only(self) -> None:
        self.assertEqual(
            ["static type check (mypy skills/alpha/scripts)", "static type check (mypy .claude/skills/local/scripts)"],
            self.jobs(
                "skills/alpha/scripts/test_run.py", ".claude/skills/local/scripts/run.py", "skills/alpha/scripts/run.py"
            ),
        )

    def test_a_file_no_type_check_reads_reaches_no_job(self) -> None:
        # A file that is not Python, Python outside every root, and a folder of shell scripts alone reach nothing.
        self.assertEqual(
            [], self.jobs("skills/alpha/SKILL.md", "docs/example.py", "skills/shell/scripts/run.sh", "README.md")
        )

    def test_a_removed_module_still_reaches_the_jobs_of_the_root_it_sat_in(self) -> None:
        # The files a change touches include those it deletes, whose importers the same runs check.
        self.assertEqual(
            [SHARED_JOB, "static type check (mypy --strict deployer)", "static type check (mypy skills/alpha/scripts)"],
            self.jobs("deployer/removed.py", "skills/alpha/scripts/removed.py"),
        )


class EveryJobTests(unittest.TestCase):
    def folders(self, text: str) -> list[str] | None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.toml"
            path.write_text(text, encoding="utf-8")
            return imported_folders(path)

    def test_the_imported_folders_are_the_packages_then_each_mypy_path_folder(self) -> None:
        self.assertEqual(
            ["deployer", "tools", "skills/core/scripts", "tests/unit"],
            self.folders(
                '[tool.mypy]\nmypy_path = ["$MYPY_CONFIG_FILE_DIR/skills/core/scripts", '
                '"$MYPY_CONFIG_FILE_DIR/tests/unit"]\n'
            ),
        )

    def test_a_mypy_path_missing_or_naming_a_folder_from_elsewhere_is_none(self) -> None:
        for text in ("", '[tool.mypy]\nmypy_path = "x"\n', '[tool.mypy]\nmypy_path = ["skills/core/scripts"]\n'):
            with self.subTest(text=text):
                self.assertIsNone(self.folders(text))

    def test_a_checker_file_or_a_module_other_roots_import_reaches_every_job(self) -> None:
        imported = ["deployer", "tools", "skills/core/scripts"]
        changed = [
            "pyproject.toml",
            "requirements-dev.in",
            "requirements-dev.txt",
            "skills/core/scripts/shared.py",
            "tools/gate.py",
            "deployer/sub/plan.py",
        ]
        self.assertEqual(
            [
                ("pyproject.toml", "configures or pins every type check"),
                ("requirements-dev.in", "configures or pins every type check"),
                ("requirements-dev.txt", "configures or pins every type check"),
                ("skills/core/scripts/shared.py", "is in skills/core/scripts, which other type-check roots import"),
                ("tools/gate.py", "is in tools, which other type-check roots import"),
                ("deployer/sub/plan.py", "is in deployer, which other type-check roots import"),
            ],
            every_job_reasons(changed, imported),
        )

    def test_an_ordinary_skill_s_script_or_a_file_that_is_not_python_stays_narrow(self) -> None:
        # A folder's name as a prefix of another's is not that folder, and a nested pyproject.toml configures nothing.
        changed = [
            "skills/alpha/scripts/run.py",
            "tools/README.md",
            "toolsmith/run.py",
            "skills/alpha/pyproject.toml",
            "tests/unit/test_run.py",
        ]
        self.assertEqual([], every_job_reasons(changed, ["deployer", "tools", "skills/core/scripts"]))

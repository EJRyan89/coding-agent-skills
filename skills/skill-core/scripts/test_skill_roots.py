"""Regression tests for the skills directories scripts refuse to write inside."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import skill_roots
from skill_roots import deployed_skill_roots, skills_directory_holding


class DeployedSkillRootsTests(unittest.TestCase):
    def test_the_two_roots_the_deployer_owns_under_the_home(self) -> None:
        home = Path("a home") / "with spaces"
        with mock.patch("pathlib.Path.home", return_value=home):
            self.assertEqual((home / ".claude" / "skills", home / ".agents" / "skills"), deployed_skill_roots())

    def test_the_running_skills_directory_is_the_one_holding_skill_core(self) -> None:
        self.assertEqual(Path(__file__).resolve().parents[2], skill_roots.SKILLS_ROOT)
        self.assertTrue((skill_roots.SKILLS_ROOT / "skill-core" / "scripts" / "skill_roots.py").is_file())


class SkillsDirectoryHoldingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "a home"
        self.source = self.root / "source tree" / "skills"
        patches = (
            mock.patch("pathlib.Path.home", return_value=self.home),
            mock.patch.object(skill_roots, "SKILLS_ROOT", self.source),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_path_inside_any_skills_directory_names_that_directory(self) -> None:
        for root in (self.home / ".claude" / "skills", self.home / ".agents" / "skills", self.source):
            for path in (root, root / "some-skill" / "out file.json"):
                with self.subTest(path=path):
                    self.assertEqual(root, skills_directory_holding(path))

    def test_parent_parts_are_resolved_first(self) -> None:
        inside = self.source / "some-skill"
        inside.mkdir(parents=True)
        self.assertEqual(self.source, skills_directory_holding(inside / ".." / "other" / "x"))
        self.assertIsNone(skills_directory_holding(inside / ".." / ".." / "beside skills"))

    def test_a_path_outside_every_skills_directory_is_none(self) -> None:
        for path in (self.root / "elsewhere" / "out.json", self.home / ".claude" / "skills-old" / "x", self.home):
            with self.subTest(path=path):
                self.assertIsNone(skills_directory_holding(path))


if __name__ == "__main__":
    unittest.main()

"""Regression tests for the sibling lookup and the skills directories scripts refuse to write inside."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import skill_roots
from skill_roots import MissingSibling, deployed_skill_roots, sibling_scripts, skills_directory_holding


class DeployedSkillRootsTests(unittest.TestCase):
    def test_the_two_roots_the_deployer_owns_under_the_home(self) -> None:
        home = Path("a home") / "with spaces"
        with mock.patch("pathlib.Path.home", return_value=home):
            self.assertEqual((home / ".claude" / "skills", home / ".agents" / "skills"), deployed_skill_roots())

    def test_the_running_skills_directory_is_the_one_holding_skill_core(self) -> None:
        self.assertEqual(Path(__file__).resolve().parents[2], skill_roots.SKILLS_ROOT)
        self.assertTrue((skill_roots.SKILLS_ROOT / "skill-core" / "scripts" / "skill_roots.py").is_file())


class SiblingScriptsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def use_skills_root(self, root: Path) -> None:
        patch = mock.patch.object(skill_roots, "SKILLS_ROOT", root)
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def make_skill(directory: Path) -> Path:
        (directory / "scripts").mkdir(parents=True)
        (directory / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
        return directory / "scripts"

    def test_this_checkout_finds_a_sibling_beside_skill_core(self) -> None:
        here = Path(__file__).resolve()
        self.assertEqual(here.parents[2] / "code-review-core" / "scripts", sibling_scripts("code-review-core"))
        self.assertEqual(here.parent, sibling_scripts("skill-core"))

    def test_a_flat_checkout_finds_the_sibling_beside_the_skills_root(self) -> None:
        skills = self.root / "source tree" / "skills"
        scripts = self.make_skill(skills / "shared-dep")
        self.make_skill(skills / "skill-core")
        self.use_skills_root(skills)
        self.assertEqual(self.root / "source tree" / "skills" / "shared-dep" / "scripts", sibling_scripts("shared-dep"))
        self.assertEqual(scripts, sibling_scripts("shared-dep"))

    def test_a_deployed_home_finds_the_sibling_beside_skill_core(self) -> None:
        deployed = self.root / "a home" / ".claude" / "skills"
        self.make_skill(deployed / "skill-core")
        self.make_skill(deployed / "shared-dep")
        self.use_skills_root(deployed)
        self.assertEqual(
            self.root / "a home" / ".claude" / "skills" / "shared-dep" / "scripts", sibling_scripts("shared-dep")
        )

    def test_an_absent_sibling_raises_with_its_name(self) -> None:
        skills = self.root / "skills"
        self.make_skill(skills / "skill-core")
        (skills / "no-scripts").mkdir()
        # The skills directory is flat, so a skill nested under another folder is no sibling.
        self.make_skill(skills / "a category" / "nested-dep")
        self.use_skills_root(skills)
        for name in ("shared-dep", "no-scripts", "nested-dep"):
            with self.subTest(name=name), self.assertRaises(MissingSibling) as caught:
                sibling_scripts(name)
            self.assertEqual(name, caught.exception.name)
            self.assertIn(f"skill {name} has no scripts under {skills}", str(caught.exception))

    def test_a_script_whose_sibling_is_absent_exits_1_without_a_traceback(self) -> None:
        deployed = self.root / "a home" / ".claude" / "skills"
        core = self.make_skill(deployed / "skill-core")
        (core / "skill_roots.py").write_text(
            (Path(__file__).resolve().parent / "skill_roots.py").read_text(encoding="utf-8"), encoding="utf-8"
        )
        script = self.make_skill(deployed / "dependent") / "run.py"
        script.write_text(
            "import sys\nfrom pathlib import Path\n"
            'sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))\n'
            "from skill_roots import sibling_scripts\n"
            'sys.path.insert(0, str(sibling_scripts("shared-dep")))\n',
            encoding="utf-8",
        )
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, text=True, check=False)
        self.assertEqual(1, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("skill shared-dep has no scripts under", result.stderr)


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

    @unittest.skipUnless(sys.platform == "win32", "only Windows spells a path with an extended-length prefix")
    def test_an_extended_length_spelling_of_a_path_or_a_root_is_found_alike(self) -> None:
        self.source.mkdir(parents=True)
        for root in (self.home / ".claude" / "skills", self.home / ".agents" / "skills", self.source):
            path = root / "some-skill" / "out file.json"
            with self.subTest(path=path):
                self.assertEqual(root, skills_directory_holding(extended(path)))
                self.assertEqual(root, skills_directory_holding(path))
        with mock.patch.object(skill_roots, "SKILLS_ROOT", extended(self.source)):
            self.assertEqual(extended(self.source), skills_directory_holding(self.source / "some-skill" / "x"))
            self.assertIsNone(skills_directory_holding(self.root / "elsewhere" / "x"))
        self.assertIsNone(skills_directory_holding(extended(self.root / "elsewhere" / "out.json")))
        self.assertIsNone(skills_directory_holding(extended(self.home / ".claude" / "skills-old" / "x")))

    @unittest.skipUnless(sys.platform == "win32", "only Windows spells a path with an extended-length prefix")
    def test_the_prefix_is_removed_before_a_drive_letter_or_a_share_and_kept_before_anything_else(self) -> None:
        volume = "\\\\?\\Volume{00000000-0000-0000-0000-000000000000}\\skills"
        cases = {
            "\\\\?\\C:\\skills\\x": "C:\\skills\\x",
            "\\\\?\\c:\\skills": "c:\\skills",
            "\\\\?\\UNC\\host\\share\\skills\\x": "\\\\host\\share\\skills\\x",
            "\\\\?\\unc\\host\\share\\skills": "\\\\host\\share\\skills",
            "C:\\skills\\x": "C:\\skills\\x",
            "\\\\host\\share\\skills": "\\\\host\\share\\skills",
            volume: volume,
        }
        for spelled, plain in cases.items():
            with self.subTest(path=spelled):
                self.assertEqual(Path(plain), skill_roots._without_extended_prefix(Path(spelled)))

    @unittest.skipIf(sys.platform == "win32", "Windows reads the prefix as part of a drive")
    def test_elsewhere_the_prefix_characters_are_an_ordinary_name(self) -> None:
        for spelled in ("\\\\?\\C:\\skills", "/home/a/\\\\?\\C:\\skills"):
            with self.subTest(path=spelled):
                self.assertEqual(Path(spelled), skill_roots._without_extended_prefix(Path(spelled)))


def extended(path: Path) -> Path:
    """`path`, resolved, written with Windows' extended-length prefix."""
    return Path("\\\\?\\" + str(path.resolve()))

"""Regression tests for the sibling lookup and the skills directories scripts refuse to write inside."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import skill_roots
from skill_roots import MissingSibling, deployed_skill_roots, plugin_name, sibling_scripts, skills_directory_holding


class DeployedSkillRootsTests(unittest.TestCase):
    def test_the_two_roots_the_deployer_owns_under_the_home(self) -> None:
        home = Path("a home") / "with spaces"
        with mock.patch("pathlib.Path.home", return_value=home):
            self.assertEqual((home / ".claude" / "skills", home / ".agents" / "skills"), deployed_skill_roots())

    def test_the_core_is_named_for_the_folder_it_is_deployed_in(self) -> None:
        self.assertEqual("ejryan89-skill-core", skill_roots.SKILL_CORE)
        self.assertEqual(skill_roots.SKILL_CORE, Path(skill_roots.__file__).resolve().parents[1].name)

    def test_the_running_skills_directory_is_the_one_holding_skill_core(self) -> None:
        self.assertEqual(Path(__file__).resolve().parents[2], skill_roots.SKILLS_ROOT)
        self.assertTrue((skill_roots.SKILLS_ROOT / "ejryan89-skill-core" / "scripts" / "skill_roots.py").is_file())


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
        self.assertEqual(
            here.parents[2] / "ejryan89-code-review-core" / "scripts", sibling_scripts("ejryan89-code-review-core")
        )
        self.assertEqual(here.parent, sibling_scripts("ejryan89-skill-core"))

    def test_a_flat_checkout_finds_the_sibling_beside_the_skills_root(self) -> None:
        skills = self.root / "source tree" / "skills"
        scripts = self.make_skill(skills / "shared-dep")
        self.make_skill(skills / "ejryan89-skill-core")
        self.use_skills_root(skills)
        self.assertEqual(self.root / "source tree" / "skills" / "shared-dep" / "scripts", sibling_scripts("shared-dep"))
        self.assertEqual(scripts, sibling_scripts("shared-dep"))

    def test_a_deployed_home_finds_the_sibling_beside_skill_core(self) -> None:
        deployed = self.root / "a home" / ".claude" / "skills"
        self.make_skill(deployed / "ejryan89-skill-core")
        self.make_skill(deployed / "shared-dep")
        self.use_skills_root(deployed)
        self.assertEqual(
            self.root / "a home" / ".claude" / "skills" / "shared-dep" / "scripts", sibling_scripts("shared-dep")
        )

    def test_an_absent_sibling_raises_with_its_name(self) -> None:
        skills = self.root / "skills"
        self.make_skill(skills / "ejryan89-skill-core")
        (skills / "no-scripts").mkdir()
        # The skills directory is flat, so a skill nested under another folder is no sibling.
        self.make_skill(skills / "a category" / "nested-dep")
        self.use_skills_root(skills)
        for name in ("shared-dep", "no-scripts", "nested-dep"):
            with (
                self.subTest(name=name),
                contextlib.redirect_stdout(io.StringIO()) as printed,
                self.assertRaises(MissingSibling) as caught,
            ):
                sibling_scripts(name)
            self.assertEqual(name, caught.exception.name)
            self.assertEqual(1, caught.exception.code)
            self.assertEqual(f"FAILED MissingSibling: {caught.exception}\n", printed.getvalue())
            self.assertIn(f"skill {name} has no scripts under {skills}", str(caught.exception))
            self.assertIn("; deploy the skills that declare it in skill_deps again", str(caught.exception))

    def test_a_script_whose_sibling_is_absent_ends_with_a_failed_line_and_exit_1(self) -> None:
        # A home no legacy code page can spell, so the line is shown to print in UTF-8 before run_main sets it up.
        deployed = self.root / "a home 家" / ".claude" / "skills"
        core = self.make_skill(deployed / "ejryan89-skill-core")
        for module in ("skill_roots.py", "file_presence.py", "console.py", "flat_text.py", "json_reader.py"):
            (core / module).write_text(
                (Path(__file__).resolve().parent / module).read_text(encoding="utf-8"), encoding="utf-8"
            )
        script = self.make_skill(deployed / "dependent") / "run.py"
        script.write_text(
            "import sys\nfrom pathlib import Path\n"
            'sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))\n'
            "from skill_roots import sibling_scripts\n"
            'sys.path.insert(0, str(sibling_scripts("shared-dep")))\n',
            encoding="utf-8",
        )
        # A plugin's manifest beside the skills directory that is not JSON ends the script the same way.
        for manifest in (None, "{not json"):
            if manifest is not None:
                (deployed.parent / ".claude-plugin").mkdir()
                (deployed.parent / ".claude-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-B", str(script)],
                capture_output=True,
                env={**os.environ, "PYTHONIOENCODING": "cp1252"},
                check=False,
            )
            with self.subTest(manifest=manifest):
                self.assertEqual(1, result.returncode)
                self.assertEqual(b"", result.stderr)
                lines = result.stdout.decode("utf-8").splitlines()
                self.assertEqual(1, len(lines))
                self.assertTrue(
                    lines[0].startswith(
                        f"FAILED MissingSibling: skill shared-dep has no scripts under {deployed.resolve()}; "
                    ),
                    lines[0],
                )


class PluginNameTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.plugin = Path(temporary.name) / "a plugin" / "1.0.0"
        (self.plugin / "skills" / "ejryan89-skill-core" / "scripts").mkdir(parents=True)
        patch = mock.patch.object(skill_roots, "SKILLS_ROOT", self.plugin / "skills")
        patch.start()
        self.addCleanup(patch.stop)

    def write_manifest(self, text: str) -> None:
        (self.plugin / ".claude-plugin").mkdir(exist_ok=True)
        (self.plugin / ".claude-plugin" / "plugin.json").write_text(text, encoding="utf-8")

    def test_this_checkout_and_a_skills_directory_without_a_manifest_beside_it_are_no_plugin(self) -> None:
        self.assertIsNone(plugin_name())
        with mock.patch.object(skill_roots, "SKILLS_ROOT", Path(__file__).resolve().parents[2]):
            self.assertIsNone(plugin_name())

    def test_a_plugin_is_named_by_the_manifest_beside_its_skills_directory(self) -> None:
        self.write_manifest('{"name": "a-suite", "version": "1.0.0"}')
        self.assertEqual("a-suite", plugin_name())

    def test_a_manifest_that_names_no_plugin_raises(self) -> None:
        for text in ('{"version": "1.0.0"}', '{"name": ""}', '{"name": 3}', '["a-suite"]'):
            self.write_manifest(text)
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "names no plugin"):
                plugin_name()

    def test_a_manifest_that_cannot_be_read_or_parsed_raises_value_error(self) -> None:
        for text, reason in (("{not json", "is not JSON"), ("", "is not JSON")):
            self.write_manifest(text)
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, reason):
                plugin_name()
        (self.plugin / ".claude-plugin" / "plugin.json").write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(ValueError, "is not JSON"):
            plugin_name()
        with (
            mock.patch("pathlib.Path.read_text", side_effect=PermissionError(13, "Permission denied")),
            self.assertRaisesRegex(ValueError, "cannot be read: Permission denied"),
        ):
            plugin_name()

    def test_a_directory_in_the_manifest_place_raises_value_error_never_reads_as_no_plugin(self) -> None:
        (self.plugin / ".claude-plugin" / "plugin.json").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "cannot be read: not a file"):
            plugin_name()

    def test_an_absent_sibling_beside_a_manifest_that_cannot_be_known_still_names_the_sibling(self) -> None:
        unreadable = mock.patch("pathlib.Path.read_text", side_effect=PermissionError(13, "Permission denied"))
        for text, reading, reason in (
            ("{not json", contextlib.nullcontext(), "is not JSON"),
            ('{"name": ""}', contextlib.nullcontext(), "names no plugin"),
            ('{"name": "a-suite"}', unreadable, "cannot be read"),
        ):
            self.write_manifest(text)
            with (
                self.subTest(reason=reason),
                reading,
                contextlib.redirect_stdout(io.StringIO()) as printed,
                self.assertRaises(MissingSibling) as caught,
            ):
                sibling_scripts("shared-dep")
            self.assertEqual(f"FAILED MissingSibling: {caught.exception}\n", printed.getvalue())
            self.assertEqual("shared-dep", caught.exception.name)
            self.assertIn("skill shared-dep has no scripts under", str(caught.exception))
            self.assertIn("; reinstall the plugin: ", str(caught.exception))
            self.assertIn(reason, str(caught.exception))

    def test_an_absent_sibling_in_a_plugin_asks_for_the_plugin_again_not_a_deployment(self) -> None:
        self.write_manifest('{"name": "a-suite"}')
        with contextlib.redirect_stdout(io.StringIO()) as printed, self.assertRaises(MissingSibling) as caught:
            sibling_scripts("shared-dep")
        self.assertEqual(f"FAILED MissingSibling: {caught.exception}\n", printed.getvalue())
        self.assertIn("; update or reinstall the plugin a-suite", str(caught.exception))
        self.assertNotIn("deploy", str(caught.exception))


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

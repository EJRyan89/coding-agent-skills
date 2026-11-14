"""Regression tests for the Agent Skills rules the deployer and audit-ai-config share."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_skills
import path_keys


class AgentSkillsRuleTests(unittest.TestCase):
    """Each value is pinned to the literal its vendor page gives, never to the constant under test."""

    def test_the_field_limits_and_reserved_words_are_anthropics(self) -> None:
        self.assertEqual(("anthropic", "claude"), agent_skills.RESERVED_WORDS)
        self.assertEqual(64, agent_skills.MAX_NAME)
        self.assertEqual(1024, agent_skills.MAX_DESCRIPTION)

    def test_codex_reads_the_invocation_policy_beside_the_skill(self) -> None:
        self.assertEqual("agents/openai.yaml", agent_skills.CODEX_METADATA)

    def test_an_xml_tag_is_found_and_a_comparison_is_not(self) -> None:
        for text in ("<b>bold</b>", "a </section> end", "<br/>", '<a href="x">'):
            with self.subTest(text=text):
                self.assertIsNotNone(agent_skills.XML_TAG_PATTERN.search(text))
        for text in ("a < b and c > d", "x <= 3", "<3 hearts", "plain"):
            with self.subTest(text=text):
                self.assertIsNone(agent_skills.XML_TAG_PATTERN.search(text))


class SkillFileTests(unittest.TestCase):
    """A skill's main file is SKILL.md as its folder's file system compares names, as the runtime that opens it finds
    it: in any case where the folder folds case, and exactly SKILL.md where it does not. Each answer is pinned with the
    folder's answer set, so it holds on a file system that folds case and one that does not."""

    def test_the_main_file_is_the_name_a_runtime_opens(self) -> None:
        self.assertEqual("SKILL.md", agent_skills.SKILL_FILE)

    def test_skill_md_in_another_case_is_the_main_file_only_where_the_folder_folds_case(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for folds in (True, False):
                with self.subTest(folds=folds), mock.patch.dict(path_keys._FOLDS_CASE, {str(folder): folds}):
                    self.assertTrue(agent_skills.is_skill_file(folder / "SKILL.md"))
                    for name in ("skill.md", "Skill.MD"):
                        self.assertEqual(folds, agent_skills.is_skill_file(folder / name), name)
                    for name in ("notes.md", "SKILL.markdown", "SKILL.md.bak"):
                        self.assertFalse(agent_skills.is_skill_file(folder / name), name)

    def test_a_name_that_is_skill_md_in_no_case_never_asks_its_folder(self) -> None:
        with mock.patch.object(path_keys, "folds_case", side_effect=AssertionError("asked")):
            self.assertFalse(agent_skills.is_skill_file(Path("alpha") / "README.md"))

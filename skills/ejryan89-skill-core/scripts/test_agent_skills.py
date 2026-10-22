"""Regression tests for the Agent Skills rules the deployer and audit-ai-config share."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_skills


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

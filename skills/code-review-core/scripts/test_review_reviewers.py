from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIRECTORY))

from review_reviewers import entrypoint_manifest, inspect_skill  # noqa: E402

FILES = {".claude/agents/review.md", ".claude/agents/db-review.md", "docs/rules.md", "src/A.cs"}


def inspect(text: str, skill: str = ".claude/agents/review.md"):
    return inspect_skill(skill, text, "a" * 40, FILES)


class InspectionTests(unittest.TestCase):
    def test_tool_lists_in_every_frontmatter_form(self) -> None:
        for frontmatter, expected in (
            ("tools: Read, Grep, Glob", ["Read", "Grep", "Glob"]),
            ('allowed-tools: ["Bash", "Read"]', ["Bash", "Read"]),
            ("tools:\n  - Read\n  - 'Agent'", ["Read", "Agent"]),
        ):
            with self.subTest(frontmatter=frontmatter):
                self.assertEqual(expected, inspect(f"---\nname: x\n{frontmatter}\n---\nReview it.\n").tools)
        self.assertIsNone(inspect("---\nname: x\n---\nReview it.\n").tools, "no list inherits every tool")
        self.assertIsNone(inspect("No frontmatter at all.\n").tools)

    def test_delegation_needs_both_the_tool_and_the_instruction(self) -> None:
        cases = {
            "yes": "---\nname: x\n---\nStart a subagent per area.\n",
            "no": "---\ntools: Read, Grep\n---\nSpawn a subagent per area.\n",  # the tools forbid it
            "unknown": "---\ntools: Read, Agent(general-purpose)\n---\nReview the change.\n",
        }
        for expected, text in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(expected, inspect(text).delegates)
        self.assertEqual("no", inspect("---\nname: x\n---\nReview the change carefully.\n").delegates)
        self.assertEqual([], inspect(cases["no"]).evidence, "evidence is moot when the tools forbid delegation")

    def test_named_agents_count_as_delegation_and_as_references(self) -> None:
        result = inspect("---\nname: x\n---\nAsk db-review about SQL changes.\nSee `docs/rules.md`.\n")
        self.assertEqual("yes", result.delegates)
        self.assertEqual([(4, "Ask db-review about SQL changes.")], result.evidence, "line numbers count the file")
        self.assertEqual([".claude/agents/db-review.md", "docs/rules.md"], result.references)
        self.assertNotIn(".claude/agents/review.md", result.references, "the skill never references itself")

    def test_references_come_from_code_spans_links_and_bare_paths_that_exist(self) -> None:
        result = inspect("Read [the rules](docs/rules.md#style), then src/A.cs and `missing/file.md`.\n")
        self.assertEqual(["docs/rules.md", "src/A.cs"], result.references)

    def test_an_entrypoint_carries_what_the_skill_names_and_needs_no_delegation(self) -> None:
        result = inspect("---\ntools: Read\n---\nApply `docs/rules.md`.\n")
        manifest = entrypoint_manifest("solo", result)
        self.assertEqual(".claude/agents/review.md", manifest["entrypoint"])
        self.assertEqual(["docs/rules.md"], manifest["resources"])
        self.assertNotIn("agent-delegation", manifest["required_capabilities"])


if __name__ == "__main__":
    unittest.main()

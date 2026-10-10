"""Regression suite for docs/implementing-changes.md: the profile must carry every section implement-change names."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

import frontmatter

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROFILE = REPOSITORY_ROOT / "docs" / "implementing-changes.md"
SKILL = REPOSITORY_ROOT / ".claude" / "skills" / "implement-change" / "SKILL.md"
REQUIRED_HEADINGS = ("Size gate", "Worktree", "Validation", "Contract files", "Documentation", "Model guidance")


def headings(text: str) -> dict[str, str]:
    """Map each second-level heading to its body."""
    parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
    return {parts[index].strip(): parts[index + 1].strip() for index in range(1, len(parts), 2)}


class ImplementingChangesProfileTest(unittest.TestCase):
    def test_profile_has_every_required_heading_with_content(self) -> None:
        found = headings(PROFILE.read_text(encoding="utf-8"))
        for name in REQUIRED_HEADINGS:
            with self.subTest(heading=name):
                self.assertIn(name, found, f"docs/implementing-changes.md lacks a '## {name}' section")
                self.assertTrue(found[name], f"'## {name}' is empty")

    def test_headings_detector_reports_a_missing_section(self) -> None:
        found = headings("# Title\n\n## Size gate\n\nText.\n\n## Worktree\n\n")
        self.assertEqual({"Size gate": "Text.", "Worktree": ""}, found)
        self.assertNotIn("Validation", found)

    def test_skill_names_each_required_heading(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        for name in REQUIRED_HEADINGS:
            with self.subTest(heading=name):
                self.assertIn(f"*{name}*", text)

    def test_skill_body_names_no_repository_command(self) -> None:
        lines = SKILL.read_text(encoding="utf-8").splitlines()
        found = frontmatter.split(lines)
        if found is None:
            self.fail("implement-change has no frontmatter")
        body = "\n".join(lines[found[1] :])
        for literal in ("tools/worktrees.py", "run_validation", "deploy.py", "change-skill", "releasing.md"):
            with self.subTest(literal=literal):
                self.assertNotIn(literal, body)


if __name__ == "__main__":
    unittest.main()

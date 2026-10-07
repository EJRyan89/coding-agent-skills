"""Regression suite for the supported line: SECURITY.md, the Updating section, and the update skill must agree."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SECURITY = REPOSITORY_ROOT / "SECURITY.md"
INSTALLATION = REPOSITORY_ROOT / "docs" / "installation.md"
UPDATE_SKILL = REPOSITORY_ROOT / "skills" / "update-coding-agent-skills" / "SKILL.md"
# The one phrase each document states the line with, pinned here as a literal rather than read from any of them.
SUPPORTED_LINE = "`main` is the supported line"


def section(text: str, heading: str) -> str:
    """The body of one second-level heading, or an empty string when the heading is absent."""
    parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
    bodies = {parts[index].strip(): parts[index + 1] for index in range(1, len(parts), 2)}
    return bodies.get(heading, "")


class SupportedLineTest(unittest.TestCase):
    def test_security_policy_names_main_as_the_supported_line(self) -> None:
        text = section(SECURITY.read_text(encoding="utf-8"), "Supported versions")
        self.assertIn(SUPPORTED_LINE, text)

    def test_security_policy_no_longer_supports_only_the_latest_release(self) -> None:
        self.assertNotIn("Only the latest release is supported", SECURITY.read_text(encoding="utf-8"))

    def test_installation_updating_section_names_the_same_line(self) -> None:
        text = section(INSTALLATION.read_text(encoding="utf-8"), "Updating")
        self.assertIn(SUPPORTED_LINE, text)

    def test_update_skill_names_the_same_line(self) -> None:
        self.assertIn(SUPPORTED_LINE, UPDATE_SKILL.read_text(encoding="utf-8"))

    def test_section_reads_only_the_named_heading(self) -> None:
        text = "# Title\n\n## Updating\n\nKept.\n\n## Uninstalling\n\nDropped.\n"
        self.assertEqual("\n\nKept.\n\n", section(text, "Updating"))
        self.assertEqual("", section(text, "Missing"))


if __name__ == "__main__":
    unittest.main()

"""Regression tests for the shared second-level Markdown section reader."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from markdown_sections import markdown_section


class MarkdownSectionTests(unittest.TestCase):
    def test_first_exact_heading_allows_trailing_whitespace_but_not_a_prefix_or_indentation(self) -> None:
        text = " ## One\nindented\n## One more\nlonger\n## One \t\nfirst  \n## One\nsecond"
        self.assertEqual("first  ", markdown_section(text, "## One"))
        self.assertIsNone(markdown_section(text, "## Missing"))

    def test_only_a_second_level_heading_ends_the_section_and_comments_are_preserved(self) -> None:
        text = "## One\n### Inner\n<!-- comment -->\n# Title\n##Two\n ## Two\nkept\t\n## End \t\noutside"
        self.assertEqual(
            "### Inner\n<!-- comment -->\n# Title\n##Two\n ## Two\nkept\t", markdown_section(text, "## One")
        )

    def test_crlf_is_normalized_without_stripping_body_whitespace_or_final_newlines(self) -> None:
        text = "## One \t\r\n\r\nfirst  \r\nlast\t\r\n"
        self.assertEqual("\nfirst  \nlast\t\n", markdown_section(text, "## One"))
        self.assertEqual("", markdown_section("## One", "## One"))


if __name__ == "__main__":
    unittest.main()

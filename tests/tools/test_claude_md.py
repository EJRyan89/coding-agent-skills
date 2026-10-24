"""Regression suite for CLAUDE.md: it stays within its size budget, and each pointer names a section.

CLAUDE.md states each rule once and points at the section that holds how the rule is enforced, written as the
section's heading in double quotes followed by `in` and the document's path in backticks. A pointer whose section
was renamed or removed would send a session nowhere, so every one must name a heading its document has.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INSTRUCTIONS = REPOSITORY_ROOT / "CLAUDE.md"
# Codex's default `project_doc_max_bytes`, 32 KiB, kept as CLAUDE.md's budget: Codex stops adding AGENTS.md files
# at that combined size and reads CLAUDE.md through the root AGENTS.md's redirect, which the limit does not govern,
# so the budget keeps the file one a session can read whole. Written out here rather than derived from the file, so
# the ceiling cannot move with what it holds.
CODEX_INSTRUCTION_LIMIT = 32768
# Every document CLAUDE.md points into, named here so that the runner selects this suite when one of them changes.
POINTED_DOCUMENTS = (
    "CONTRIBUTING.md",
    "docs/adding-a-skill.md",
    "docs/design.md",
    "docs/implementing-changes.md",
    "docs/parallel-sessions.md",
    "docs/releasing.md",
    "docs/skill-evaluations.md",
)
POINTER = re.compile(r'"([^"\n]+)" in `([^`\n]+\.md)`')


def pointers(text: str) -> list[tuple[str, str]]:
    """Each (section, document) pair the text points at, in the order it names them."""
    return POINTER.findall(text)


def has_section(document: str, section: str) -> bool:
    """Whether the document has a heading, at any level, whose text is exactly the section's."""
    return re.search(rf"^#+ {re.escape(section)}[ \t]*$", document, re.MULTILINE) is not None


class ClaudeMdTests(unittest.TestCase):
    def test_the_file_fits_what_codex_reads(self) -> None:
        self.assertLess(len(INSTRUCTIONS.read_bytes()), CODEX_INSTRUCTION_LIMIT)

    def test_each_pointer_names_a_section_its_document_has(self) -> None:
        found = pointers(INSTRUCTIONS.read_text(encoding="utf-8"))
        self.assertTrue(found, "CLAUDE.md points at no section")
        for section, document in found:
            with self.subTest(section=section, document=document):
                path = REPOSITORY_ROOT / document
                self.assertTrue(path.is_file(), f"{document} does not exist")
                self.assertTrue(has_section(path.read_text(encoding="utf-8"), section))

    def test_the_pointed_documents_are_the_ones_named_here(self) -> None:
        named = {document for _, document in pointers(INSTRUCTIONS.read_text(encoding="utf-8"))}
        self.assertEqual(set(POINTED_DOCUMENTS), named)


class PointerReadingTests(unittest.TestCase):
    def test_a_quoted_heading_before_a_document_is_a_pointer(self) -> None:
        text = 'See "Template values" in `docs/adding-a-skill.md`, and "Python checks" in `CONTRIBUTING.md`.'
        self.assertEqual(
            [("Template values", "docs/adding-a-skill.md"), ("Python checks", "CONTRIBUTING.md")], pointers(text)
        )

    def test_a_quotation_without_a_document_is_no_pointer(self) -> None:
        self.assertEqual([], pointers('A "checked on" date, and the "Runtime support" table in `docs/skills.md`.'))

    def test_a_heading_matches_at_any_level_and_only_whole(self) -> None:
        document = "# Title\n\n## Validation\n\n### The guard's contract\n\n## Validation on Linux\n"
        self.assertTrue(has_section(document, "Validation"))
        self.assertTrue(has_section(document, "The guard's contract"))
        self.assertFalse(has_section(document, "The guard"))
        self.assertFalse(has_section(document, "Linux"))

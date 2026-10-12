"""Regression tests for the documented ordered levels and leading entry-level reader."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from release_levels import VERSION, largest_level, level_word, versioning_levels

LEVELS = (
    "## Versioning \t\r\n**Levels**, from the stable release on:\r\n\r\n"
    "- **Patch.** Fixes.\r\n- **Minor.** Additions.\r\n  Continued.\r\n- **Major.** Migrations.\r\n\r\n"
    "**Before stability**, minor may carry a breaking change.\r\n- **Other.** Not a level.\r\n"
    "## Next\r\n- **Outside.** Not a level.\r\n"
)


class ReleaseLevelTests(unittest.TestCase):
    def test_version_matches_only_the_complete_tag_shape(self) -> None:
        self.assertIsNotNone(VERSION.fullmatch("v0.5.0"))
        for value in ("0.5.0", "v1.2", "v1.2.3rc1", "v1.2.3\n"):
            with self.subTest(value=value):
                self.assertIsNone(VERSION.fullmatch(value))

    def test_levels_come_only_from_the_documented_list_in_its_order(self) -> None:
        self.assertEqual(["patch", "minor", "major"], versioning_levels(LEVELS))
        self.assertEqual([], versioning_levels("## Other\n**Levels**\n- **Patch.** Fixes.\n"))
        self.assertEqual([], versioning_levels("## Versioning\nNo list.\n"))

    def test_a_level_word_keeps_its_original_case_and_does_not_guess_from_the_prose(self) -> None:
        for value, expected in (
            ("Patch. A fix.", "Patch"),
            ("minor: would be major later.", "minor"),
            ("major, a migration.", "major"),
            ("minor2.", "minor2."),
            ("minor_update", "minor_update"),
            ("`minor`", "`minor`"),
            ("0.5.0", "0.5.0"),
            ("", None),
        ):
            with self.subTest(value=value):
                self.assertEqual(expected, level_word(value))

    def test_the_largest_level_uses_the_document_order_and_empty_entries_have_no_level(self) -> None:
        levels = versioning_levels(LEVELS)
        for values, expected in (
            ([], None),
            (["Patch. A fix."], "patch"),
            (["patch", "MINOR: an addition", "patch"], "minor"),
            (["minor", "major", "patch"], "major"),
        ):
            with self.subTest(values=values):
                self.assertEqual(expected, largest_level(values, levels))
        self.assertEqual("patch", largest_level(["patch", "minor"], ["minor", "patch", "major"]))

    def test_missing_unknown_or_malformed_levels_and_an_empty_or_duplicate_order_fail(self) -> None:
        for value in ("", "breaking", "0.5.0", "`minor`", "minor2", "minor_update"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Level"):
                largest_level([value], versioning_levels(LEVELS))
        for levels in ([], ["patch", "patch"]):
            with self.subTest(levels=levels), self.assertRaisesRegex(ValueError, "Levels"):
                largest_level([], levels)


if __name__ == "__main__":
    unittest.main()

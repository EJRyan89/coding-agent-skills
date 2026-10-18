"""Regression tests for the documented ordered levels and leading entry-level reader."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from release_levels import VERSION, largest_level, level_word, versioning_levels

# Runs no test of its own; the test functions below check through its assertions, as the class does.
CHECK = unittest.TestCase()

LEVELS = (
    "## Versioning \t\r\n**Levels**, from the stable release on:\r\n\r\n"
    "- **Patch.** Fixes.\r\n- **Minor.** Additions.\r\n  Continued.\r\n- **Major.** Migrations.\r\n\r\n"
    "**Before stability**, minor may carry a breaking change.\r\n- **Other.** Not a level.\r\n"
    "## Next\r\n- **Outside.** Not a level.\r\n"
)


class ReleaseLevelTests(unittest.TestCase):
    def test_version_matches_only_the_complete_tag_shape(self) -> None:
        self.assertIsNotNone(VERSION.fullmatch("v0.5.0"))

    def test_levels_come_only_from_the_documented_list_in_its_order(self) -> None:
        self.assertEqual(["patch", "minor", "major"], versioning_levels(LEVELS))
        self.assertEqual([], versioning_levels("## Other\n**Levels**\n- **Patch.** Fixes.\n"))
        self.assertEqual([], versioning_levels("## Versioning\nNo list.\n"))


@pytest.mark.parametrize("value", ["0.5.0", "v1.2", "v1.2.3rc1", pytest.param("v1.2.3\n", id="v1.2.3-newline")])
def test_version_matches_only_the_complete_tag_shape(value: str) -> None:
    CHECK.assertIsNone(VERSION.fullmatch(value))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Patch. A fix.", "Patch"),
        ("minor: would be major later.", "minor"),
        ("major, a migration.", "major"),
        ("minor2.", "minor2."),
        ("minor_update", "minor_update"),
        ("`minor`", "`minor`"),
        ("0.5.0", "0.5.0"),
        pytest.param("", None, id="empty"),
    ],
)
def test_a_level_word_keeps_its_original_case_and_does_not_guess_from_the_prose(
    value: str, expected: str | None
) -> None:
    CHECK.assertEqual(expected, level_word(value))


@pytest.mark.parametrize(
    ("values", "levels", "expected"),
    [
        pytest.param([], versioning_levels(LEVELS), None, id="no-entries"),
        pytest.param(["Patch. A fix."], versioning_levels(LEVELS), "patch", id="one-patch"),
        pytest.param(
            ["patch", "MINOR: an addition", "patch"], versioning_levels(LEVELS), "minor", id="minor-among-patches"
        ),
        pytest.param(["minor", "major", "patch"], versioning_levels(LEVELS), "major", id="major-among-others"),
        pytest.param(["patch", "minor"], ["minor", "patch", "major"], "patch", id="another-order"),
    ],
)
def test_the_largest_level_uses_the_document_order_and_empty_entries_have_no_level(
    values: list[str], levels: list[str], expected: str | None
) -> None:
    CHECK.assertEqual(expected, largest_level(values, levels))


@pytest.mark.parametrize(
    ("values", "levels", "pattern"),
    [
        *(
            pytest.param([value], versioning_levels(LEVELS), "Level", id=f"level-{value or 'empty'}")
            for value in ("", "breaking", "0.5.0", "`minor`", "minor2", "minor_update")
        ),
        pytest.param([], [], "Levels", id="no-levels"),
        pytest.param([], ["patch", "patch"], "Levels", id="a-duplicate-level"),
    ],
)
def test_missing_unknown_or_malformed_levels_and_an_empty_or_duplicate_order_fail(
    values: list[str], levels: list[str], pattern: str
) -> None:
    with CHECK.assertRaisesRegex(ValueError, pattern):
        largest_level(values, levels)

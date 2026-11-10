from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ordinal_case import has_suffix, ordinal_fold, simple_uppercase


@pytest.mark.parametrize(
    ("expected", "first", "second"),
    [
        (True, "Lantern.Core", "LANTERN.core"),
        # A letter beyond ASCII matches its own other case.
        (True, "\u00e9clair", "\u00c9CLAIR"),
        (True, "\u03c3", "\u03a3"),
        # Nothing beyond ASCII folds onto ASCII, and nothing folds onto several characters.
        (False, "stra\u00dfe", "strasse"),
        (False, "\u017fdk", "sdk"),
        (False, "\u212alient", "klient"),
        (False, "\u0130", "i"),
        (False, "\ufb00", "ff"),
    ],
)
def test_two_spellings_fold_alike_only_where_dotnets_ordinal_comparison_ignoring_case_finds_them_equal(
    expected: bool, first: str, second: str
) -> None:
    assert expected is (ordinal_fold(first) == ordinal_fold(second))


def test_ascii_text_folds_to_its_lowercase_so_it_keeps_its_order() -> None:
    assert ordinal_fold("Lantern.Core") == "lantern.core"
    assert ordinal_fold("A\u00e9b") == "a\u00c9b"


def test_a_character_whose_simple_uppercase_cannot_be_told_matches_only_itself() -> None:
    assert simple_uppercase("\u1f80") is None
    assert ordinal_fold("\u1f80") != ordinal_fold("\u1f88")
    assert simple_uppercase("\u00df") == "\u00df"


@pytest.mark.parametrize(
    ("expected", "name"),
    [
        (True, "App.sln"),
        (True, "App.SLNX"),
        (False, "App.\u017flnx"),
        (False, "App.\u017fln"),
        (False, "App.slnx.bak"),
    ],
)
def test_a_suffix_is_matched_ordinally_ignoring_case(expected: bool, name: str) -> None:
    assert expected is has_suffix(name, (".sln", ".slnx"))

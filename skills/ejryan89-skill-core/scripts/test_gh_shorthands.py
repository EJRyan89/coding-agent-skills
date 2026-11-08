"""Regression tests for the one reader of a word of gh shorthand flags."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gh_shorthands import GH_VALUE_SHORTHANDS, Shorthand, shorthands

API_VALUES = frozenset("HfFXpqt")


@pytest.mark.parametrize(
    ("word", "flags"),
    [
        ("-i", [Shorthand("-i", None, False)]),
        ("-X", [Shorthand("-X", None, True)]),
        ("-XPOST", [Shorthand("-X", "POST", False)]),
        ("-X=POST", [Shorthand("-X", "POST", False)]),
        # pflag reads a `=` only before a value of more than nothing, so `-X=` gives -X the value `=`.
        ("-X=", [Shorthand("-X", "=", False)]),
        ("-ifbody=hello", [Shorthand("-i", None, False), Shorthand("-f", "body=hello", False)]),
        ("-iFbody=hello", [Shorthand("-i", None, False), Shorthand("-F", "body=hello", False)]),
        ("-iXPOST", [Shorthand("-i", None, False), Shorthand("-X", "POST", False)]),
        ("-iX", [Shorthand("-i", None, False), Shorthand("-X", None, True)]),
        # A `=` after a letter that takes no value ends the cluster too, the rest its value.
        ("-i=fbody", [Shorthand("-i", "fbody", False)]),
        ("-zi", [Shorthand("-z", None, False), Shorthand("-i", None, False)]),
        ("-fX", [Shorthand("-f", "X", False)]),
    ],
)
def test_a_cluster_is_read_letter_by_letter_as_pflag_reads_it(word: str, flags: list[Shorthand]) -> None:
    assert flags == shorthands(word, API_VALUES)


@pytest.mark.parametrize("word", ["", "-", "--", "--method=POST", "--iX", "repos/o/r", "body=-x"])
def test_a_word_that_is_no_cluster_sets_nothing(word: str) -> None:
    assert shorthands(word, API_VALUES) == []


def test_the_letters_that_take_a_value_in_every_command_that_has_them() -> None:
    assert frozenset("ABFHLORSXbefjklmoprs") == GH_VALUE_SHORTHANDS

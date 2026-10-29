"""Regression tests for closing_references.py, the one reader of the issues a pull request body closes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from closing_references import ClosingReference, closing_references


# Each form GitHub closes an issue by, after each keyword form, in any case, with or without a colon.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Closes #12", ClosingReference(None, 12)),
        ("close #12", ClosingReference(None, 12)),
        ("CLOSED: #12", ClosingReference(None, 12)),
        ("fix #12", ClosingReference(None, 12)),
        ("Fixes #12", ClosingReference(None, 12)),
        ("fixed #12", ClosingReference(None, 12)),
        ("resolve #12", ClosingReference(None, 12)),
        ("Resolves: #12", ClosingReference(None, 12)),
        ("resolved #12", ClosingReference(None, 12)),
        ("Closes owner/repo.name#12", ClosingReference("owner/repo.name", 12)),
        ("Fixes https://github.com/owner/repo/issues/12", ClosingReference("owner/repo", 12)),
    ],
)
def test_each_keyword_and_reference_form_is_read(text: str, expected: ClosingReference) -> None:
    assert closing_references(text) == [expected]


@pytest.mark.parametrize(
    "text",
    [
        "See #12",  # no closing keyword
        "discloses #12",  # a keyword inside a word
        "Closes #",  # no number
        "Closes https://example.com/owner/repo/issues/12",  # another host
        "Closes https://github.com/owner/repo/pull/12",  # a pull request, not an issue
        f"Closes #1{'0' * 5000}",  # past the digit limit
    ],
)
def test_what_closes_no_issue_is_not_read(text: str) -> None:
    assert closing_references(text) == []


def test_every_reference_is_read_in_order() -> None:
    text = "Closes #1, and fixes o/r#2.\nResolves https://github.com/o/r/issues/3\n"
    assert closing_references(text) == [
        ClosingReference(None, 1),
        ClosingReference("o/r", 2),
        ClosingReference("o/r", 3),
    ]


def test_a_reference_is_in_a_repository_by_its_name_in_any_case_or_by_naming_none() -> None:
    assert ClosingReference(None, 1).in_repository("Owner/Repo")
    assert ClosingReference("owner/REPO", 1).in_repository("Owner/Repo")
    assert not ClosingReference("other/repo", 1).in_repository("Owner/Repo")

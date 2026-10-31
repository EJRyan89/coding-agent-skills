"""Regression tests for closing_references.py, the one reader of the issues a pull request closes."""

from __future__ import annotations

import itertools
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from closing_references import REPOSITORY, ClosingReference, closing_references, pull_request_references


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
        "Closes #12a",  # a number running into a word
        "fixes GH-hosted runners",  # GH- with no number
        "Closes GH-",  # GH- with no number
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


def test_the_repository_form_matches_a_name_in_any_case_on_its_own() -> None:
    # A caller may build on REPOSITORY without a case-insensitive flag, and GitHub names keep the case they were made
    # with, so the form itself takes both cases; a name with any other character is none.
    for name in ("EJRyan89/coding-agent-skills", "owner/REPO.name", "lower/case_name-1"):
        assert re.fullmatch(REPOSITORY, name), name
    for name in ("owner", "owner/repo/extra", "own er/repo", "owner/rep#o"):
        assert not re.fullmatch(REPOSITORY, name), name


def test_a_reference_is_in_a_repository_by_its_name_in_any_case_or_by_naming_none() -> None:
    assert ClosingReference(None, 1).in_repository("Owner/Repo")
    assert ClosingReference("owner/REPO", 1).in_repository("Owner/Repo")
    assert not ClosingReference("other/repo", 1).in_repository("Owner/Repo")


# The catalogue the reader covers, from which the table below is generated, so a spelling added to one axis is read in
# every combination with the others. Each keyword form, in three cases:
KEYWORDS = [
    spelled
    for keyword in ("close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved")
    for spelled in (keyword, keyword.title(), keyword.upper())
]
# What may stand between the keyword and the reference, `{}` marking where the keyword goes.
SEPARATORS = ["{} ", "{}: ", "{}:", "{}", "{}\t", "{} : ", "**{}** ", "**{}:** ", "**{}**: ", "{} **"]
# Each form of the issue: the two GitHub's closing-keyword page names, the two its autolink page adds, and the URL's
# spellings its pages leave open.
REFERENCES = {
    "#12": ClosingReference(None, 12),
    "o/r.name#12": ClosingReference("o/r.name", 12),
    "GH-12": ClosingReference(None, 12),
    "gh-12": ClosingReference(None, 12),
    "https://github.com/o/r/issues/12": ClosingReference("o/r", 12),
    "http://github.com/o/r/issues/12": ClosingReference("o/r", 12),
    "https://www.github.com/o/r/issues/12": ClosingReference("o/r", 12),
    "github.com/o/r/issues/12": ClosingReference("o/r", 12),
    "<https://github.com/o/r/issues/12>": ClosingReference("o/r", 12),
    "https://github.com/o/r/issues/12#issuecomment-1": ClosingReference("o/r", 12),
}


@pytest.mark.parametrize(
    ("keyword", "separator", "reference"),
    [
        *((keyword, separator, "#12") for keyword, separator in itertools.product(KEYWORDS, SEPARATORS)),
        *(
            ("Closes", separator, reference)
            for separator, reference in itertools.product(SEPARATORS, list(REFERENCES)[1:])
        ),
    ],
)
def test_every_keyword_separator_and_reference_in_the_catalogue_is_read(
    keyword: str, separator: str, reference: str
) -> None:
    text = separator.format(keyword) + reference
    # With nothing between them, a reference that starts with a letter runs into the keyword, which is then no word.
    expected = [] if separator == "{}" and reference[0].isalnum() else [REFERENCES[reference]]
    assert closing_references(f"Intro.\n{text} and more.\n") == expected, text


# The spellings the third outside review found unread, each now read.
@pytest.mark.parametrize(
    "text",
    [
        "Closes GH-1",
        "Closes <https://github.com/o/r/issues/1>",
        "**Closes** #1",
        "CLOSES:#1",
        "Closes http://github.com/o/r/issues/1",
        "Closes https://www.github.com/o/r/issues/1",
    ],
)
def test_each_spelling_the_review_found_unread_is_read(text: str) -> None:
    assert [reference.number for reference in closing_references(text)] == [1]


# What the reader does not see, as its docstring names it; a row that starts to read fails here, so the docstring is
# brought along with the reader.
NOT_READ = {
    "Closes\n#1": "a keyword and its reference on different lines",
    "_Closes_ #1": "`_` emphasis",
    "~~Closes~~ #1": "`~` emphasis",
    "Closes \\#1": "a backslash-escaped number",
    "Schließt #1": "a keyword in another language",
    "Closes JIRA-1": "a custom autolink prefix",
}


@pytest.mark.parametrize("text", list(NOT_READ))
def test_what_the_docstring_names_as_unseen_is_not_read(text: str) -> None:
    assert closing_references(text) == [], NOT_READ[text]


def test_only_the_first_of_a_list_that_names_its_keyword_once_is_read() -> None:
    assert closing_references("Closes #1, #2 and #3") == [ClosingReference(None, 1)]


def test_a_long_run_between_a_keyword_and_no_reference_is_read_in_linear_time() -> None:
    for run in ("*", " ", ":", "*:", " *"):
        text = "fixes" + run * 50_000 + "x"
        started = time.monotonic()
        assert closing_references(text) == []
        assert time.monotonic() - started < 5, repr(run)


def test_a_pull_request_closes_what_its_title_or_its_body_names_comments_included() -> None:
    body = "Closes #3\n<!-- fixes #4 -->\n"
    assert pull_request_references("fix: resolves #1 and fixes o/r#2", body) == [
        ClosingReference(None, 1),
        ClosingReference("o/r", 2),
        ClosingReference(None, 3),
        ClosingReference(None, 4),
    ]
    assert pull_request_references(None, body) == [ClosingReference(None, 3), ClosingReference(None, 4)]
    assert pull_request_references("fix: resolves #1", None) == [ClosingReference(None, 1)]
    assert pull_request_references(None, None) == []


def test_a_title_ending_in_a_keyword_never_reaches_a_body_starting_with_a_reference() -> None:
    assert pull_request_references("fix: what this closes", "#5 was the cause.\n") == []

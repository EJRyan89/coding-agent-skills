"""Regression tests for closing_references.py, the one reader of the issues a pull request closes."""

from __future__ import annotations

import itertools
import re
import sys
import time
from collections.abc import Iterable
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from closing_references import (
    CLOSING_KEYWORDS,
    KEYWORDS,
    NOT_READ,
    REFERENCES_READ,
    REPOSITORY,
    SEPARATORS_READ,
    ClosingReference,
    closing_references,
    pull_request_references,
    stray_closing_lines,
    title_holds_closing_reference,
)


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
        ("Fixes https://github.com/owner/repo/pull/12", ClosingReference("owner/repo", 12)),
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


# GitHub's closing keywords as its page "Linking a pull request to an issue" lists them
# (https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue),
# pinned here because GitHub publishes no list a test can read, so a keyword the page adds is pinned by hand and then
# fails the catalogue test until KEYWORDS reads it or NOT_READ names it.
DOCUMENTED_KEYWORDS = ("close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved")
# The cases the same page writes a keyword in, `Closes` and `CLOSES`, and the lower case of its list.
DOCUMENTED_CASES = (str.lower, str.title, str.upper)
# What the same page writes between a keyword and its reference, `{}` marking the keyword: a space, as in its
# `Closes #<n>`, and a colon, as in its `Closes: #<n>`.
DOCUMENTED_SEPARATORS = ("{} ", "{}: ")
# Each form of an issue or pull request reference GitHub's pages document, written for number 1 of o/r: the closing
# page's two, `#<n>` and `<owner>/<repo>#<n>`, which also says a keyword before a pull request's reference closes that
# pull request; the rows of the "Issues and pull requests" table of its autolink page
# (https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/autolinked-references-and-urls),
# which adds `GH-<n>` and the URL of an issue or a pull request; and the `redirect.github.com` URL that page's
# "Avoiding backlinks to linked references" offers in place of `github.com`.
DOCUMENTED_REFERENCES = (
    "#1",
    "o/r#1",
    "GH-1",
    "https://github.com/o/r/issues/1",
    "https://github.com/o/r/pull/1",
    "https://redirect.github.com/o/r/issues/1",
)
# Each example the two pages write, after `Closes` where the autolink page writes a bare reference, and the issues the
# reader reads it as closing, the closing page's list that names its keyword before each issue among them.
DOCUMENTED_EXAMPLES = {
    "Closes #10": [ClosingReference(None, 10)],
    "Fixes octo-org/octo-repo#100": [ClosingReference("octo-org/octo-repo", 100)],
    "Resolves #10, resolves #123, resolves octo-org/octo-repo#100": [
        ClosingReference(None, 10),
        ClosingReference(None, 123),
        ClosingReference("octo-org/octo-repo", 100),
    ],
    "Closes: #10": [ClosingReference(None, 10)],
    "CLOSES #10": [ClosingReference(None, 10)],
    "CLOSES: #10": [ClosingReference(None, 10)],
    "Closes https://github.com/jlord/sheetsee.js/issues/26": [ClosingReference("jlord/sheetsee.js", 26)],
    "Closes #26": [ClosingReference(None, 26)],
    "Closes GH-26": [ClosingReference(None, 26)],
    "Closes jlord/sheetsee.js#26": [ClosingReference("jlord/sheetsee.js", 26)],
    "Closes github-linguist/linguist#4039": [ClosingReference("github-linguist/linguist", 4039)],
}


def not_read_keyword(keyword: str) -> bool:
    """Whether NOT_READ names `keyword` as a spelling the reader leaves unread, before the first issue's reference."""
    return f"{keyword} #1" in NOT_READ


def not_read_separator(separator: str) -> bool:
    """Whether NOT_READ names `separator` as a spelling the reader leaves unread, between `Closes` and the first
    issue's reference."""
    return f"{separator.format('Closes')}#1" in NOT_READ


def not_read_reference(reference: str) -> bool:
    """Whether NOT_READ names `reference` as a spelling the reader leaves unread, as `Closes <reference>`."""
    return f"Closes {reference}" in NOT_READ


def undecided(keywords: Iterable[str], separators: Iterable[str], references: Iterable[str]) -> list[tuple[str, str]]:
    """Each keyword, separator, and reference of a catalogue that no table of the reader decides, or that a table reads
    and NOT_READ also names, with its axis."""
    found: list[tuple[str, str]] = []
    for axis, forms, read, not_read in (
        ("keyword", keywords, set(KEYWORDS), not_read_keyword),
        ("separator", separators, set(SEPARATORS_READ), not_read_separator),
        ("reference", references, set(REFERENCES_READ), not_read_reference),
    ):
        found.extend((axis, form) for form in forms if (form in read) == not_read(form))
    return found


def test_the_reader_reads_no_keyword_github_does_not_document() -> None:
    assert sorted(set(KEYWORDS) - set(DOCUMENTED_KEYWORDS)) == [], "remove each: GitHub documents no such keyword"
    # Each documented keyword is a word of the pattern in any case, and a word that only starts like one is none.
    for keyword in DOCUMENTED_KEYWORDS:
        for spelled in (keyword, keyword.title(), keyword.upper(), keyword.swapcase()):
            assert re.fullmatch(CLOSING_KEYWORDS, spelled, re.IGNORECASE), spelled
    for word in ("clos", "closing", "closure", "fixing", "fixs", "fixd", "resolv", "resolving", "resolution", "fixe"):
        assert not re.fullmatch(CLOSING_KEYWORDS, word, re.IGNORECASE), word


def test_every_documented_keyword_separator_and_reference_is_decided_once() -> None:
    assert undecided(DOCUMENTED_KEYWORDS, DOCUMENTED_SEPARATORS, DOCUMENTED_REFERENCES) == [], (
        "add each to KEYWORDS, SEPARATORS_READ, or REFERENCES_READ in closing_references.py with where GitHub "
        "documents it, or name it in closing_references.NOT_READ as `<keyword> #1`, `<separator>#1` for `Closes`, or "
        "`Closes <reference>` with the reason; never in both"
    )


def test_a_form_no_table_decides_or_two_tables_decide_fails_the_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    assert undecided(["closes", "shuts"], ["{} ", "{} -> "], ["#1", "o/r!1"]) == [
        ("keyword", "shuts"),
        ("separator", "{} -> "),
        ("reference", "o/r!1"),
    ]
    assert undecided([], [], ["https://redirect.github.com/o/r/issues/1"]) == []
    monkeypatch.setitem(NOT_READ, "Closes #1", "a reference the reader reads, named here too")
    assert undecided([], [], ["#1"]) == [("reference", "#1")]


def test_each_example_github_documents_is_read_as_its_page_says() -> None:
    for text, expected in DOCUMENTED_EXAMPLES.items():
        assert closing_references(text) == expected, text
        assert title_holds_closing_reference(text), text


def reference_read(reference: str) -> ClosingReference:
    """The issue a reference of REFERENCES_READ names: number 1 of o/r, or of the same repository for a reference that
    starts with `#` or `GH-`."""
    return ClosingReference(None if reference.startswith("#") or reference.upper().startswith("GH-") else "o/r", 1)


def test_every_keyword_case_separator_and_reference_the_catalogue_reads_is_read() -> None:
    misread: list[str] = []
    for keyword, case, separator, reference in itertools.product(
        KEYWORDS, DOCUMENTED_CASES, SEPARATORS_READ, REFERENCES_READ
    ):
        text = separator.format(case(keyword)) + reference
        # With nothing between them, a reference that starts with a letter runs into the keyword, which is then no word.
        if separator == "{}" and reference[0].isalnum():
            if closing_references(text) != [] or title_holds_closing_reference(text):
                misread.append(text)
            continue
        if (
            closing_references(f"Intro.\n{text} and more.\n") != [reference_read(reference)]
            or not title_holds_closing_reference(f"fix: {text}")
            or stray_closing_lines(f"Prose, {text}\n", TEMPLATE) == []
        ):
            misread.append(text)
    assert misread == [], "each is read, held in a title, and named as a stray body line, as the catalogue says"


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


# What the reader does not see, as its NOT_READ names it; a row that starts to read, or to be refused, fails here, so
# the table and the docstring are brought along with the reader.
@pytest.mark.parametrize("text", list(NOT_READ))
def test_what_not_read_names_is_neither_read_nor_refused(text: str) -> None:
    assert closing_references(text) == [], f"remove it from closing_references.NOT_READ: {NOT_READ[text]}"
    assert not title_holds_closing_reference(text), f"remove it from closing_references.NOT_READ: {NOT_READ[text]}"
    assert stray_closing_lines(f"{text}\n", TEMPLATE) == [], NOT_READ[text]


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


# A template as the pull request checks read one: a comment naming the closing line, which no issue number follows, and
# the closing line left with no issue.
TEMPLATE = "<!-- Name each issue in a\n     Closes #N line of its own. -->\nCloses #\n\n<!-- Why. -->\n"


# Each body and the lines it closes an issue on outside a `Closes #<n>` line of its own, as implement-change's
# `pull_request.py check` printed them as `CLOSES_LINE <line> <text>` before it read them here.
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "Closes #1\n\nTwo examples, such as `Closes #12`, read wrong.\n",
            [(3, "Two examples, such as `Closes #12`, read wrong.", False)],
        ),
        ("```text\nCloses #12 when it merges\n```\n", [(2, "Closes #12 when it merges", False)]),
        ("<!-- fixes #12 -->\n", [(1, "<!-- fixes #12 -->", False)]),
        ("Closes #1, #12\n", [(1, "Closes #1, #12", False)]),
        ("- Closes #12\n", [(1, "- Closes #12", False)]),
        ("closes #12\n", [(1, "closes #12", False)]),
        ("Closes: #12\n", [(1, "Closes: #12", False)]),
        ("Closes #0\n", [(1, "Closes #0", False)]),
        ("Closes #012\n", [(1, "Closes #012", False)]),
        ("Closes #abc\n", [(1, "Closes #abc", False)]),
        ("Resolves GH-12\n", [(1, "Resolves GH-12", False)]),
        (
            "  Fixed https://github.com/owner/repo/issues/12  \n",
            [(1, "Fixed https://github.com/owner/repo/issues/12", False)],
        ),
        ("Closes #1\r\n\r\nSee `fixes #12`.\r\n", [(3, "See `fixes #12`.", False)]),
        ("Closes #1\r\rSee `fixes #12`.\r", [(3, "See `fixes #12`.", False)]),
        ("And\tfixes   #13 too.\n", [(1, "And\tfixes   #13 too.", False)]),
        ("  Closes #12 \t\n", []),
        ("Closes #\n", []),
        ("This fixes the crash and fixes GH-hosted runners.\n", []),
        ("This closes #abc.\n", []),
        (TEMPLATE, []),
        (TEMPLATE.replace("Closes #N", "Closes #12"), [(2, "Closes #12 line of its own. -->", False)]),
    ],
)
def test_stray_closing_lines_names_each_line_outside_a_closes_line(
    body: str, expected: list[tuple[int, str, bool]]
) -> None:
    assert stray_closing_lines(body, TEMPLATE) == expected


# Each `Closes #<n>` line of its own the rendered body does not show, which closes its issue on merge without GitHub
# linking it, and each GitHub's renderer links, as `gh api markdown` rendered them: hidden in a comment the author
# wrote, in a fenced code block, closed or left open, and in indented code, at the top or inside a list item; shown
# after a paragraph's text, as a list item's paragraph, under a template comment, inside an HTML block, and after a
# fence that shows a `<!--` as code, which opens no comment.
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Closes #1\n<!--\nCloses #12\n-->\n", [(3, "Closes #12", True)]),
        ("Closes #1\n\n<!-- Notes:\n   Closes #12\n-->\n", [(4, "Closes #12", True)]),
        ("Why.\n\n```\nCloses #12\n```\n", [(4, "Closes #12", True)]),
        ("~~~\nCloses #12\n", [(2, "Closes #12", True)]),
        ("Why.\n\n    Closes #12\n", [(3, "Closes #12", True)]),
        ("- a\n\n      Closes #12\n", [(3, "Closes #12", True)]),
        ("Text\n    Closes #12\n", []),
        ("- a\n\n  Closes #12\n", []),
        ("<!-- Why. -->\nCloses #12\n", []),
        ("<!-- Why. -->Closes #12\n", []),
        ("<details>\nCloses #12\n</details>\n", []),
        ("```\nx\n<!--\n```\nCloses #12\n-->\n", []),
        ("Closes #12\n---\n", []),
        ("Closes #1\n<!--\nfixes #12\n-->\n", [(3, "fixes #12", False)]),
    ],
)
def test_a_closes_line_the_rendered_body_does_not_show_is_named_hidden(
    body: str, expected: list[tuple[int, str, bool]]
) -> None:
    assert stray_closing_lines(body, TEMPLATE) == expected


def test_a_hidden_closes_line_is_read_where_the_template_comments_are_blanked() -> None:
    # The template's own comments stay unread, and a line the author hid below them is found on its own line number.
    body = TEMPLATE.replace("Closes #\n", "Closes #1\n<!--\nCloses #12\n-->\n", 1)
    assert stray_closing_lines(body, TEMPLATE) == [(5, "Closes #12", True)]


# Each title and whether it holds a closing reference, as `pull_request.py check` printed `TITLE_CLOSES` for it before
# it read it here.
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("fix: refuse a bad name, closes #12", True),
        ("fix: close #abc", True),
        ("fix: Resolves GH-12", True),
        ("fix: refuse a bad name, `fixed owner/repo#12`", True),
        ("fix: close the file", False),
        ("ci: run on GH-hosted runners", False),
        ("fix: refuse issue #12's spelling", False),
        ("fix: closes GH-x", False),
    ],
)
def test_title_holds_closing_reference_reads_a_keyword_before_a_reference_start(title: str, expected: bool) -> None:
    assert title_holds_closing_reference(title) is expected


# The repositories a reference names, as the reader read them before its owner/name class moved to github_remote: any
# run of the repository characters on each side of one `/`, and nothing else.
@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("o/r", ["o/r"]),
        ("O-1/r.2", ["O-1/r.2"]),
        ("o_/_r", ["o_/_r"]),
        (".github/.github", [".github/.github"]),
        ("o/.", ["o/."]),
        ("o/..", ["o/.."]),
        ("-o/r-", ["-o/r-"]),
        ("o/r.git", ["o/r.git"]),
        ("a/b/c", []),
        ("o r/x", []),
        ("\u00f6/r", []),
        ("o/r!", []),
        ("/r", []),
        ("o/", []),
        ("o/r\u0661", []),
    ],
)
def test_each_owner_and_name_spelling_is_read_as_before(written: str, expected: list[str]) -> None:
    assert [reference.repository for reference in closing_references(f"Closes {written}#1")] == expected
    assert REPOSITORY == "[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"

"""Regression tests for the one reader of Markdown link destinations."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from link_destinations import Destination, link_destinations


def urls(*lines: str) -> list[str]:
    return [destination.url for destination in link_destinations(list(lines))]


@pytest.mark.parametrize(
    ("line", "url", "path"),
    [
        ("[a](bin/notes.md)", "bin/notes.md", "bin/notes.md"),
        # Percent-encoding is decoded in the path, and only there: the url is what CommonMark reads.
        ("[a](bin/my%20notes.md)", "bin/my%20notes.md", "bin/my notes.md"),
        # Between < and > a destination may hold spaces.
        ("[b](<bin/my notes.md>)", "bin/my notes.md", "bin/my notes.md"),
        # A backslash before a letter is itself: a separator on Windows, part of the name elsewhere.
        ("[c](references\\win.md)", "references\\win.md", "references\\win.md"),
        ("[d](/abs/x.md)", "/abs/x.md", "/abs/x.md"),
        # An escaped punctuation character and a character reference stand for their characters.
        ("[e](a\\(1\\).md)", "a(1).md", "a(1).md"),
        ("[f](a&amp;b&#32;c.md)", "a&b c.md", "a&b c.md"),
        # Balanced parentheses belong to a bare destination.
        ("[g](a(1).md)", "a(1).md", "a(1).md"),
        ("[h](guide.md#the-step)", "guide.md#the-step", "guide.md"),
        ("[i](guide.md?raw=1)", "guide.md?raw=1", "guide.md"),
        ('[j](guide.md "A title")', "guide.md", "guide.md"),
        ("[k](  guide.md  )", "guide.md", "guide.md"),
        # Only a space or a control character ends a bare destination: a no-break space is part of it.
        ("[o](a.md\u00a0)", "a.md\u00a0", "a.md\u00a0"),
        ("![l](image.png)", "image.png", "image.png"),
        ("[m]: refs/def.md", "refs/def.md", "refs/def.md"),
        ('   [n]: <refs/my def.md> "Title"', "refs/my def.md", "refs/my def.md"),
    ],
)
def test_a_destination_is_read_as_commonmark_reads_it(line: str, url: str, path: str) -> None:
    [destination] = link_destinations([line])
    assert (destination.url, destination.path) == (url, path)
    assert line[destination.start : destination.end] == destination.written


@pytest.mark.parametrize(
    "line",
    [
        "[a]()",
        "[a](<>)",
        "[a](<open.md)",
        "[a](<a<b.md>)",
        "[a](a(1.md)",
        "[a](a.md b.md)",
        "[a](a.md",
        "`[a](code.md)`",
        "``[a](`code`.md)``",
        "    [a]: indented.md",
        "[a]: two.md words",
        "a ](not-a-link.md and more",
    ],
)
def test_text_commonmark_reads_as_no_destination_gives_none(line: str) -> None:
    assert link_destinations([line]) == []


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # A label and colon that open no definition leave the line's inline links to read.
        ("[Note]: see [a](missing.md)", ["missing.md"]),
        ("[Note]: two.md words, then [b](b.md)", ["b.md"]),
        # A footnote is no definition, so its text is not read as a destination, and its inline links are.
        ("[^1]: notes.md", []),
        ("[^1]: See [c](c.md).", ["c.md"]),
    ],
)
def test_a_line_that_opens_like_a_definition_but_holds_none_is_read_for_its_links(
    line: str, expected: list[str]
) -> None:
    assert urls(line) == expected


def test_a_link_in_a_code_block_is_code() -> None:
    lines = ["```", "[a](fenced.md)", "```", "", "    [b](indented.md)", "", "[c](prose.md)"]
    assert [destination.line for destination in link_destinations(lines)] == [7]


def test_each_destination_on_a_line_is_read_in_order_with_its_columns() -> None:
    line = "See [a](one.md) and [b](<two words.md>)."
    assert link_destinations([line]) == [
        Destination(1, 8, 14, "one.md", "one.md"),
        Destination(1, 24, 38, "<two words.md>", "two words.md"),
    ]


@pytest.mark.parametrize(
    ("url", "is_url"),
    [
        ("https://example.com/a.md", True),
        ("mailto:a@example.com", True),
        ("//host/a.md", True),
        ("C:/x.md", False),
        ("C:\\x.md", False),
        ("a.md", False),
        ("/abs/a.md", False),
    ],
)
def test_a_scheme_or_a_host_is_a_url_and_a_drive_letter_is_not(url: str, is_url: bool) -> None:
    assert Destination(1, 0, len(url), url, url).is_url is is_url


def test_a_fragment_alone_leads_to_the_document_and_is_decoded() -> None:
    [destination] = link_destinations(["[a](#a%20step)"])
    assert (destination.path, destination.fragment) == ("", "a step")
    assert urls("[a](b.md)", "[c](d.md#e)") == ["b.md", "d.md#e"]

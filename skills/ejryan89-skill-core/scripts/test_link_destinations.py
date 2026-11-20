"""Regression tests for the one reader of Markdown link destinations."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from link_destinations import Destination, link_destinations, written_destination


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
        # A run that no other matches is text, so the single backticks after it pair into a span over the link.
        "``a` [x](y.md) `",
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


def test_a_double_run_never_closes_on_part_of_a_longer_run_so_the_link_after_it_is_read() -> None:
    assert urls("`` [x](y.md) ```") == ["y.md"]


def test_a_line_with_a_long_unmatched_backtick_run_is_read_in_time_in_proportion_to_it() -> None:
    # The old code-span pattern searched each start in the run against the rest of the line: about three minutes. The
    # line opens with text, since one that opens with the run is a fence's opener.
    started = time.perf_counter()
    assert urls("See " + "`" * 32_768 + " [x](y.md) " + "a" * 32_768) == ["y.md"]
    assert time.perf_counter() - started < 1.0


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


@pytest.mark.parametrize(
    ("line", "posix_path"),
    [
        ("[a](./docs/rules.md#step)", "docs/rules.md"),
        ("[a](docs/my%20rules.md)", "docs/my rules.md"),
        ("[a](#step)", ""),
        # A backslash is read as the platform reads one in a path: a separator on Windows, part of the name elsewhere.
        ("[a](docs\\rules.md)", Path("docs\\rules.md").as_posix()),
    ],
)
def test_a_posix_path_names_the_file_as_a_repository_does(line: str, posix_path: str) -> None:
    [destination] = link_destinations([line])
    assert destination.posix_path == posix_path


@pytest.mark.parametrize(
    ("path", "written"),
    [
        ("notes.md", "notes.md"),
        ("decision(cache).md", "decision\\(cache\\).md"),
        ("my notes.md", "<my notes.md>"),
        ("my (notes).md", "<my \\(notes\\).md>"),
        ("100%#1?.md", "100%25%231%3F.md"),
        ("a\\b<c>&amp;.md", "a\\\\b\\<c\\>\\&amp;.md"),
        ("<lead.md", "\\<lead.md"),
        ("tab\tbell\x07.md", "tab%09bell%07.md"),
        # A backslash escapes no backtick from a code span, so a backtick is percent-encoded.
        ("a`b`.md", "a%60b%60.md"),
        ("ünïcode\u00a0name.md", "ünïcode\u00a0name.md"),
    ],
)
def test_a_written_destination_reads_back_as_its_path(path: str, written: str) -> None:
    assert written_destination(path) == written
    # A backtick in the text before the destination pairs with none written in it.
    [destination] = link_destinations([f"- [Ti`tle]({written}) — hook"])
    assert (destination.path, destination.written) == (path, written)

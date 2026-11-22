"""Regression tests for hidden_comments.py, the one reader of the HTML comments GitHub hides.

The readers that read through it, forms_considered, release notes, the silent-revert check, and `implement-change`'s
body check, show their own answers in their own suites; these show the reading's.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hidden_comments import WALK_WINDOW, hidden_spans, read_comments, without_hidden_comments
from markdown_sections import blocks
from test_time_in_proportion import assert_time_in_proportion

COMMENTS = [
    ("a <!-- b --> c", "a  c"),
    # An unclosed `<!--` within a paragraph is text, as GitHub (`gh api markdown`) renders it, whether or not the body
    # ends with a line break, while one that opens an HTML block hides the rest.
    ("a <!-- b", "a <!-- b"),
    ("a <!-- b\n", "a <!-- b\n"),
    ("a <!-- b --> c <!-- d", "a  c <!-- d"),
    ("`<!--` a <!-- b --> c", "`<!--` a  c"),
    ("```\n<!--\n```\nx <!-- y -->", "```\n<!--\n```\nx "),
    # A fence-like line a comment holds opens no block, so the comment ends at its own `-->`.
    ("<!--\n```\n-->\n## Forms considered", "\n## Forms considered"),
    # A backtick a comment holds pairs with none after the comment goes.
    ("<!-- ` --> a <!-- ` --> b", " a  b"),
    # A comment that opens a block ends the paragraph, so an indented line after it is code that shows its `<!--`.
    ("<!-- x -->\n    <!-- y", "\n    <!-- y"),
    ("Text.\n<!-- x -->\n    <!-- y -->", "Text.\n\n    <!-- y -->"),
    # A comment block inside a quote holds its fence-like lines too.
    ("> <!-- x\n> ```\n> -->\n<!-- y -->", "> \n"),
    # A fence-like line inside a comment within a line is a fence, as GitHub reads the blocks before the comments,
    # so the fence shows the `<!--` after it, and the comment inside the line, whose paragraph the fence ends before
    # any `-->`, is text.
    ("a <!-- b\n```\n--> <!-- c -->", "a <!-- b\n```\n--> <!-- c -->"),
    # A `<!--` inside a paragraph, a list item's text, or a heading that its block ends before any `-->` is text,
    # while one that opens an HTML block, at a line's start or a list item's or a quote's content, hides the rest.
    ("a <!-- b\n\nc", "a <!-- b\n\nc"),
    ("- the construct <!-- with no closer\n- VISIBLE", "- the construct <!-- with no closer\n- VISIBLE"),
    ("text <!-- a\n## H\n-->", "text <!-- a\n## H\n-->"),
    ("text <!-- a\nb --> c", "text  c"),
    ("<!-- a\n\nb", ""),
    ("- <!-- a\n- b", "- "),
    ("> <!-- a\n\nb", "> "),
    ("- a\n      <!-- b\n\nc", "- a\n      <!-- b\n\nc"),
    # A comment on a line GitHub passes on as HTML, after a comment block's `-->` on its line, runs to the end.
    ("<!-- a --> b <!-- c\n\nd", " b "),
    ("<!-->x<!--->y", "xy"),
    # Code spans pair whole backtick runs, as GitHub (`gh api markdown`) renders them: the double run no other matches
    # is text, and the single runs after it make a span that shows the comment. The pattern this read before
    # closed a span on the double run's last backtick and hid the comment, giving "``a`` z".
    ("``a`<!-- x -->` z", "``a`<!-- x -->` z"),
    # Past a hidden comment within a paragraph, the line's runs pair from its end, as GitHub renders these: the
    # backtick inside the first comment pairs with none after it, so the second comment is hidden, and the span after
    # a comment shows the one it holds. A line opening with `<!--` would open an HTML block and never be read so.
    ("p <!-- `x --> <!-- z --> `", "p   `"),
    ("p <!-- ` --> `a <!-- b --> c`", "p  `a <!-- b --> c`"),
    # A span before a comment is passed over and the next span, which shows the later comment, is still read.
    ("p `a` <!-- b --> `c <!-- d --> e`", "p `a`  `c <!-- d --> e`"),
    # The body's first `<!--` on a line after its first is read against that line's spans too.
    ("a\n`<!-- x -->` b", "a\n`<!-- x -->` b"),
]


@pytest.mark.parametrize(("text", "expected"), COMMENTS)
def test_a_comment_is_hidden_only_where_it_opens_outside_code(text: str, expected: str) -> None:
    assert without_hidden_comments(text) == expected


# What each stated limit of the module's docstring reads, so a change to one is seen.
LIMITS = [
    pytest.param("> a <!-- b\n>\n> c -->\nd", "> a \nd", id="a comment in a quote ends anywhere in the quote"),
    pytest.param("q\n> a\n> 2. <!-- b\n\nc", "q\n> a\n> 2. ", id="a quoted marker that cannot interrupt opens a block"),
    pytest.param("- <!-- a\nb\n-->\nc", "- \nc", id="a block comment its item stops holding ends at its closer"),
    pytest.param("<div>\n```\n</div>\n\n<!-- a -->", "<div>\n```\n</div>\n\n<!-- a -->", id="a div is a paragraph"),
    pytest.param("<h1>a\n    <!-- b\n\nc", "<h1>a\n    <!-- b\n\nc", id="an indented opener an h1 holds is code"),
    pytest.param("a <!-- b---> c", "a  c", id="a comment whose text ends with a dash is hidden"),
    pytest.param("\\`<!-- x -->\\` z", "\\`<!-- x -->\\` z", id="an escaped backtick opens a span"),
    pytest.param("`foo\nbar <!-- x --> baz` VISIBLE", "`foo\nbar  baz` VISIBLE", id="a span is read within one line"),
]


@pytest.mark.parametrize(("text", "expected"), LIMITS)
def test_a_comment_is_read_within_the_module_s_stated_limits(text: str, expected: str) -> None:
    assert without_hidden_comments(text) == expected


# Bodies of one form repeated to GitHub's limit, each of which once took seconds to minutes to read, or would read in
# time growing with the square of the body if a comment block's walk started again from the body's start.
SCALING = {
    "closed comments": ("", "<!-- c -->\n", ""),
    "empty comments": ("", "<!-->\n", ""),
    "openers a fence shows": ("```\n", "<!--\n", "```\n"),
    "comments inside lines": ("", "x <!-- c --> y\n", ""),
    "unclosed comments inside lines": ("", "x <!-- y\n", ""),
    "comments holding a fence-like line": ("", "<!--\n```\n-->\n", ""),
    "fences showing an opener closed after them": ("", "```\n<!--\n```\n-->\n", ""),
    "quoted comments holding a fence-like line": ("", "> <!--\n> ```\n> -->\n", ""),
    "comments in a list item": ("- a\n", "  <!--\n  x\n  -->\n", ""),
    "comments in a list item holding a fence-like line": ("- a\n", "  <!--\n  ```\n  -->\n", ""),
    "comments in an item in a quote": ("> - a\n", ">   <!--\n>   ```\n>   -->\n", ""),
    "comments their item stops holding": ("", "- <!--\nx\n-->\n", ""),
    "HTML headings holding a fence-like line": ("", "<h2>x</h2>\n```\ntext\n\n", ""),
    # Comments left unclosed in list items, each of which once made every walk read to the end of the body.
    "unclosed comments that open list items": ("", "- <!--\n", ""),
    "unclosed comments in list items": ("", "- a\n  <!--\n", ""),
    "unclosed comments in items above a fence-like line": ("", "1. <!--\n- ```\n", ""),
    "nested list items": ("", "".join("  " * depth + "- <!--\n" + "  " * depth + "  -->\n" for depth in range(60)), ""),
    # A run of quote markers a comment block's start once backtracked through in time exponential in its length.
    "nested quote markers": ("<!-- c -->\n", "> " * 40 + "x\n", ""),
}
# How many times over the block walks may read a body's lines, whatever its comments.
WALKS_PER_LINE = 24

# Fences whose closer, or whose end, falls before, at, and past each width the window doubles through, so every
# window the fence's reading takes reads it alike.
FENCE_LENGTHS = sorted(
    {length for width in (WALK_WINDOW, WALK_WINDOW * 2, WALK_WINDOW * 4) for length in (width - 2, width - 1, width)}
)


@pytest.mark.parametrize("length", FENCE_LENGTHS)
def test_a_fence_longer_than_a_window_shows_its_openers_and_ends_where_it_closes(length: int) -> None:
    fence = "```\n" + "<!-- shown\n" * length + "```\n"
    assert without_hidden_comments(f"{fence}a <!-- b --> c") == f"{fence}a  c"


@pytest.mark.parametrize("length", FENCE_LENGTHS)
def test_an_unclosed_fence_longer_than_a_window_shows_every_opener_to_the_end(length: int) -> None:
    text = "```\n" + "<!-- shown\n" * length + "<!-- also shown -->"
    assert without_hidden_comments(text) == text


@pytest.mark.parametrize("length", FENCE_LENGTHS)
def test_a_quoted_fence_longer_than_a_window_ends_with_its_quote(length: int) -> None:
    fence = "> ```\n" + "> <!-- shown\n" * length
    assert without_hidden_comments(f"{fence}\n<!-- hidden -->after") == f"{fence}\nafter"


@pytest.mark.parametrize(
    ("text", "spans"),
    [
        ("no comment", []),
        ("a <!-- b --> c <!-- d", [(2, 12)]),
        ("<!-- a --> b\n\n<!-- c", [(0, 10), (14, 20)]),
        ("`<!-- shown -->` <!-- hidden -->", [(17, 32)]),
        ("```\n<!-- shown -->\n```\n<!-- hidden -->", [(23, 38)]),
        ("<!-->x<!--->y", [(0, 5), (6, 12)]),
    ],
)
def test_each_hidden_comment_s_span_is_where_its_opener_starts_and_just_past_its_end(
    text: str, spans: list[tuple[int, int]]
) -> None:
    assert hidden_spans(text) == spans


# Each reading's lines, where each starts, and the spans of its hidden comments, at CRLF, a lone CR, and LF, with a
# comment block whose `-->` lies on a later line and a comment within a line that runs past its break. Pinned before
# the reading took its line starts from text_lines.line_starts, and unchanged by it.
READINGS = [
    pytest.param(
        "a\r\n<!-- x\ry -->\nb <!-- c\r\nd --> e\rf\n",
        ["a", "<!-- x", "y -->", "b <!-- c", "d --> e", "f", ""],
        [0, 3, 10, 16, 26, 34, 36],
        [(3, 15), (18, 31)],
        id="a block at a lone CR and a comment within a line past a CRLF",
    ),
    pytest.param(
        "<!--\r\none\r\n-->\r\n\r\ntext <!-- in\rline --> end\r\n",
        ["<!--", "one", "-->", "", "text <!-- in", "line --> end", ""],
        [0, 6, 11, 16, 18, 31, 45],
        [(0, 14), (23, 39)],
        id="a block at CRLFs and a comment within a line past a lone CR",
    ),
    pytest.param(
        "x\r<!-- a -->\r<!-- b\nc -->\n",
        ["x", "<!-- a -->", "<!-- b", "c -->", ""],
        [0, 2, 13, 20, 26],
        [(2, 12), (13, 25)],
        id="a block on one line and one past an LF",
    ),
]


@pytest.mark.parametrize(("text", "lines", "starts", "hidden"), READINGS)
def test_a_reading_numbers_its_lines_at_crlf_cr_and_lf(
    text: str, lines: list[str], starts: list[int], hidden: list[tuple[int, int]]
) -> None:
    reading = read_comments(text)
    assert (reading.lines, reading.starts, reading.hidden) == (lines, starts, hidden)


@pytest.mark.parametrize("text", [text for text, _ in COMMENTS] + [case.values[0] for case in LIMITS])
def test_the_text_without_its_hidden_spans_is_the_text_without_its_hidden_comments(text: str) -> None:
    kept, position = [], 0
    for first, past in hidden_spans(text):
        kept.append(text[position:first])
        position = past
    assert "".join([*kept, text[position:]]) == without_hidden_comments(text)


@pytest.mark.parametrize(("head", "unit", "tail"), SCALING.values(), ids=SCALING.keys())
def test_the_reading_walks_each_line_a_bounded_number_of_times(head: str, unit: str, tail: str) -> None:
    # Counting the lines the walks read, at two sizes, holds the reading linear without timing it.
    for repeats in (100, 400):
        text = head + unit * repeats + tail
        with mock.patch("hidden_comments.blocks", wraps=blocks) as walk:
            hidden_spans(text)
        read = sum(len(call.args[0]) for call in walk.call_args_list)
        assert read <= WALKS_PER_LINE * (text.count("\n") + 1)


# Bodies eight times as long as others: a linear reading takes about eight times as long and a quadratic one about 64
# times. The bound sits halfway between them in log space. The walk count above, which runs first, fails a reading
# that walks too far before this one times it.
SCALE = 8
SCALE_BOUND = SCALE**1.5
SMALL_BODY = 2_000


# A line of one backtick run that no other matches, before a `<!--`, at a size and at sixteen times it. A linear
# reading takes about sixteen times as long, and the pattern this read before, which tried each length of the
# run from each of its backticks, about 4,096 times, taking minutes over the longer line; the bound sits halfway
# between linear and quadratic in log space. The longer line is also read in under a second of processor time.
RUN_FACTOR = 16
RUN_BOUND = RUN_FACTOR**1.5
LONG_RUN = 32_768


def _run_before_a_comment(length: int) -> str:
    return "a " + "`" * length + " <!-- x -->"


def test_a_long_backtick_run_before_a_comment_is_read_in_time_in_proportion_to_it() -> None:
    long = _run_before_a_comment(LONG_RUN)
    started = time.process_time()
    assert without_hidden_comments(long) == "a " + "`" * LONG_RUN + " "
    assert time.process_time() - started < 1
    assert_time_in_proportion(hidden_spans, _run_before_a_comment(LONG_RUN // RUN_FACTOR), long, RUN_BOUND)


@pytest.mark.parametrize(("head", "unit", "tail"), SCALING.values(), ids=SCALING.keys())
def test_a_body_scale_times_as_long_is_read_in_about_scale_times_the_time(head: str, unit: str, tail: str) -> None:
    repeats = max(1, SMALL_BODY // len(unit))
    small, large = head + unit * repeats + tail, head + unit * repeats * SCALE + tail
    assert_time_in_proportion(hidden_spans, small, large, SCALE_BOUND)

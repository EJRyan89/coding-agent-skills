"""Regression tests for the shared Markdown readers."""

from __future__ import annotations

import math
import random
import sys
import time
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bash_syntax import awaits_more
from markdown_sections import (
    ATX_HEADING,
    EXECUTABLE_FENCES,
    FENCE_LANGUAGES,
    FENCE_OPENER,
    HTML_COMMENT,
    POWERSHELL_FENCES,
    SHELL_FENCES,
    CodeSpan,
    CodeSpanReader,
    ListItem,
    blocks,
    code_block_lines,
    code_spans,
    contained_lines,
    document_headings,
    document_items,
    find_fences,
    is_blank,
    list_item,
    markdown_section,
    outside_fences,
    prose_code_spans,
    session_commands,
    starts_block,
    top_level_sections,
    without_code_spans,
)
from test_time_in_proportion import assert_time_in_proportion

NBSP = chr(0xA0)
EM_SPACE = chr(0x2003)
ARABIC_THREE = chr(0x663)


@pytest.mark.parametrize(
    ("line", "text"),
    [
        ("- item", "item"),
        ("+\titem", "item"),
        ("   * item", "item"),
        ("123456789. item", "item"),
        ("7) item", "item"),
        ("- ", ""),
        # Ten digits, a digit outside ASCII, a Unicode space beside the marker, and no space are not list items.
        ("1234567890. item", None),
        (f"{ARABIC_THREE}. item", None),
        (f"1.{NBSP}item", None),
        (f"{EM_SPACE}1. item", None),
        ("1.item", None),
        ("-item", None),
    ],
)
def test_a_list_item_is_read_as_commonmark_reads_one(line: str, text: str | None) -> None:
    assert list_item(line) == text


@pytest.mark.parametrize(
    ("line", "text"),
    [
        ("- item", "item"),
        ("1. item", "item"),
        ("1) item", "item"),
        ("001. item", "item"),
        # An ordered item numbered other than 1, and an item with no text, continue the paragraph above.
        ("2. item", None),
        ("123456789. item", None),
        ("0. item", None),
        ("- ", None),
        ("1.  \t", None),
        # Up to three columns of indentation interrupt; four or more, a tab among them, continue the paragraph.
        ("   - item", "item"),
        ("    - item", None),
        ("\t- item", None),
        ("  \t1. item", None),
    ],
)
def test_only_a_bullet_or_an_item_numbered_one_with_text_interrupts_a_paragraph(line: str, text: str | None) -> None:
    assert list_item(line, interrupting=True) == text


@pytest.mark.parametrize(
    ("line", "opener", "starts"),
    [
        ("> quote", False, True),
        ("   >quote", False, True),
        ("## heading", False, True),
        ("---", False, True),
        (" * * *", False, True),
        ("- item", False, True),
        ("1. item", False, True),
        ("```", True, True),
        ("#", False, True),
        ("  ###\t", False, True),
        # Text, a marker four columns in, an item that may not interrupt, and a no-break space after the marks continue
        # the paragraph.
        ("text", False, False),
        ("    > quote", False, False),
        ("    ## heading", False, False),
        ("2. item", False, False),
        (f"##{NBSP}heading", False, False),
        ("#######", False, False),
        ("    #", False, False),
    ],
)
def test_a_block_start_ends_a_paragraph_and_any_other_line_continues_it(line: str, opener: bool, starts: bool) -> None:
    assert starts_block(line, opener=opener) is starts


@pytest.mark.parametrize(
    ("document", "headings"),
    [
        # A heading ends a nested quote that interrupts a paragraph, and a lazy line continuing it.
        ("para\n> > nested quote\n## after\n", [2]),
        ("> > nested\nlazy line\n## after\n", [2]),
        # One quote marker fewer before a heading leaves it in the outer quote, and an empty heading ends a quote.
        ("> > nested\n> ## inside\n", []),
        ("> quote\n#\n## after\n", [2]),
    ],
)
def test_a_block_start_ends_a_quote_the_walk_reads(document: str, headings: list[int]) -> None:
    assert [index for index, _ in document_headings(document.splitlines())] == headings


# Documents, each with the list items that hold each line, as each item's opening line and marker, outermost first,
# and the HTML `gh api markdown` renders the document as, each line break in it made a space and none kept between tags.
HOLDING_ITEMS = {
    # A lazy line continues the numbered item's paragraph; a line indented to the bullet's content after a blank line
    # leaves the numbered item; and one at the margin after a blank line leaves both.
    # <ul><li><p>a</p><ol><li>b more lazy</li></ol><p>c</p></li></ul><p>d</p>
    "a numbered item in a bullet": (
        "- a\n  1. b\n     more\nlazy\n\n  c\n\nd",
        [[(0, "-")], *[[(0, "-"), (1, "1.")]] * 4, [(0, "-")], [(0, "-")], []],
    ),
    # A line at the margin after an item's indented code is the document's: only a paragraph continues lazily.
    # <ul><li><p>item</p><pre><code>code</code></pre></li></ul><p>lazy</p>
    "a lazy-looking line after indented code": ("- item\n\n      code\nlazy", [[(0, "-")], [(0, "-")], [(0, "-")], []]),
    # A quote inside an item, and an item inside it, hold their lazy lines, the item's own and the quote's.
    # <ol><li>a<blockquote><ul><li>b c d</li></ul></blockquote></li></ol>
    "an item in a quote in an item": (
        "1. a\n   > - b\n   >   c\n   d",
        [[(0, "1.")], [(0, "1."), (1, "-")], [(0, "1."), (1, "-")], [(0, "1."), (1, "-")]],
    ),
    # Two items open on one line, and a line after a blank one indented to the outer item's content leaves the inner.
    # <ul><li><ul><li>inner</li></ul><p>outer</p></li></ul>
    "two items on one line": ("- - inner\n\n  outer", [[(0, "-"), (0, "-")], [(0, "-"), (0, "-")], [(0, "-")]]),
    # An item that opens empty holds the line under it, unless that line is blank, which ends the item.
    # <ul><li>foo</li></ul>
    "an empty item and its text": ("- \n  foo", [[(0, "-")], [(0, "-")]]),
    # <ul><li></li></ul><p>foo</p>
    "an empty item and a blank line": ("- \n\n  foo", [[(0, "-")], [], []]),
    # A blank line in a quote ends the item it holds, and the quote's next line is the quote's alone.
    # <blockquote><ul><li>a</li></ul><p>b</p></blockquote>
    "an item ending in a quote": ("> - a\n>\n> b", [[(0, "-")], [(0, "-")], []]),
}


@pytest.mark.parametrize(("document", "held"), HOLDING_ITEMS.values(), ids=HOLDING_ITEMS.keys())
def test_the_list_items_holding_each_line_are_the_walk_s(document: str, held: list[list[tuple[int, str]]]) -> None:
    lines = document.split("\n")
    walked = document_items(lines)
    assert [[(item.start, item.marker) for item in reversed(list(holders(item)))] for item in walked.holding] == held
    # A line a list item holds is one a container holds, and every item holding a line is one the walk opened.
    assert {index for index, item in enumerate(walked.holding) if item} <= contained_lines(lines)
    assert {item for line in walked.holding for item in holders(line)} <= set(walked.items)


def holders(item: ListItem | None) -> Iterator[ListItem]:
    """The item and each one that holds it, innermost first."""
    while item is not None:
        yield item
        item = item.parent


def test_a_list_item_gives_its_text_and_takes_off_only_the_quotes_that_hold_it() -> None:
    # <blockquote><ol><li>one<blockquote><p>quoted lazy</p></blockquote></li></ol></blockquote>, and for the last two,
    # <ul><li><ul><li>x</li></ul></li></ul> and <blockquote><blockquote><ul><li>y</li></ul></blockquote></blockquote>
    lines = ["> 1. one", ">    > quoted", "lazy"]
    item = document_items(lines).holding[1]
    assert item is not None
    assert (item.start, item.marker, item.after, item.quotes, item.parent) == (0, "1.", 5, 1, None)
    assert item.text(lines[0]) == "one"
    assert [item.unquoted(line) for line in lines] == ["1. one", "   > quoted", "lazy"]
    assert [(found.text(lines[0]), found.parent) for found in document_items(lines).items] == [("one", None)]
    outer, inner = document_items(["- - x"]).items
    assert [(found.text("- - x"), found.parent) for found in (outer, inner)] == [("- x", None), ("x", outer)]
    assert document_items(["- - x"]).items[0] != document_items(["- - x"]).items[0]
    (deep,) = document_items(["> > *   y"]).items
    assert (deep.marker, deep.text("> > *   y"), deep.quotes) == ("*", "y", 2)


# Documents, each with the lines a list item or a quote holds as GitHub renders it (`gh api markdown`).
CONTAINED_LINES = {
    # A numbered line under a bullet's text opens an item of its own, whose content starts at the fourth column, so a
    # line indented two columns after a blank line is the document's; the blank line, as every one under an open item,
    # is the item's.
    "a numbered item that leaves a bullet": ("- a\n2. b\n\n  text\n  ---", {0, 1, 2}),
    # A lazy line continues the quote's paragraph, and so does an `=` line under it, while a `-` line is a rule.
    "lazy lines of a quote": ("> a\nlazy\n===\n---\nafter", {0, 1, 2}),
    # An item with text interrupts a paragraph and holds the lines indented to its content.
    "an item under a paragraph": ("text\n- # heading\n  w3\n  ---\n---", {1, 2, 3}),
    # A fence's lines inside an item are the item's, and the line indented less ends both.
    "a fence in an item": ("- a\n  ```\n  x\n  ```\nb", {0, 1, 2, 3}),
    # An item that opens empty holds its own line, and a blank line under it ends it.
    "an item that opens empty": ("- \n\n  text", {0}),
    "no container": ("# h\n\ntext\n---", set()),
}


@pytest.mark.parametrize(("document", "held"), CONTAINED_LINES.values(), ids=CONTAINED_LINES.keys())
def test_the_lines_a_container_holds_are_the_walk_s(document: str, held: set[int]) -> None:
    lines = document.split("\n")
    assert contained_lines(lines) == held
    assert not {index for index, _ in document_headings(lines)} & held


def test_a_fence_inside_a_list_item_reads_the_marker_as_commonmark_does() -> None:
    assert FENCE_OPENER.fullmatch("3. ```bash") is not None
    assert FENCE_OPENER.fullmatch("123456789) ~~~") is not None
    assert FENCE_OPENER.fullmatch(f"{ARABIC_THREE}. ```bash") is None
    assert FENCE_OPENER.fullmatch("1234567890. ```bash") is None


def test_a_nested_item_is_read_at_any_indentation_where_it_interrupts_nothing() -> None:
    assert list_item("        - nested") == "nested"
    assert list_item("\t\t2. nested") == "nested"


def test_a_fence_closes_only_on_a_run_indented_at_most_three_columns_with_spaces_and_tabs() -> None:
    # Another Unicode space is text, and a space and a tab reach the fourth column, so none of them closes the fence.
    lines = ["```", "body", f"{EM_SPACE}```", f"{NBSP}```", " \t```", "   ```", "after"]
    assert find_fences(lines)[0].lines() == range(6)
    assert find_fences(["```", "body", "```\r", "after"])[0].lines() == range(3)


def test_a_heading_followed_by_another_unicode_space_is_not_that_heading() -> None:
    assert markdown_section(f"## One{NBSP}\nfirst\n## One\nsecond", "## One") == "second"


def test_a_section_s_lines_end_where_commonmark_ends_one() -> None:
    # A lone CR ends a line as CRLF and LF do, and each becomes LF.
    assert markdown_section("## One\rfirst\r\nsecond\r## Two\rthird", "## One") == "first\nsecond"
    # A line separator ends no line, so the heading it holds is text.
    assert markdown_section("## One\nfirst\u2028## Two\nlast", "## One") == "first\u2028## Two\nlast"


def test_only_spaces_and_tabs_make_a_line_blank() -> None:
    assert is_blank("")
    assert is_blank(" \t ")
    assert not is_blank(NBSP)
    assert not is_blank(EM_SPACE)


class MarkdownSectionTests(unittest.TestCase):
    def test_first_exact_heading_allows_trailing_whitespace_but_not_a_prefix_or_indentation(self) -> None:
        text = " ## One\nindented\n## One more\nlonger\n## One \t\nfirst  \n## One\nsecond"
        self.assertEqual("first  ", markdown_section(text, "## One"))
        self.assertIsNone(markdown_section(text, "## Missing"))

    def test_only_a_second_level_heading_ends_the_section_and_comments_are_preserved(self) -> None:
        text = "## One\n### Inner\n<!-- comment -->\n# Title\n##Two\n ## Two\nkept\t\n## End \t\noutside"
        self.assertEqual(
            "### Inner\n<!-- comment -->\n# Title\n##Two\n ## Two\nkept\t", markdown_section(text, "## One")
        )

    def test_a_second_level_line_inside_a_fence_is_neither_the_heading_nor_the_end(self) -> None:
        text = "```\n## One\nfenced\n```\n## One\nfirst\n~~~text\n## not a heading\n~~~\nlast\n## End\noutside"
        self.assertEqual("first\n~~~text\n## not a heading\n~~~\nlast", markdown_section(text, "## One"))
        self.assertIsNone(markdown_section("```\n## One\nfenced", "## One"))

    def test_crlf_is_normalized_without_stripping_body_whitespace_or_final_newlines(self) -> None:
        text = "## One \t\r\n\r\nfirst  \r\nlast\t\r\n"
        self.assertEqual("\nfirst  \nlast\t\n", markdown_section(text, "## One"))
        self.assertEqual("", markdown_section("## One", "## One"))


class CodeSpanTests(unittest.TestCase):
    # What the single-backtick pattern code_spans replaced read from these lines, which code_spans reads too.
    def test_a_code_span_is_one_backtick_pair_on_one_line_with_its_text_kept(self) -> None:
        self.assertEqual(
            ["git status", " a.py "], [span.text for span in code_spans("Run `git status`, then ` a.py `.")]
        )

    def test_a_span_does_not_cross_a_line_or_match_an_unclosed_backtick(self) -> None:
        self.assertEqual([], code_spans("`open\nclose and `unclosed"))


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param(
            "`a` and ``b ` c`` and ```d```",
            [CodeSpan(0, 3, 1, "a"), CodeSpan(8, 17, 2, "b ` c"), CodeSpan(22, 29, 3, "d")],
            id="each run closes on the next run of its length and a span may hold a shorter run",
        ),
        # The issue's line: CommonMark reads the double run, which no other matches, as text, and pairs the single
        # backticks after it into a span over the link. The old pattern opened a span on the double run's last backtick.
        pytest.param("``a` [x](y.md) `", [CodeSpan(3, 16, 1, " [x](y.md) ")], id="an unmatched run is text"),
        # The old pattern closed a single run's span on the first backtick of the double run.
        pytest.param("`a``", [], id="a span never closes on part of a longer run"),
        pytest.param("`` [x](y.md) ```", [], id="a double run never closes on part of a triple run"),
        pytest.param("a``b`` ```c``", [CodeSpan(1, 6, 2, "b")], id="runs after a span pair only with their own length"),
        pytest.param("````", [], id="one run is no span"),
        pytest.param("``open\nclose``", [], id="a line break ends a run's search"),
        pytest.param("x\n`b`", [CodeSpan(2, 5, 1, "b")], id="a span after a line break keeps its column"),
        pytest.param("a\nbb\n`c`", [CodeSpan(5, 8, 1, "c")], id="a span after two line breaks keeps its column"),
    ],
)
def test_a_code_span_pairs_whole_backtick_runs_as_commonmark_does(line: str, expected: list[CodeSpan]) -> None:
    assert code_spans(line) == expected


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        pytest.param(0, [CodeSpan(0, 3, 1, "a"), CodeSpan(8, 13, 1, " c "), CodeSpan(14, 17, 1, "e")], id="the start"),
        pytest.param(3, [CodeSpan(8, 13, 1, " c "), CodeSpan(14, 17, 1, "e")], id="past a span"),
        # From inside ` c `, its closing backtick opens a span the next run closes, and the last run is text.
        pytest.param(9, [CodeSpan(12, 15, 1, " ")], id="inside a span"),
        pytest.param(17, [], id="the end"),
    ],
)
def test_a_line_s_spans_are_read_as_if_the_line_started_at_the_column(column: int, expected: list[CodeSpan]) -> None:
    assert list(CodeSpanReader("`a` and ` c ` `e`").spans(column)) == expected


def test_a_run_that_starts_before_the_column_opens_no_span() -> None:
    # From the start, the two double runs pair into `a`; from column 1, the second has no partner and is text.
    assert list(CodeSpanReader("``a`` `b`").spans(1)) == [CodeSpan(6, 9, 1, "b")]


def test_a_run_with_no_partner_is_text_and_the_reading_goes_on_past_it() -> None:
    # The first single run pairs with the second, past the double run; the last double run has no partner after it, so
    # it is text, and the first double run, inside the span, opens nothing.
    assert [span.text for span in CodeSpanReader("`a``b`c``").spans()] == ["a``b"]


def test_a_span_s_text_starts_past_its_opening_run() -> None:
    line = "see ``a ` b`` here"
    [span] = code_spans(line)
    assert (span.text_start, line[span.text_start : span.end - span.run]) == (6, "a ` b")


@pytest.mark.parametrize(
    ("fill", "expected"),
    [("", "see  and [x](y) ``"), (" ", "see " + " " * 10 + " and [x](y) ``")],
    ids=["removed", "blanked"],
)
def test_a_line_without_its_code_spans_keeps_an_unmatched_run(fill: str, expected: str) -> None:
    assert without_code_spans("see ``[a](b)`` and [x](y) ``", fill) == expected


def test_a_line_without_its_code_spans_drops_them_by_default() -> None:
    assert without_code_spans("a `b` c") == "a  c"


# Lines sixteen times as long as others: a linear reading takes about sixteen times as long; a reading that searches
# the rest of the line from each run that no other matches, as the old pattern did, about 64 times; and a quadratic
# one about 256 times. Each line's bound sits halfway in log space between linear and the slower reading it catches:
# SPAN_BOUND below a reading 64 times as long, and SPAN_QUADRATIC_BOUND below a quadratic one, which leaves a linear
# reading a margin of about four rather than two.
SPAN_FACTOR = 16
SPAN_BOUND = SPAN_FACTOR**1.25
SPAN_QUADRATIC_BOUND = SPAN_FACTOR**1.5
# Each shape at a size and at SPAN_FACTOR times it, its bound, and the slower reading it catches: one long run that no
# other matches, which the old pattern, `(`+)(.+?)\1`, searched from each backtick of the run to the line's end; a run
# of every length up to about the line's square root, none matched, which a pattern that pairs whole runs, such as
# ``(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)``, searches from each run to the line's end; many spans of one run length,
# which a reader that looked for a closer among every run of the length, not only those past the opener, reads in
# time growing with their square; and unmatched runs of every length up to four times the square root of the spans
# after them, which a reader that scanned the later runs one by one for each opener's closer reads in time growing
# with the spans times that root. Four times the root makes that scan most of the slower reading's time on the
# shorter line too, so it takes close to 64 times as long on the longer one, as far above the bound as a linear
# reading is below it.
SPAN_SCALING = {
    "one long unmatched run": (lambda size: "`" * size + "a" * size, 256, 256 * SPAN_FACTOR, SPAN_BOUND),
    "unmatched runs of every length": (
        lambda size: "".join("`" * length + "a" for length in range(1, size + 1)),
        64,
        257,
        SPAN_BOUND,
    ),
    "many spans": (lambda size: "`a` " * size, 256, 256 * SPAN_FACTOR, SPAN_QUADRATIC_BOUND),
    "unmatched runs before many spans": (
        lambda size: "".join("`" * length + "a" for length in range(2, 4 * math.isqrt(size) + 2)) + "`a` " * size,
        1024,
        1024 * SPAN_FACTOR,
        SPAN_BOUND,
    ),
}


@pytest.mark.parametrize(("shape", "small", "large", "bound"), SPAN_SCALING.values(), ids=SPAN_SCALING.keys())
def test_a_line_s_code_spans_are_read_in_time_in_proportion_to_it(
    shape: Callable[[int], str], small: int, large: int, bound: float
) -> None:
    # The runs of every length grow as the square of their count, so four times the lengths is about sixteen times
    # the line.
    assert round(len(shape(large)) / len(shape(small))) == SPAN_FACTOR
    assert_time_in_proportion(code_spans, shape(small), shape(large), bound)


class HtmlCommentTests(unittest.TestCase):
    def test_a_comment_is_removed_across_lines_and_each_one_ends_at_its_own_close(self) -> None:
        self.assertEqual("a  b  c", HTML_COMMENT.sub("", "a <!-- one\ntwo --> b <!-- three --> c"))
        self.assertEqual("a <!-- open", HTML_COMMENT.sub("", "a <!-- open"))

    def test_prose_spans_skip_every_code_block_and_keep_the_line_index(self) -> None:
        text = "Run `a`.\n```bash\n`b`\n```\n> ~~~sh\n> `c`\n> ~~~\n    ```\n    `d`\n    ```\nthen `e`"
        self.assertEqual([(0, "a"), (10, "e")], prose_code_spans(text.split("\n")))

    def test_prose_spans_skip_an_indented_code_block_s_backticks(self) -> None:
        # GitHub renders a backtick run inside indented code as the block's text, never a code span.
        self.assertEqual([(2, "y")], prose_code_spans(["# H", "    run `x`", "then `y`"]))

    def test_prose_spans_pair_whole_backtick_runs(self) -> None:
        # The single-backtick pattern prose_code_spans read before read these lines as ["a"], ["x", " and "], and
        # [" ", " "]: a single backtick inside or beside a double run paired with another.
        lines = ["``a`b``", "see ``x`` and `y`", "`` `x` ``"]
        self.assertEqual([(0, "a`b"), (1, "x"), (1, "y"), (2, " `x` ")], prose_code_spans(lines))


class FenceLanguageTests(unittest.TestCase):
    def test_each_label_names_one_language_and_the_sets_are_drawn_from_them(self) -> None:
        labels = [label for found in FENCE_LANGUAGES.values() for label in found]
        self.assertEqual(len(labels), len(set(labels)), "a label names two languages")
        self.assertEqual(frozenset(labels), EXECUTABLE_FENCES)
        self.assertEqual(
            frozenset({"bash", "sh", "shell", "console", "zsh", "ksh", "dash", "fish", "cmd", "bat", "batch"}),
            SHELL_FENCES,
        )
        self.assertEqual(frozenset({"powershell", "pwsh", "ps1"}), POWERSHELL_FENCES)
        for label in ("py", "python3", "js", "node", "ts"):
            with self.subTest(label=label):
                self.assertIn(label, EXECUTABLE_FENCES)
        self.assertTrue(all(label == label.casefold() for label in labels), "labels are compared casefolded")


def fences(text: str) -> list[tuple[int, int | None, str, int, list[str]]]:
    lines = text.split("\n")
    return [
        (fence.opener, fence.closer, fence.language, fence.stop, [fence.text(lines[i]) for i in fence.body()])
        for fence in find_fences(lines)
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("```bash\necho\n```", [(0, 2, "bash", 2, ["echo"])], id="top level"),
        pytest.param("~~~zsh\necho\n~~~", [(0, 2, "zsh", 2, ["echo"])], id="tildes"),
        pytest.param("1. ```sh\n   echo\n   ```", [(0, 2, "sh", 2, ["   echo"])], id="on a list item's marker line"),
        pytest.param("> ```bash\n> echo\n>```", [(0, 2, "bash", 2, ["echo"])], id="in a blockquote"),
        pytest.param(
            "> - ```bash\n>   echo\n>   ```", [(0, 2, "bash", 2, ["  echo"])], id="in a list item in a blockquote"
        ),
        pytest.param("> > ```bash\n> > echo\n> > ```", [(0, 2, "bash", 2, ["echo"])], id="in nested blockquotes"),
    ],
)
def test_every_fence_form_markdown_allows_is_read(
    text: str, expected: list[tuple[int, int | None, str, int, list[str]]]
) -> None:
    assert expected == fences(text)


@pytest.mark.parametrize("marker", [">", "*"])
def test_a_long_run_of_container_markers_is_read_in_linear_time(marker: str) -> None:
    # A run of whitespace could once be split between two markers in exponentially many ways before a mismatch.
    started = time.perf_counter()
    assert find_fences([marker + f"\t\t{marker}" * 5000 + "x"]) == []
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("````text\n```\n````", [(0, 2, "text", 2, ["```"])], id="a shorter run is body"),
        pytest.param(
            "```text\n> ```\n```",
            [(0, 2, "text", 2, ["> ```"])],
            id="a quoted run does not close an unquoted fence",
        ),
        pytest.param("```a`b```\nprose", [], id="a backtick run with a backtick in its info is inline code"),
        pytest.param("```bash\necho", [(0, None, "bash", 2, ["echo"])], id="an unclosed fence runs to the end"),
        pytest.param(
            "> ```bash\n> echo\nprose\n```sh\nls\n```",
            [(0, None, "bash", 2, ["echo"]), (3, 5, "sh", 5, ["ls"])],
            id="an unclosed quoted fence ends with its blockquote",
        ),
    ],
)
def test_a_fence_closes_only_on_its_own_run_inside_what_holds_it(
    text: str, expected: list[tuple[int, int | None, str, int, list[str]]]
) -> None:
    assert expected == fences(text)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("## Contents", ("##", "Contents")),
        ("   ###\tContents ##  ", ("###", "Contents")),
        ("# Contents#", ("#", "Contents#")),
        pytest.param("#\u00a0Contents", None, id="no-break-space"),
        pytest.param("#\u2003Contents", None, id="em-space"),
        ("#Contents", None),
        ("    # Indented", None),
        ("####### Seven", None),
    ],
)
def test_an_atx_heading_is_read_as_commonmark_reads_one(line: str, expected: tuple[str, str] | None) -> None:
    heading = ATX_HEADING.match(line)
    assert expected == (None if heading is None else (heading.group("marks"), heading.group("text")))


@pytest.mark.parametrize(
    ("lines", "content"),
    [
        # A list item's fence: up to the item's indentation comes off each line. A line indented less, as "  y",
        # ends the item, and the fence with it, as CommonMark reads it.
        (["1. Step:", "", "   ```bash", "   EOF", "     x", "  y", "   ```"], ["EOF", "  x"]),
        # A fence indented on its own takes up to its indentation off each line.
        (["  ```", "    a", " b", "c", "  ```"], ["  a", "b", "c"]),
        # A blockquote's markers come off first, then the indentation inside it.
        (["> - ```sh", ">   EOF", "> ```"], ["EOF"]),
        # A tab counts to the next multiple of four, and one that runs past the indentation gives its columns past
        # it as spaces, as CommonMark gives a partly consumed tab.
        (["- a", "", "\t```", "\tEOF", "  \tx", "\t```"], ["EOF", "x"]),
        (["  ```", "\tx", "  ```"], ["  x"]),
        (["- ```", "\t\tx", "```"], ["  \tx"]),
    ],
)
def test_a_fence_s_content_is_each_line_without_the_indentation_commonmark_takes(
    lines: list[str], content: list[str]
) -> None:
    fence = find_fences(lines)[0]
    assert [fence.content(lines[index]) for index in fence.body()] == content


@pytest.mark.parametrize(
    ("document", "headings"),
    [
        pytest.param("# A\n\n- item\n\n  ## In the item\n\n## B\n", ["A", "B"], id="indented-under-a-bullet"),
        pytest.param("1. item\n\n   ## In the item\n\n  ## Out\n", ["Out"], id="under-an-ordered-item"),
        pytest.param("- item\nlazy\n  ## In the item\n## Out\n", ["Out"], id="after-a-lazy-continuation"),
        pytest.param("- a\n  - b\n    - c\n\n      ## In c\n\n  ## In a\n", [], id="nested-items"),
        pytest.param("- ```\n  ## Fenced\n  ```\n  ## In the item\n", [], id="fence-opening-an-item"),
        pytest.param("- item\n\n```\nx\n```\n  ## Out\n", ["Out"], id="fence-ending-the-list"),
        pytest.param("    ## Code\n\n> ## Quoted\n\n   ## Out ##\n", ["Out"], id="code-quote-and-closing-run"),
        # A line that starts a block ends a quoted paragraph and the quote, rather than lazily continuing it.
        pytest.param("## A\n\n> note\n## B\n", ["A", "B"], id="heading-after-a-quote"),
        pytest.param("> note\n   # Out\n", ["Out"], id="indented-heading-after-a-quote"),
        pytest.param("## A\n\n> a\n---\n## B\n", ["A", "B"], id="thematic-break-after-a-quote"),
        pytest.param("> a\n```\n## Fenced\n```\n## Out\n", ["Out"], id="fence-after-a-quote"),
        pytest.param("> a\n- item\n## Out\n", ["Out"], id="list-item-after-a-quote"),
        pytest.param("> a\nlazy\n## Out\n", ["Out"], id="heading-after-a-lazy-quote-line"),
        # Past MAX_BLOCK_INDENT columns a heading-shaped line continues the paragraph above it.
        pytest.param("Text\n    # Not a heading\n   # Out\n", ["Out"], id="deep-line-after-a-paragraph"),
    ],
)
def test_a_document_heading_is_one_no_list_item_quote_or_code_block_holds(document: str, headings: list[str]) -> None:
    assert [heading.group("text") for _, heading in document_headings(document.split("\n"))] == headings


@pytest.mark.parametrize(
    ("document", "code"),
    [
        # A fence, a heading, or a list item that interrupts the item's paragraph ends the items indented past it, so
        # the line after it is indented code at the document's level.
        pytest.param("- item\n\n```\nx\n```\n    code\n", [2, 3, 4, 5], id="after-a-fence-ending-the-list"),
        pytest.param("- item\n## Heading\n    code\n", [2], id="after-a-heading-ending-the-list"),
        # A nested item is read past the content of the item that holds it, so its text holds the indented line.
        pytest.param("- a\n  - b\n    - c\n\n        text\n", [], id="inside-a-nested-item"),
        pytest.param("> a\n## Heading\n    code\n", [2], id="after-a-heading-ending-a-quote"),
        # A heading up to MAX_BLOCK_INDENT columns past an item's content ends its paragraph; one further in is the
        # paragraph's text, inside an item or lazily continuing a quote, so the line after it is no code.
        pytest.param("- item\n     # Heading\n      code\n", [2], id="after-a-heading-in-an-item"),
        pytest.param("- item\n      # Not a heading\n      more\n", [], id="deep-line-in-an-item-s-paragraph"),
        pytest.param("> a\n    # lazy\n    more\n", [], id="deep-line-lazily-continuing-a-quote"),
    ],
)
def test_a_block_that_ends_a_list_item_ends_its_indentation(document: str, code: list[int]) -> None:
    assert sorted(code_block_lines(document.split("\n"))) == code


# The documents every reader of Markdown's blocks splits alike, since each walks them through `blocks`: a quote and a
# thematic break after text, a `1.` item, which interrupts a paragraph, a `2.` line, which continues one, indented
# code, and a `2.` item after a paragraph a list item holds past a blank line, which opens the list's next item. Each
# reader's suite holds its reading of the same documents.
SHARED_SPLITS = [
    pytest.param("Text.\n> quoted\n", [(0, "paragraph", "Text."), (1, "quote", "> quoted")], id="quote"),
    pytest.param("Text.\n***\nMore.\n", [(0, "paragraph", "Text."), (2, "paragraph", "More.")], id="thematic-break"),
    pytest.param("Text.\n1. first\n", [(0, "paragraph", "Text."), (1, "item", "1. first")], id="item-numbered-1"),
    pytest.param("Text.\n2. second\n", [(0, "paragraph", "Text. 2. second")], id="line-numbered-2"),
    pytest.param("Text.\n\n    code\n", [(0, "paragraph", "Text."), (2, "code", "code")], id="indented-code"),
    pytest.param(
        "1. Run it.\n\n   It prints.\n2. Then stop.\n",
        [(0, "item", "1. Run it."), (2, "paragraph", "It prints."), (3, "item", "2. Then stop.")],
        id="item-after-an-item-s-paragraph",
    ),
]


@pytest.mark.parametrize(("document", "expected"), SHARED_SPLITS)
def test_every_reader_s_blocks_split_where_commonmark_splits_them(
    document: str, expected: list[tuple[int, str, str]]
) -> None:
    assert [(block.start, block.kind, block.text()) for block in blocks(document.split("\n"))] == expected


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param("# Title\nBody.\n#\n", [(0, "heading"), (1, "paragraph"), (2, "heading")], id="headings"),
        pytest.param("Tag notes.\n#hashtag\n", [(0, "paragraph")], id="hashtag-continues"),
        pytest.param("#hashtag opens\n", [(0, "paragraph")], id="hashtag-opens"),
        pytest.param("| a |\n| - |\n\nAfter.\n", [(0, "table row"), (1, "table row"), (3, "paragraph")], id="table"),
        pytest.param("- a\n  more\n- b\n2. c\n", [(0, "item"), (2, "item"), (3, "item")], id="items"),
        pytest.param("> a\n> b\nlazy\n- item\n", [(0, "quote"), (3, "item")], id="quote-lines"),
        pytest.param("Text.\n```\nx\n```\nMore.\n", [(0, "paragraph"), (2, "code"), (4, "paragraph")], id="fence"),
        pytest.param("```\n```\n", [(1, "code")], id="empty-fence"),
        # Past MAX_BLOCK_INDENT columns a heading-shaped line or a table row continues the paragraph above it, counted
        # in a list item from the column its content starts at.
        pytest.param("Text.\n    # deep\n", [(0, "paragraph")], id="deep-heading-after-text"),
        pytest.param("Text.\n    | deep |\n", [(0, "paragraph")], id="deep-row-after-text"),
        pytest.param("- item\n     # Heading\n", [(0, "item"), (1, "heading")], id="heading-in-an-item"),
        pytest.param("- item\n      # deep\n", [(0, "item")], id="deep-line-in-an-item"),
        # A list item stays open across a blank line, so a line is read at the column of its content: a quote or a
        # break up to MAX_BLOCK_INDENT columns past it ends the item's paragraph, and a numbered line inside the item
        # continues it, since only `1.` starts a list there.
        pytest.param("- item\n    > quoted\n", [(0, "item"), (1, "quote")], id="quote-in-an-item"),
        pytest.param("- item\n    ***\nAfter.\n", [(0, "item"), (2, "paragraph")], id="break-in-an-item"),
        pytest.param("- item\n  2. more\n", [(0, "item")], id="numbered-line-in-an-item"),
        pytest.param("- a\n\n  b\n- c\n", [(0, "item"), (2, "paragraph"), (3, "item")], id="item-after-a-paragraph"),
        pytest.param("- a\n\n  b\n  2. c\n", [(0, "item"), (2, "paragraph")], id="numbered-line-in-a-paragraph"),
        pytest.param("- a\n\nb\n2. c\n", [(0, "item"), (2, "paragraph")], id="paragraph-after-the-list"),
        # A fence opened on a list item's line opens the item, so a `2.` line after the item's paragraph opens the
        # list's next item; a fence at the margin ends the items, so a `2.` line after a later paragraph continues it.
        pytest.param(
            "- ```\n  x\n  ```\n  Text.\n2. Next.\n",
            [(1, "code"), (3, "paragraph"), (4, "item")],
            id="fence-opens-an-item",
        ),
        pytest.param(
            "- a\n```\nx\n```\n\n  b\n2. c\n", [(0, "item"), (2, "code"), (5, "paragraph")], id="fence-ends-the-items"
        ),
    ],
)
def test_a_block_is_read_as_github_renders_it(document: str, expected: list[tuple[int, str]]) -> None:
    assert [(block.start, block.kind) for block in blocks(document.split("\n"))] == expected


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        # A setext heading is read as a paragraph: an `=` underline joins it, and a `---` one is a thematic break.
        pytest.param("Title\n=====\nText.\n", [(0, "paragraph")], id="setext-equals"),
        pytest.param("Title\n---\nText.\n", [(0, "paragraph"), (2, "paragraph")], id="setext-dashes"),
        # An HTML block is read as a paragraph, until a line that starts a block.
        pytest.param("Text.\n<!-- note -->\n", [(0, "paragraph")], id="html"),
        pytest.param("<div>\n# x\n</div>\n", [(0, "paragraph"), (1, "heading"), (2, "paragraph")], id="html-heading"),
        # A list item's own line is the item's block, so a heading on it is the item's text.
        pytest.param("- # H\n", [(0, "item")], id="heading-on-an-item-s-line"),
        # A bare marker right after a quoted line continues the quote, since it opens no item.
        pytest.param("> quoted\n-\n", [(0, "quote")], id="bare-marker-after-a-quote"),
        # A line that starts with `|` is a table row even after a paragraph's text, and a table with no leading pipe
        # is read as a paragraph.
        pytest.param("Text\n| a |\n", [(0, "paragraph"), (1, "table row")], id="pipe-after-text"),
        pytest.param("a | b\n---|---\nc | d\n", [(0, "paragraph")], id="table-without-leading-pipes"),
        # A list marker with nothing after it opens no item, so it and the lines after it are a paragraph.
        pytest.param("-\n  a\n- b\n", [(0, "paragraph"), (2, "item")], id="bare-marker"),
    ],
)
def test_a_block_is_read_within_the_walker_s_stated_limits(document: str, expected: list[tuple[int, str]]) -> None:
    assert [(block.start, block.kind) for block in blocks(document.split("\n"))] == expected


@pytest.mark.parametrize(
    ("document", "expected", "code", "headings"),
    [
        # A `2.` marker that leaves an item opens the next list's item, even after the item's paragraph, so the line
        # indented short of its content is the document's heading, and one indented as far is the item's paragraph.
        pytest.param(
            "- a\n2. b\n  # H\n", [(0, "item"), (1, "item"), (2, "heading")], [], ["H"], id="ordered-leaving-a-bullet"
        ),
        pytest.param(
            "- a\n\n  p\n10. b\n\n      x\n",
            [(0, "item"), (2, "paragraph"), (3, "item"), (5, "paragraph")],
            [],
            [],
            id="item-ten-after-an-item-s-paragraph",
        ),
        # A marker more than MAX_BLOCK_INDENT columns past the content of the item it leaves opens none: it lazily
        # continues the nested item's paragraph.
        pytest.param(
            "- a\n  -    b\n      - c\n", [(0, "item"), (1, "item")], [], [], id="deep-marker-leaving-an-item"
        ),
        # A marker whose text opens indented code opens its item, which holds the lines indented as far as its
        # content, so a `2.` line after the item's paragraph opens the list's next item, and a heading is the item's.
        pytest.param(
            "-     code\n\n  text\n2. next\n",
            [(0, "code"), (2, "paragraph"), (3, "item")],
            [0],
            [],
            id="item-opening-code-holds-a-paragraph",
        ),
        pytest.param(
            "-     code\n\n  # H\n", [(0, "code"), (2, "heading")], [0], [], id="item-opening-code-holds-a-heading"
        ),
        # A quote-shaped line inside a fence is the fence's text, so the item holding the fence stays open after it.
        pytest.param(
            "- a\n\n  ```\n  > x\n  ```\n  # H\n",
            [(0, "item"), (3, "code"), (5, "heading")],
            [2, 3, 4],
            [],
            id="quote-shaped-line-in-a-fenced-item",
        ),
        # A blockquote inside a list item leaves the item open: a marker that leaves both opens the list's next item,
        # and a heading the item holds ends the quote but stays in the item.
        pytest.param(
            "1. Do X.\n   > Note\n2. Do Y.\n",
            [(0, "item"), (1, "quote"), (2, "item")],
            [],
            [],
            id="quote-in-an-item-then-the-next-item",
        ),
        pytest.param(
            "1. Do X.\n\n   > Note\n2. Do Y.\n",
            [(0, "item"), (2, "quote"), (3, "item")],
            [],
            [],
            id="quote-after-a-blank-line-in-an-item-then-the-next-item",
        ),
        pytest.param(
            "* s\n  > q\n    # H\n",
            [(0, "item"), (1, "quote"), (2, "heading")],
            [],
            [],
            id="heading-ending-a-quote-in-an-item",
        ),
        # A marker other than `1.` after a quoted line opens a list, since the paragraph is the quote's.
        pytest.param("> quoted\n2. more\n", [(0, "quote"), (1, "item")], [], [], id="numbered-item-after-a-quote"),
        # A `|` line that lazily continues an item's paragraph is that paragraph's text, so the item stays open; a
        # table row is a paragraph's text for what may follow it.
        pytest.param("- a\n| t |\n  # H\n", [(0, "item"), (2, "heading")], [], [], id="lazy-pipe-line-in-an-item"),
        pytest.param(
            "1. x\n  | t |\n   # H\n    - c\n",
            [(0, "item"), (2, "heading"), (3, "item")],
            [],
            [],
            id="lazy-pipe-line-then-a-nested-item",
        ),
        pytest.param(
            "| t |\n- \n\n\t- t\n",
            [(0, "table row"), (1, "paragraph"), (3, "code")],
            [3],
            [],
            id="bare-marker-after-a-row-then-code",
        ),
        pytest.param(
            "  | t |\n  2. v\n\n        code\n",
            [(0, "table row"), (1, "paragraph"), (3, "code")],
            [3],
            [],
            id="numbered-line-after-a-row-then-code",
        ),
        # A marker whose text opens indented code opens a code block of its own, apart from the code above it.
        pytest.param("    x\n\n-     y\n", [(0, "code"), (2, "code")], [0, 2], [], id="item-opening-code-after-code"),
        pytest.param(
            "- a\n\n      x\n\n-     y\n",
            [(0, "item"), (2, "code"), (4, "code")],
            [2, 4],
            [],
            id="item-opening-code-after-an-item-s-code",
        ),
        # A fence in a list item ends at the first line the item does not hold, so the run that would have closed it
        # opens a fence of its own.
        pytest.param(
            "- a\n  ```\nx\n  ```\n# H\n",
            [(0, "item"), (2, "code"), (2, "paragraph"), (4, "code")],
            [1, 3, 4, 5],
            [],
            id="fence-ending-with-its-item",
        ),
        pytest.param(
            "  - ```\n> - qi\n  ```\n    # H\n",
            [(1, "code"), (1, "quote"), (3, "code")],
            [0, 2, 3, 4],
            [],
            id="fence-ending-at-a-quote",
        ),
        # A list item with nothing after its marker, then a blank line, is empty and closes.
        pytest.param("1. \n\n   # H\n", [(0, "item"), (2, "heading")], [], ["H"], id="empty-item-closes"),
        pytest.param("- \n\n    code\n", [(0, "item"), (2, "code")], [2], [], id="empty-item-then-code"),
        # A marker with only spaces or a tab after it opens an empty item whose content is one column past the marker,
        # so the line under it indented that far is the item's.
        pytest.param(
            "1. Do X.\n2.  \n   Do Y.\n3. Do Z.\n",
            [(0, "item"), (1, "item"), (3, "item")],
            [],
            [],
            id="empty-step-with-spaces-then-the-next-step",
        ),
        pytest.param(
            "- a\n-  \n  # H\n", [(0, "item"), (1, "item"), (2, "heading")], [], [], id="empty-item-with-spaces"
        ),
        pytest.param(
            "- a\n- \t\n  # H\n", [(0, "item"), (1, "item"), (2, "heading")], [], [], id="empty-item-with-a-tab"
        ),
        # A list item's content is counted from where the blockquote holding it starts its content on each line.
        pytest.param("> - qi\n>\n   >     code\n", [(0, "quote")], [], [], id="quoted-item-under-a-reindented-quote"),
        pytest.param(
            "> - qi\n  > ```\n> ```\n", [(0, "quote"), (2, "code")], [1, 2], [], id="quoted-fence-leaving-an-item"
        ),
        # A `|` row with a delimiter row under it is a table: a list item after it opens at any number, a line that
        # starts no block is another row, and indented code opens.
        pytest.param(
            "| a |\n| - |\n| b |\n2. Do Y.\n3. Do Z.\n",
            [(0, "table row"), (1, "table row"), (2, "table row"), (3, "item"), (4, "item")],
            [],
            [],
            id="numbered-steps-after-a-table",
        ),
        pytest.param(
            "| a |\n| - |\ntext\n2. b\n",
            [(0, "table row"), (1, "table row"), (2, "table row"), (3, "item")],
            [],
            [],
            id="text-row-after-a-table",
        ),
        pytest.param(
            "| a |\n| - |\n    x\n", [(0, "table row"), (1, "table row"), (2, "code")], [2], [], id="code-after-a-table"
        ),
        # A table needs a delimiter row with as many cells as the row above it, counting a pipe in a code span and not
        # an escaped one; otherwise both lines are a paragraph's text, which an indented line continues.
        pytest.param(
            "| a | b |\n| - |\n    code\n",
            [(0, "table row"), (1, "table row"), (2, "paragraph")],
            [],
            [],
            id="fewer-delimiter-cells-than-header-cells",
        ),
        pytest.param(
            "| a |\n| - | - |\n    code\n",
            [(0, "table row"), (1, "table row"), (2, "paragraph")],
            [],
            [],
            id="more-delimiter-cells-than-header-cells",
        ),
        pytest.param(
            "|\n| - |\n    code\n",
            [(0, "table row"), (1, "table row"), (2, "paragraph")],
            [],
            [],
            id="header-of-one-pipe",
        ),
        pytest.param(
            "| a | b |\n|\t-\t|\n    code\n",
            [(0, "table row"), (1, "table row"), (2, "paragraph")],
            [],
            [],
            id="mismatched-cells-with-tabs",
        ),
        pytest.param(
            "- x\n  | a | b |\n  | - |\n      code\n",
            [(0, "item"), (1, "table row"), (2, "table row"), (3, "paragraph")],
            [],
            [],
            id="mismatched-cells-in-an-item",
        ),
        pytest.param("> | a | b |\n> | - |\n>     code\n", [(0, "quote")], [], [], id="mismatched-cells-in-a-quote"),
        pytest.param(
            "| a \\| b |\n| - |\n    code\n",
            [(0, "table row"), (1, "table row"), (2, "code")],
            [2],
            [],
            id="escaped-pipe-is-one-cell",
        ),
        pytest.param(
            "| `a|b` |\n| - | - |\n    code\n",
            [(0, "table row"), (1, "table row"), (2, "code")],
            [2],
            [],
            id="pipe-in-a-code-span-divides-cells",
        ),
        pytest.param(
            "| a |\n|\t-\t|\n    code\n",
            [(0, "table row"), (1, "table row"), (2, "code")],
            [2],
            [],
            id="matching-cells-with-tabs",
        ),
        # A line that lazily continues a lone row's paragraph, where no block is open, opens a paragraph block.
        pytest.param(
            "- a\n  | t |\nlazy\n",
            [(0, "item"), (1, "table row"), (2, "paragraph")],
            [],
            [],
            id="lazy-line-after-a-row-in-an-item",
        ),
        # A no-break space is text, so a heading-shaped line behind one lazily continues the item.
        pytest.param("- a\n\u00a0# b\n", [(0, "item")], [], [], id="no-break-space-before-a-heading-shape"),
        # A marker three columns in that leaves an item opens the next list.
        pytest.param(
            "  - a\n   2. b\n", [(0, "item"), (1, "item")], [], [], id="marker-three-columns-in-leaving-an-item"
        ),
        # A backtick run whose info string holds a backtick is inline code; a tilde run's may hold one.
        pytest.param(
            "``` a`b\nx\n```\n", [(0, "paragraph"), (3, "code")], [2, 3], [], id="backtick-in-a-backtick-run-s-info"
        ),
        pytest.param("~~~ a`b\nx\n~~~\n", [(1, "code")], [0, 1, 2], [], id="backtick-in-a-tilde-run-s-info"),
        # A run three columns in opens and closes a fence; a closing run followed by a no-break space closes none.
        pytest.param("   ```\nx\n   ```\n", [(1, "code")], [0, 1, 2], [], id="runs-three-columns-in"),
        pytest.param(
            "```\nx\n   ```\ny\n", [(1, "code"), (3, "paragraph")], [0, 1, 2], [], id="closing-run-three-columns-in"
        ),
        pytest.param(
            "```\nx\n```\u00a0\ny\n", [(1, "code")], [0, 1, 2, 3, 4], [], id="closing-run-and-a-no-break-space"
        ),
        # A row's no-break space is a cell's text, and `||` holds one empty cell.
        pytest.param(
            "| a |\u00a0\n| - |\n    code\n",
            [(0, "table row"), (1, "table row"), (2, "paragraph")],
            [],
            [],
            id="no-break-space-after-a-row",
        ),
        pytest.param(
            "||\n|-|\n    code\n",
            [(0, "table row"), (1, "table row"), (2, "code")],
            [2],
            [],
            id="header-of-one-empty-cell",
        ),
        # A table's header is the row right above its delimiter row, whatever rows stand above it.
        pytest.param(
            "  | t |\n|a|\n|-|\n   text\n",
            [(0, "table row"), (1, "table row"), (2, "table row"), (3, "table row")],
            [],
            [],
            id="header-under-another-row",
        ),
        pytest.param(
            "> - ```\n|a|\n|-|\n\ttext\n",
            [(1, "code"), (1, "table row"), (2, "table row"), (3, "code")],
            [0, 3],
            [],
            id="table-after-a-quoted-fence",
        ),
        pytest.param(
            "---\n|a|\n|-|\n   2. w\n",
            [(1, "table row"), (2, "table row"), (3, "item")],
            [],
            [],
            id="table-after-a-thematic-break",
        ),
        pytest.param(
            "\n|a|\n|-|\n    ```\n",
            [(1, "table row"), (2, "table row"), (3, "code")],
            [3],
            [],
            id="table-after-a-blank-line",
        ),
        pytest.param(
            "  | t |\n  | t |\n    - c\n",
            [(0, "table row"), (1, "table row"), (2, "paragraph")],
            [],
            [],
            id="rows-without-a-delimiter",
        ),
        pytest.param(
            "| a |\n```\n```\n| - |\n    code\n",
            [(0, "table row"), (2, "code"), (3, "table row"), (4, "paragraph")],
            [1, 2],
            [],
            id="delimiter-shaped-row-after-a-fence",
        ),
        pytest.param(
            "    ```\n  | t |\n      # H\n",
            [(0, "code"), (1, "table row"), (2, "paragraph")],
            [0],
            [],
            id="row-after-indented-code",
        ),
        # An item's paragraph after a quote, a blank line, or an empty item's next item reads as GitHub renders it.
        pytest.param(
            "* s\n  > q\n\n\ttext\n",
            [(0, "item"), (1, "quote"), (3, "paragraph")],
            [],
            [],
            id="paragraph-after-a-quote-in-an-item",
        ),
        pytest.param(
            "  - b\n\n\n\ttext\n", [(0, "item"), (3, "paragraph")], [], [], id="paragraph-after-blank-lines-in-an-item"
        ),
        pytest.param("> 2. qn\n>\n   >     code\n", [(0, "quote")], [], [], id="paragraph-in-a-quoted-numbered-item"),
        pytest.param(
            "1. a\n   ```\n   ```\n\n   ```\nb\n",
            [(0, "item"), (2, "code"), (5, "code"), (5, "paragraph")],
            [1, 2, 4],
            [],
            id="fence-after-a-blank-line-in-an-item",
        ),
        pytest.param(
            "   ~~~\n   ~~~\n- # H\n     text\n",
            [(1, "code"), (2, "item"), (3, "paragraph")],
            [0, 1],
            [],
            id="paragraph-after-an-item-s-heading",
        ),
        pytest.param("* s\n  > q\n>\n", [(0, "item"), (1, "quote"), (2, "quote")], [], [], id="quote-leaving-an-item"),
        pytest.param("- \n  2. v\n", [(0, "item"), (1, "item")], [], [], id="item-in-an-empty-item"),
        # A delimiter-shaped row makes a table only right under a row its containers hold: not after a line that opens a
        # container, a blank line, an empty item, or a heading.
        pytest.param(
            "|a|\n> |-|\n>     code\n", [(0, "table row"), (1, "quote")], [], [], id="delimiter-in-a-new-quote"
        ),
        pytest.param(
            "|a|\n\n|-|\n    code\n",
            [(0, "table row"), (2, "table row"), (3, "paragraph")],
            [],
            [],
            id="delimiter-after-a-blank-line",
        ),
        pytest.param(
            "|a|\n\n- \n  |-|\n      code\n",
            [(0, "table row"), (2, "item"), (3, "table row"), (4, "paragraph")],
            [],
            [],
            id="delimiter-under-an-empty-item",
        ),
        pytest.param(
            "|a|\n# H\n|-|\n    code\n",
            [(0, "table row"), (1, "heading"), (2, "table row"), (3, "paragraph")],
            [],
            ["H"],
            id="delimiter-after-a-heading",
        ),
        # GFM tries a paragraph's first delimiter-shaped row as a table and no later one; a blank line starts a new try.
        pytest.param(
            "| a |\n|---|---|\n| b | c |\n|---|---|\n2. y\n",
            [(0, "table row"), (1, "table row"), (2, "table row"), (3, "table row"), (4, "paragraph")],
            [],
            [],
            id="retried-delimiter-under-rows",
        ),
        pytest.param(
            "text\n|---|---|\n| b | c |\n|---|---|\n2. y\n",
            [(0, "paragraph"), (1, "table row"), (2, "table row"), (3, "table row"), (4, "paragraph")],
            [],
            [],
            id="retried-delimiter-under-text",
        ),
        pytest.param(
            "- | a |\n  |---|---|\n  | b | c |\n  |---|---|\n  2. y\n",
            [(0, "item"), (1, "table row"), (2, "table row"), (3, "table row"), (4, "paragraph")],
            [],
            [],
            id="retried-delimiter-in-an-item",
        ),
        pytest.param(
            "> | a |\n> |---|---|\n> | b | c |\n> |---|---|\n>     code\n",
            [(0, "quote")],
            [],
            [],
            id="retried-delimiter-in-a-quote",
        ),
        pytest.param(
            "|\n|---|---|\n|---|---|\n\ttext\n",
            [(0, "table row"), (1, "table row"), (2, "table row"), (3, "paragraph")],
            [],
            [],
            id="retried-delimiter-under-a-lone-pipe",
        ),
        pytest.param(
            "   | - |\n| - | - |\n| - | - |\n\ttext\n",
            [(0, "table row"), (1, "table row"), (2, "table row"), (3, "paragraph")],
            [],
            [],
            id="retried-delimiter-after-a-mismatch",
        ),
        pytest.param(
            "  |-|-|\n   | - |\n|-|\n    code\n",
            [(0, "table row"), (1, "table row"), (2, "table row"), (3, "paragraph")],
            [],
            [],
            id="retried-delimiter-after-delimiter-shaped-rows",
        ),
        pytest.param(
            "| a |\n|-|-|\ntext\n| b | c |\n|---|---|\n2. y\n",
            [
                (0, "table row"),
                (1, "table row"),
                (2, "paragraph"),
                (3, "table row"),
                (4, "table row"),
                (5, "paragraph"),
            ],
            [],
            [],
            id="retried-delimiter-after-text",
        ),
        # A header read across a blank line is no header: the paragraph's text starts the rows anew.
        pytest.param(
            "| x | y |\n\na\n| - | - |\n    code\n",
            [(0, "table row"), (2, "paragraph"), (3, "table row"), (4, "paragraph")],
            [],
            [],
            id="no-header-across-a-blank-line",
        ),
        pytest.param(
            "| a |\n\n| b |\n|-|\n    code\n",
            [(0, "table row"), (2, "table row"), (3, "table row"), (4, "code")],
            [4],
            [],
            id="delimiter-tried-again-after-a-blank-line",
        ),
        # A backtick run indented as far as indented code past its container neither opens nor closes a fence.
        pytest.param("+ p\n      ```\n```\n", [(0, "item"), (3, "code")], [2, 3], [], id="deep-run-in-an-item"),
        pytest.param(
            "    ```\n    x\n    ```\n# H\n", [(0, "code"), (3, "heading")], [0, 1, 2], ["H"], id="indented-runs"
        ),
        pytest.param(
            "- a\n  ```\n      ```\n  # H\n", [(0, "item"), (2, "code")], [1, 2, 3, 4], [], id="deep-run-in-a-fence"
        ),
    ],
)
def test_every_reader_holds_a_line_in_the_list_item_github_renders_it_in(
    document: str, expected: list[tuple[int, str]], code: list[int], headings: list[str]
) -> None:
    # `blocks`, `code_block_lines`, and `document_headings` read one walk's container stack, so none reads a line in
    # an item another leaves it out of.
    lines = document.split("\n")
    assert [(block.start, block.kind) for block in blocks(lines)] == expected
    assert sorted(code_block_lines(lines)) == code
    assert [heading.group("text") for _, heading in document_headings(lines)] == headings


@pytest.mark.parametrize(
    ("document", "expected", "code"),
    [
        # A nested blockquote and a lazy line join the quote's block, as every line a blockquote holds does.
        pytest.param("> a\n>> b\n", [(0, "quote", 2)], [], id="nested-quote-opens"),
        pytest.param(">> a\n> b\n", [(0, "quote", 2)], [], id="nested-quote-continues-lazily"),
        pytest.param("> | r |\nlazy\n", [(0, "quote", 2)], [], id="lazy-line-after-a-quoted-row"),
        pytest.param(
            "> a\n>\n> b\n> # h\n> | r |\n> - i\n>     code\n", [(0, "quote", 7)], [], id="every-quoted-block-joins"
        ),
        # Indented code inside a blockquote is a code block of its own.
        pytest.param("> a\n>\n>     code\n", [(0, "quote", 2), (2, "code", 1)], [2], id="quoted-code"),
    ],
)
def test_a_blockquote_s_lines_join_its_block(
    document: str, expected: list[tuple[int, str, int]], code: list[int]
) -> None:
    lines = document.split("\n")
    assert [(block.start, block.kind, len(block.lines)) for block in blocks(lines)] == expected
    assert sorted(code_block_lines(lines)) == code
    assert document_headings(lines) == []


def _walked(lines: list[str]) -> object:
    return list(blocks(lines)), code_block_lines(lines), document_headings(lines)


# Documents eight times as long as others: a linear walk takes about eight times as long and a quadratic one about 64
# times. The bound sits halfway between them in log space.
WALK_FACTOR = 8
WALK_BOUND = WALK_FACTOR**1.5


def assert_walked_in_proportion(small: str, large: str) -> None:
    assert_time_in_proportion(_walked, small.split("\n"), large.split("\n"), WALK_BOUND)


@pytest.mark.parametrize(
    "repeated",
    [
        pytest.param("1. Run:\n   ```bash\ncmd\n   ```\n", id="fence-ending-with-its-item"),
        pytest.param("> - a\n>   ```\n> b\n", id="fence-ending-with-a-quoted-item"),
        pytest.param("  - " * 30 + "x\n", id="deep-nesting"),
    ],
)
def test_the_walk_takes_time_in_proportion_to_the_document(repeated: str) -> None:
    # A walk that read the rest of the document again at each line would take time growing with its square.
    assert_walked_in_proportion(repeated * 500, repeated * 500 * WALK_FACTOR)


@pytest.mark.parametrize("marker", ["- ", "* ", "> - ", "1. "])
def test_a_line_of_nested_markers_takes_time_in_proportion_to_its_length(marker: str) -> None:
    # Each marker is read where it stands: a walk that read the rest of the line again at each marker, such as one that
    # tried a thematic break there, would take time growing with the square of the markers.
    assert_walked_in_proportion(marker * 1000 + "x", marker * 1000 * WALK_FACTOR + "x")


@pytest.mark.parametrize("marker", ["* ", "> - "])
def test_a_line_of_nested_markers_is_never_copied_at_each_marker(marker: str) -> None:
    # A walk that kept a copy of the rest of the line at each list item it opens would take time growing with the
    # markers times the line, which a long text after them makes plain beside the cost of reading them.
    tail = 128

    def document(markers: int) -> str:
        return marker * markers + "x" * (tail * markers)

    assert_walked_in_proportion(document(1000), document(1000 * WALK_FACTOR))


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(lambda depth: "- " * depth + "x\n" + "\n" * depth, id="blank-lines"),
        pytest.param(lambda depth: "- " * depth + "x\n" + "   \n" * depth, id="lines-of-spaces"),
        pytest.param(lambda depth: "> - " * depth + "x\n" + ">\n" * depth, id="quoted-blank-lines"),
        pytest.param(lambda depth: "- " * depth + "```\n" + "\n" * depth, id="blank-lines-in-a-fence"),
    ],
)
def test_blank_lines_under_a_deep_item_take_time_in_proportion_to_them(shape: Callable[[int], str]) -> None:
    # Every list item holds a blank line, so it is read past them at once: one that visited each item the line passes
    # would take time growing with the square of the depth and the lines.
    assert_walked_in_proportion(shape(1000), shape(1000 * WALK_FACTOR))


@pytest.mark.parametrize(
    ("document", "body", "content"),
    [
        pytest.param("- a\n\n  ```bash\n  x\n  ```\n", ("  x",), ["x"], id="in-an-indented-list-item"),
        pytest.param("> ```py\n> x\n> ```\n", ("x",), ["x"], id="in-a-quote"),
        pytest.param("> 1. a\n>    ~~~\n>      y\n>    ~~~\n", ("     y",), ["  y"], id="in-a-quoted-list-item"),
    ],
)
def test_a_fence_s_code_block_holds_its_body_as_written_and_its_content_without_its_containers(
    document: str, body: tuple[str, ...], content: list[str]
) -> None:
    # The walk records a fence's language, blockquotes, and indentation, so its code block and its content agree.
    lines = document.split("\n")
    (fence,) = find_fences(lines)
    (code,) = [block for block in blocks(lines) if block.kind == "code"]
    assert code.lines == body
    assert [fence.content(lines[index]) for index in fence.body()] == content


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Each case was checked against `gh api markdown`: GitHub ends or keeps each fence where it is read here.
        pytest.param(
            "1. Run:\n   ```bash\ncmd\n   ```",
            [(1, None, "bash", 2, []), (3, None, "", 4, [])],
            id="body-leaving-its-item",
        ),
        pytest.param("- ```\n  x\n```", [(0, None, "", 2, ["x"]), (2, None, "", 3, [])], id="closer-leaving-its-item"),
        pytest.param(
            "1. Step:\n\n   ```bash\n   EOF\n     x\n  y\n   ```",
            [(2, None, "bash", 5, ["EOF", "  x"]), (6, None, "", 7, [])],
            id="body-line-indented-less-than-its-item",
        ),
        pytest.param("    ```bash\n    echo\n    ```", [], id="opener-indented-four"),
        pytest.param("```\nx\n    ```\ny\n```", [(0, 4, "", 4, ["x", "    ```", "y"])], id="closer-indented-four"),
        pytest.param(
            "- a\n  ```\n      ```\n  # H", [(1, None, "", 4, ["    ```", "# H"])], id="closer-indented-four-in-an-item"
        ),
        pytest.param("+ p\n      ```\n```", [(2, None, "", 3, [])], id="opener-indented-four-in-an-item"),
        pytest.param(
            "> - ```\n>   x\n> y\n> ```",
            [(0, None, "", 2, ["x"]), (3, None, "", 4, [])],
            id="quoted-item-fence-leaving-the-item",
        ),
        pytest.param("- ```\n  x\n     ```\n  y", [(0, 2, "", 2, ["x"])], id="closer-three-columns-into-an-item"),
    ],
)
def test_a_fence_ends_with_the_containers_that_hold_it_and_opens_or_closes_only_three_columns_into_them(
    text: str, expected: list[tuple[int, int | None, str, int, list[str]]]
) -> None:
    lines = text.split("\n")
    found = [
        (fence.opener, fence.closer, fence.language, fence.stop, [fence.content(lines[i]) for i in fence.body()])
        for fence in find_fences(lines)
    ]
    assert found == expected


# The lines a random document is drawn from: fence runs of each character, length, and indentation, with and without
# an info string; the list item and blockquote markers that may hold them, nested; text, headings, and blank lines.
FENCE_VOCABULARY = [
    "```",
    "````",
    "~~~",
    "```bash",
    "~~~ sh x",
    "```a`b",
    "  ```",
    "   ```",
    "    ```",
    "\t```",
    "- ```",
    "1. ```bash",
    "  - ```",
    "> ```",
    ">```",
    "> - ```",
    "> > ```",
    "- > ```",
    "  > ```",
    "- a",
    "2. b",
    "  text",
    "   text",
    "    code",
    "text",
    "# H",
    "  # H",
    "> text",
    "> ",
    ">",
    "---",
    "| a |",
    "",
    "",
]
FUZZ_SEED = 2979
FUZZ_DOCUMENTS = 3000


def test_every_reader_reads_the_fences_find_fences_reads_on_any_document() -> None:
    # One detector decides every fence: on documents drawn at random, each fence find_fences gives is a code block of
    # `blocks` holding its body, every line of it is code for code_block_lines, and none of its lines is a document
    # heading; fences come in order, none overlapping, and each closer is a run of backticks or tildes alone.
    draw = random.Random(FUZZ_SEED)  # noqa: S311 - a seeded draw of test documents, never a secret
    for _ in range(FUZZ_DOCUMENTS):
        lines = [draw.choice(FENCE_VOCABULARY) for _ in range(draw.randint(1, 12))]
        fences = find_fences(lines)
        code = code_block_lines(lines)
        code_blocks = [(block.start, block.lines) for block in blocks(lines) if block.kind == "code"]
        headings = {index for index, _ in document_headings(lines)}
        previous = 0
        for fence in fences:
            assert previous <= fence.opener < fence.lines().stop <= len(lines), lines
            assert set(fence.lines()) <= code, lines
            assert not set(fence.lines()) & headings, lines
            assert (fence.opener + 1, tuple(fence.text(lines[i]) for i in fence.body())) in code_blocks, lines
            if fence.closer is not None:
                closing = set(fence.text(lines[fence.closer]).strip(" \t"))
                assert fence.closer == fence.stop, lines
                assert closing in ({"`"}, {"~"}), lines
            previous = fence.lines().stop


def test_outside_fences_blanks_every_line_find_fences_places_and_keeps_the_rest_at_its_number() -> None:
    lines = ["# Title", "```markdown", "## Shown", "```", "> ~~~", "> ### Quoted", "> ~~~", "## Kept", "```", "open"]
    assert outside_fences(lines) == ["# Title", "", "", "", "", "", "", "## Kept", "", ""]


def test_a_list_item_with_nothing_after_its_marker_takes_the_line_it_holds_as_its_text() -> None:
    (item,) = blocks(["- ", "  text"])
    assert (item.kind, item.lines) == ("item", ("- ", "  text"))


def test_a_block_s_lines_are_kept_as_written_and_its_text_may_leave_a_list_item_s_marker_out() -> None:
    document = "-   an item\n  wraps\n\n> ```\n> quoted\n> ```\n\n    one\n\n    two\n"
    item, fenced, code = blocks(document.split("\n"))
    assert item.lines == ("-   an item", "  wraps")
    assert (item.text(), item.text(marker=False)) == ("-   an item wraps", "an item wraps")
    assert (fenced.start, fenced.lines) == (4, ("quoted",))
    assert code.lines == ("    one", "", "    two")


def test_top_level_sections_run_from_each_heading_of_a_level_to_the_next_of_that_level() -> None:
    # A heading a list item, a quote, or a fence holds is its container's text, and one of another level, deeper or
    # higher, is the section's, as markdown_section ends a section only at the next `## `.
    document = [
        "# Title",  # 0
        "## One",  # 1
        "- Notes",  # 2
        "  ## Held by the item",  # 3
        "> ## Quoted",  # 4
        "```",  # 5
        "## Fenced",  # 6
        "```",  # 7
        "### Deeper",  # 8
        "## Two ##",  # 9
        "text",  # 10
        "# Above",  # 11
        "## Three",  # 12
        "last",  # 13
    ]
    sections = [(index, heading.group("text"), end) for index, heading, end in top_level_sections(document, 2)]
    assert sections == [(1, "One", 9), (9, "Two", 12), (12, "Three", 14)]
    assert [(index, end) for index, _, end in top_level_sections(document, 1)] == [(0, 11), (11, 14)]


def test_session_commands_reads_a_console_fence_s_prompted_commands_without_prompts_or_output() -> None:
    lines = ["```console", "$ echo hi", "it's done", "(venv) user@host:~$ git status \\", "> --short", "```"]
    (fence,) = find_fences(lines)
    assert session_commands(fence, lines, awaits_more) == [(1, "echo hi"), (3, "git status \\"), (4, "--short")]
    # A console fence with no prompt is no session, and neither is a fence of another label.
    unprompted = ["```console", "echo hi", "```"]
    assert session_commands(find_fences(unprompted)[0], unprompted, awaits_more) is None
    bash = ["```bash", "$ echo hi", "```"]
    assert session_commands(find_fences(bash)[0], bash, awaits_more) is None


@pytest.mark.parametrize(
    ("body", "commands"),
    [
        # Bash reads the lines after a command it has not finished as more of it, with no prompt before them: a
        # here-document's body, a quoted string's next line, and a command after a pipe that ends a line.
        (["$ cat <<EOF > notes.txt", "hello", "EOF", "done"], [(1, "cat <<EOF > notes.txt\nhello\nEOF")]),
        (["$ cat <<-EOF", "\thello", "\tEOF"], [(1, "cat <<-EOF\n\thello\n\tEOF")]),
        (['$ git commit -m "one', 'two"', "[main 1a2b3c4] one"], [(1, 'git commit -m "one\ntwo"')]),
        (["$ ls |", "  grep x", "x.txt"], [(1, "ls |\n  grep x")]),
        # Lines that never finish the command are output, and the command is read as written.
        (["$ if then", "bash: syntax error", "$ echo next"], [(1, "if then"), (3, "echo next")]),
    ],
)
def test_session_commands_read_a_command_bash_has_not_finished_on_through_the_lines_that_finish_it(
    body: list[str], commands: list[tuple[int, str]]
) -> None:
    lines = ["```console", *body, "```"]
    assert session_commands(find_fences(lines)[0], lines, awaits_more) == commands

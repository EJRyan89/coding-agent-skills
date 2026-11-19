"""Regression tests for the shared Markdown readers."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from markdown_sections import (
    ATX_HEADING,
    CODE_SPAN,
    COMMONMARK_CODE_SPAN,
    EXECUTABLE_FENCES,
    FENCE_LANGUAGES,
    FENCE_OPENER,
    HTML_COMMENT,
    POWERSHELL_FENCES,
    SHELL_FENCES,
    code_block_lines,
    document_headings,
    find_fences,
    is_blank,
    items_holding,
    list_item,
    list_item_content,
    markdown_section,
    prose_code_spans,
    starts_block,
)

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
        # Text, a marker four columns in, an item that may not interrupt, and a no-break space after the marks continue
        # the paragraph.
        ("text", False, False),
        ("    > quote", False, False),
        ("    ## heading", False, False),
        ("2. item", False, False),
        (f"##{NBSP}heading", False, False),
    ],
)
def test_a_block_start_ends_a_paragraph_and_any_other_line_continues_it(line: str, opener: bool, starts: bool) -> None:
    assert starts_block(line, opener=opener) is starts


def test_a_list_item_s_content_starts_past_its_marker_and_the_spaces_after_it() -> None:
    assert [list_item_content(line) for line in ("- item", "  -   item", "1.\titem", "-      code", "text")] == [
        2,
        6,
        4,
        2,
        None,
    ]
    assert [items_holding([2, 4], width) for width in (0, 2, 3, 4, 8)] == [0, 1, 1, 2, 2]


def test_a_fence_inside_a_list_item_reads_the_marker_as_commonmark_does() -> None:
    assert FENCE_OPENER.fullmatch("3. ```bash") is not None
    assert FENCE_OPENER.fullmatch("123456789) ~~~") is not None
    assert FENCE_OPENER.fullmatch(f"{ARABIC_THREE}. ```bash") is None
    assert FENCE_OPENER.fullmatch("1234567890. ```bash") is None


def test_a_nested_item_is_read_at_any_indentation_where_it_interrupts_nothing() -> None:
    assert list_item("        - nested") == "nested"
    assert list_item("\t\t2. nested") == "nested"


def test_a_fence_closes_only_on_a_run_indented_with_spaces_and_tabs() -> None:
    assert find_fences(["```", "body", f"{EM_SPACE}```", f"{NBSP}```", " \t```", "after"])[0].lines() == range(5)
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
    def test_a_code_span_is_one_backtick_pair_on_one_line_with_its_text_as_the_group(self) -> None:
        self.assertEqual(["git status", " a.py "], CODE_SPAN.findall("Run `git status`, then ` a.py `."))

    def test_a_span_does_not_cross_a_line_or_match_an_unclosed_backtick(self) -> None:
        self.assertEqual([], CODE_SPAN.findall("`open\nclose and `unclosed"))

    def test_a_commonmark_span_closes_on_a_run_of_its_opener_s_length_and_may_hold_a_backtick(self) -> None:
        self.assertEqual(
            [("`", "a"), ("``", "b ` c"), ("```", "d")],
            COMMONMARK_CODE_SPAN.findall("`a` and ``b ` c`` and ```d```"),
        )
        self.assertEqual("see  and [x](y)", COMMONMARK_CODE_SPAN.sub("", "see ``[a](b)`` and [x](y)"))
        self.assertEqual([], COMMONMARK_CODE_SPAN.findall("``open\nclose``"))


class HtmlCommentTests(unittest.TestCase):
    def test_a_comment_is_removed_across_lines_and_each_one_ends_at_its_own_close(self) -> None:
        self.assertEqual("a  b  c", HTML_COMMENT.sub("", "a <!-- one\ntwo --> b <!-- three --> c"))
        self.assertEqual("a <!-- open", HTML_COMMENT.sub("", "a <!-- open"))

    def test_prose_spans_skip_every_fence_form_and_keep_the_line_index(self) -> None:
        text = "Run `a`.\n```bash\n`b`\n```\n> ~~~sh\n> `c`\n> ~~~\n    ```\n    `d`\n    ```\nthen `e`"
        self.assertEqual([(0, "a"), (10, "e")], prose_code_spans(text.split("\n")))


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
        pytest.param("    ```bash\n    echo\n    ```", [(0, 2, "bash", 2, ["    echo"])], id="indented four spaces"),
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
        # ends the item under CommonMark, which find_fences does not track, so it is read as the fence's.
        (["1. Step:", "", "   ```bash", "   EOF", "     x", "  y", "   ```"], ["EOF", "  x", "y"]),
        # A fence indented on its own takes up to its indentation off each line.
        (["  ```", "    a", " b", "c", "  ```"], ["  a", "b", "c"]),
        # A blockquote's markers come off first, then the indentation inside it.
        (["> - ```sh", ">   EOF", "> ```"], ["EOF"]),
        # A tab counts to the next multiple of four, and one that runs past the indentation gives its columns past
        # it as spaces, as CommonMark gives a partly consumed tab.
        (["\t```", "\tEOF", "  \tx", "\t```"], ["EOF", "x"]),
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

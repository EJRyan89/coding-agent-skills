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
    blocks,
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


def _walk_seconds(document: str) -> float:
    lines = document.split("\n")
    times = []
    for _ in range(3):
        started = time.perf_counter()
        list(blocks(lines))
        code_block_lines(lines)
        document_headings(lines)
        times.append(time.perf_counter() - started)
    return min(times)


@pytest.mark.parametrize(
    "repeated",
    [
        pytest.param("1. Run:\n   ```bash\ncmd\n   ```\n", id="fence-ending-with-its-item"),
        pytest.param("> - a\n>   ```\n> b\n", id="fence-ending-with-a-quoted-item"),
        pytest.param("  - " * 30 + "x\n", id="deep-nesting"),
    ],
)
def test_the_walk_takes_time_in_proportion_to_the_document(repeated: str) -> None:
    # Four times the lines take about four times as long; a walk that read the rest of the document again at each line
    # would take about sixteen.
    assert _walk_seconds(repeated * 2000) < 8 * max(_walk_seconds(repeated * 500), 0.001)


@pytest.mark.parametrize("marker", ["- ", "* ", "> - ", "1. "])
def test_a_line_of_nested_markers_takes_time_in_proportion_to_its_length(marker: str) -> None:
    # Each marker is read where it stands: a walk that read the rest of the line again at each marker would take
    # about sixteen times as long for four times the markers.
    assert _walk_seconds(marker * 4000 + "x") < 8 * max(_walk_seconds(marker * 1000 + "x"), 0.001)


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

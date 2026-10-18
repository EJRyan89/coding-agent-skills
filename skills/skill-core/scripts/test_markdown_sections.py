"""Regression tests for the shared Markdown readers."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from markdown_sections import (
    CODE_SPAN,
    COMMONMARK_CODE_SPAN,
    EXECUTABLE_FENCES,
    FENCE_LANGUAGES,
    HTML_COMMENT,
    POWERSHELL_FENCES,
    SHELL_FENCES,
    find_fences,
    markdown_section,
    prose_code_spans,
)

# Runs no test of its own; the test functions below check through its assertions, as the classes do.
CHECK = unittest.TestCase()


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
    CHECK.assertEqual(expected, fences(text))


@pytest.mark.parametrize("marker", [">", "*"])
def test_a_long_run_of_container_markers_is_read_in_linear_time(marker: str) -> None:
    # A run of whitespace could once be split between two markers in exponentially many ways before a mismatch.
    started = time.perf_counter()
    CHECK.assertEqual([], find_fences([marker + f"\t\t{marker}" * 5000 + "x"]))
    CHECK.assertLess(time.perf_counter() - started, 1.0)


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
    CHECK.assertEqual(expected, fences(text))

"""Regression tests for the one Markdown escaper the skills share."""

from __future__ import annotations

import string
import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from markdown_literal import markdown_line, markdown_literal, navigation_url


def code_points(text: str) -> str:
    """A test ID naming each character of `text` by its code point, since most are invisible."""
    return "+".join(f"U+{ord(character):04X}" for character in text)


class MarkdownLiteralTests(unittest.TestCase):
    def test_every_ascii_punctuation_character_becomes_an_entity(self) -> None:
        encoded = markdown_literal(string.punctuation)
        self.assertEqual(
            "&#33;&#34;&#35;&#36;&#37;&amp;&#39;&#40;&#41;&#42;&#43;&#44;&#45;&#46;&#47;&#58;&#59;&lt;&#61;&gt;&#63;&#64;"
            "&#91;&#92;&#93;&#94;&#95;&#96;&#123;&#124;&#125;&#126;",
            encoded,
        )

    def test_letters_digits_and_other_characters_are_kept(self) -> None:
        self.assertEqual("Café 42 ü", markdown_literal("Café 42 ü"))

    def test_leading_whitespace_is_encoded_and_inner_whitespace_kept(self) -> None:
        self.assertEqual("&#32;&#9;a  b", markdown_literal(" \ta  b"))

    def test_line_breaks_are_kept_by_the_literal_and_flattened_by_the_line(self) -> None:
        self.assertEqual("a\nb", markdown_literal("a\nb"))

    def test_a_line_cannot_hold_a_comment_a_cell_or_an_escape(self) -> None:
        self.assertEqual(
            "&lt;&#33;&#45;&#45; x &#45;&#45;&gt; &#92;&#124; &#124;",
            markdown_line("<!-- x --> \\| |"),
        )


class NavigationUrlTests(unittest.TestCase):
    def test_an_absolute_https_url_keeps_its_destination_with_delimiters_encoded(self) -> None:
        self.assertEqual(
            "https://github.com/o/r/pull/1?a=%28b%29%7C%3Cc%3E%20d",
            navigation_url("https://github.com/o/r/pull/1?a=(b)|<c>%20d"),
        )


@pytest.mark.parametrize(
    "separator", ["\r", "\n", "\r\n", "\u0085", "\u2028", "\u2029", "\x0b", "\x0c", "\x1c"], ids=code_points
)
def test_line_breaks_are_kept_by_the_literal_and_flattened_by_the_line(separator: str) -> None:
    assert markdown_line(f" a{separator}b ") == "a b"


@pytest.mark.parametrize(
    "url", ["http://github.com/o/r", "javascript:alert(1)", "https://host/a b", "//host/a", "https://h_st/"]
)
def test_any_other_url_is_refused(url: str) -> None:
    assert navigation_url(url) is None

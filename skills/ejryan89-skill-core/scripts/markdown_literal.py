"""The one Markdown escaper for text a script did not write, such as a pull request's title or a reviewer's prose.

Every ASCII punctuation character is encoded as a character entity, which is text in both Markdown and HTML, so the
text cannot open a link, an image, emphasis, code, math, a tag, or an HTML comment, cannot add a table cell or close
one, and cannot be read as a bare URL, an email address, or a mention, whatever the parity of the backslashes it
holds. A one-line value is flattened first, so no line break, U+0085, U+2028, or U+2029 among them, ends its row.
"""

from __future__ import annotations

import re
import string
from urllib.parse import quote

from flat_text import flat_text

LITERAL_PUNCTUATION = str.maketrans(
    {char: {"&": "&amp;", "<": "&lt;", ">": "&gt;"}.get(char, f"&#{ord(char)};") for char in string.punctuation}
)


def markdown_literal(text: str) -> str:
    """`text` with its punctuation encoded, and its leading whitespace too, so it never opens an indented code block."""
    text = text.translate(LITERAL_PUNCTUATION)
    return re.sub(r"^[ \t]+", lambda match: "".join(f"&#{ord(char)};" for char in match[0]), text)


def markdown_line(text: str) -> str:
    """Literal single-line text, for a table cell, a link's text, a heading, or an HTML summary."""
    return markdown_literal(flat_text(text))


def navigation_url(url: str) -> str | None:
    """An absolute HTTPS `url` as a link destination with its delimiters percent-encoded, or None for any other, which
    then stays text."""
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?(?:/[^\x00-\x20]*)?", url):
        return None
    return quote(url, safe="/:?#@!$&+,;=%")

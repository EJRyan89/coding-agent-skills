"""The one reader of Markdown link destinations, as CommonMark reads them.

A destination is the target of an inline link or image, `[text](destination "title")`, or of a link reference
definition, `[label]: destination "title"`, at the start of a line with at most three spaces before it. A line that
opens like a definition but holds none, such as `[Note]: see [a](b.md)`, is read for its inline links, and a footnote,
`[^1]: text`, which GitHub renders as a footnote, is never a definition. CommonMark writes a destination in either of
two forms: between `<` and `>`, where it may hold spaces but no line ending, `<`, or `>` unescaped; or bare, a run with
no space or control character whose parentheses balance unless escaped. In both a backslash before an ASCII
punctuation character escapes it, and a backslash before anything else is itself, so `references\\win.md` keeps its
backslash, which a path reads as the platform does: a separator on Windows, part of the name elsewhere; an entity or
numeric character reference, such as `&amp;` or `&#32;`, stands for its character. A link's text is not read, and
neither is anything inside a code span or a code block, fenced or indented, which CommonMark reads as code rather than
as a link.

The `url` this gives is the destination after its escapes and references, which CommonMark percent-encodes into the
rendered `href`; `path` and `fragment` are what a reader opening the destination as a URL reaches, each percent-decoded,
and `posix_path` is the path as a repository names a file, with `/` between its parts. `written_destination` is the
other direction: the text to write as a link's destination so that this reader reads back a given path.

Limits: a destination is read on the line its `](` or its definition's `]:` is on, so a link whose destination
starts on the next line, or whose `)` or title follows on the next line, is not read; a `](` is read as a link's
without matching the `[` that opens its text, and a definition wherever a line starts with one, even where it would
continue a paragraph; a title after a destination is not checked to end the definition's line or the link, so
`[r]: x.md "t" and [b](y.md)` reads `x.md` and drops `y.md`; and a backslash-escaped backtick still opens a code span,
so ``see \\` and [a](x.md) and `y` `` reads no link.
"""

from __future__ import annotations

import html
import re
import string
from dataclasses import dataclass
from pathlib import PurePath
from urllib.parse import quote, unquote

from markdown_sections import code_block_lines, without_code_spans

# What opens an inline link's destination: the `](` after its text, and the spaces or tabs CommonMark allows before it.
INLINE_OPENER = re.compile(r"\]\([ \t]*")
# A link reference definition's label and colon, at the start of a line, and the spaces or tabs before its destination.
DEFINITION_OPENER = re.compile(r" {0,3}\[(?:[^\\\[\]]|\\.)*[^\s\\\[\]](?:[^\\\[\]]|\\.)*\]:[ \t]*")
# What opens a footnote's label, `[^1]: text`, which GitHub renders as a footnote, never as a link reference definition.
FOOTNOTE_LABEL = "[^"
# An entity or numeric character reference CommonMark replaces with its character; one with no `;` is text.
CHARACTER_REFERENCE = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
# A URL scheme, as a CommonMark autolink reads one: two to 32 characters, so a Windows drive letter such as `C:` is
# none; or `//`, a network path, which names a host rather than a file.
URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{1,31}:|//")
# What may open the title after a destination and the spaces or tabs before it; an inline link may instead close with
# its `)`, and a definition end its line.
TITLE_OPENERS = frozenset("\"'(")
# What written_destination percent-encodes in a path, beside the control characters: `%`, which the path decodes, `#`
# and `?`, which would end it, and a backtick, which could open or close a code span, where a backslash escapes none.
PERCENT_ENCODED = frozenset("%#?`")
# What written_destination escapes with a backslash: the backslash itself, the parentheses that would end or unbalance
# a bare destination, the `<` and `>` that open and close the other form, and the `&` that would open a reference.
BACKSLASH_ESCAPED = frozenset("\\()<>&")


@dataclass(frozen=True)
class Destination:
    """One link destination: its line number, from 1, the columns of its text as written, `<` and `>` included, and
    its `url` after escapes and character references."""

    line: int
    start: int
    end: int
    written: str
    url: str

    @property
    def is_url(self) -> bool:
        """Whether the destination names a scheme or a host, so it is no path in the document's own tree."""
        return URL_SCHEME.match(self.url) is not None

    @property
    def path(self) -> str:
        """The destination as a path, percent-decoded, without its fragment or query: "" for one that is only a
        fragment, which leads to the document that holds it."""
        location = self.url.partition("#")[0].partition("?")[0]
        return unquote(location)

    @property
    def fragment(self) -> str:
        """The fragment after the destination's first `#`, percent-decoded, or "" when it has none."""
        return unquote(self.url.partition("#")[2])

    @property
    def posix_path(self) -> str:
        """The path as a repository names a file, with `/` between its parts and no `.` part: a backslash is read as
        the platform reads one in a path, a separator on Windows and part of the name elsewhere, so the file this
        names is the one a reader on this platform opens. "" for a destination that is only a fragment."""
        return PurePath(self.path).as_posix() if self.path else ""


def written_destination(path: str) -> str:
    """The destination text that `link_destinations` reads back as `path`, as `Destination.path` gives it.

    A control character, `%`, `#`, `?`, and a backtick are percent-encoded, and a backslash, a parenthesis, `<`, `>`,
    and `&` are escaped with a backslash; a path holding a space is written between `<` and `>`, where a space needs no
    encoding. Any other character is written as it is, so a plain name such as `notes.md` is written unchanged.
    `path` is not empty, since an empty destination is none.
    """
    text = "".join(_written_character(character) for character in path)
    return f"<{text}>" if " " in path else text


def _written_character(character: str) -> str:
    if character in PERCENT_ENCODED or ord(character) < 0x20 or ord(character) == 0x7F:
        return quote(character, safe="")
    return f"\\{character}" if character in BACKSLASH_ESCAPED else character


def link_destinations(lines: list[str]) -> list[Destination]:
    """Every link destination the lines hold outside code, in the order they are written. An empty destination, as in
    `[text]()`, is none."""
    code = code_block_lines(lines)
    found: list[Destination] = []
    for index, line in enumerate(lines):
        if index in code:
            continue
        text = without_code_spans(line, " ")
        definition = DEFINITION_OPENER.match(text)
        if definition and not text.lstrip(" ").startswith(FOOTNOTE_LABEL):
            parsed = _destination(text, definition.end(), inline=False)
            if parsed is not None:
                found.append(_found(index, line, definition.end(), *parsed))
                continue
        for opener in INLINE_OPENER.finditer(text):
            parsed = _destination(text, opener.end(), inline=True)
            if parsed is not None:
                found.append(_found(index, line, opener.end(), *parsed))
    return found


def _found(index: int, line: str, start: int, end: int, url: str) -> Destination:
    return Destination(index + 1, start, end, line[start:end], url)


def _destination(text: str, start: int, *, inline: bool) -> tuple[int, str] | None:
    """The end column and url of the destination that starts at `start`, or None when CommonMark reads none there."""
    read = _pointed(text, start + 1) if text.startswith("<", start) else _bare(text, start)
    if read is None:
        return None
    end, url = read
    rest = text[end:]
    after = rest.lstrip(" \t")
    titled = after != rest and after[:1] in TITLE_OPENERS
    closes = after.startswith(")") if inline else not after
    return (end, url) if (closes or titled) and url else None


def _pointed(text: str, position: int) -> tuple[int, str] | None:
    """A destination between `<` and `>`, read from just after its `<`: its end after the `>`, and its url."""
    url: list[str] = []
    while position < len(text):
        character = text[position]
        if character == ">":
            return position + 1, "".join(url)
        if character == "<":
            return None
        position = _read_character(text, position, url)
    return None


def _bare(text: str, position: int) -> tuple[int, str] | None:
    """A destination with no `<`: a run with no space or control character whose parentheses balance."""
    url: list[str] = []
    depth = 0
    while position < len(text):
        character = text[position]
        if character in " \t" or ord(character) < 0x20 or ord(character) == 0x7F:
            break
        if character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                break
            depth -= 1
        position = _read_character(text, position, url)
    return (position, "".join(url)) if depth == 0 else None


def _read_character(text: str, position: int, url: list[str]) -> int:
    """Append the character at `position` to `url` as CommonMark reads it, an escape or a reference as the character
    it stands for, and return the position after it."""
    character = text[position]
    if character == "\\" and position + 1 < len(text) and text[position + 1] in string.punctuation:
        url.append(text[position + 1])
        return position + 2
    if character == "&":
        reference = CHARACTER_REFERENCE.match(text, position)
        if reference:
            url.append(html.unescape(reference.group(0)))
            return reference.end()
    url.append(character)
    return position + 1

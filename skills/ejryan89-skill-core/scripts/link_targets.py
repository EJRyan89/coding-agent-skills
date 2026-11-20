"""The one resolver of a Markdown link's destination to the repository path it leads to, and of the columns the link
takes on its line.

A link's destination, as ejryan89-skill-core's `link_destinations` reads it into `Destination.posix_path`, is resolved
as GitHub resolves a link in a file it renders and as Claude Code reads one in a skill: against the folder of the file
that holds the link, and, when it starts with `/`, from the repository root. So `references/rules.md` and
`./references/rules.md` in `.claude/skills/review/SKILL.md` both lead to `.claude/skills/review/references/rules.md`,
`../shared.md` to `.claude/skills/shared.md`, and `/docs/rules.md` to `docs/rules.md`. A destination that is only a
fragment leads to the linking file itself.

Only a link's destination has a folder to resolve from. A path written bare in prose or in a code span is a reader's own
reading, such as a review skill's file named from the repository root its reviewer works in, and is not resolved here.
A link's text, its destination, and its title are not prose, so `link_spans` gives the columns a reader of prose
paths passes over: an inline link's from the `[` that opens its text to its closing `)`, and a definition's from its
label to the end of its line, so `[references/rules.md](references/rules.md)` names one file, the one its destination
leads to.

Limits: only a line a destination is read on has a span, so a reference link's text, as in `[text][label]`,
`[text][]`, or `[label]`, is still read as prose; `link_spans` finds the `[` that opens an inline link's text by
matching the brackets before its `](` on the same line, outside code spans and backslash escapes, and starts at the
`]` when no `[` opens the text; a link whose text starts on an earlier line, or whose `)` is on a later one, is passed
over only on the destination's line; a definition's line is passed over whole, so text after its title, as in
`[r]: d.md 'title' docs/rules.md`, is not read; and an inline link whose title never closes, or that no `)` closes
after its title, is passed over to the end of its line, so a path after it there is not read.
"""

from __future__ import annotations

import posixpath

from link_destinations import Destination
from markdown_sections import COMMONMARK_CODE_SPAN

# What follows an inline link's text, and what follows a link reference definition's label, before the destination.
INLINE_OPENER = "]("
DEFINITION_OPENER = "]:"
# What closes a title opened by each of its openers.
TITLE_CLOSERS = {'"': '"', "'": "'", "(": ")"}


def link_target(linking_file: str, destination: str) -> str | None:
    """The repository path a link's `destination` leads to from `linking_file`, both `/`-separated repository paths, or
    None when it climbs out of the repository with `..` or names the repository root itself.

    `destination` is a path, not a URL: a caller passes no destination `Destination.is_url` reports, so a leading `//`,
    a network path, never reaches here, and any leading `/` is read as the repository root.
    """
    if not destination:
        return linking_file or None
    base = "" if destination.startswith("/") else posixpath.dirname(linking_file)
    path = posixpath.normpath(posixpath.join(base, destination.lstrip("/")))
    if path in (".", "..") or path.startswith("../"):
        return None
    return path


def link_spans(line: str, destinations: list[Destination]) -> list[tuple[int, int]]:
    """The columns of `line` that each of `destinations`, those read on it, takes with its link: its text, its
    destination, and its title, each as a start and an end past the last, in the order of `destinations`.

    Every span on the line comes from one reading of it, so a line of many links costs time in proportion to its
    length: its code spans masked once, its escapes and brackets read in one pass forward, and the next unescaped `)`,
    `"`, and `'` after each column in one pass back."""
    if not destinations:
        return []
    text = COMMONMARK_CODE_SPAN.sub(lambda span: " " * len(span.group(0)), line)
    opener = _bracket_openers(text)
    following = _next_unescaped(text, ")\"'")
    indent = len(text) - len(text.lstrip(" "))
    spans: list[tuple[int, int]] = []
    for destination in destinations:
        before = destination.start
        while before > 0 and text[before - 1] in " \t":
            before -= 1
        if before >= len(DEFINITION_OPENER) and text.startswith(DEFINITION_OPENER, before - len(DEFINITION_OPENER)):
            spans.append((indent, len(line)))
            continue
        close = before - len(INLINE_OPENER)
        spans.append((opener.get(close, close), _link_end(text, destination.end, following)))
    return spans


def _bracket_openers(text: str) -> dict[int, int]:
    """The column of the `[` that opens each `]` of `text` that one opens, matched as nested brackets match, a
    backslash-escaped bracket being none."""
    openers: dict[int, int] = {}
    stack: list[int] = []
    backslashes = 0
    for position, character in enumerate(text):
        if backslashes % 2 == 0:
            if character == "[":
                stack.append(position)
            elif character == "]" and stack:
                openers[position] = stack.pop()
        backslashes = backslashes + 1 if character == "\\" else 0
    return openers


def _next_unescaped(text: str, characters: str) -> dict[str, list[int]]:
    """For each of `characters`, the column of its first unescaped occurrence at or after each column of `text`, or the
    length of `text` when none follows."""
    escaped = [False] * len(text)
    backslashes = 0
    for position, character in enumerate(text):
        escaped[position] = backslashes % 2 == 1
        backslashes = backslashes + 1 if character == "\\" else 0
    following: dict[str, list[int]] = {}
    for wanted in characters:
        columns = [len(text)] * (len(text) + 1)
        for position in range(len(text) - 1, -1, -1):
            found = text[position] == wanted and not escaped[position]
            columns[position] = position if found else columns[position + 1]
        following[wanted] = columns
    return following


def _link_end(text: str, end: int, following: dict[str, list[int]]) -> int:
    """The column past an inline link's closing `)`, from the end of its destination, over any title; the end of the
    line when its title never closes or no `)` follows it."""
    position = end
    while position < len(text) and text[position] in " \t":
        position += 1
    closer = TITLE_CLOSERS.get(text[position]) if position < len(text) else None
    if closer is not None:
        position = following[closer][position + 1] + 1
        if position > len(text):
            return len(text)
    close = following[")"][min(position, len(text))]
    return min(close + 1, len(text))

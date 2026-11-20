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
A link's text, its destination, and its title are not prose, so `link_span` gives the columns a reader of prose paths
passes over: an inline link's from the `[` that opens its text to its closing `)`, and a definition's from its label to
the end of its line, so `[references/rules.md](references/rules.md)` names one file, the one its destination leads to.

Limits: `link_span` finds the `[` that opens an inline link's text by balancing the brackets before its `](` on the
same line, outside code spans and backslash escapes, and starts at the `]` when no `[` opens the text; and a link whose
text starts on an earlier line, or whose `)` is on a later one, is passed over only on the destination's line.
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


def link_span(line: str, destination: Destination) -> tuple[int, int]:
    """The columns of `line`, the line `destination` was read on, that its link takes: its text, its destination, and
    its title, as a start and an end past the last."""
    text = COMMONMARK_CODE_SPAN.sub(lambda span: " " * len(span.group(0)), line)
    before = text[: destination.start].rstrip(" \t")
    if before.endswith(DEFINITION_OPENER) and not before.endswith(INLINE_OPENER):
        return len(text) - len(text.lstrip(" ")), len(line)
    return _opening_bracket(text, len(before) - len(INLINE_OPENER)), _link_end(text, destination.end)


def _escaped(text: str, position: int) -> bool:
    """Whether the character at `position` follows an odd run of backslashes, which escapes it."""
    run = len(text[:position]) - len(text[:position].rstrip("\\"))
    return run % 2 == 1


def _opening_bracket(text: str, close: int) -> int:
    """The column of the `[` that opens the link text the `]` at `close` ends, or `close` when none does."""
    depth = 0
    for position in range(close - 1, -1, -1):
        character = text[position]
        if character not in "[]" or _escaped(text, position):
            continue
        if character == "]":
            depth += 1
        elif depth == 0:
            return position
        else:
            depth -= 1
    return close


def _unescaped(text: str, character: str, start: int) -> int:
    """The column of the first `character` at or after `start` that no backslash escapes, or -1."""
    position = text.find(character, start)
    while position != -1 and _escaped(text, position):
        position = text.find(character, position + 1)
    return position


def _link_end(text: str, end: int) -> int:
    """The column past an inline link's closing `)`, from the end of its destination, over any title; the end of the
    line when no `)` closes it there."""
    position = end + len(text[end:]) - len(text[end:].lstrip(" \t"))
    closer = TITLE_CLOSERS.get(text[position : position + 1])
    if closer is not None:
        title_end = _unescaped(text, closer, position + 1)
        position = len(text) if title_end == -1 else title_end + 1
    close = _unescaped(text, ")", position)
    return len(text) if close == -1 else close + 1

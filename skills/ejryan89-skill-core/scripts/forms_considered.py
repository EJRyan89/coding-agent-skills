r"""The one reader of a fix pull request's `## Forms considered` section, which validation's body check and the
`implement-change` repository skill's body check share.

A pull request titled `fix` fills the section its template holds with text of the author's own, not nothing and not the
template's guidance left as it was. This reads the section's shape, never its judgment, as GitHub renders it. The
heading is an ATX `## Forms considered` no container holds, indented up to three spaces; a setext `Forms considered`
over `---`, an HTML `<h2>`, and a heading inside a quote or a list item are read as missing. The section ends at the
next first- or second-level heading GitHub renders that no container holds: an ATX heading, a setext heading, or an
HTML `<h1>` or `<h2>`, indented up to three spaces; a third-level heading, and a heading inside a quote, a list item, or
a code block, are inside it. Text that renders as nothing, such as an empty tag, an entity for a space, or an invisible
character, is no text.

The body's blocks are read by markdown_sections' block walker, the one reader of its fences, its code blocks, its list
items, and its quotes, as GitHub reads them before it hides any comment. A `<!--` that starts a line, after the quote
and list item markers that may hold it, may open an HTML block, and so may an HTML `<h1>` or `<h2>`; each is read as an
ATX heading, which starts only where an HTML block may and, like one, ends a paragraph, and the walk says whether it
does. An HTML block runs on to the line its comment's `-->` is on, or, for a heading no container holds, to the blank
line under it, and stops where its container stops holding its lines, and its lines are read as the block's, so a
fence-like line or a heading it holds is neither. Such a block changes how the lines under it are read, so the walk is
taken again past each one, over windows of lines that double, and a walk confirms a run of blocks it reads alike at
once; the reading takes time in proportion to the body.

A comment GitHub hides is not read, and the comments are read left to right: one inside a code block or an inline code
span is text GitHub shows; one that opens an HTML block, and one on a line GitHub passes on as HTML, runs to its `-->`,
or to the end of the body; and one inside a block's text runs to its `-->` when that falls within the block, and is
otherwise text.

The limits:
- inside a quote, a comment within a line runs to a `-->` anywhere in the quote, past the paragraph it opens in, where
  GitHub shows the `<!--` as text;
- inside a quote, a `<!--` after a list marker that cannot interrupt the quote's paragraph, as `> 2. <!--` under quoted
  text, is read as opening a block, where GitHub shows it as text;
- a comment block that a quote or list item stops holding before its `-->`, or a `<!--` after a block comment's `-->`
  on its closing line, ends at that `-->`, where GitHub hides the rest of the body; and a block's lines that an inner
  container stops holding while an outer one holds them, or that the walk reads as indented code, stay the block's;
- an HTML block other than a comment's or an `<h1>` or `<h2>`, such as a `<div>`, is read as a paragraph, so a
  fence-like line or a heading it holds is read as one, and an `<h1>` or `<h2>` inside a container is read as one line;
- a setext heading is held by a list item as this module's own item reading holds it, which tracks the outermost item
  alone and reads a numbered line under an item's text as a lazy line of it, so after `- a\n2. b` and a blank line, an
  underlined line indented two columns is read as the first item's, where GitHub ends the section at it, since
  markdown_sections offers no reader of the lines its containers hold;
- a code span is read by markdown_sections' `COMMONMARK_CODE_SPAN`, which may pair backtick runs of unequal length, so
  ``` ``a`<!-- x -->` ``` hides a comment GitHub shows in code, and whose search, on a line that holds a `<!--`, takes
  time that grows with the square of the line's backtick runs;
- a backslash-escaped backtick is read as opening a span, so ``\`<!-- x -->\` `` keeps a comment GitHub hides;
- a span is read within one line, so a span that crosses a line break shields no `<!--` on its second line, and its
  closing backtick opens a span on that line, so `` `a\nb` <!-- x --> `c` `` keeps a comment GitHub hides;
- `placeholder_forms` reads the template's guidance from every comment its section holds, with no code test, so a
  template whose section shows `<!--` in code yields guidance that is no comment.

A title is a fix's when it opens with the type `fix`, in any case, an optional scope, an optional `!`, a colon, and a
space, by the grammar `TITLE` reads every pull request title with.
"""

from __future__ import annotations

import html
import re
import unicodedata
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from enum import Enum

from agent_skills import XML_TAG_PATTERN
from markdown_sections import (
    CODE,
    COMMONMARK_CODE_SPAN,
    DELIMITER_ROW,
    INTERRUPTING_LIST_MARKER,
    LIST_ITEM_BLOCK,
    LIST_MARKER,
    MAX_BLOCK_INDENT,
    ORDERED_MARKER,
    PARAGRAPH,
    QUOTE,
    TABLE_ROW,
    THEMATIC_BREAK,
    blocks,
    code_block_lines,
    document_headings,
    indentation,
    is_blank,
    list_item,
    markdown_section,
)
from text_lines import LINE_BREAK

FIX_TYPE = "fix"
FORMS_HEADING = "## Forms considered"
# A pull request title: its type, an optional scope, an optional breaking-change mark, a colon, a space, and text.
TITLE = re.compile(r"(?P<type>[A-Za-z]+)(?P<scope>\([^()\s][^()]*\))?(?P<mark>!?): \S.*")
# The section's own heading: an ATX heading of the second level indented up to three spaces, with any closing hashes.
FORMS_LINE = re.compile(r" {0,3}## +Forms considered(?: +#*)?[ \t]*")
# The deepest heading that ends the section: one of the first or second level does, while a third-level one, such as a
# table's caption, is inside it.
SECTION_LEVEL = 2
# The blockquote and list item markers a line opens with, read as markdown_sections' FENCE_OPENER reads the ones that
# hold a fence, each followed by its spaces and tabs once, so the match stays linear.
CONTAINER_MARKERS = rf"[ \t]*(?:(?:>|{LIST_MARKER}[ \t])[ \t]*)*"
# A line whose `<!--` may open an HTML block, after the markers that may hold it; whether it does is the block walk's to
# say, from what the line is read as.
BLOCK_COMMENT = re.compile(rf"{CONTAINER_MARKERS}<!--")
# A line that ends at a list marker with nothing after it: an item that opens empty, which the block walk reads once a
# space follows.
BARE_MARKER = re.compile(rf"{CONTAINER_MARKERS}{LIST_MARKER}")
# A line that ends at a run of `#` with nothing but spaces and tabs after it: an empty ATX heading, which the block
# walk, reading a heading's text with the spaces around it stripped, reads once a `#` of text follows.
EMPTY_HEADING = re.compile(rf"{CONTAINER_MARKERS}#{{1,6}}[ \t]*")
# An HTML h1 or h2 block's start, indented up to three spaces. CommonMark starts an HTML block only where the tag name
# is followed by a space, a tab, the line's end, `>`, or `/>`, so any other Unicode space leaves the line text.
HTML_HEADING = re.compile(r" {0,3}<h[12](?:[ \t>]|/>|$)", re.IGNORECASE)
# What a line that starts with a comment opener, or with an HTML heading's tag, is read as for the block walk: an ATX
# heading, which, as an HTML block does, starts only where a block may, at most MAX_BLOCK_INDENT columns into the
# content that holds it, ends a paragraph, and is no lazy line, and which the walk reports when no container holds it.
HEADING_STAND_IN = "# stand-in"
# The blocks whose text a setext underline may make a heading: a paragraph, and a `|` line GitHub reads as one.
TEXT_BLOCKS = frozenset({PARAGRAPH, TABLE_ROW})
# What a line inside an HTML heading's block is read as for the block walk, after the markers and indentation that hold
# it: text, which starts no block and continues the block's paragraph, as the HTML block's lines are its own.
HTML_TEXT = "html"
# A line that is no paragraph's text: blank, a heading, a list item, a quote, a fence, or a rule. Markdown's whitespace
# is the space and the tab, and its list markers are read by markdown_sections' rules, so a digit outside ASCII or a
# line of no-break spaces is text.
NOT_PARAGRAPH = re.compile(
    rf"[ \t]*$| {{0,3}}(?:#{{1,6}}(?:[ \t]|$)|[-*+](?:[ \t]|$)|{ORDERED_MARKER}(?:[ \t]|$)|>|`{{3,}}|~{{3,}}"
    r"|([-*_])(?:[ \t]*\1){2,}[ \t]*$)"
)
# A line that ends the paragraph above it: as NOT_PARAGRAPH, but a list item only where CommonMark lets one interrupt a
# paragraph, a bullet or an item numbered 1 with text after it; any other ordered line continues the paragraph.
ENDS_PARAGRAPH = re.compile(
    rf"[ \t]*$| {{0,3}}(?:#{{1,6}}(?:[ \t]|$)|{INTERRUPTING_LIST_MARKER}[ \t]+[^ \t]|>|`{{3,}}|~{{3,}}"
    r"|([-*_])(?:[ \t]*\1){2,}[ \t]*$)"
)
# A line that opens a list item, indented up to three spaces: its marker, and the spaces or tabs after it, or none
# where the item opens empty.
LIST_OPENER = re.compile(rf"(?P<indent> {{0,3}})(?P<marker>{LIST_MARKER})(?P<gap>[ \t]*)(?P<text>.*)")
# Past this many columns after its marker, an item's text is an indented code block, and its content starts one
# column after the marker.
MAX_ITEM_GAP = 4
# A setext heading's underline, which makes the paragraph above it, in the same container, a heading.
SETEXT_UNDERLINE = re.compile(r" {0,3}(?:=+|-+)[ \t]*")
# An HTML comment as GitHub hides it: the empty forms `<!-->` and `<!--->`, or up to `-->`, or, left unclosed, to the
# end of the body.
HIDDEN_COMMENT = re.compile(r"<!--(?:-?>|.*?(?:-->|\Z))", re.DOTALL)
COMMENT_OPENER = "<!--"
COMMENT_CLOSER = "-->"
# The blockquote markers a line opens with, matched greedily once and never backtracked into, which a comment block's
# later lines keep so the quote holds them.
QUOTE_PREFIX = re.compile(r"(?:[ \t]*>[ \t]?)*")
# A stand-in's line that only blockquotes hold: its quote markers from the line's start, and the stand-in at most one
# space after them. A list item holds no line its markers start at the first column, and one inside a quote holds a
# line only from two columns past the quote's content, where its own text starts.
QUOTED_OPENER = re.compile(rf"(?:>[ \t]?)+[ ]?{re.escape(HEADING_STAND_IN)}")
# The lines the block walk first reads past the last place it starts from to find the next comment block that spans
# lines, doubled until it finds one, so a body is walked a bounded number of times over.
WALK_WINDOW = 8
# Characters that render as nothing but are neither whitespace nor a format character, which are dropped by their
# category: the Hangul fillers and the blank braille pattern.
INVISIBLE = frozenset("ᅟᅠㅤﾠ⠀")
# What a line holds that is no text of the author's: list, quote, and table markers, rules, and punctuation. A list
# marker is read by markdown_sections' rule, followed by a space, a tab, or the line's end. The class's `\s` is not a
# block rule: every Unicode space renders blank, so it is no text of the author's either.
MARKUP = re.compile(rf"[\s\-*+>|#:.,;_=~`'\"()\[\]]+|{ORDERED_MARKER}(?=[ \t]|$)")
# A filler word that stands in for an answer rather than giving one; the comparison ignores case.
FILLERS = frozenset({"none", "n/a", "na", "tbd", "todo", "nothing", "same", "see above", "see below"})


class FormsFault(Enum):
    """What is wrong with a fix body's section: no section, the template's placeholder, or no text of the author's."""

    MISSING = "missing"
    PLACEHOLDER = "placeholder"
    EMPTY = "empty"


def is_fix_title(title: str) -> bool:
    """Whether a pull request title's type is `fix`, in any case, so `Fix: ...` is a fix here as well as a title the
    title check refuses."""
    match = TITLE.fullmatch(title.strip())
    return match is not None and match.group("type").lower() == FIX_TYPE


@dataclass(frozen=True)
class _Walked:
    """What one walk of markdown_sections' block walker says of the lines it read, by each line's index in the body:
    the lines of code blocks, the level of each heading no container holds, and, for each line of a block that is not
    code, the block's kind and whether the line is its first."""

    code: frozenset[int]
    headings: dict[int, int]
    held: dict[int, tuple[str, bool]]


def _walked(read: list[str], offset: int, *, fences: bool = False) -> _Walked:
    """The walk of `read`, the body's lines from index `offset` on as the walk reads them. Its code is the lines of its
    code blocks' content, and, with `fences`, each fence's opening and closing lines too, which a stand-in never is."""
    held: dict[int, tuple[str, bool]] = {}
    code: set[int] = set()
    for block in blocks(read):
        lines = range(offset + block.start, offset + block.start + len(block.lines))
        if block.kind == CODE:
            code.update(lines)
        else:
            held.update((line, (block.kind, line == lines.start)) for line in lines)
    if fences:
        code.update(offset + index for index in code_block_lines(read))
    return _Walked(
        frozenset(code), {offset + index: len(match.group("marks")) for index, match in document_headings(read)}, held
    )


def _opens_block(walked: _Walked, index: int, line: str) -> bool:
    """Whether the stand-in heading on the line the walk read opens a block there, as the `<!--` it stands in for opens
    an HTML block: not in code, and not text that continues a paragraph or a list item's text. A quote's lines are one
    block to the walk, so a quoted stand-in opens one when it stands at most MAX_BLOCK_INDENT columns after the quote's
    markers."""
    if index in walked.code:
        return False
    kind, first = walked.held.get(index, ("", True))
    if kind == PARAGRAPH:
        return False
    if kind == LIST_ITEM_BLOCK:
        return first
    if kind == QUOTE:
        quotes = QUOTE_PREFIX.match(line)
        return indentation(line[quotes.end() if quotes is not None else 0 :]) <= MAX_BLOCK_INDENT
    return True


@dataclass(frozen=True)
class _Reading:
    """The body read for its comments and blocks: its lines, where each starts in it, the lines as the block walk read
    them, the walk of those, the lines a stand-in heading reads for that are no heading of the body's, and the span of
    each comment GitHub hides, in order."""

    text: str
    lines: list[str]
    starts: list[int]
    read: list[str]
    walked: _Walked
    stand_ins: frozenset[int]
    hidden: list[tuple[int, int]]

    def visible(self, start: int, stop: int) -> str:
        """The body from index `start` to `stop` without the hidden comments in it."""
        kept: list[str] = []
        position = start
        for first, past in self.hidden[max(bisect_right(self.hidden, (start, len(self.text))) - 1, 0) :]:
            if first >= stop:
                break
            if past > position:
                kept.append(self.text[position : max(first, position)])
                position = min(past, stop)
        kept.append(self.text[position:stop])
        return "".join(kept)


def _reading(text: str) -> _Reading:
    """The body read as GitHub reads it before it hides comments: its blocks, with each comment and each HTML heading
    that opens an HTML block read as one, then the comments it hides, left to right."""
    lines = LINE_BREAK.split(text)
    starts = [0, *(match.end() for match in LINE_BREAK.finditer(text))]
    read = [_with_bare_end(line) for line in lines]
    headings: set[int] = set()
    # Each HTML block that may run past its line: its first line's index, its last line's, and whether it is a heading.
    spanning: list[tuple[int, int, bool]] = []
    closers = _closers(text)
    blank_after = len(lines)
    blanks = [len(lines)] * len(lines)
    for index in range(len(lines) - 1, -1, -1):
        blanks[index] = blank_after
        if is_blank(lines[index]):
            blank_after = index
    for index, line in enumerate(lines):
        if HTML_HEADING.match(line):
            read[index] = line[: len(line) - len(line.lstrip(" "))] + HEADING_STAND_IN
            headings.add(index)
            if blanks[index] - 1 > index:
                spanning.append((index, blanks[index] - 1, True))
            continue
        block = BLOCK_COMMENT.match(line) if COMMENT_OPENER in line else None
        if block is None:
            continue
        read[index] = line[: block.end() - len(COMMENT_OPENER)] + HEADING_STAND_IN
        end = _comment_end(text, closers, starts[index] + block.end() - len(COMMENT_OPENER))
        last = bisect_right(starts, end - 1) - 1
        if last > index:
            spanning.append((index, last, False))
    blocked = _spanning_blocks(lines, read, spanning)
    walked = _walked(read, 0, fences=True)
    # Every line a stand-in heading reads for, but an HTML heading's own, is no heading of the body's.
    stand_ins = {index for index, line in enumerate(read) if line != lines[index] and line.endswith(HEADING_STAND_IN)}
    # The lines GitHub passes on as HTML, where a comment runs to its `-->` wherever that falls: a block's first line,
    # a comment block's last, and every line of an HTML heading's block.
    raw = {index for index in stand_ins | headings if _opens_block(walked, index, read[index])}
    for index, (last, heading) in blocked.items():
        raw.update(range(index + 1, last + 1) if heading else (last,))
    hidden = _hidden_comments(text, closers, lines, starts, walked, raw)
    return _Reading(text, lines, starts, read, walked, frozenset(stand_ins - headings), hidden)


def _with_bare_end(line: str) -> str:
    """The line as the block walk reads it: one that ends at a list marker gets a space after it, and one that ends at
    a run of `#` a `#` of text, so the walk reads the item that opens empty, or the empty heading, CommonMark reads
    there."""
    if BARE_MARKER.fullmatch(line):
        return line + " "
    if EMPTY_HEADING.fullmatch(line):
        return line.rstrip(" \t") + " #"
    return line


def _spanning_blocks(
    lines: list[str], read: list[str], spanning: list[tuple[int, int, bool]]
) -> dict[int, tuple[int, bool]]:
    """Each HTML block that runs past its first line, a comment's or a heading's, as its first line's index, its last
    line's, and whether it is a heading's, read into `read` by `_Blocks`, so the walk meets no fence-like line or
    heading the block holds.

    Whether a stand-in opens a block depends only on the lines above it, so a walk judges each stand-in exactly up to
    the first whose block it has not read as one. A walk therefore settles the blocks in order while each one's reading
    agrees with its judgment, corrects the first that does not, and reads every later block in its window as that walk
    judged it, for the next walk to confirm or correct. A walk starts past the last block no container holds, or past
    the last heading no container holds, since nothing above either changes how the lines below it are read, and reads
    a window of lines that doubles while every block in it is confirmed, so a run of blocks the walk judges alike is
    walked a bounded number of times over."""
    blocks = _Blocks(lines, read, sorted(spanning))
    start, window = 0, WALK_WINDOW
    while blocks.settled < len(blocks.spanning):
        stop = min(len(read), max(start + window, blocks.reach()))
        walked = _walked(read[start:stop], start)
        corrected, restart = blocks.settle(walked, stop)
        if restart > start:
            start, window = restart, WALK_WINDOW
        elif not corrected:
            if stop >= len(read):
                break
            window *= 2
    return blocks.found()


class _Blocks:
    """The spanning HTML blocks `_spanning_blocks` reads: for each, its last line, whether `read` reads it as a block,
    and whether a container holds it; how many are settled, and the last line of the settled blocks, which a later
    stand-in at or above is inside."""

    def __init__(self, lines: list[str], read: list[str], spanning: list[tuple[int, int, bool]]) -> None:
        self.lines, self.read, self.initial, self.spanning = lines, read, list(read), spanning
        self.lasts = [last for _, last, _ in spanning]
        self.applied = [False] * len(spanning)
        # Whether each block read as one has its lines read as a container's, which the walk may not confirm.
        self.filled_contained = [False] * len(spanning)
        self.settled = 0
        self.cover = -1

    def reach(self) -> int:
        """The line a walk reads up to at least: past the first unsettled block's first line, and past its last when
        it is read as one, so the walk shows whether its container holds every line of it."""
        index = self.spanning[self.settled][0]
        return self.lasts[self.settled] + 1 if self.applied[self.settled] else index + 1

    def found(self) -> dict[int, tuple[int, bool]]:
        """Each block read as one, by its first line: its last line, and whether it is an HTML heading's."""
        return {
            index: (self.lasts[entry], heading)
            for entry, (index, _, heading) in enumerate(self.spanning)
            if self.applied[entry]
        }

    def judge(self, walked: _Walked, entry: int) -> bool:
        """Whether the walk reads the stand-in of the block as opening one: an HTML heading's block only where no
        container holds it."""
        index, _, heading = self.spanning[entry]
        return _opens_block(walked, index, self.read[index]) and (not heading or index in walked.headings)

    def apply(self, entry: int, block: bool, walked: _Walked, cover: int = -1) -> None:
        """Read the block's lines after its first as the block's, or as they are, but for those up to `cover`, the last
        line of an earlier block read as one, which are that block's."""
        index, _, heading = self.spanning[entry]
        self.applied[entry] = block
        contained = index not in walked.headings
        self.filled_contained[entry] = block and contained
        for later in range(max(index, cover) + 1 if not block else index + 1, self.lasts[entry] + 1):
            if not block:
                self.read[later] = self.initial[later]
                continue
            quotes = QUOTE_PREFIX.match(self.lines[later])
            held = "" if quotes is None else quotes.group()
            rest = self.lines[later][len(held) :]
            indent = rest[: len(rest) - len(rest.lstrip(" \t"))]
            if heading:
                self.read[later] = held + indent + HTML_TEXT
            elif contained and not is_blank(rest):
                self.read[later] = held + indent + HEADING_STAND_IN
            else:
                self.read[later] = held

    def settle(self, walked: _Walked, stop: int) -> tuple[bool, int]:
        """Settle the blocks the walk confirms, in order, then correct the first it does not and read each later one
        in the window as the walk judged it. Returns whether it corrected one, and the furthest line a later walk may
        start from, or -1 when none."""
        cover, restart = self.cover, -1
        for entry in range(self.settled, len(self.spanning)):
            index = self.spanning[entry][0]
            if index >= stop:
                break
            block = index > cover and self.judge(walked, entry)
            if block and self.applied[entry] and self._contained(walked, entry) and self.lasts[entry] >= stop:
                # The walk stops before the block's last line, so the next walk, which reaches it, judges the block.
                break
            left = self._left(walked, entry) if block and self.applied[entry] else None
            # A block read as one whose lines were read for another place than the walk shows is read again.
            refill = block and self.applied[entry] and self.filled_contained[entry] != (index not in walked.headings)
            if block == self.applied[entry] and left is None and not refill:
                restart = max(restart, self._settle(walked, entry))
                cover = self.cover
                continue
            if left is not None:
                # The container stopped holding the comment's lines, which ends its HTML block, as CommonMark ends it,
                # and no container holds the line it stopped at, so a later walk may start there.
                self.read[left : self.lasts[entry] + 1] = self.initial[left : self.lasts[entry] + 1]
                self.lasts[entry] = left - 1
                restart = max(restart, left)
            else:
                self.apply(entry, block, walked)
            if block and left is None and self._contained(walked, entry):
                # The next walk shows whether the container holds the block's lines, so it stays unsettled.
                self._guess(walked, entry, stop)
                return True, restart
            restart = max(restart, self._settle(walked, entry))
            self._guess(walked, entry, stop)
            return True, restart
        return False, restart

    def _guess(self, walked: _Walked, corrected: int, stop: int) -> None:
        """Read each block after the corrected one in the window as the walk judged it, but where the walk read its
        stand-in as code that starts inside the lines of an earlier block now read as one, whose fence-like line the
        walk read as a fence, so the next walk confirms a run of such blocks at once."""
        cover = max(self.cover, self.lasts[corrected] if self.applied[corrected] else -1)
        extent = (self.spanning[corrected][0], self.lasts[corrected]) if self.applied[corrected] else (-1, -1)
        runs: dict[int, int] = {}
        for line in sorted(walked.code):
            runs[line] = runs.get(line - 1, line)
        for later in range(corrected + 1, len(self.spanning)):
            index = self.spanning[later][0]
            if index >= stop:
                break
            guess = index > cover and (
                self.judge(walked, later) or (index in runs and extent[0] < runs[index] <= extent[1])
            )
            if guess != self.applied[later]:
                self.apply(later, guess, walked, cover)
            if guess:
                cover = max(cover, self.lasts[later])
                extent = (index, self.lasts[later])

    def _contained(self, walked: _Walked, entry: int) -> bool:
        """Whether the block is a comment's that a container holds, whose lines the container may stop holding."""
        return not self.spanning[entry][2] and self.spanning[entry][0] not in walked.headings

    def _left(self, walked: _Walked, entry: int) -> int | None:
        """The first line of a comment's block, read as one inside a container, that the container no longer holds."""
        index = self.spanning[entry][0]
        if not self._contained(walked, entry):
            return None
        return next((line for line in range(index + 1, self.lasts[entry] + 1) if line in walked.headings), None)

    def _settle(self, walked: _Walked, entry: int) -> int:
        """Settle the block; the line a later walk may start from, where the walk's reading of the lines above
        changes nothing below, or -1: past a block read as one that no container holds, or that only blockquotes hold,
        whose lines below reopen them by their own markers, or else past the last heading no container holds above
        it."""
        self.settled = entry + 1
        index = self.spanning[entry][0]
        if self.applied[entry]:
            self.cover = max(self.cover, self.lasts[entry])
            if index in walked.headings or QUOTED_OPENER.fullmatch(self.read[index]):
                return self.lasts[entry] + 1
        return max((line + 1 for line in walked.headings if line < index), default=-1)


def _closers(text: str) -> list[int]:
    """The index of each `-->` in the text, read once, so finding a comment's end takes a search, not a scan."""
    found: list[int] = []
    position = text.find(COMMENT_CLOSER)
    while position != -1:
        found.append(position)
        position = text.find(COMMENT_CLOSER, position + 1)
    return found


def _comment_end(text: str, closers: list[int], opener: int) -> int:
    """The index just past the comment whose `<!--` is at `opener`, as HIDDEN_COMMENT reads it: past an empty `<!-->`
    or `<!--->`, past the first `-->` after the opener, or the end of the text when none follows. `closers` holds every
    `-->` in the text."""
    for empty in (">", "->"):
        if text.startswith(empty, opener + len(COMMENT_OPENER)):
            return opener + len(COMMENT_OPENER) + len(empty)
    following = bisect_left(closers, opener + len(COMMENT_OPENER))
    return closers[following] + len(COMMENT_CLOSER) if following < len(closers) else len(text)


def _hidden_comments(
    text: str, closers: list[int], lines: list[str], starts: list[int], walked: _Walked, raw: set[int]
) -> list[tuple[int, int]]:
    """The span of each comment GitHub hides, read left to right: one whose `<!--` opens outside every code block and
    code span, up to its `-->`. A comment within a block's text ends within that block, or is text; one on a line
    GitHub passes on as HTML, a comment block's first or last, runs to its `-->` wherever that falls, or to the end of
    the body."""
    hidden: list[tuple[int, int]] = []
    position = 0
    # The next code span on the line the last `<!--` was on, read up to that line's end, which `searched` holds.
    span: re.Match[str] | None = None
    searched = -1
    ends = _block_ends(walked, len(lines))
    while (opener := text.find(COMMENT_OPENER, position)) != -1:
        line = bisect_right(starts, opener) - 1
        line_end = starts[line] + len(lines[line])
        if searched != line_end or (span is not None and span.start() < position):
            span = COMMONMARK_CODE_SPAN.search(text, max(position, starts[line]), line_end)
            searched = line_end
        while span is not None and span.end() <= opener:
            span = COMMONMARK_CODE_SPAN.search(text, span.end(), line_end)
        if span is not None and span.start() < opener:
            # A code span that holds the `<!--` shows it, and every character up to the span's end.
            position = span.end()
            continue
        end = _comment_end(text, closers, opener)
        last = ends[line]
        if line in walked.code or (line not in raw and end > starts[last] + len(lines[last])):
            position = opener + 1
            continue
        hidden.append((opener, end))
        position = end
    return hidden


def _block_ends(walked: _Walked, count: int) -> list[int]:
    """For each line, the index of the last line of the block that holds it, or its own index when none does."""
    ends = list(range(count))
    last: int | None = None
    for index in range(count - 1, -1, -1):
        kind, first = walked.held.get(index, ("", True))
        if not kind:
            last = None
            continue
        last = index if last is None else last
        ends[index] = last
        if first:
            last = None
    return ends


def without_hidden_comments(text: str) -> str:
    """`text` without each comment GitHub hides: one whose `<!--` opens outside every code block and code span, up to
    its `-->`, as `_hidden_comments` reads them. A `<!--` that code shows is text, and so is one within a block's text
    that its block ends before any `-->`."""
    if COMMENT_OPENER not in text:
        return text
    reading = _reading(text)
    return reading.visible(0, len(text))


def forms_section(body: str) -> str | None:
    """The text under the body's first `## Forms considered` heading, up to the next heading of the first or second
    level, with hidden comments removed; None when the body has no such heading. A heading inside a hidden comment, a
    code block, a list item, or a quote is not the section's, and none ends it."""
    reading = _reading(body)
    headings = {index: level for index, level in reading.walked.headings.items() if index not in reading.stand_ins}
    start = next(
        (
            index + 1
            for index in sorted(headings)
            if headings[index] == SECTION_LEVEL and FORMS_LINE.fullmatch(_visible_line(reading, index))
        ),
        None,
    )
    if start is None:
        return None
    ends = [index for index, level in headings.items() if index >= start and level <= SECTION_LEVEL]
    end = min([*ends, *_setext_headings(reading, start), len(reading.lines)])
    if end == start:
        return ""
    stop = reading.starts[end - 1] + len(reading.lines[end - 1])
    return "\n".join(LINE_BREAK.split(reading.visible(reading.starts[start], stop)))


def _visible_line(reading: _Reading, index: int) -> str:
    """The line at `index` without the hidden comments in it."""
    start = reading.starts[index]
    return reading.visible(start, start + len(reading.lines[index]))


def _words(text: str) -> str:
    """The text as it renders, with its markup dropped and its whitespace collapsed, lowercased, for comparison: tags
    removed, then entities read, so `&lt;br&gt;` stays the text it shows, then format and invisible characters
    removed."""
    # A tag renders as no text of its own.
    text = html.unescape(XML_TAG_PATTERN.sub(" ", text))
    text = "".join(
        character for character in text if unicodedata.category(character) != "Cf" and character not in INVISIBLE
    )
    return " ".join(MARKUP.sub(" ", text).split()).lower()


def placeholder_forms(template: str) -> tuple[str, set[str]]:
    """The template section's visible text, and the text of each of its comments, which an author may have kept with
    the comment markers dropped, each as _words gives it."""
    section = markdown_section(template.replace("\r\n", "\n"), FORMS_HEADING) or ""
    guidance = {_words(comment.removeprefix("<!--").removesuffix("-->")) for comment in HIDDEN_COMMENT.findall(section)}
    return _words(without_hidden_comments(section)), guidance - {""}


def is_placeholder(words: str, template: str) -> bool:
    """Whether a section's words are still the template's: its visible placeholder, or its guidance standing alone or
    beside that placeholder or a filler."""
    visible, guidance = placeholder_forms(template)
    if visible and words == visible:
        return True
    rest = words
    for comment in guidance:
        rest = rest.replace(comment, " ")
    rest = " ".join(rest.split())
    return rest != words and (not rest or rest == visible or rest in FILLERS)


def forms_fault(body: str, template: str) -> FormsFault | None:
    """What is wrong with the body's `## Forms considered` section as a fix's, judged against `template`, the pull
    request template the body was written from; None when the section holds text of the author's own."""
    section = forms_section(body)
    if section is None:
        return FormsFault.MISSING
    words = _words(section)
    if is_placeholder(words, template):
        return FormsFault.PLACEHOLDER
    if not words or words in FILLERS:
        return FormsFault.EMPTY
    return None


def in_list_items(lines: list[str], inside_fences: set[int]) -> set[int]:
    """The index of each line after a list item's first that the item holds, as CommonMark reads it: a blank line, a
    line indented at least to the column its text starts at, a fence's body, or a lazy line continuing its paragraph.
    An item that opens empty holds no blank line right after its marker's line, since CommonMark lets an item begin
    with one blank line at most, so that line ends it. Only the outermost item is tracked, since a line a nested item
    holds is held by the outer one too."""
    held: set[int] = set()
    content: int | None = None
    # Whether the item opened empty on the line before, so a blank line now ends it.
    opened_empty = False
    # Whether the line before is paragraph text, which a lazy line continues and only some markers interrupt.
    paragraph = False
    for index, line in enumerate(lines):
        if content is not None and opened_empty and is_blank(line) and index not in inside_fences:
            content = None
        opened_empty = False
        if content is not None:
            if index in inside_fences or is_blank(line) or indentation(line) >= content:
                held.add(index)
                nested = " " * (indentation(line) - content) + line.lstrip(" \t")
                paragraph = not is_blank(line) and index not in inside_fences and not NOT_PARAGRAPH.match(nested)
                continue
            if paragraph and not ENDS_PARAGRAPH.match(line):
                held.add(index)
                continue
            content = None
        opener = LIST_OPENER.fullmatch(line)
        if (
            opener is not None
            and not THEMATIC_BREAK.fullmatch(line)
            and list_item(line, interrupting=paragraph) is not None
        ):
            content = _content_column(opener)
            paragraph = not is_blank(opener.group("text"))
            opened_empty = not paragraph
        elif opener is not None and is_blank(opener.group("gap") + opener.group("text")) and not paragraph:
            # An item that opens empty: its content starts one column after the marker.
            content = len(opener.group("indent")) + len(opener.group("marker")) + 1
            paragraph = False
            opened_empty = True
        else:
            paragraph = index not in inside_fences and not NOT_PARAGRAPH.match(line)
    return held


def _content_column(opener: re.Match[str]) -> int:
    """The column a list item's text starts at, which a line must be indented to for the item to hold it."""
    marker_end = len(opener.group("indent")) + len(opener.group("marker"))
    gap = indentation(opener.group("gap") + "x")
    return marker_end + (1 if gap > MAX_ITEM_GAP or is_blank(opener.group("text")) else gap)


def _setext_headings(reading: _Reading, start: int) -> list[int]:
    """The first line of the first setext heading of the first or second level at or after `start` that no container
    holds: a paragraph, or a `|` line GitHub reads as one, with an underline right under it, both outside every
    container and code block. A table's rows are no paragraph."""
    walked = reading.walked
    # Which lines a container holds: a quote's, as the walk reads them, and a list item's, by this module's own item
    # reading until markdown_sections offers one.
    contained = in_list_items(reading.read, set(walked.code)) | {
        line for line, (kind, _) in walked.held.items() if kind == QUOTE
    }
    for underline in range(start + 1, len(reading.lines)):
        if underline in walked.code or underline in contained:
            continue
        if not SETEXT_UNDERLINE.fullmatch(reading.lines[underline]):
            continue
        first = underline - 1
        while first >= start and first not in contained and walked.held.get(first, ("", True))[0] in TEXT_BLOCKS:
            if DELIMITER_ROW.fullmatch(reading.read[first]) and walked.held.get(first - 1, ("", True))[0] == TABLE_ROW:
                first = underline
                break
            first -= 1
        if first + 1 < underline:
            return [first + 1]
    return []

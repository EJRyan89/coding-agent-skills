r"""The one reader of the HTML comments GitHub hides in a pull request body, a commit message, or any other Markdown it
renders, which forms_considered, release notes, the silent-revert check, and `implement-change`'s body check share.

A comment GitHub hides is not read, while a `<!--` that code shows is text. The body's blocks are read by
markdown_sections' block walker, the one reader of its fences, its code blocks, its list items, and its quotes, as
GitHub reads them before it hides any comment. A `<!--` that starts a line, after the quote and list item markers that
may hold it, may open an HTML block, and so may an HTML `<h1>` or `<h2>`; each is read as an ATX heading, which starts
only where an HTML block may and, like one, ends a paragraph, and the walk says whether it does. An HTML block runs on
to the line its comment's `-->` is on, or, for a heading no container holds, to the blank line under it, and stops
where its container stops holding its lines, and its lines are read as the block's, so a fence-like line or a heading
it holds is neither. Such a block changes how the lines under it are read, so the walk is taken again past each one,
over windows of lines that double, and a walk confirms a run of blocks it reads alike at once; the reading takes time
in proportion to the body.

The comments are read left to right: one inside a code block is text GitHub shows, and so is one inside an inline code
span but on a line GitHub passes on as HTML, where no backtick opens a span; one that opens an HTML block, and one on a
line GitHub passes on as HTML, runs to its `-->`, or to the end of the body; and one inside a block's text runs to its
`-->` when that falls within the block, and is otherwise text. An inline code span is read as markdown_sections'
code_spans reads one, pairing whole backtick runs, and, past a comment hidden on its line, as the line's runs pair from
that comment's end, as CommonMark reads them; a line's spans are read in time in proportion to it. `hidden_spans` gives
where each hidden comment starts and ends, so a reader that numbers lines can blank a comment in place, and
`without_hidden_comments` the text without them.

The limits:
- inside a quote, a comment within a line runs to a `-->` anywhere in the quote, past the paragraph it opens in, where
  GitHub shows the `<!--` as text;
- inside a quote, a `<!--` after a list marker that cannot interrupt the quote's paragraph, as `> 2. <!--` under quoted
  text, is read as opening a block, where GitHub shows it as text;
- a comment block that a quote or list item stops holding before its `-->`, or a `<!--` after a block comment's `-->`
  on its closing line, ends at that `-->`, and a heading it holds is hidden, while GitHub hides the rest of the body,
  or, where a later comment passes a `-->` on as HTML, hides up to it and shows what follows inside the quote or list
  item, so a heading after it is the container's; and a block's lines that an inner container stops holding while an
  outer one holds them, or that the walk reads as indented code, stay the block's;
- an HTML block other than a comment's or an `<h1>` or `<h2>`, such as a `<div>`, is read as a paragraph, so a
  fence-like line or a heading it holds is read as one, and an `<h1>` or `<h2>` inside a container is read as one line;
- a `<!--` four or more columns into a line an HTML `<h1>` or `<h2>` block holds is read as indented code, and so is
  text, where GitHub passes the line on as HTML and hides the rest of the body;
- a comment whose text ends with `-`, as `<!-- a--->`, is hidden, where GitHub shows it as text;
- a backslash-escaped backtick is read as opening a span, so ``\`<!-- x -->\` `` keeps a comment GitHub hides;
- a span is read within one line, so a span that crosses a line break shields no `<!--` on its second line, and its
  closing backtick opens a span on that line, so `` `a\nb` <!-- x --> `c` `` keeps a comment GitHub hides.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterator
from dataclasses import dataclass

from markdown_sections import (
    CODE,
    LIST_ITEM_BLOCK,
    LIST_MARKER,
    MAX_BLOCK_INDENT,
    PARAGRAPH,
    QUOTE,
    CodeSpan,
    CodeSpanReader,
    blocks,
    code_block_lines,
    document_headings,
    indentation,
    is_blank,
)
from text_lines import LINE_BREAK, line_index, line_starts

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
# What a line inside an HTML heading's block is read as for the block walk, after the markers and indentation that hold
# it: text, which starts no block and continues the block's paragraph, as the HTML block's lines are its own.
HTML_TEXT = "html"
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


@dataclass(frozen=True)
class Walked:
    """What one walk of markdown_sections' block walker says of the lines it read, by each line's index in the body:
    the lines of code blocks, the level of each heading no container holds, and, for each line of a block that is not
    code, the block's kind and whether the line is its first."""

    code: frozenset[int]
    headings: dict[int, int]
    held: dict[int, tuple[str, bool]]


def _walked(read: list[str], offset: int, *, fences: bool = False) -> Walked:
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
    return Walked(
        frozenset(code), {offset + index: len(match.group("marks")) for index, match in document_headings(read)}, held
    )


def _opens_block(walked: Walked, index: int, line: str) -> bool:
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
class Reading:
    """The body read for its comments and blocks: its lines, where each starts in it, the lines as the block walk read
    them, the walk of those, the lines a stand-in heading reads for that are no heading of the body's, and the span of
    each comment GitHub hides, in order."""

    text: str
    lines: list[str]
    starts: list[int]
    read: list[str]
    walked: Walked
    stand_ins: frozenset[int]
    hidden: list[tuple[int, int]]

    def hides(self, index: int) -> bool:
        """Whether a hidden comment holds the first character of the line at `index` that is no space or tab."""
        line = self.lines[index]
        position = self.starts[index] + len(line) - len(line.lstrip(" \t"))
        span = bisect_right(self.hidden, (position, len(self.text) + 1)) - 1
        return span >= 0 and self.hidden[span][0] <= position < self.hidden[span][1]

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


def read_comments(text: str) -> Reading:
    """The body read as GitHub reads it before it hides comments: its blocks, with each comment and each HTML heading
    that opens an HTML block read as one, then the comments it hides, left to right."""
    lines = LINE_BREAK.split(text)
    starts = line_starts(text)
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
        last = line_index(starts, end - 1)
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
    return Reading(text, lines, starts, read, walked, frozenset(stand_ins - headings), hidden)


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
    whether a container holds it, and the last of its lines `read` reads as the block's so far, which grow with the
    walk's window, so a block that runs on past where its container stops holding it costs only the lines read up to
    there; how many are settled, and the last line of the settled blocks, which a later stand-in at or above is
    inside."""

    def __init__(self, lines: list[str], read: list[str], spanning: list[tuple[int, int, bool]]) -> None:
        self.lines, self.read, self.initial, self.spanning = lines, read, list(read), spanning
        self.lasts = [last for _, last, _ in spanning]
        self.applied = [False] * len(spanning)
        # Whether each block read as one has its lines read as a container's, which the walk may not confirm.
        self.filled_contained = [False] * len(spanning)
        self.filled = [index for index, _, _ in spanning]
        self.settled = 0
        self.cover = -1

    def reach(self) -> int:
        """The line a walk reads up to at least: past the first unsettled block's first line."""
        return self.spanning[self.settled][0] + 1

    def found(self) -> dict[int, tuple[int, bool]]:
        """Each block read as one, by its first line: its last line, and whether it is an HTML heading's, with every
        one of its lines read as the block's."""
        for entry in range(len(self.spanning)):
            if self.applied[entry]:
                self._fill(entry, self.lasts[entry])
        return {
            index: (self.lasts[entry], heading)
            for entry, (index, _, heading) in enumerate(self.spanning)
            if self.applied[entry]
        }

    def judge(self, walked: Walked, entry: int) -> bool:
        """Whether the walk reads the stand-in of the block as opening one: an HTML heading's block only where no
        container holds it."""
        index, _, heading = self.spanning[entry]
        return _opens_block(walked, index, self.read[index]) and (not heading or index in walked.headings)

    def apply(self, entry: int, block: bool, walked: Walked, upto: int, cover: int = -1) -> None:
        """Read the block's lines after its first as the block's, up to the line `upto`, or as they are, but for those
        up to `cover`, the last line of an earlier block read as one, which are that block's."""
        index = self.spanning[entry][0]
        filled = self.filled[entry]
        self.read[max(index, cover) + 1 : filled + 1] = self.initial[max(index, cover) + 1 : filled + 1]
        self.filled[entry] = index
        self.applied[entry] = block
        self.filled_contained[entry] = block and index not in walked.headings
        if block:
            self._fill(entry, upto)

    def _fill(self, entry: int, upto: int) -> None:
        """Read the block's lines up to `upto`, past those read so far, as the block's."""
        heading = self.spanning[entry][2]
        for later in range(self.filled[entry] + 1, min(upto, self.lasts[entry]) + 1):
            quotes = QUOTE_PREFIX.match(self.lines[later])
            held = "" if quotes is None else quotes.group()
            rest = self.lines[later][len(held) :]
            indent = rest[: len(rest) - len(rest.lstrip(" \t"))]
            if heading:
                self.read[later] = held + indent + HTML_TEXT
            elif self.filled_contained[entry] and not is_blank(rest):
                self.read[later] = held + indent + HEADING_STAND_IN
            else:
                self.read[later] = held
        self.filled[entry] = max(self.filled[entry], min(upto, self.lasts[entry]))

    def settle(self, walked: Walked, stop: int) -> tuple[bool, int]:
        """Settle the blocks the walk confirms, in order, then correct the first it does not and read each later one
        in the window as the walk judged it. Returns whether it corrected one, and the furthest line a later walk may
        start from, or -1 when none."""
        cover, restart = self.cover, -1
        headings = sorted(walked.headings)
        for entry in range(self.settled, len(self.spanning)):
            index = self.spanning[entry][0]
            if index >= stop:
                break
            block = index > cover and self.judge(walked, entry)
            applied = block and self.applied[entry]
            # A block read as one whose lines were read for another place than the walk shows is read again.
            refill = applied and self.filled_contained[entry] != (index not in walked.headings)
            left = self._left(walked, entry, stop) if applied and not refill else None
            if applied and not refill and left is None and self.filled[entry] < min(self.lasts[entry], stop - 1):
                # The walk read lines of the block past those read as its own, so a later walk reads them as its.
                self._fill(entry, 2 * stop - index)
                return True, restart
            if applied and not refill and left is None and self._contained(walked, entry) and self.lasts[entry] >= stop:
                # The walk stops before the block's last line with its container holding every line it read, so a
                # later walk, its window doubled, judges the block.
                break
            if block == self.applied[entry] and left is None and not refill:
                restart = max(restart, self._settle(walked, entry, headings))
                cover = self.cover
                continue
            if left is not None:
                # The container stopped holding the comment's lines, which ends its HTML block, as CommonMark ends it,
                # and no container holds the line it stopped at, so a later walk may start there.
                filled = self.filled[entry]
                self.read[left : filled + 1] = self.initial[left : filled + 1]
                self.lasts[entry] = left - 1
                self.filled[entry] = min(filled, left - 1)
                restart = max(restart, left)
            else:
                self.apply(entry, block, walked, stop - 1)
            if block and left is None and self._contained(walked, entry):
                # The next walk shows whether the container holds the block's lines, so it stays unsettled.
                self._guess(walked, entry, stop)
                return True, restart
            restart = max(restart, self._settle(walked, entry, headings))
            self._guess(walked, entry, stop)
            return True, restart
        return False, restart

    def _guess(self, walked: Walked, corrected: int, stop: int) -> None:
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
                self.apply(later, guess, walked, stop - 1, cover)
            if guess:
                cover = max(cover, self.lasts[later])
                extent = (index, self.lasts[later])

    def _contained(self, walked: Walked, entry: int) -> bool:
        """Whether the block is a comment's that a container holds, whose lines the container may stop holding."""
        return not self.spanning[entry][2] and self.spanning[entry][0] not in walked.headings

    def _left(self, walked: Walked, entry: int, stop: int) -> int | None:
        """The first line of a comment's block, read as one inside a container, that the container no longer holds,
        among the lines the walk read."""
        index = self.spanning[entry][0]
        if not self._contained(walked, entry):
            return None
        last = min(self.lasts[entry], self.filled[entry], stop - 1)
        return next((line for line in range(index + 1, last + 1) if line in walked.headings), None)

    def _settle(self, walked: Walked, entry: int, headings: list[int]) -> int:
        """Settle the block; the line a later walk may start from, where the walk's reading of the lines above
        changes nothing below, or -1: past a block read as one that no container holds, or that only blockquotes hold,
        whose lines below reopen them by their own markers, or else past the last heading no container holds above
        it, `headings` holding the walk's in order."""
        self.settled = entry + 1
        index = self.spanning[entry][0]
        if self.applied[entry]:
            self.cover = max(self.cover, self.lasts[entry])
            if index in walked.headings or QUOTED_OPENER.fullmatch(self.read[index]):
                return self.lasts[entry] + 1
        above = bisect_left(headings, index)
        return headings[above - 1] + 1 if above else -1


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
    text: str, closers: list[int], lines: list[str], starts: list[int], walked: Walked, raw: set[int]
) -> list[tuple[int, int]]:
    """The span of each comment GitHub hides, read left to right: one whose `<!--` opens outside every code block and
    code span, up to its `-->`. A comment within a block's text ends at a `-->` within that block, or is text; one on
    a line GitHub passes on as HTML, a comment block's first or last, runs to its `-->` wherever that falls, or to the
    end of the body."""
    hidden: list[tuple[int, int]] = []
    position = 0
    # The code spans of the line the last `<!--` was on, which `read` holds, read from the column the line's reading
    # last started at, and the next of them, its columns counted from the body's start.
    reader = CodeSpanReader("")
    spans: Iterator[CodeSpan] = iter(())
    span: tuple[int, int] | None = None
    read = -1
    ends = _block_ends(walked, len(lines))
    while (opener := text.find(COMMENT_OPENER, position)) != -1:
        line = line_index(starts, opener)
        if read != line or (span is not None and span[0] < position):
            # A comment hidden past the next span's start sets the line's backticks before its end aside, so the spans
            # after it pair as CommonMark pairs them from there.
            if read != line:
                reader, read = CodeSpanReader(lines[line]), line
            spans = reader.spans(max(position, starts[line]) - starts[line])
            span = _next_span(spans, starts[line])
        while span is not None and span[1] <= opener:
            span = _next_span(spans, starts[line])
        if span is not None and span[0] < opener and line not in raw:
            # A code span that holds the `<!--` shows it, and every character up to the span's end, but on a line GitHub
            # passes on as HTML, where no backtick opens a span.
            position = span[1]
            continue
        end = _comment_end(text, closers, opener)
        last = ends[line]
        # A comment within a block's text that no `-->` closes is text, even where the block ends with the body, as in
        # a body with no line break after its last line.
        within = end <= starts[last] + len(lines[last]) and text.endswith(COMMENT_CLOSER, opener, end)
        if line in walked.code or (line not in raw and not within):
            position = opener + 1
            continue
        hidden.append((opener, end))
        position = end
    return hidden


def _next_span(spans: Iterator[CodeSpan], line_start: int) -> tuple[int, int] | None:
    """The next of a line's code spans, as the columns in the body where it starts and just past where it ends, or
    None past the last."""
    span = next(spans, None)
    return None if span is None else (line_start + span.start, line_start + span.end)


def _block_ends(walked: Walked, count: int) -> list[int]:
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
    reading = read_comments(text)
    return reading.visible(0, len(text))


def hidden_spans(text: str) -> list[tuple[int, int]]:
    """Each comment GitHub hides in `text`, in order, as the index of its `<!--` and the index just past it, the spans
    `without_hidden_comments` removes."""
    if COMMENT_OPENER not in text:
        return []
    return list(read_comments(text).hidden)

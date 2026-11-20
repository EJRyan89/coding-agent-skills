"""The Markdown readers shared by skills, repository tools, the deployer, and validation: the second-level section
reader, the fenced code block reader, the block walker, the labels of a fence in an executable language, the inline
code span patterns, the HTML comment pattern, and the block rules every reader of Markdown's structure reads as
CommonMark does: the whitespace of block structure, list markers, and which list item may interrupt a paragraph."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from text_lines import LINE_BREAK

# The whitespace of Markdown's block structure: CommonMark reads only the space and the tab as indentation, as the
# separator after a list marker, and as what a blank line holds, so every other Unicode space is text. A reader of
# block structure strips this, never everything `str.strip` takes.
MARKDOWN_SPACE = " \t"
# What a line read for block structure may end with beside that whitespace: the carriage return of a CRLF line a caller
# split at LF alone.
LINE_END = MARKDOWN_SPACE + "\r"
# The most columns of indentation a block marker below a paragraph may have: four or more make the line the paragraph's
# continuation, never a list item.
MAX_BLOCK_INDENT = 3
# The columns of indentation past what holds it that make a line no paragraph continues a line of an indented code
# block.
CODE_INDENT = MAX_BLOCK_INDENT + 1
# A list marker as CommonMark reads one: a bullet, or one to nine ASCII digits and `.` or `)`. `\d` would admit every
# Unicode digit, and an unbounded count, so a line Markdown shows as prose would be read as a list item.
BULLET_MARKER = r"[-*+]"
ORDERED_MARKER = r"[0-9]{1,9}[.)]"
LIST_MARKER = rf"(?:{BULLET_MARKER}|{ORDERED_MARKER})"
# The list marker that may interrupt a paragraph: CommonMark lets a list's first item interrupt one only when it is a
# bullet or an ordered marker numbered 1, and only with text after it; any other ordered line continues the paragraph.
INTERRUPTING_LIST_MARKER = rf"(?:{BULLET_MARKER}|0{{0,8}}1[.)])"
# A line that opens a list item: any indentation, the marker, then a space or a tab before its text.
LIST_ITEM = re.compile(rf"[ \t]*(?P<marker>{LIST_MARKER})[ \t]+(?P<text>.*)")
# A thematic break, matched in full against a line: up to three spaces, then three or more of one of `-`, `*`, or `_`,
# with spaces or tabs between. A line of bullets spaced apart is one rather than a list item.
THEMATIC_BREAK = re.compile(r" {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*")
# A line that is an ATX heading with text, as CommonMark reads one: up to three spaces of indentation, one to six `#`
# as its level, a space or a tab, then its text, less a closing run of `#` after a space or a tab. `\s` would admit
# every Unicode space, so a line Markdown shows as prose, such as `#` and a no-break space, would be read as a heading.
ATX_HEADING = re.compile(r" {0,3}(?P<marks>#{1,6})[ \t]+(?P<text>.*?)(?:[ \t]+#+)?[ \t]*$")
# An ATX heading with no text: up to three spaces of indentation and one to six `#`, with nothing after them but spaces
# or tabs. It ends a paragraph as any heading does, though ATX_HEADING, which reads a heading's text, matches none.
EMPTY_ATX_HEADING = re.compile(r" {0,3}#{1,6}[ \t]*")
# An inline code span on one line, with its text as the first group, as a skill reads commands or cited paths from it.
CODE_SPAN = re.compile(r"`([^`\n]+)`")
# An inline code span as CommonMark reads one: a run of backticks, its text, and a run of the same length, with the run
# as the first group and the text as the second. A reader that must not take a span's text for prose, such as a link
# or a name a document cites, reads spans by it, since a span written with two backticks may hold one.
COMMONMARK_CODE_SPAN = re.compile(r"(`+)(.+?)\1")
# An HTML comment, which Markdown renders as nothing, across lines.
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# A fence's opening line: the blockquote and list item markers that hold it, any indentation, a run of three or more
# backticks or tildes, and its info string. Whitespace is read once, after each marker, so no run of it can be split
# between two markers and the match stays linear.
FENCE_OPENER = re.compile(
    rf"(?P<container>[ \t]*(?:(?:>|{LIST_MARKER}[ \t])[ \t]*)*)(?P<marker>`{{3,}}|~{{3,}})(?P<info>.*)"
)
QUOTE_MARKER = re.compile(r"[ \t]*>[ \t]?")
# Every label a fence of commands or a program may carry, by the language it names: the POSIX shells, fish, and cmd,
# whose lines the model runs through its Bash or PowerShell tool, PowerShell, and Python, JavaScript, and TypeScript,
# each under every name Markdown gives it. The renderer substitutes a token by these languages, validation holds every
# such fence to the five-line and output-placeholder rules, and the audit-skill audit reads the shells' fences as
# commands: a Bash fence, and an untagged one that is Bash, through the Bash grammar, and each line's first word of any
# other shell's, fish's, cmd's, and PowerShell's, after a `$ ` or `> ` prompt; a console fence that holds a prompt is
# a session, read on the lines audit-skill's session_commands gives, without their prompt, and one with none is read
# like the others.
FENCE_LANGUAGES: dict[str, frozenset[str]] = {
    "shell": frozenset({"bash", "sh", "shell", "console", "zsh", "ksh", "dash"}),
    "fish": frozenset({"fish"}),
    "cmd": frozenset({"cmd", "bat", "batch"}),
    "powershell": frozenset({"powershell", "pwsh", "ps1"}),
    "python": frozenset({"python", "python3", "py"}),
    "javascript": frozenset({"javascript", "js", "node", "mjs", "cjs"}),
    "typescript": frozenset({"typescript", "ts"}),
}
# The labels of a fence whose lines are commands for a command shell, and for PowerShell.
SHELL_FENCES = FENCE_LANGUAGES["shell"] | FENCE_LANGUAGES["fish"] | FENCE_LANGUAGES["cmd"]
POWERSHELL_FENCES = FENCE_LANGUAGES["powershell"]
EXECUTABLE_FENCES = frozenset(label for labels in FENCE_LANGUAGES.values() for label in labels)


@dataclass(frozen=True)
class Fence:
    """A fenced code block: its opening line's index, its closing line's (None when unclosed), its info string's
    first word, the index just past its last body line, how many blockquotes hold it, and the columns before its run
    of backticks or tildes once those are taken off, the indentation of the fence and of the list items that hold it."""

    opener: int
    closer: int | None
    language: str
    stop: int
    quotes: int = 0
    indent: int = 0

    def body(self) -> range:
        """The indexes of the lines between the opener and the closer, or the end of what holds an unclosed fence."""
        return range(self.opener + 1, self.stop)

    def lines(self) -> range:
        """The indexes of the opener, the body, and the closer."""
        return range(self.opener, self.stop if self.closer is None else self.closer + 1)

    def text(self, line: str) -> str:
        """A line of the fence without the blockquote markers that hold it."""
        for _ in range(self.quotes):
            marker = QUOTE_MARKER.match(line)
            if marker is None:
                break
            line = line[marker.end() :]
        return line

    def content(self, line: str) -> str:
        """A body line as CommonMark gives it to the fence's content: without the blockquote markers that hold it, and
        without up to `indent` columns of the spaces and tabs that start it, which the fence's own indentation and the
        list items that hold it take, so an indented fence's here-document delimiter closes its body. A tab counts to
        the next multiple of four, and one that runs past `indent` gives the columns past it as spaces, as CommonMark
        gives a partly consumed tab. Inside a blockquote, a tab is counted from the end of the markers, not the line's
        column, and a tab right after `>` is taken whole as the marker's space, where CommonMark takes one column of
        it; no fence here sits in a blockquote."""
        line = self.text(line)
        width = 0
        for index, character in enumerate(line):
            if width >= self.indent or character not in MARKDOWN_SPACE:
                return line[index:]
            following = width + 1 if character == " " else width + 4 - width % 4
            if following > self.indent:
                return " " * (following - self.indent) + line[index + 1 :]
            width = following
        return ""


def is_blank(line: str) -> bool:
    """Whether Markdown reads the line as blank: nothing on it but spaces and tabs."""
    return not line.strip(MARKDOWN_SPACE)


def indentation(line: str) -> int:
    """The columns a line's leading spaces and tabs take, a tab advancing to the next multiple of four, as CommonMark
    counts them."""
    width = 0
    for character in line:
        if character == " ":
            width += 1
        elif character == "\t":
            width += 4 - width % 4
        else:
            break
    return width


def list_item(line: str, *, interrupting: bool = False) -> str | None:
    """The text of the list item the line opens, or None when Markdown reads no list item there.

    With `interrupting`, the line follows the text of a paragraph that is no list item's, where CommonMark starts a list
    only at a bullet or an ordered marker numbered 1, indented at most MAX_BLOCK_INDENT columns, with text after it, and
    reads any other line as the paragraph's continuation. Without it, any indentation is read, as a list nested in
    another is.
    """
    match = LIST_ITEM.fullmatch(line)
    if match is None:
        return None
    if interrupting and (
        re.fullmatch(INTERRUPTING_LIST_MARKER, match.group("marker")) is None
        or is_blank(match.group("text"))
        or indentation(line) > MAX_BLOCK_INDENT
    ):
        return None
    return match.group("text")


def find_fences(lines: list[str]) -> list[Fence]:
    """Every fenced code block, by CommonMark's rules for the forms Markdown allows.

    A run of three or more backticks or tildes opens a fence, except a backtick run whose info string holds a backtick,
    which is inline code. Indentation is ignored rather than limited to three spaces, and blockquote and list item
    markers before the run are read as what holds it, so a fence nested in a list item or a blockquote counts. Only a
    run of the same character at least as long as the opener's, with nothing after it and nothing before it but
    spaces, tabs, and the blockquote markers that hold the fence, closes it, so a ``` line inside a ```` fence is body,
    and so is one indented with any other Unicode space. The info string's first word, the language, is split as
    commonmark.js splits it, at any whitespace.
    A fence left open runs to the end of the blockquote that holds it, or of the document. Rendering, the extraction
    of Bash for ShellCheck, validation's Markdown policies, and the audit-skill audit all read fences through
    this one function.
    """
    fences: list[Fence] = []
    index = 0
    while index < len(lines):
        match = FENCE_OPENER.fullmatch(lines[index].rstrip(LINE_END))
        if match is None or (match.group("marker")[0] == "`" and "`" in match.group("info")):
            index += 1
            continue
        fence = _fence_from(lines, index, match)
        fences.append(fence)
        index = fence.lines().stop
    return fences


def _fence_from(lines: list[str], opener: int, match: re.Match[str]) -> Fence:
    """The fence that opens at the opener: closed by a matching run, ended by the end of its blockquote, or open."""
    marker, quotes = match.group("marker"), match.group("container").count(">")
    words = match.group("info").split()
    language = words[0] if words else ""
    unquoted = Fence(opener, None, language, opener, quotes).text(lines[opener])
    # A list item's marker takes a column for each of its characters, and a space or a tab as indentation counts it.
    before = unquoted[: unquoted.index(marker)]
    indent = indentation(re.sub(r"[^ \t]", " ", before))
    for later in range(opener + 1, len(lines)):
        line = lines[later]
        for _ in range(quotes):
            quote = QUOTE_MARKER.match(line)
            if quote is None:
                return Fence(opener, None, language, later, quotes, indent)
            line = line[quote.end() :]
        closing = line.strip(LINE_END)
        if len(closing) >= len(marker) and set(closing) == {marker[0]}:
            return Fence(opener, later, language, later, quotes, indent)
    return Fence(opener, None, language, len(lines), quotes, indent)


def code_block_lines(lines: list[str]) -> set[int]:
    """The indexes of the lines of every code block, fenced or indented, as CommonMark reads them.

    An indented code block is a run of lines each indented at least CODE_INDENT columns past the content of the list
    items that hold it, with the blank lines between them, starting where no paragraph is open: an indented line after
    a paragraph's text continues the paragraph, while one after a heading or a thematic break opens code. How list
    items, blockquotes, and paragraphs hold a line, and the limits of that reading, are `_read_blocks`'."""
    return _read_blocks(lines)[0]


def document_headings(lines: list[str]) -> list[tuple[int, re.Match[str]]]:
    """Each ATX heading at the document's top level, as its line's index and its ATX_HEADING match, in document order.

    A heading-shaped line inside a code block, fenced or indented, is the block's text, and a heading inside a list
    item or a blockquote is part of what holds it, as GitHub renders it, so none of them is here: a reader that ends a
    section at the next heading reads past them. A list item holds a heading indented as far as its content, written
    on the item's lines or after a lazy continuation of its paragraph; one indented less, or after a blank line that
    ends the list, is the document's. How list items and blockquotes hold a line, and the limits of that reading, are
    `_read_blocks`'."""
    return _read_blocks(lines)[1]


def _read_blocks(lines: list[str]) -> tuple[set[int], list[tuple[int, re.Match[str]]]]:
    """The lines of every code block, and each ATX heading no list item or blockquote holds, read in one pass.

    A list item holds the lines indented as far as its content, which starts after its marker and the one to four
    spaces that follow it, or one space when more follow, since the text then opens with indented code on the item's
    own line. A line indented less leaves the item after a blank line or any block but a paragraph, and so does a
    heading, a thematic break, a fence, or a list item that interrupts the paragraph, while any other line lazily
    continues it. A block marker is read at most MAX_BLOCK_INDENT columns past the content of the items that hold it,
    and a fence's opener that opens a list item, as `- ```` does, holds the lines after it in that item.
    Blockquote markers are taken off a line first, and a change in how many hold it starts a new container, but for a
    line that lazily continues a quoted paragraph: one that holds text and starts no block. The limits: the partial tab
    a blockquote or list marker consumes is counted as a whole one; a fence's opener opens one list item at most; an
    HTML block or a setext heading is read as a paragraph, so a heading-shaped line inside an HTML comment is read as a
    heading; a blockquote inside a list item closes the item for the lines after it, so a heading or indented line
    that CommonMark keeps in the item is read at the document's level; a list marker with nothing after it on its
    line opens no item, so the line after it is read as the document's; and an ordered marker not numbered 1, or a
    bare marker, right after a quoted line is read as the quoted paragraph's lazy continuation, where CommonMark's
    reference parsers end the quote and open a list."""
    fences = find_fences(lines)
    openers = {fence.opener for fence in fences}
    fenced = {index for fence in fences for index in fence.lines()}
    blocks = _Blocks()
    for index, quoted in enumerate(lines):
        line, quotes = _unquoted(quoted)
        if quotes != blocks.depth:
            if blocks.paragraph and quotes < blocks.depth and _continues(line, opener=index in openers):
                continue  # a lazy continuation of the quoted paragraph
            blocks.items, blocks.depth, blocks.paragraph, blocks.run = [], quotes, False, []
        if index in fenced:
            blocks.fence_line(line, opener=index in openers)
        elif is_blank(line):
            blocks.paragraph = False
            if blocks.run:
                blocks.run.append(index)
        elif not blocks.code_line(index, line):
            blocks.block_line(index, line)
    return fenced | blocks.code, blocks.headings


@dataclass
class _Blocks:
    """What `_read_blocks` has read so far: the column each open list item's content starts at, whether a paragraph is
    open, the indented code run and the blank lines after it, how many blockquotes hold the line, the code lines, and
    the document's headings."""

    items: list[int] = field(default_factory=list)
    paragraph: bool = False
    run: list[int] = field(default_factory=list)
    depth: int = 0
    code: set[int] = field(default_factory=set)
    headings: list[tuple[int, re.Match[str]]] = field(default_factory=list)

    def fence_line(self, line: str, *, opener: bool) -> None:
        """A fence's line: its opener ends the items indented past it, and opens one when a list marker holds it."""
        if opener:
            width = indentation(line)
            del self.items[items_holding(self.items, width) :]
            item = _item_content(line, interrupting=False, relative=width - (self.items[-1] if self.items else 0))
            if item is not None:
                self.items.append(item[0])
        self.paragraph, self.run = False, []

    def code_line(self, index: int, line: str) -> bool:
        """Whether a line that continues no paragraph is indented code, read after the items it leaves are closed."""
        if self.paragraph:
            return False
        width = indentation(line)
        del self.items[items_holding(self.items, width) :]
        if width < (self.items[-1] if self.items else 0) + CODE_INDENT:
            return False
        self.code.update(self.run)
        self.code.add(index)
        self.run = [index]
        return True

    def block_line(self, index: int, line: str) -> None:
        """A line that is no code: a thematic break, a list item, or a heading, each ending the items indented past
        it, or a paragraph's text."""
        self.run = []
        width = indentation(line)
        held = items_holding(self.items, width)
        relative = width - (self.items[held - 1] if held else 0)
        block = line.strip(LINE_END) if relative <= MAX_BLOCK_INDENT else ""
        item = _item_content(line, interrupting=self.paragraph, relative=relative)
        heading = ATX_HEADING.fullmatch(block)
        if THEMATIC_BREAK.fullmatch(block) or item is not None or heading is not None:
            del self.items[held:]
            self.paragraph = False
        else:
            self.paragraph = True
        if THEMATIC_BREAK.fullmatch(block):
            return
        if item is not None:
            content, text, opens_code = item
            self.items.append(content)
            self.paragraph = not (opens_code or is_blank(text))
            if opens_code:
                self.code.add(index)
                self.run = [index]
        elif heading is not None and not self.items and not self.depth:
            self.headings.append((index, heading))


def _continues(line: str, *, opener: bool) -> bool:
    """Whether a line, its blockquote markers taken off, may lazily continue a paragraph: it holds text and starts no
    block, which would end the paragraph and what holds it."""
    return not (is_blank(line) or starts_block(line, opener=opener))


def starts_block(line: str, *, opener: bool) -> bool:
    """Whether a line after a paragraph's text, read at the column of the content that holds it, starts a block, which
    ends the paragraph, rather than lazily continuing it: a fence, whose opener `find_fences` reads and the caller
    names with `opener`, a blockquote marker, an ATX heading, or a thematic break, each indented at most
    MAX_BLOCK_INDENT columns, an empty ATX heading among them, or a list item that may interrupt a paragraph."""
    block = line.strip(LINE_END) if indentation(line) <= MAX_BLOCK_INDENT else ""
    return bool(
        opener
        or block.startswith(">")
        or THEMATIC_BREAK.fullmatch(block)
        or starts_heading(block)
        or list_item(line, interrupting=True) is not None
    )


def starts_heading(line: str) -> bool:
    """Whether a line is an ATX heading, with text as ATX_HEADING reads one or empty, `#` to `######` alone."""
    return ATX_HEADING.fullmatch(line) is not None or EMPTY_ATX_HEADING.fullmatch(line) is not None


def items_holding(items: list[int], width: int) -> int:
    """How many of the open list items, by the column each one's content starts at, outermost first, hold a line
    indented `width` columns."""
    held = 0
    while held < len(items) and items[held] <= width:
        held += 1
    return held


def _unquoted(line: str) -> tuple[str, int]:
    """The line without the blockquote markers that open it, each indented at most MAX_BLOCK_INDENT columns, and how
    many there were."""
    quotes = 0
    while indentation(line) <= MAX_BLOCK_INDENT and (marker := QUOTE_MARKER.match(line)) is not None:
        line, quotes = line[marker.end() :], quotes + 1
    return line, quotes


def list_item_content(line: str) -> int | None:
    """The column the content of the list item a line opens starts at, as `_read_blocks` reads it, or None when the
    line opens no list item. Whether the item may interrupt a paragraph there is `list_item`'s."""
    item = _item_content(line, interrupting=False, relative=0)
    return None if item is None else item[0]


def _item_content(line: str, *, interrupting: bool, relative: int) -> tuple[int, str, bool] | None:
    """For a line that opens a list item as `list_item` reads it, indented `relative` columns past the content of the
    items that hold it, the column the item's content starts at, its text, and whether that text opens an indented
    code block, as it does after more than CODE_INDENT columns of space; None for any other line."""
    match = LIST_ITEM.fullmatch(line)
    if match is None or list_item(" " * relative + line.lstrip(MARKDOWN_SPACE), interrupting=interrupting) is None:
        return None
    after = _columns(line[: match.end("marker")])
    spaces = _columns(line[: match.start("text")]) - after
    opens_code = spaces > CODE_INDENT and not is_blank(match.group("text"))
    return after + (1 if opens_code else spaces), match.group("text"), opens_code


def _columns(text: str) -> int:
    """The column `text` ends at, a tab advancing to the next multiple of four, as CommonMark counts them."""
    width = 0
    for character in text:
        width += 4 - width % 4 if character == "\t" else 1
    return width


# The kinds of block `blocks` yields.
PARAGRAPH = "paragraph"
LIST_ITEM_BLOCK = "item"
QUOTE = "quote"
TABLE_ROW = "table row"
HEADING = "heading"
CODE = "code"
# The kinds that hold prose: what a reader of a document's statements reads, leaving out code, headings, and tables.
PROSE_BLOCKS = frozenset({PARAGRAPH, LIST_ITEM_BLOCK, QUOTE})


@dataclass(frozen=True)
class Block:
    """A block of a Markdown document: the index of its first line, its kind, and its lines as written. A code block's
    lines are its content: a fenced block's body without the blockquote markers that hold it, an indented block's lines
    as they are."""

    start: int
    kind: str
    lines: tuple[str, ...]

    def text(self, *, marker: bool = True) -> str:
        """The block's lines without the spaces and tabs around each, joined by a space; without `marker`, a list
        item's text starts after its marker."""
        lines = list(self.lines)
        if not marker and self.kind == LIST_ITEM_BLOCK:
            lines[0] = list_item(lines[0]) or ""
        return " ".join(line.strip(MARKDOWN_SPACE) for line in lines)


def blocks(lines: list[str]) -> Iterator[Block]:
    """Each block of a Markdown document in order, as GitHub renders it: a paragraph, a list item, a blockquote, a
    table row, an ATX heading, or a code block, fenced or indented. A thematic break and a blank line end a block and
    are none. Each reader of a document's blocks walks them here and chooses after the walk what it keeps and how it
    reads a block's lines, so no two readers split a document apart.

    A list item stays open across blank lines for the lines indented as far as its content, as `_read_blocks` reads
    it, and each line is read at the column of the content of the items that hold it. A line after a paragraph's text
    continues it unless `starts_block` reads it, at that column, as a block of its own: a heading, a thematic break,
    a blockquote, or a list item, which starts a list inside the paragraph's item, or the document, only at a bullet
    or `1.`, while a marker that leaves an open item opens that list's next item, whatever its number. A table row is
    a block of its own read the same way. A list item's further lines, indented or lazy, join it, and a blockquote's
    quoted and lazy lines join it. A heading or a table row is one line; `#` opens a heading only as `starts_heading`
    reads one, so `#hashtag` is text. Code, fenced or indented, is read through `find_fences` and `code_block_lines`.

    The limits: a setext heading is read as a paragraph, so the text above a `---` underline is a paragraph the
    thematic break ends and an `=` underline joins the text above it; an HTML block is read as a paragraph, so its
    lines are paragraph text until one that starts a block, such as a heading-shaped or list line, which is read as
    that block; a line that starts with `|` is a table row even where it follows a paragraph's text, a line right
    after a table's rows is read as a paragraph, where GitHub reads it as another row, and a table with no leading
    pipe is read as a paragraph; an ordered marker not numbered 1, or a bare marker, right after a quoted line
    continues the quote, where CommonMark ends the quote and opens a list; a list marker with nothing after it on its
    line opens no item, so it and the lines after it are read as a paragraph; a list marker more than
    MAX_BLOCK_INDENT columns past the content of an item it leaves opens an item, where CommonMark continues that
    item's paragraph; and a quote's own lines are not split into the blocks it holds."""
    fences = {fence.opener: fence for fence in find_fences(lines)}
    code = code_block_lines(lines)
    walk = _Walk()
    index = 0
    while index < len(lines):
        fence = fences.get(index)
        if fence is not None or index in code:
            yield from walk.close()
            if fence is not None:
                walk.fence(lines[index])
            stop = fence.lines().stop if fence is not None else _code_run_end(lines, index, code, fences)
            body = [fence.text(lines[inside]) for inside in fence.body()] if fence is not None else lines[index:stop]
            yield Block(fence.opener + 1 if fence is not None else index, CODE, tuple(body))
            index = stop
            continue
        yield from walk.line(index, lines[index])
        index += 1
    yield from walk.close()


def _code_run_end(lines: list[str], start: int, code: set[int], fences: dict[int, Fence]) -> int:
    """The index past an indented code block that starts at `start`: past its last line, short of a fence."""
    stop = start + 1
    while stop < len(lines) and stop in code and stop not in fences:
        stop += 1
    return stop


@dataclass
class _Walk:
    """What `blocks` has read of the lines no code holds: the open block's first line, kind, and lines, and the column
    each open list item's content starts at, outermost first."""

    start: int = 0
    kind: str = ""
    open: list[str] = field(default_factory=list)
    items: list[int] = field(default_factory=list)

    def close(self) -> Iterator[Block]:
        """The open block, if any, which ends here; the list items that hold it stay open."""
        if self.open:
            yield Block(self.start, self.kind, tuple(self.open))
        self.open = []

    def fence(self, line: str) -> None:
        """A fence's opener: it ends the items indented past it, and opens one when a list marker holds it."""
        width = indentation(line)
        del self.items[items_holding(self.items, width) :]
        item = _item_content(line, interrupting=False, relative=width - (self.items[-1] if self.items else 0))
        if item is not None:
            self.items.append(item[0])

    def line(self, index: int, line: str) -> Iterator[Block]:
        """A line no code holds: one that ends the open block, opens one, or continues it."""
        stripped = line.strip(MARKDOWN_SPACE)
        if not stripped:
            yield from self.close()
            return
        width = indentation(line)
        held = items_holding(self.items, width)
        relative = width - (self.items[held - 1] if held else 0)
        # The line as the content that holds it reads it: indented only past that content's column.
        at = " " * relative + line.lstrip(MARKDOWN_SPACE)
        continued, quoted = bool(self.open), self.kind == QUOTE
        single = not continued or relative <= MAX_BLOCK_INDENT
        item = _item_content(line, interrupting=continued and held == len(self.items), relative=relative)
        if THEMATIC_BREAK.fullmatch(at):
            yield from self.close()
            del self.items[held:]
        elif single and (starts_heading(stripped) or stripped.startswith("|")):
            yield from self.close()
            del self.items[held:]
            yield Block(index, HEADING if starts_heading(stripped) else TABLE_ROW, (line,))
        elif item is not None or not continued or (starts_block(at, opener=False) and not quoted):
            yield from self.close()
            del self.items[held:]
            if item is not None:
                self.items.append(item[0])
            self.start, self.open = index, [line]
            self.kind = LIST_ITEM_BLOCK if item is not None else QUOTE if stripped.startswith(">") else PARAGRAPH
        else:
            self.open.append(line)


def prose_code_spans(lines: list[str]) -> list[tuple[int, str]]:
    """Each inline code span outside every fence, as its line's index and its text."""
    fenced = {index for fence in find_fences(lines) for index in fence.lines()}
    return [
        (index, span) for index, line in enumerate(lines) if index not in fenced for span in CODE_SPAN.findall(line)
    ]


def markdown_section(text: str, heading: str) -> str | None:
    """The lines under the first exact heading, ignoring its trailing spaces and tabs, up to the next `## ` heading.

    A line inside a fenced code block is the block's text, as GitHub renders it, so it is neither the heading nor the
    section's end: a section whose body holds a fenced `## ` line runs past it. Its lines end where CommonMark ends
    one, at CR, LF, or CRLF, and each becomes LF; body whitespace and comments are otherwise preserved. None means
    the heading is absent.
    """
    lines = LINE_BREAK.split(text)
    fenced = {index for fence in find_fences(lines) for index in fence.lines()}
    start = next(
        (index + 1 for index, line in enumerate(lines) if index not in fenced and line.rstrip(LINE_END) == heading),
        None,
    )
    if start is None:
        return None
    end = next(
        (index for index in range(start, len(lines)) if index not in fenced and lines[index].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end])

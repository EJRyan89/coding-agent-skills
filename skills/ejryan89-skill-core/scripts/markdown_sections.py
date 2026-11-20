"""The Markdown readers shared by skills, repository tools, the deployer, and validation: the second-level section
reader, the fenced code block reader, the block walker, the labels of a fence in an executable language, the inline
code span patterns, the HTML comment pattern, and the block rules every reader of Markdown's structure reads as
CommonMark does: the whitespace of block structure, list markers, and which list item may interrupt a paragraph."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field, replace

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
    items, blockquotes, and paragraphs hold a line, and the limits of that reading, are `_walk`'s, the one walk
    `blocks` and `document_headings` read too."""
    return _walk(lines).code


def document_headings(lines: list[str]) -> list[tuple[int, re.Match[str]]]:
    """Each ATX heading at the document's top level, as its line's index and its ATX_HEADING match, in document order.

    A heading-shaped line inside a code block, fenced or indented, is the block's text, and a heading inside a list
    item or a blockquote is part of what holds it, as GitHub renders it, so none of them is here: a reader that ends a
    section at the next heading reads past them. A list item holds a heading indented as far as its content, written
    on the item's lines or after a lazy continuation of its paragraph; one indented less, or after a blank line that
    ends the list, is the document's. How list items and blockquotes hold a line, and the limits of that reading, are
    `_walk`'s, the one walk `blocks` and `code_block_lines` read too."""
    return _walk(lines).headings


def _starts_leaving(line: str, start: int, *, opener: bool) -> bool:
    """Whether a line that an open container does not hold, read at the column `start` the content of the ones that
    hold it starts at, starts a block, which closes the others, rather than lazily continuing their paragraph: a block
    `starts_block` reads, or a list item indented at most MAX_BLOCK_INDENT columns past that content, whatever its
    number, since the paragraph is not in the container the item would open in, as CommonMark reads it."""
    relative = indentation(line) - start
    at = " " * max(relative, 0) + line.lstrip(MARKDOWN_SPACE)
    return starts_block(at, opener=opener) or (relative <= MAX_BLOCK_INDENT and list_item(at) is not None)


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


def list_item_content(line: str) -> int | None:
    """The column the content of the list item a line opens starts at, as `_walk` reads it, or None when the
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

    The blocks come from `_walk`, the one walk `code_block_lines` and `document_headings` read too, so a line each
    reader holds in a list item or a blockquote, or reads as code, is held and read alike here. A line after a
    paragraph's text continues it unless it starts a block of its own at the column of the content that holds it: a
    heading, a thematic break, a blockquote, or a list item, which starts a list inside the paragraph's container, or
    the document, only at a bullet or `1.`, while a marker that leaves an open item or blockquote opens an item,
    whatever its number, and a marker more than MAX_BLOCK_INDENT columns past that content opens none. A table row is
    a block of its own read the same way. A list item's further lines, indented or lazy, join it, and so does the first
    line it holds when its marker has only spaces after it; every line a blockquote holds joins it, lazy ones too. A
    heading or a table row is one line; `#` opens a heading only as `starts_heading` reads one, so `#hashtag` is text.

    The limits, beside `_walk`'s: a setext heading is read as a paragraph, so the text above a `---` underline is a
    paragraph the thematic break ends and an `=` underline joins the text above it; an HTML block is read as a
    paragraph, so its lines are paragraph text until one that starts a block, such as a heading-shaped or list line,
    which is read as that block; a line that starts with `|` is a table row even where it follows a paragraph's text in
    the same container, though one that lazily continues a paragraph is that paragraph's text, a line right after a
    table's rows is read as a paragraph, where GitHub reads it as another row, and a table with no leading pipe is read
    as a paragraph; and a quote's own lines are not split into the blocks it holds, though a code block inside it is a
    block of its own and ends the quote's."""
    yield from _walk(lines).blocks


def _walk(lines: list[str]) -> _Walker:
    """The document read in one pass, holding one stack of open containers, the list items and blockquotes that hold
    the line, outermost first: its blocks, the lines of its code blocks, and each heading no container holds.

    Each line is read as CommonMark reads one. It continues each open container in turn: a blockquote while a `>` marker
    stands at most MAX_BLOCK_INDENT columns past the content that holds it, and a list item while the line is blank or
    indented as far as the item's content, which starts after its marker and the one to four spaces that follow it, or
    one space when more follow, since the text then opens with indented code on the item's own line. When a container
    is not continued, a line that holds text and starts no block lazily continues the open paragraph, and any other
    line closes the containers it left. Then the line opens containers: a blockquote marker, and a list item, which
    interrupts a paragraph its containers all hold only at a bullet or `1.` with text after it, and opens whatever its
    number where the paragraph's container was left; each marker is read at most MAX_BLOCK_INDENT columns past the
    content that holds it. What is left of the line is a fence's opener, indented code where no paragraph is open, a
    heading, a thematic break, a table row, or paragraph text. A list item whose marker has only spaces after it, then a
    blank line, is empty and closes.

    The limits: the partial tab a blockquote or list marker consumes is counted as a whole one; an HTML block or a
    setext heading is read as a paragraph, so a heading-shaped line inside an HTML comment is read as a heading; a list
    marker with nothing after it on its line opens no item, so a bare marker is text, and lazily continues a paragraph
    above it; a table row is read as a paragraph's text for what may follow it, so a list interrupts it only at a
    bullet or `1.`, and a line indented as far as indented code after it continues it rather than opening code; and a
    fence is read where `find_fences` reads one, ignoring indentation, so a line indented as far as indented code
    that opens a fence there opens one here, though the fence ends, as CommonMark ends it, at the first line the
    containers that hold its opener leave, and the lines after that are read afresh."""
    walker = _Walker(lines)
    fences = walker.fences_from(0)
    index = 0
    while index < len(lines):
        fence = fences.get(index)
        index = walker.line(index, fence)
        if fence is not None and index < fence.lines().stop:
            # The fence ended with the containers that hold it, so its closing line, if any, is read afresh.
            fences = {opener: kept for opener, kept in fences.items() if opener < index} | walker.fences_from(index)
    walker.close()
    return walker


# How the container stack names a blockquote, beside the content column of each list item, which is never negative.
QUOTE_CONTAINER = -1


@dataclass
class _Walker:
    """What `_walk` has read so far. The structure: the containers that hold the line, outermost first, as the column
    each list item's content starts at or QUOTE_CONTAINER, whether a paragraph's text is open, and whether the last
    list item opened with nothing after its marker. What it yields: the blocks, the code lines, and the document's
    headings. The open block: its first line, kind, and lines, and the blank lines after an open indented code block,
    which join it only when more code follows."""

    lines: list[str]
    containers: list[int] = field(default_factory=list)
    paragraph: bool = False
    bare: bool = False
    blocks: list[Block] = field(default_factory=list)
    code: set[int] = field(default_factory=set)
    headings: list[tuple[int, re.Match[str]]] = field(default_factory=list)
    first: int = 0
    kind: str = ""
    open: list[str] = field(default_factory=list)
    pending: list[int] = field(default_factory=list)

    def close(self) -> None:
        """The open block, if any, ends here; the containers that hold it stay open."""
        if self.open:
            self.blocks.append(Block(self.first, self.kind, tuple(self.open)))
        self.kind, self.open, self.pending = "", [], []

    def begin(self, index: int, kind: str) -> None:
        """The line opens a block of the kind, ending the open one."""
        self.close()
        self.first, self.kind, self.open = index, kind, [self.lines[index]]

    def held(self, line: str) -> tuple[int, str, int]:
        """How many open containers hold the line, the line with their blockquote markers turned to spaces, and the
        column the content of the innermost of them starts at."""
        start = 0
        for count, container in enumerate(self.containers):
            if container == QUOTE_CONTAINER:
                if indentation(line) - start > MAX_BLOCK_INDENT or not line.lstrip(MARKDOWN_SPACE).startswith(">"):
                    return count, line, start
                line, start = _past_quote_marker(line)
            elif is_blank(line) or indentation(line) >= container:
                start = container
            else:
                return count, line, start
        return len(self.containers), line, start

    def fences_from(self, start: int) -> dict[int, Fence]:
        """Each fence `find_fences` reads in the lines from `start` on, by its opener's index."""
        return {
            fence.opener + start: replace(
                fence,
                opener=fence.opener + start,
                closer=None if fence.closer is None else fence.closer + start,
                stop=fence.stop + start,
            )
            for fence in find_fences(self.lines[start:])
        }

    def fenced(self, fence: Fence) -> Fence:
        """The fence as the containers that hold its opener hold it: it ends at the first line they leave."""
        for index in fence.lines()[1:]:
            if self.held(self.lines[index])[0] < len(self.containers):
                return replace(fence, closer=None, stop=index)
        return fence

    def line(self, index: int, fence: Fence | None) -> int:
        """A line, or a fence's opener with the fence after it: it continues, closes, and opens containers, then is
        read as what is left of it. The index of the line after what was read."""
        count, line, start = self.held(self.lines[index])
        closed = count < len(self.containers)
        if closed:
            if self.paragraph and not is_blank(line) and not _starts_leaving(line, start, opener=fence is not None):
                self.lazy(index)
                return index + 1
            del self.containers[count:]
            self.paragraph, self.bare = False, False
        opened = len(self.containers)
        line, start, text = self.opened(line, start)
        if len(self.containers) > opened:
            self.paragraph = False
        # Whether the line has the containers of the line above, so an indented code block may run on across it.
        same = not closed and len(self.containers) == opened
        if fence is not None:
            fence = self.fenced(fence)
            self.paragraph, self.bare = False, False
            self.code.update(fence.lines())
            self.close()
            self.blocks.append(Block(fence.opener + 1, CODE, tuple(fence.text(self.lines[i]) for i in fence.body())))
        elif text is not None and is_blank(text):
            self.paragraph, self.bare = False, True
            self.emit(index, LIST_ITEM_BLOCK, opened, joins=False)
        elif is_blank(line):
            self.blank(index, opened, same=same)
        else:
            self.leaf(index, line, start, opened, same=same)
        return index + 1 if fence is None else fence.lines().stop

    def lazy(self, index: int) -> None:
        """A line that lazily continues the open paragraph, though a container that holds it does not hold the line."""
        if self.open:
            self.open.append(self.lines[index])
        else:
            self.begin(index, PARAGRAPH)

    def opened(self, line: str, start: int) -> tuple[str, int, str | None]:
        """The containers the line opens, each read past the content that holds it: the line with their markers turned
        to spaces, the column its content starts at, and the text of the last list item it opens, None for none."""
        interrupting, text = self.paragraph, None
        while not is_blank(line) and indentation(line) - start <= MAX_BLOCK_INDENT:
            rest = line.lstrip(MARKDOWN_SPACE)
            if rest.startswith(">"):
                self.containers.append(QUOTE_CONTAINER)
                line, start = _past_quote_marker(line)
            elif THEMATIC_BREAK.fullmatch(rest.rstrip(LINE_END)):
                break
            else:
                item = _item_content(line, interrupting=interrupting, relative=indentation(line) - start)
                if item is None:
                    break
                start, text, _ = item
                self.containers.append(start)
                match = LIST_ITEM.fullmatch(line)
                if match is not None:
                    line = (
                        line[: match.start("marker")] + " " * len(match.group("marker")) + line[match.end("marker") :]
                    )
                if is_blank(text):
                    break
            interrupting = False
        return line, start, text

    def blank(self, index: int, opened: int, *, same: bool) -> None:
        """A line blank once its containers' markers are taken off: an empty list item above it closes, and it ends the
        open block, but an indented code block's, which it may join, and a blockquote's, whose `>` line it joins."""
        if self.bare and self.containers and self.containers[-1] != QUOTE_CONTAINER:
            self.containers.pop()
        self.paragraph, self.bare = False, False
        if self.kind == CODE and same:
            self.pending.append(index)
        elif QUOTE_CONTAINER in self.containers and not is_blank(self.lines[index]):
            self.emit(index, QUOTE, opened, joins=True)
        else:
            self.close()

    def leaf(self, index: int, line: str, start: int, opened: int, *, same: bool) -> None:
        """What is left of a line once its containers are read: indented code, a thematic break, a heading, a table
        row, or paragraph text."""
        relative = indentation(line) - start
        if not self.paragraph and relative >= CODE_INDENT:
            self.bare = False
            if self.kind == CODE and same:
                self.code.update(self.pending)
                self.open.extend(self.lines[blank] for blank in self.pending)
                self.open.append(self.lines[index])
                self.pending = []
            else:
                self.begin(index, CODE)
            self.code.add(index)
            return
        block = line.strip(LINE_END) if relative <= MAX_BLOCK_INDENT else ""
        joins = self.paragraph or (self.bare and same)
        self.bare = False
        if THEMATIC_BREAK.fullmatch(block):
            self.paragraph = False
            self.emit(index, "", opened, joins=False)
        elif starts_heading(block):
            self.paragraph = False
            match = ATX_HEADING.fullmatch(block)
            if match is not None and not self.containers:
                self.headings.append((index, match))
            self.emit(index, HEADING, opened, joins=False)
        else:
            self.paragraph = True
            self.emit(index, TABLE_ROW if block.startswith("|") else PARAGRAPH, opened, joins=joins)

    def emit(self, index: int, kind: str, opened: int, *, joins: bool) -> None:
        """The line's block, given its own kind, or none for a thematic break: a line a blockquote holds joins the
        blockquote's block, unless the blockquote opens on it, a line that opens a list item opens the item's block,
        and any other line opens a block of its kind, or joins the open one when it continues its paragraph."""
        if QUOTE_CONTAINER in self.containers:
            if self.kind == QUOTE and self.containers.index(QUOTE_CONTAINER) < opened:
                self.open.append(self.lines[index])
            else:
                self.begin(index, QUOTE)
        elif len(self.containers) > opened:
            self.begin(index, LIST_ITEM_BLOCK)
        elif kind in {HEADING, TABLE_ROW}:
            self.close()
            self.blocks.append(Block(index, kind, (self.lines[index],)))
        elif not kind:
            self.close()
        elif joins and self.open and self.kind != CODE:
            self.open.append(self.lines[index])
        else:
            self.begin(index, kind)


def _past_quote_marker(line: str) -> tuple[str, int]:
    """The line with its first `>` and the space or tab after it turned to spaces, and the column after them."""
    marker = len(line) - len(line.lstrip(MARKDOWN_SPACE))
    end = marker + 1 + (line[marker + 1 : marker + 2] in {" ", "\t"})
    line = line[:marker] + " " * (end - marker) + line[end:]
    return line, _columns(line[:end])


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

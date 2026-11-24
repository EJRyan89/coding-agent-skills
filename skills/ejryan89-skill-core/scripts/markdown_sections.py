"""The Markdown readers shared by skills, repository tools, the deployer, and validation: the second-level section
reader, the fenced code block reader, the block walker and the readers of the lines its containers hold and of the list
items that hold each line, the labels of a fence in an executable language, the inline code span reader, the HTML
comment pattern, and the block rules every reader of Markdown's structure reads as CommonMark does: the whitespace of
block structure, list markers, and which list item may interrupt a paragraph."""

from __future__ import annotations

import bisect
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
# A list marker where it stands in a line, with the spaces and tabs after it, at least one: matched at an index, so a
# reader of a line's nested markers reads each once.
LIST_MARKER_AT = re.compile(rf"(?P<marker>{LIST_MARKER})[ \t]+")
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
# A run of backticks, which opens or closes an inline code span only whole.
BACKTICK_RUN = re.compile(r"`+")
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
        return _unquoted(line, self.quotes)

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


def _unquoted(line: str, quotes: int) -> str:
    """A line without the first `quotes` blockquote markers it starts with, or as many as it holds when fewer, as a
    lazy line holds none."""
    for _ in range(quotes):
        marker = QUOTE_MARKER.match(line)
        if marker is None:
            break
        line = line[marker.end() :]
    return line


@dataclass(frozen=True, eq=False)
class ListItem:
    """A list item as `_walk` reads it: the index of the line it opens on, its marker as written, such as `-` or `1.`,
    the index in that line past the marker and the spaces after it, where its text starts, how many blockquotes hold
    it, and the innermost list item that holds it, or None. Two items are equal only when they are one, so neither a
    comparison nor a hash reads the items that hold one. The walk keeps the index rather than the text, so a line of
    many nested markers is not copied once for each."""

    start: int
    marker: str
    after: int
    quotes: int = 0
    parent: ListItem | None = field(default=None, repr=False)

    def text(self, line: str) -> str:
        """What follows the item's marker and the spaces after it on `line`, the line it opens on."""
        return line[self.after :]

    def unquoted(self, line: str) -> str:
        """A line the item holds without the blockquote markers that hold the item, so a quote inside the item keeps
        its own `>`."""
        return _unquoted(line, self.quotes)


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


def outside_fences(lines: list[str]) -> list[str]:
    """`lines` with each line of a fenced code block, as `find_fences` places it, made empty, so a reader of the
    document's own headings and fields never reads a fence's text as one. The list keeps its length, so each line keeps
    its number."""
    fenced = {index for fence in find_fences(lines) for index in fence.lines()}
    return ["" if index in fenced else line for index, line in enumerate(lines)]


def find_fences(lines: list[str]) -> list[Fence]:
    """Every fenced code block, in document order, by CommonMark's rules for the forms Markdown allows: the fences of
    `_walk`, the one walk `blocks`, `code_block_lines`, and `document_headings` read too, so every reader of a
    document places a fence alike. Rendering, the extraction of Bash for ShellCheck, validation's Markdown policies, and
    the audit-skill audit all read fences through this one function.

    The list items and blockquotes that hold a line are read first, as `_walk` reads them, so a fence nested in a list
    item or a blockquote counts. A run of three or more backticks or tildes at most MAX_BLOCK_INDENT columns past the
    content that holds it opens a fence, except a backtick run whose info string holds a backtick, which is inline
    code; one indented further is indented code or a paragraph's text. Only a run of the same character at least as
    long as the opener's, as far in, with nothing after it but spaces and tabs, closes it, so a ``` line inside a ````
    fence is body, and so is one indented with any other Unicode space. A fence left open runs to the first line its
    list items or blockquotes do not hold, as a line indented less than its list item's content, or to the end of the
    document. The info string's first word, the language, is split as commonmark.js splits it, at any whitespace.

    The limits are `_walk`'s, which this inherits. Two of them place a fence apart from GitHub: a list marker with
    nothing after it opens no item, so under a bare `-` a run indented four columns is indented code, where GitHub reads
    the item and the fence inside it; and the partial tab a container marker consumes is counted as a whole one, so a
    tab right after a blockquote's `>` puts the line's content at the margin and leaves the list item the fence is in,
    where GitHub keeps the line in the fence.
    """
    return _walk(lines).fences


def _fence_opener(line: str) -> re.Match[str] | None:
    """The FENCE_OPENER match of a line that opens a fence, or None: a backtick run whose info string holds a backtick
    is inline code."""
    match = FENCE_OPENER.fullmatch(line.rstrip(LINE_END))
    if match is None or (match.group("marker")[0] == "`" and "`" in match.group("info")):
        return None
    return match


def _opened_fence(line: str, opener: int, match: re.Match[str]) -> Fence:
    """The fence the line at `opener` opens, as far as its opener: its language, how many blockquotes hold it, and its
    indentation, with no closer and no line past the opener yet."""
    marker, quotes = match.group("marker"), match.group("container").count(">")
    words = match.group("info").split()
    language = words[0] if words else ""
    unquoted = Fence(opener, None, language, opener, quotes).text(line)
    # A list item's marker takes a column for each of its characters, and a space or a tab as indentation counts it.
    before = unquoted[: unquoted.index(marker)]
    return Fence(opener, None, language, opener, quotes, indentation(re.sub(r"[^ \t]", " ", before)))


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


def contained_lines(lines: list[str]) -> set[int]:
    """The indexes of the lines a list item or a blockquote holds, as GitHub renders them: each line that opens one,
    each line the open ones continue, a blank line among them, a fence's lines inside one, and a line that lazily
    continues the paragraph of one though its marker or indentation does not. A reader that asks whether a block is the
    document's own, such as a setext heading's text and underline, asks it here. How list items and blockquotes hold a
    line, and the limits of that reading, are `_walk`'s, the one walk `blocks`, `code_block_lines`, and
    `document_headings` read too, so a line held here is never a heading `document_headings` gives."""
    return _walk(lines).contained


def _starts_leaving(rest: str, column: int, start: int) -> bool:
    """Whether what is left of a line that an open container does not hold, starting at `column`, starts a block at
    the column `start` the content of the containers that hold it starts at, which closes the others, rather than
    lazily continuing their paragraph: a block `starts_block` reads, a fence's opener, or a list item indented at most
    MAX_BLOCK_INDENT columns past that content, whatever its number, since the paragraph is not in the container the
    item would open in, as CommonMark reads it."""
    relative = column + indentation(rest) - start
    at = " " * max(relative, 0) + rest.lstrip(MARKDOWN_SPACE)
    near = relative <= MAX_BLOCK_INDENT
    return starts_block(at, opener=near and _fence_run(rest) is not None) or (near and list_item(at) is not None)


def _fence_run(rest: str) -> re.Match[str] | None:
    """The FENCE_RUN match of what is left of a line once its containers are read, when it opens a fence: a backtick
    run whose info string holds a backtick is inline code."""
    match = FENCE_RUN.fullmatch(rest.strip(LINE_END))
    if match is None or (match.group("marker")[0] == "`" and "`" in match.group("info")):
        return None
    return match


def starts_block(line: str, *, opener: bool) -> bool:
    """Whether a line after a paragraph's text, read at the column of the content that holds it, starts a block, which
    ends the paragraph, rather than lazily continuing it: a fence, whose opener `_walk` reads and the caller names with
    `opener`, a blockquote marker, an ATX heading, or a thematic break, each indented at most
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


@dataclass(frozen=True)
class DocumentItems:
    """A document's list items: each one in the order its line opens it, an item before those it holds, and for each
    line the innermost list item that holds it, or None, whose `parent` gives the items around it."""

    items: list[ListItem]
    holding: list[ListItem | None]


def document_items(lines: list[str]) -> DocumentItems:
    """Every list item of a document and the items that hold each line, as GitHub renders them: an item holds the line
    it opens on, each line it continues, a blank line under it, a fence's lines inside it, and a line that lazily
    continues its paragraph though its indentation does not, while a line that ends it, such as one indented less after
    a blank line or after its indented code, is held by the items around it only. How list items hold a line, and the
    limits of that reading, are `_walk`'s, the one walk `blocks`, `contained_lines`, and `document_headings` read too.
    Each line's item is read once, so the time is linear in the document however deep its items nest."""
    walker = _walk(lines)
    return DocumentItems(walker.items, walker.holding)


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
    reads a block's lines, so no two readers split a document apart. Its fences are `find_fences`'.

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
    the same container, though one that lazily continues a paragraph is that paragraph's text, so no delimiter row under
    it makes a table, where GitHub may make one, and a table is read only where its header and its delimiter row both
    start with `|`; a delimiter row under a header that does not start with `|` still takes the paragraph's one try, so
    where GitHub makes that table, the `|` rows under it, a later delimiter row among them, are read as the paragraph's
    text; a list item's own line is the item's block whatever it holds but a blockquote, a fence, or indented code, each
    a block of its own, so a heading on it, as in `- # H`, is the item's text; and a quote's own lines are not split
    into the blocks it holds, though a code block inside it is a block of its own and ends the quote's."""
    yield from _walk(lines).blocks


def _walk(lines: list[str]) -> _Walker:
    """The document read in one pass, holding one stack of open containers, the list items and blockquotes that hold
    the line, outermost first: its blocks, the lines of its code blocks, each heading no container holds, and the lines
    a container holds.

    Each line is read as CommonMark reads one, once. It continues each open container in turn: a blockquote while a `>`
    marker stands at most MAX_BLOCK_INDENT columns past the content that holds it, and a list item while the line is
    blank or indented as far as the item's content, counted from where the content that holds the item starts on this
    line. An item's content starts after its marker and the one to four spaces that follow it, or one column past the
    marker when more follow, since the text then opens with indented code, or when the line holds nothing else. When a
    container is not continued, a line that holds text and starts no block lazily continues the open paragraph, and any
    other line closes the containers it left. Then the line opens containers: a blockquote marker, and a list item,
    which interrupts a paragraph its containers all hold only at a bullet or `1.` with text after it, and opens whatever
    its number where the paragraph's container was left; each marker is read at most MAX_BLOCK_INDENT columns past the
    content that holds it. What is left of the line is a fence's opener, indented code where no paragraph is open, a
    heading, a thematic break, a table row, or paragraph text.

    A fence opens at a run of three or more backticks or tildes at most MAX_BLOCK_INDENT columns past the content that
    holds it, and runs to a run of the same character at least as long, as far in, or to the first line its containers
    leave; `find_fences` gives the fences this walk reads, so no reader of fences places one apart from it. A table is
    a `|` row and a delimiter row right under it with as many cells, such as `| - |`, a paragraph trying only its first
    delimiter-shaped row; its rows run on, and so does any line under them that starts no block, as GitHub reads them,
    and a list item or indented code after it opens as after any block but a paragraph. A list item whose marker has
    only spaces after it, then a blank line, is empty and closes.

    The limits: the partial tab a blockquote or list marker consumes is counted as a whole one; an HTML block or a
    setext heading is read as a paragraph, so a heading-shaped line inside an HTML comment is read as a heading; a list
    marker with nothing after it on its line opens no item, so a bare marker is text, and lazily continues a paragraph
    above it; a `|` line with no delimiter row under it is read as a paragraph's text for what may follow it, as GitHub
    reads it, though it is a table row of its own; a table is read only from a header row that starts with `|`, and a
    line under its rows that starts with `<` ends it as a paragraph, as an HTML block's start would; and a delimiter row
    under a header that does not start with `|` still takes the paragraph's one try, so where GitHub makes that table,
    the `|` rows under it, a later delimiter row among them, are read as the paragraph's text."""
    walker = _Walker(lines)
    for index in range(len(lines)):
        walker.line(index)
        # A line the open containers hold once it is read: one they continue, one it opens them on, and a lazy one.
        if walker.containers:
            walker.contained.add(index)
        walker.holding.append(walker.innermost())
    walker.end_fence(len(lines))
    walker.close()
    return walker


# A fence's run and info string, once the containers that hold its line and its indentation are read.
FENCE_RUN = re.compile(r"(?P<marker>`{3,}|~{3,})(?P<info>.*)")
# How the container stack names a blockquote, beside the width of each list item's content past the content that holds
# the item, which is never negative.
QUOTE_CONTAINER = -1
# A table's delimiter row, under its header row: cells of dashes, each with an optional `:` at either end, between
# pipes.
DELIMITER_ROW = re.compile(r"\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*")


def _cells(row: str) -> int:
    """How many cells a table row holds: the parts its dividing pipes make once a leading and a trailing pipe are taken
    off, as GitHub reads them, so a row of one pipe holds none and `||` one empty cell. A dividing pipe is one no
    backslash escapes, inside a code span too."""
    row = row.strip(LINE_END)
    row = row[1:] if row.startswith("|") else row
    if not row:
        return 0
    row = row[:-1] if row.endswith("|") else row
    return len(re.split(r"(?<!\\)\|", row))


@dataclass
class _Walker:
    """What `_walk` has read so far. The structure: the containers that hold the line, outermost first, as the width
    of each list item's content past the content that holds it, or QUOTE_CONTAINER, and the place of each blockquote
    among them; whether a paragraph's text is open; whether the last list item opened with nothing after its marker;
    how many `|` rows in a row the containers hold, the last of them, whether the paragraph they are in has tried to be
    a table, and whether they are a table's; the open fence, with its run; and `holders`, beside the containers, the
    innermost list item among each container and those outside it, or None. What it yields: the blocks, the code lines,
    the fences, the document's headings, the lines a container holds, the list items, and each line's innermost item.
    The open block: its first line, kind, and lines, and the blank lines after an open indented code block, which join
    it only when more code follows."""

    lines: list[str]
    containers: list[int] = field(default_factory=list)
    quoted: list[int] = field(default_factory=list)
    paragraph: bool = False
    bare: bool = False
    rows: int = 0
    header: str = ""
    tried: bool = False
    table: bool = False
    fence: Fence | None = None
    run: str = ""
    blocks: list[Block] = field(default_factory=list)
    code: set[int] = field(default_factory=set)
    headings: list[tuple[int, re.Match[str]]] = field(default_factory=list)
    fences: list[Fence] = field(default_factory=list)
    contained: set[int] = field(default_factory=set)
    holders: list[ListItem | None] = field(default_factory=list)
    items: list[ListItem] = field(default_factory=list)
    holding: list[ListItem | None] = field(default_factory=list)
    first: int = 0
    kind: str = ""
    open: list[str] = field(default_factory=list)
    pending: list[int] = field(default_factory=list)

    def innermost(self) -> ListItem | None:
        """The innermost list item among the open containers, or None."""
        return self.holders[-1] if self.holders else None

    def close(self) -> None:
        """The open block, if any, ends here; the containers that hold it stay open."""
        if self.open:
            self.blocks.append(Block(self.first, self.kind, tuple(self.open)))
        self.kind, self.open, self.pending = "", [], []

    def begin(self, index: int, kind: str) -> None:
        """The line opens a block of the kind, ending the open one."""
        self.close()
        self.first, self.kind, self.open = index, kind, [self.lines[index]]

    def held(self, line: str) -> tuple[int, int, int, int]:
        """How many open containers hold the line; the index in it past their markers; the column that index is at;
        and the column the content of the innermost of them starts at. Each character is read once."""
        position, column, start = 0, 0, 0
        last = len(line.rstrip(MARKDOWN_SPACE))
        first, width = _skip(line, 0)
        for count, container in enumerate(self.containers):
            if container == QUOTE_CONTAINER:
                if column + width - start > MAX_BLOCK_INDENT or first >= last or line[first] != ">":
                    return count, position, column, start
                position, column = _past_quote_marker(line, first, column + width)
                start = column
                first, width = _skip(line, position)
            elif first >= last:
                # Every list item holds a blank line, so the next blockquote is the first container that may not.
                later = bisect.bisect_left(self.quoted, count)
                return (
                    (self.quoted[later] if later < len(self.quoted) else len(self.containers)),
                    position,
                    column,
                    start,
                )
            elif column + width >= start + container:
                start += container
            else:
                return count, position, column, start
        return len(self.containers), position, column, start

    def line(self, index: int) -> None:
        """A line: a fence's, or one that continues, closes, and opens containers and is read as what is left of it."""
        if self.fence is not None and self.fenced(index):
            return
        line = self.lines[index]
        count, position, column, start = self.held(line)
        closed = count < len(self.containers)
        if closed:
            rest = line[position:]
            if self.paragraph and not is_blank(rest) and not _starts_leaving(rest, column, start):
                self.lazy(index)
                return
            self.leave(count)
            self.paragraph, self.bare = False, False
        opened = len(self.containers)
        position, column, start, empty = self.opened(index, position, column, start)
        rest = line[position:]
        if len(self.containers) > opened:
            self.paragraph = False
        # Whether the line has the containers of the line above, so an indented code block or a table may run on.
        same = not closed and len(self.containers) == opened
        if not same:
            self.rows, self.table = 0, False
        relative = column + indentation(rest) - start
        run = _fence_run(rest) if relative <= MAX_BLOCK_INDENT else None
        if run is not None:
            self.open_fence(index, run)
        elif empty:
            self.paragraph, self.bare, self.rows, self.table = False, True, 0, False
            self.emit(index, LIST_ITEM_BLOCK, opened, joins=False)
        elif is_blank(rest):
            self.blank(index, opened, same=same)
        else:
            self.leaf(index, rest, relative, opened, same=same)

    def leave(self, count: int) -> None:
        """The containers past the first `count` close."""
        del self.containers[count:], self.holders[count:]
        del self.quoted[bisect.bisect_left(self.quoted, count) :]

    def lazy(self, index: int) -> None:
        """A line that lazily continues the open paragraph, though a container that holds it does not hold the line."""
        if self.open:
            self.open.append(self.lines[index])
        else:
            self.begin(index, PARAGRAPH)

    def opened(self, index: int, position: int, column: int, start: int) -> tuple[int, int, int, bool]:
        """The containers the line at `index` opens from `position`, at `column`, each read past the content that
        holds it: the index past their markers, the column it is at, the column the content of the innermost container
        starts at, and whether the last list item it opens holds nothing after its marker. Each marker is matched where
        it stands and each character read once, and a thematic break is looked for only where the rest of the line can
        be one."""
        line = self.lines[index]
        interrupting, empty = self.paragraph, False
        last = len(line.rstrip(MARKDOWN_SPACE))
        thematic = _thematic_from(line)
        first, width = _skip(line, position)
        while first < last and column + width - start <= MAX_BLOCK_INDENT:
            if line[first] == ">":
                self.quoted.append(len(self.containers))
                self.containers.append(QUOTE_CONTAINER)
                self.holders.append(self.innermost())
                position, column = _past_quote_marker(line, first, column + width)
                start = column
            elif first >= thematic and THEMATIC_BREAK.fullmatch(line[first:].rstrip(LINE_END)):
                break
            else:
                marker = LIST_MARKER_AT.match(line, first)
                if marker is None or (
                    interrupting
                    and (re.fullmatch(INTERRUPTING_LIST_MARKER, marker.group("marker")) is None or marker.end() >= last)
                ):
                    break
                after = width + len(marker.group("marker"))
                _, spaces = _skip(line, marker.end("marker"), after)
                empty = marker.end() >= last
                opens_code = spaces > CODE_INDENT and not empty
                content = column + after + (1 if opens_code or empty else spaces)
                self.containers.append(content - start)
                item = ListItem(index, marker.group("marker"), marker.end(), len(self.quoted), self.innermost())
                self.items.append(item)
                self.holders.append(item)
                position, column, start = marker.end("marker"), column + after, content
                if empty:
                    break
            interrupting = False
            first, width = _skip(line, position)
        return position, column, start, empty

    def open_fence(self, index: int, run: re.Match[str]) -> None:
        """A fence's opener: it ends the open block, and its lines run until its closing run or its containers end."""
        self.paragraph, self.bare, self.rows, self.table = False, False, 0, False
        self.close()
        words = run.group("info").split()
        opener = _fence_opener(self.lines[index])
        indent = 0 if opener is None else _opened_fence(self.lines[index], index, opener).indent
        quotes = len(self.quoted)
        self.fence = Fence(index, None, words[0] if words else "", index, quotes, indent)
        self.run = run.group("marker")

    def fenced(self, index: int) -> bool:
        """Whether the open fence holds the line, as its body or its closing run; when its containers do not hold the
        line, the fence ends above it."""
        count, position, column, start = self.held(self.lines[index])
        if count < len(self.containers):
            self.end_fence(index)
            return False
        rest = self.lines[index][position:]
        closing = rest.strip(LINE_END)
        near = column + indentation(rest) - start <= MAX_BLOCK_INDENT
        if near and len(closing) >= len(self.run) and set(closing) == {self.run[0]}:
            self.end_fence(index + 1, closer=index)
        return True

    def end_fence(self, stop: int, closer: int | None = None) -> None:
        """The open fence, if any, ends: its lines are code, and its body is a code block."""
        if self.fence is None:
            return
        fence = replace(self.fence, closer=closer, stop=stop if closer is None else closer)
        self.fence = None
        self.fences.append(fence)
        self.code.update(fence.lines())
        self.blocks.append(Block(fence.opener + 1, CODE, tuple(fence.text(self.lines[i]) for i in fence.body())))

    def blank(self, index: int, opened: int, *, same: bool) -> None:
        """A line blank once its containers' markers are taken off: an empty list item above it closes, and it ends the
        open block, but an indented code block's, which it may join, and a blockquote's, whose `>` line it joins."""
        if self.bare and self.containers and self.containers[-1] != QUOTE_CONTAINER:
            self.containers.pop()
            self.holders.pop()
        self.paragraph, self.bare, self.rows, self.table = False, False, 0, False
        if self.kind == CODE and same:
            self.pending.append(index)
        elif self.quoted and not is_blank(self.lines[index]):
            self.emit(index, QUOTE, opened, joins=True)
        else:
            self.close()

    def leaf(self, index: int, rest: str, relative: int, opened: int, *, same: bool) -> None:
        """What is left of a line once its containers are read, `relative` columns past their content: indented code,
        a thematic break, a heading, a table row, or paragraph text."""
        joins = self.paragraph or (self.bare and same)
        self.bare = False
        if not self.paragraph and relative >= CODE_INDENT:
            self.rows, self.table = 0, False
            self.code_line(index, same=same)
            return
        block = rest.strip(LINE_END) if relative <= MAX_BLOCK_INDENT else ""
        if THEMATIC_BREAK.fullmatch(block) or starts_heading(block):
            self.paragraph, self.rows, self.table = False, 0, False
            match = ATX_HEADING.fullmatch(block)
            if match is not None and not self.containers:
                self.headings.append((index, match))
            self.emit(index, HEADING if starts_heading(block) else "", opened, joins=False)
        elif block.startswith("|") or (self.table and not block.startswith("<")):
            # A paragraph's first delimiter-shaped row makes the row right above it a table's header when their cells
            # match, whatever `|` lines stand above the header, and GFM tries no later one in the paragraph.
            self.tried = self.tried and self.paragraph
            if not self.table and self.paragraph and not self.tried and DELIMITER_ROW.fullmatch(block) is not None:
                self.table, self.tried = bool(self.rows and _cells(block) == _cells(self.header)), True
            self.header = block
            self.rows += 1
            # A lone `|` line is a paragraph's text, as GitHub reads it, until a delimiter row under it makes a table.
            self.paragraph = not self.table
            self.emit(index, TABLE_ROW, opened, joins=False)
        else:
            self.tried = self.tried and self.paragraph
            self.paragraph, self.rows, self.table = True, 0, False
            self.emit(index, PARAGRAPH, opened, joins=joins)

    def code_line(self, index: int, *, same: bool) -> None:
        """A line of an indented code block, which joins the open one with the blank lines before it, or opens one."""
        if self.kind == CODE and same:
            self.code.update(self.pending)
            self.open.extend(self.lines[blank] for blank in self.pending)
            self.open.append(self.lines[index])
            self.pending = []
        else:
            self.begin(index, CODE)
        self.code.add(index)

    def emit(self, index: int, kind: str, opened: int, *, joins: bool) -> None:
        """The line's block, given its own kind, or none for a thematic break: a line a blockquote holds joins the
        blockquote's block, unless the blockquote opens on it, a line that opens a list item opens the item's block,
        and any other line opens a block of its kind, or joins the open one when it continues its paragraph."""
        if self.quoted:
            if self.kind == QUOTE and self.quoted[0] < opened:
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


def _past_quote_marker(line: str, marker: int, column: int) -> tuple[int, int]:
    """The index past the `>` at `marker`, which stands at `column`, and the space or tab after it, and the column
    that index is at."""
    end = marker + 1 + (line[marker + 1 : marker + 2] in {" ", "\t"})
    return end, column + end - marker


def _skip(line: str, position: int, offset: int = 0) -> tuple[int, int]:
    """The index of the first character from `position` that is neither a space nor a tab, and the columns the spaces
    and tabs before it take, a tab advancing to the next multiple of four counted from `offset` columns before
    `position`."""
    width, index = 0, position
    while index < len(line) and line[index] in MARKDOWN_SPACE:
        width += 4 - (offset + width) % 4 if line[index] == "\t" else 1
        index += 1
    return index, width


def _thematic_from(line: str) -> int:
    """The least index from which the line holds only one of `-`, `*`, or `_` and spaces and tabs, where a thematic
    break may start, or past the line when none can; THEMATIC_BREAK decides whether one does."""
    text = line.rstrip(LINE_END)
    if not text or text[-1] not in "-*_":
        return len(line) + 1
    index = len(text)
    while index and text[index - 1] in (text[-1], " ", "\t"):
        index -= 1
    return index


@dataclass(frozen=True)
class CodeSpan:
    """An inline code span: the column of its opening backtick run, the column just past its closing run, how many
    backticks each run holds, and its text as written between them, its spaces kept."""

    start: int
    end: int
    run: int
    text: str

    @property
    def text_start(self) -> int:
        """The column of the span's text, just past its opening run."""
        return self.start + self.run


def code_spans(line: str) -> list[CodeSpan]:
    """Every inline code span of `line`, left to right, as CommonMark pairs backtick runs. A whole run opens a span,
    which the next whole run of the same length closes, and a run that no later run matches is text, so a span never
    closes on part of a longer run or opens on the end of one. A span is read within one line: a line break in `line`
    ends every run's search. A reader that must not take a span's text for prose, such as a link or a name a document
    cites, reads spans by it, since a span written with two backticks may hold one.

    Limits: a backtick run after a backslash still opens a span, where CommonMark reads its first backtick as escaped
    text and lets only the rest of the run open one. GitHub's renderer departs from CommonMark in two ways this reader
    does not follow: a run of more than 80 backticks opens no span there, and after a run that no later run matches,
    GitHub may read a later span on the line as text, where CommonMark, and this reader, read it as code.

    Each run's closer, the next run of its length, is found in one pass from the line's end, and the spans are read in
    one pass from its start, so the time is linear in the line's length, however its runs are written."""
    spans: list[CodeSpan] = []
    offset = 0
    for segment in line.split("\n"):
        spans.extend(_segment_code_spans(segment, offset))
        offset += len(segment) + 1
    return spans


def _segment_code_spans(segment: str, offset: int) -> list[CodeSpan]:
    """The code spans of a text with no line break, each column moved by `offset`."""
    return [replace(span, start=span.start + offset, end=span.end + offset) for span in CodeSpanReader(segment).spans()]


class CodeSpanReader:
    """The code spans of one line with no line break, read from any column on, as code_spans reads them from the
    line's start: a reader that sets part of the line aside, such as an HTML comment it hides, reads the spans after
    that part as CommonMark pairs them there. The line's runs are paired once, so a reading from each of several
    columns, each past the last, takes time linear in the line's length, however many columns it starts from."""

    def __init__(self, line: str) -> None:
        self.line = line
        self.runs = [(run.start(), run.end()) for run in BACKTICK_RUN.finditer(line)]
        # The index of the next run of each run's length, or -1 where no later run has it: the run that closes the
        # span the run opens.
        self.closers = [-1] * len(self.runs)
        following: dict[int, int] = {}
        for index in range(len(self.runs) - 1, -1, -1):
            start, end = self.runs[index]
            self.closers[index] = following.get(end - start, -1)
            following[end - start] = index

    def spans(self, column: int = 0) -> Iterator[CodeSpan]:
        """Each code span of the line, left to right, read as if the line started at `column`: a run that starts
        before it opens and closes no span."""
        index = bisect.bisect_left(self.runs, (column,))
        while index < len(self.runs):
            closer = self.closers[index]
            if closer < 0:
                index += 1
                continue
            start, end = self.runs[index]
            close_start, close_end = self.runs[closer]
            yield CodeSpan(start, close_end, end - start, self.line[end:close_start])
            index = closer + 1


def without_code_spans(line: str, fill: str = "") -> str:
    """`line` with each code span code_spans reads replaced by `fill` once for each of its characters: removed by
    default, or blanked with `" "`, which keeps every later column where it was."""
    kept: list[str] = []
    position = 0
    for span in code_spans(line):
        kept.append(line[position : span.start] + fill * (span.end - span.start))
        position = span.end
    kept.append(line[position:])
    return "".join(kept)


def prose_code_spans(lines: list[str]) -> list[tuple[int, str]]:
    """Each inline code span outside every code block, fenced or indented, as its line's index and its text: a backtick
    run inside a code block is the block's text, as GitHub renders it."""
    code = code_block_lines(lines)
    return [(index, span.text) for index, line in enumerate(lines) if index not in code for span in code_spans(line)]


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

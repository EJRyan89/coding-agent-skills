"""The Markdown readers shared by skills, repository tools, the deployer, and validation: the second-level section
reader, the fenced code block reader, the labels of a fence in an executable language, the inline code span
patterns, the HTML comment pattern, and the block rules every reader of Markdown's structure reads as CommonMark does:
the whitespace of block structure, list markers, and which list item may interrupt a paragraph."""

from __future__ import annotations

import re
from dataclasses import dataclass

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
# such fence to the five-line and output-placeholder rules, and the audit-skill audit reads the shells' fences
# as commands.
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
    first word, the index just past its last body line, and how many blockquotes hold it."""

    opener: int
    closer: int | None
    language: str
    stop: int
    quotes: int = 0

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
    for later in range(opener + 1, len(lines)):
        line = lines[later]
        for _ in range(quotes):
            quote = QUOTE_MARKER.match(line)
            if quote is None:
                return Fence(opener, None, language, later, quotes)
            line = line[quote.end() :]
        closing = line.strip(LINE_END)
        if len(closing) >= len(marker) and set(closing) == {marker[0]}:
            return Fence(opener, later, language, later, quotes)
    return Fence(opener, None, language, len(lines), quotes)


def code_block_lines(lines: list[str]) -> set[int]:
    """The indexes of the lines of every code block, fenced or indented, as CommonMark reads them.

    An indented code block is a run of lines each indented at least CODE_INDENT columns past the content of the list
    items that hold it, with the blank lines between them, starting where no paragraph is open: an indented line after
    a paragraph's text continues the paragraph, while one after a heading or a thematic break opens code. A list item
    holds the lines indented as far as its content, which starts after its marker and the one to four spaces that
    follow it, or one space when more follow, since the text then opens with indented code on the item's own line; a
    line after a blank one, or after any block but a paragraph, indented less leaves the item.
    Blockquote markers are taken off a line first, and a change in how many hold it starts a new container, but for a
    line that lazily continues a quoted paragraph. The limits: the partial tab a blockquote or list marker consumes is
    counted as a whole one, and an HTML block or a setext heading is read as a paragraph."""
    fenced = {index for fence in find_fences(lines) for index in fence.lines()}
    code: set[int] = set()
    items: list[int] = []
    paragraph = False
    run: list[int] = []
    depth = 0
    for index, quoted in enumerate(lines):
        line, quotes = _unquoted(quoted)
        if quotes != depth:
            if paragraph and quotes < depth and not is_blank(line):
                continue  # a lazy continuation of the quoted paragraph
            items, depth, paragraph, run = [], quotes, False, []
        if index in fenced:
            paragraph, run = False, []
            continue
        if is_blank(line):
            paragraph = False
            if run:
                run.append(index)
            continue
        width = indentation(line)
        if not paragraph:
            while items and width < items[-1]:
                items.pop()
            if width >= (items[-1] if items else 0) + CODE_INDENT:
                code.update(run)
                code.add(index)
                run = [index]
                continue
        run = []
        if THEMATIC_BREAK.fullmatch(line.rstrip(LINE_END)):
            paragraph = False
            continue
        item = _item_content(line, interrupting=paragraph)
        if item is not None:
            while items and width < items[-1]:
                items.pop()
            content, text, opens_code = item
            items.append(content)
            paragraph = not (opens_code or is_blank(text))
            if opens_code:
                code.add(index)
                run = [index]
        else:
            paragraph = ATX_HEADING.match(line) is None
    return fenced | code


def _unquoted(line: str) -> tuple[str, int]:
    """The line without the blockquote markers that open it, each indented at most MAX_BLOCK_INDENT columns, and how
    many there were."""
    quotes = 0
    while indentation(line) <= MAX_BLOCK_INDENT and (marker := QUOTE_MARKER.match(line)) is not None:
        line, quotes = line[marker.end() :], quotes + 1
    return line, quotes


def _item_content(line: str, *, interrupting: bool) -> tuple[int, str, bool] | None:
    """For a line that opens a list item as `list_item` reads it, the column the item's content starts at, its text,
    and whether that text opens an indented code block, as it does after more than CODE_INDENT columns of space; None
    for any other line."""
    match = LIST_ITEM.fullmatch(line)
    if match is None or list_item(line, interrupting=interrupting) is None:
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

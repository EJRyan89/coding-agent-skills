"""The Markdown readers shared by skills, repository tools, the deployer, and validation: the second-level section
reader, the fenced code block reader, and the inline code span pattern."""

from __future__ import annotations

import re
from dataclasses import dataclass

# An inline code span on one line, with its text as the first group, as a skill reads commands or cited paths from it.
CODE_SPAN = re.compile(r"`([^`\n]+)`")
# A fence's opening line: the blockquote and list item markers that hold it, any indentation, a run of three or more
# backticks or tildes, and its info string.
FENCE_OPENER = re.compile(
    r"(?P<container>(?:[ \t]*>[ \t]?|[ \t]*(?:[-+*]|\d{1,9}[.)])[ \t]+)*)[ \t]*(?P<marker>`{3,}|~{3,})(?P<info>.*)"
)
QUOTE_MARKER = re.compile(r"[ \t]*>[ \t]?")


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


def find_fences(lines: list[str]) -> list[Fence]:
    """Every fenced code block, by CommonMark's rules for the forms Markdown allows.

    A run of three or more backticks or tildes opens a fence, except a backtick run whose info string holds a backtick,
    which is inline code. Indentation is ignored rather than limited to three spaces, and blockquote and list item
    markers before the run are read as what holds it, so a fence nested in a list item or a blockquote counts. Only a
    run of the same character at least as long as the opener's, with nothing after it and nothing before it but
    whitespace and the blockquote markers that hold the fence, closes it, so a ``` line inside a ```` fence is body.
    A fence left open runs to the end of the blockquote that holds it, or of the document. Rendering, the extraction
    of Bash for ShellCheck, validation's Markdown policies, and the analyze-skill-cost audit all read fences through
    this one function.
    """
    fences: list[Fence] = []
    index = 0
    while index < len(lines):
        match = FENCE_OPENER.fullmatch(lines[index].rstrip())
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
        closing = line.strip()
        if len(closing) >= len(marker) and set(closing) == {marker[0]}:
            return Fence(opener, later, language, later, quotes)
    return Fence(opener, None, language, len(lines), quotes)


def prose_code_spans(lines: list[str]) -> list[tuple[int, str]]:
    """Each inline code span outside every fence, as its line's index and its text."""
    fenced = {index for fence in find_fences(lines) for index in fence.lines()}
    return [
        (index, span) for index, line in enumerate(lines) if index not in fenced for span in CODE_SPAN.findall(line)
    ]


def markdown_section(text: str, heading: str) -> str | None:
    """The lines under the first exact heading, ignoring its trailing whitespace, up to the next `## ` heading.

    CRLF becomes LF; body whitespace and comments are otherwise preserved. None means the heading is absent.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    start = next((index + 1 for index, line in enumerate(lines) if line.rstrip() == heading), None)
    if start is None:
        return None
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])

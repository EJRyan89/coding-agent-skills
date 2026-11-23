"""The one splitter of a file an author writes into its lines, breaking them only where the program that loads the
file does.

Every program that loads such a file breaks its lines at a carriage return, a line feed, or the pair of them, and
nowhere else: YAML 1.2, which each runtime's parser reads a skill's or an agent's frontmatter by, reads every other
character as text, the next line (U+0085) and the line and paragraph separators (U+2028, U+2029) included, which only
YAML 1.1 broke at; CommonMark ends a line of Markdown, such as a CLAUDE.md, an AGENTS.md, or a memory, at the same
three; and .NET's `TextReader.ReadLine`, through which MSBuild reads a solution file, breaks at the same three.
`str.splitlines` also breaks at the vertical tab, the form feed, the file, group, and record separators, and those
three, so a reader splitting with it would read a key, an import, or a project line the program never reads. A reader of
a file an author writes splits it with `text_lines`, never `str.splitlines`, or with `lines_and_breaks` where it rejoins
the lines it keeps or finds where one starts in the text. The exceptions are the files whose program breaks a line
elsewhere: an analyzer config, which the C# compiler breaks at more (`analyzer_config.py`), and a Bash script and a
`.shellcheckrc`, which Bash and ShellCheck break at LF alone, reading a CR as part of the line (the Bash grammar's
`bash_syntax.line_numbers`, ejryan89-code-review-core's `review_analyzers._shellcheckrc`). A program's output is split
where that program ends a line, at LF and nowhere else: `output_lines` for a program that ends a line with LF alone, as
git does, which `git_client` gives its callers under the same name, and `program_lines`, built on it, for one that may
end a line with CRLF, as a program on Windows does, taking off the CR before each LF. These are two functions rather
than one with a parameter because the CR a program on Windows writes is a line end only to such a program, while git
never writes it, and a CR before git's LF is the last character of a name or a message; a caller picks the program it
reads by the function's name, never by a flag it could leave out. A line of a program's output can hold any other
character, a name or a path it prints among them, U+2028 or a lone CR included. Validation refuses `str.splitlines` on
anything but plain bytes, which it breaks at CR, LF, and CRLF alone, and a split at LF alone of a file an author writes,
outside a module-level `LINE_SPLIT_ALLOWED` that states why; and a split of a program's output anywhere but at LF, by
`str.splitlines`, `bytes.splitlines`, `text_lines`, `lines_and_breaks`, or `line_starts` at `LINE_BREAK`, outside a
module-level `OUTPUT_SPLIT_ALLOWED` that states why.

A reader that numbers many positions of one text reads its `line_starts` once and takes each position's line with
`bisect_right` over them, never by counting the breaks from the text's start once per position, which takes time
that grows with the square of the text.
"""

from __future__ import annotations

import re
from bisect import bisect_right

# A line break as YAML 1.2, CommonMark, and .NET's TextReader.ReadLine read one.
LINE_BREAK = re.compile(r"\r\n|\r|\n")
# A line feed alone, where Bash and ShellCheck end a line, for a reader that counts a file's lines as they do.
LINE_FEED = re.compile("\n")
# A line with the break that ends it, or the last line when no break ends it.
KEPT_LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")


def text_lines(text: str, *, keepends: bool = False) -> list[str]:
    """The lines of `text`, split only at CRLF, CR, and LF, without their breaks unless `keepends` asks, as
    `str.splitlines` takes it. As `str.splitlines` does, a break that ends the text ends its last line rather than
    starting an empty one, and an empty text has no lines."""
    if keepends:
        return KEPT_LINE.findall(text)
    lines = LINE_BREAK.split(text)
    if lines[-1] == "":
        lines.pop()
    return lines


def lines_and_breaks(text: str) -> tuple[list[str], list[str]]:
    """The lines of `text` as `text_lines` gives them, and beside them the break that ends each, a CRLF, a CR, an LF,
    or nothing for a last line no break ends, so a reader that finds a line by its index can rejoin the lines it keeps,
    or find where a line starts, character for character."""
    kept = text_lines(text, keepends=True)
    # Each kept line holds one break, at its end, and no CR or LF before it.
    lines = [line.rstrip("\r\n") for line in kept]
    return lines, [line[len(body) :] for line, body in zip(kept, lines, strict=True)]


def output_lines(output: str) -> list[str]:
    """The lines of a program's output that ends each line with LF alone, as git does, split there and nowhere else:
    a CR, U+0085, U+2028, or any other character `str.splitlines` breaks on is part of its line, since a name or a
    path the program prints can hold it. As `text_lines` does, a break that ends the output ends its last line rather
    than starting an empty one, and empty output has no lines."""
    return output.removesuffix("\n").split("\n") if output else []


def program_lines(output: str) -> list[str]:
    """The lines of a program's output, split at LF alone as `output_lines` splits them, each without the CR before
    its LF that Windows writes."""
    return [line.removesuffix("\r") for line in output_lines(output)]


def line_starts(text: str, breaks: re.Pattern[str] = LINE_BREAK) -> list[int]:
    """Where each line of `text` starts, its lines ending at each match of `breaks`: CRLF, CR, and LF, as `text_lines`
    breaks them, unless the caller counts lines elsewhere, as LINE_FEED does at LF alone. A text that a break ends has
    one start more, at its end, so every position up to and including the text's end stands on a line."""
    return [0, *(match.end() for match in breaks.finditer(text))]


def line_index(starts: list[int], position: int) -> int:
    """The index, from 0, of the line `position` stands on among `starts`, as `line_starts` gives them: the number of
    breaks that end at or before it. A break stands on the line it ends."""
    return bisect_right(starts, position) - 1

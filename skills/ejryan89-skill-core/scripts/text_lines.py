"""The one splitter of a file an author writes into its lines, breaking them only where the program that loads the
file does.

Every program that loads such a file breaks its lines at a carriage return, a line feed, or the pair of them, and
nowhere else: YAML 1.2, which each runtime's parser reads a skill's or an agent's frontmatter by, reads every other
character as text, the next line (U+0085) and the line and paragraph separators (U+2028, U+2029) included, which only
YAML 1.1 broke at; CommonMark ends a line of Markdown, such as a CLAUDE.md, an AGENTS.md, or a memory, at the same
three; and .NET's `TextReader.ReadLine`, through which MSBuild reads a solution file, breaks at the same three.
`str.splitlines` also breaks at the vertical tab, the form feed, the file, group, and record separators, and those
three, so a reader splitting with it would read a key, an import, or a project line the program never reads. A reader
of a file an author writes splits it with `text_lines`, never `str.splitlines`. The exceptions are the files whose
program breaks a line elsewhere: an analyzer config, which the C# compiler breaks at more (`analyzer_config.py`), and a
Bash script and a `.shellcheckrc`, which Bash and ShellCheck break at LF alone, reading a CR as part of the line
(audit-skill's `skill_inventory.shell_lines`, ejryan89-code-review-core's `review_analyzers._shellcheckrc`). A
program's output is split where that program ends a line: `program_lines` for a program that ends one with LF, or
with CRLF as Windows does, and `git_client.output_lines` for git's. A line of a program's output can hold any other
character, a name or a path it prints among them, U+2028 or a lone CR included. Validation refuses `str.splitlines`
on anything but plain bytes, which it breaks at CR, LF, and CRLF alone, and a split at LF alone of a file an author
writes, outside a module-level `LINE_SPLIT_ALLOWED` that states why.
"""

from __future__ import annotations

import re

# A line break as YAML 1.2, CommonMark, and .NET's TextReader.ReadLine read one.
LINE_BREAK = re.compile(r"\r\n|\r|\n")
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


def program_lines(output: str) -> list[str]:
    """The lines of a program's output, split at LF alone, each without the CR before its LF that Windows writes. As
    `text_lines` does, a break that ends the output ends its last line rather than starting an empty one, and empty
    output has no lines."""
    lines = output.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]

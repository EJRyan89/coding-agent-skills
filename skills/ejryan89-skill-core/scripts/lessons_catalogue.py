"""The one reader of the lessons catalogue, docs/lessons.md, which validation's ledger check and the
`implement-change` repository skill's pattern lister share.

A pattern is a `###` heading under one of the catalogue's sections, `SECTIONS`: `## Patterns`, the patterns of code,
which the catalogue must hold, and `## Prompt patterns`, the patterns of the prompts that do the work, which it may,
each section once, with the lines after the heading up to the next heading of either level two or three. Each pattern
names the section it stands in. A line inside a fenced code block, which markdown_sections' `outside_fences`
blanks, is read as blank, so a heading or a field a fence shows is none. A field is a list item that opens with its
bold name and a colon, `- **Rule:** ...`; a line indented under it continues it, and any other line ends it. A field's
code spans are read through markdown_sections' `code_spans`, as CommonMark pairs backtick runs.

A mechanism a pattern names under **Use** is `<module>:<name>` or `<module>:<class>.<name>`, the form
`pkgutil.resolve_name` reads, `MECHANISM`, whose module is importable from one of `mechanism_bases`: the repository
root, or a skill's or repository skill's `scripts`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import file_presence
from markdown_sections import code_spans, outside_fences
from text_lines import text_lines

# The catalogue's path from the repository root.
CATALOGUE = "docs/lessons.md"
# The section of the patterns of code, which the catalogue must hold, and the section of the patterns of prompts.
PATTERNS = "Patterns"
PROMPT_PATTERNS = "Prompt patterns"
# The sections whose `###` headings are patterns, in the order the catalogue holds them.
SECTIONS = (PATTERNS, PROMPT_PATTERNS)
# The fields a pattern may have, each at most once; validation's ledger check says which a section's patterns need.
FIELDS = frozenset({"Rule", "Why", "Prompt", "Use", "Guard", "No guard", "Search for", "Caught by", "Examples"})
# A mechanism a pattern uses: a module, a name it binds, and a name that class binds, when it is one.
MECHANISM = re.compile(r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*):([A-Za-z_]\w*)(?:\.([A-Za-z_]\w*))?")
# A field's first line: its bold name, a colon inside the bold, and the field's text.
FIELD_LINE = re.compile(r"^- \*\*([^*:]+):\*\*(.*)$")


class CatalogueError(Exception):
    """The catalogue cannot be read, or holds no pattern: its message says which, naming the catalogue."""


@dataclass(frozen=True)
class Pattern:
    """One pattern: its heading's line number and name, each field's lines by number, the problems, by line, of a
    field the catalogue does not define or one given twice, and the section it stands in."""

    line: int
    name: str
    fields: dict[str, list[tuple[int, str]]]
    problems: list[tuple[int, str]]
    section: str = PATTERNS

    def spans(self, field: str) -> list[tuple[int, list[str]]]:
        """Each line of `field` with the text of the code spans it holds, each stripped."""
        return [
            (number, [span.text.strip() for span in code_spans(text)]) for number, text in self.fields.get(field, [])
        ]

    def clauses(self, field: str) -> list[list[tuple[int, str]]]:
        """The `;`-separated clauses of `field`, each the code spans it holds with the line each stands on, each
        stripped. A `;` inside a code span, such as `{ ...; }`, separates nothing, and a clause runs on across the
        field's continuation lines."""
        clauses: list[list[tuple[int, str]]] = [[]]
        for number, text in self.fields.get(field, []):
            column = 0
            for span in code_spans(text):
                clauses += [[] for _ in range(text.count(";", column, span.start))]
                clauses[-1].append((number, span.text.strip()))
                column = span.end
            clauses += [[] for _ in range(text.count(";", column))]
        return [clause for clause in clauses if clause]


def _fields(entry: list[tuple[int, str]]) -> tuple[dict[str, list[tuple[int, str]]], list[tuple[int, str]]]:
    """A pattern's fields, each the lines of its list item, and the problems, by line, of a field the catalogue does
    not define or one given twice."""
    fields: dict[str, list[tuple[int, str]]] = {}
    problems: list[tuple[int, str]] = []
    current: list[tuple[int, str]] | None = None
    for number, line in entry:
        if match := FIELD_LINE.match(line):
            name = match.group(1)
            if name not in FIELDS:
                problems.append((number, f"names the field {name!r}, which a pattern does not have"))
            elif name in fields:
                problems.append((number, f"names the field {name!r} more than once"))
            current = fields.setdefault(name, [])
            current.append((number, match.group(2).strip()))
        elif current is not None and line[:1].isspace() and line.strip():
            # A field's continuation line, indented under its list item, is read with the line it continues.
            current.append((number, line.strip()))
        else:
            current = None
    return fields, problems


def patterns(text: str) -> list[Pattern]:
    """The patterns of the catalogue's text, in the order it holds them; CatalogueError when the text has no
    `## Patterns` section or no pattern under it, or names one of its sections twice."""
    lines = outside_fences(text_lines(text))
    headings = [line[3:].rstrip() for line in lines if line.startswith("## ")]
    for title in SECTIONS:
        if headings.count(title) > 1:
            raise CatalogueError(f"{CATALOGUE} has more than one '## {title}' section")
    if PATTERNS not in headings:
        raise CatalogueError(f"{CATALOGUE} has no '## {PATTERNS}' section")
    entries: list[tuple[str, list[tuple[int, str]]]] = []
    section: str | None = None
    for number, line in enumerate(lines, start=1):
        if line.startswith("## "):
            name = line[3:].rstrip()
            section = name if name in SECTIONS else None
            continue
        if section is None:
            continue
        if line.startswith("### "):
            entries.append((section, []))
        if entries and entries[-1][0] == section:
            entries[-1][1].append((number, line))
    if not any(title == PATTERNS for title, _ in entries):
        raise CatalogueError(f"{CATALOGUE} has no pattern under '## {PATTERNS}'")
    found: list[Pattern] = []
    for title, entry in entries:
        number, heading = entry[0]
        fields, problems = _fields(entry[1:])
        found.append(Pattern(number, heading[4:].strip(), fields, problems, title))
    return found


def read_patterns(root: Path) -> list[Pattern]:
    """The patterns of the catalogue in the repository at `root`; CatalogueError when the catalogue is missing, is not
    a file, is not UTF-8, naming the decoder's words, or holds no pattern; and the OSError of a stat or a read that
    fails, which is never read as a catalogue that is not there."""
    path = root / CATALOGUE
    try:
        present = file_presence.is_present(path)
    except file_presence.NotAFile:
        raise CatalogueError(f"{CATALOGUE} is not a file") from None
    if not present:
        raise CatalogueError(f"{CATALOGUE} does not exist")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise CatalogueError(f"{CATALOGUE} cannot be read: {error}") from error
    return patterns(text)


def mechanism_bases(root: Path) -> list[Path]:
    """The folders a mechanism's module is importable from: the repository root, then each skill's and repository
    skill's `scripts`, in path order; the OSError of a skills folder that cannot be listed."""
    return [
        root,
        *sorted(
            (
                *file_presence.matching(root, "skills/*/scripts"),
                *file_presence.matching(root, ".claude/skills/*/scripts"),
            )
        ),
    ]

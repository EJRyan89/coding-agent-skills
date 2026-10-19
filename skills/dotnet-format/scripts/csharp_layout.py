"""Region layout checks no Roslyn analyzer offers, and the analyzer configuration for the rest of the C# layout rules.

    check  --repo-root R --file-list LIST [--fix]                  check (and fix) #region / #endregion layout
    config --repo-root R --solution S --file-list LIST [--apply]   report (and add) the layout analyzer configuration

`check` enforces the rules that StyleCop, Roslynator, and the IDE rules do not:

    region-scope            #endregion must close in the brace scope its #region opened in (report only)
    endregion-description   #endregion takes no trailing text (Roslynator RCS1189 enforces the opposite)
    region-spacing          exactly one blank line between an #endregion and a directly following #region
    endregion-brace-spacing no blank line between an #endregion and a directly following closing brace

It prints `VIOLATION <file> <line> <severity> <rule> <message>` per violation, `FIXED <file> <line> <rule>` per fix
with --fix (preserving line endings, BOM, and encoding), `UNREADABLE <file> <reason>` per file whose byte order mark
names an encoding its bytes are not in, which it neither checks nor fixes, and `SUMMARY <violations> <files> <rules>`.
It exits 1 when a VIOLATION remains or a file was UNREADABLE (findings), 0 otherwise.

`config` prints `PACKAGE Roslynator.Formatting.Analyzers present|missing`, or `... unknown <reason>`, and one
`SETTING <key> <wanted> <state>` per analyzer setting (state: present, missing, or other:<value>), judged as the
listed files see it. The package is present when MSBuild's evaluation of each project of the solution that owns a
listed file (`dotnet msbuild -getItem`, conditions and imports settled as a build settles them) holds a PackageReference
to it, or an Analyzer item of its assembly, as a packages.config project does; missing when one project's holds
neither; and unknown when dotnet is not installed or MSBuild cannot evaluate a project. A setting is read as the
compiler reads it: from the .editorconfig files above the file, nearest last, up to the repository root or the first
whose preamble, the lines before its first section, sets `root = true`; and below them from the global analyzer
configs, the nearest `.globalconfig` at or above the file's project folder within the repository and any .editorconfig
whose preamble sets `is_global = true`, each giving its preamble to every file and a section only to the file whose
absolute path it names, the higher `global_level` winning (100 for a `.globalconfig` that sets none, 0 for any other
file) and a key two files at one level both set left unset. A global config a project names in
`GlobalAnalyzerConfigFiles` is not read. With --apply it adds only the missing settings, as a `[*.cs]` section placed
first in the outermost .editorconfig that applies, so every existing section, and every value a global config sets,
still holds, and prints `ADDED <.editorconfig> <key> <value>`; it never changes a value the repository already sets
and never adds a package reference. It exits 1 when a setting is still missing, because --apply was not given or
could not write it (findings), 0 otherwise: another value is the repository's choice, and the package is advice the
skill cannot act on.

Both act only inside the repository. `config` evaluates a project the solution lists only when the entry lies inside
the repository root, judged from its text before anything touches it; an entry outside the root, or written as a
network share or a device, prints `REFUSED <entry> <reason>` and is left unread. Neither writes
a file that is a symbolic link or a Windows junction, or that lies below one inside the repository, since the write
would land wherever the link points: `check --fix` prints `REFUSED <file> <reason>` and leaves the file unfixed, and
`config --apply` prints `REFUSED <.editorconfig> <reason>`, adds nothing there, and exits 1, a setting still missing.

A failure, such as a listed file that does not exist or a file it cannot read or write, prints `FAILED <reason>`
as the last line and exits 1. A usage error exits 2.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from console import run_main
from dotnet_format_targets import owning_projects, read_file_list
from dotnet_solution import project_path, read_listed_projects, read_text, refusal, same_path
from folders_up import folders_up
from link_probe import is_link_or_junction
from run_dotnet_format import Services
from xml_reader import XmlRefused

LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")
DIRECTIVE = re.compile(r"[ \t]*#[ \t]*([A-Za-z]+)")
PACKAGE = "Roslynator.Formatting.Analyzers"
# The items of MSBuild's evaluation that show the package: a PackageReference to it, or an Analyzer item of its
# assembly, which a packages.config project lists in its own file; each with the metadata naming it.
PACKAGE_ITEMS = (("PackageReference", "Identity"), ("Analyzer", "Filename"))
MSBUILD_TIMEOUT_SECONDS = 120
GLOBAL_CONFIG = ".globalconfig"
# The global_level the compiler gives a global analyzer config that sets none: 100 for a file named .globalconfig.
NAMED_GLOBAL_LEVEL = 100
# The layout rules the analyzers enforce; the dotnet-format section of docs/skills.md says which rule each covers.
SETTINGS = (
    ("insert_final_newline", "true"),
    ("dotnet_diagnostic.RCS0041.severity", "warning"),
    ("dotnet_diagnostic.RCS0010.severity", "warning"),
    ("dotnet_diagnostic.RCS0012.severity", "warning"),
    ("dotnet_diagnostic.RCS0063.severity", "warning"),
    ("dotnet_diagnostic.RCS0002.severity", "warning"),
    ("dotnet_diagnostic.RCS0005.severity", "warning"),
)
ACCEPTED = {"true": {"true"}, "warning": {"warning", "error"}}


# --------------------------------------------------------------------------------------------------- lexing


@dataclass
class Event:
    kind: str  # "open", "close", or "directive"
    line: int
    name: str = ""


@dataclass
class Scan:
    events: list[Event] = field(default_factory=list)
    continuation: set[int] = field(default_factory=set)  # lines that begin inside a string or comment


@dataclass
class CodeState:
    """One Lexer.code call's state, which an interpolation hole lexed inside it keeps apart."""

    hole: int  # the hole's closing brace count, or 0 outside a hole
    depth: int = 0  # braces opened inside the hole
    nesting: int = 0  # parentheses and brackets, for the format clause of a hole


class Lexer:
    """Find code braces and preprocessor directives, skipping comments, strings, and character literals.

    Handles regular, verbatim, interpolated, and raw (including multi-dollar interpolated) string literals, so
    braces and '#' inside them are never mistaken for code. It is hand-rolled by decision: skills use only the
    standard library, which has no C# lexer, so its edge cases are pinned by fixtures in test_csharp_layout.py.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0
        self.line = 0
        self.scan = Scan()

    # Low-level helpers -------------------------------------------------------------------------------------

    def peek(self, offset: int = 0) -> str:
        index = self.i + offset
        return self.text[index] if index < len(self.text) else ""

    def newline(self, inside: bool) -> None:
        """Consume one line break; inside a token the next line is a continuation line."""
        self.i += 2 if self.text.startswith("\r\n", self.i) else 1
        self.line += 1
        if inside:
            self.scan.continuation.add(self.line)

    def at_newline(self) -> bool:
        return self.peek() in ("\r", "\n")

    def run_length(self, char: str) -> int:
        length = 0
        while self.peek(length) == char:
            length += 1
        return length

    # Code ------------------------------------------------------------------------------------------------

    def lex(self) -> Scan:
        self.code(hole=0)
        return self.scan

    def code(self, hole: int) -> None:
        """Lex code; inside an interpolation hole (hole = closing brace count) return after the hole closes."""
        state = CodeState(hole)
        line_start = hole == 0 and self.i == 0
        while self.i < len(self.text):
            char = self.peek()
            if self.at_newline():
                self.newline(inside=hole > 0)
                line_start = hole == 0
                continue
            if char in " \t\f\v":
                self.i += 1
                continue
            if char == "#" and line_start:
                self.directive()
                continue
            line_start = False
            if self.token(char, state):
                return

    def token(self, char: str, state: CodeState) -> bool:
        """Lex the token at char, past any line break, blank, or directive; return whether it closed the hole."""
        if char == "/" and self.peek(1) == "/":
            self.line_comment()
        elif char == "/" and self.peek(1) == "*":
            self.block_comment()
        elif char == "'":
            self.character()
        elif self.string_start():
            pass
        elif char in "([":
            state.nesting += 1
            self.i += 1
        elif char in ")]":
            state.nesting = max(0, state.nesting - 1)
            self.i += 1
        elif char in "{}":
            return self.brace(char, state)
        elif char == ":" and state.hole and state.depth == 0 and state.nesting == 0 and self.peek(1) != ":":
            self.format_clause(state.hole)
            return True
        elif char == ":" and self.peek(1) == ":":
            self.i += 2
        else:
            self.i += 1
        return False

    def brace(self, char: str, state: CodeState) -> bool:
        """Lex a brace: an event in code, a level in a hole, or the hole's closing run; return whether it closed it."""
        if char == "{":
            if state.hole:
                state.depth += 1
            else:
                self.scan.events.append(Event("open", self.line))
            self.i += 1
            return False
        if state.hole and state.depth == 0:
            self.i += min(state.hole, self.run_length("}"))
            return True
        if state.hole:
            state.depth -= 1
        else:
            self.scan.events.append(Event("close", self.line))
        self.i += 1
        return False

    def line_comment(self) -> None:
        while self.i < len(self.text) and not self.at_newline():
            self.i += 1

    def directive(self) -> None:
        start = self.i
        while self.i < len(self.text) and not self.at_newline():
            self.i += 1
        match = DIRECTIVE.match(self.text, start, self.i)
        self.scan.events.append(Event("directive", self.line, match.group(1) if match else ""))

    def block_comment(self) -> None:
        self.i += 2
        while self.i < len(self.text):
            if self.text.startswith("*/", self.i):
                self.i += 2
                return
            if self.at_newline():
                self.newline(inside=True)
            else:
                self.i += 1

    def character(self) -> None:
        self.i += 1
        while self.i < len(self.text) and not self.at_newline():
            char = self.peek()
            self.i += 2 if char == "\\" else 1
            if char == "'":
                return

    def format_clause(self, hole: int) -> None:
        """Skip an interpolation format clause, which is literal text up to the hole's closing brace."""
        while self.i < len(self.text) and self.peek() != "}":
            if self.at_newline():
                self.newline(inside=True)
            else:
                self.i += 1
        self.i += min(hole, self.run_length("}"))

    # Strings ---------------------------------------------------------------------------------------------

    def string_start(self) -> bool:
        """Lex a string literal starting here, if one does; return whether it did."""
        index = self.i
        dollars = 0
        verbatim = False
        while index < len(self.text) and self.text[index] in "$@":
            if self.text[index] == "$":
                dollars += 1
            elif verbatim:
                return False
            else:
                verbatim = True
            index += 1
        if index >= len(self.text) or self.text[index] != '"':
            return False
        self.i = index
        quotes = self.run_length('"')
        if not verbatim and quotes >= 3:
            self.i += quotes
            self.raw_string(quotes, dollars)
        elif not verbatim and quotes == 2:
            self.i += 2
        else:
            self.i += 1
            self.quoted_string(verbatim, interpolated=dollars > 0)
        return True

    def quoted_string(self, verbatim: bool, interpolated: bool) -> None:
        while self.i < len(self.text):
            char = self.peek()
            if self.at_newline():
                if not verbatim:
                    return
                self.newline(inside=True)
            elif char == "\\" and not verbatim:
                self.i += 2
            elif char == '"':
                if verbatim and self.peek(1) == '"':
                    self.i += 2
                    continue
                self.i += 1
                return
            elif interpolated and char in "{}" and self.peek(1) == char:
                self.i += 2
            elif interpolated and char == "{":
                self.i += 1
                self.code(hole=1)
            else:
                self.i += 1

    def raw_string(self, quotes: int, dollars: int) -> None:
        while self.i < len(self.text):
            char = self.peek()
            if self.at_newline():
                self.newline(inside=True)
            elif char == '"':
                run = self.run_length('"')
                self.i += run
                if run >= quotes:
                    return
            elif char == "{" and dollars:
                run = self.run_length("{")
                self.i += run
                if run >= dollars:
                    self.code(hole=dollars)
            else:
                self.i += 1


# ------------------------------------------------------------------------------------------------- checks


@dataclass(frozen=True)
class Violation:
    line: int  # zero-based
    severity: str
    rule: str
    message: str
    fixable: bool


def split_lines(text: str) -> list[str]:
    return LINE.findall(text)


def body(line: str) -> str:
    return line.rstrip("\r\n")


def scope_violations(scan: Scan) -> list[Violation]:
    """#endregion lines whose enclosing brace scope differs from their #region's, following the first #if branch."""
    scopes = [0]
    next_scope = 1
    regions: list[tuple[int, tuple[int, ...]]] = []
    conditionals: list[list[tuple[int, ...] | None]] = []
    found: list[Violation] = []
    for event in scan.events:
        if event.kind == "open":
            scopes.append(next_scope)
            next_scope += 1
        elif event.kind == "close":
            if len(scopes) > 1:
                scopes.pop()
        elif event.name == "region":
            regions.append((event.line, tuple(scopes)))
        elif event.name == "endregion" and regions:
            opened, opened_scopes = regions.pop()
            if opened_scopes != tuple(scopes):
                found.append(
                    Violation(
                        event.line,
                        "error",
                        "region-scope",
                        f"#endregion closes outside the brace scope where its #region (line {opened + 1}) was opened",
                        False,
                    )
                )
        elif event.name == "if":
            conditionals.append([tuple(scopes), None])
        elif event.name in ("elif", "else") and conditionals:
            if conditionals[-1][1] is None:
                conditionals[-1][1] = tuple(scopes)
            scopes = list(conditionals[-1][0] or (0,))
        elif event.name == "endif" and conditionals:
            started, first_branch = conditionals.pop()
            scopes = list(first_branch or started or (0,))
    return found


def following_blank_lines(lines: list[str], index: int, continuation: set[int]) -> tuple[int, int]:
    """The number of blank lines after line index, and the index of the next non-blank line (or len(lines))."""
    cursor = index + 1
    while cursor < len(lines) and cursor not in continuation and not body(lines[cursor]).strip():
        cursor += 1
    return cursor - index - 1, cursor


def check_text(text: str) -> list[Violation]:
    lines = split_lines(text)
    scan = Lexer(text).lex()
    directives = {event.line: event.name for event in scan.events if event.kind == "directive"}
    found = scope_violations(scan)
    for index, name in sorted(directives.items()):
        if name != "endregion":
            continue
        line = body(lines[index])
        keyword_end = line.index("endregion") + len("endregion")
        if line[keyword_end:].strip():
            found.append(
                Violation(
                    index,
                    "warning",
                    "endregion-description",
                    "#endregion must not have a description; remove the trailing text",
                    True,
                )
            )
        blanks, following = following_blank_lines(lines, index, scan.continuation)
        if following >= len(lines) or following in scan.continuation:
            continue
        if directives.get(following) == "region" and blanks != 1:
            found.append(
                Violation(
                    index,
                    "warning",
                    "region-spacing",
                    f"expected exactly 1 blank line between #endregion and the next #region, found {blanks}",
                    True,
                )
            )
        elif following not in directives and body(lines[following]).lstrip().startswith("}") and blanks:
            found.append(
                Violation(
                    index,
                    "warning",
                    "endregion-brace-spacing",
                    f"expected no blank lines between #endregion and the closing brace, found {blanks}",
                    True,
                )
            )
    return sorted(found, key=lambda violation: (violation.line, violation.rule))


def fix_text(text: str) -> tuple[str, list[Violation]]:
    """Apply the safe fixes, which only touch blank lines and #endregion's trailing text."""
    lines = split_lines(text)
    continuation = Lexer(text).lex().continuation
    fixed = [violation for violation in check_text(text) if violation.fixable]
    for violation in sorted(fixed, key=lambda violation: violation.line, reverse=True):
        index = violation.line
        line = lines[index]
        ending = line[len(body(line)) :]
        if violation.rule == "endregion-description":
            keyword_end = line.index("endregion") + len("endregion")
            lines[index] = line[:keyword_end] + ending
            continue
        # Bottom-up, so the lines and continuation set above this edit are still accurate.
        _, following = following_blank_lines(lines, index, continuation)
        lines[index + 1 : following] = [ending] * (1 if violation.rule == "region-spacing" else 0)
    return "".join(lines), fixed


# ------------------------------------------------------------------------------------------- file encoding


@dataclass(frozen=True)
class Source:
    text: str
    bom: bytes
    encoding: str


class Unreadable(ValueError):
    """A file whose byte order mark names an encoding its bytes are not in; the message says where they stop."""


def read_source(path: Path) -> Source:
    """Decode a source file so that it can be written back byte-for-byte except where it was changed.

    A file with a byte order mark is decoded strictly in the encoding the mark names, and raises `Unreadable` when its
    bytes are not in it: reading it any other way would write it back changed."""
    data = path.read_bytes()
    for bom, encoding in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if data.startswith(bom):
            try:
                return Source(data[len(bom) :].decode(encoding, errors="surrogatepass"), bom, encoding)
            except UnicodeDecodeError as error:
                raise Unreadable(
                    f"it starts with a {encoding.upper()} byte order mark, but byte {len(bom) + error.start} is not"
                    f" {encoding.upper()} text ({error.reason})"
                ) from None
    try:
        return Source(data.decode("utf-8"), b"", "utf-8")
    except UnicodeDecodeError:
        return Source(data.decode("latin-1"), b"", "latin-1")


def write_source(path: Path, source: Source, text: str) -> None:
    path.write_bytes(source.bom + text.encode(source.encoding, errors="surrogatepass"))


def link_refusal(root: Path, path: Path) -> str | None:
    """Why `path`, a file under `root` the script would write, is not written, or None: it, or a folder between it
    and `root`, is a link, so the write would land wherever the link points. A path that does not exist yet, such
    as an .editorconfig the script adds, is judged by the folders that do."""
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        try:
            if is_link_or_junction(current):
                where = "the file" if current == path else f"its folder {current.relative_to(root).as_posix()}"
                return f"{where} is a symbolic link or junction, which the script does not write through"
        except FileNotFoundError:
            return None
        except OSError as error:
            return f"{current.relative_to(root).as_posix()} cannot be examined: {error.strerror or error}"
    return None


class Failed(Exception):
    pass


def check_files(root: Path, names: list[str], fix: bool, emit: Callable[[str], None]) -> bool:
    """Check (and fix) each file; return whether a violation remains or a file could not be read. A file
    `link_refusal` refuses is checked and left unfixed, and one `read_source` cannot decode is reported and left
    alone while the rest are checked."""
    missing = next((root / name for name in names if not (root / name).is_file()), None)
    if missing is not None:
        raise Failed(f"{missing} does not exist")
    remaining: list[tuple[str, Violation]] = []
    unreadable = False
    for name in names:
        path = root / name
        try:
            source = read_source(path)
        except Unreadable as error:
            emit(f"UNREADABLE\t{name}\t{error}")
            unreadable = True
            continue
        text = source.text
        refused = link_refusal(root, path) if fix else None
        if refused:
            emit(f"REFUSED\t{name}\t{refused}")
        elif fix:
            text, fixed = fix_text(text)
            if fixed:
                write_source(path, source, text)
            for violation in fixed:
                emit(f"FIXED\t{name}\t{violation.line + 1}\t{violation.rule}")
        remaining += [(name, violation) for violation in check_text(text)]
    for name, violation in remaining:
        emit(
            "\t".join(
                ("VIOLATION", name, str(violation.line + 1), violation.severity, violation.rule, violation.message)
            )
        )
    rules = sorted({violation.rule for _, violation in remaining})
    emit(f"SUMMARY\t{len(remaining)}\t{len({name for name, _ in remaining})}\t{','.join(rules) or '-'}")
    return bool(remaining) or unreadable


# ------------------------------------------------------------------------------------------ configuration


# The EditorConfig glob engine below is hand-rolled by decision: skills use only the standard library, whose fnmatch
# and glob lack EditorConfig's braces, numeric ranges, and separator rules, so test_csharp_layout.py pins its cases.
NUMERIC_RANGE = re.compile(r"([+-]?[0-9]+)\.\.([+-]?[0-9]+)")


def _skip(pattern: str, index: int) -> int:
    """The index after the character at index, treating a backslash and the character it escapes as one."""
    return index + 2 if pattern[index] == "\\" else index + 1


def _closing_brace(pattern: str, start: int) -> int | None:
    depth = 0
    index = start
    while index < len(pattern):
        if pattern[index] == "{":
            depth += 1
        elif pattern[index] == "}":
            depth -= 1
            if depth == 0:
                return index
        index = _skip(pattern, index)
    return None


def _alternatives(body: str) -> list[str]:
    """body split at its top-level commas."""
    parts: list[str] = []
    depth = start = index = 0
    while index < len(body):
        if body[index] == "{":
            depth += 1
        elif body[index] == "}":
            depth -= 1
        elif body[index] == "," and depth == 0:
            parts.append(body[start:index])
            start = index + 1
        index = _skip(body, index)
    return [*parts, body[start:]]


def _bracket(pattern: str, start: int) -> tuple[str, int] | None:
    """A regular expression for the [seq] or [!seq] at start, and the index after it; None when it is unclosed.

    A bracket containing a path separator is a literal, and a negated one never matches a separator.
    """
    index = start + 1
    negated = pattern.startswith("!", index)
    index += negated
    body_start = index
    while index < len(pattern) and pattern[index] != "]":
        index = _skip(pattern, index)
    if index >= len(pattern) or index == body_start:
        return None
    body = pattern[body_start:index]
    if "/" in body:
        return re.escape(pattern[start : index + 1]), index + 1
    members: list[str] = []
    position = 0
    while position < len(body):
        if body[position] == "\\" and position + 1 < len(body):
            position += 1
        low = body[position]
        position += 1
        if body.startswith("-", position) and position + 1 < len(body) and body[position + 1] >= low:
            members.append(f"{re.escape(low)}-{re.escape(body[position + 1])}")
            position += 2
        else:
            members.append(re.escape(low))
    return ("[^/" if negated else "[") + "".join(members) + "]", index + 1


def _translate(pattern: str, ranges: list[tuple[int, int]]) -> tuple[str, bool]:
    """A regular expression for an EditorConfig glob, and whether it has a path separator outside brackets.

    Each {num1..num2} becomes a capturing group, the only one, whose bounds are appended to ranges in group order.
    """
    out: list[str] = []
    separator = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            out.append(re.escape(pattern[index + 1]))
            separator |= pattern[index + 1] == "/"
            index += 2
        elif pattern.startswith("**/", index) and (index == 0 or pattern[index - 1] == "/"):
            out.append("(?:.*/)?")  # zero or more whole directories
            separator = True
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif char == "*":
            out.append("[^/]*")
            index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        elif char == "[" and (bracket := _bracket(pattern, index)) is not None:
            out.append(bracket[0])
            index = bracket[1]
        elif char == "{" and (end := _closing_brace(pattern, index)) is not None:
            body = pattern[index + 1 : end]
            if numbers := NUMERIC_RANGE.fullmatch(body):
                low, high = sorted(int(value) for value in numbers.groups())
                ranges.append((low, high))
                out.append(r"([+-]?[0-9]+)")
            elif len(options := _alternatives(body)) == 1:  # braces without a comma are literal
                inner, inner_separator = _translate(body, ranges)
                out.append(r"\{" + inner + r"\}")
                separator |= inner_separator
            else:
                translated = [_translate(option, ranges) for option in options]
                out.append("(?:" + "|".join(regex for regex, _ in translated) + ")")
                separator |= any(has for _, has in translated)
            index = end + 1
        else:
            out.append(re.escape(char))
            separator |= char == "/"
            index += 1
    return "".join(out), separator


@functools.cache
def section_glob(section: str) -> tuple[re.Pattern[str], tuple[tuple[int, int], ...]]:
    """The compiled EditorConfig glob for a section name (https://spec.editorconfig.org/#glob-expressions).

    A glob with a path separator is relative to the .editorconfig's directory; any other matches at every level.
    """
    ranges: list[tuple[int, int]] = []
    anchored = section.startswith("/")
    regex, separator = _translate(section[1:] if anchored else section, ranges)
    prefix = "" if anchored or separator else "(?:.*/)?"
    return re.compile(prefix + regex, re.DOTALL), tuple(ranges)


def section_matches(section: str, relative_path: str) -> bool:
    """Whether an .editorconfig section applies to a file at relative_path from that .editorconfig's directory."""
    regex, ranges = section_glob(section)
    match = regex.fullmatch(relative_path)
    return match is not None and all(
        value is None or low <= int(value) <= high for value, (low, high) in zip(match.groups(), ranges, strict=True)
    )


@dataclass(frozen=True)
class AnalyzerConfig:
    """One analyzer configuration file as the compiler reads it: the properties of its preamble, the lines before its
    first section, and each section's name and properties in order, keys and values case-folded."""

    path: Path
    preamble: dict[str, str]
    sections: list[tuple[str, dict[str, str]]]

    @property
    def named_global(self) -> bool:
        return self.path.name.casefold() == GLOBAL_CONFIG

    @property
    def is_global(self) -> bool:
        """Whether the compiler reads it as a global config: a file named .globalconfig, or one whose preamble sets
        `is_global = true`, which an .editorconfig may."""
        return self.named_global or self.preamble.get("is_global") == "true"

    @property
    def is_root(self) -> bool:
        """Whether it ends the .editorconfig chain: `root = true` counts only in the preamble."""
        return not self.is_global and self.preamble.get("root") == "true"

    @property
    def global_level(self) -> int:
        try:
            return int(self.preamble["global_level"])
        except (KeyError, ValueError):
            return NAMED_GLOBAL_LEVEL if self.named_global else 0


def read_analyzer_config(path: Path) -> AnalyzerConfig:
    preamble: dict[str, str] = {}
    sections: list[tuple[str, dict[str, str]]] = []
    current = preamble
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            current = {}
            sections.append((line[1:-1], current))
        elif "=" in line:
            key, value = (part.strip() for part in line.split("=", 1))
            current[key.casefold()] = value.casefold()
    return AnalyzerConfig(path, preamble, sections)


def names_file(section: str, probe: Path) -> bool:
    """Whether a global config's section applies to the file: its name is the file's absolute path."""
    named = Path(section.replace("\\", "/"))
    return named.is_absolute() and same_path(named) == same_path(probe)


def global_settings(configs: list[AnalyzerConfig], probe: Path) -> dict[str, str]:
    """The values the global configs give a file: each one's preamble, and after it each section naming the file;
    between files the higher global_level wins, and a key two files at one level both set is unset."""
    chosen: dict[str, tuple[int, str | None]] = {}
    for config in configs:
        values = {key: value for key, value in config.preamble.items() if key not in ("is_global", "global_level")}
        for section, properties in config.sections:
            if names_file(section, probe):
                values.update(properties)
        level = config.global_level
        for key, value in values.items():
            current = chosen.get(key)
            if current is None or current[0] < level:
                chosen[key] = (level, value)
            elif current[0] == level:
                chosen[key] = (level, None)
    return {key: value for key, (_, value) in chosen.items() if value is not None}


class AnalyzerConfigs:
    """The analyzer configuration the compiler reads for files in the repository, each file read once, and none
    above the repository root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.read: dict[Path, AnalyzerConfig | None] = {}

    def config(self, path: Path) -> AnalyzerConfig | None:
        if path not in self.read:
            self.read[path] = read_analyzer_config(path) if path.is_file() else None
        return self.read[path]

    def above(self, directory: Path, name: str) -> list[AnalyzerConfig]:
        """The files named `name` in directory and each folder above it up to the root, nearest first."""
        return [
            config for folder in folders_up(directory, self.root) if (config := self.config(folder / name)) is not None
        ]

    def chain(self, probe: Path) -> list[AnalyzerConfig]:
        """The .editorconfig files that configure the file, nearest first, up to the root or the first root file."""
        chain: list[AnalyzerConfig] = []
        for config in self.above(probe.parent, ".editorconfig"):
            if config.is_global:
                continue
            chain.append(config)
            if config.is_root:
                break
        return chain

    def global_configs(self, probe: Path) -> list[AnalyzerConfig]:
        """The global configs that reach the file: every global .editorconfig above it, and the nearest .globalconfig
        at or above its project's folder, which the .NET SDK adds by itself."""
        projects = owning_projects(self.root, probe)
        start = projects[0].parent if projects else probe.parent
        globals_found = [config for config in self.above(probe.parent, ".editorconfig") if config.is_global]
        return globals_found + self.above(start, GLOBAL_CONFIG)[:1]

    def settings(self, probe: Path) -> dict[str, str]:
        """The file's settings: the global configs' values, which every .editorconfig section that applies overrides,
        a nearer file's over a farther one's and a later section's over an earlier one's."""
        settings = global_settings(self.global_configs(probe), probe)
        for config in reversed(self.chain(probe)):
            relative_path = Path(os.path.relpath(probe, config.path.parent)).as_posix()
            for section, properties in config.sections:
                if section_matches(section, relative_path):
                    settings.update(properties)
        return settings

    def apply_target(self, probe: Path) -> Path:
        """Where --apply adds a setting the file lacks: the outermost .editorconfig of its chain, else the root's."""
        chain = self.chain(probe)
        return chain[-1].path if chain else self.root / ".editorconfig"


def listed_projects_inside(root: Path, solution: Path, emit: Callable[[str], None]) -> dict[str, Path]:
    """The projects a solution lists, by `same_path`; a solution that cannot be read fails. An entry
    ejryan89-skill-core's `refusal` refuses, outside `root` or a network-share or device form, is reported and never
    touched."""
    try:
        listed = read_listed_projects(solution)
    except XmlRefused as error:
        raise Failed(f"{solution} cannot be read: {error}") from None
    projects: dict[str, Path] = {}
    for entry in listed:
        project = project_path(solution.parent, entry)
        refused = refusal(project, root)
        if refused:
            emit(f"REFUSED\t{entry}\t{refused}")
        else:
            projects.setdefault(same_path(project), project)
    return projects


def evaluated_reference(root: Path, project: Path, services: Services) -> bool | str:
    """Whether MSBuild's evaluation of the project holds the package, or why it could not tell."""
    name = project.relative_to(root).as_posix()
    arguments = ["dotnet", "msbuild", f"./{project.name}", "-nologo", "-nodeReuse:false"]
    arguments += [f"-getItem:{kind}" for kind, _ in PACKAGE_ITEMS]
    result = services.run(arguments, project.parent, MSBUILD_TIMEOUT_SECONDS)
    if result.timed_out:
        return f"MSBuild did not evaluate {name} within {MSBUILD_TIMEOUT_SECONDS} seconds"
    if result.returncode != 0:
        return f"MSBuild could not evaluate {name}: it exited with code {result.returncode}"
    text = result.output.decode("utf-8", errors="replace")
    try:
        document, _ = json.JSONDecoder().raw_decode(text, text.index("{"))
        items = document["Items"]
        found = [item[metadata] for kind, metadata in PACKAGE_ITEMS for item in items.get(kind, [])]
    except (ValueError, KeyError, TypeError, AttributeError):
        return f"MSBuild's evaluation of {name} is not the items it was asked for"
    return any(isinstance(value, str) and value.casefold() == PACKAGE.casefold() for value in found)


def package_state(
    root: Path, solution: Path, files: list[str], services: Services, emit: Callable[[str], None]
) -> tuple[str, str]:
    """The package's state for the projects of the solution that own a listed file, and why when it is unknown:
    missing as soon as one evaluation lacks it, unknown when none lacks it but one could not be evaluated."""
    listed = listed_projects_inside(root, solution, emit)
    projects: dict[str, Path] = {}
    for name in files:
        for project in owning_projects(root, root / name):
            if same_path(project) in listed:
                projects.setdefault(same_path(project), project)
    if projects and not services.which("dotnet"):
        return "unknown", "dotnet is not installed, so MSBuild cannot evaluate the projects"
    unknown = ""
    for project in projects.values():
        state = evaluated_reference(root, project, services)
        if state is False:
            return "missing", ""
        if isinstance(state, str):
            unknown = unknown or state
    if unknown:
        return "unknown", unknown
    return ("present", "") if projects else ("missing", "")


def insert_settings(path: Path, settings: list[tuple[str, str]]) -> None:
    """Add a [*.cs] section as the file's first section, so every existing section and nearer file still wins.

    Within an .editorconfig, a later section overrides an earlier one, so appending would override a
    path-specific choice such as `[src/**.cs]` setting a rule to none.
    """
    source = read_source(path) if path.is_file() else Source("", b"", "utf-8")
    newline = "\r\n" if "\r\n" in source.text else "\n"
    section = newline.join(["[*.cs]", *(f"{key} = {value}" for key, value in settings)]) + newline
    lines = source.text.splitlines(keepends=True)
    first = next((index for index, line in enumerate(lines) if line.strip().startswith("[")), None)
    if first is None:
        text = source.text
        if text and not text.endswith(("\r", "\n")):
            text += newline
        text += (newline if text.strip() else "") + section
    else:
        text = "".join(lines[:first]) + section + newline + "".join(lines[first:])
    write_source(path, source, text)


def configure(
    root: Path, solution: Path, files: list[str], apply: bool, services: Services, emit: Callable[[str], None]
) -> bool:
    """Report each setting as the changed files see it; with apply, fill only the gaps. Return whether a setting
    is still missing, which an .editorconfig `link_refusal` refuses to write, or `read_source` cannot read, leaves
    so."""
    state, reason = package_state(root, solution, files, services, emit)
    emit("\t".join(("PACKAGE", PACKAGE, state, *([reason] if reason else []))))
    configs = AnalyzerConfigs(root)
    effective = [(configs.apply_target(root / name), configs.settings(root / name)) for name in files]
    gaps: dict[Path, list[tuple[str, str]]] = {}
    for key, wanted in SETTINGS:
        values = [settings.get(key.casefold()) for _, settings in effective]
        if any(value is None for value in values):
            state = "missing"
            for target, settings in effective:
                if settings.get(key.casefold()) is None and (key, wanted) not in gaps.setdefault(target, []):
                    gaps[target].append((key, wanted))
        elif all(value in ACCEPTED[wanted] for value in values):
            state = "present"
        else:
            # No value is None here: the first branch took that case.
            state = "other:" + ",".join(
                sorted({value for value in values if value is not None and value not in ACCEPTED[wanted]})
            )
        emit(f"SETTING\t{key}\t{wanted}\t{state}")
    if not apply:
        return bool(gaps)
    still_missing = False
    for target, missing in gaps.items():
        refused = link_refusal(root, target)
        if refused:
            emit(f"REFUSED\t{target.relative_to(root).as_posix()}\t{refused}")
            still_missing = True
            continue
        try:
            insert_settings(target, missing)
        except Unreadable as error:
            emit(f"UNREADABLE\t{target.relative_to(root).as_posix()}\t{error}")
            still_missing = True
            continue
        for key, value in missing:
            emit(f"ADDED\t{target.relative_to(root).as_posix()}\t{key}\t{value}")
    return still_missing


# -------------------------------------------------------------------------------------------------- main


def main(argv: Sequence[str] | None = None, services: Services | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser("check", help="check (and fix) #region / #endregion layout")
    check_parser.add_argument("--repo-root", required=True, type=Path)
    check_parser.add_argument("--file-list", required=True, type=Path, help="FILE_LIST from dotnet_format_targets.py")
    check_parser.add_argument("--fix", action="store_true", help="apply the safe fixes")
    config_parser = commands.add_parser("config", help="report (and add) the layout analyzer configuration")
    config_parser.add_argument("--repo-root", required=True, type=Path)
    config_parser.add_argument("--solution", required=True, help="SOLUTION path, relative to the repository root")
    config_parser.add_argument("--file-list", required=True, type=Path, help="FILE_LIST from dotnet_format_targets.py")
    config_parser.add_argument("--apply", action="store_true", help="add the settings the changed files lack")
    arguments = parser.parse_args(argv)
    root = arguments.repo_root.resolve()
    try:
        try:
            names = read_file_list(arguments.file_list)
        except ValueError as exc:
            raise Failed(str(exc)) from None
        if arguments.command == "check":
            findings = check_files(root, names, arguments.fix, print)
        else:
            findings = configure(root, root / arguments.solution, names, arguments.apply, services or Services(), print)
    except (Failed, OSError) as error:
        print(f"FAILED {error}")
        return 1
    return 1 if findings else 0


if __name__ == "__main__":
    run_main(main, errors="replace")

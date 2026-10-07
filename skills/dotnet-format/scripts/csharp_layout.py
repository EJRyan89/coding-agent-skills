"""Region layout checks no Roslyn analyzer offers, and the analyzer configuration for the rest of the C# layout rules.

    check  --repo-root R --file-list LIST [--fix]                  check (and fix) #region / #endregion layout
    config --repo-root R --solution S --file-list LIST [--apply]   report (and add) the layout analyzer configuration

`check` enforces the rules that StyleCop, Roslynator, and the IDE rules do not:

    region-scope            #endregion must close in the brace scope its #region opened in (report only)
    endregion-description   #endregion takes no trailing text (Roslynator RCS1189 enforces the opposite)
    region-spacing          exactly one blank line between an #endregion and a directly following #region
    endregion-brace-spacing no blank line between an #endregion and a directly following closing brace

It prints `VIOLATION <file> <line> <severity> <rule> <message>` per violation, `FIXED <file> <line> <rule>` per fix
with --fix (preserving line endings, BOM, and encoding), and `SUMMARY <violations> <files> <rules>`. It exits 1 when
a VIOLATION remains (findings), 0 when none does.

`config` prints `PACKAGE Roslynator.Formatting.Analyzers present|missing` and one `SETTING <key> <wanted> <state>`
per .editorconfig setting (state: present, missing, or other:<value>), judged as the listed files see it. With
--apply it adds only the missing settings, as a `[*.cs]` section placed first in the outermost .editorconfig that
applies, so every existing section still overrides them, and prints `ADDED <.editorconfig> <key> <value>`; it never
changes a value the repository already sets and never adds a package reference. It exits 1 when a setting is
missing and --apply was not given (findings), 0 otherwise: another value is the repository's choice, and the
package is advice the skill cannot act on.

A failure, such as a listed file that does not exist or a file it cannot read or write, prints `FAILED <reason>`
as the last line and exits 1. A usage error exits 2.
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from console import use_utf8_output
from dotnet_format_targets import read_text, same_path, solution_projects

LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")
DIRECTIVE = re.compile(r"[ \t]*#[ \t]*([A-Za-z]+)")
PACKAGE = "Roslynator.Formatting.Analyzers"
PACKAGE_FILES = ("Directory.Build.props", "Directory.Build.targets", "Directory.Packages.props", "packages.config")
# The layout rules the analyzers enforce; see SKILL.md for which rule each setting covers.
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
    braces and '#' inside them are never mistaken for code.
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


def read_source(path: Path) -> Source:
    """Decode a source file so that it can be written back byte-for-byte except where it was changed."""
    data = path.read_bytes()
    for bom, encoding in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if data.startswith(bom):
            return Source(data[len(bom) :].decode(encoding, errors="surrogatepass"), bom, encoding)
    try:
        return Source(data.decode("utf-8"), b"", "utf-8")
    except UnicodeDecodeError:
        return Source(data.decode("latin-1"), b"", "latin-1")


def write_source(path: Path, source: Source, text: str) -> None:
    path.write_bytes(source.bom + text.encode(source.encoding, errors="surrogatepass"))


class Failed(Exception):
    pass


def check_files(root: Path, names: list[str], fix: bool, emit: Callable[[str], None]) -> bool:
    """Check (and fix) each file; return whether a violation remains."""
    missing = next((root / name for name in names if not (root / name).is_file()), None)
    if missing is not None:
        raise Failed(f"{missing} does not exist")
    remaining: list[tuple[str, Violation]] = []
    for name in names:
        path = root / name
        source = read_source(path)
        text = source.text
        if fix:
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
    return bool(remaining)


# ------------------------------------------------------------------------------------------ configuration


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


def editorconfig_chain(root: Path, directory: Path) -> list[Path]:
    """The .editorconfig files that apply in directory, nearest first, up to the repository root or root=true."""
    chain: list[Path] = []
    while True:
        candidate = directory / ".editorconfig"
        if candidate.is_file():
            chain.append(candidate)
            if re.search(r"^\s*root\s*=\s*true\s*$", read_text(candidate), re.IGNORECASE | re.MULTILINE):
                break
        if same_path(directory) == same_path(root) or directory.parent == directory:
            break
        directory = directory.parent
    return chain


def effective_settings(chain: list[Path], probe: Path) -> dict[str, str]:
    settings: dict[str, str] = {}
    for config in reversed(chain):
        relative_path = Path(os.path.relpath(probe, config.parent)).as_posix()
        applies = False
        for raw in read_text(config).splitlines():
            line = raw.strip()
            if not line or line[0] in "#;":
                continue
            if line.startswith("[") and line.endswith("]"):
                applies = section_matches(line[1:-1], relative_path)
            elif applies and "=" in line:
                key, value = (part.strip() for part in line.split("=", 1))
                settings[key.casefold()] = value.casefold()
    return settings


def package_referenced(root: Path, solution: Path) -> bool:
    directories: set[Path] = set()
    candidates: list[Path] = []
    for project in sorted(solution_projects(solution)):
        path = Path(project)
        if path.is_file():
            candidates.append(path)
        directory = path.parent
        while directory not in directories:
            directories.add(directory)
            candidates += [directory / name for name in PACKAGE_FILES]
            if same_path(directory) == same_path(root) or directory.parent == directory:
                break
            directory = directory.parent
    return any(path.is_file() and PACKAGE.casefold() in read_text(path).casefold() for path in candidates)


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


def configure(root: Path, solution: Path, files: list[str], apply: bool, emit: Callable[[str], None]) -> bool:
    """Report each setting as the changed files see it; with apply, fill only the gaps. Return whether a setting
    is still missing."""
    emit(f"PACKAGE\t{PACKAGE}\t{'present' if package_referenced(root, solution) else 'missing'}")
    effective: list[tuple[Path, dict[str, str]]] = []
    for name in files:
        path = root / name
        chain = editorconfig_chain(root, path.parent)
        effective.append((chain[-1] if chain else root / ".editorconfig", effective_settings(chain, path)))
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
    for target, missing in gaps.items():
        insert_settings(target, missing)
        for key, value in missing:
            emit(f"ADDED\t{target.relative_to(root).as_posix()}\t{key}\t{value}")
    return False


# -------------------------------------------------------------------------------------------------- main


def main(argv: Sequence[str] | None = None) -> int:
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
            text = arguments.file_list.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            raise Failed(f"{arguments.file_list} is not UTF-8 text") from None
        names = [name for name in (line.strip() for line in text.splitlines()) if name]
        if arguments.command == "check":
            findings = check_files(root, names, arguments.fix, print)
        else:
            findings = configure(root, root / arguments.solution, names, arguments.apply, print)
    except (Failed, OSError) as error:
        print(f"FAILED {error}")
        return 1
    return 1 if findings else 0


if __name__ == "__main__":
    use_utf8_output(errors="replace")
    sys.exit(main())

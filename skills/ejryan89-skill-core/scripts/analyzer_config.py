"""The one reader of an analyzer config's lines, an `.editorconfig` or `.globalconfig`, as the C# compiler reads them.

Roslyn's `AnalyzerConfig.Parse` breaks the text where its `SourceText` breaks lines: at a carriage return, a line
feed, the pair of them, a next line, a line separator, or a paragraph separator, so `str.splitlines`, which also
breaks at a vertical tab, a form feed, and the file and group separators, splits lines the compiler keeps whole. A
blank line or one whose first other character is `#` or `;` is a comment. A section header and a property line are
read by the compiler's own patterns: a key holds word characters, `.`, `-`, and `_`, `=` or `:` separates it from its
value, and a `#` or `;` after a header or a value starts a comment. Any other line sets nothing. The compiler lowers
each key, and the values it compares without regard to case, as `roslyn_lower` does.

`read_line` classifies one line, so `dotnet-format`'s analyzer configuration and the code-review analyzer inventory
read a file alike, each deciding what a line that sets nothing means to it.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal, NamedTuple

# The characters below 128 that .NET reads as whitespace, in char.IsWhiteSpace and in a pattern's \s.
ASCII_WHITESPACE = " \t\n\v\f\r"
# An analyzer config's line, its terminator kept, as Roslyn's SourceText breaks lines: at CRLF, or at any of
# TextUtilities.IsAnyLineBreakCharacter's carriage return, line feed, next line, line separator, and paragraph
# separator.
ANALYZER_CONFIG_BREAKS = "\r\n\x85\u2028\u2029"
ANALYZER_CONFIG_LINE = re.compile(r"[^\r\n\x85\u2028\u2029]*(?:\r\n|[\r\n\x85\u2028\u2029])|[^\r\n\x85\u2028\u2029]+\Z")
# An analyzer config's section header and property line, Roslyn's s_sectionMatcher and s_propertyMatcher in
# AnalyzerConfig.cs, matched as ASCII against a line `compiler_classes` maps: a key holds word characters, `.`, `-`,
# and `_`, `=` or `:` separates it from the value, and a `#` or `;` after a header or a value starts a comment.
SECTION_LINE = re.compile(r"^\s*\[(([^#;]|\\#|\\;)+)\]\s*([#;].*)?$", re.ASCII)
PROPERTY_LINE = re.compile(r"^\s*([\w\.\-_]+)\s*[=:]\s*(.*?)\s*([#;].*)?$", re.ASCII)
# The general categories .NET's regular expressions read as a word character: letters, nonspacing marks, decimal
# digits, and connector punctuation.
WORD_CATEGORIES = frozenset({"Lu", "Ll", "Lt", "Lm", "Lo", "Mn", "Nd", "Pc"})


class ConfigLine(NamedTuple):
    """One line as the compiler reads it: a `comment` (a blank line too), a `section` with its name, a `property` with
    its key and value as written, or `other`, a line that sets nothing."""

    kind: Literal["comment", "section", "property", "other"]
    name: str = ""
    value: str = ""


def _roslyn_lower(character: str) -> str:
    """A character as the compiler's CaseInsensitiveComparison.ToLower lowers it: its simple lowercase, one character
    for one, `İ` as `i`, and a character beyond the 16-bit range, which .NET holds as two surrogates it does not lower,
    unchanged."""
    if character == "\u0130":
        return "i"
    lower = character.lower()
    return lower if len(lower) == 1 and ord(character) <= 0xFFFF else character


def roslyn_lower(text: str) -> str:
    """Text lowered as the compiler lowers an analyzer config's key, and the values it compares without regard to
    case, one character at a time, never with Unicode case folding, which would read the long s as `s` or the
    sharp s as `ss`."""
    return text.lower() if text.isascii() else "".join(_roslyn_lower(character) for character in text)


def _compiler_class(character: str) -> str:
    """A character beyond ASCII, within an analyzer config's line, as the class .NET reads it in: a space for a space
    separator, the whitespace a line holds, since the other characters .NET reads as whitespace beyond ASCII (next line
    and the line and paragraph separators) end the line; `a` for a word character; and itself, which an ASCII pattern
    reads as neither, otherwise."""
    category = unicodedata.category(character)
    if category == "Zs":
        return " "
    return "a" if category in WORD_CATEGORIES else character


def compiler_classes(line: str) -> str:
    """The line with each character beyond ASCII replaced, one for one, by `_compiler_class`, so an ASCII pattern
    matches it as .NET matches the line and a match's spans index the line itself."""
    if line.isascii():
        return line
    return "".join(character if character.isascii() else _compiler_class(character) for character in line)


def analyzer_config_lines(text: str) -> list[str]:
    """The text's lines as the compiler breaks an analyzer config, each with its terminator."""
    return ANALYZER_CONFIG_LINE.findall(text)


def is_section_header(line: str) -> bool:
    """Whether the compiler reads the line, its terminator removed, as a section header."""
    return SECTION_LINE.match(compiler_classes(line)) is not None


def read_line(terminated: str) -> ConfigLine:
    """One of `analyzer_config_lines`, its terminator kept or not, as the compiler reads it."""
    line = terminated.rstrip(ANALYZER_CONFIG_BREAKS)
    classes = compiler_classes(line)
    stripped = classes.lstrip(ASCII_WHITESPACE)
    if not stripped or stripped[0] in "#;":
        return ConfigLine("comment")
    if section := SECTION_LINE.match(classes):
        return ConfigLine("section", line[section.start(1) : section.end(1)])
    if found := PROPERTY_LINE.match(classes):
        return ConfigLine("property", line[found.start(1) : found.end(1)], line[found.start(2) : found.end(2)])
    return ConfigLine("other")

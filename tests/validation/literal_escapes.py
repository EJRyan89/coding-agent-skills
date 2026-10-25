"""The string literal policy: a non-raw literal never holds a control escape where it reads as a path or a pattern, as
`"\\bin"` holds a backspace where a rooted Windows path was meant and `"C:\\temp"` a tab."""

from __future__ import annotations

import ast
import re
import unittest
from collections.abc import Iterator
from pathlib import Path

from python_checks import FORMAT_ROOTS, sanctioned_problems
from validation_support import (
    REPOSITORY_ROOT,
    import_aliases,
    python_source,
    python_trees,
    qualified_name,
    repository_files,
    scope_problems,
    scoped_nodes,
    under,
)

# The control escapes Python accepts without a warning, so a misread one passes the interpreter and ruff silently.
CONTROL_ESCAPES = "abfnrtv"
ALLOWANCE = "ESCAPE_ALLOWED"


def literal_pieces(segment: str) -> Iterator[tuple[str, str]]:
    """Each string literal in the source of one string expression, as its prefix and its body between the quotes: one
    piece, or several an implicit concatenation joins, with the whitespace, comments, and line continuations between
    them skipped."""
    index = 0
    while index < len(segment):
        character = segment[index]
        if character.isspace() or character in "()":
            index += 1
            continue
        if character == "\\":
            index += 2
            continue
        if character == "#":
            index = segment.find("\n", index) if "\n" in segment[index:] else len(segment)
            continue
        start = index
        while index < len(segment) and segment[index].isalpha():
            index += 1
        prefix = segment[start:index].lower()
        quote = segment[index : index + 3] if segment[index : index + 3] in ('"""', "'''") else segment[index]
        index += len(quote)
        body_start = index
        while index < len(segment) and not segment.startswith(quote, index):
            index += 2 if segment[index] == "\\" else 1
        yield prefix, segment[body_start:index]
        index += len(quote)


def control_escapes(body: str) -> Iterator[tuple[int, str]]:
    """Each control escape a non-raw literal's body writes, as its offset and its letter; an escaped backslash is a
    backslash, so the letter after it is no escape."""
    index = 0
    while index < len(body) - 1:
        if body[index] == "\\":
            if body[index + 1] in CONTROL_ESCAPES:
                yield index, body[index + 1]
            index += 2
        else:
            index += 1


def misread_escapes(prefix: str, body: str, context: str) -> list[str]:
    """Each control escape in one literal piece that reads as part of a path or a pattern, worded for a problem.

    Lexically, an escape is misread where a name follows it and a drive letter and its colon or a slash come before it,
    as in "C:\\temp" and "dir/\\new". \\a, \\b, and \\v, which text seldom means, are misread too wherever a word
    character sits beside them, as in "\\bin" and "C:\\\\Users\\bin"; \\n, \\r, \\t, and \\f beside one are the
    characters text means, as in "\\nNext line", "tab\\there", and the form feed that opens a PDF page's text. A
    literal that means the character, such as "\\n" alone, "line\\n" ending in it, or "name:\\n" closing a YAML key, is
    never one. A literal a path constructor takes is misread at any control escape, since no path holds one. A pattern
    re takes is misread at \\b, a backspace where the pattern means a word boundary; the other control escapes match
    the character they write either way."""
    path = re.compile(r"(?:(?:^|\W)[A-Za-z]:|/)$")
    word_before = re.compile(r"\w$")
    word = re.compile(r"\w")
    if "r" in prefix:
        return []
    # The body with each escape's two characters blanked, so the letter of one escape is never the word beside another.
    masked = re.sub(r"\\.", "\0\0", body, flags=re.DOTALL)
    found: list[str] = []
    for offset, letter in control_escapes(body):
        before, after = masked[:offset], masked[offset + 2 :]
        follows_name = word.match(after) is not None
        lexical = (follows_name and path.search(before) is not None) or (
            letter in "abv" and (follows_name or word_before.search(before) is not None)
        )
        if lexical or context == "path" or (context == "pattern" and letter == "b"):
            found.append(f"\\{letter}")
    return found


def literal_context(tree: ast.Module) -> dict[int, str]:
    """The id of each string literal passed straight to a path constructor or as a pattern to re, with which."""

    def readers() -> tuple[frozenset[str], frozenset[str]]:
        paths = frozenset(
            f"pathlib.{name}" for name in ("Path", "PurePath", "PureWindowsPath", "PurePosixPath", "WindowsPath")
        )
        functions = ("compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "subn", "split")
        return paths, frozenset(f"re.{name}" for name in functions)

    paths, patterns = readers()
    aliases = import_aliases(tree)
    context: dict[int, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = qualified_name(node.func, aliases)
        if name in paths:
            context.update((id(argument), "path") for argument in node.args)
        elif name in patterns:
            pattern = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "pattern"), None)
            if pattern is not None:
                context[id(pattern)] = "pattern"
    return context


def string_literals(tree: ast.Module) -> Iterator[tuple[ast.expr, str]]:
    """Each whole string or bytes literal, an f-string as one, with the scope that holds it; the parts inside an
    f-string are read with it."""
    inner: set[int] = set()
    for node, scope in scoped_nodes(tree.body):
        if id(node) in inner:
            continue
        if isinstance(node, ast.JoinedStr):
            inner.update(id(child) for child in ast.walk(node) if child is not node)
            yield node, scope
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            yield node, scope


def source_segment(lines: list[bytes], node: ast.expr) -> str:
    """A node's source text from its file's lines, each encoded, since the tree counts columns in UTF-8 bytes;
    ast.get_source_segment splits the whole file again for every node."""
    end_line = node.end_lineno or node.lineno
    end_column = node.end_col_offset if node.end_col_offset is not None else len(lines[end_line - 1])
    if node.lineno == end_line:
        return lines[node.lineno - 1][node.col_offset : end_column].decode("utf-8")
    first = lines[node.lineno - 1][node.col_offset :]
    middle = lines[node.lineno : end_line - 1]
    return b"\n".join([first, *middle, lines[end_line - 1][:end_column]]).decode("utf-8")


def escape_files(root: Path, files: list[Path]) -> list[Path]:
    """The Python the policy reads: every file ruff formats, suites included, since a test is where the slip hides."""
    return sorted(
        path for path in files if path.suffix == ".py" and under(path.relative_to(root).as_posix(), FORMAT_ROOTS)
    )


def literal_escape_problems(root: Path, files: list[Path]) -> list[str]:
    """Each non-raw string literal whose control escape reads as part of a path or a pattern, by its line, unless its
    scope's ESCAPE_ALLOWED entry states why."""
    problems: list[str] = []
    for path, name, tree in python_trees(escape_files(root, files), root, problems):
        lines = [line.encode("utf-8") for line in python_source(path).split("\n")]
        context = literal_context(tree)
        found: list[tuple[int, str, str]] = []
        for node, scope in string_literals(tree):
            segment = source_segment(lines, node)
            escapes = [
                escape
                for prefix, body in literal_pieces(segment)
                for escape in misread_escapes(prefix, body, context.get(id(node), ""))
            ]
            if escapes:
                written = ", ".join(sorted(set(escapes)))
                found.append(
                    (
                        node.lineno,
                        scope,
                        f"writes {written} in a non-raw literal that reads as a path or a pattern; make it raw, or "
                        f"escape the backslash, or state why in {ALLOWANCE}",
                    )
                )
        problems += sanctioned_problems(name, tree, ALLOWANCE, found)
    return problems


class LiteralEscapePolicies(unittest.TestCase):
    def test_no_literal_writes_a_control_escape_where_it_reads_as_a_path_or_pattern(self) -> None:
        self.assertEqual([], literal_escape_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_the_literal_policy_reads_every_module_its_rule_speaks_of(self) -> None:
        files = repository_files(REPOSITORY_ROOT)
        names = [path.relative_to(REPOSITORY_ROOT).as_posix() for path in files if path.suffix == ".py"]
        read = [path.relative_to(REPOSITORY_ROOT).as_posix() for path in escape_files(REPOSITORY_ROOT, files)]
        formatted = [name for name in names if under(name, FORMAT_ROOTS)]
        self.assertEqual([], scope_problems("the literal policy", formatted, read, {}))

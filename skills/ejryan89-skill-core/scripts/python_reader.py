"""The one parse of Python source, and of a Python literal, that a script did not write itself.

`ast.parse` raises a `SyntaxError` for source that does not parse, but a source nested past the parser's own limits,
such as a long run of unary operators, ends in a `MemoryError` or a `RecursionError` instead, and a null byte in a
`ValueError` on some releases; `ast.literal_eval` adds a `TypeError` for a literal set or dictionary of an unhashable
key. A handler that names `SyntaxError` alone lets the rest escape as a traceback rather than the caller's own
refusal. `parse`, `parse_expression`, and `literal` raise every one of them as `PythonRefused`, a `ValueError` whose
message names the parser's reason and whose `line` is the line it names, so a caller names one family whole and the
content-errors policy holds it to that.

The parser reads a tree far deeper than a Python walk over it recurses, such as a call through a long attribute chain
or a long run of chained conditional expressions, so a tree it gives could still end that walk in a
`RecursionError`. Each reader therefore refuses a tree past `nesting_depth`'s `TREE_DEPTH_LIMIT`, each node one level
below the node that holds it (`ast.iter_child_nodes`), as `json_reader` and `xml_reader` refuse a value past
`DEPTH_LIMIT`, so no walk over a tree they give needs a bound of its own. A tree nests one level for each link of a
chain, a `+` or an `elif` included, so the limit is the tree's own rather than a document's, and a chain of a little
under two hundred links is read. A tree past it is valid Python the parser read, so its refusal, `FOLLOWED`, says the
reader follows no deeper, apart from `NESTED`, source the parser itself cannot read.
"""

from __future__ import annotations

import ast
from typing import Any

import nesting_depth

# The refusal of a source or literal nested deeper than the parser reads.
NESTED = "nests deeper than the Python parser can read"
# The refusal of a tree the parser read that nests past TREE_DEPTH_LIMIT.
FOLLOWED = "nests deeper than the reader follows"


class PythonRefused(ValueError):
    """Python source or a literal the reader would not take; the message names the parser's reason, and `line` the line
    it names, or None when it names none."""

    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.line = line


# What `parse`, `parse_expression`, and `literal` raise on bad content.
CONTENT_ERRORS: tuple[type[Exception], ...] = (PythonRefused,)
# The functions that raise CONTENT_ERRORS on content.
CONTENT_READERS = ("parse", "parse_expression", "literal")


def _refused(error: Exception) -> PythonRefused:
    if isinstance(error, SyntaxError):
        line = error.lineno
        return PythonRefused(f"{error.msg} at line {line}" if line is not None else str(error.msg), line)
    if isinstance(error, MemoryError | RecursionError):
        return PythonRefused(NESTED)
    return PythonRefused(str(error))


def _bounded[Tree: ast.AST](tree: Tree) -> Tree:
    """`tree`, or `PythonRefused` with `FOLLOWED` when it nests past `TREE_DEPTH_LIMIT`."""
    if nesting_depth.nests_past(tree, ast.iter_child_nodes, nesting_depth.TREE_DEPTH_LIMIT):
        raise PythonRefused(FOLLOWED)
    return tree


def parse(source: str, filename: str = "<unknown>") -> ast.Module:
    """The module `source` is, or `PythonRefused` when it does not parse or nests past the parser's limits or
    `TREE_DEPTH_LIMIT`."""
    try:
        tree = ast.parse(source, filename=filename)
    except (SyntaxError, ValueError, MemoryError, RecursionError) as error:
        raise _refused(error) from None
    return _bounded(tree)


def parse_expression(source: str, filename: str = "<unknown>") -> ast.Expression:
    """The expression `source` is, or `PythonRefused` when it is not one or nests past the parser's limits or
    `TREE_DEPTH_LIMIT`."""
    try:
        tree = ast.parse(source, filename=filename, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError) as error:
        raise _refused(error) from None
    return _bounded(tree)


def literal(value: ast.AST | str) -> Any:
    """The value a literal node, or a literal's source, holds, or `PythonRefused` when it is no literal, holds an
    unhashable key, or nests past the parser's limits or `TREE_DEPTH_LIMIT`. A source is parsed as `ast.literal_eval`
    parses it, past the spaces and tabs that lead it."""
    node = parse_expression(value.lstrip(" \t")) if isinstance(value, str) else _bounded(value)
    try:
        return ast.literal_eval(node)
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError) as error:
        raise _refused(error) from None

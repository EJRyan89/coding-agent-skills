"""The one parse of Python source, and of a Python literal, that a script did not write itself.

`ast.parse` raises a `SyntaxError` for source that does not parse, but a source nested past the parser's own limits,
such as a long run of unary operators, ends in a `MemoryError` or a `RecursionError` instead, and a null byte in a
`ValueError` on some releases; `ast.literal_eval` adds a `TypeError` for a literal set or dictionary of an unhashable
key. A handler that names `SyntaxError` alone lets the rest escape as a traceback rather than the caller's own
refusal. `parse`, `parse_expression`, and `literal` raise every one of them as `PythonRefused`, a `ValueError` whose
message names the parser's reason and whose `line` is the line it names, so a caller names one family whole and the
content-errors policy holds it to that.
"""

from __future__ import annotations

import ast
from typing import Any

# The refusal of a source or literal nested deeper than the parser reads.
NESTED = "nests deeper than the Python parser can read"


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


def parse(source: str, filename: str = "<unknown>") -> ast.Module:
    """The module `source` is, or `PythonRefused` when it does not parse or nests past the parser's limits."""
    try:
        return ast.parse(source, filename=filename)
    except (SyntaxError, ValueError, MemoryError, RecursionError) as error:
        raise _refused(error) from None


def parse_expression(source: str, filename: str = "<unknown>") -> ast.Expression:
    """The expression `source` is, or `PythonRefused` when it is not one or nests past the parser's limits."""
    try:
        return ast.parse(source, filename=filename, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError) as error:
        raise _refused(error) from None


def literal(value: ast.AST | str) -> Any:
    """The value a literal node, or a literal's source, holds, or `PythonRefused` when it is no literal, holds an
    unhashable key, or nests past the parser's limits."""
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError) as error:
        raise _refused(error) from None

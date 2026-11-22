"""The line-numbering policy: a reader that numbers many positions of one text never counts the line breaks from the
text's start once per position, which takes time that grows with the square of the text, but reads the text's
`text_lines.line_starts` once and takes each position's line with `text_lines.line_index`.

Once per position, inside a loop, a `for`, an `async for`, a `while`, or a comprehension, or in a function that such a
loop of the same module calls by its name, as `line_of(text, match.start())` or `self.line_of(...)`, in any non-test
module the claim policies read, it refuses each of these forms that counts from the text's start:

- a `count` of a line break, a literal CR, LF, or CRLF, as text or bytes, or `chr(10)` or `chr(13)`, bound or unbound
  as `str.count(text, "\\n", 0, end)`, given a literal start of 0 and an end, or given no start on a slice of the text
  from its start, as `text[:end].count("\\n")`;
- a `text_lines`, `lines_and_breaks`, `program_lines`, or `line_starts` of text_lines given a slice of a text from its
  start, as `len(text_lines(text[:end]))`;
- a `findall`, `finditer`, or `split` of text_lines' `LINE_BREAK`, `LINE_FEED`, or `KEPT_LINE` given a slice of a text
  from its start, or a `findall` or `finditer` of one given a literal start of 0 and an end, as
  `LINE_BREAK.findall(text, 0, end)`.

Inside a loop means in its body, in a `while` loop's test, or in a comprehension's element, conditions, or any
iterable but its first, each of which runs once per item. A count that must stay states why in a module-level
LINE_COUNT_ALLOWED = {scope: reason}.

Stated limits, forms no module of the repository writes: a count in a function that numbers one position, called once
per position from a loop of another module, through another function, or through a name bound to it; a line break, a
start, or a pattern given through a name the policy does not resolve, as `text.count(NEWLINE, 0, end)` or a module's
own `re.compile("\\n")`; `re.findall` and `re.finditer` with a line-break pattern, which take no start; and a scan of
each character for a line break, which no call shows.
"""

from __future__ import annotations

import ast
import unittest
from collections.abc import Iterator
from pathlib import Path

from python_checks import COMPREHENSIONS, claim_files, resolves_to, sanctioned_problems
from validation_support import (
    REPOSITORY_ROOT,
    import_aliases,
    python_trees,
    qualified_name,
    repository_files,
    scoped_nodes,
)

LINE_COUNT_ALLOWANCE = "LINE_COUNT_ALLOWED"
# The line breaks a count may be given, as text or bytes, and the code points chr() gives them by.
LINE_BREAKS = frozenset({"\n", "\r", "\r\n", b"\n", b"\r", b"\r\n"})
LINE_BREAK_CODES = frozenset({10, 13})
# text_lines' readers of a text's lines and its line-break patterns, which a slice of a text from its start re-reads.
LINE_READERS = tuple(
    f"text_lines.{name}" for name in ("text_lines", "lines_and_breaks", "program_lines", "line_starts")
)
LINE_PATTERNS = tuple(f"text_lines.{name}" for name in ("LINE_BREAK", "LINE_FEED", "KEPT_LINE"))
# The pattern methods that take a start after the text, and those that read a slice of it.
POSITIONED_SEARCHES = frozenset({"findall", "finditer"})
PATTERN_READS = POSITIONED_SEARCHES | {"split"}
REFUSAL = (
    "counts line breaks from the text's start once per position, in a loop or a function a loop calls, in time that "
    "grows with the square of the text; read the text's text_lines.line_starts once and take each position's line with "
    f"text_lines.line_index, or state why in {LINE_COUNT_ALLOWANCE}"
)


def _is_zero(node: ast.expr | None) -> bool:
    """Whether an expression is the literal integer 0."""
    return isinstance(node, ast.Constant) and type(node.value) is int and node.value == 0


def _from_start(node: ast.expr) -> bool:
    """Whether an expression slices a value from its start to a given end, as `text[:end]` or `text[0:end]` does."""
    if not (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)):
        return False
    window = node.slice
    return (window.lower is None or _is_zero(window.lower)) and window.upper is not None and window.step is None


def _is_line_break(node: ast.expr) -> bool:
    """Whether an expression is a literal line break, or chr() of one's code point."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str | bytes) and node.value in LINE_BREAKS
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "chr"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in LINE_BREAK_CODES
    )


def _counts_from_start(call: ast.Call, aliases: dict[str, str]) -> bool:
    """Whether a call counts line breaks from a text's start, in any form the module docstring names."""
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "count":
        unbound = isinstance(func.value, ast.Name) and func.value.id in ("str", "bytes")
        receiver, given = (call.args[0], call.args[1:]) if unbound and call.args else (func.value, list(call.args))
        if not given or not _is_line_break(given[0]):
            return False
        if len(given) == 1:
            return _from_start(receiver)
        return len(given) > 2 and _is_zero(given[1])
    first = call.args[0] if call.args else None
    if any(resolves_to(qualified_name(func, aliases), reader) for reader in LINE_READERS):
        return first is not None and _from_start(first)
    if not (isinstance(func, ast.Attribute) and func.attr in PATTERN_READS):
        return False
    if not any(resolves_to(qualified_name(func.value, aliases), pattern) for pattern in LINE_PATTERNS):
        return False
    started = func.attr in POSITIONED_SEARCHES and len(call.args) > 2 and _is_zero(call.args[1])
    return first is not None and (_from_start(first) or started)


def _looped(tree: ast.Module) -> set[int]:
    """The id of each node a loop runs once per item: a `for` loop's body, a `while` loop's test and body, and a
    comprehension's element, conditions, and every iterable but its first, with all they hold."""
    looped: set[int] = set()

    def mark(node: ast.AST, looping: bool) -> None:
        if looping:
            looped.add(id(node))
        if isinstance(node, ast.For | ast.AsyncFor):
            parts: list[tuple[ast.AST, bool]] = [(node.target, looping), (node.iter, looping)]
            parts += [(statement, True) for statement in node.body]
            parts += [(statement, looping) for statement in node.orelse]
        elif isinstance(node, ast.While):
            parts = [(node.test, True), *((statement, True) for statement in node.body)]
            parts += [(statement, looping) for statement in node.orelse]
        elif isinstance(node, COMPREHENSIONS):
            first = node.generators[0]
            parts = [(first.iter, looping), (first.target, True), *((test, True) for test in first.ifs)]
            parts += [(child, True) for child in ast.iter_child_nodes(node) if child is not first]
        else:
            parts = [(child, looping) for child in ast.iter_child_nodes(node)]
        for child, inside in parts:
            mark(child, inside)

    mark(tree, False)
    return looped


def _called_in_loops(tree: ast.Module, looped: set[int]) -> set[int]:
    """The id of each node in a function, at any depth, that a loop of the module calls by its name, as `f(...)`, or as
    a method of `self` or `cls`, as `self.f(...)`."""
    called: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and id(node) in looped):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            called.add(func.id)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in ("self", "cls"):
            called.add(func.attr)
    return {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in called
        for statement in node.body
        for inner in ast.walk(statement)
    }


def _found(tree: ast.Module) -> Iterator[tuple[int, str, str]]:
    """Each count from a text's start that runs once per position, as its line, its scope, and the refusal."""
    aliases = import_aliases(tree)
    looped = _looped(tree)
    repeated = looped | _called_in_loops(tree, looped)
    for node, scope in scoped_nodes(tree.body):
        if isinstance(node, ast.Call) and id(node) in repeated and _counts_from_start(node, aliases):
            yield node.lineno, scope, REFUSAL


def line_numbering_problems(root: Path, files: list[Path]) -> list[str]:
    """Each count of line breaks from a text's start once per position, in the non-test modules the claim policies read,
    that no LINE_COUNT_ALLOWED entry sanctions, and that no module was read when none was, since a policy that read
    nothing found nothing."""
    read = claim_files(root, files)
    if not read:
        return ["no Python module was read, so the line-numbering policy could not judge anything"]
    problems: list[str] = []
    for _, name, tree in python_trees(read, root, problems):
        problems += sanctioned_problems(name, tree, LINE_COUNT_ALLOWANCE, list(_found(tree)))
    return problems


class LineNumberingPolicies(unittest.TestCase):
    def test_no_reader_counts_line_breaks_from_a_text_s_start_once_per_position(self) -> None:
        self.assertEqual([], line_numbering_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

"""The one bound on how deep a reader descends into text a script did not write, such as a JSON or XML document, an
EditorConfig glob, or an MSBuild condition.

A parser that recurses once per level of its input, or a walk that recurses once per level of the value a parser
gave, ends a document nested past the interpreter's recursion limit in a `RecursionError`, which names no input. That
limit is not one number: the C JSON scanner and the XML parser nest far deeper than a Python function can recurse, so
a document they read can still end the walk over it. `DEPTH_LIMIT` is far past what any file the repository reads
nests and far below the recursion limit. `deeper` is the check a recursive parser makes as it enters each level,
raising its caller's own error, which names the input; `nests_past` measures a value already parsed, or one a
writer is given, without recursing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

# The most levels a reader descends: a JSON array or object, an XML element, a glob's brace, or a condition's
# parenthesis or negation each is one.
DEPTH_LIMIT = 100


def deeper(depth: int, refused: Exception) -> int:
    """The level a recursive parser enters from `depth`, the level it is at, or `refused` raised when that level is
    past `DEPTH_LIMIT`."""
    if depth >= DEPTH_LIMIT:
        raise refused
    return depth + 1


def nests_past(root: object, children: Callable[[Any], Iterable[object]]) -> bool:
    """Whether `root`, at level one, holds a node past `DEPTH_LIMIT`, each node's `children` one level below it. It
    walks with a stack of its own, so a value nested deeper than the interpreter recurses is measured too, whatever
    the Python version, which a check that waits for a `RecursionError` is not. A node that holds one of the nodes
    above it, as a list that holds itself does, is not entered again, so such a value is measured down to that repeat
    and its writer names the cycle itself."""
    above: set[int] = set()
    stack: list[tuple[object, int, bool]] = [(root, 1, False)]
    while stack:
        node, level, leaving = stack.pop()
        if leaving:
            above.discard(id(node))
        elif id(node) not in above:
            if level > DEPTH_LIMIT:
                return True
            above.add(id(node))
            stack.append((node, level, True))
            stack.extend((child, level + 1, False) for child in children(node))
    return False

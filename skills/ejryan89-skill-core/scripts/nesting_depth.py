"""The one bound on how deep a reader descends into text a script did not write, such as a JSON or XML document, an
EditorConfig glob, or an MSBuild condition.

A parser that recurses once per level of its input, or a walk that recurses once per level of the value a parser
gave, ends a document nested past the interpreter's recursion limit in a `RecursionError`, which names no input. That
limit is not one number: the C JSON scanner and the XML parser nest far deeper than a Python function can recurse, so
a document they read can still end the walk over it. `DEPTH_LIMIT` is far past what any file the repository reads
nests and far below the recursion limit. `deeper` is the check a recursive parser makes as it enters each level,
raising its caller's own error, which names the input; `nests_past` measures a value already parsed without recursing.
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
    walks with a stack of its own, so a value nested deeper than the interpreter recurses is measured too."""
    stack = [(root, 1)]
    while stack:
        node, level = stack.pop()
        if level > DEPTH_LIMIT:
            return True
        stack.extend((child, level + 1) for child in children(node))
    return False

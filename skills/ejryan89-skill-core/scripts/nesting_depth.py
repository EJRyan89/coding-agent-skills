"""The bounds on how deep a reader descends into text a script did not write, such as a JSON or XML document, an
EditorConfig glob, an MSBuild condition, or Python source.

A parser that recurses once per level of its input, or a walk that recurses once per level of the value a parser
gave, ends a document nested past the interpreter's recursion limit in a `RecursionError`, which names no input. That
limit is not one number: the C JSON scanner and the XML parser nest far deeper than a Python function can recurse, so
a document they read can still end the walk over it. `DEPTH_LIMIT` is far past what any file the repository reads
nests and far below the recursion limit. `deeper` is the check a recursive parser makes as it enters each level,
raising its caller's own error, which names the input; `nests_past` measures a value already parsed, or one a
writer is given, without recursing.

A Python syntax tree is measured against `TREE_DEPTH_LIMIT` instead. Each level of a document is a bracket or an
element its writer opened, but a tree nests one level for each link of a chain written flat: `1 + 1 + 1`, an `if`
and its `elif` arms, `a.b.c`, and `x if c else y if d else z` each nest one level deeper with every link, so a valid
file with a chain a hundred links long passes `DEPTH_LIMIT`, though CPython parses chains far longer. A walk that
recurses once per level over a tree cannot follow every chain CPython parses, so the limit stays below where such
a walk runs out of the default recursion limit, and at twice `DEPTH_LIMIT`, as deep as CPython's tokenizer lets
brackets nest, it holds a chain of a little under two hundred links.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

# The most levels a reader descends: a JSON array or object, an XML element, a glob's brace, or a condition's
# parenthesis or negation each is one.
DEPTH_LIMIT = 100
# The most levels python_reader takes a Python syntax tree to: each node is one level below the node that holds it.
TREE_DEPTH_LIMIT = 200


def deeper(depth: int, refused: Exception) -> int:
    """The level a recursive parser enters from `depth`, the level it is at, or `refused` raised when that level is
    past `DEPTH_LIMIT`."""
    if depth >= DEPTH_LIMIT:
        raise refused
    return depth + 1


def nests_past(root: object, children: Callable[[Any], Iterable[object]], limit: int = DEPTH_LIMIT) -> bool:
    """Whether `root`, at level one, holds a node past `limit`, `DEPTH_LIMIT` unless given, each node's `children` one
    level below it. It walks with a stack of its own, so a value nested deeper than the interpreter recurses is
    measured too, whatever the Python version, which a check that waits for a `RecursionError` is not. A node that
    holds one of the nodes above it, as a list that holds itself does, is not entered again, so such a value is
    measured down to that repeat and its writer names the cycle itself."""
    above: set[int] = set()
    stack: list[tuple[object, int, bool]] = [(root, 1, False)]
    while stack:
        node, level, leaving = stack.pop()
        if leaving:
            above.discard(id(node))
        elif id(node) not in above:
            if level > limit:
                return True
            above.add(id(node))
            stack.append((node, level, True))
            stack.extend((child, level + 1, False) for child in children(node))
    return False

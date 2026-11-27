"""The one walk over a graph that reads each node once per question.

A walk that carries the nodes on its path down the recursion, as `seen | {node}`, reads a node again on every path
that reaches it, so a ladder of diamonds, each node reached through two, is read in time that doubles with each rung.
When what a walk learns from a node depends only on the node, it keeps one answer per node for the whole question
instead: `MemoizedWalk` is that walk. It is built for one question from a `read(node, walk)` that answers a node,
following each node it leads to by calling `walk(other)`, and is called with the question's first node.

Each node is read once while the walk lasts, and asked again it gives the answer its read gave. A node asked while
its own read is still under way, through a cycle, gives `cycle` instead, the answer that adds nothing, such as an
empty set for a union or False for an any. So a node read inside a cycle answers without the nodes on the path the
cycle leads back to; for a union or an any, every such node is still read in full by its own read, so the answer to
the question's first node is whole. `entered(node)` holds a node as under way without reading it, for a question
whose first node answers in its own way and must cut a cycle back to it. A read that raises leaves its node unread,
so asking it again reads it again. A new question, whose answers may differ, builds a new walk.

validation's per-path walk policy (tests/validation/python_checks.py) refuses a recursion that carries such a path
instead, unless its module states in WALK_ALLOWED why the state must be per path and what bounds the walk.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager


class MemoizedWalk[Node: Hashable, Answer]:
    """One question's walk: `read(node, walk)` answers a node, once, following other nodes through `walk(other)`."""

    def __init__(self, read: Callable[[Node, MemoizedWalk[Node, Answer]], Answer], cycle: Answer) -> None:
        self._read = read
        self._cycle = cycle
        self._answers: dict[Node, Answer] = {}
        self._reading: set[Node] = set()

    def __call__(self, node: Node) -> Answer:
        """The node's answer: the one its read gave, `cycle` while its read is under way, or else its read's, kept."""
        if node in self._answers:
            return self._answers[node]
        if node in self._reading:
            return self._cycle
        with self.entered(node):
            answer = self._read(node, self)
        self._answers[node] = answer
        return answer

    @contextmanager
    def entered(self, node: Node) -> Iterator[None]:
        """Hold the node as under way while the block runs, so a cycle back to it gives `cycle`."""
        self._reading.add(node)
        try:
            yield
        finally:
            self._reading.discard(node)

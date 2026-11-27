"""The one walk over a graph that reads each node once per question, and answers as a walk per path does.

A walk that carries the nodes on its path down the recursion, as `seen | {node}`, reads a node again on every path
that reaches it, so a ladder of diamonds, each node reached through two, is read in time that doubles with each rung.
`MemoizedWalk` keeps each node's answer instead. It is built for one question from a `read(node, walk)` that answers a
node, following each node it leads to by calling `walk(other)`, and is called with the question's first node.

A node reached while it is under way, through a cycle, gives `cycle`, as a walk per path cuts it. A node's answer is
given again only where it would come out the same: where, of the nodes its read reached, those under way now are
exactly those its read found under way and cut. Otherwise it is read again. So every answer is the one a walk per
path gives, whatever the caller does with it: asks several first nodes of one walk, keeps an inner node's answer, or
combines answers by concatenation or a first match rather than a union. Only a read that meets no node under way
above it is read once whatever reaches it; a cycle's nodes are read again on each path into the cycle, as a walk per
path reads them.

`under(node)`, by default the node itself, is what a node is under way as: a walk whose nodes ask different questions
of one thing, and must cut a cycle through that thing whatever question it is asked, names the thing. `entered(node)`
holds a node as under way without reading it, for a question whose first node answers in its own way and must cut a
cycle back to it. A read that raises keeps no answer. A new question, whose answers may differ, builds a new walk.

validation's per-path walk policy (tests/validation/python_checks.py) refuses a recursion that carries its path
instead, unless its module states in WALK_ALLOWED why the state must be per path and what bounds the walk.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class _Reading:
    """What one read under way has met: each mark it reached, and each mark it found under way and cut."""

    reached: set[Hashable] = field(default_factory=set)
    cut: set[Hashable] = field(default_factory=set)


@dataclass(frozen=True)
class _Kept[Answer]:
    """A node's answer, with the marks its read reached and those it found under way above it and cut."""

    answer: Answer
    reached: frozenset[Hashable]
    cut: frozenset[Hashable]


def _itself(node: Hashable) -> Hashable:
    return node


class MemoizedWalk[Node: Hashable, Answer]:
    """One question's walk: `read(node, walk)` answers a node, following other nodes through `walk(other)`, each read
    once wherever its answer would come out the same."""

    def __init__(
        self,
        read: Callable[[Node, MemoizedWalk[Node, Answer]], Answer],
        cycle: Answer,
        under: Callable[[Node], Hashable] = _itself,
    ) -> None:
        self._read = read
        self._cycle = cycle
        self._under = under
        self._kept: dict[Node, _Kept[Answer]] = {}
        self._under_way: set[Hashable] = set()
        self._readings: list[_Reading] = []

    def __call__(self, node: Node) -> Answer:
        """The node's answer: `cycle` while it is under way, the kept one where it would come out the same, or else
        its read's, kept."""
        mark = self._under(node)
        if mark in self._under_way:
            self._met({mark}, {mark})
            return self._cycle
        kept = self._kept.get(node)
        if kept is not None and {other for other in self._under_way if other in kept.reached} == kept.cut:
            self._met(kept.reached, kept.cut)
            return kept.answer
        with self.entered(node) as reading:
            answer = self._read(node, self)
        cut = frozenset(other for other in reading.cut if other in self._under_way)
        self._kept[node] = _Kept(answer, frozenset(reading.reached | {mark}), cut)
        return answer

    @contextmanager
    def entered(self, node: Node) -> Iterator[_Reading]:
        """Hold the node as under way while the block runs, so a cycle back to it gives `cycle`."""
        mark = self._under(node)
        reading = _Reading()
        self._under_way.add(mark)
        self._readings.append(reading)
        try:
            yield reading
        finally:
            self._readings.pop()
            self._under_way.discard(mark)
        self._met(reading.reached | {mark}, reading.cut)

    def _met(self, reached: set[Hashable] | frozenset[Hashable], cut: set[Hashable] | frozenset[Hashable]) -> None:
        """Add what a read or a kept answer met to the read under way that asked for it."""
        if self._readings:
            self._readings[-1].reached.update(reached)
            self._readings[-1].cut.update(cut)

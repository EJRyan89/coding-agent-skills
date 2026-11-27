"""Regression tests for the one walk that reads each node once per question."""

from __future__ import annotations

import itertools
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memoized_walk import MemoizedWalk


def diamonds(depth: int) -> dict[str, set[str]]:
    """A ladder of diamonds: each rung's node points at the next rung's two nodes, and each of those at the rung's
    one node after them, so the bottom node is reached by 2**depth paths."""
    edges: dict[str, set[str]] = {}
    for rung in range(depth):
        edges[f"top{rung}"] = {f"left{rung}", f"right{rung}"}
        edges[f"left{rung}"] = {f"top{rung + 1}"}
        edges[f"right{rung}"] = {f"top{rung + 1}"}
    edges[f"top{depth}"] = set()
    return edges


def reachable(edges: dict[str, set[str]], reads: Counter[str]) -> MemoizedWalk[str, frozenset[str]]:
    """A walk answering, for a node, every node reachable from it, itself included, counting each read."""

    def read(node: str, walk: MemoizedWalk[str, frozenset[str]]) -> frozenset[str]:
        reads[node] += 1
        found = {node}
        for other in sorted(edges[node]):
            found |= walk(other)
        return frozenset(found)

    return MemoizedWalk(read, frozenset())


def test_a_diamond_ladder_reads_each_node_once() -> None:
    edges = diamonds(16)
    reads: Counter[str] = Counter()
    walk = reachable(edges, reads)
    assert walk("top0") == frozenset(edges)
    assert set(reads.values()) == {1}
    assert set(reads) == set(edges)


def test_a_node_asked_again_in_the_same_question_is_its_memoized_answer() -> None:
    edges = diamonds(2)
    reads: Counter[str] = Counter()
    walk = reachable(edges, reads)
    first = walk("left0")
    assert walk("left0") is first
    assert reads["left0"] == 1


def test_a_node_reached_again_while_it_is_read_answers_the_cycle_value() -> None:
    edges = {"a": {"b"}, "b": {"a", "c"}, "c": {"c"}}
    reads: Counter[str] = Counter()
    walk = reachable(edges, reads)
    assert walk("a") == {"a", "b", "c"}
    assert reads == Counter({"a": 1, "b": 1, "c": 1})


def test_each_walk_is_its_own_question() -> None:
    edges = diamonds(1)
    reads: Counter[str] = Counter()
    reachable(edges, reads)("top0")
    reachable(edges, reads)("top0")
    assert set(reads.values()) == {2}


def test_a_read_that_fails_leaves_its_node_unread_so_asking_again_reads_it_again() -> None:
    calls: Counter[str] = Counter()

    def read(node: str, walk: MemoizedWalk[str, int]) -> int:
        calls[node] += 1
        if calls[node] == 1:
            raise LookupError(node)
        return 1

    walk = MemoizedWalk(read, 0)
    with pytest.raises(LookupError):
        walk("x")
    assert walk("x") == 1
    assert walk("x") == 1
    assert calls["x"] == 2


def test_entered_nodes_answer_the_cycle_value_while_the_walk_reads_beneath_them() -> None:
    edges = {"root": {"a"}, "a": {"root", "b"}, "b": set()}
    reads: Counter[str] = Counter()
    walk = reachable(edges, reads)
    with walk.entered("root"):
        assert walk("a") == {"a", "b"}
    assert "root" not in reads
    assert walk("root") == {"root", "a", "b"}


def nested(edges: dict[str, list[str]], reads: Counter[str]) -> MemoizedWalk[str, str]:
    """A walk answering, for a node, its name and its children's answers in brackets: an answer whose order and
    whose inner parts show every cut, as text built by concatenation does."""

    def read(node: str, walk: MemoizedWalk[str, str]) -> str:
        reads[node] += 1
        return node + "(" + ",".join(walk(other) for other in edges[node]) + ")"

    return MemoizedWalk(read, "^")


def per_path(edges: dict[str, list[str]], node: str, path: frozenset[str] = frozenset()) -> str:
    """The same answer from a walk per path, which the memoized walk must give."""
    if node in path:
        return "^"
    return node + "(" + ",".join(per_path(edges, other, path | {node}) for other in edges[node]) + ")"


def test_a_node_read_inside_a_cycle_and_asked_again_from_elsewhere_answers_as_a_walk_per_path() -> None:
    # x and y lead to each other; asked first through x, y is read with x under way and cut, which is not y's answer
    # when y is asked first, nor when another walk's first node asks for both.
    edges = {"r": ["x", "y"], "x": ["y"], "y": ["x"]}
    walk = nested(edges, Counter())
    assert walk("r") == per_path(edges, "r") == "r(x(y(^)),y(x(^)))"
    assert walk("y") == per_path(edges, "y") == "y(x(^))"
    assert walk("x") == per_path(edges, "x") == "x(y(^))"


def test_each_answer_of_a_graph_of_cycles_and_diamonds_is_the_per_path_one() -> None:
    edges = {
        "a": ["b", "c", "d"],
        "b": ["d", "a"],
        "c": ["d"],
        "d": ["e", "b"],
        "e": ["c", "e"],
    }
    walk = nested(edges, Counter())
    for node in ["c", "a", "e", "b", "d", "a"]:
        assert walk(node) == per_path(edges, node)


def test_a_cycle_cut_only_where_it_was_entered_is_read_once_from_each_path_into_it() -> None:
    edges = {"r": ["p", "q"], "p": ["x"], "q": ["x"], "x": ["y", "z"], "y": ["x"], "z": []}
    reads: Counter[str] = Counter()
    walk = nested(edges, reads)
    assert walk("r") == per_path(edges, "r")
    assert reads["z"] == 1
    assert reads["x"] == 1
    assert reads["y"] == 1


def test_nodes_under_one_mark_cut_each_other_whatever_question_each_asks() -> None:
    edges = {("x", 1): [("y", 1)], ("y", 1): [("x", 2)], ("x", 2): [("y", 2)], ("y", 2): []}

    def read(node: tuple[str, int], walk: MemoizedWalk[tuple[str, int], str]) -> str:
        return node[0] + "(" + ",".join(walk(other) for other in edges[node]) + ")"

    walk = MemoizedWalk(read, "^", under=lambda node: node[0])
    assert walk(("x", 1)) == "x(y(^))"
    assert walk(("x", 2)) == "x(y())"


def test_every_answer_of_every_small_graph_asked_in_turn_is_the_per_path_one() -> None:
    # Every graph on three nodes whose each node leads to at most two, in order, repeats allowed: every cycle,
    # self-loop, and diamond such a graph can hold, each asked in an order that reaches nodes kept inside a cycle.
    nodes = "abc"
    choices = [list(chosen) for size in range(3) for chosen in itertools.product(nodes, repeat=size)]
    for lists in itertools.product(choices, repeat=len(nodes)):
        edges = dict(zip(nodes, lists, strict=True))
        walk = nested(edges, Counter())
        for node in "abcba":
            assert walk(node) == per_path(edges, node), edges

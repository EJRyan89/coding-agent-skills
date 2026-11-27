"""Regression tests for the one walk that reads each node once per question."""

from __future__ import annotations

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

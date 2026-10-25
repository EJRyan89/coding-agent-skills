"""Regression tests for the one bound on how deep a reader descends."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nesting_depth


class Refused(ValueError):
    """The caller's own error, which the check raises as given."""


def chain(levels: int) -> object:
    """A list nested `levels` deep, built without recursing."""
    value: list[object] = []
    for _ in range(levels - 1):
        value = [value]
    return value


def items(node: object) -> list[object]:
    return node if isinstance(node, list) else []


class DeeperTests(unittest.TestCase):
    def test_the_limit_is_a_hundred_levels(self) -> None:
        # A pinned contract value: json_reader, xml_reader, and every recursive parser of text share it.
        self.assertEqual(100, nesting_depth.DEPTH_LIMIT)

    def test_each_level_up_to_the_limit_is_entered(self) -> None:
        for depth, entered in ((0, 1), (1, 2), (98, 99), (99, 100)):
            with self.subTest(depth=depth):
                self.assertEqual(entered, nesting_depth.deeper(depth, Refused("unused")))

    def test_a_level_past_the_limit_raises_the_callers_own_error(self) -> None:
        for depth in (100, 101, 5000):
            with self.subTest(depth=depth):
                error = Refused(f"nested past {depth}")
                with self.assertRaises(Refused) as raised:
                    nesting_depth.deeper(depth, error)
                self.assertIs(error, raised.exception)


class NestsPastTests(unittest.TestCase):
    def test_a_value_at_the_limit_does_not_nest_past_it_and_one_level_more_does(self) -> None:
        self.assertFalse(nesting_depth.nests_past(chain(100), items))
        self.assertTrue(nesting_depth.nests_past(chain(101), items))

    def test_a_leaf_is_level_one(self) -> None:
        self.assertFalse(nesting_depth.nests_past("leaf", items))

    def test_the_deepest_branch_counts_wherever_it_sits(self) -> None:
        self.assertTrue(nesting_depth.nests_past(["a", chain(100), "b"], items))
        self.assertFalse(nesting_depth.nests_past(["a", chain(99), chain(99)], items))

    def test_a_value_nested_past_the_recursion_limit_is_measured(self) -> None:
        self.assertTrue(nesting_depth.nests_past(chain(sys.getrecursionlimit() * 10), items))

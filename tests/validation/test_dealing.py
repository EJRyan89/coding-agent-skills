"""Regression tests for tests/validation/dealing.py, the one dealer and the one shard reader."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dealing
from dealing import deal, shard_of


class DealTests(unittest.TestCase):
    def test_the_heaviest_goes_first_onto_the_lightest_shard_and_ties_go_to_the_lowest_index(self) -> None:
        self.assertEqual({"a": 0, "b": 1, "c": 2, "d": 0}, deal(["d", "c", "b", "a"], 3, {}))
        self.assertEqual({"a": 1, "b": 2, "c": 0, "d": 1}, deal(["a", "b", "c", "d"], 3, {"c": 5}))


class ShardTests(unittest.TestCase):
    def test_a_shard_is_an_index_from_one_up_to_its_count(self) -> None:
        self.assertEqual((1, 1), shard_of("1/1"))
        self.assertEqual((2, 4), shard_of("2/4"))
        self.assertEqual((4, 4), shard_of("4/4"))
        for text in ("0/4", "5/4", "2/0", "2", "2/4/8", "-1/4", " 2/4", "a/b", "02/4"):
            with self.subTest(text=text), self.assertRaises(ValueError) as raised:
                shard_of(text)
            self.assertEqual(f"<index>/<count> with 1 <= index <= count, found {text!r}", str(raised.exception))

    def test_the_module_imports_no_pytest(self) -> None:
        # The runner and the policies import it in a process that has not found pytest.
        tree = ast.parse(Path(dealing.__file__).read_text(encoding="utf-8"))
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual({"__future__", "collections.abc", "re"}, imported)

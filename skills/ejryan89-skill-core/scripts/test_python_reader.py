"""Regression tests for the one parse of Python source and literals the scripts share."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from python_reader import CONTENT_ERRORS, CONTENT_READERS, PythonRefused, literal, parse, parse_expression

# A run of unary operators past the parser's stack, which ast.parse raises as a MemoryError on some releases and a
# RecursionError on others, rather than a SyntaxError.
TOO_DEEP = "x = " + "-" * 100_000 + "1"
NESTED = "nests deeper than the Python parser can read"
# The most levels a tree the readers give nests, each node one level below the node that holds it, as
# nesting_depth.DEPTH_LIMIT holds it, written out so that a change to that limit fails here.
LIMIT = 100


def attribute_chain(parts: int) -> str:
    """An expression statement of `parts` attributes: the module, the statement, each attribute, the name, and the
    name's load context each one level, so it nests `parts + 4` levels."""
    return "x" + ".a" * parts + "\n"


def conditional_chain(parts: int) -> str:
    """An assignment of `parts` chained conditional expressions, each in the one before it: the module, the
    assignment, each conditional, the last one's test name, and its load context each one level, so it nests
    `parts + 4` levels."""
    return "y = " + "1 if x else " * parts + "0\n"


def nested_lists(levels: int) -> ast.expr:
    """A list literal's node `levels` lists deep around the constant 1: each list, and the constant beside the
    innermost list's load context, each one level, so it nests `levels + 1` levels."""
    node: ast.expr = ast.Constant(1)
    for _ in range(levels):
        node = ast.List(elts=[node], ctx=ast.Load())
    return node


class ParseTests(unittest.TestCase):
    def test_source_that_parses_is_its_module(self) -> None:
        tree = parse("x = 1\n", "m.py")
        self.assertIsInstance(tree, ast.Module)
        self.assertIsInstance(tree.body[0], ast.Assign)

    def test_a_syntax_error_is_refused_with_its_reason_and_line(self) -> None:
        with self.assertRaises(PythonRefused) as raised:
            parse("x = 1\ndef (\n", "m.py")
        self.assertEqual(2, raised.exception.line)
        self.assertTrue(str(raised.exception).endswith(" at line 2"), str(raised.exception))

    def test_source_nested_past_the_parser_is_refused_not_raised_as_memory_or_recursion(self) -> None:
        with self.assertRaises(PythonRefused) as raised:
            parse(TOO_DEEP, "m.py")
        self.assertEqual(NESTED, str(raised.exception))
        self.assertIsNone(raised.exception.line)

    def test_a_tree_at_the_limit_parses_and_one_level_past_it_is_refused_as_nested(self) -> None:
        # Both nest within the parser's own limits but past what a recursive walk over the tree, such as
        # python_names.qualified_name over an attribute chain, can recurse once they grow.
        for chain in (attribute_chain, conditional_chain):
            with self.subTest(chain=chain.__name__):
                self.assertIsInstance(parse(chain(LIMIT - 4), "m.py"), ast.Module)
                with self.assertRaises(PythonRefused) as raised:
                    parse(chain(LIMIT - 3), "m.py")
                self.assertEqual(NESTED, str(raised.exception))
                self.assertIsNone(raised.exception.line)

    def test_a_tree_the_parser_reads_but_a_walk_cannot_recurse_through_is_refused_as_nested(self) -> None:
        # The parser reads a chain of this length on every supported release; a walk that recurses once per level
        # cannot.
        for source in (
            "import sys\nsys" + ".a" * 1500 + "()\n",
            "def f() -> int:\n    return " + "1 if x else " * 1500 + "0\n",
        ):
            with self.subTest(source=source[:20]), self.assertRaises(PythonRefused) as raised:
                parse(source, "m.py")
            self.assertEqual(NESTED, str(raised.exception))

    def test_an_expression_at_the_limit_parses_and_one_level_past_it_is_refused(self) -> None:
        # The expression node stands in the module's and the statement's place, one level for the two.
        self.assertIsInstance(parse_expression("x" + ".a" * (LIMIT - 3)), ast.Expression)
        with self.assertRaises(PythonRefused) as raised:
            parse_expression("x" + ".a" * (LIMIT - 2))
        self.assertEqual(NESTED, str(raised.exception))

    def test_a_null_byte_is_refused(self) -> None:
        with self.assertRaises(PythonRefused):
            parse("x = 1\0\n")

    def test_an_expression_parses_and_a_statement_is_refused(self) -> None:
        self.assertIsInstance(parse_expression("a + b"), ast.Expression)
        with self.assertRaises(PythonRefused):
            parse_expression("x = 1")
        with self.assertRaises(PythonRefused) as raised:
            parse_expression("-" * 100_000 + "1")
        self.assertEqual(NESTED, str(raised.exception))


class LiteralTests(unittest.TestCase):
    def test_a_literal_node_and_a_literal_source_read_as_their_values(self) -> None:
        self.assertEqual({"a": (1, 2)}, literal("{'a': (1, 2)}"))
        self.assertEqual([1], literal(ast.parse("[1]", mode="eval").body))

    def test_what_is_no_literal_is_refused(self) -> None:
        for value in ("f(x)", "{[1]: 2}", "{[1]}", "(", "[" * 100_000):
            with self.subTest(value=value[:10]), self.assertRaises(PythonRefused):
                literal(value)

    def test_a_literal_source_at_the_limit_reads_and_one_level_past_it_is_refused(self) -> None:
        # The expression, each list, and the innermost list's load context each one level.
        # Spaces and tabs before it are passed over, as ast.literal_eval passes them.
        self.assertIsInstance(literal(" \t" + "[" * (LIMIT - 2) + "]" * (LIMIT - 2)), list)
        with self.assertRaises(PythonRefused) as raised:
            literal("[" * (LIMIT - 1) + "]" * (LIMIT - 1))
        self.assertEqual(NESTED, str(raised.exception))

    def test_a_literal_node_at_the_limit_reads_and_one_level_past_it_is_refused(self) -> None:
        self.assertIsInstance(literal(nested_lists(LIMIT - 1)), list)
        with self.assertRaises(PythonRefused) as raised:
            literal(nested_lists(LIMIT))
        self.assertEqual(NESTED, str(raised.exception))

    def test_the_family_is_one_value_error_class(self) -> None:
        self.assertEqual((PythonRefused,), CONTENT_ERRORS)
        self.assertTrue(issubclass(PythonRefused, ValueError))

    def test_the_readers_that_raise_the_family_are_named(self) -> None:
        # The content-errors policy holds each caller of a reader this names to the family, so a name left out is a
        # caller left unheld.
        self.assertEqual(("parse", "parse_expression", "literal"), CONTENT_READERS)

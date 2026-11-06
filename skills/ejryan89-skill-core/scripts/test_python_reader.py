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

    def test_the_family_is_one_value_error_class(self) -> None:
        self.assertEqual((PythonRefused,), CONTENT_ERRORS)
        self.assertTrue(issubclass(PythonRefused, ValueError))

    def test_the_readers_that_raise_the_family_are_named(self) -> None:
        # The content-errors policy holds each caller of a reader this names to the family, so a name left out is a
        # caller left unheld.
        self.assertEqual(("parse", "parse_expression", "literal"), CONTENT_READERS)

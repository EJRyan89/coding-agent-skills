"""Regression tests for the one reading of what ends a Python program with a code."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import exit_calls


def argument(source: str) -> ast.expr | None:
    """The first argument of the call `source` is, or None when it has none."""
    call = ast.parse(source, mode="eval").body
    assert isinstance(call, ast.Call)
    return call.args[0] if call.args else None


class ExitCallTests(unittest.TestCase):
    def test_the_calls_are_sys_exit_exit_quit_os_exit_and_system_exit(self) -> None:
        self.assertEqual(
            {"sys.exit", "exit", "quit", "builtins.exit", "builtins.quit", "os._exit", "SystemExit"},
            exit_calls.EXIT_CALLS,
        )

    def test_os_exit_takes_its_code_as_status_and_every_other_call_as_code(self) -> None:
        for function, keyword in (
            ("os._exit", "status"),
            ("sys.exit", "code"),
            ("quit", "code"),
            ("builtins.exit", "code"),
        ):
            with self.subTest(function=function):
                self.assertEqual(keyword, exit_calls.code_keyword(function))

    def test_no_code_or_none_exits_0_a_message_exits_1_and_anything_else_is_left_to_the_caller(self) -> None:
        for source, code in (
            ("exit()", 0),
            ("exit(None)", 0),
            ("exit('stale plan')", 1),
            ("exit(3)", None),
            ("exit(REFUSED)", None),
        ):
            with self.subTest(source=source):
                self.assertEqual(code, exit_calls.fixed_code(argument(source)))

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

    def test_os_exit_takes_its_code_as_status_sys_exit_and_system_exit_by_position_alone_and_the_rest_as_code(
        self,
    ) -> None:
        # Pinned against what Python 3.12 and later do: sys.exit's parameter is positional-only, and SystemExit takes
        # no keyword, so either given code= raises TypeError.
        for function, keyword in (
            ("os._exit", "status"),
            ("sys.exit", None),
            ("SystemExit", None),
            ("exit", "code"),
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

    def test_a_call_python_refuses_for_its_arguments_exits_1_and_any_other_reads_its_code_argument(self) -> None:
        # Each code below is what the call exits with when Python runs it, a TypeError uncaught exiting 1.
        for source, function, code in (
            ("SystemExit(code=19)", "SystemExit", 1),
            ("sys.exit(code=19)", "sys.exit", 1),
            ("sys.exit(status=19)", "sys.exit", 1),
            ("exit(status=19)", "exit", 1),
            ("os._exit(code=19)", "os._exit", 1),
            ("exit(3, code=4)", "exit", 1),
            ("sys.exit(3, 4)", "sys.exit", 1),
            ("SystemExit(3, 4)", "SystemExit", 1),
            ("exit(code=19)", "exit", None),
            ("os._exit(status=19)", "os._exit", None),
            ("sys.exit(19)", "sys.exit", None),
            ("sys.exit()", "sys.exit", 0),
            ("exit(code=None)", "exit", 0),
            ("quit(code='stale')", "quit", 1),
        ):
            with self.subTest(source=source):
                call = ast.parse(source, mode="eval").body
                assert isinstance(call, ast.Call)
                self.assertEqual(code, exit_calls.call_code(call, function))

    def test_the_code_argument_is_the_first_positional_or_the_keyword_and_an_unpacking_is_unread(self) -> None:
        for source, function, expected in (
            ("sys.exit(19)", "sys.exit", "19"),
            ("exit(code=19)", "exit", "19"),
            ("os._exit(status=19)", "os._exit", "19"),
            ("exit(*codes)", "exit", "*codes"),
            ("exit(**options)", "exit", "*options"),
            ("sys.exit(code=19)", "sys.exit", None),
            ("exit()", "exit", None),
        ):
            with self.subTest(source=source):
                call = ast.parse(source, mode="eval").body
                assert isinstance(call, ast.Call)
                found = exit_calls.exit_argument(call, function)
                self.assertEqual(expected, None if found is None else ast.unparse(found))

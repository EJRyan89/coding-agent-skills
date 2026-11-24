"""Regression tests for the one reading of the number a name stands for when a program exits with it."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import exit_constants
from python_names import import_aliases


def constants(source: str) -> exit_constants.ModuleConstants:
    tree = ast.parse(source)
    return exit_constants.read(tree, import_aliases(tree))


def expression(source: str) -> ast.expr:
    return ast.parse(source, mode="eval").body


class ReadTests(unittest.TestCase):
    def test_a_plain_chained_annotated_or_final_assignment_of_an_int_literal_is_a_constant(self) -> None:
        found = constants(
            "from typing import Final\n"
            "PLAIN = 1\nFIRST = SECOND = 2\nANNOTATED: int = 3\nFINAL: Final = 4\nTYPED: Final[int] = 5\n"
        )
        self.assertEqual(
            {
                "PLAIN": frozenset({1}),
                "FIRST": frozenset({2}),
                "SECOND": frozenset({2}),
                "ANNOTATED": frozenset({3}),
                "FINAL": frozenset({4}),
                "TYPED": frozenset({5}),
            },
            found.values,
        )
        self.assertEqual(frozenset(), found.unread)

    def test_a_constant_bound_in_a_module_level_block_is_read_and_each_branch_gives_its_value(self) -> None:
        found = constants(
            "import sys\n"
            "if sys.platform == 'win32':\n    STOP = 3\nelse:\n    STOP = 4\n"
            "try:\n    import fast\nexcept ImportError:\n    MISSING = 5\nfinally:\n    DONE = 6\n"
            "with open('x') as handle:\n    HELD = 7\n"
            "while False:\n    LOOPED = 8\n"
            "match sys.argv:\n    case []:\n        MATCHED = 9\n"
        )
        self.assertEqual(
            {
                "STOP": frozenset({3, 4}),
                "MISSING": frozenset({5}),
                "DONE": frozenset({6}),
                "HELD": frozenset({7}),
                "LOOPED": frozenset({8}),
                "MATCHED": frozenset({9}),
            },
            found.values,
        )

    def test_a_later_binding_in_one_block_replaces_an_earlier_one_as_python_binds_it(self) -> None:
        # Every literal binding was unioned, so STALE = 3 then STALE = 4 read as 3 or 4 where Python leaves 4.
        cases = {
            "at the top": ("STALE = 3\nSTALE = 4\n", {4}),
            "in one if arm": ("if X:\n    STALE = 3\n    STALE = 4\nelse:\n    STALE = 5\n", {4, 5}),
            "in an else arm": ("if X:\n    STALE = 5\nelse:\n    STALE = 3\n    STALE = 4\n", {4, 5}),
            "in one match case": ("match X:\n    case 1:\n        STALE = 3\n        STALE = 4\n", {4}),
            "in a try's else": ("try:\n    pass\nexcept E:\n    pass\nelse:\n    STALE = 3\n    STALE = 4\n", {4}),
            "in a try's handler": ("try:\n    pass\nexcept E:\n    STALE = 3\n    STALE = 4\n", {4}),
            "after an if": ("if X:\n    STALE = 3\nelse:\n    STALE = 5\nSTALE = 4\n", {4}),
            "an if with no else": ("STALE = 3\nif X:\n    STALE = 4\n", {3, 4}),
            "a loop's body": ("STALE = 3\nwhile X:\n    STALE = 4\n", {3, 4}),
            "a loop's else, which a break may skip": (
                "STALE = 3\nfor x in y:\n    pass\nelse:\n    STALE = 4\n",
                {3, 4},
            ),
            "a try's paths": (
                "STALE = 1\ntry:\n    STALE = 2\n    STALE = 3\nexcept E:\n    pass\nelse:\n    STALE = 4\n",
                {1, 2, 3, 4},
            ),
            "a try's finally": ("try:\n    STALE = 2\nexcept E:\n    STALE = 3\nfinally:\n    STALE = 4\n", {4}),
            "a match with no case taken": ("STALE = 3\nmatch X:\n    case 1:\n        STALE = 4\n", {3, 4}),
            "a with block, which may suppress an error": ("STALE = 3\nwith X:\n    STALE = 4\n", {3, 4}),
        }
        for case, (source, values) in cases.items():
            with self.subTest(case):
                self.assertEqual({"STALE": frozenset(values)}, constants(source).values)

    def test_a_block_that_may_stop_partway_keeps_every_value_python_can_leave(self) -> None:
        # Reading a loop, a try's body, or a with's body as run to its end left out values a break, a continue, an
        # error a handler or a context manager catches, or an inner handler's error leave, so a code the program exits
        # with went unread. Each set is what running the probe on every path leaves; the reader may hold more, never
        # less. X() is any truth value, R() may raise E, IT() yields any number of items, and SUP() suppresses E.
        cases = {
            "a break after a binding": ("C = 3\nwhile True:\n    C = 4\n    if X():\n        break\n    C = 5\n", {4}),
            "a for's break": ("C = 3\nfor _ in IT():\n    C = 4\n    if X():\n        break\n    C = 5\n", {3, 4, 5}),
            "a continue": ("C = 3\nfor _ in IT():\n    C = 4\n    if X():\n        continue\n    C = 5\n", {3, 4, 5}),
            "a for's else a break skips": (
                "C = 3\nfor _ in IT():\n    C = 4\n    if X():\n        break\nelse:\n    C = 5\n",
                {4, 5},
            ),
            "a continue in a loop's handler": (
                "C = 1\nfor _ in IT():\n    try:\n        C = 2\n        R()\n        C = 3\n    except E:\n"
                "        continue\n    C = 4\n",
                {1, 2, 4},
            ),
            "a break in a loop's handler": (
                "C = 1\nwhile True:\n    try:\n        C = 2\n        R()\n    except E:\n        break\n"
                "    C = 3\n    break\n",
                {2, 3},
            ),
            "an error inside an if of a try's body": (
                "C = 1\ntry:\n    if X():\n        C = 4\n        R()\n        C = 5\nexcept E:\n    pass\n",
                {1, 4, 5},
            ),
            "an error inside a with of a try's body": (
                "C = 1\ntry:\n    with CM():\n        C = 4\n        R()\n        C = 5\nexcept E:\n    pass\n",
                {4, 5},
            ),
            "an error inside a for of a try's body": (
                "C = 1\ntry:\n    for _ in IT():\n        C = 4\n        R()\n        C = 5\nexcept E:\n    pass\n",
                {1, 4, 5},
            ),
            "an inner handler that raises to an outer try": (
                "C = 1\ntry:\n    try:\n        C = 2\n        R()\n        C = 3\n    except E:\n        C = 4\n"
                "        R()\n        C = 5\nexcept E:\n    pass\n",
                {3, 4, 5},
            ),
            "a with that suppresses an error": ("C = 3\nwith SUP():\n    R()\n    C = 4\n", {3, 4}),
            "an except star": ("C = 1\ntry:\n    C = 2\n    R()\n    C = 3\nexcept* E:\n    pass\n", {2, 3}),
            "an error no handler catches, which the finally sees": (
                "C = 1\ntry:\n    C = 2\n    R()\nexcept E:\n    C = 4\nelse:\n    C = 5\nfinally:\n    pass\n",
                {2, 4, 5},
            ),
        }
        for case, (source, python) in cases.items():
            with self.subTest(case):
                found = constants(source).values.get("C", frozenset())
                self.assertLessEqual(python, found, "a value Python can leave is missing")

    def test_an_annotation_alone_binds_nothing_and_a_bool_or_other_value_is_no_constant(self) -> None:
        found = constants("DECLARED: int\nFLAG = True\nTEXT = 'x'\nSUM = 2 + 1\n")
        self.assertEqual({}, found.values)
        self.assertEqual(frozenset({"FLAG", "TEXT", "SUM"}), found.unread)

    def test_a_name_any_other_binding_reaches_is_unread_whatever_literal_another_gives_it(self) -> None:
        cases = {
            "unpacked": "A = 1\nA, B = 2, 3\n",
            "augmented": "A = 1\nA += 1\n",
            "a loop target": "A = 1\nfor A in range(3):\n    pass\n",
            "a with target": "A = 1\nwith open('x') as A:\n    pass\n",
            "a walrus": "A = 1\nif (A := 2):\n    pass\n",
            "a walrus in a comprehension": "A = 1\n[(A := n) for n in range(2)]\n",
            "deleted": "A = 1\ndel A\n",
            "an except name": "A = 1\ntry:\n    pass\nexcept OSError as A:\n    pass\n",
            "a match capture": "A = 1\nmatch 2:\n    case A:\n        pass\n",
            "a match star": "A = 1\nmatch []:\n    case [*A]:\n        pass\n",
            "a match rest": "A = 1\nmatch {}:\n    case {**A}:\n        pass\n",
            "a function": "A = 1\ndef A():\n    pass\n",
            "a class": "A = 1\nclass A:\n    pass\n",
            "a type statement": "A = 1\ntype A = int\n",
            "a type statement in a block": "A = 1\nif ready:\n    type A = int\n",
            "declared global": "A = 1\ndef rebind():\n    global A\n    A = 2\n",
            "imported too": "A = 1\ntry:\n    from fast import A\nexcept ImportError:\n    pass\n",
            "a loop target before the literal": "for A in range(3):\n    pass\nA = 1\n",
        }
        for case, source in cases.items():
            with self.subTest(case):
                found = constants(source)
                self.assertNotIn("A", found.values)
                self.assertIn("A", found.unread)

    def test_a_function_s_or_a_class_s_own_binding_or_a_comprehension_s_target_is_not_the_module_s(self) -> None:
        found = constants(
            "A = 1\n"
            "def local():\n    A = 2\n    B = 3\n"
            "class Holder:\n    A = 4\n"
            "handler = lambda A=5: A\n"
            "[A for A in range(2)]\n"
            # A type parameter binds in its own annotation scope, never the module's.
            "def generic[A](): pass\n"
            "class Generic[*A]: pass\n"
            "type Alias[**A] = int\n"
        )
        self.assertEqual({"A": frozenset({1})}, found.values)
        self.assertNotIn("B", found.unread)


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.modules = {
            "codes": constants("from typing import Final\nUSAGE: Final = 2\nREFUSED = 5\nworked = len('x')\n"),
            "relay": constants("from codes import USAGE\nfrom codes import REFUSED as STOPPED\n"),
            "pkg.codes": constants("HELD = 6\n"),
            "loop.first": constants("from loop.second import CODE\n"),
            "loop.second": constants("from loop.first import CODE\n"),
        }
        self.located: list[str] = []

    def resolve(self, module: str, source: str) -> frozenset[int] | None:
        self.located = []
        return exit_constants.resolve(expression(source), constants(module), self.locate)

    def locate(self, dotted: str) -> exit_constants.ModuleConstants | None:
        # A bound on the lookups, so a cycle the loop fails to stop fails the test rather than hanging it.
        self.located.append(dotted)
        if len(self.located) > len(self.modules):
            raise AssertionError(f"the cycle was not stopped: {self.located}")
        return self.modules.get(dotted)

    def test_a_literal_or_a_name_of_the_module_s_own_constants_is_its_values(self) -> None:
        self.assertEqual(frozenset({7}), self.resolve("", "7"))
        self.assertEqual(frozenset({3, 4}), self.resolve("if X:\n    STOP = 3\nelse:\n    STOP = 4\n", "STOP"))

    def test_a_constant_an_import_names_is_read_in_the_module_located_by_its_dotted_name(self) -> None:
        self.assertEqual(frozenset({2}), self.resolve("from codes import USAGE\n", "USAGE"))
        self.assertEqual(frozenset({5}), self.resolve("from codes import REFUSED as NO\n", "NO"))
        self.assertEqual(frozenset({2}), self.resolve("import codes\n", "codes.USAGE"))
        self.assertEqual(frozenset({2}), self.resolve("import codes as c\n", "c.USAGE"))
        self.assertEqual(frozenset({6}), self.resolve("import pkg.codes\n", "pkg.codes.HELD"))

    def test_a_constant_a_module_re_exports_is_followed_to_the_module_that_binds_it(self) -> None:
        self.assertEqual(frozenset({2}), self.resolve("from relay import USAGE\n", "USAGE"))
        self.assertEqual(frozenset({5}), self.resolve("import relay\n", "relay.STOPPED"))

    def test_a_re_export_cycle_ends_after_each_module_is_located_once(self) -> None:
        self.assertIsNone(self.resolve("from loop.first import CODE\n", "CODE"))
        self.assertEqual(["loop.first", "loop.second"], self.located)

    def test_a_name_it_cannot_read_is_none_never_a_number(self) -> None:
        cases = {
            "an undefined or builtin name": ("", "CODE"),
            "a name bound otherwise": ("CODE = len('x')\n", "CODE"),
            "a module the locator does not find": ("from elsewhere import CODE\n", "CODE"),
            "a name the found module binds otherwise": ("from codes import worked\n", "worked"),
            "a name the found module does not bind": ("from codes import ABSENT\n", "ABSENT"),
            "a relative import": ("from .codes import USAGE\n", "USAGE"),
            "an attribute of a value": ("", "result.returncode"),
            "a call": ("", "main()"),
            "a re-export cycle": ("from loop.first import CODE\n", "CODE"),
            "a constant shadowed by a later import": ("USAGE = 9\nfrom codes import USAGE\n", "USAGE"),
        }
        for case, (module, source) in cases.items():
            with self.subTest(case):
                self.assertIsNone(self.resolve(module, source))

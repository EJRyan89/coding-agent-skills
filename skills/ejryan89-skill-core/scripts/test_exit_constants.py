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
            "declared global": "A = 1\ndef rebind():\n    global A\n    A = 2\n",
            "imported too": "A = 1\ntry:\n    from fast import A\nexcept ImportError:\n    pass\n",
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
        )
        self.assertEqual({"A": frozenset({1})}, found.values)
        self.assertNotIn("B", found.unread)


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.modules = {
            "codes": constants("from typing import Final\nUSAGE: Final = 2\nREFUSED = 5\nworked = len('x')\n"),
            "relay": constants("from codes import USAGE\nfrom codes import REFUSED as STOPPED\n"),
            "loop.first": constants("from loop.second import CODE\n"),
            "loop.second": constants("from loop.first import CODE\n"),
        }

    def resolve(self, module: str, source: str) -> frozenset[int] | None:
        return exit_constants.resolve(expression(source), constants(module), self.modules.get)

    def test_a_literal_or_a_name_of_the_module_s_own_constants_is_its_values(self) -> None:
        self.assertEqual(frozenset({7}), self.resolve("", "7"))
        self.assertEqual(frozenset({3, 4}), self.resolve("if X:\n    STOP = 3\nelse:\n    STOP = 4\n", "STOP"))

    def test_a_constant_an_import_names_is_read_in_the_module_located_by_its_dotted_name(self) -> None:
        self.assertEqual(frozenset({2}), self.resolve("from codes import USAGE\n", "USAGE"))
        self.assertEqual(frozenset({5}), self.resolve("from codes import REFUSED as NO\n", "NO"))
        self.assertEqual(frozenset({2}), self.resolve("import codes\n", "codes.USAGE"))
        self.assertEqual(frozenset({2}), self.resolve("import codes as c\n", "c.USAGE"))

    def test_a_constant_a_module_re_exports_is_followed_to_the_module_that_binds_it(self) -> None:
        self.assertEqual(frozenset({2}), self.resolve("from relay import USAGE\n", "USAGE"))
        self.assertEqual(frozenset({5}), self.resolve("import relay\n", "relay.STOPPED"))

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

"""Regression tests for the one reader of what a Python module's names stand for through its imports."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from python_names import (
    LITERAL_DISPLAYS,
    REFUSED_CALLEES,
    ModuleImport,
    chosen_callables,
    chosen_callee,
    import_aliases,
    module_imports,
    qualified_name,
    type_parameter_names,
)

# A function each form chosen_callee reads chooses by a value, by the form's ast node, each among print and os.system.
CHOSEN_SAMPLES = {
    "BoolOp": ["(os.system or print)", "(print and (os.system or print))"],
    "IfExp": ["(print if quiet else os.system)", "((run := os.system) if quiet else print)"],
    "NamedExpr": ["(run := os.system if quiet else print)", "(run := (print or os.system))"],
    "Tuple": ["(print, os.system)[1]"],
    "List": ["[print, (os.system if quiet else print)][0]"],
    "Set": ["{os.system, print}[0]"],
    "Dict": ["{'run': os.system, 'log': print}['run']"],
}


class PythonNameTests(unittest.TestCase):
    def test_each_type_parameter_of_a_function_a_class_or_a_type_statement_is_named_and_nothing_else_has_one(
        self,
    ) -> None:
        for source, names in (
            ("def f[T, *Ts, **P](): pass", ["T", "Ts", "P"]),
            ("async def f[T: int](): pass", ["T"]),
            ("class C[K, V]: pass", ["K", "V"]),
            ("type Pair[A] = tuple[A, A]", ["A"]),
            ("type Plain = int", []),
            ("def f(): pass", []),
            ("x = 1", []),
        ):
            with self.subTest(source=source):
                self.assertEqual(names, type_parameter_names(ast.parse(source).body[0]))

    def test_each_absolute_import_binds_its_names_and_a_relative_one_is_left_out(self) -> None:
        tree = ast.parse("import a.b as c\nimport os.path\nfrom sys import exit as leave, argv\nfrom . import local\n")
        self.assertEqual(
            [
                ModuleImport(1, "a.b", (("c", "a.b"),)),
                ModuleImport(2, "os.path", (("os", "os"),)),
                ModuleImport(3, "sys", (("leave", "sys.exit"), ("argv", "sys.argv"))),
            ],
            module_imports(tree),
        )
        self.assertEqual({"c": "a.b", "os": "os", "leave": "sys.exit", "argv": "sys.argv"}, import_aliases(tree))

    def test_a_name_resolves_through_the_imports_and_anything_else_stands_for_itself_or_nothing(self) -> None:
        aliases = {"system": "sys", "leave": "os._exit"}
        for source, name in (
            ("system.exit", "sys.exit"),
            ("leave", "os._exit"),
            ("smtp.quit", "smtp.quit"),
            ("exit", "exit"),
            ("make().quit", ""),
        ):
            with self.subTest(source=source):
                self.assertEqual(name, qualified_name(ast.parse(source, mode="eval").body, aliases))

    def test_every_form_chosen_callee_reads_has_a_sample(self) -> None:
        displays = {display.__name__ for display in LITERAL_DISPLAYS}
        self.assertEqual(sorted({*REFUSED_CALLEES, *displays}), sorted(CHOSEN_SAMPLES))

    def test_each_form_that_chooses_a_function_by_a_value_reads_as_the_callables_it_chooses_among(self) -> None:
        for form, samples in CHOSEN_SAMPLES.items():
            for sample in samples:
                with self.subTest(sample=sample):
                    function = ast.parse(sample, mode="eval").body
                    held = function.value if isinstance(function, ast.Subscript) else function
                    self.assertEqual(form, type(held).__name__)
                    self.assertNotEqual("", chosen_callee(function))
                    read = sorted(ast.unparse(callable_) for callable_ in chosen_callables(function))
                    self.assertEqual(["os.system", "print"], sorted(set(read)))

    def test_an_attribute_of_an_assignment_expression_s_value_reads_as_that_attribute_of_each_value(self) -> None:
        for source, read in (
            ("(fs := shutil).rmtree", ["shutil.rmtree"]),
            ("(fs := shutil).os.remove", ["shutil.os.remove"]),
            ("(fs := shutil if quiet else os).rmtree", ["shutil.rmtree", "os.rmtree"]),
        ):
            with self.subTest(source=source):
                function = ast.parse(source, mode="eval").body
                self.assertEqual(
                    "calls an attribute of the value an assignment expression binds", chosen_callee(function)
                )
                self.assertEqual(read, [ast.unparse(callable_) for callable_ in chosen_callables(function)])

    def test_a_function_no_value_chooses_is_its_own_one_callable_and_a_starred_item_stands_as_itself(self) -> None:
        for source in ("os.system", "run", "make()", "KINDS[kind]", "(text or '').strip", "[[f]][0][0]"):
            with self.subTest(source=source):
                function = ast.parse(source, mode="eval").body
                self.assertEqual("", chosen_callee(function))
                self.assertEqual([function], chosen_callables(function))
        starred = ast.parse("[*fs, {**more}][0]", mode="eval").body
        self.assertEqual(["*fs", "{**more}"], [ast.unparse(callable_) for callable_ in chosen_callables(starred)])

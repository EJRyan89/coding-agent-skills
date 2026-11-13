"""Regression tests for the one reader of what a Python module's names stand for through its imports."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from python_names import ModuleImport, import_aliases, module_imports, qualified_name


class PythonNameTests(unittest.TestCase):
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

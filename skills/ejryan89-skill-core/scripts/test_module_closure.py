"""Regression tests for the one builder of a throwaway copy of ejryan89-skill-core's modules, which copies what each
module imports from the source rather than from a list a contributor keeps."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from module_closure import SCRIPTS, closure, copy_with_imports, imported_names, imports_of


class ModuleClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="module closure ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scripts = self.root / "source scripts"
        self.scripts.mkdir()

    def module(self, name: str, source: str) -> None:
        (self.scripts / f"{name}.py").write_text(source, encoding="utf-8")

    def test_a_new_import_of_a_copied_module_is_copied_without_any_list_edited(self) -> None:
        self.module("entry", "import json\nfrom first import VALUE\n")
        self.module("first", 'VALUE = "first"\n')
        self.module("second", 'VALUE = "second"\n')
        self.module("unrelated", "")
        self.assertEqual(["entry", "first"], copy_with_imports(self.root / "before", "entry", scripts=self.scripts))
        # The change that broke each hand-kept list: a copied module starts importing another shared module.
        self.module("first", 'from second import VALUE as SECOND\nVALUE = "first " + SECOND\n')
        built = self.root / "built tree"
        self.assertEqual(["entry", "first", "second"], copy_with_imports(built, "entry", scripts=self.scripts))
        self.assertEqual({"entry.py", "first.py", "second.py"}, {path.name for path in built.iterdir()})
        result = subprocess.run(
            [sys.executable, "-B", "-c", "from entry import VALUE; print(VALUE)"],
            capture_output=True,
            text=True,
            cwd=built,
            check=False,
        )
        self.assertEqual((0, "first second\n"), (result.returncode, result.stdout), result.stderr)

    def test_every_form_of_import_counts_and_the_standard_library_is_not_copied(self) -> None:
        self.module(
            "entry",
            "import importlib\nimport os.path\n"
            "def late():\n    import inner\n    from dotted.part import name\n"
            'loaded = importlib.import_module("loaded")\nbuilt_in = __import__("builtin_loaded")\n'
            "from . import relative\n",
        )
        for name in ("inner", "dotted", "loaded", "builtin_loaded", "relative"):
            self.module(name, "")
        self.assertEqual(
            {"importlib", "os", "inner", "dotted", "loaded", "builtin_loaded"},
            imported_names((self.scripts / "entry.py").read_text(encoding="utf-8")),
        )
        self.assertEqual(
            ["builtin_loaded", "dotted", "entry", "inner", "loaded"], closure("entry", scripts=self.scripts)
        )

    def test_a_script_outside_the_directory_names_what_it_imports_from_it(self) -> None:
        self.module("first", "import second\n")
        self.module("second", "")
        script = self.root / "other skill" / "run.py"
        script.parent.mkdir()
        script.write_text("import json\nfrom first import VALUE\nimport elsewhere\n", encoding="utf-8")
        self.assertEqual(["first"], imports_of(script, scripts=self.scripts))
        built = self.root / "built tree"
        self.assertEqual(
            ["first", "second"],
            copy_with_imports(built, *imports_of(script, scripts=self.scripts), scripts=self.scripts),
        )

    def test_a_cycle_ends_and_a_missing_module_is_refused(self) -> None:
        self.module("one", "import two\n")
        self.module("two", "import one\n")
        self.assertEqual(["one", "two"], closure("two", scripts=self.scripts))
        with self.assertRaises(FileNotFoundError):
            closure("absent", scripts=self.scripts)

    def test_the_default_source_is_ejryan89_skill_core_scripts(self) -> None:
        self.assertEqual(Path(__file__).resolve().parent, SCRIPTS)
        self.assertIn("flat_text", closure("console"))

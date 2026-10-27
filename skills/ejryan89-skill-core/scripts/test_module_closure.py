"""Regression tests for the one builder of a throwaway copy of ejryan89-skill-core's modules, which copies what each
module imports from the source rather than from a list a contributor keeps."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from module_closure import SCRIPTS, closure, copy_with_imports, imported_names, imports_of

# A comment every module here opens with, whose UTF-8 holds byte 0x81, which neither ASCII nor Windows' code page 1252
# decodes, so a read in the locale's encoding instead of UTF-8 fails outside UTF-8 mode.
UNDECODABLE_IN_ANSI = "# ā\n"
# An interpreter whose locale encoding is not UTF-8: UTF-8 mode off, and on Unix the C locale, not coerced to UTF-8.
LOCALE_ENCODING_ENVIRONMENT = {"PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C"}


class ModuleClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="module closure ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scripts = self.root / "source scripts"
        self.scripts.mkdir()

    def module(self, name: str, source: str) -> None:
        (self.scripts / f"{name}.py").write_text(UNDECODABLE_IN_ANSI + source, encoding="utf-8")

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
            encoding="utf-8",
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
            'part = importlib.import_module("dotted_loaded.part")\n'
            # A call given a string that loads no module, and one whose callee has no name of its own, import nothing.
            'notes = open("notes")\nhandlers[0]("handled")\n'
            "from . import relative\n",
        )
        for name in ("inner", "dotted", "loaded", "builtin_loaded", "dotted_loaded", "notes", "handled", "relative"):
            self.module(name, "")
        self.assertEqual(
            {"importlib", "os", "inner", "dotted", "loaded", "builtin_loaded", "dotted_loaded"},
            imported_names((self.scripts / "entry.py").read_text(encoding="utf-8")),
        )
        self.assertEqual(
            ["builtin_loaded", "dotted", "dotted_loaded", "entry", "inner", "loaded"],
            closure("entry", scripts=self.scripts),
        )

    def test_a_script_outside_the_directory_names_what_it_imports_from_it(self) -> None:
        self.module("first", "import second\n")
        self.module("second", "")
        script = self.root / "other skill" / "run.py"
        script.parent.mkdir()
        script.write_text(
            UNDECODABLE_IN_ANSI + "import json\nfrom first import VALUE\nimport elsewhere\n", encoding="utf-8"
        )
        self.assertEqual(["first"], imports_of(script, scripts=self.scripts))
        built = self.root / "built tree"
        self.assertEqual(
            ["first", "second"],
            copy_with_imports(built, *imports_of(script, scripts=self.scripts), scripts=self.scripts),
        )

    def test_a_cycle_ends_and_a_missing_module_is_refused(self) -> None:
        self.module("one", "import two\n")
        self.module("two", "import one\n")
        # In a child with a time limit, so a walk that never ends fails this test rather than hanging the suite.
        program = (
            "import sys\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n"
            "from module_closure import closure\nprint(closure('two', scripts=Path(sys.argv[2])))\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", program, str(SCRIPTS), str(self.scripts)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
        self.assertEqual((0, "['one', 'two']\n"), (result.returncode, result.stdout), result.stderr)
        with self.assertRaises(FileNotFoundError):
            closure("absent", scripts=self.scripts)

    def test_modules_are_read_as_utf_8_whatever_the_locale_encoding(self) -> None:
        self.module("entry", "import first\n")
        self.module("first", "")
        script = self.root / "run.py"
        script.write_text(UNDECODABLE_IN_ANSI + "import first\n", encoding="utf-8")
        program = (
            "import sys\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n"
            "from module_closure import closure, imports_of\n"
            "scripts = Path(sys.argv[2])\n"
            "print(closure('entry', scripts=scripts), imports_of(Path(sys.argv[3]), scripts=scripts))\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", program, str(SCRIPTS), str(self.scripts), str(script)],
            capture_output=True,
            text=True,
            # The child writes in the locale's encoding, which its environment turns UTF-8 mode off to keep.
            encoding="locale",
            env={**os.environ, **LOCALE_ENCODING_ENVIRONMENT},
            check=False,
        )
        self.assertEqual((0, "['entry', 'first'] ['first']\n"), (result.returncode, result.stdout), result.stderr)

    def test_the_default_source_is_ejryan89_skill_core_scripts(self) -> None:
        self.assertEqual(Path(__file__).resolve().parent, SCRIPTS)
        self.assertIn("flat_text", closure("console"))

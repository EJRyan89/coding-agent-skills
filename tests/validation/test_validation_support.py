"""Fixture tests for the import reader in tests/validation/validation_support.py, which every policy that reads what a
module imports shares.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from validation_support import ModuleImport, import_aliases, module_imports, qualified_name

SOURCE = (
    "import os.path\n"
    "import subprocess as sp, json\n"
    "from github_client import GitHubClient as Client, GitHubError\n"
    "from . import sibling\n"
    'TEXT = "import hidden"\n'
    "def load():\n"
    "    import bounded_process\n"
)


class ImportReaderFixtures(unittest.TestCase):
    def test_module_imports_reads_every_absolute_import_and_no_text(self) -> None:
        self.assertEqual(
            [
                ModuleImport(1, "os.path", (("os", "os"),)),
                ModuleImport(2, "subprocess", (("sp", "subprocess"),)),
                ModuleImport(2, "json", (("json", "json"),)),
                ModuleImport(
                    3,
                    "github_client",
                    (("Client", "github_client.GitHubClient"), ("GitHubError", "github_client.GitHubError")),
                ),
                ModuleImport(7, "bounded_process", (("bounded_process", "bounded_process"),)),
            ],
            module_imports(ast.parse(SOURCE)),
        )

    def test_names_resolve_through_the_imports_that_bind_them(self) -> None:
        tree = ast.parse(SOURCE)
        aliases = import_aliases(tree)
        self.assertEqual("os", aliases["os"])
        calls = {"sp.run": "subprocess.run", "Client": "github_client.GitHubClient", "os.path.join": "os.path.join"}
        for written, resolved in calls.items():
            self.assertEqual(resolved, qualified_name(ast.parse(written, mode="eval").body, aliases))
        # A name no import binds stands for itself, and an expression that is no dotted name stands for nothing.
        self.assertEqual("run", qualified_name(ast.parse("run", mode="eval").body, aliases))
        self.assertEqual("", qualified_name(ast.parse("make().run", mode="eval").body, aliases))


if __name__ == "__main__":
    unittest.main()

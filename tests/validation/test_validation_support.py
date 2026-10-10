"""Fixture tests for the helpers in tests/validation/validation_support.py that several policies share: the import
reader, the Markdown section and fence readers, the allowance reader, and the docstring finder.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from validation_support import (
    ModuleImport,
    docstring_ids,
    fence_body_line,
    fence_holders,
    import_aliases,
    markdown_section,
    module_allowance,
    module_imports,
    qualified_name,
)

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


class SharedReaderFixtures(unittest.TestCase):
    def test_a_section_runs_from_its_heading_to_the_next_second_level_heading(self) -> None:
        text = "# Title\n## One\nfirst\n### Inner\nkept\n## Two\nsecond"
        self.assertEqual("first\n### Inner\nkept", markdown_section(text, "## One"))
        self.assertEqual("second", markdown_section(text, "## Two"))
        self.assertIsNone(markdown_section(text, "## Three"))

    def test_a_fence_body_line_excludes_the_opener_and_the_closer(self) -> None:
        lines = ["text", "```bash", "echo hi", "```", "after"]
        holders = fence_holders(lines)
        self.assertEqual(
            [False, False, True, False, False], [fence_body_line(holder, index) for index, holder in enumerate(holders)]
        )
        # An unclosed fence holds every line after its opener.
        unclosed = ["```bash", "echo hi"]
        self.assertEqual(
            [False, True], [fence_body_line(holder, index) for index, holder in enumerate(fence_holders(unclosed))]
        )

    def test_an_allowance_maps_each_name_to_a_reason(self) -> None:
        def allowance(source: str) -> dict[str, str] | None:
            return module_allowance(ast.parse(source), "ALLOWED")

        self.assertEqual({}, allowance("OTHER = {'a': 'reason'}\n"))
        self.assertEqual({"a": "reason"}, allowance("ALLOWED = {'a': 'reason'}\n"))
        for invalid in ("ALLOWED = {'a': ''}", "ALLOWED = {'a': 1}", "ALLOWED = ['a']", "ALLOWED = make()"):
            with self.subTest(invalid=invalid):
                self.assertIsNone(allowance(invalid))
        # Only a module-level assignment counts, so this one reads as no allowance.
        self.assertEqual({}, allowance("def f():\n    ALLOWED = {'a': 'reason'}\n"))

    def test_docstring_ids_name_each_docstring_and_no_other_string(self) -> None:
        tree = ast.parse('"""Module."""\nclass C:\n    """Class."""\ndef f():\n    """Function."""\n    x = "text"\n')
        strings = {node.value: id(node) for node in ast.walk(tree) if isinstance(node, ast.Constant)}
        self.assertEqual({strings["Module."], strings["Class."], strings["Function."]}, docstring_ids(tree))


if __name__ == "__main__":
    unittest.main()

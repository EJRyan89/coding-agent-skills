"""Fixture tests for the helpers in tests/validation/validation_support.py that several policies share: the import
reader, the Markdown section and fence readers, the allowance reader, and the docstring finder.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

from git_test_base import GitTestCase
from validation_support import (
    CallableResolver,
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


class ImportReaderFixtures(GitTestCase):
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


class CallableResolverFixtures(GitTestCase):
    def test_assigned_aliases_follow_imports_builtins_and_other_aliases(self) -> None:
        tree = ast.parse(
            "import shutil as sh\nfrom io import open as read\n"
            "writer = open\ncopy = sh.copyfile\nalias: object = copy\n"
            'writer("x", "w")\nalias("x", "y")\nread("x", "r")\n'
        )
        resolver = CallableResolver(tree)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        self.assertEqual(["open", "shutil.copyfile", "io.open"], [resolver.name(call.func) for call in calls])
        self.assertEqual([], resolver.problems("fixture.py"))

    def test_function_bindings_and_imports_do_not_leak_into_other_scopes(self) -> None:
        tree = ast.parse(
            "writer = open\n"
            "def one():\n"
            "    import io\n"
            "    writer = io.open\n"
            '    writer("x", "r")\n'
            "def two():\n"
            "    writer = io.open\n"
            '    writer("x", "w")\n'
            'writer("x", "w")\n'
        )
        resolver = CallableResolver(tree)
        self.assertEqual(
            ["fixture.py:8 cannot resolve callable alias writer; use a single binding to a known callable"],
            resolver.problems("fixture.py"),
        )
        names = {node.lineno: resolver.name(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        self.assertEqual({5: "io.open", 8: "", 9: "open"}, names)

    def test_any_other_binding_invalidates_an_alias_and_its_dependents(self) -> None:
        changes = (
            "writer = open",
            "writer = None",
            "writer += other",
            "writer, other = callbacks",
            "for writer in callbacks:\n    pass",
            "with callback as writer:\n    pass",
            "try:\n    pass\nexcept Exception as writer:\n    pass",
            "(writer := other)",
            "del writer",
            "def writer():\n    pass",
            "match value:\n    case writer:\n        pass",
        )
        for change in changes:
            source = f'writer = open\nalias = writer\n{change}\nalias("x", "w")\n'
            with self.subTest(change=change):
                tree = ast.parse(source)
                resolver = CallableResolver(tree)
                self.assertEqual(
                    [
                        f"fixture.py:{len(source.splitlines())} cannot resolve callable alias alias; "
                        "use a single binding to a known callable"
                    ],
                    resolver.problems("fixture.py"),
                )

    def test_parameters_and_local_definitions_shadow_builtins_without_hiding_alias_rebindings(self) -> None:
        tree = ast.parse(
            "def one(open):\n"
            "    writer = open\n"
            '    writer("x", "w")\n'
            "def two():\n"
            "    def open():\n"
            "        pass\n"
            "    writer = open\n"
            "    writer()\n"
        )
        resolver = CallableResolver(tree)
        self.assertEqual([], resolver.problems("fixture.py"))
        self.assertEqual(
            ["<local>.open", "<local>.open"],
            [resolver.name(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)],
        )

    def test_a_function_alias_can_name_its_own_enclosing_definition(self) -> None:
        resolver = CallableResolver(ast.parse("def example():\n    alias = example\n    alias()\n"))
        call = next(node for node in ast.walk(resolver.tree) if isinstance(node, ast.Call))
        self.assertEqual("<local>.example", resolver.name(call.func))
        self.assertEqual([], resolver.problems("fixture.py"))

    def test_unknown_cyclic_and_rebound_import_aliases_are_reported_at_the_call(self) -> None:
        for prefix in (
            "writer = unknown\n",
            "writer = other\nother = writer\n",
            "import builtins as module\nwriter = module.open\nmodule = None\n",
        ):
            source = prefix + 'writer("x", "w")\n'
            with self.subTest(prefix=prefix):
                resolver = CallableResolver(ast.parse(source))
                self.assertEqual(
                    [
                        f"fixture.py:{len(source.splitlines())} cannot resolve callable alias writer; "
                        "use a single binding to a known callable"
                    ],
                    resolver.problems("fixture.py"),
                )

    def test_rebound_data_receivers_are_not_callable_aliases(self) -> None:
        resolver = CallableResolver(ast.parse('text = header\ntext = "value"\ntext.replace("a", "b")\n'))
        self.assertEqual([], resolver.problems("fixture.py"))

    def test_comprehensions_and_class_bodies_do_not_shadow_an_enclosing_alias(self) -> None:
        tree = ast.parse(
            "writer = open\n"
            "items = [writer() for writer in callbacks]\n"
            "class Example:\n"
            "    writer = unknown\n"
            "    def write(self):\n"
            '        writer("x", "w")\n'
            'writer("x", "w")\n'
        )
        resolver = CallableResolver(tree)
        names = {node.lineno: resolver.name(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        self.assertEqual({2: "<local>.writer", 6: "open", 7: "open"}, names)
        self.assertEqual([], resolver.problems("fixture.py"))

    def test_a_default_argument_uses_the_enclosing_scope_not_its_parameter(self) -> None:
        tree = ast.parse('writer = open\ndef example(writer=writer("x", "w")):\n    writer()\n')
        resolver = CallableResolver(tree)
        names = {node.lineno: resolver.name(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        self.assertEqual({2: "open", 3: "<local>.writer"}, names)
        self.assertEqual([], resolver.problems("fixture.py"))

    def test_global_and_nonlocal_rebindings_invalidate_the_original_alias(self) -> None:
        for source in (
            "writer = open\ndef change():\n    global writer\n    writer = None\nwriter()\n",
            "def outer():\n    writer = open\n    def change():\n        nonlocal writer\n"
            "        writer = None\n    writer()\n",
            "writer = open\ndef outer():\n    def change():\n        nonlocal writer\n"
            "        writer = None\n    writer = open\n    writer()\n",
            "writer = open\nitems = [(writer := other) for other in callbacks]\nwriter()\n",
        ):
            with self.subTest(source=source):
                resolver = CallableResolver(ast.parse(source))
                self.assertEqual(
                    [
                        f"fixture.py:{len(source.splitlines())} cannot resolve callable alias writer; "
                        "use a single binding to a known callable"
                    ],
                    resolver.problems("fixture.py"),
                )

    def test_an_annotation_without_a_value_does_not_rebind_an_alias(self) -> None:
        resolver = CallableResolver(ast.parse('writer = open\nwriter: object\nwriter("x", "w")\n'))
        call = next(node for node in ast.walk(resolver.tree) if isinstance(node, ast.Call))
        self.assertEqual("open", resolver.name(call.func))
        self.assertEqual([], resolver.problems("fixture.py"))

    def test_dynamic_callable_construction_is_not_a_simple_assignment_alias(self) -> None:
        resolver = CallableResolver(ast.parse("callback = library().callback\ncallback()\n"))
        self.assertEqual([], resolver.problems("fixture.py"))

    def test_every_binding_to_a_name_or_attribute_is_followed_as_an_assignment_is(self) -> None:
        for source in (
            '(writer := open)\nwriter("x", "w")\n',
            'if (writer := open):\n    writer("x", "w")\n',
            'writer, other = open, print\nwriter("x", "w")\n',
            '[other, (writer, _)] = [print, (open, 0)]\nwriter("x", "w")\n',
            'first, *rest, writer = print, 0, 1, open\nwriter("x", "w")\n',
            'for writer in (open,):\n    writer("x", "w")\n',
            'for name, writer in [("x", open)]:\n    writer(name, "w")\n',
            'items = [writer("x", "w") for writer in [open]]\n',
            'def example(writer=open):\n    writer("x", "w")\n',
            'def example(*, writer=open):\n    writer("x", "w")\n',
            'example = lambda writer=open: writer("x", "w")\n',
            'match open:\n    case writer:\n        writer("x", "w")\n',
            'match open:\n    case object() as writer:\n        writer("x", "w")\n',
        ):
            with self.subTest(source=source):
                tree = ast.parse(source)
                resolver = CallableResolver(tree)
                call = next(
                    node for node in ast.walk(tree) if isinstance(node, ast.Call) and "writer" in ast.dump(node)
                )
                self.assertEqual("open", resolver.name(call.func))
                self.assertEqual([], resolver.problems("fixture.py"))

    def test_a_name_bound_to_several_callables_or_to_an_unknown_one_is_reported_at_the_call(self) -> None:
        for source in (
            '(writer := missing)\nwriter("x", "w")\n',
            'for writer in (open, print):\n    writer("x", "w")\n',
            'items = [writer("x", "w") for writer in {open, print}]\n',
            'def example(writer=missing):\n    writer("x", "w")\n',
            'def example(writer=open):\n    writer = print\n    writer("x", "w")\n',
            'example = lambda writer=open: [(writer := print), writer("x", "w")]\n',
            'import os, shutil\nfor writer in (os, shutil):\n    writer.remove("x")\n',
        ):
            function = "writer.remove" if "remove" in source else "writer"
            with self.subTest(source=source):
                self.assertEqual(
                    [
                        f"fixture.py:{len(source.splitlines())} cannot resolve callable alias {function}; "
                        "use a single binding to a known callable"
                    ],
                    CallableResolver(ast.parse(source)).problems("fixture.py"),
                )

    def test_a_value_built_any_other_way_stays_a_local_value(self) -> None:
        # A call's result, an item of anything but a literal, a starred list, a with target, a caught exception, a part
        # of a case's subject, and a parameter whose default is no name all hold values the module built.
        for source in (
            "writer, other = callbacks\nwriter()\n",
            "writer, other = open, print, input\nwriter()\n",
            "first, *writer = open, print\nwriter()\n",
            "for writer in callbacks:\n    writer()\n",
            "with factory() as writer:\n    writer()\n",
            "try:\n    pass\nexcept Exception as writer:\n    writer()\n",
            "match open:\n    case [writer]:\n        writer()\n",
            "def example(writer=None):\n    writer()\n",
            "def example(writer=factory()):\n    writer()\n",
        ):
            with self.subTest(source=source):
                tree = ast.parse(source)
                resolver = CallableResolver(tree)
                call = next(
                    node for node in ast.walk(tree) if isinstance(node, ast.Call) and "writer" in ast.dump(node)
                )
                self.assertEqual("<local>.writer", resolver.name(call.func))
                self.assertEqual([], resolver.problems("fixture.py"))


class SharedReaderFixtures(GitTestCase):
    def test_a_section_runs_from_its_heading_to_the_next_second_level_heading(self) -> None:
        text = "# Title\n## One\nfirst\n### Inner\nkept\n## Two\nsecond"
        self.assertEqual("first\n### Inner\nkept", markdown_section(text, "## One"))
        self.assertEqual("second", markdown_section(text, "## Two"))
        self.assertIsNone(markdown_section(text, "## Three"))

    def test_section_readers_share_trailing_whitespace_and_crlf_semantics(self) -> None:
        from tools.release_notes import markdown_section as release_section

        self.assertIs(markdown_section, release_section)
        text = "# Title\r\n## One \t\r\nfirst  \r\n### Inner\r\nkept\t\r\n## Two \t\r\nsecond"
        self.assertEqual("first  \n### Inner\nkept\t", markdown_section(text, "## One"))
        self.assertEqual("second", markdown_section(text, "## Two"))
        self.assertIsNone(markdown_section(text, "## On"))

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

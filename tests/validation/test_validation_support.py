"""Fixture tests for the helpers in tests/validation/validation_support.py that several policies share: the import
reader, the Markdown section and fence readers, the allowance reader, and the docstring finder.
"""

from __future__ import annotations

import ast
import codecs
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

import duplication
import skill_scripts
from git_test_base import GitTestCase
from validation_support import (
    CODE_SPAN,
    CallableResolver,
    ModuleImport,
    ScriptsEntry,
    UnreadablePython,
    docstring_ids,
    fence_body_line,
    fence_holders,
    import_aliases,
    markdown_section,
    module_allowance,
    module_imports,
    parse_python,
    python_trees,
    qualified_name,
    scripts_entries,
    write_fixture_tree,
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

    def test_the_code_span_reader_is_skill_core_s(self) -> None:
        import markdown_sections
        import skill_layout

        self.assertIs(markdown_sections.CODE_SPAN, CODE_SPAN)
        self.assertIs(CODE_SPAN, skill_layout.CODE_SPAN)

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
        # An annotated allowance is read as a plain one is, and held to the same shape.
        self.assertEqual({"a": "reason"}, allowance("ALLOWED: dict[str, str] = {'a': 'reason'}\n"))
        self.assertIsNone(allowance("ALLOWED: dict[str, str] = {'a': ''}\n"))
        for invalid in ("ALLOWED = {'a': ''}", "ALLOWED = {'a': 1}", "ALLOWED = ['a']", "ALLOWED = make()"):
            with self.subTest(invalid=invalid):
                self.assertIsNone(allowance(invalid))
        # Only a module-level assignment counts, so this one reads as no allowance.
        self.assertEqual({}, allowance("def f():\n    ALLOWED = {'a': 'reason'}\n"))

    def test_docstring_ids_name_each_docstring_and_no_other_string(self) -> None:
        tree = ast.parse('"""Module."""\nclass C:\n    """Class."""\ndef f():\n    """Function."""\n    x = "text"\n')
        strings = {node.value: id(node) for node in ast.walk(tree) if isinstance(node, ast.Constant)}
        self.assertEqual({strings["Module."], strings["Class."], strings["Function."]}, docstring_ids(tree))


class PythonFileReaderFixtures(GitTestCase):
    """Every policy reads a Python file one way: UTF-8 without a byte-order mark, so a file in another encoding is
    named by every policy that reads it, and the policy goes on to the next file."""

    def write(self, root: Path, files: dict[str, bytes]) -> None:
        for name, data in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(data)

    def test_a_byte_order_mark_a_bad_byte_and_a_syntax_error_are_named_by_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write(
                root,
                {
                    "a_mark.py": codecs.BOM_UTF8 + b"x = 1\n",
                    "b_byte.py": b"x = '\xe9'\n",
                    "c_syntax.py": b"def (\n",
                    "d_good.py": b"x = 1\r\ny = 2\r\n",
                },
            )
            problems: list[str] = []
            read = [name for _, name, _ in python_trees(sorted(root.glob("*.py")), root, problems)]
            self.assertEqual(["d_good.py"], read)
            self.assertEqual(
                [
                    "a_mark.py starts with a UTF-8 byte-order mark; save it as UTF-8 without one",
                    "b_byte.py holds a byte that is not UTF-8 at offset 5; save it as UTF-8",
                    "c_syntax.py does not parse: invalid syntax at line 1",
                ],
                problems,
            )
            # Line endings read as newlines, as a text read gives them.
            self.assertEqual(2, len(parse_python(root / "d_good.py").body))
            with self.assertRaisesRegex(UnreadablePython, "byte-order mark"):
                parse_python(root / "a_mark.py")

    def test_every_policy_that_reads_a_skill_script_names_an_unreadable_one_and_reads_the_next(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "skills/demo/SKILL.md": "---\nname: demo\n---\n",
                    "skills/demo/scripts/later.py": "import sys\n\n\ndef main():\n    sys.exit(7)\n",
                    "skills/skill-core/scripts/console.py": "",
                    "deployer/__init__.py": "",
                },
            )
            self.write(root, {"skills/demo/scripts/early.py": codecs.BOM_UTF8 + b"x = 1\n"})
            unreadable = (
                "skills/demo/scripts/early.py starts with a UTF-8 byte-order mark; save it as UTF-8 without one"
            )
            for policy in (
                skill_scripts.console_setup_problems,
                skill_scripts.script_contract_problems,
                skill_scripts.client_command_problems,
                skill_scripts.secret_named_function_problems,
                skill_scripts.gh_filter_problems,
                duplication.duplicated_definition_problems,
            ):
                with self.subTest(policy=policy.__name__):
                    self.assertIn(unreadable, policy(root))
            # The policy goes on past the unreadable file to the next one.
            self.assertIn(
                "skills/demo/scripts/later.py:5 exits 7; scripts exit only 0, 1, or 2; see "
                '"Script results" in docs/adding-a-skill.md',
                skill_scripts.script_contract_problems(root),
            )


class ScriptsEntryFixtures(GitTestCase):
    """Every way a module puts a sibling's scripts directory on sys.path is read, so the skill_deps, sibling-path, and
    mypy_path policies see it."""

    def entries(self, source: str) -> list[ScriptsEntry]:
        return scripts_entries(ast.parse(source))

    def test_each_way_a_lookup_reaches_sys_path_is_read(self) -> None:
        for source in (
            'import sys\nsys.path.insert(0, str(sibling_scripts("dep")))\n',
            'import sys\np = sibling_scripts("dep")\nsys.path.insert(0, str(p))\n',
            'import sys\nsys.path.extend([str(sibling_scripts("dep"))])\n',
            'import sys\nsys.path[:0] = [str(sibling_scripts("dep"))]\n',
            'import sys\nsys.path += [str(sibling_scripts("dep"))]\n',
            'import sys as system\nsystem.path.append(str(sibling_scripts("dep")))\n',
        ):
            with self.subTest(source=source):
                self.assertEqual(["dep"], [entry.name for entry in self.entries(source)])
                self.assertFalse(self.entries(source)[0].hand_written)

    def test_each_hand_written_path_to_a_sibling_is_read(self) -> None:
        for source in (
            'sys.path.insert(0, str(Path(__file__).parents[2] / "repo-cleanup" / "scripts"))\n',
            'SIBLING = Path(__file__).resolve().parents[2] / "repo-cleanup" / "scripts"\n'
            "sys.path.insert(0, str(SIBLING))\n",
            'sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "repo-cleanup", "scripts"))\n',
            'sys.path.insert(0, str(Path(__file__).parents[2] / "repo-cleanup/scripts"))\n',
            'sys.path.insert(0, str(Path(__file__).parents[2].joinpath("repo-cleanup", "scripts")))\n',
        ):
            with self.subTest(source=source):
                entries = self.entries(f"import sys\nimport os\nfrom pathlib import Path\n{source}")
                self.assertEqual([ScriptsEntry(3 + len(source.splitlines()), "repo-cleanup", True)], entries)

    def test_a_path_that_names_no_sibling_and_a_lookup_off_sys_path_are_not_entries(self) -> None:
        for source in (
            "import sys\nsys.path.insert(0, str(Path(__file__).parent))\n",
            'import sys\nsys.path.insert(0, str(Path(__file__).parent / "scripts"))\n',
            'other = []\nother.insert(0, str(sibling_scripts("dep")))\n',
            'p = sibling_scripts("dep")\nprint(p)\n',
        ):
            with self.subTest(source=source):
                self.assertEqual([], self.entries(source))

    def test_a_lookup_whose_name_is_not_one_literal_is_an_entry_without_a_name(self) -> None:
        self.assertEqual(
            [ScriptsEntry(3, None, False)],
            self.entries('import sys\nNAME = "dep"\nsys.path.insert(0, str(sibling_scripts(NAME)))\n'),
        )


if __name__ == "__main__":
    unittest.main()

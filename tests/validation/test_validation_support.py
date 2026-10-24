"""Fixture tests for the helpers in tests/validation/validation_support.py that several policies share: the import
reader, the call-argument and scope readers, the main-guard test, the Markdown section and fence readers, the allowance
reader, the docstring finder, and the repository's file list.
"""

from __future__ import annotations

import ast
import codecs
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import duplication
import skill_scripts
from validation_support import (
    CODE_SPAN,
    COMPUTED_NAME,
    MODULE_SCOPE,
    CallableResolver,
    ModuleImport,
    ScriptsEntry,
    UnreadablePython,
    call_argument,
    call_arguments,
    docstring_ids,
    fence_body_line,
    fence_holders,
    import_aliases,
    is_main_guard,
    listed,
    markdown_section,
    module_allowance,
    module_imports,
    module_lookup,
    parse_python,
    python_trees,
    qualified_name,
    repository_files,
    scoped_nodes,
    scripts_entries,
    skill_dependency_closure,
    statement_scope,
    write_fixture_tree,
)

from tools.release_notes import ReleaseNotesError

SOURCE = (
    "import os.path\n"
    "import subprocess as sp, json\n"
    "from github_client import GitHubClient as Client, GitHubError\n"
    "from . import sibling\n"
    'TEXT = "import hidden"\n'
    "def load():\n"
    "    import bounded_process\n"
)


def last_expression(tree: ast.Module) -> ast.expr:
    """The expression a fixture module's last statement is."""
    statement = tree.body[-1]
    if not isinstance(statement, ast.Expr):
        raise AssertionError(f"the fixture ends with {ast.unparse(statement)}, not an expression")
    return statement.value


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


class CallableResolverFixtures(unittest.TestCase):
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

    def test_an_annotation_without_a_value_does_not_rebind_an_alias(self) -> None:
        resolver = CallableResolver(ast.parse('writer = open\nwriter: object\nwriter("x", "w")\n'))
        call = next(node for node in ast.walk(resolver.tree) if isinstance(node, ast.Call))
        self.assertEqual("open", resolver.name(call.func))
        self.assertEqual([], resolver.problems("fixture.py"))

    def test_an_augmented_assignment_rebinds_a_name_unless_it_updates_in_place(self) -> None:
        source = "import os\nenv = os.environ\nenv |= {}\nenv\n"
        for in_place, expected in ((False, ""), (True, "os.environ")):
            with self.subTest(in_place_updates=in_place):
                tree = ast.parse(source)
                self.assertEqual(expected, CallableResolver(tree, in_place).name(last_expression(tree)))
        # A name bound twice stays unresolved however an augmented assignment is read.
        rebound = ast.parse("import os\nenv = os.environ\nenv = {}\nenv |= {}\nenv\n")
        self.assertEqual("", CallableResolver(rebound, in_place_updates=True).name(last_expression(rebound)))

    def test_dynamic_callable_construction_is_not_a_simple_assignment_alias(self) -> None:
        resolver = CallableResolver(ast.parse("callback = library().callback\ncallback()\n"))
        self.assertEqual([], resolver.problems("fixture.py"))


@pytest.mark.parametrize(
    "change",
    [
        pytest.param("writer = open", id="bound again"),
        pytest.param("writer = None", id="rebound to a value"),
        pytest.param("writer += other", id="augmented"),
        pytest.param("writer, other = callbacks", id="unpacked"),
        pytest.param("for writer in callbacks:\n    pass", id="a loop target"),
        pytest.param("with callback as writer:\n    pass", id="a with target"),
        pytest.param("try:\n    pass\nexcept Exception as writer:\n    pass", id="a caught exception"),
        pytest.param("(writer := other)", id="an assignment expression"),
        pytest.param("del writer", id="deleted"),
        pytest.param("def writer():\n    pass", id="a definition"),
        pytest.param("match value:\n    case writer:\n        pass", id="a case capture"),
    ],
)
def test_any_other_binding_invalidates_an_alias_and_its_dependents(change: str) -> None:
    source = f'writer = open\nalias = writer\n{change}\nalias("x", "w")\n'
    tree = ast.parse(source)
    resolver = CallableResolver(tree)
    assert [
        f"fixture.py:{len(source.splitlines())} cannot resolve callable alias alias; "
        "use a single binding to a known callable"
    ] == resolver.problems("fixture.py")


@pytest.mark.parametrize(
    "prefix",
    [
        pytest.param("writer = unknown\n", id="unknown"),
        pytest.param("writer = other\nother = writer\n", id="cyclic"),
        pytest.param("import builtins as module\nwriter = module.open\nmodule = None\n", id="rebound import"),
    ],
)
def test_unknown_cyclic_and_rebound_import_aliases_are_reported_at_the_call(prefix: str) -> None:
    source = prefix + 'writer("x", "w")\n'
    resolver = CallableResolver(ast.parse(source))
    assert [
        f"fixture.py:{len(source.splitlines())} cannot resolve callable alias writer; "
        "use a single binding to a known callable"
    ] == resolver.problems("fixture.py")


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("writer = open\ndef change():\n    global writer\n    writer = None\nwriter()\n", id="global"),
        pytest.param(
            "def outer():\n    writer = open\n    def change():\n        nonlocal writer\n"
            "        writer = None\n    writer()\n",
            id="nonlocal",
        ),
        pytest.param(
            "writer = open\ndef outer():\n    def change():\n        nonlocal writer\n"
            "        writer = None\n    writer = open\n    writer()\n",
            id="nonlocal in a nested function",
        ),
        pytest.param(
            "writer = open\nitems = [(writer := other) for other in callbacks]\nwriter()\n",
            id="an assignment expression in a comprehension",
        ),
    ],
)
def test_global_and_nonlocal_rebindings_invalidate_the_original_alias(source: str) -> None:
    resolver = CallableResolver(ast.parse(source))
    assert [
        f"fixture.py:{len(source.splitlines())} cannot resolve callable alias writer; "
        "use a single binding to a known callable"
    ] == resolver.problems("fixture.py")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("import importlib\nimportlib.import_module('a.b')", "a.b", id="import_module"),
        pytest.param("from importlib import import_module as load\nload(name='a.b')", "a.b", id="renamed by keyword"),
        pytest.param("__import__('a.b')", "a", id="__import__"),
        pytest.param("__import__('a.b', fromlist=['c'])", "a.b", id="__import__ with a fromlist"),
        pytest.param("import sys\nsys.modules['a.b']", "a.b", id="sys.modules"),
        pytest.param("import importlib\nmodule = importlib.import_module('a')\nmodule", "a", id="bound"),
        pytest.param("import importlib\nimportlib.import_module(name)", COMPUTED_NAME, id="import_module computed"),
        pytest.param("import sys\nsys.modules[name]", COMPUTED_NAME, id="sys.modules computed"),
        pytest.param(
            "import importlib\nmodule = importlib.import_module('a')\nmodule = None\nmodule", "", id="rebound"
        ),
        pytest.param("modules = {}\nmodules['a']", "", id="another dictionary"),
        pytest.param("load('a')", "", id="an unknown call"),
    ],
)
def test_a_module_looked_up_by_its_name_is_read_through_the_imports(source: str, expected: str) -> None:
    tree = ast.parse(source)
    assert expected == module_lookup(last_expression(tree), CallableResolver(tree))


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('(writer := open)\nwriter("x", "w")\n', id="an assignment expression"),
        pytest.param('if (writer := open):\n    writer("x", "w")\n', id="an assignment expression in a test"),
        pytest.param('writer, other = open, print\nwriter("x", "w")\n', id="a tuple"),
        pytest.param('[other, (writer, _)] = [print, (open, 0)]\nwriter("x", "w")\n', id="a nested list"),
        pytest.param('first, *rest, writer = print, 0, 1, open\nwriter("x", "w")\n', id="after a starred target"),
        pytest.param('for writer in (open,):\n    writer("x", "w")\n', id="a loop over one callable"),
        pytest.param('for name, writer in [("x", open)]:\n    writer(name, "w")\n', id="a loop unpacking"),
        pytest.param('items = [writer("x", "w") for writer in [open]]\n', id="a comprehension"),
        pytest.param('def example(writer=open):\n    writer("x", "w")\n', id="a default"),
        pytest.param('def example(*, writer=open):\n    writer("x", "w")\n', id="a keyword-only default"),
        pytest.param('example = lambda writer=open: writer("x", "w")\n', id="a lambda default"),
        pytest.param('match open:\n    case writer:\n        writer("x", "w")\n', id="a case capture"),
        pytest.param('match open:\n    case object() as writer:\n        writer("x", "w")\n', id="a case as pattern"),
    ],
)
def test_every_binding_to_a_name_or_attribute_is_followed_as_an_assignment_is(source: str) -> None:
    tree = ast.parse(source)
    resolver = CallableResolver(tree)
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call) and "writer" in ast.dump(node))
    assert resolver.name(call.func) == "open"
    assert resolver.problems("fixture.py") == []


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('(writer := missing)\nwriter("x", "w")\n', id="an unknown name"),
        pytest.param('for writer in (open, print):\n    writer("x", "w")\n', id="a loop over two callables"),
        pytest.param('items = [writer("x", "w") for writer in {open, print}]\n', id="a comprehension over a set"),
        pytest.param('def example(writer=missing):\n    writer("x", "w")\n', id="an unknown default"),
        pytest.param('def example(writer=open):\n    writer = print\n    writer("x", "w")\n', id="a rebound parameter"),
        pytest.param(
            'example = lambda writer=open: [(writer := print), writer("x", "w")]\n', id="a rebound lambda parameter"
        ),
        pytest.param('import os, shutil\nfor writer in (os, shutil):\n    writer.remove("x")\n', id="two modules"),
    ],
)
def test_a_name_bound_to_several_callables_or_to_an_unknown_one_is_reported_at_the_call(source: str) -> None:
    function = "writer.remove" if "remove" in source else "writer"
    assert [
        f"fixture.py:{len(source.splitlines())} cannot resolve callable alias {function}; "
        "use a single binding to a known callable"
    ] == CallableResolver(ast.parse(source)).problems("fixture.py")


# A call's result, an item of anything but a literal, a starred list, a with target, a caught exception, a part of a
# case's subject, and a parameter whose default is no name all hold values the module built.
@pytest.mark.parametrize(
    "source",
    [
        pytest.param("writer, other = callbacks\nwriter()\n", id="an item of a value"),
        pytest.param("writer, other = open, print, input\nwriter()\n", id="an item of a mismatched tuple"),
        pytest.param("first, *writer = open, print\nwriter()\n", id="a starred list"),
        pytest.param("for writer in callbacks:\n    writer()\n", id="a loop over a value"),
        pytest.param("with factory() as writer:\n    writer()\n", id="a with target"),
        pytest.param("try:\n    pass\nexcept Exception as writer:\n    writer()\n", id="a caught exception"),
        pytest.param("match open:\n    case [writer]:\n        writer()\n", id="a part of a case subject"),
        pytest.param("def example(writer=None):\n    writer()\n", id="a default that is no name"),
        pytest.param("def example(writer=factory()):\n    writer()\n", id="a default call"),
    ],
)
def test_a_value_built_any_other_way_stays_a_local_value(source: str) -> None:
    tree = ast.parse(source)
    resolver = CallableResolver(tree)
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call) and "writer" in ast.dump(node))
    assert resolver.name(call.func) == "<local>.writer"
    assert resolver.problems("fixture.py") == []


class SharedReaderFixtures(unittest.TestCase):
    def test_a_section_runs_from_its_heading_to_the_next_second_level_heading(self) -> None:
        text = "# Title\n## One\nfirst\n### Inner\nkept\n## Two\nsecond"
        self.assertEqual("first\n### Inner\nkept", markdown_section(text, "## One"))
        self.assertEqual("second", markdown_section(text, "## Two"))
        self.assertIsNone(markdown_section(text, "## Three"))

    def test_section_readers_share_trailing_whitespace_and_crlf_semantics(self) -> None:
        from tools import release_notes

        # tools/release_notes.py imports markdown_section and exports none, so its own binding is read.
        self.assertIs(markdown_section, vars(release_notes)["markdown_section"])
        text = "# Title\r\n## One \t\r\nfirst  \r\n### Inner\r\nkept\t\r\n## Two \t\r\nsecond"
        self.assertEqual("first  \n### Inner\nkept\t", markdown_section(text, "## One"))
        self.assertEqual("second", markdown_section(text, "## Two"))
        self.assertIsNone(markdown_section(text, "## On"))

    def test_the_code_span_reader_is_skill_core_s(self) -> None:
        import markdown_links
        import markdown_sections
        import skill_layout

        self.assertIs(markdown_sections.CODE_SPAN, CODE_SPAN)
        # skill_layout and markdown_links import these and export none, so each module's own binding is read.
        self.assertIs(CODE_SPAN, vars(skill_layout)["CODE_SPAN"])
        self.assertIs(markdown_sections.COMMONMARK_CODE_SPAN, vars(markdown_links)["COMMONMARK_CODE_SPAN"])

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

    def test_the_dependency_closure_follows_skill_deps_through_each_other_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deploy-meta/alpha.json": '{"skill_deps": ["beta", "gamma"]}',
                    "deploy-meta/beta.json": '{"skill_deps": ["delta", "alpha"]}',
                    "deploy-meta/gamma.json": '{"skill_deps": ["delta"]}',
                    "deploy-meta/delta.json": "{}",
                    # A dependency with no metadata of its own is reached and depends on nothing.
                    "deploy-meta/epsilon.json": '{"skill_deps": ["unlisted"]}',
                },
            )
            self.assertEqual(["beta", "delta", "gamma"], skill_dependency_closure(root, "alpha"))
            self.assertEqual(["delta", "alpha", "gamma"], skill_dependency_closure(root, "beta"))
            self.assertEqual([], skill_dependency_closure(root, "delta"))
            self.assertEqual(["unlisted"], skill_dependency_closure(root, "epsilon"))
            self.assertEqual([], skill_dependency_closure(root, "missing"))

    def test_an_allowance_maps_each_name_to_a_reason(self) -> None:
        def allowance(source: str) -> dict[str, str] | None:
            return module_allowance(ast.parse(source), "ALLOWED")

        self.assertEqual({}, allowance("OTHER = {'a': 'reason'}\n"))
        self.assertEqual({"a": "reason"}, allowance("ALLOWED = {'a': 'reason'}\n"))
        # An annotated allowance is read as a plain one is, and held to the same shape.
        self.assertEqual({"a": "reason"}, allowance("ALLOWED: dict[str, str] = {'a': 'reason'}\n"))
        self.assertIsNone(allowance("ALLOWED: dict[str, str] = {'a': ''}\n"))
        # Only a module-level assignment counts, so this one reads as no allowance.
        self.assertEqual({}, allowance("def f():\n    ALLOWED = {'a': 'reason'}\n"))

    def test_docstring_ids_name_each_docstring_and_no_other_string(self) -> None:
        tree = ast.parse('"""Module."""\nclass C:\n    """Class."""\ndef f():\n    """Function."""\n    x = "text"\n')
        strings = {node.value: id(node) for node in ast.walk(tree) if isinstance(node, ast.Constant)}
        self.assertEqual({strings["Module."], strings["Class."], strings["Function."]}, docstring_ids(tree))


@pytest.mark.parametrize(
    "invalid",
    [
        "ALLOWED = {'a': ''}",
        "ALLOWED = {'a': 1}",
        "ALLOWED = ['a']",
        "ALLOWED = make()",
        # A dictionary or a set with an unhashable key, which literal_eval cannot build.
        "ALLOWED = {['a']: 'reason'}",
        "ALLOWED = {('a', ['b']): 'reason'}",
        "ALLOWED = {{'a': 1}}",
    ],
)
def test_an_allowance_of_any_other_shape_is_unreadable(invalid: str) -> None:
    assert module_allowance(ast.parse(invalid), "ALLOWED") is None


def _call(source: str) -> ast.Call:
    expression = ast.parse(source, mode="eval").body
    if not isinstance(expression, ast.Call):
        raise TypeError(f"{source!r} is not a call")
    return expression


class CallArgumentFixtures(unittest.TestCase):
    def read(self, source: str, position: int | None, *keywords: str) -> str | None:
        found = call_argument(_call(source), position, *keywords)
        return None if found is None else ast.unparse(found)

    def test_a_plain_positional_or_keyword_argument_is_the_value(self) -> None:
        self.assertEqual("'w'", self.read("open(path, 'w')", 1, "mode"))
        self.assertEqual("'w'", self.read("open(path, mode='w')", 1, "mode"))
        self.assertEqual("'git'", self.read("which(name='git')", 0, "cmd", "name"))
        self.assertIsNone(self.read("open(path)", 1, "mode"))

    def test_an_unpacking_that_may_hold_the_parameter_is_returned_as_one_either_way(self) -> None:
        self.assertEqual("*rest", self.read("open(*rest, 'w')", 1, "mode"))
        self.assertEqual("*rest", self.read("open(path, *rest)", 1, "mode"))
        unpacked = call_argument(_call("open(path, **options)"), 1, "mode")
        self.assertIsInstance(unpacked, ast.Starred)
        self.assertEqual("*options", ast.unparse(unpacked) if unpacked else None)

    def test_a_keyword_wins_over_an_unpacking_since_python_refuses_one_given_twice(self) -> None:
        self.assertEqual("'r'", self.read("open(*rest, mode='r')", 1, "mode"))
        self.assertEqual("'r'", self.read("open(path, **options, mode='r')", 1, "mode"))

    def test_a_position_of_none_reads_keywords_alone(self) -> None:
        self.assertEqual("'log'", self.read("basicConfig(level, filename='log')", None, "filename"))
        self.assertIsNone(self.read("basicConfig('log')", None, "filename"))

    def test_every_argument_that_may_give_a_parameter_is_listed_in_the_call_s_order(self) -> None:
        found = call_arguments(_call("os.open(*rest, path, flags, dir_fd=fd, **more)"), 1, {"path", "dir_fd"})
        self.assertEqual(["*rest", "path", "fd", "*more"], [ast.unparse(argument) for argument in found])
        self.assertEqual([], call_arguments(_call("extractall()"), 0, {"path"}))


class ScopeAndGuardFixtures(unittest.TestCase):
    def test_each_node_is_in_its_innermost_definition_or_its_named_assignment(self) -> None:
        tree = ast.parse(
            "TABLE = {'a': f()}\n"
            "x, y = g(), h()\n"
            "@decorate(1)\n"
            "class Outer:\n"
            "    def method(self, value=k()):\n"
            "        return m()\n"
            "if READY:\n"
            "    def guarded():\n"
            "        return n()\n"
        )
        calls = {ast.unparse(node.func): scope for node, scope in scoped_nodes(tree.body) if isinstance(node, ast.Call)}
        self.assertEqual(
            {
                "f": "TABLE",
                "g": MODULE_SCOPE,
                "h": MODULE_SCOPE,
                "decorate": "Outer",
                "k": "Outer.method",
                "m": "Outer.method",
                "n": "guarded",
            },
            calls,
        )
        self.assertEqual(["TABLE", MODULE_SCOPE], [statement_scope(statement) for statement in tree.body[:2]])

    def test_words_are_listed_with_a_comma_before_the_conjunction_only_past_two(self) -> None:
        self.assertEqual("a", listed(["a"]))
        self.assertEqual("a or b", listed(["a", "b"]))
        self.assertEqual("a, b, and c", listed(["a", "b", "c"], "and"))
        self.assertEqual("", listed([]))


@pytest.mark.parametrize(
    ("test", "expected"),
    [
        ("__name__ == '__main__'", True),
        ("'__main__' == __name__", True),
        ("__name__ != '__main__'", False),
        ("__name__ == 'main'", False),
        ("name == '__main__'", False),
        ("__name__ == '__main__' == x", False),
    ],
)
def test_the_main_guard_is_read_either_way_round_and_nothing_else_is_one(test: str, expected: bool) -> None:
    assert expected == is_main_guard(ast.parse(test, mode="eval").body)


class PythonFileReaderFixtures(unittest.TestCase):
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
                    "skills/ejryan89-skill-core/scripts/console.py": "",
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


def sys_path_entries(source: str) -> list[ScriptsEntry]:
    return scripts_entries(ast.parse(source))


class ScriptsEntryFixtures(unittest.TestCase):
    """Every way a module puts a sibling's scripts directory on sys.path is read, so the skill_deps, sibling-path, and
    mypy_path policies see it."""

    def test_a_lookup_whose_name_is_not_one_literal_is_an_entry_without_a_name(self) -> None:
        self.assertEqual(
            [ScriptsEntry(3, None, False)],
            sys_path_entries('import sys\nNAME = "dep"\nsys.path.insert(0, str(sibling_scripts(NAME)))\n'),
        )


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('import sys\nsys.path.insert(0, str(sibling_scripts("dep")))\n', id="insert"),
        pytest.param('import sys\np = sibling_scripts("dep")\nsys.path.insert(0, str(p))\n', id="insert a name"),
        pytest.param('import sys\nsys.path.extend([str(sibling_scripts("dep"))])\n', id="extend"),
        pytest.param('import sys\nsys.path[:0] = [str(sibling_scripts("dep"))]\n', id="a slice"),
        pytest.param('import sys\nsys.path += [str(sibling_scripts("dep"))]\n', id="augmented"),
        pytest.param('import sys as system\nsystem.path.append(str(sibling_scripts("dep")))\n', id="append renamed"),
    ],
)
def test_each_way_a_lookup_reaches_sys_path_is_read(source: str) -> None:
    assert [entry.name for entry in sys_path_entries(source)] == ["dep"]
    assert not sys_path_entries(source)[0].hand_written


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('sys.path.insert(0, str(Path(__file__).parents[2] / "repo-cleanup" / "scripts"))\n', id="joined"),
        pytest.param(
            'SIBLING = Path(__file__).resolve().parents[2] / "repo-cleanup" / "scripts"\n'
            "sys.path.insert(0, str(SIBLING))\n",
            id="a constant",
        ),
        pytest.param(
            'sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "repo-cleanup", "scripts"))\n',
            id="os.path.join",
        ),
        pytest.param(
            'sys.path.insert(0, str(Path(__file__).parents[2] / "repo-cleanup/scripts"))\n', id="one joined segment"
        ),
        pytest.param(
            'sys.path.insert(0, str(Path(__file__).parents[2].joinpath("repo-cleanup", "scripts")))\n', id="joinpath"
        ),
    ],
)
def test_each_hand_written_path_to_a_sibling_is_read(source: str) -> None:
    entries = sys_path_entries(f"import sys\nimport os\nfrom pathlib import Path\n{source}")
    assert [ScriptsEntry(3 + len(source.splitlines()), "repo-cleanup", True)] == entries


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import sys\nsys.path.insert(0, str(Path(__file__).parent))\n", id="the own folder"),
        pytest.param(
            'import sys\nsys.path.insert(0, str(Path(__file__).parent / "scripts"))\n', id="the own scripts folder"
        ),
        pytest.param('other = []\nother.insert(0, str(sibling_scripts("dep")))\n', id="another list"),
        pytest.param('p = sibling_scripts("dep")\nprint(p)\n', id="a lookup never added"),
    ],
)
def test_a_path_that_names_no_sibling_and_a_lookup_off_sys_path_are_not_entries(source: str) -> None:
    assert sys_path_entries(source) == []


class RepositoryFilesFixtures(unittest.TestCase):
    @staticmethod
    def repository(path: Path, files: dict[str, str]) -> None:
        write_fixture_tree(path, files)
        for arguments in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "base"]):
            subprocess.run(["git", "-C", str(path), *arguments], check=True, capture_output=True)

    def test_the_files_are_the_checkouts_whatever_repository_git_dir_names(self) -> None:
        # The other repository tracks a file the checkout ignores, so its index would add that file to the list.
        with tempfile.TemporaryDirectory() as temporary:
            checkout, other = Path(temporary) / "checkout", Path(temporary) / "another repository"
            self.repository(checkout, {".gitignore": "ignored.txt\n", "tracked.txt": "tracked\n"})
            self.repository(other, {"ignored.txt": "theirs\n"})
            (checkout / "ignored.txt").write_text("ignored\n", encoding="utf-8")
            (checkout / "untracked.txt").write_text("untracked\n", encoding="utf-8")
            expected = [checkout / ".gitignore", checkout / "tracked.txt", checkout / "untracked.txt"]
            self.assertEqual(expected, sorted(repository_files(checkout)))
            with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
                self.assertEqual(expected, sorted(repository_files(checkout)))
            # Outside any repository git cannot list them, and the failure says so rather than reading as no files.
            with self.assertRaisesRegex(
                ReleaseNotesError, "git ls-files --cached --others --exclude-standard -z failed"
            ):
                repository_files(Path(temporary))
